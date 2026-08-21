from __future__ import annotations

from collections import Counter
from statistics import median
import threading
import time
from typing import Any

from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE


# The dashboard polls this report frequently.  It must never scan the entire
# multi-million-row raw table merely to render a status card.
_health_cache_lock = threading.Lock()
_health_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_HEALTH_CACHE_TTL_SECONDS = 20.0
_decision_health_cache_lock = threading.Lock()
_decision_health_cache: tuple[float, dict[str, Any]] | None = None
_DECISION_HEALTH_CACHE_TTL_SECONDS = 20.0
_FACTOR_COVERAGE_THRESHOLDS = {
    "close": 0.8,
    "volume": 0.8,
    "turnover": 0.8,
    "float_mv": 0.8,
    "total_mv": 0.8,
    # pe_ttm is legitimately null for loss-making or otherwise
    # non-comparable companies; treating it like a price field creates a
    # permanent false alarm across the full A-share universe.
    "pe_ttm": 0.65,
    "pb": 0.8,
}
_DECISION_ROW_COUNT_DRIFT_FLOOR = 0.80
_DECISION_DRIFT_LOOKBACK_DAYS = 5


def _low_core_factor_coverage(coverage: dict[str, float]) -> dict[str, float]:
    return {
        key: value for key, value in coverage.items()
        if value < _FACTOR_COVERAGE_THRESHOLDS.get(key, 0.8)
    }


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
    from app.data_platform.db import _read_conn

    with _read_conn() as conn:
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
            low_core = _low_core_factor_coverage(coverage)
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
    from app.data_platform.db import _read_conn
    with _read_conn() as conn:
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


def _latest_adj_factor_coverage(trade_date: str) -> dict[str, Any]:
    """Read immutable, field-valid adj-factor observations for experiment gating."""
    from app.data_platform.db import _conn
    from app.data_platform.services.data_center import _strict_adj_factor_codes

    with _conn() as conn:
        row = conn.execute(
            """SELECT MAX(rows_read) AS rows_read
               FROM dc_tushare_request_coverage
               WHERE api_name='adj_factor' AND trade_date=? AND result_status='success'""",
            (str(trade_date),),
        ).fetchone()
        valid_codes = _strict_adj_factor_codes(conn, trade_date)
    rows_read = int((dict(row) if row else {}).get("rows_read") or 0)
    return {
        "trade_date": str(trade_date),
        "rows_read": len(valid_codes),
        "request_rows_read": rows_read,
        "ready": len(valid_codes) >= 500,
        "minimum_rows": 500,
        "evidence_source": "immutable_observation_versions",
    }


def _latest_benchmark_coverage(trade_date: str) -> dict[str, Any]:
    """Require a valid immutable HS300 close for current experimentation.

    This is intentionally *not* a historical T15 query.  A T-day benchmark
    that arrived after close may support today's attribution once observed,
    but it cannot rewrite history for OOS/sealed PIT checks; those consumers
    have their own fixed-cutoff readers.
    """
    from app.data_platform.db import _conn
    from app.data_platform.services.data_center import _strict_benchmark_bar

    value = str(trade_date or "").replace("-", "")[:8]
    with _conn() as conn:
        bar = _strict_benchmark_bar(conn, value)
    return {
        "trade_date": value,
        "ts_code": "000300.SH",
        "rows_read": 1 if bar else 0,
        "source_code": str((bar or {}).get("source_code") or "") or None,
        "ready": bool(bar),
        "evidence_source": "immutable_observation_versions",
        "historical_point_in_time_policy": "oos_and_sealed_use_fixed_t15_readers",
    }


def assess_latest_decision_data_health(
    trade_date: str = "",
    *,
    observation_cutoff: str | None = None,
) -> dict[str, Any]:
    """Decide whether one completed data slice may feed research and simulation.

    Daily execution already has its own hard gate. This broader read-only
    contract catches coverage drift before the autonomous research bus creates
    candidates from a partial market slice. Recovery tasks pass their frozen
    trade date explicitly so a post-midnight run does not silently switch to
    the new calendar day's still-incomplete data.
    """
    global _decision_health_cache
    requested_trade_date = str(trade_date or "").replace("-", "")[:8]
    now = time.monotonic()
    if not requested_trade_date:
        with _decision_health_cache_lock:
            cached = _decision_health_cache
        if cached and now - cached[0] < _DECISION_HEALTH_CACHE_TTL_SECONDS:
            return cached[1]

    if requested_trade_date:
        from app.data_platform.services.decision_data_contract import decision_data_contract

        contract_args: dict[str, Any] = {}
        if observation_cutoff is not None:
            contract_args["as_of_time"] = observation_cutoff
        contract = decision_data_contract(requested_trade_date, **contract_args)
        readiness = {
            "ok": bool(contract.get("ok")),
            "level": "healthy" if contract.get("ok") else "blocked",
            "summary": (
                f"{requested_trade_date} 决策数据完整"
                if contract.get("ok")
                else str(contract.get("reason") or f"{requested_trade_date} 决策数据未就绪")
            ),
            "target_trade_date": requested_trade_date,
            "latest_trade_date": requested_trade_date,
            "latest_decision_ready_trade_date": requested_trade_date if contract.get("ok") else "",
            "decision_contract": contract,
        }
    else:
        from app.data_platform.services.daily_update import simple_health

        readiness = simple_health()

    report = build_data_health_report(limit_days=_DECISION_DRIFT_LOOKBACK_DAYS + 1)
    snapshots = report.get("market_snapshots") if isinstance(report.get("market_snapshots"), dict) else {}
    recent_days = snapshots.get("recent_days") if isinstance(snapshots.get("recent_days"), list) else []
    target_date = requested_trade_date or str(readiness.get("target_trade_date") or "").replace("-", "")[:8]
    # Health must evaluate the exchange-calendar target.  Choosing the newest
    # stored snapshot can silently turn an incomplete current session into a
    # healthy historical one.
    latest = next(
        (item for item in recent_days if str(item.get("date") or "").replace("-", "")[:8] == target_date),
        {},
    )
    latest_date = target_date
    latest_rows = int(latest.get("symbol_rows") or 0)
    baseline_candidates = [
        item for item in recent_days
        if str(item.get("date") or "") < latest_date
    ] if latest_date else []
    baseline_rows = [
        int(item.get("symbol_rows") or 0)
        for item in baseline_candidates
        if int(item.get("symbol_rows") or 0) > 0
    ][-_DECISION_DRIFT_LOOKBACK_DAYS:]
    baseline = float(median(baseline_rows)) if len(baseline_rows) >= 3 else 0.0
    row_ratio = latest_rows / baseline if baseline > 0 else None
    low_core = _low_core_factor_coverage(
        latest.get("core_factor_coverage") if isinstance(latest.get("core_factor_coverage"), dict) else {}
    )
    issues: list[dict[str, Any]] = []
    if not bool(readiness.get("ok")):
        issues.append({
            "level": "error",
            "code": "latest_decision_date_not_ready",
            "message": str(readiness.get("summary") or "最新交易日决策数据未就绪"),
        })
    if not latest_date or latest_rows <= 0:
        issues.append({
            "level": "error",
            "code": "latest_snapshot_missing",
            "message": "最新决策截面不存在或为空",
        })
    if low_core:
        issues.append({
            "level": "error",
            "code": "latest_core_factor_coverage_low",
            "message": "最新决策截面核心字段覆盖不足",
            "detail": low_core,
        })
    if row_ratio is not None and row_ratio < _DECISION_ROW_COUNT_DRIFT_FLOOR:
        issues.append({
            "level": "error",
            "code": "latest_snapshot_row_count_drift",
            "message": "最新截面股票数相对近期中位数异常下降",
            "detail": {"latest_rows": latest_rows, "baseline_rows": baseline, "ratio": round(row_ratio, 4)},
        })
    adj_factor = _latest_adj_factor_coverage(latest_date) if latest_date else {
        "trade_date": "", "rows_read": 0, "ready": False, "minimum_rows": 500,
    }
    if not adj_factor["ready"]:
        issues.append({
            "level": "warning",
            "code": "latest_adj_factor_not_ready",
            "message": "最新复权因子切片未达到正式研究合同，暂停新增策略实验",
            "detail": adj_factor,
        })
    benchmark = _latest_benchmark_coverage(latest_date) if latest_date else {
        "trade_date": "", "ts_code": "000300.SH", "rows_read": 0, "ready": False,
    }
    if not benchmark["ready"]:
        issues.append({
            "level": "warning",
            "code": "latest_benchmark_not_ready",
            "message": "最新沪深300点时切片尚未就绪，暂停 OOS、Shadow 与收益归因实验",
            "detail": benchmark,
        })
    decision_ready = not any(item["level"] == "error" for item in issues)
    experiment_ready = (
        decision_ready
        and bool(adj_factor["ready"])
        and bool(benchmark["ready"])
    )
    result = {
        "ok": decision_ready,
        "status": "ready" if experiment_ready else "degraded" if decision_ready else "blocked",
        "schema_version": "alis_latest_decision_data_health_v1",
        "decision_ready": decision_ready,
        "experiment_ready": experiment_ready,
        # Promote the immutable observation instant to the gate root.  Formal
        # plans must align this value with the selection snapshot and version
        # manifest; keeping it only under ``readiness`` made every V2 plan
        # appear to have a missing fourth clock.
        "observation_cutoff": (
            ((readiness.get("decision_contract") or {}).get("observation_cutoff"))
            if isinstance(readiness.get("decision_contract"), dict)
            else None
        ),
        "observation_contract": (
            ((readiness.get("decision_contract") or {}).get("observation_contract"))
            if isinstance(readiness.get("decision_contract"), dict)
            else {}
        ),
        "readiness": readiness,
        "latest_snapshot": {
            "trade_date": latest_date,
            "symbol_rows": latest_rows,
            "core_factor_coverage": latest.get("core_factor_coverage") or {},
            "baseline_rows": baseline or None,
            "row_count_ratio": round(row_ratio, 4) if row_ratio is not None else None,
        },
        "adj_factor": adj_factor,
        "benchmark": benchmark,
        "issues": issues,
        "policy": {
            "row_count_drift_floor": _DECISION_ROW_COUNT_DRIFT_FLOOR,
            "minimum_prior_days_for_drift": 3,
            "simulation_execution": "blocked only for decision-data errors",
            "strategy_experiments": (
                "require decision data, latest adj_factor coverage, and point-in-time HS300 benchmark"
            ),
        },
    }
    if not requested_trade_date:
        with _decision_health_cache_lock:
            _decision_health_cache = (time.monotonic(), result)
    return result
