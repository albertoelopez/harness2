"""Coding tasks and independent promotion checks around Harness²'s recursion engine."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .core import schedule, tree
from .core.library import Library, has_experience
from .core.spec import BenchPrompts, BenchSpec, ChannelSpec
from .local_codex import CodexProposer, LocalCodex, run_process, write_json
from .substrate.codex import CodexSubstrate


def safe_relative(name: str) -> str:
    path = Path(name)
    if not name or path.is_absolute() or any(p in (".", "..") for p in name.split("/")):
        raise ValueError(f"Expected a contained relative path: {name!r}")
    if "\\" in name:
        raise ValueError(f"Backslashes are not supported in paths: {name!r}")
    return name


def plain_files(root: Path) -> dict[str, str]:
    files = {}
    if not root.is_dir():
        raise ValueError(f"Missing directory: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not supported: {path}")
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return files


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_tasks(root: Path) -> dict:
    tasks = {}
    for manifest in sorted(root.glob("*/task.json")):
        if manifest.is_symlink() or manifest.parent.is_symlink():
            raise ValueError("Task manifests must not be symlinks")
        data = json.loads(manifest.read_text())
        task_id = data["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", task_id) or task_id in tasks:
            raise ValueError(f"Invalid or duplicate task id: {task_id!r}")
        if not isinstance(data["prompt"], str) or not data["prompt"].strip():
            raise ValueError(f"Task {task_id} needs a prompt")
        source = manifest.parent / "source"
        if source.is_symlink():
            raise ValueError("Task source must not be a symlink")
        files = plain_files(source)
        if not files:
            raise ValueError(f"Task {task_id} needs source files")
        reserved = {"AGENTS.md", ".agents", ".codex", ".git", "scripts", "_verify.py"}
        if any(Path(name).parts[0] in reserved or Path(name).name.startswith(".env")
               for name in files):
            raise ValueError(
                "Task source must not contain harness, Git, environment or verifier files"
            )
        outputs = data["outputs"]
        if not isinstance(outputs, list) or not outputs or len(set(outputs)) != len(outputs):
            raise ValueError("outputs must be a nonempty list of distinct source paths")
        for name in outputs:
            safe_relative(name)
            if name not in files:
                raise ValueError(f"Output {name} needs a starter file in source/")
        verifier = manifest.parent / "verify.py"
        if verifier.is_symlink():
            raise ValueError("Verifier must not be a symlink")
        verifier_text = verifier.read_text(encoding="utf-8")
        compile(verifier_text, str(verifier), "exec")
        tasks[task_id] = {**data, "source": source, "files": files,
                          "verifier": verifier_text}
    if not tasks:
        raise ValueError(f"No */task.json files found under {root}")
    return tasks


class CodingAdapter:
    def __init__(self, tasks: dict, base: Path, root: Path, runner: LocalCodex):
        self.items, self._base, self.root, self.runner = tasks, base, root, runner
        self.substrate = CodexSubstrate()
        rejected = []
        plain_files(base)
        tree.read_tree(self.substrate.surface(), base, rejected=rejected)
        if rejected:
            raise ValueError(f"Base harness has files outside its editable surface: {rejected}")
        channel = ChannelSpec("codex_cli", runner.model, effort=runner.effort,
                              timeout=runner.timeout)
        self.spec = BenchSpec(
            name="coding", runs_root=root / "runs", install_mode="codex_root",
            prompts=BenchPrompts(
                work="implement Python functions according to their stated behavior",
                inv_levers="Improve reusable coding instructions, skills, and Python helpers.",
                dom_levers="Inspect edge cases, preserve public interfaces, and exercise the code.",
                domain_guidance=("Only AGENTS.md, skills, and Python helper scripts are editable. "
                                 "Do not add hooks, configuration, permissions, or subagents."),
            ), channels={"proposer": channel, "selector": channel},
        )
        self.validate_tree(tree.read_tree(self.substrate.surface(), base))

    def tasks(self, split="all"):
        return list(self.items)

    def domain_of(self, task_id):
        return "coding"

    def task_prompt(self, task_id):
        return self.items[task_id]["prompt"]

    def task_files(self, task_id):
        source = self.root / "sources" / task_id
        for name, content in self.items[task_id]["files"].items():
            dest = source / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        return source

    def base_harness(self):
        return self._base

    def validate_tree(self, harness):
        tree.validate(self.substrate.surface(), harness, allow_subagents=False)
        for name in harness:
            if name != "AGENTS.md" and not name.startswith((".agents/skills/", "scripts/")):
                raise ValueError(f"Only prompts, skills and Python helpers may evolve: {name}")

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        task = self.items[task_id]
        harness = tree.read_tree(self.substrate.surface(), harness_dir or self._base)
        self.validate_tree(harness)
        identity = digest({"task": task["prompt"], "source": task["files"],
                           "outputs": task["outputs"], "harness": harness,
                           "model": self.runner.model, "effort": self.runner.effort})
        run_dir = self.root / "executions" / safe_relative(run_id)
        marker = run_dir / "execution.json"
        if marker.exists():
            record = json.loads(marker.read_text())
            if record["identity"] != identity or not self.artifacts_intact(run_dir):
                raise ValueError(
                    f"Execution inputs or artifacts changed: {run_dir}; use a new output"
                )
            return run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="harness2-code-") as temporary:
            workspace = Path(temporary)
            for name, content in task["files"].items():
                dest = workspace / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
            tree.write_tree(self.substrate.surface(), harness, workspace, stubs=False)
            result = self.runner.run(
                "The plan and implementation are approved. Complete this coding task autonomously "
                "in the current workspace. Do not spawn agents or read outside the workspace. "
                "Keep the harness files intact. Implement the requested files, exercise your work, "
                "and summarize the result.\n\n" + task["prompt"], workspace, timeout=timeout,
            )
            outputs = {}
            for name in task["outputs"]:
                path = workspace / name
                if path.is_symlink() or not path.resolve().is_relative_to(workspace.resolve()):
                    raise ValueError(f"Output escapes workspace: {name}")
                outputs[name] = path.read_text(encoding="utf-8")
            for name, content in outputs.items():
                dest = run_dir / "artifacts" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
        shutil.copyfile(Path(result["audit"]) / "events.jsonl", run_dir / "events.jsonl")
        (run_dir / "answer.md").write_text(result["text"], encoding="utf-8")
        write_json(marker, {"identity": identity, "outputs": digest(outputs),
                           "events": digest((run_dir / "events.jsonl").read_text()),
                           "wall_s": result["wall_s"], "usage": result["usage"],
                           "cost_usd": None})
        return run_dir

    def artifacts_intact(self, run_dir):
        record = json.loads((run_dir / "execution.json").read_text())
        return (record["outputs"] == digest(plain_files(run_dir / "artifacts"))
                and record["events"] == digest((run_dir / "events.jsonl").read_text()))

    def delivered(self, run_dir):
        return (run_dir / "execution.json").is_file() and self.artifacts_intact(run_dir)

    def trajectory(self, run_dir, *, obs_trunc):
        return self.substrate.read_trajectory(run_dir / "events.jsonl", obs_trunc=obs_trunc,
                                             max_chars=100_000)

    def deliverable_files(self, run_dir):
        return plain_files(run_dir / "artifacts")

    def deliverable_text(self, run_dir):
        return "\n\n".join(f"## {name}\n{content}"
                           for name, content in self.deliverable_files(run_dir).items())


def verify(task: dict, artifacts: Path, *, timeout=30) -> dict:
    """Supply pristine verification code only AFTER model execution has finished."""
    outputs = plain_files(artifacts)
    if set(outputs) != set(task["outputs"]):
        raise ValueError("Deliverable file set does not match the task contract")
    with tempfile.TemporaryDirectory(prefix="harness2-check-") as temporary:
        workspace = Path(temporary)
        for name, content in {**task["files"], **outputs}.items():
            dest = workspace / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        verifier = workspace / "_verify.py"
        verifier.write_text(task["verifier"], encoding="utf-8")
        try:
            result = run_process([sys.executable, "-I", str(verifier)], cwd=workspace,
                                 timeout=timeout, env={"PATH": str(Path(sys.executable).parent)})
        except subprocess.TimeoutExpired:
            return {"passed": False, "error": "verification timeout", "outputs": digest(outputs)}
        if verifier.read_text() != task["verifier"]:
            raise ValueError("Verification code was modified during execution")
        return {"passed": result.returncode == 0, "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr, "outputs": digest(outputs)}


def harness_version(root: Path, harness: dict) -> str:
    version = digest(harness)
    target = root / "versions" / version
    surface = CodexSubstrate().surface()
    if target.exists():
        if tree.read_tree(surface, target) != harness:
            raise ValueError(f"Version was modified: {version}")
    else:
        tree.write_tree(surface, harness, target, stubs=False)
    return version


def verify_run(task: dict, run_dir: Path, timeout: int) -> dict:
    result = verify(task, run_dir / "artifacts", timeout=timeout)
    execution = json.loads((run_dir / "execution.json").read_text())
    if result["outputs"] != execution["outputs"]:
        raise ValueError("Executed artifacts changed before verification")
    result["execution"] = {key: execution[key] for key in ("wall_s", "usage", "cost_usd")}
    return result


def rollback(root: Path, version: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", version):
        raise ValueError("Version must be a full SHA-256 identifier from a promotion report")
    harness = tree.read_tree(CodexSubstrate().surface(), root / "versions" / version)
    if not harness or digest(harness) != version:
        raise ValueError("Version missing or modified")
    tree.validate(CodexSubstrate().surface(), harness, allow_subagents=False)
    current = json.loads((root / "active.json").read_text())
    write_json(root / "active.json", {"version": version, "previous": current["version"],
                                      "reason": "manual rollback"})
    # Lessons from a rejected generation must not influence runs after a rollback.
    library = Library(root / "library", "coding", CodexSubstrate().surface())
    library.forget(library.entries())


def run_coding(tasks: dict, holdouts: dict, base: Path, output: Path, runner: LocalCodex,
               *, k=1, mode="sequential", verify_timeout=30) -> dict:
    if set(tasks) & set(holdouts):
        raise ValueError("Training and held-out task IDs must be distinct")
    if k < 1 or not holdouts or verify_timeout < 1:
        raise ValueError("Positive k, verification timeout and held-out tasks are required")
    mode = schedule.mode_token(mode)
    surface = CodexSubstrate().surface()
    initial = tree.read_tree(surface, base)
    adapter = CodingAdapter(tasks, base, output, runner)
    inputs = {"tasks": {key: {k: v for k, v in task.items() if k != "source"}
                         for key, task in tasks.items()},
              "holdouts": {key: {k: v for k, v in task.items() if k != "source"}
                            for key, task in holdouts.items()},
              "base": initial, "model": runner.model, "effort": runner.effort,
              "k": k, "mode": mode, "timeout": runner.timeout, "verify_timeout": verify_timeout}
    identity = digest(inputs)
    manifest = output / "experiment.json"
    if manifest.exists():
        if json.loads(manifest.read_text())["identity"] != identity:
            raise ValueError("Experiment configuration changed; use a new --output directory")
    else:
        write_json(manifest, {"identity": identity, "inputs": inputs})
    initial_version = harness_version(output, initial)
    active_file = output / "active.json"
    if not active_file.exists():
        write_json(active_file, {"version": initial_version, "reason": "initial baseline"})
    library = Library(output / "library", "coding", surface)
    reports = []
    for task_id in tasks:
        active = json.loads(active_file.read_text())["version"]
        active_tree = tree.read_tree(surface, output / "versions" / active)
        if digest(active_tree) != active:
            raise ValueError("Active harness was modified outside version control")
        adapter._base = output / "versions" / active
        # A new active version gets its own cache, so repeated experiments cannot reuse stale work.
        task_root = output / "iterations" / task_id / active
        report_file = task_root / "promotion.json"
        adapter.root = task_root
        adapter.spec = BenchSpec(**{**vars(adapter.spec), "runs_root": task_root / "runs"})
        if report_file.exists():
            report = json.loads(report_file.read_text())
            for run_dir, expected in report["artifacts"].items():
                if digest(plain_files(Path(run_dir) / "artifacts")) != expected:
                    raise ValueError("Saved evaluation artifacts changed; use a new output")
            saved_tree = tree.read_tree(surface, output / "versions" / report["candidate_version"])
            if digest(saved_tree) != report["candidate_version"]:
                raise ValueError("Saved candidate harness changed")
            # Recover a crash between recording the decision and activating its version.
            # An explicit rollback takes precedence over replaying a previous promotion.
            manual_rollback = json.loads(active_file.read_text())["reason"] == "manual rollback"
            if report["accepted"] and not manual_rollback:
                if has_experience(report.get("experience")):
                    library.add(task_id, saved_tree, why=report["why"],
                                label=report["chosen_label"], run_id=report["chosen_run_id"],
                                experience=report["experience"])
                write_json(active_file, {"version": report["candidate_version"], "previous": active,
                                        "reason": "verified promotion", "report": str(report_file)})
            reports.append(report)
            continue
        task_root.mkdir(parents=True, exist_ok=True)
        # Existing evidence is visible to this task; only verified lessons are later committed.
        staging = task_root / "library"
        if staging.exists():
            shutil.rmtree(staging)
        if library.dir.exists():
            shutil.copytree(library.dir, staging / "coding")
        working_library = Library(staging, "coding", surface)
        proposer = CodexProposer(adapter.spec.channels["proposer"], runner, task_root / "audit")
        print(f"[{task_id}] baseline -> propose -> probe -> compose", flush=True)
        result = schedule.run_task(task_id, k, adapter=adapter, prop=proposer,
                                   runs_root=adapter.spec.runs_root, modes=(mode,),
                                   library=working_library)
        if result.status not in ("ok", "skipped"):
            raise RuntimeError(f"Improvement cycle failed for {task_id}: {result.errors}")
        if result.modes[mode].get("fell_back") or any(not call.ok for call in proposer._calls):
            raise RuntimeError(f"Improvement or reflection failed for {task_id}; inspect {task_root}")
        selected = result.selections[mode]
        chosen_dir = Path(selected["chosen_run_dir"])
        work = schedule.Namespace("coding", "cx", mode, k).task_dir(
            adapter.spec.runs_root, task_id
        )
        candidate_dir = work / "harnesses" / selected["chosen_label"]
        candidate = tree.read_tree(surface, candidate_dir)
        adapter.validate_tree(candidate)
        checks, artifacts = {}, {}
        for index, row in enumerate(result.base):
            rd = Path(row["run_dir"])
            checks[f"base{index + 1}"] = verify_run(tasks[task_id], rd, verify_timeout)
            artifacts[str(rd)] = checks[f"base{index + 1}"]["outputs"]
        checks["candidate"] = verify_run(tasks[task_id], chosen_dir, verify_timeout)
        artifacts[str(chosen_dir)] = checks["candidate"]["outputs"]
        holdout_adapter = CodingAdapter(holdouts, adapter._base, task_root / "holdout", runner)
        for holdout_id, task in holdouts.items():
            for label, harness in (("base", adapter._base), ("candidate", candidate_dir)):
                rd = holdout_adapter.execute(holdout_id, harness, f"{holdout_id}/{label}")
                name = f"holdout/{holdout_id}/{label}"
                checks[name] = verify_run(task, rd, verify_timeout)
                artifacts[str(rd)] = checks[name]["outputs"]
        accepted = checks["candidate"]["passed"] and all(
            checks[f"holdout/{name}/candidate"]["passed"] for name in holdouts
        )
        changed = candidate != active_tree
        version = harness_version(output, candidate)
        report = {"task": task_id, "baseline_version": active, "candidate_version": version,
                  "accepted": accepted and changed, "changed": changed, "checks": checks,
                  "artifacts": artifacts, "quality_gain_observed": accepted and any(
                      not check["passed"] for name, check in checks.items()
                      if name.startswith("base") or name.endswith("/base")),
                  "reason": ("unchanged harness" if not changed else
                             "verified" if accepted else "candidate failed independent checks"),
                  "experience": selected.get("experience", {}), "why": selected.get("why", ""),
                  "chosen_label": selected["chosen_label"],
                  "chosen_run_id": selected["chosen_run_id"],
                  "cost_usd": None}
        write_json(report_file, report)
        if report["accepted"]:
            if has_experience(selected.get("experience")):
                library.add(task_id, candidate, why=selected.get("why", ""),
                            label=selected["chosen_label"], run_id=selected["chosen_run_id"],
                            experience=selected["experience"])
            write_json(active_file, {"version": version, "previous": active,
                                    "reason": "verified promotion", "report": str(report_file)})
        reports.append(report)
        print(f"[{task_id}] {'promoted' if report['accepted'] else 'baseline retained'}", flush=True)
    result = {"tasks": reports, "active": json.loads(active_file.read_text()),
              "calls_started": json.loads((runner.root / "calls.json").read_text())["started"],
              "cost_usd": None}
    write_json(output / "report.json", result)
    return result
