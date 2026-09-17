#!/usr/bin/env python
"""Print the non-persisting 0914/0916 tiered production-path comparison."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.tiered_offline_replay import run_tiered_offline_replay_comparison


if __name__ == "__main__":
    print(json.dumps(run_tiered_offline_replay_comparison(), ensure_ascii=False, indent=2, sort_keys=True))
