"""Local status and disposable demo; no live apply/approve CLI shortcut."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import Operator, OperatorError


class _LocalLedgerOnly:
    def __init__(self, target: str):
        self.target = target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CapRover Operator local utilities")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Inspect an existing private ledger; no server requests")
    status.add_argument("--data-dir", type=Path, required=True)
    status.add_argument("--target", required=True, help="Exact CapRover origin recorded in this ledger")
    status.add_argument("--plan-id")
    status.add_argument("--json", action="store_true")
    demo = commands.add_parser("demo", help="Disposable loopback API fixture; no real CapRover or images")
    demo.add_argument("--json", action="store_true")
    demo.add_argument("--output", type=Path, help="Save synthetic JSON evidence at this new path")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "demo":
            from .demo import run_demo
            result = run_demo(emit=None if arguments.json else print)
            if arguments.output:
                with arguments.output.open("x", encoding="utf-8") as output:
                    json.dump(result, output, indent=2, sort_keys=True)
                    output.write("\n")
            if arguments.json:
                print(json.dumps(result, sort_keys=True))
            return 0
        data_dir = arguments.data_dir.absolute()
        if not (data_dir / "ledger.sqlite3").is_file():
            raise OperatorError("ledger_not_found")
        operator = Operator(data_dir, _LocalLedgerOnly(arguments.target))
        try:
            result = operator.status(arguments.plan_id)
        finally:
            operator.close()
        print(json.dumps(result, indent=None if arguments.json else 2, sort_keys=True))
        return 0
    except (OperatorError, OSError) as exc:
        code = getattr(exc, "code", "local_operation_failed")
        print(json.dumps({"error": {"code": code}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
