from __future__ import annotations

from collections import Counter
from typing import Any

from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE


def _summarize_dates(dates: list[str]) -> dict[str, Any]:
    return {
        "count": len(dates),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
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
    postgres_snapshot_dates: list[str] = []
    postgres_daily_basic_dates: list[str] = []
    market_snapshots = _summarize_postgresql_market_snapshots(limit_days)
    from app.data_platform.db import _conn
    with _conn() as conn:
        postgres_snapshot_dates = [
            str(dict(row).get("trade_date") or "")
            for row in conn.execute("SELECT DISTINCT trade_date FROM dc_stock_daily_snapshot ORDER BY trade_date").fetchall()
        ]
        postgres_daily_basic_dates = [
            str(dict(row).get("trade_date") or "")
            for row in conn.execute("SELECT DISTINCT trade_date FROM dc_tushare_raw_records WHERE api_name='daily_basic' ORDER BY trade_date").fetchall()
        ]

    available_trade_dates = sorted(set(postgres_snapshot_dates))

    issues: list[dict[str, Any]] = []
    if market_snapshots["count"] <= 0:
        issues.append(
            {
                "level": "error",
                "code": "postgresql_market_snapshot_missing",
                "message": "No PostgreSQL market snapshots found. Run data-center sync/backfill before trusting selection or backtest outputs.",
            }
        )
    if not postgres_daily_basic_dates:
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

    return {
        "ok": not any(x.get("level") == "error" for x in issues),
        "limit_days": limit_days,
        "storage": "postgresql",
        "trade_dates": _summarize_dates(available_trade_dates),
        "market_snapshots": market_snapshots,
        "snapshots": {
            "postgresql_snapshot": _summarize_dates(postgres_snapshot_dates),
        },
        "daily_basic": {
            "postgresql": _summarize_dates(postgres_daily_basic_dates),
        },
        "factor_keys": list(FACTOR_KEYS_FOR_COVERAGE),
        "issues": issues,
    }
