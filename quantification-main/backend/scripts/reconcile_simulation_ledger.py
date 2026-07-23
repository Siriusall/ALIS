#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from app.services.simulation_ledger_audit import reconcile_current_projection


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or register an explicit simulation-ledger migration baseline.",
    )
    parser.add_argument("--execute", action="store_true", help="persist the proposed reconciliation")
    parser.add_argument(
        "--reason",
        default="",
        help="required with --execute; explains why the historical projection is adopted",
    )
    args = parser.parse_args()
    result = reconcile_current_projection(reason=args.reason, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
