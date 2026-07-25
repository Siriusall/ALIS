from __future__ import annotations

from typing import Any

from app.data_platform.db import _conn


REQUIRED_DECISION_APIS = ("daily", "daily_basic", "stk_limit")
DECISION_MIN_STOCK_COVERAGE = 0.8


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row else {}


def latest_market_trade_date(conn: Any) -> str:
    row = conn.execute("SELECT MAX(trade_date) AS value FROM dc_available_trade_dates").fetchone()
    value = str(_row_dict(row).get("value") or "")
    if value:
        return value
    row = conn.execute("SELECT MAX(trade_date) AS value FROM dc_stock_daily_snapshot").fetchone()
    return str(_row_dict(row).get("value") or "")


def decision_data_contract(trade_date: str | None = None, *, conn: Any | None = None) -> dict[str, Any]:
    """Return the single production decision-data coverage contract."""

    context = _conn() if conn is None else None
    active = context.__enter__() if context is not None else conn
    try:
        value = str(trade_date or latest_market_trade_date(active) or "")
        stock_row = active.execute("SELECT COUNT(*) AS count FROM dc_stock_master").fetchone()
        stock_count = int(_row_dict(stock_row).get("count") or 0)
        threshold = max(1, int(stock_count * DECISION_MIN_STOCK_COVERAGE)) if stock_count else 1
        api_rows = active.execute(
            """SELECT api_name,COUNT(*) AS rows FROM dc_tushare_raw_records
               WHERE trade_date=? AND api_name IN ('daily','daily_basic','stk_limit')
               GROUP BY api_name""",
            (value,),
        ).fetchall() if value else []
        api_counts = {
            str(_row_dict(row).get("api_name") or ""): int(_row_dict(row).get("rows") or 0)
            for row in api_rows
        }
        snapshot = active.execute(
            """SELECT COUNT(*) AS snapshot_rows,
                      SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) AS close_rows,
                      SUM(CASE WHEN total_mv IS NOT NULL THEN 1 ELSE 0 END) AS valuation_rows,
                      SUM(CASE WHEN up_limit IS NOT NULL AND down_limit IS NOT NULL THEN 1 ELSE 0 END) AS limit_rows
               FROM dc_stock_daily_snapshot WHERE trade_date=?""",
            (value,),
        ).fetchone() if value else None
        raw_expected = active.execute(
            """SELECT COUNT(DISTINCT daily.ts_code) AS count
               FROM dc_tushare_raw_records daily
               JOIN dc_stock_master stock ON stock.ts_code=daily.ts_code
               WHERE daily.api_name='daily' AND daily.trade_date=?""",
            (value,),
        ).fetchone() if value else None
        raw_covered = active.execute(
            """SELECT COUNT(DISTINCT limits.ts_code) AS count
               FROM dc_tushare_raw_records limits
               JOIN dc_tushare_raw_records daily
                 ON daily.api_name='daily' AND daily.trade_date=limits.trade_date
                AND daily.ts_code=limits.ts_code
               JOIN dc_stock_master stock ON stock.ts_code=limits.ts_code
               WHERE limits.api_name='stk_limit' AND limits.trade_date=?""",
            (value,),
        ).fetchone() if value else None
    finally:
        if context is not None:
            context.__exit__(None, None, None)

    aggregate = _row_dict(snapshot)
    raw_limit_expected = int(_row_dict(raw_expected).get("count") or 0)
    raw_limit_covered = int(_row_dict(raw_covered).get("count") or 0)
    counts = {
        "snapshot": int(aggregate.get("snapshot_rows") or 0),
        "daily": int(aggregate.get("close_rows") or 0),
        "daily_basic": int(aggregate.get("valuation_rows") or 0),
        "stk_limit": min(int(aggregate.get("limit_rows") or 0), raw_limit_covered),
    }
    snapshot_limit_rows = int(aggregate.get("limit_rows") or 0)
    expected_rows = {
        "daily": stock_count,
        "daily_basic": stock_count,
        "stk_limit": raw_limit_expected or counts["daily"] or stock_count,
    }
    minimum_rows_by_api = {
        api: max(1, int(expected_rows[api] * DECISION_MIN_STOCK_COVERAGE))
        for api in REQUIRED_DECISION_APIS
    }
    present_apis = [api for api in REQUIRED_DECISION_APIS if api_counts.get(api, 0) > 0]
    missing_apis = [api for api in REQUIRED_DECISION_APIS if api not in present_apis]
    incomplete_apis = [api for api in REQUIRED_DECISION_APIS if counts[api] < minimum_rows_by_api[api]]
    coverage = {
        api: round(counts[api] / expected_rows[api], 6) if expected_rows[api] else 0.0
        for api in REQUIRED_DECISION_APIS
    }
    ready = bool(value) and not missing_apis and not incomplete_apis and counts["snapshot"] >= threshold
    blocking_reasons = []
    if not value:
        blocking_reasons.append("missing_trade_date")
    if missing_apis:
        blocking_reasons.append("missing_required_api")
    if incomplete_apis:
        blocking_reasons.append("insufficient_stock_coverage")
    if counts["snapshot"] < threshold:
        blocking_reasons.append("insufficient_snapshot_coverage")
    return {
        "ok": ready,
        "schema_version": "alis_decision_data_contract_v1",
        "latest_trade_date": value,
        "required_apis": list(REQUIRED_DECISION_APIS),
        "present_apis": present_apis,
        "missing_apis": missing_apis,
        "incomplete_apis": incomplete_apis,
        "stock_count": stock_count,
        "minimum_coverage": DECISION_MIN_STOCK_COVERAGE,
        "minimum_rows": threshold,
        "minimum_rows_by_api": minimum_rows_by_api,
        "expected_rows": expected_rows,
        "counts": counts,
        "snapshot_limit_rows": snapshot_limit_rows,
        "raw_limit_rows": raw_limit_covered,
        "coverage": coverage,
        "shortfall": {api: max(0, minimum_rows_by_api[api] - counts[api]) for api in REQUIRED_DECISION_APIS},
        "snapshot_ready": counts["snapshot"] >= threshold,
        "blocking_reasons": blocking_reasons,
        "reason": "" if ready else f"{value or '最新交易日'} 决策数据覆盖不足：{','.join(incomplete_apis or missing_apis) or 'snapshot'}",
    }


def latest_decision_ready_trade_date(conn: Any) -> str:
    stock_row = conn.execute("SELECT COUNT(*) AS count FROM dc_stock_master").fetchone()
    stock_count = int(_row_dict(stock_row).get("count") or 0)
    threshold = max(1, int(stock_count * DECISION_MIN_STOCK_COVERAGE)) if stock_count else 1
    latest = latest_market_trade_date(conn)
    if latest and decision_data_contract(latest, conn=conn).get("ok"):
        return latest
    row = conn.execute(
        """SELECT s.trade_date AS value
           FROM dc_stock_daily_snapshot s
           WHERE s.trade_date < ?
             AND EXISTS (SELECT 1 FROM dc_tushare_raw_records r WHERE r.trade_date=s.trade_date AND r.api_name='daily')
             AND EXISTS (SELECT 1 FROM dc_tushare_raw_records r WHERE r.trade_date=s.trade_date AND r.api_name='daily_basic')
             AND EXISTS (SELECT 1 FROM dc_tushare_raw_records r WHERE r.trade_date=s.trade_date AND r.api_name='stk_limit')
           GROUP BY s.trade_date
           HAVING SUM(CASE WHEN s.close IS NOT NULL THEN 1 ELSE 0 END) >= ?
              AND SUM(CASE WHEN s.total_mv IS NOT NULL THEN 1 ELSE 0 END) >= ?
              AND SUM(CASE WHEN s.up_limit IS NOT NULL AND s.down_limit IS NOT NULL THEN 1 ELSE 0 END)
                  >= SUM(CASE WHEN s.close IS NOT NULL THEN 1 ELSE 0 END) * ?
           ORDER BY s.trade_date DESC LIMIT 1""",
        (latest or "99999999", threshold, threshold, DECISION_MIN_STOCK_COVERAGE),
    ).fetchone()
    return str(_row_dict(row).get("value") or "")
