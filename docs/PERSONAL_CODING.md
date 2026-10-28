# Alberto's self-evolving coding harness

This extension lives in `albertoelopez/harness2`. It uses the existing Harness²
Propose–Probe–Compose engine and experience library, with the installed Codex CLI
for both coding and harness improvement. Model weights stay fixed. No additional
Python dependencies are required for this workflow.

## Run locally

Use Python 3.12 or 3.13 and a recent Codex CLI with `--ignore-user-config`,
`--ephemeral`, `--json`, and `--sandbox workspace-write` support. Authenticate with
`codex login` before running. The local workflow uses that login directly; it does
not need the research benchmark's Vertex bridge or OpenCode installation.

From the repository root, set `HARNESS2_CODEX_MODEL` to a model your account can
use (or pass `--model` explicitly), then validate inputs without calling a model:

```sh
python3.12 -m harness2.cli.coding run \
  --tasks examples/coding/tasks \
  --holdout examples/coding/holdout \
  --base examples/coding/base \
  --output runs/personal-coding \
  --dry-run
```

Remove `--dry-run` to run the cycle. `harness2-coding` is the equivalent command
when the package is installed. Defaults are one sequential refinement step,
24 total Codex session starts per output folder, 300 seconds per model session,
and 30 seconds per independent verification process. Set `--max-calls`,
`--timeout`, `--verify-timeout`, `--k`, and `--mode` explicitly to change these.

A normal one-step cycle with one training and one held-out task needs ten model
sessions: four task executions, three harness edits, one reflection, and two
held-out executions. Invalid edits can require additional sessions. Calls consume
your configured Codex account's allowance. The call limit persists across restarts
and applies to execution, improvement, reflection, and held-out runs together.
Token usage and elapsed execution time are recorded; dollar cost is **unknown**
(`null`), not zero. The upstream research summary's legacy `usd` fields do not
provide a price estimate for these local sessions. Call and timeout limits are
not a monetary cap.

## What evolves and what remains controlled by the runner

The starting harness is `examples/coding/base/AGENTS.md`. A model can revise:

- `AGENTS.md`: reusable coding instructions.
- `.agents/skills/<name>/SKILL.md` and supported skill scripts.
- `scripts/<name>.py`: reusable Python helpers.

The adapter rejects hooks, executable configuration, subagent definitions, and
files outside this surface. CLI permissions, call limits, task manifests, and
independent verifiers are not part of the editable harness. Codex runs in fresh
temporary workspaces with workspace-write sandboxing, web search disabled, and
without loading user config. Authentication still uses the existing Codex login.
User/global instructions may still apply. The runner explicitly authorizes the
bounded coding task and prohibits delegation in its task prompts.

By default, each cycle executes the active baseline, proposes general and task-specific
changes, probes them, and composes a candidate through the upstream engine.
The model sees execution evidence, source material, and earlier accepted lessons.
Independent verifier code is never copied into its workspace or improvement
context. Only after composition does the runner test the baseline and candidate.
The candidate must pass **every** independent check on its training task and on
all held-out tasks. Failed checks retain the active baseline and exclude the
candidate's lesson from the persistent library.

When read-only task source contains evaluator vocabulary, the adapter omits that
source from refinement context and records the omitted paths outside that context.
Complete, exact quotations of Python source can also be omitted from the rendered
trajectory when execution provenance matches the task's source snapshot. Partial
quotations and unrecognized verdict output still reach the unchanged verdict
check. Original execution source and raw event logs remain intact. This handling
concerns benchmark evidence only; it does not change model access or platform
safety checks.

A changed candidate that passes every check becomes a versioned active harness.
If both baseline and candidate pass, the report says no quality gain was observed:
a passing promotion is evidence of retained correctness on those checks, not proof
of a general improvement. Held-out results are stored separately from the model's
reflection. Reusing a held-out set indefinitely weakens its value; use additional
unseen tasks when measuring generalization.

## Learn from public development failures

Opt in with `--development-feedback`. Each training task must then have a trusted
`development.py` beside its private `verify.py`. The development script is an
explicitly public regression: assert the requirement, expected result, and actual
result with a useful failure message. The signed-total example includes one.
Do not copy the private verifier into this file. The loader rejects identical
scripts, but that is not a substitute for designing separate cases.

The runner executes this public script against saved baseline and probe outputs
**before** proposals or composition consume them. Refinement receives
`context/development/<label>/test.py`, `result.json`, and reproduction instructions.
The result includes the actual exit status, output, exception, and artifact/test
fingerprints. Evidence is cached and checked for changes on reuse. A development
timeout is an incomplete execution and raises an error; it is not a behavior failure.
Public tests and results are not injected into coding workspaces. Fresh coding
attempts receive only the task, source, and selected harness.

Every proposal and composition must return a `<notes>` block containing JSON:

```json
{
  "evidence": ["base1"],
  "hypothesis": "Likely cause supported by the observed failure",
  "change": "Reusable harness behavior to address it, or a reason to KEEP_BASE",
  "test": "An executable check that could disprove the hypothesis"
}
```

Evidence entries may use a rollout label (`base1`) or its exact public-evidence
directory path (`context/development/base1`). Both identify the same available
rollout without a format-only retry. Unknown labels, file paths, and other path
spellings remain invalid; the original citation is preserved in decision notes.

The runner validates the fields and evidence references; it cannot guarantee that
the model's causal explanation is correct. Invalid notes receive the engine's
bounded retry, and exhausted retries prevent promotion. The composer sees the
probe results. With `--mode sequential --k 2`, the second round also sees the first
candidate's development outcome and can revise it. This is a fixed two-round
budget, not an unbounded retry-until-pass loop. Selection sees public results under
anonymous candidate IDs. Existing private-verdict tripwires remain enabled.

After refinement and reflection finish, the candidate is frozen. Only then does
the runner execute private `verify.py` on the original outputs and on
`--promotion-runs` fresh coding attempts **per harness** (default five), alternating
baseline/candidate order. It also checks both harnesses on each held-out task.
These final test scripts and outcomes never enter refinement or reflection.
Final verification timeouts stop the experiment without a reliability verdict.

In feedback mode, promotion requires a changed harness, a passing development
candidate, passing original and fresh candidate final checks, passing candidate
holdouts, and strictly more fresh successes than the baseline. Ties retain the
baseline. Reports separate development evidence, original checks, and fresh-run
counts. A five-attempt difference is preliminary evidence, not statistical proof.

For a new failure-driven study, turn the already-observed failure into a public
development regression, and freeze new private variants and related held-out
tasks before launching models. Results from previously exposed tests measure
repair, not unseen generalization. Never replace the prior experiment's frozen
files; use a new task suite and output directory.

An example input validation command (no model calls):

```sh
python3.12 -m harness2.cli.coding run \
  --tasks examples/coding/tasks \
  --holdout examples/coding/holdout \
  --base examples/coding/base \
  --output runs/personal-coding-feedback \
  --development-feedback --mode sequential --k 2 \
  --promotion-runs 5 --max-calls 32 --dry-run
```

Set `HARNESS2_CODEX_MODEL` or pass `--model` as above. Enabling feedback, changing
its tests, or changing the fresh-run count requires a new output directory.
Existing runs without the flag keep the original feedback-free behavior.

For the next real reliability study, budget **30 planned model sessions, capped
at 32**: 16 for two refinement rounds (eight coding executions, six edits, one
selection, one reflection), ten for five fresh attempts per harness, and four for
two related held-out tasks under both harnesses. The remaining two slots cover
invalid-proposal retries, not extra sampling until a favorable result appears.
The runner reserves the fresh and held-out execution slots during refinement;
if refinement exhausts its share, it stops rather than consuming that reserve.
With only one held-out task, the planned total is 28. Local development/final
tests consume no model sessions. This recommendation does not launch or authorize
a new paid/account-usage experiment.

## Add your own tasks

Training and held-out roots each contain task folders:

```text
my-tasks/
  example/
    task.json
    source/
      solution.py
    verify.py
    development.py  # optional public regression; required with --development-feedback
```

`task.json` declares the input and exact deliverable paths:

```json
{
  "id": "example",
  "prompt": "Implement the behavior and public API described here.",
  "outputs": ["solution.py"]
}
```

Every declared output needs a starter file in `source/`. Other source files are
available to the model, but only declared outputs are collected for verification.
Source files must be UTF-8 text; symlinks, Git metadata, environment files, and
harness configuration in the source root are rejected. Task IDs must be distinct
between training and held-out sets. The initial supported workload is Python code
using dependencies already installed in the selected Python environment.

`verify.py` is trusted code that exits zero only if all checks pass. It executes
with `python -I` in a fresh directory containing pristine supporting files and the
collected outputs. Use `runpy.run_path` or an explicit import path to load the
solution, as the interpreter's isolated mode does not add the working directory
to `sys.path`. Verifier failures and timeouts reject promotion. A verifier must
actually assert behavior: a script that returns zero unconditionally is not an
evaluation. The included task fixtures demonstrate real assertions.

Only run trusted task suites locally. Independent verification is a fresh process,
not a container security boundary for hostile generated code. Do not put secrets
in task sources or prompts. This first version is not a secure evaluation service
for untrusted third-party submissions.

## Reviewed project lessons

Real-project fixes can supply useful development guidance without establishing that
an evolved harness is more reliable. Reviewed records live in
`harness2/lessons/*.json`, separately from the experimentally promoted
`library/coding/`. Each record includes a situation cue, an actionable lesson,
public development evidence, a confidence limit, and `verified_project_lesson`
status. Editing this catalog does not promote a harness or create an experience
library entry.

The first record, `audio-device-snapshot.json`, cites the public transcribeYouTube
fix and its changing-probe regression. It advises using the same external-state
snapshot for a decision and its diagnostic, preserving explicit empty snapshots
and standalone discovery. Transfer to other projects is not yet measured.

The second record, `vision-model-preflight.json`, cites the public vision-model
fix and its regression tests. It advises checking a requested optional capability
before expensive work, distinguishing missing models from unavailable services,
and preserving independent output with a clear warning. For example, “Check the
installed vision model before batch processing” retrieves it; “Improve vision
model caption quality” does not. Runtime fallback still matters because an early
check cannot guarantee later availability. This pilot verified the fix, not a
reliability advantage over a lesson-disabled harness.

The third record, `translation-output-cleanup.json`, cites the public translation
cleanup fix and its reused-directory regressions. It advises clearing only a
requested operation's known stale outputs once at startup, preserving unrelated
files, and propagating cleanup errors. “Fix stale translated transcripts when
reusing an output directory” retrieves it; “Improve translation quality” does not.
Incremental flushes must preserve newly generated output. Apply the advice only
when the task's retention requirements call for replacing prior results. The
baseline fixed this issue on its first attempt; transfer reliability is unmeasured.

`LocalCodex` automatically appends relevant lessons to coding execution prompts.
Matching is conservative and deterministic: after normalizing case, punctuation,
and underscores, the **public task description** must contain a whole phrase from
every `match_all` group. For example, “Fix contradictory audio-device error
messages” and “Fix inconsistent service availability diagnostics” match;
“Implement signed_total” and “Add audio-device recording support” do not. This is
phrase matching, not semantic understanding; unlisted paraphrases can miss a
useful lesson. Keep the task description specific to the actual issue.

The coding adapter selects from `task.json`'s `prompt` only, never source code,
verifiers, or evaluation results. For direct project executions, `runner.run(prompt,
workspace)` uses the prompt; supply `task_prompt=description` when it also contains
unrelated background. Proposer, composer, selector, and reflection sessions do not
automatically retrieve project lessons from their mission text. Existing library
and public development-evidence flows remain unchanged.

Each session saves the exact injected records in `project-lessons.json` alongside
its final `prompt.md`. Relevant records are included in execution and experiment
identities, including the public descriptions of held-out tasks. A changed or
disabled lesson requires a new output directory for affected experiments. Each
runner snapshots the catalog at construction. Both comparison arms receive the
same catalog; this is not evidence of an evolved-harness advantage. To explicitly
run a control without these lessons, construct `LocalCodex(...,
use_project_lessons=False)` and use a separate output directory.

The catalog is packaged with the Python distribution and uses no additional
dependencies. Its retrieval, subprocess delivery, audit, and cache behavior are
covered by the full test suite.

## Evidence, continuation, and rollback

The output folder contains:

| Path | Contents |
| --- | --- |
| `report.json` | Latest invocation's decisions and active version |
| `active.json` | Active version ID and previous version |
| `versions/<sha256>/` | Versioned harness files, checked for modifications when reused |
| `library/coding/` | Accepted harnesses and supported lessons for future tasks |
| `iterations/<task>/<baseline-version>/` | Upstream evidence, composition, checks, and promotion report |
| `sessions/<number>/` | Model prompt, JSON events, stderr, elapsed time, and token usage |
| `calls.json` | Total model sessions started, including failed calls |
| `experiment.json` | Fixed experiment inputs and their fingerprint |

In feedback mode, each refinement execution also has `development.json`, bound
to `execution.json` by a digest. Private checks are saved as `verification.json`
outside model context. The promotion report adds `development` records and a
`fresh` summary; individual final results are under `checks/fresh/<arm>/<number>`.

The original source checkout is never edited by coding rollouts. The CLI locks
its output folder against concurrent runs. Completed executions are reused only
when their input identity and artifacts match. Changing model, tasks, verifiers,
base harness, or recursion settings requires a new output directory; raising the
call limit is allowed to finish a budget-limited run.

Repeating the command continues from the active version. A successful promotion
can therefore start a new improvement cycle on the next invocation. An unchanged
baseline with a completed decision reuses that decision without new model calls.
Interrupted cycles reuse completed model calls and executions. A recorded
promotion is recovered if activation was interrupted. Explicit rollback takes
precedence over recovering an older promotion.

To restore a version ID from a report:

```sh
python3.12 -m harness2.cli.coding rollback \
  --output runs/personal-coding \
  --version <full-sha256-from-report>
```

Rollback keeps all evidence and versions, changes the active pointer, and clears
learned lessons so rejected-generation advice does not carry forward.

## Verification

The full suite includes the upstream workflow tests, unit tests, and subprocess
end-to-end tests using a deterministic Codex CLI fixture:

```sh
python3.12 -m unittest discover -s tests -v
```

The fixture covers both recursion modes, cross-task experience, failed model
sessions, rejected held-out regressions, call limits, immutable evaluation inputs,
artifact integrity, rollback, and the public command. It does not prove a real
model improves. Run the command above without `--dry-run` for a model-backed
experiment, then inspect the baseline/candidate checks and `quality_gain_observed`.
Feedback tests additionally cover public evidence arriving before composition,
a second round resolving an earlier failure, anonymous selection using public
results, private-result isolation, altered evidence, incomplete checks, and
fresh-run promotion gates. These fixtures exercise the mechanism, not real model
learning or general reliability.
An additional subprocess regression uses directory-path evidence citations through
two refinement rounds, model selection, reflection, five fresh attempts per harness,
and two held-out tasks. It completes in 30 fixture sessions under a 32-session cap
and checks that the reflected lesson is persisted only after final gates pass.
