"""UTF-8 JSON command line interface for Crashfence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .fixtures import write_fixtures
from .tracecheck import check_trace
from .model import simulate_scenario


def _load_json(path: str | Path) -> Any:
    if str(path) == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_json(value: Any, output: str | None = None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False)
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8", newline="\n")
    else:
        print(text)


def _merge_steps(scenario: dict[str, Any], steps_doc: Any) -> dict[str, Any]:
    if isinstance(steps_doc, list):
        scenario = dict(scenario)
        scenario["steps"] = steps_doc
    elif isinstance(steps_doc, dict):
        merged = dict(scenario)
        merged.update(steps_doc)
        if "steps" in scenario and "steps" not in steps_doc:
            merged["steps"] = scenario["steps"]
        scenario = merged
    else:
        raise ValueError("STEPS file must be a JSON array or object")
    return scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crashfence",
        description="Offline deterministic crash/fencing/policy-recovery simulator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sim = sub.add_parser("simulate", help="simulate SCENARIO, optionally overriding steps with STEPS")
    sim.add_argument("scenario", metavar="SCENARIO", help="UTF-8 JSON scenario path")
    sim.add_argument("steps", metavar="STEPS", nargs="?", help="optional JSON steps array/object")
    sim.add_argument("-o", "--output", help="write report to this UTF-8 JSON file")
    sim.add_argument(
        "--compact-state",
        action="store_true",
        help="omit per-step full safe_state snapshots",
    )
    sim.add_argument(
        "--strict",
        action="store_true",
        help="exit 3 when the report status is fatal_violation",
    )

    check = sub.add_parser("check", help="check an offline TRACE and complete missing entries")
    check.add_argument("trace", metavar="TRACE", help="UTF-8 JSON trace path")
    check.add_argument("-o", "--output", help="write report to this UTF-8 JSON file")
    check.add_argument(
        "--include-simulations",
        action="store_true",
        help="embed full simulation reports for every legal history",
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help="exit 4 for no_legal_history and 5 for ambiguous_authorization",
    )

    fix = sub.add_parser("fixtures", help="write synthetic JSON fixtures into a directory")
    fix.add_argument("directory", nargs="?", default="fixtures", help="output directory")

    phase = sub.add_parser("phase-worker", help=argparse.SUPPRESS)
    phase.add_argument("worker_args", nargs=argparse.REMAINDER)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "simulate":
            scenario = _load_json(args.scenario)
            if args.steps:
                scenario = _merge_steps(scenario, _load_json(args.steps))
            report = simulate_scenario(scenario)
            if args.compact_state:
                for step in report.get("steps", []):
                    step.pop("safe_state", None)
            _print_json(report, args.output)
            if args.strict and report["status"] == "fatal_violation":
                return 3
            return 0

        if args.command == "check":
            doc = _load_json(args.trace)
            report = check_trace(doc)
            if args.include_simulations:
                from .tracecheck import enumerate_histories

                embedded = enumerate_histories(doc, include_simulations=True)
                by_index = {h["history_index"]: h for h in embedded["legal_histories"]}
                for h in report["histories"]:
                    full = by_index.get(h["history_index"])
                    if full and "simulation" in full:
                        h["simulation"] = full["simulation"]
            _print_json(report, args.output)
            if args.strict and report["status"] == "no_legal_history":
                return 4
            if args.strict and report["status"] == "ambiguous_authorization":
                return 5
            return 0

        if args.command == "phase-worker":
            from .worker import main as worker_main

            return worker_main(args.worker_args)

        if args.command == "fixtures":
            written = write_fixtures(args.directory)
            _print_json({"written": written}, None)
            return 0

        parser.error(f"unknown command {args.command}")
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _print_json({"status": "cli_error", "error": str(exc)}, None)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
