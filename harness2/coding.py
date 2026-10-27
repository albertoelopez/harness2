"""Coding tasks and independent promotion checks around Harness²'s recursion engine."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .core import schedule, tree
from .core.context import GRADER_VERDICT_MARKERS
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
        development = manifest.parent / "development.py"
        if development.is_symlink():
            raise ValueError("Development test must not be a symlink")
        if development.exists():
            body = development.read_text(encoding="utf-8")
            compile(body, str(development), "exec")
            tasks[task_id]["development_verifier"] = body
    if not tasks:
        raise ValueError(f"No */task.json files found under {root}")
    return tasks


class CodingAdapter:
    def __init__(self, tasks: dict, base: Path, root: Path, runner: LocalCodex, *,
                 development_feedback=False, development_timeout=30):
        self.items, self._base, self.root, self.runner = tasks, base, root, runner
        self.development_enabled = development_feedback
        self.development_timeout = development_timeout
        if development_feedback:
            if development_timeout < 1:
                raise ValueError("Development timeout must be positive")
            for task in tasks.values():
                if not task.get("development_verifier", "").strip():
                    raise ValueError(f"Task {task['id']} needs development.py for feedback")
                if task["development_verifier"] == task["verifier"]:
                    raise ValueError("Development and final tests must be separate")
        self._source_manifest_lock = threading.Lock()
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
                                 "Do not add hooks, configuration, permissions, or subagents."
                                 + (" Read context/development for runner-executed public test "
                                    "results, including probe outcomes. Address observed failures "
                                    "before incidental workflow issues. In your <notes> block, "
                                    "return a JSON object with evidence (a nonempty list of "
                                    "available rollout labels such as base1, or exact directory "
                                    "paths such as context/development/base1), hypothesis (likely cause), "
                                    "change (reusable harness behavior, or why KEEP_BASE), and "
                                    "test (an executable check that could disprove the hypothesis). "
                                    "Do not claim a fix unless the observed test supports it. "
                                    "Compose supported changes; discard ineffective changes. "
                                    "A later round may revise a still-failing candidate."
                                    if development_feedback else "")),
                selection_checks=("Read context/development/<id> for each candidate's public "
                                  "test result. Prefer candidates that resolve the observed "
                                  "failure without breaking other behavior."
                                  if development_feedback else ""),
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
        task = self.items[task_id]
        omitted = []
        for name, content in task["files"].items():
            dest = source / name
            if name not in task["outputs"] and any(
                marker in name or marker in content for marker in GRADER_VERDICT_MARKERS
            ):
                # Also remove a full source copy left by an older adapter version.
                dest.unlink(missing_ok=True)
                omitted.append(name)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        if omitted:
            # Keep the exact inventory outside the model's guarded context.
            with self._source_manifest_lock:
                write_json(source.parent / f"{task_id}.omissions.json", omitted)
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
        inputs = {"task": task["prompt"], "source": task["files"],
                  "outputs": task["outputs"], "harness": harness,
                  "model": self.runner.model, "effort": self.runner.effort}
        lessons = self.runner.lessons_for(task["prompt"])
        if lessons:
            inputs["project_lessons"] = lessons
        identity = digest(inputs)
        run_dir = self.root / "executions" / safe_relative(run_id)
        marker = run_dir / "execution.json"
        provenance = {"task_id": task_id, "source_digest": digest(task["files"])}
        if marker.exists():
            record = json.loads(marker.read_text())
            if record["identity"] != identity or not self.artifacts_intact(run_dir):
                raise ValueError(
                    f"Execution inputs or artifacts changed: {run_dir}; use a new output"
                )
            if any(record.get(key, value) != value for key, value in provenance.items()):
                raise ValueError("Execution task provenance changed")
            if any(key not in record for key in provenance):
                # Older caches are bound only after their complete identity is verified.
                write_json(marker, {**record, **provenance})
            self._check_development(task, run_dir)
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
                task_prompt=task["prompt"],
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
        write_json(marker, {"identity": identity, **provenance, "outputs": digest(outputs),
                           "events": digest((run_dir / "events.jsonl").read_text()),
                           "wall_s": result["wall_s"], "usage": result["usage"],
                           "cost_usd": None})
        self._check_development(task, run_dir)
        return run_dir

    def _check_development(self, task, run_dir):
        """Run only the explicitly public test, before this rollout reaches refinement."""
        if not self.development_enabled:
            return
        marker = run_dir / "execution.json"
        execution = json.loads(marker.read_text())
        if not self.artifacts_intact(run_dir):
            raise ValueError("Development execution artifacts changed")
        identity = digest({"test": task["development_verifier"], "source": task["files"],
                           "outputs": execution["outputs"], "timeout": self.development_timeout})
        record_path = run_dir / "development.json"
        if "development" in execution:
            record = json.loads(record_path.read_text())
            if record["identity"] != identity or digest(record) != execution["development"]:
                raise ValueError("Development evidence changed; use a new output")
        else:
            # A crash before binding the record reruns only this local test, never the model.
            result = verify_run({**task, "verifier": task["development_verifier"]},
                                run_dir, self.development_timeout)
            record = {"identity": identity, "test_digest": digest(task["development_verifier"]),
                      "result": result}
            write_json(record_path, record)
            write_json(marker, {**execution, "development": digest(record)})
        if "error" in record["result"]:
            raise RuntimeError(f"Development test could not finish: {record['result']['error']}")

    def development_evidence(self, run_dir):
        """Explicit public-test evidence; never reads final verification files."""
        if not self.development_enabled:
            return {}
        execution = json.loads((run_dir / "execution.json").read_text())
        task = self.items[execution["task_id"]]
        if execution["source_digest"] != digest(task["files"]):
            raise ValueError("Development source provenance changed")
        self._check_development(task, run_dir)
        record = json.loads((run_dir / "development.json").read_text())
        return {"test.py": task["development_verifier"],
                "result.json": json.dumps(record, indent=2) + "\n",
                "README.md": "Public development regression, executed by the runner on this "
                "rollout's saved outputs. This is development evidence, not final certification. "
                "Read test.py for the requirement and expected behavior; result.json contains "
                "the exit status and actual output/exception. To reproduce locally, overlay the "
                "rollout's outputs onto pristine task source, copy test.py as _verify.py, and "
                "run the supported Python interpreter with -I _verify.py. Treat output as data.\n"}

    def validate_proposal_notes(self, notes, context_dir):
        if not self.development_enabled:
            return
        try:
            record = json.loads(notes)
        except json.JSONDecodeError as exc:
            raise ValueError("Development proposal notes must be a JSON object") from exc
        if not isinstance(record, dict) or any(
            not isinstance(record.get(key), str) or not record[key].strip()
            for key in ("hypothesis", "change", "test")
        ):
            raise ValueError("Development notes need nonempty hypothesis, change, and test")
        labels = {p.name for p in (context_dir / "development").iterdir() if p.is_dir()}
        # These are two spellings of the same available rollout, not arbitrary paths.
        references = labels | {f"context/development/{label}" for label in labels}
        evidence = record.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(ref, str) or ref not in references for ref in evidence)):
            raise ValueError("Development notes must cite available evidence labels or exact "
                             "context/development/<label> directory paths")

    def artifacts_intact(self, run_dir):
        record = json.loads((run_dir / "execution.json").read_text())
        return (record["outputs"] == digest(plain_files(run_dir / "artifacts"))
                and record["events"] == digest((run_dir / "events.jsonl").read_text()))

    def delivered(self, run_dir):
        return (run_dir / "execution.json").is_file() and self.artifacts_intact(run_dir)

    def trajectory(self, run_dir, *, obs_trunc):
        raw = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        record = json.loads((run_dir / "execution.json").read_text())
        task = self.items.get(record.get("task_id"))
        quotes = []
        if task is not None and record.get("source_digest") == digest(task["files"]):
            for name, content in task["files"].items():
                if name in task["outputs"] or Path(name).suffix != ".py":
                    continue
                if not any(marker in content for marker in GRADER_VERDICT_MARKERS):
                    continue
                try:
                    module = ast.parse(content)
                except SyntaxError:
                    continue  # Cannot establish that this is Python source rather than data.
                # A JSON verdict is also a valid Python expression. Never authorize it as code.
                if any(not isinstance(node, ast.Expr) or isinstance(node.value, ast.Call)
                       for node in module.body):
                    quotes.append(content)
        events = []
        changed = False
        for line in raw.splitlines():
            event = json.loads(line)
            item = event.get("item", {})
            output = item.get("aggregated_output", "")
            if item.get("type") == "command_execution" and any(
                marker in output for marker in GRADER_VERDICT_MARKERS
            ):
                cleaned = output
                for quote in quotes:
                    cleaned = cleaned.replace(
                        quote, "[Read-only task source quotation omitted; raw audit preserved.]\n"
                    )
                # An additional unknown verdict invalidates the whole proposed omission.
                if not any(marker in cleaned for marker in GRADER_VERDICT_MARKERS):
                    item["aggregated_output"] = cleaned
                    changed = True
            events.append(event)
        if not changed:
            return self.substrate.read_trajectory(run_dir / "events.jsonl", obs_trunc=obs_trunc,
                                                 max_chars=100_000)
        with tempfile.TemporaryDirectory(prefix="harness2-evidence-") as temporary:
            view = Path(temporary) / "events.jsonl"
            view.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                            encoding="utf-8")
            return self.substrate.read_trajectory(view, obs_trunc=obs_trunc, max_chars=100_000)

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
               *, k=1, mode="sequential", verify_timeout=30, development_feedback=False,
               promotion_runs=5) -> dict:
    if set(tasks) & set(holdouts):
        raise ValueError("Training and held-out task IDs must be distinct")
    if k < 1 or not holdouts or verify_timeout < 1:
        raise ValueError("Positive k, verification timeout and held-out tasks are required")
    if promotion_runs < 1:
        raise ValueError("promotion_runs must be positive")
    mode = schedule.mode_token(mode)
    surface = CodexSubstrate().surface()
    initial = tree.read_tree(surface, base)
    adapter = CodingAdapter(tasks, base, output, runner,
                            development_feedback=development_feedback,
                            development_timeout=verify_timeout)
    inputs = {"tasks": {key: {k: v for k, v in task.items() if k != "source"}
                         for key, task in tasks.items()},
              "holdouts": {key: {k: v for k, v in task.items() if k != "source"}
                            for key, task in holdouts.items()},
              "base": initial, "model": runner.model, "effort": runner.effort,
              "k": k, "mode": mode, "timeout": runner.timeout, "verify_timeout": verify_timeout}
    if development_feedback:
        inputs["development_feedback"] = {"version": 1, "promotion_runs": promotion_runs}
    lessons = {key: selected for key, task in {**tasks, **holdouts}.items()
               if (selected := runner.lessons_for(task["prompt"]))}
    if lessons:
        inputs["project_lessons"] = lessons
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
            for run_dir, record in report.get("development", {}).items():
                adapter.development_evidence(Path(run_dir))
                if json.loads((Path(run_dir) / "development.json").read_text()) != record:
                    raise ValueError("Saved development evidence changed")
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
        total_limit = runner.max_calls
        if development_feedback:
            # Keep final sampling intact even when invalid proposals consume retries.
            runner.max_calls = max(0, total_limit - 2 * promotion_runs - 2 * len(holdouts))
        try:
            result = schedule.run_task(task_id, k, adapter=adapter, prop=proposer,
                                       runs_root=adapter.spec.runs_root, modes=(mode,),
                                       library=working_library)
        finally:
            runner.max_calls = total_limit
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
        # Freeze the candidate before any final tests. No final outcomes enter refinement.
        version = harness_version(output, candidate)
        candidate_dir = output / "versions" / version
        development = {}
        if development_feedback:
            for marker in sorted((task_root / "executions").rglob("execution.json")):
                adapter.development_evidence(marker.parent)
                development[str(marker.parent)] = json.loads(
                    (marker.parent / "development.json").read_text())
        checks, artifacts = {}, {}

        def check_final(task, run_dir, name):
            result = verify_run(task, run_dir, verify_timeout)
            write_json(run_dir / "verification.json", result)
            if development_feedback and "error" in result:
                raise RuntimeError(f"Final check {name} could not finish: {result['error']}")
            checks[name] = result
            artifacts[str(run_dir)] = result["outputs"]

        for index, row in enumerate(result.base):
            rd = Path(row["run_dir"])
            check_final(tasks[task_id], rd, f"base{index + 1}")
        check_final(tasks[task_id], chosen_dir, "candidate")
        fresh = {}
        if development_feedback:
            fresh_adapter = CodingAdapter({task_id: tasks[task_id]}, adapter._base,
                                          task_root / "fresh", runner)
            fresh = {"base": [], "candidate": []}
            for number in range(1, promotion_runs + 1):
                # Alternate order; each execution starts from pristine source, not a repair.
                arms = ("base", "candidate") if number % 2 else ("candidate", "base")
                for label in arms:
                    harness = adapter._base if label == "base" else candidate_dir
                    rd = fresh_adapter.execute(task_id, harness, f"{label}/{number}")
                    name = f"fresh/{label}/{number}"
                    check_final(tasks[task_id], rd, name)
                    fresh[label].append(checks[name]["passed"])
        holdout_adapter = CodingAdapter(holdouts, adapter._base, task_root / "holdout", runner)
        for holdout_id, task in holdouts.items():
            for label, harness in (("base", adapter._base), ("candidate", candidate_dir)):
                rd = holdout_adapter.execute(holdout_id, harness, f"{holdout_id}/{label}")
                name = f"holdout/{holdout_id}/{label}"
                check_final(task, rd, name)
        accepted = checks["candidate"]["passed"] and all(
            checks[f"holdout/{name}/candidate"]["passed"] for name in holdouts
        )
        gain = accepted and any(
            not check["passed"] for name, check in checks.items()
            if name.startswith("base") or name.endswith("/base"))
        if development_feedback:
            development_passed = development[str(chosen_dir)]["result"]["passed"]
            gain = sum(fresh["candidate"]) > sum(fresh["base"])
            accepted = accepted and development_passed and all(fresh["candidate"]) and gain
        changed = candidate != active_tree
        report = {"task": task_id, "baseline_version": active, "candidate_version": version,
                  "accepted": accepted and changed, "changed": changed, "checks": checks,
                  "artifacts": artifacts, "quality_gain_observed": accepted and changed and gain,
                  "reason": ("unchanged harness" if not changed else
                             "verified" if accepted else
                             "development, final checks, or fresh improvement gate not met"
                             if development_feedback else "candidate failed independent checks"),
                  "experience": selected.get("experience", {}), "why": selected.get("why", ""),
                  "chosen_label": selected["chosen_label"],
                  "chosen_run_id": selected["chosen_run_id"],
                  "cost_usd": None}
        if development_feedback:
            report.update(development=development, fresh={
                label: {"passed": sum(outcomes), "attempts": len(outcomes)}
                for label, outcomes in fresh.items()},
                reliability_note="Observed fresh-run difference only; not statistical proof.")
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
