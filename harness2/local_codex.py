"""Local Codex sessions with bounded calls, isolated workspaces, and durable evidence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .core.proposer import Proposer, ProposerError, Reply, _call_fingerprint
from .project_lessons import lesson_context, load_project_lessons, relevant_project_lessons


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_process(command, *, cwd, timeout, stdin=None, env=None):
    """Kill the complete process group on timeout, including tool subprocesses."""
    with subprocess.Popen(
        command, cwd=cwd, env=env, text=True, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class LocalCodex:
    def __init__(self, root: Path, model: str, *, binary="codex", effort="medium",
                 max_calls=24, timeout=300, use_project_lessons=True):
        if not model.strip() or max_calls < 1 or timeout < 1:
            raise ValueError("model, positive max_calls and positive timeout are required")
        self.root, self.model = Path(root), model
        self.binary, self.effort = binary, effort
        self.max_calls, self.timeout = max_calls, timeout
        # Snapshot once so every execution in this runner sees the same guidance.
        self.project_lessons = load_project_lessons() if use_project_lessons else []
        self._lock = threading.Lock()

    def lessons_for(self, task_prompt: str) -> list[dict]:
        return relevant_project_lessons(task_prompt, self.project_lessons)

    def run(self, prompt: str, workspace: Path, *, timeout=None, task_prompt=None,
            include_project_lessons=True) -> dict:
        lessons = (self.lessons_for(prompt if task_prompt is None else task_prompt)
                   if include_project_lessons else [])
        prompt += lesson_context(lessons)
        with self._lock:
            counter = self.root / "calls.json"
            count = json.loads(counter.read_text())["started"] if counter.exists() else 0
            if count >= self.max_calls:
                raise RuntimeError(
                    f"Codex call budget exhausted ({self.max_calls}); no call started"
                )
            count += 1
            write_json(counter, {"started": count})
        audit = self.root / "sessions" / f"{count:04d}"
        audit.mkdir(parents=True)
        write_json(audit / "project-lessons.json", {"selected": lessons})
        (audit / "prompt.md").write_text(prompt, encoding="utf-8")
        command = [
            self.binary, "exec", "--ignore-user-config", "--skip-git-repo-check",
            "--ephemeral", "--json", "--sandbox", "workspace-write",
            "--model", self.model, "-C", str(workspace),
            "-c", 'approval_policy="never"',
            "-c", f"model_reasoning_effort={json.dumps(self.effort)}",
            "-c", 'web_search="disabled"', "-c", "features.multi_agent=false",
            "-c", "features.apps=false",
            "-c", 'shell_environment_policy.inherit="core"',
            "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c", "sandbox_workspace_write.exclude_slash_tmp=true", "-",
        ]
        started = time.monotonic()
        try:
            result = run_process(command, cwd=workspace, timeout=min(timeout or self.timeout,
                                 self.timeout), stdin=prompt)
        except subprocess.TimeoutExpired:
            write_json(audit / "result.json", {"ok": False, "error": "timeout"})
            raise
        (audit / "events.jsonl").write_text(result.stdout, encoding="utf-8")
        (audit / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        completed = [event for event in events if event.get("type") == "turn.completed"]
        failures = [event for event in events if event.get("type") == "turn.failed"]
        messages = [event["item"]["text"] for event in events
                    if event.get("type") == "item.completed"
                    and event.get("item", {}).get("type") == "agent_message"]
        record = {
            "ok": result.returncode == 0 and bool(completed) and not failures and bool(messages),
            "returncode": result.returncode, "wall_s": round(time.monotonic() - started, 3),
            "usage": completed[-1].get("usage", {}) if completed else {},
            "cost_usd": None, "audit": str(audit), "text": messages[-1] if messages else "",
            "project_lessons": [lesson["id"] for lesson in lessons],
        }
        write_json(audit / "result.json", record)
        if not record["ok"]:
            raise RuntimeError(f"Codex session failed; inspect {audit}")
        return record


class CodexProposer(Proposer):
    """Use the same local Codex runner for proposing, composing, and reflection."""

    def __init__(self, spec, runner: LocalCodex, audit_dir: Path):
        super().__init__(spec, audit_dir=audit_dir, attempts=1)
        self.runner = runner

    def call(self, mission, context_dir, label, *, system="", timeout=None,
             max_attempts=None, harvest=None):
        import shutil

        identity = f"{self.spec.model}/{self.spec.effort}\n{mission}"
        key = _call_fingerprint(label, identity, system, context_dir)
        cached = self._cached(label, key, require_workspace=bool(harvest))
        if cached is not None:
            self._calls.append(cached)
            return cached
        try:
            with tempfile.TemporaryDirectory(prefix="harness2-improve-") as temporary:
                workspace = Path(temporary)
                if context_dir:
                    shutil.copytree(context_dir, workspace / "context")
                prompt = (
                    "This implementation work and its plan are approved. Work autonomously "
                    "inside this workspace. Do not spawn other agents. Only edit context/harness "
                    "when edits are requested; evidence is read-only. Never search outside the "
                    "workspace. Reply with the requested notes or reflection "
                    "as your final message.\n\n"
                    + system + "\n\n" + mission
                )
                result = self.runner.run(prompt, workspace, timeout=timeout,
                                         include_project_lessons=False)
                reply = Reply(label, result["text"], True, wall_s=result["wall_s"],
                              attempts=1, channel="codex_cli")
                if harvest:
                    source = workspace / harvest
                    if any(path.is_symlink() for path in source.rglob("*")):
                        raise ValueError("Proposed harness must not contain symlinks")
                    reply.workspace = self._harvest(workspace, harvest, label, key)
                self._write_audit(reply, mission, system, key)
                self._calls.append(reply)
                return reply
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            # The core retries invalid edits and records failed stages; never return empty success.
            self._calls.append(
                Reply(label, "", False, attempts=1, channel="codex_cli", error=str(exc))
            )
            raise ProposerError(str(exc), attempts=1) from exc
