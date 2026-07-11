from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from config import DB_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    finished_at TEXT,
    total INTEGER DEFAULT 0,
    success INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS sectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    stock_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sector_id INTEGER,
    market TEXT,
    updated_at TEXT,
    FOREIGN KEY (sector_id) REFERENCES sectors(id)
);

CREATE TABLE IF NOT EXISTS stock_factors (
    code TEXT NOT NULL,
    crawl_run_id INTEGER NOT NULL,
    revenue_yoy REAL,
    deduct_profit_yoy REAL,
    cagr_3y REAL,
    gross_margin REAL,
    net_margin REAL,
    roe REAL,
    roic REAL,
    ocf_to_profit REAL,
    free_cashflow REAL,
    debt_ratio REAL,
    interest_debt REAL,
    cash_coverage REAL,
    dividend_yield REAL,
    payout_ratio REAL,
    buyback_amount REAL,
    profit_volatility_5y REAL,
    cyclicality TEXT,
    pe REAL,
    pb REAL,
    ps REAL,
    valuation_percentile REAL,
    fund_holding REAL,
    northbound REAL,
    etf_holding REAL,
    research_count INTEGER,
    crawled_at TEXT,
    PRIMARY KEY (code, crawl_run_id),
    FOREIGN KEY (code) REFERENCES stocks(code),
    FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_stock_factors_run ON stock_factors(crawl_run_id);
CREATE INDEX IF NOT EXISTS idx_stocks_sector ON stocks(sector_id);

CREATE TABLE IF NOT EXISTS crawl_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_run_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    market TEXT,
    error TEXT,
    failed_at TEXT,
    FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_crawl_errors_run ON crawl_errors(crawl_run_id);
"""


def ensure_data_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn(readonly: bool = False) -> Iterator[sqlite3.Connection]:
    ensure_data_dir()
    if readonly:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)


def create_crawl_run() -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO crawl_runs (status, started_at) VALUES ('running', ?)",
            (now,),
        )
        return int(cur.lastrowid)


def reopen_crawl_run(run_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE crawl_runs
            SET status = 'running',
                started_at = ?,
                finished_at = NULL,
                message = 'resuming'
            WHERE id = ?
            """,
            (now, run_id),
        )


def update_crawl_run(
    run_id: int,
    *,
    status: str | None = None,
    total: int | None = None,
    success: int | None = None,
    failed: int | None = None,
    message: str | None = None,
    finished: bool = False,
) -> None:
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if total is not None:
        fields.append("total = ?")
        values.append(total)
    if success is not None:
        fields.append("success = ?")
        values.append(success)
    if failed is not None:
        fields.append("failed = ?")
        values.append(failed)
    if message is not None:
        fields.append("message = ?")
        values.append(message)
    if finished:
        fields.append("finished_at = ?")
        values.append(datetime.now().isoformat(timespec="seconds"))
    if not fields:
        return
    values.append(run_id)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE crawl_runs SET {', '.join(fields)} WHERE id = ?",
            values,
        )


def upsert_sector(code: str, name: str, stock_count: int = 0) -> int:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sectors (code, name, stock_count)
            VALUES (?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                stock_count = excluded.stock_count
            """,
            (code, name, stock_count),
        )
        row = conn.execute("SELECT id FROM sectors WHERE code = ?", (code,)).fetchone()
        return int(row["id"])


def upsert_stock(code: str, name: str, sector_id: int | None, market: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO stocks (code, name, sector_id, market, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                sector_id = excluded.sector_id,
                market = excluded.market,
                updated_at = excluded.updated_at
            """,
            (code, name, sector_id, market, now),
        )


def list_stocks() -> list[dict[str, Any]]:
    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT
                s.code,
                s.name,
                s.market,
                COALESCE(sec.name, '未分类') AS industry
            FROM stocks s
            LEFT JOIN sectors sec ON sec.id = s.sector_id
            ORDER BY s.code ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def count_stock_universe() -> int:
    with get_conn(readonly=True) as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM stocks").fetchone()
        return int(row["cnt"])


def count_completed_factors(run_id: int) -> int:
    with get_conn(readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stock_factors WHERE crawl_run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["cnt"])


def completed_factor_codes(run_id: int) -> set[str]:
    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            "SELECT code FROM stock_factors WHERE crawl_run_id = ?",
            (run_id,),
        ).fetchall()
        return {row["code"] for row in rows}


def insert_stock_factors(run_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = [
        "code", "crawl_run_id",
        "revenue_yoy", "deduct_profit_yoy", "cagr_3y",
        "gross_margin", "net_margin", "roe", "roic",
        "ocf_to_profit", "free_cashflow",
        "debt_ratio", "interest_debt", "cash_coverage",
        "dividend_yield", "payout_ratio", "buyback_amount",
        "profit_volatility_5y", "cyclicality",
        "pe", "pb", "ps", "valuation_percentile",
        "fund_holding", "northbound", "etf_holding", "research_count",
        "crawled_at",
    ]
    placeholders = ", ".join("?" for _ in columns)
    col_names = ", ".join(columns)
    sql = f"""
        INSERT OR REPLACE INTO stock_factors ({col_names})
        VALUES ({placeholders})
    """
    now = datetime.now().isoformat(timespec="seconds")
    payload = []
    for row in rows:
        payload.append(tuple(row.get(c) if c != "crawled_at" else row.get(c, now) for c in columns))
        payload[-1] = (
            *payload[-1][:1],
            run_id,
            *payload[-1][2:-1],
            row.get("crawled_at", now),
        )
    # rebuild tuples correctly
    payload = []
    for row in rows:
        item = [row.get("code"), run_id]
        for col in columns[2:]:
            if col == "crawled_at":
                item.append(row.get(col, now))
            else:
                item.append(row.get(col))
        payload.append(tuple(item))

    with get_conn() as conn:
        conn.executemany(sql, payload)


def insert_crawl_error(run_id: int, code: str, market: str | None, error: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO crawl_errors (crawl_run_id, code, market, error, failed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, code, market, error, now),
        )


def get_latest_crawl_run(readonly: bool = True) -> dict[str, Any] | None:
    with get_conn(readonly=readonly) as conn:
        row = conn.execute(
            """
            SELECT * FROM crawl_runs
            WHERE status = 'completed'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


def get_crawl_status(run_id: int | None = None) -> dict[str, Any] | None:
    with get_conn(readonly=True) as conn:
        if run_id:
            row = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
