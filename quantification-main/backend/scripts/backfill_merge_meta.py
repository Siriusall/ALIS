from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="手工执行 all_in_one 历史 merge_meta 补齐")
    parser.add_argument("--force", action="store_true", help="忽略 marker，强制再次全量扫描")
    args = parser.parse_args()

    # 允许从 backend/scripts 直接执行：python scripts/backfill_merge_meta.py
    backend_dir = Path(__file__).resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from app.services.cache_consolidation import backfill_merge_meta_for_existing_all_in_one

    result = backfill_merge_meta_for_existing_all_in_one(force=bool(args.force))
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
