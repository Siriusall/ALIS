"""Web/F10 fallback ingestion, consolidated from the former ``news`` subsystem."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.data_platform.db import _conn

SOURCE_WEB_FALLBACK = "web_fallback"
FACTOR_FIELDS = (
    "revenue_yoy", "deduct_profit_yoy", "cagr_3y", "gross_margin", "net_margin",
    "roe", "roic", "ocf_to_profit", "free_cashflow", "debt_ratio", "interest_debt",
    "cash_coverage", "dividend_yield", "payout_ratio", "buyback_amount",
    "profit_volatility_5y", "cyclicality", "pe", "pb", "ps",
    "valuation_percentile", "fund_holding", "northbound", "etf_holding", "research_count",
)

_crawl_lock = threading.Lock()
_active_crawl: dict[str, Any] = {}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _ts_code(symbol: str, market: str = "") -> str:
    suffix = (market or ("SH" if symbol.startswith(("5", "6", "9")) else "SZ")).upper()
    return f"{symbol}.{suffix}"


def ensure_web_fallback_source() -> None:
    now = _now()
    values = (
        SOURCE_WEB_FALLBACK, "公开网页/F10 兜底", "crawler", 90, "fallback",
        "仅在结构化主数据源失败、无权限或字段缺失时补充；结果保留来源和采集批次。", now, now,
    )
    with _conn() as c:
        if c._is_mysql:
            c.execute(
                """INSERT INTO data_sources
                (code,name,source_type,priority,status,description,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE name=VALUES(name),
                source_type=VALUES(source_type),priority=VALUES(priority),status=VALUES(status),
                description=VALUES(description),updated_at=VALUES(updated_at)""", values,
            )
        else:
            c.execute(
                """INSERT INTO data_sources
                (code,name,source_type,priority,status,description,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                source_type=excluded.source_type,priority=excluded.priority,status=excluded.status,
                description=excluded.description,updated_at=excluded.updated_at""", values,
            )


def import_legacy_news_db(path: str | Path) -> dict[str, Any]:
    """Import a legacy crawl when explicitly supplied.

    The old SQLite news database is no longer part of the runtime. Keeping this
    function as an explicit migration-only entry point prevents accidental
    recreation of a deleted SQLite fact store.
    """
    ensure_web_fallback_source()
    db_path = Path(path).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(
            f"legacy SQLite source not found (SQLite runtime store was removed): {db_path}"
        )

    legacy = sqlite3.connect(str(db_path))
    legacy.row_factory = sqlite3.Row
    try:
        run = legacy.execute(
            """SELECT cr.* FROM crawl_runs cr WHERE EXISTS
            (SELECT 1 FROM stock_factors sf WHERE sf.crawl_run_id=cr.id)
            ORDER BY cr.id DESC LIMIT 1"""
        ).fetchone()
        if run is None:
            raise ValueError("legacy news database has no factor crawl")
        rows = legacy.execute(
            """SELECT sf.*, COALESCE(s.name,sf.code) name, COALESCE(s.market,'') market,
            COALESCE(sec.name,'') industry FROM stock_factors sf
            LEFT JOIN stocks s ON s.code=sf.code LEFT JOIN sectors sec ON sec.id=s.sector_id
            WHERE sf.crawl_run_id=? ORDER BY sf.code""", (run["id"],),
        ).fetchall()
    finally:
        legacy.close()

    run_key = f"legacy-news:{run['id']}"
    started = _now()
    with _conn() as c:
        c.execute(
            """INSERT INTO data_ingestion_runs
            (job_name,source_code,status,started_at,rows_read,rows_written,meta_json)
            VALUES (?,?,?,?,?,?,?)""",
            ("web_fallback.import_legacy", SOURCE_WEB_FALLBACK, "running", started, len(rows), 0,
             json.dumps({"path": str(db_path), "legacy_run_id": run["id"]}, ensure_ascii=False)),
        )
        ingestion_id = c.last_insert_id()

    written = 0
    try:
        with _conn() as c:
            c.execute("DELETE FROM dc_web_factor_snapshot WHERE source_run_key = ?", (run_key,))
            for row in rows:
                item = dict(row)
                symbol = str(item.get("code") or "").zfill(6)
                factors = {key: item.get(key) for key in FACTOR_FIELDS}
                crawled_at = str(item.get("crawled_at") or started)
                c.execute(
                    """INSERT INTO dc_web_factor_snapshot
                    (symbol,ts_code,name,industry,source_code,source_run_key,crawled_at,
                     available_time,factors_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (symbol, _ts_code(symbol, str(item.get("market") or "")), str(item.get("name") or symbol),
                     str(item.get("industry") or ""), SOURCE_WEB_FALLBACK, run_key, crawled_at,
                     crawled_at, json.dumps(factors, ensure_ascii=False), _now()),
                )
                written += 1
        status, error = "completed", ""
    except Exception as exc:
        status, error = "failed", str(exc)[:1000]
        raise
    finally:
        with _conn() as c:
            c.execute(
                """UPDATE data_ingestion_runs SET status=?,finished_at=?,rows_read=?,rows_written=?,error=?,meta_json=?
                WHERE id=?""",
                (status, _now(), len(rows), written, error,
                 json.dumps({"path": str(db_path), "legacy_run_id": run["id"], "source_run_key": run_key}, ensure_ascii=False),
                 ingestion_id),
            )
    return {"ok": True, "run_id": ingestion_id, "source_run_key": run_key, "rows_read": len(rows), "rows_written": written}


def list_web_factors(*, q: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    ensure_web_fallback_source()
    where, params = "", []
    if q.strip():
        where = "WHERE symbol LIKE ? OR ts_code LIKE ? OR name LIKE ?"
        needle = f"%{q.strip()}%"
        params = [needle, needle, needle]
    with _conn() as c:
        latest = "id IN (SELECT MAX(id) FROM dc_web_factor_snapshot GROUP BY ts_code)"
        filtered_where = f"WHERE {latest}" + (f" AND ({where.removeprefix('WHERE ')})" if where else "")
        total = c.execute(f"SELECT COUNT(*) cnt FROM dc_web_factor_snapshot {filtered_where}", tuple(params)).fetchone()
        rows = c.execute(
            f"""SELECT id,symbol,ts_code,name,industry,source_code,source_run_key,crawled_at,factors_json
            FROM dc_web_factor_snapshot {filtered_where} ORDER BY crawled_at DESC,symbol ASC LIMIT ? OFFSET ?""",
            tuple(params + [limit, offset]),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["factors"] = json.loads(item.pop("factors_json") or "{}")
        items.append(item)
    return {"total": int(dict(total).get("cnt") or 0), "items": items, "limit": limit, "offset": offset}


def _persist_live_factor(run_key: str, stock: dict[str, Any], factor_row: dict[str, Any]) -> None:
    symbol = str(stock.get("code") or factor_row.get("code") or "").zfill(6)
    crawled_at = str(factor_row.get("crawled_at") or _now())
    factors = {key: factor_row.get(key) for key in FACTOR_FIELDS}
    with _conn() as c:
        c.execute(
            """INSERT INTO dc_web_factor_snapshot
            (symbol,ts_code,name,industry,source_code,source_run_key,crawled_at,
             available_time,factors_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, _ts_code(symbol, str(stock.get("market") or "")), str(stock.get("name") or symbol),
             str(stock.get("industry") or ""), SOURCE_WEB_FALLBACK, run_key, crawled_at,
             crawled_at, json.dumps(factors, ensure_ascii=False), _now()),
        )


def _crawl_worker(ingestion_id: int, codes: list[str], limit: int) -> None:
    from app.data_platform.web_fallback.config import MAX_RETRIES
    from app.data_platform.web_fallback.scraper import browser_page, market_prefix, scrape_stock_factors, scrape_stock_universe

    run_key = f"live-web:{ingestion_id}"
    succeeded = failed = 0
    errors: list[dict[str, str]] = []
    try:
        with browser_page(headless=True) as page:
            if codes:
                stocks = [{"code": code, "name": code, "market": market_prefix(code), "industry": "定向兜底"} for code in codes]
            else:
                stocks = scrape_stock_universe(page, limit=limit)
            stocks = stocks[:limit]
            with _conn() as c:
                c.execute("UPDATE data_ingestion_runs SET rows_read=?,meta_json=? WHERE id=?", (
                    len(stocks), json.dumps({"codes": codes, "limit": limit, "source_run_key": run_key}, ensure_ascii=False), ingestion_id,
                ))
            for index, stock in enumerate(stocks, 1):
                last_error: Exception | None = None
                for _ in range(MAX_RETRIES):
                    try:
                        row = scrape_stock_factors(page, stock["code"], stock.get("market"))
                        _persist_live_factor(run_key, stock, row)
                        succeeded += 1
                        last_error = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                if last_error is not None:
                    failed += 1
                    errors.append({"code": stock["code"], "error": str(last_error)[:300]})
                with _conn() as c:
                    c.execute("UPDATE data_ingestion_runs SET rows_written=?,meta_json=? WHERE id=?", (
                        succeeded,
                        json.dumps({"codes": codes, "limit": limit, "source_run_key": run_key, "progress": index,
                                    "total": len(stocks), "failed": failed, "errors": errors[-20:]}, ensure_ascii=False), ingestion_id,
                    ))
        final_status, final_error = "completed", ""
    except Exception as exc:  # noqa: BLE001
        final_status, final_error = "failed", str(exc)[:1000]
    with _conn() as c:
        c.execute(
            "UPDATE data_ingestion_runs SET status=?,finished_at=?,rows_written=?,error=? WHERE id=?",
            (final_status, _now(), succeeded, final_error, ingestion_id),
        )
    with _crawl_lock:
        _active_crawl.update({"running": False, "run_id": ingestion_id, "status": final_status,
                              "rows_written": succeeded, "failed": failed, "error": final_error})


def start_web_fallback_crawl(*, codes: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    """Start one integrated crawler task. Only one live fallback crawl may run at a time."""
    ensure_web_fallback_source()
    normalized = list(dict.fromkeys(str(code).split(".")[0].strip().zfill(6) for code in (codes or []) if str(code).strip()))
    with _crawl_lock:
        if _active_crawl.get("running"):
            return {"ok": False, "reason": "already_running", **_active_crawl}
        with _conn() as c:
            c.execute(
                """INSERT INTO data_ingestion_runs
                (job_name,source_code,status,started_at,rows_read,rows_written,meta_json)
                VALUES (?,?,?,?,?,?,?)""",
                ("web_fallback.live_crawl", SOURCE_WEB_FALLBACK, "running", _now(), 0, 0,
                 json.dumps({"codes": normalized, "limit": limit}, ensure_ascii=False)),
            )
            run_id = c.last_insert_id()
        _active_crawl.clear()
        _active_crawl.update({"running": True, "run_id": run_id, "status": "running", "rows_written": 0, "failed": 0})
        threading.Thread(target=_crawl_worker, args=(run_id, normalized, limit), daemon=True, name=f"web-fallback-{run_id}").start()
        return {"ok": True, **_active_crawl}


def get_web_fallback_crawl_status() -> dict[str, Any]:
    with _crawl_lock:
        return {"ok": True, **_active_crawl} if _active_crawl else {"ok": True, "running": False}
