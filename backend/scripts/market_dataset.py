"""Publish or sync the shared immutable market dataset."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.market_dataset import publish_market_dataset, sync_market_dataset, refresh_market_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh", help="DB-local refresh of the existing dataset through latest normalized date")
    refresh.add_argument("--root", type=Path)
    publish = sub.add_parser("publish", help="run on the database host")
    publish.add_argument("--start", required=True, help="YYYYMMDD")
    publish.add_argument("--end", required=True, help="YYYYMMDD")
    publish.add_argument("--root", type=Path)
    publish.add_argument("--refresh-from", default="", help="reuse verified full partitions before YYYYMMDD; omit for full rebuild")
    sync = sub.add_parser("sync", help="run on the research worker")
    sync.add_argument("--source", help="local path or ssh://user@host/absolute/path")
    sync.add_argument("--version")
    sync.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.command == "publish":
        result = publish_market_dataset(args.start, args.end, root=args.root, refresh_from=args.refresh_from)
    elif args.command == "refresh":
        result = refresh_market_dataset(root=args.root)
    else:
        result = sync_market_dataset(source=args.source, version=args.version, root=args.root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
