#!/usr/bin/env python3
"""Audit or resumably fill ALIS Champion OOS permanent hard data."""

from __future__ import annotations

import argparse
import json

from app.data_platform.services.assets import get_data_assets, invalidate_data_assets_cache
from app.data_platform.services.oos_hard_data import audit_oos_hard_data, repair_oos_hard_data_batch
from app.data_platform.services.readiness import invalidate_readiness_cache, readiness_overview


def main() -> int:
    parser = argparse.ArgumentParser(description="补全 Champion 正式 OOS 永久硬数据；默认只审计，不写入。")
    parser.add_argument("--execute", action="store_true", help="实际调用数据源并写入 PostgreSQL")
    parser.add_argument("--batch-size", type=int, default=10, help="每批交易日数，默认 10")
    parser.add_argument("--max-batches", type=int, default=1, help="本次最多执行批数，默认 1；设 0 表示直到完成")
    parser.add_argument("--end-date", default="", help="合同截止交易日 YYYYMMDD，默认数据中台目标日")
    args = parser.parse_args()
    batch_size = max(1, min(int(args.batch_size), 80))
    max_batches = max(0, int(args.max_batches))
    audit = audit_oos_hard_data(end_date=args.end_date or None, include_dates=True)
    print(json.dumps({key: audit[key] for key in (
        "schema_version", "status", "required_sessions", "calendar_sessions", "eligible_sessions",
        "missing_sessions", "coverage", "window_start", "window_end",
    )}, ensure_ascii=False, indent=2))
    if not args.execute or audit["status"] == "ready":
        return 0 if audit["status"] == "ready" else 2
    batches = 0
    execution_ok = True
    while audit["status"] != "ready" and (max_batches == 0 or batches < max_batches):
        details = {str(item["trade_date"]): item for item in audit.get("dates") or []}
        dates = list(audit.get("repair_dates") or [])[:batch_size]
        if not dates:
            print("当前交易日历不足，无法枚举剩余交易日；请先同步更早的 SSE 交易日历。")
            return 3
        result = repair_oos_hard_data_batch(dates, date_gaps={value: details[value] for value in dates})
        print(json.dumps({"batch": batches + 1, "dates": dates, "ok": result.get("ok")}, ensure_ascii=False))
        batches += 1
        audit = audit_oos_hard_data(end_date=args.end_date or None, include_dates=True)
        if not result.get("ok"):
            execution_ok = False
            break
    invalidate_data_assets_cache()
    invalidate_readiness_cache()
    assets = get_data_assets(force=True)
    readiness = readiness_overview()
    final = audit_oos_hard_data(end_date=args.end_date or None, include_dates=False)
    print(json.dumps({
        "final": {key: final[key] for key in ("status", "eligible_sessions", "required_sessions", "missing_sessions", "coverage")},
        "asset_synced": any(item.get("key") == "oos_hard_data" for item in assets.get("items") or []),
        "readiness_synced": any(item.get("key") in {"backtest", "model_training"} for item in readiness.get("consumers") or []),
    }, ensure_ascii=False, indent=2))
    # A quota-safe partial batch is normal progress, not a process failure.
    # Non-zero is reserved for audit-only incompleteness or an actual failed batch.
    return 0 if execution_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
