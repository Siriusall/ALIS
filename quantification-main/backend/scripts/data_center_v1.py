#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":
    backend = Path(__file__).resolve().parent.parent
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

from app.db.store import init_db
from app.services.data_center import (
    build_stock_daily_snapshot,
    ensure_data_center_seeded,
    estimate_data_center_runtime,
    get_data_center_status,
    import_industry_metrics,
    run_data_quality_checks,
    sync_cninfo_announcements,
    sync_expectation_bundle,
    sync_financial_batch,
    sync_financial_bundle,
    sync_financial_period_bundle,
    sync_governance_bundle,
    sync_index_bundle,
    sync_market_bundle,
    sync_stock_basic,
    sync_trade_calendar,
    sync_tushare_api_raw,
    sync_tushare_catalog_from_probe,
)


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_seed(_: argparse.Namespace) -> int:
    init_db()
    _print(ensure_data_center_seeded())
    return 0


def cmd_import_probe(args: argparse.Namespace) -> int:
    init_db()
    path = Path(args.path)
    if not path.is_absolute():
        path = Path.cwd() / path
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"probe result must be a JSON list: {path}")
    _print(sync_tushare_catalog_from_probe(payload))
    return 0


def cmd_sync_basic(args: argparse.Namespace) -> int:
    init_db()
    result = {"stock_basic": None, "trade_calendar": None}
    if not args.trade_calendar_only:
        result["stock_basic"] = sync_stock_basic()
    if not args.stock_basic_only:
        result["trade_calendar"] = sync_trade_calendar(
            start_date=args.start_date,
            end_date=args.end_date,
            exchange=args.exchange,
        )
    _print({"ok": True, **result})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    init_db()
    _print(get_data_center_status(limit_runs=args.limit_runs))
    return 0


def cmd_sync_api(args: argparse.Namespace) -> int:
    init_db()
    params = json.loads(args.params) if args.params else {}
    _print(sync_tushare_api_raw(args.api, params=params, limit_rows=args.limit_rows))
    return 0


def cmd_sync_market(args: argparse.Namespace) -> int:
    init_db()
    _print(sync_market_bundle(args.trade_date, include_suspend=args.include_suspend))
    return 0


def cmd_sync_financial(args: argparse.Namespace) -> int:
    init_db()
    _print(sync_financial_bundle(args.ts_code, args.period, include_yearly=not args.no_yearly))
    return 0


def cmd_sync_financial_period(args: argparse.Namespace) -> int:
    init_db()
    _print(sync_financial_period_bundle(args.period, include_yearly=args.include_yearly))
    return 0


def cmd_sync_financial_batch(args: argparse.Namespace) -> int:
    init_db()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None
    _print(sync_financial_batch(args.period, codes=codes, limit=args.limit, include_yearly=args.include_yearly))
    return 0


def cmd_sync_governance(args: argparse.Namespace) -> int:
    init_db()
    _print(sync_governance_bundle(args.ts_code, period=args.period))
    return 0


def cmd_sync_expectation(args: argparse.Namespace) -> int:
    init_db()
    _print(sync_expectation_bundle(args.ts_code, period=args.period))
    return 0


def cmd_sync_index(args: argparse.Namespace) -> int:
    init_db()
    _print(sync_index_bundle(index_code=args.index_code, start_date=args.start_date, end_date=args.end_date))
    return 0


def cmd_build_snapshot(args: argparse.Namespace) -> int:
    init_db()
    _print(build_stock_daily_snapshot(args.trade_date))
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    init_db()
    _print(run_data_quality_checks(trade_date=args.trade_date))
    return 0


def cmd_sync_announcements(args: argparse.Namespace) -> int:
    init_db()
    _print(
        sync_cninfo_announcements(
            start_date=args.start_date,
            end_date=args.end_date,
            ts_code=args.ts_code,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
    )
    return 0


def cmd_import_industry(args: argparse.Namespace) -> int:
    init_db()
    _print(import_industry_metrics(args.path))
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    _print(estimate_data_center_runtime(years=args.years))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Data Center V1 management.")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Seed data source and TuShare API catalog.")
    seed.set_defaults(func=cmd_seed)

    import_probe = sub.add_parser("import-probe", help="Import probe_tushare_permissions.py JSON result.")
    import_probe.add_argument("path", help="Probe JSON path, e.g. .cache/tushare_permission_probe.json")
    import_probe.set_defaults(func=cmd_import_probe)

    sync_basic = sub.add_parser("sync-basic", help="Sync low-cost base data: stock_basic and trade_cal.")
    sync_basic.add_argument("--stock-basic-only", action="store_true", help="Only sync stock_basic.")
    sync_basic.add_argument("--trade-calendar-only", action="store_true", help="Only sync trade_cal.")
    sync_basic.add_argument("--start-date", help="trade_cal start date YYYYMMDD.")
    sync_basic.add_argument("--end-date", help="trade_cal end date YYYYMMDD.")
    sync_basic.add_argument("--exchange", default="SSE", help="trade_cal exchange, default SSE.")
    sync_basic.set_defaults(func=cmd_sync_basic)

    status = sub.add_parser("status", help="Show data-center status.")
    status.add_argument("--limit-runs", type=int, default=10)
    status.set_defaults(func=cmd_status)

    sync_api = sub.add_parser("sync-api", help="Sync any catalogued TuShare API into raw records.")
    sync_api.add_argument("api", help="API name, e.g. daily_basic")
    sync_api.add_argument("--params", default="", help='JSON params, e.g. \'{"trade_date":"20260709"}\'')
    sync_api.add_argument("--limit-rows", type=int, default=0, help="Optional row limit for tests.")
    sync_api.set_defaults(func=cmd_sync_api)

    sync_market = sub.add_parser("sync-market", help="Sync daily/daily_basic/stk_limit and build stock snapshot.")
    sync_market.add_argument("trade_date", help="YYYYMMDD")
    sync_market.add_argument("--include-suspend", action="store_true")
    sync_market.set_defaults(func=cmd_sync_market)

    sync_financial = sub.add_parser("sync-financial", help="Sync income/balancesheet/cashflow/fina_indicator bundle.")
    sync_financial.add_argument("ts_code", help="e.g. 000001.SZ")
    sync_financial.add_argument("period", help="report period YYYYMMDD")
    sync_financial.add_argument("--no-yearly", action="store_true", help="Skip fina_audit and fina_mainbz.")
    sync_financial.set_defaults(func=cmd_sync_financial)

    sync_financial_period = sub.add_parser("sync-financial-period", help="Bulk sync financial bundle for all stocks in one report period.")
    sync_financial_period.add_argument("period", help="report period YYYYMMDD")
    sync_financial_period.add_argument("--include-yearly", action="store_true", help="Also sync fina_audit and fina_mainbz.")
    sync_financial_period.set_defaults(func=cmd_sync_financial_period)

    sync_financial_batch_parser = sub.add_parser("sync-financial-batch", help="Sync financial bundle for a small stock batch.")
    sync_financial_batch_parser.add_argument("period", help="report period YYYYMMDD")
    sync_financial_batch_parser.add_argument("--codes", default="", help="Comma-separated ts_codes. If omitted, use dc_stock_master order.")
    sync_financial_batch_parser.add_argument("--limit", type=int, default=20)
    sync_financial_batch_parser.add_argument("--include-yearly", action="store_true")
    sync_financial_batch_parser.set_defaults(func=cmd_sync_financial_batch)

    sync_governance = sub.add_parser("sync-governance", help="Sync holders/dividend/pledge/disclosure bundle.")
    sync_governance.add_argument("ts_code")
    sync_governance.add_argument("--period", help="report period YYYYMMDD")
    sync_governance.set_defaults(func=cmd_sync_governance)

    sync_expectation = sub.add_parser("sync-expectation", help="Sync forecast/express bundle.")
    sync_expectation.add_argument("ts_code")
    sync_expectation.add_argument("--period", help="report period YYYYMMDD")
    sync_expectation.set_defaults(func=cmd_sync_expectation)

    sync_index = sub.add_parser("sync-index", help="Sync index_basic/index_daily/index_weight bundle.")
    sync_index.add_argument("--index-code", default="000300.SH")
    sync_index.add_argument("--start-date")
    sync_index.add_argument("--end-date")
    sync_index.set_defaults(func=cmd_sync_index)

    snapshot = sub.add_parser("build-snapshot", help="Build dc_stock_daily_snapshot for one trade date from raw records.")
    snapshot.add_argument("trade_date")
    snapshot.set_defaults(func=cmd_build_snapshot)

    quality = sub.add_parser("quality", help="Run data quality checks.")
    quality.add_argument("--trade-date")
    quality.set_defaults(func=cmd_quality)

    announcements = sub.add_parser("sync-announcements", help="Sync CNINFO announcement metadata and coarse events.")
    announcements.add_argument("--start-date", required=True, help="YYYYMMDD or YYYY-MM-DD")
    announcements.add_argument("--end-date", required=True, help="YYYYMMDD or YYYY-MM-DD")
    announcements.add_argument("--ts-code", help="Optional stock code, e.g. 000001.SZ")
    announcements.add_argument("--page-size", type=int, default=30)
    announcements.add_argument("--max-pages", type=int, default=1)
    announcements.set_defaults(func=cmd_sync_announcements)

    industry = sub.add_parser("import-industry", help="Import industry metric definitions/values from JSON or CSV.")
    industry.add_argument("path")
    industry.set_defaults(func=cmd_import_industry)

    estimate = sub.add_parser("estimate-runtime", help="Estimate first full-load and daily incremental runtime.")
    estimate.add_argument("--years", type=int, default=10)
    estimate.set_defaults(func=cmd_estimate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
