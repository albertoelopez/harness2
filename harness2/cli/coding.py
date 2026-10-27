"""Evolve a personal Codex coding harness using independently verified tasks."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

from ..coding import load_tasks, rollback, run_coding
from ..local_codex import LocalCodex


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run Propose–Probe–Compose and independent checks")
    run.add_argument("--tasks", type=Path, required=True)
    run.add_argument("--holdout", type=Path, required=True)
    run.add_argument("--base", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--model", default=os.environ.get("HARNESS2_CODEX_MODEL"))
    run.add_argument("--codex", default="codex", help="Installed Codex executable")
    run.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
    run.add_argument("--k", type=int, default=1)
    run.add_argument("--mode", choices=("sequential", "parallel"), default="sequential")
    run.add_argument("--max-calls", type=int, default=24,
                     help="Total session starts per output folder")
    run.add_argument("--timeout", type=int, default=300, help="Seconds per model session")
    run.add_argument("--verify-timeout", type=int, default=30)
    run.add_argument("--development-feedback", action="store_true",
                     help="Feed public development.py results to refinement; keep verify.py private")
    run.add_argument("--promotion-runs", type=int, default=5,
                     help="Fresh attempts per harness before promotion with development feedback")
    run.add_argument("--dry-run", action="store_true", help="Validate inputs without model calls")
    restore = commands.add_parser("rollback",
                                  help="Activate a saved version and clear learned lessons")
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if args.command == "run":
        if not args.model:
            parser.error("pass --model or set HARNESS2_CODEX_MODEL")
        tasks, holdouts = load_tasks(args.tasks.resolve()), load_tasks(args.holdout.resolve())
        if set(tasks) & set(holdouts):
            parser.error("training and held-out task IDs must be distinct")
        if min(args.k, args.max_calls, args.timeout, args.verify_timeout, args.promotion_runs) < 1:
            parser.error("k, max-calls, promotion-runs and timeouts must be positive")
        runner = LocalCodex(output, args.model, binary=args.codex, effort=args.effort,
                            max_calls=args.max_calls, timeout=args.timeout)
        from ..coding import CodingAdapter
        CodingAdapter(tasks, args.base.resolve(), output, runner,
                      development_feedback=args.development_feedback,
                      development_timeout=args.verify_timeout)
        if args.dry_run:
            print(f"Validated {len(tasks)} coding task(s), {len(holdouts)} held-out task(s).")
            print(f"Codex model: {args.model}; {args.mode}; k={args.k}; max calls={args.max_calls}")
            return 0
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another coding run owns {output}") from exc
        if args.command == "rollback":
            rollback(output, args.version)
            print(f"Active harness restored to {args.version}")
        else:
            result = run_coding(tasks, holdouts, args.base.resolve(), output, runner,
                                k=args.k, mode=args.mode, verify_timeout=args.verify_timeout,
                                development_feedback=args.development_feedback,
                                promotion_runs=args.promotion_runs)
            print(f"Report: {output / 'report.json'}")
            print(f"Active harness: {output / 'versions' / result['active']['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
