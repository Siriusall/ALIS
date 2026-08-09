from __future__ import annotations

from typing import Any


def _value(row: Any, key: str, default: Any = "") -> Any:
    if row is None:
        return default
    value = dict(row).get(key, default)
    return default if value is None else value


def _summary_table_available(c: Any) -> bool:
    """Check the optional summary table without aborting the transaction."""
    if bool(getattr(c, "_is_postgres", False)):
        row = c.execute(
            "SELECT to_regclass(?) AS table_name",
            ("public.dc_stock_daily_snapshot_stats",),
        ).fetchone()
        return bool(_value(row, "table_name", ""))
    row = c.execute(
        "SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name=?",
        ("dc_stock_daily_snapshot_stats",),
    ).fetchone()
    return row is not None


def snapshot_stats_for_range(c: Any, start: str, end: str, *, expected_dates: list[str] | tuple[str, ...] | None = None) -> dict[str, dict[str, Any]] | None:
    """Read persisted daily snapshot statistics, or return None when incomplete.

    Audits must never silently treat a partially backfilled summary as complete;
    callers retain their bounded compatibility aggregate when a date is absent.
    """
    if not _summary_table_available(c):
        return None
    rows = c.execute(
        """SELECT trade_date,rows_total,market_rows,min_available_time,max_available_time
             FROM dc_stock_daily_snapshot_stats
            WHERE trade_date BETWEEN ? AND ?""",
        (start, end),
    ).fetchall()
    result = {str(_value(row, "trade_date", "")): dict(row) for row in rows if _value(row, "trade_date", "")}
    if expected_dates is not None and not set(str(value) for value in expected_dates).issubset(result):
        return None
    return result


def candidate_snapshot_dates(c: Any, *, before: str = "", limit: int = 32) -> list[str]:
    """Return recent market dates without grouping the full snapshot table."""
    limit = max(1, min(int(limit or 32), 200))
    params: list[Any] = []
    where = ""
    if before:
        where = "WHERE trade_date < ?"
        params.append(str(before))
    try:
        rows = c.execute(
            f"SELECT trade_date FROM dc_available_trade_dates {where} ORDER BY trade_date DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    except Exception:
        rows = []
    dates = [str(_value(row, "trade_date", "")) for row in rows]
    dates = [value for value in dates if value]
    if dates:
        return dates

    # Compatibility fallback for old databases that predate the date index.
    fallback_where = "WHERE trade_date < ?" if before else ""
    fallback_params = (str(before), limit) if before else (limit,)
    rows = c.execute(
        f"SELECT trade_date FROM dc_stock_daily_snapshot {fallback_where} GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
        fallback_params,
    ).fetchall()
    return [str(_value(row, "trade_date", "")) for row in rows if _value(row, "trade_date", "")]


def latest_usable_snapshot_date(c: Any, stock_count: int, *, before: str = "") -> str:
    """Find a usable recent snapshot using bounded date candidates."""
    threshold = max(1, int(stock_count * 0.8)) if stock_count else 1
    dates = candidate_snapshot_dates(c, before=before, limit=64)
    if not dates:
        return ""
    placeholders = ",".join("?" for _ in dates)
    rows = c.execute(
        f"""SELECT trade_date,
                   SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) AS close_rows
              FROM dc_stock_daily_snapshot
             WHERE trade_date IN ({placeholders})
             GROUP BY trade_date
             ORDER BY trade_date DESC""",
        tuple(dates),
    ).fetchall()
    for row in rows:
        if int(_value(row, "close_rows", 0) or 0) >= threshold:
            return str(_value(row, "trade_date", ""))
    return ""


def estimated_table_count(c: Any, table_name: str) -> tuple[int, bool]:
    """Use PostgreSQL catalog estimates for large status-card counts."""
    allowed = {"dc_stock_daily_snapshot", "dc_tushare_raw_records"}
    if table_name not in allowed:
        raise ValueError("unsupported_estimate_table")
    if bool(getattr(c, "_is_postgres", False)):
        row = c.execute(
            "SELECT COALESCE(reltuples, 0)::BIGINT AS n FROM pg_class WHERE oid=?::regclass",
            (table_name,),
        ).fetchone()
        return max(0, int(_value(row, "n", 0) or 0)), True
    row = c.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
    return max(0, int(_value(row, "n", 0) or 0)), False
