"""Subprocess entry point used by real kill/recover unittest cases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:  # Executed as a plain script.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from crashfence.durable import PHASE_COUNT, recover_root, run_phases
else:
    from .durable import PHASE_COUNT, recover_root, run_phases


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="crashfence-worker")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run six durable phases")
    run_p.add_argument("--root", required=True)
    run_p.add_argument("--crash-at", type=int, choices=range(1, PHASE_COUNT + 1))
    run_p.add_argument("--ready-file")

    recover_p = sub.add_parser("recover", help="recover a local ledger")
    recover_p.add_argument("--root", required=True)

    args = parser.parse_args(argv)
    if args.command == "run":
        result = run_phases(args.root, args.crash_at, args.ready_file)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "recover":
        print(json.dumps(recover_root(args.root), ensure_ascii=False, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
