from __future__ import annotations

from collections import Counter
import threading
import time
from typing import Any

from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE


# The dashboard polls this report frequently.  It must never scan the entire
# multi-million-row raw table merely to render a status card.
_health_cache_lock = threading.Lock()
_health_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_HEALTH_CACHE_TTL_SECONDS = 20.0


def _summarize_dates(dates: list[str]) -> dict[str, Any]:
    return {
        "count": len(dates),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
    }


def _summarize_date_row(row: Any) -> dict[str, Any]:
    values = dict(row) if row else {}
    return {
        "count": int(values.get("count") or 0),
        "first": str(values.get("first") or "") or None,
        "last": str(values.get("last") or "") or None,
    }


def _summarize_postgresql_market_snapshots(limit_days: int) -> dict[str, Any]:
    """Summarize the authoritative PostgreSQL market snapshots only."""
    from app.data_platform.db import _conn

    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM dc_stock_daily_snapshot ORDER BY trade_date DESC LIMIT ?",
            (limit_days,),
        ).fetchall()
        dates = sorted(str(dict(row).get("trade_date") or "") for row in rows if str(dict(row).get("trade_date") or ""))
        day_summaries: list[dict[str, Any]] = []
        weak_days: list[dict[str, Any]] = []
        fields = ("close", "volume", "turnover", "circ_mv", "total_mv", "pe_ttm", "pb")
        labels = {"circ_mv": "float_mv"}
        for ymd in dates:
            row = conn.execute(
                "SELECT COUNT(*) AS total," + ",".join(
                    f"COUNT({field}) AS {field}_count" for field in fields
                ) + " FROM dc_stock_daily_snapshot WHERE trade_date=?",
                (ymd,),
            ).fetchone()
            stat = dict(row)
            total = int(stat.get("total") or 0)
            coverage = {
                labels.get(field, field): round(int(stat.get(f"{field}_count") or 0) / max(1, total), 4)
                for field in fields
            }
            low_core = {key: value for key, value in coverage.items() if value < 0.8}
            item = {"date": ymd, "symbol_rows": total, "source_counts": {"postgresql_snapshot": total}, "core_factor_coverage": coverage}
            day_summaries.append(item)
            if total <= 0 or low_core:
                weak_days.append({"date": ymd, "symbol_rows": total, "low_core_factors": low_core})
    return {**_summarize_dates(dates), "recent_days": day_summaries, "weak_days": weak_days, "storage": "postgresql"}


def build_data_health_report(limit_days: int = 30) -> dict[str, Any]:
    """Return a local, read-only data coverage report."""

    limit_days = max(1, min(int(limit_days or 30), 250))
    now = time.monotonic()
    with _health_cache_lock:
        cached = _health_cache.get(limit_days)
        if cached and now - cached[0] < _HEALTH_CACHE_TTL_SECONDS:
            return cached[1]

    market_snapshots = _summarize_postgresql_market_snapshots(limit_days)
    from app.data_platform.db import _conn
    with _conn() as conn:
        # This compact table is maintained while materializing a daily
        # snapshot.  It is the authoritative dashboard index and avoids a
        # full DISTINCT scan across daily_basic raw records on every poll.
        date_summary = _summarize_date_row(conn.execute(
            "SELECT COUNT(*) AS count,MIN(trade_date) AS first,MAX(trade_date) AS last FROM dc_available_trade_dates"
        ).fetchone())
        if not date_summary["count"]:
            date_summary = _summarize_date_row(conn.execute(
                "SELECT COUNT(DISTINCT trade_date) AS count,MIN(trade_date) AS first,MAX(trade_date) AS last FROM dc_stock_daily_snapshot"
            ).fetchone())

    issues: list[dict[str, Any]] = []
    if market_snapshots["count"] <= 0:
        issues.append(
            {
                "level": "error",
                "code": "postgresql_market_snapshot_missing",
                "message": "No PostgreSQL market snapshots found. Run data-center sync/backfill before trusting selection or backtest outputs.",
            }
        )
    if not date_summary["count"]:
        issues.append(
            {
                "level": "warning",
                "code": "daily_basic_missing",
                "message": "No PostgreSQL daily_basic records found. Market-cap and valuation filters may be skipped.",
            }
        )
    weak_days = list(market_snapshots.get("weak_days", []))
    if weak_days:
        factor_counts: Counter[str] = Counter()
        for weak in weak_days:
            factor_counts.update((weak.get("low_core_factors") or {}).keys())
        factor_summary = ", ".join(
            f"{factor} {count}/{len(weak_days)} 天"
            for factor, count in sorted(factor_counts.items())
        )
        issues.append(
            {
                "level": "warning",
                "code": "weak_factor_coverage",
                "message": f"PostgreSQL market snapshot factor coverage is weak: {factor_summary}.",
                "detail": {
                    "affected_days": len(weak_days),
                    "total_days": int(market_snapshots.get("count") or 0),
                    "factor_counts": dict(factor_counts),
                    "examples": weak_days[:3],
                },
            }
        )

    report = {
        "ok": not any(x.get("level") == "error" for x in issues),
        "limit_days": limit_days,
        "storage": "postgresql",
        "trade_dates": date_summary,
        "market_snapshots": market_snapshots,
        "snapshots": {
            "postgresql_snapshot": date_summary,
        },
        "daily_basic": {
            # Daily snapshots are only marked available after daily_basic is
            # merged, so their compact date index is the status contract.
            "postgresql": date_summary,
        },
        "factor_keys": list(FACTOR_KEYS_FOR_COVERAGE),
        "issues": issues,
    }
    with _health_cache_lock:
        _health_cache[limit_days] = (time.monotonic(), report)
    return report
