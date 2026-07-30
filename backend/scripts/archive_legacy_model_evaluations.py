from __future__ import annotations

import argparse
import json

from app.services.model_evaluation_migration import (
    audit_legacy_model_evaluations,
    execute_legacy_model_evaluation_archive,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="archive and remove legacy evaluation evidence",
    )
    args = parser.parse_args()
    result = (
        execute_legacy_model_evaluation_archive()
        if args.execute
        else audit_legacy_model_evaluations()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
