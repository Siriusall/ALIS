from __future__ import annotations

import argparse
import json
import sys

from config import OCR_LANG, TESSERACT_CMD
from db import get_conn, get_crawl_status, get_latest_crawl_run, init_db
from pipeline import run_crawl
from sources.web_scraper import browser_page, market_prefix, scrape_stock_factors


def _force_utf8_io() -> None:
    """Ensure CLI output stays UTF-8 on Windows terminals."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def cmd_crawl(args: argparse.Namespace) -> int:
    codes = []
    if args.codes:
        codes.extend(code.strip() for code in args.codes.split(",") if code.strip())
    if args.codes_file:
        with open(args.codes_file, encoding="utf-8") as fh:
            for line in fh:
                code = line.strip().split(",")[0].strip()
                if code and not code.startswith("#"):
                    codes.append(code)
    codes = list(dict.fromkeys(codes)) or None
    resume_run_id = args.resume_run_id
    if not args.new_run and resume_run_id is None:
        latest = get_crawl_status()
        if latest and latest.get("status") != "completed" and (latest.get("success") or 0) > 0:
            resume_run_id = latest["id"]
        elif latest and (latest.get("success") or 0) == 0:
            # Latest run has no data — look back for the last run with data
            from db import get_conn
            with get_conn(readonly=True) as conn:
                row = conn.execute(
                    "SELECT id FROM crawl_runs WHERE success > 0 ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    resume_run_id = row["id"]
    run_id = run_crawl(
        sector_filter=args.sector,
        limit=args.limit,
        codes=codes,
        resume_run_id=resume_run_id,
    )
    print(f"Crawl completed. run_id={run_id}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    init_db()
    status = get_crawl_status()
    if not status:
        print("No crawl runs yet.")
        return 0
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def cmd_ocr_check(_: argparse.Namespace) -> int:
    try:
        import pytesseract

        if TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
        version = pytesseract.get_tesseract_version()
        print(json.dumps({
            "ok": True,
            "version": str(version),
            "lang": OCR_LANG,
            "tesseract_cmd": TESSERACT_CMD,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "lang": OCR_LANG,
            "tesseract_cmd": TESSERACT_CMD,
        }, ensure_ascii=False, indent=2))
        return 1


def cmd_single(args: argparse.Namespace) -> int:
    market = args.market or market_prefix(args.code)
    with browser_page(headless=True) as page:
        row = scrape_stock_factors(page, args.code, market)
    if args.summary:
        keys = [
            "code",
            "revenue_yoy",
            "deduct_profit_yoy",
            "cagr_3y",
            "gross_margin",
            "net_margin",
            "roe",
            "roic",
            "ocf_to_profit",
            "free_cashflow",
            "debt_ratio",
            "interest_debt",
            "cash_coverage",
            "dividend_yield",
            "payout_ratio",
            "buyback_amount",
            "profit_volatility_5y",
            "cyclicality",
            "pe",
            "pb",
            "ps",
            "valuation_percentile",
            "fund_holding",
            "northbound",
            "etf_holding",
            "research_count",
        ]
        row = {key: row.get(key) for key in keys}
        row["non_null"] = sum(1 for key, value in row.items() if key not in {"code", "non_null"} and value is not None)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    init_db()
    run_id = args.run_id
    if run_id is None:
        latest = get_latest_crawl_run(readonly=True)
        if not latest:
            print("No completed crawl run.")
            return 1
        run_id = latest["id"]

    columns = [
        "revenue_yoy", "deduct_profit_yoy", "cagr_3y",
        "gross_margin", "net_margin", "roe", "roic",
        "ocf_to_profit", "free_cashflow",
        "debt_ratio", "interest_debt", "cash_coverage",
        "dividend_yield", "payout_ratio", "buyback_amount",
        "profit_volatility_5y", "cyclicality",
        "pe", "pb", "ps", "valuation_percentile",
        "fund_holding", "northbound", "etf_holding", "research_count",
    ]
    with get_conn(readonly=True) as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stock_factors WHERE crawl_run_id = ?",
            (run_id,),
        ).fetchone()["cnt"]
        result = {"run_id": run_id, "total": total, "fields": {}}
        for col in columns:
            count = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM stock_factors WHERE crawl_run_id = ? AND {col} IS NOT NULL",
                (run_id,),
            ).fetchone()["cnt"]
            result["fields"][col] = {
                "non_null": count,
                "coverage": round(count / total, 4) if total else 0,
            }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_errors(args: argparse.Namespace) -> int:
    init_db()
    run_id = args.run_id
    if run_id is None:
        status = get_crawl_status()
        if not status:
            print("No crawl run.")
            return 1
        run_id = status["id"]

    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT code, market, error, failed_at
            FROM crawl_errors
            WHERE crawl_run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (run_id, args.limit),
        ).fetchall()
    print(json.dumps({
        "run_id": run_id,
        "total": len(rows),
        "items": [dict(row) for row in rows],
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A-share factor crawler")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Run full crawl")
    crawl.add_argument("--sector", help="Only crawl stocks in sector name containing this text")
    crawl.add_argument("--limit", type=int, help="Limit number of stocks (for testing)")
    crawl.add_argument("--codes", help="Comma-separated stock codes for targeted crawling")
    crawl.add_argument("--codes-file", help="Text/CSV file containing one stock code per line")
    crawl.add_argument("--resume-run-id", type=int, help="Continue an existing crawl run and skip completed stocks")
    crawl.add_argument("--new-run", action="store_true", help="Force create a new run id instead of default resume")
    crawl.set_defaults(func=cmd_crawl)

    status = sub.add_parser("status", help="Show latest crawl status")
    status.set_defaults(func=cmd_status)

    ocr_check = sub.add_parser("ocr-check", help="Check local Tesseract OCR availability")
    ocr_check.set_defaults(func=cmd_ocr_check)

    single = sub.add_parser("single", help="Scrape one stock and print factor JSON")
    single.add_argument("code", help="Six-digit stock code")
    single.add_argument("--market", choices=["SH", "SZ"], help="Override market prefix")
    single.add_argument("--summary", action="store_true", help="Print concise factor summary")
    single.set_defaults(func=cmd_single)

    coverage = sub.add_parser("coverage", help="Show non-null factor coverage for a crawl run")
    coverage.add_argument("--run-id", type=int)
    coverage.set_defaults(func=cmd_coverage)

    errors = sub.add_parser("errors", help="Show failed stocks for a crawl run")
    errors.add_argument("--run-id", type=int)
    errors.add_argument("--limit", type=int, default=100)
    errors.set_defaults(func=cmd_errors)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
