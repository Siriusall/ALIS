from __future__ import annotations

import os
import sys
from typing import Callable

from config import MAX_RETRIES
from csv_sync import append_stock_to_csv, export_all_to_csv, CSV_PATH
from db import (
    completed_factor_codes,
    create_crawl_run,
    count_stock_universe,
    init_db,
    insert_crawl_error,
    insert_stock_factors,
    list_stocks,
    reopen_crawl_run,
    update_crawl_run,
    upsert_sector,
    upsert_stock,
)
from sources.web_scraper import browser_page, scrape_stock_factors, scrape_stock_universe

MIN_FULL_STOCK_UNIVERSE_SIZE = 3000


def _match_sector(name: str, sector_filter: str | None) -> bool:
    if not sector_filter:
        return True
    return sector_filter in name


def _scrape_stock_factors_with_retries(page, code: str, market: str | None) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return scrape_stock_factors(page, code, market)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[WARN] {code} attempt {attempt}/{MAX_RETRIES} failed: {exc}", file=sys.stderr, flush=True)
    assert last_error is not None
    raise last_error


def _explicit_stock_list(codes: list[str]) -> list[dict[str, str]]:
    return [
        {
            "code": code,
            "name": code,
            "market": "SH" if code.startswith(("5", "6", "9")) else "SZ",
            "industry": "自选列表",
        }
        for code in codes
    ]


def _persist_stock_universe(stocks: list[dict[str, str]]) -> None:
    sector_ids: dict[str, int] = {}
    for stock in stocks:
        industry = stock.get("industry") or "未分类"
        if industry not in sector_ids:
            sector_ids[industry] = upsert_sector(f"IND:{industry}", industry, 0)
        upsert_stock(
            stock["code"],
            stock.get("name") or stock["code"],
            sector_ids[industry],
            stock.get("market") or ("SH" if stock["code"].startswith(("5", "6", "9")) else "SZ"),
        )


def _load_or_scrape_stock_universe(page, *, limit: int | None = None, force_scrape: bool = False) -> list[dict[str, str]]:
    existing_count = count_stock_universe()
    if not force_scrape and existing_count > 0:
        stocks = list_stocks()
        print(f"[INFO] stock universe loaded from db: {len(stocks)} stocks", flush=True)
        return stocks

    print("[INFO] stock universe db is empty, fetching from web", flush=True)
    stocks = scrape_stock_universe(page, limit=limit)
    if not stocks:
        raise RuntimeError("stock universe is empty")
    _persist_stock_universe(stocks)
    print(f"[INFO] stock universe saved to db: {len(stocks)} stocks", flush=True)
    return stocks


def _initial_csv_export(stocks: list[dict[str, str]], run_id: int) -> None:
    """Export all existing factors from DB to CSV as initial snapshot."""
    from db import completed_factor_codes, get_conn

    completed = completed_factor_codes(run_id)
    if not completed:
        return
    with get_conn(readonly=True) as conn:
        factors = []
        for code in completed:
            row = conn.execute(
                "SELECT * FROM stock_factors WHERE crawl_run_id = ? AND code = ?",
                (run_id, code),
            ).fetchone()
            if row:
                factors.append(dict(row))
    if factors:
        export_all_to_csv(stocks, factors)
        print(f"[INFO] initial CSV snapshot: {len(factors)} rows", flush=True)


def run_crawl(
    sector_filter: str | None = None,
    limit: int | None = None,
    codes: list[str] | None = None,
    resume_run_id: int | None = None,
    on_progress: Callable[[int, int, int, int], None] | None = None,
) -> int:
    init_db()
    run_id = resume_run_id or create_crawl_run()
    if resume_run_id:
        reopen_crawl_run(run_id)
    success = 0
    failed = 0
    skipped = 0

    try:
        with browser_page(headless=True) as page:
            if codes:
                print(f"[INFO] using explicit code list: {len(codes)} stocks", flush=True)
                stocks = _explicit_stock_list(codes)
            else:
                stocks = _load_or_scrape_stock_universe(
                    page,
                    limit=limit if not sector_filter else None,
                )

            if sector_filter and not codes:
                stocks = [
                    s for s in stocks
                    if _match_sector(s.get("industry", ""), sector_filter)
                    or _match_sector(s.get("name", ""), sector_filter)
                ]
            if limit:
                stocks = stocks[:limit]

            completed = completed_factor_codes(run_id)
            if completed:
                before = len(stocks)
                stocks = [stock for stock in stocks if stock["code"] not in completed]
                skipped = before - len(stocks)
                print(f"[INFO] resume run {run_id}: completed={skipped}, pending={len(stocks)}", flush=True)
                if stocks:
                    print(
                        f"[INFO] resume start position: {skipped + 1}/{before}, next code={stocks[0]['code']}",
                        flush=True,
                    )
                else:
                    print("[INFO] resume run has no pending stocks", flush=True)

            total = len(stocks)
            total_all = total + skipped
            print(f"[INFO] universe ready: pending={total}, total={total_all}", flush=True)
            update_crawl_run(run_id, total=total_all, success=skipped, message="crawling web pages")

            # Initial CSV snapshot if file doesn't exist yet
            if not os.path.exists(str(CSV_PATH)):
                _initial_csv_export(stocks, run_id)

            sector_ids: dict[str, int] = {}
            for idx, stock in enumerate(stocks, start=1):
                global_idx = skipped + idx
                code = stock["code"]
                industry = stock.get("industry") or "未分类"
                if industry not in sector_ids:
                    sector_ids[industry] = upsert_sector(f"IND:{industry}", industry, 0)

                try:
                    print(f"[INFO] crawling {global_idx}/{total_all} {code} {stock['name']}", flush=True)
                    upsert_stock(code, stock["name"], sector_ids[industry], stock.get("market", "SH"))
                    factor_row = _scrape_stock_factors_with_retries(page, code, stock.get("market"))
                    success += 1
                    insert_stock_factors(run_id, [factor_row])
                    append_stock_to_csv(stock, factor_row)
                    print(
                        f"[INFO] saved {code} success={success + skipped} failed={failed}",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    insert_crawl_error(run_id, code, stock.get("market"), str(exc))
                    print(f"[WARN] {code} failed: {exc}", file=sys.stderr, flush=True)

                update_crawl_run(run_id, success=success + skipped, failed=failed)
                if on_progress:
                    on_progress(run_id, global_idx, total_all, success + skipped)
                elif idx % 5 == 0 or idx == total:
                    print(f"Progress: {global_idx}/{total_all} success={success + skipped} failed={failed}")

        update_crawl_run(
            run_id,
            status="completed",
            success=success + skipped,
            failed=failed,
            message="done",
            finished=True,
        )
        return run_id
    except Exception as exc:  # noqa: BLE001
        update_crawl_run(
            run_id,
            status="failed",
            success=success + skipped,
            failed=failed,
            message=str(exc),
            finished=True,
        )
        raise
