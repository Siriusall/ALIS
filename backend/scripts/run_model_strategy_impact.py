from __future__ import annotations

import argparse
import json

from app.services.model_strategy_impact import (
    run_controlled_model_strategy_ab,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the current task-native controlled model strategy A/B.",
    )
    parser.add_argument("model_version_id", type=int)
    args = parser.parse_args()
    result = run_controlled_model_strategy_ab(
        model_version_id=args.model_version_id,
        parallel_workers=5,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
