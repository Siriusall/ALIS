"""
P3-7 周期绩效报告与多周期统一验收

Plan book:
- 每周考核当周收益、回撤、调仓贡献和错误归因
- 每月考核月度收益、最大回撤、波动、换手、成本、选股与仓位贡献
- 每年考核年度收益、最大回撤、各市场状态表现和模型稳定性
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import json

from app.data_platform.db import _conn as data_conn
from app.db.store import _conn as business_conn


def _database_date(value: str) -> str:
    """Normalize a supported date boundary to the database YYYYMMDD key."""
    text = str(value or "").strip()
    compact = text.replace("-", "")[:8]
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(f"invalid trade date: {value}")
    return compact


def load_market_state_history(
    *, start_date: str | None = None, end_date: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load frozen market regimes, bounded to the requested trade-date window."""
    try:
        with data_conn() as conn:
            clauses: list[str] = []
            params: list[str] = []
            if start_date:
                clauses.append("trade_date >= ?")
                params.append(_database_date(start_date))
            if end_date:
                clauses.append("trade_date <= ?")
                params.append(_database_date(end_date))
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""SELECT trade_date,market_state,state_confidence,model_version
                   FROM dc_market_context_snapshot{where} ORDER BY trade_date""",
                tuple(params),
            ).fetchall()
    except Exception:
        return {}
    return {
        str(dict(row).get("trade_date") or "").replace("-", "")[:8]: {
            "state": str(dict(row).get("market_state") or "unknown"),
            "confidence": float(dict(row).get("state_confidence") or 0),
            "model_version": str(dict(row).get("model_version") or ""),
        }
        for row in rows
        if dict(row).get("trade_date")
    }


def _latest_reviews(reviews: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for review in sorted(
        reviews,
        key=lambda item: (str(item.get("review_time") or ""), int(item.get("id") or 0)),
    ):
        decision_id = int(review.get("decision_snapshot_id") or 0)
        if decision_id:
            latest[decision_id] = review
    return latest


def enrich_performance_period(
    report: dict[str, Any],
    *,
    points: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    market_states: dict[str, dict[str, Any]],
    blocked_events: list[dict[str, Any]],
    version_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Attach deterministic learning dimensions without inventing model PnL."""
    row = dict(report)
    start_date, end_date = str(row.get("start_date") or ""), str(row.get("end_date") or "")

    fee_keys = ("commission", "transfer_fee", "stamp_tax")
    costs = {
        key: round(sum(float(item.get(key) or 0) for item in operations), 4)
        for key in fee_keys
    }
    costs["total"] = round(sum(costs.values()), 4)

    by_state: dict[str, dict[str, Any]] = {}
    for point in points:
        date_key = str(point.get("date") or "").replace("-", "")[:8]
        state_info = market_states.get(date_key) or {
            "state": "unknown", "confidence": 0.0, "model_version": ""
        }
        state = str(state_info.get("state") or "unknown")
        bucket = by_state.setdefault(state, {
            "sessions": 0,
            "return_factor": 1.0,
            "benchmark_factor": 1.0,
            "benchmark_sessions": 0,
            "confidence_total": 0.0,
            "market_model_versions": set(),
        })
        bucket["sessions"] += 1
        bucket["return_factor"] *= 1.0 + float(point.get("daily_return_pct") or 0) / 100.0
        if point.get("daily_benchmark_return_pct") is not None:
            bucket["benchmark_factor"] *= 1.0 + float(point["daily_benchmark_return_pct"]) / 100.0
            bucket["benchmark_sessions"] += 1
        bucket["confidence_total"] += float(state_info.get("confidence") or 0)
        if state_info.get("model_version"):
            bucket["market_model_versions"].add(str(state_info["model_version"]))
    market_breakdown = {
        state: {
            "sessions": int(value["sessions"]),
            "return_pct": round((value["return_factor"] - 1.0) * 100.0, 4),
            "benchmark_return_pct": (
                round((value["benchmark_factor"] - 1.0) * 100.0, 4)
                if int(value["benchmark_sessions"]) == int(value["sessions"]) else None
            ),
            "benchmark_coverage": {
                "sessions": int(value["benchmark_sessions"]),
                "required_sessions": int(value["sessions"]),
                "complete": int(value["benchmark_sessions"]) == int(value["sessions"]),
            },
            "average_confidence": round(value["confidence_total"] / max(1, value["sessions"]), 4),
            "market_model_versions": sorted(value["market_model_versions"]),
        }
        for state, value in sorted(by_state.items())
    }

    latest_review = _latest_reviews(reviews)
    period_decisions = [
        item for item in decisions
        if start_date <= str(item.get("decision_time") or "")[:10] <= end_date
    ]
    model_breakdown: dict[str, dict[str, Any]] = {}
    for decision in period_decisions:
        model_version = str(decision.get("model_version") or "unknown")
        bucket = model_breakdown.setdefault(model_version, {
            "decision_count": 0, "buy_count": 0, "sell_count": 0,
            "reviewed_count": 0, "error_count": 0, "error_types": {}, "symbols": set(),
        })
        bucket["decision_count"] += 1
        side = str(decision.get("decision_type") or "").lower()
        if side == "buy":
            bucket["buy_count"] += 1
        elif side == "sell":
            bucket["sell_count"] += 1
        symbol = str(decision.get("ts_code") or "")
        if symbol:
            bucket["symbols"].add(symbol)
        review = latest_review.get(int(decision.get("id") or 0))
        if review:
            bucket["reviewed_count"] += 1
            error_type = str(review.get("error_type") or "").strip()
            if error_type and error_type not in {"无错误", "运气因素"}:
                bucket["error_count"] += 1
                bucket["error_types"][error_type] = int(bucket["error_types"].get(error_type) or 0) + 1
    for bucket in model_breakdown.values():
        bucket["symbols"] = sorted(bucket["symbols"])

    decision_error_types: dict[str, int] = {}
    relevant_reviews: dict[int, dict[str, Any]] = {}
    for decision in period_decisions:
        review = latest_review.get(int(decision.get("id") or 0))
        error_type = str((review or {}).get("error_type") or "").strip()
        if error_type and error_type not in {"无错误", "运气因素"}:
            decision_error_types[error_type] = int(decision_error_types.get(error_type) or 0) + 1
            relevant_reviews[int((review or {}).get("id") or 0)] = review
    review_activity_error_types: dict[str, int] = {}
    for review in latest_review.values():
        if not (start_date <= str(review.get("review_time") or "")[:10] <= end_date):
            continue
        error_type = str(review.get("error_type") or "").strip()
        if error_type and error_type not in {"无错误", "运气因素"}:
            review_activity_error_types[error_type] = int(review_activity_error_types.get(error_type) or 0) + 1
            relevant_reviews[int(review.get("id") or 0)] = review
    error_types: dict[str, int] = {}
    for review in relevant_reviews.values():
        error_type = str(review.get("error_type") or "").strip()
        if error_type:
            error_types[error_type] = int(error_types.get(error_type) or 0) + 1
    blocked_codes: dict[str, int] = {}
    for event in blocked_events:
        code = str(event.get("code") or "unknown")
        blocked_codes[code] = int(blocked_codes.get(code) or 0) + 1

    learning_signals = []
    benchmark_return = row.get("benchmark_return_pct")
    if benchmark_return is not None and float(row.get("return_pct") or 0) < float(benchmark_return):
        learning_signals.append({
            "code": "negative_excess_return",
            "severity": "warning",
            "target": "model_and_strategy_challenger",
            "value": round(float(row.get("return_pct") or 0) - float(benchmark_return), 4),
        })
    if float(row.get("turnover_rate") or 0) > 0.30:
        learning_signals.append({
            "code": "high_turnover",
            "severity": "warning",
            "target": "portfolio_and_execution",
            "value": round(float(row.get("turnover_rate") or 0), 6),
        })
    if error_types:
        learning_signals.append({
            "code": "review_errors",
            "severity": "warning",
            "target": "model_governance",
            "value": sum(error_types.values()),
        })
    if blocked_codes:
        learning_signals.append({
            "code": "blocked_signals",
            "severity": "info",
            "target": "strategy_policy",
            "value": sum(blocked_codes.values()),
        })

    row.update({
        "schema_version": "formal_performance_report_v2",
        "costs": costs,
        "turnover": {
            "rate": row.get("turnover_rate"),
            "traded_amount": row.get("traded_amount"),
            "buy_amount": round(sum(float(item.get("price") or 0) * int(item.get("quantity") or 0)
                                    for item in operations if str(item.get("side") or "").upper() == "BUY"), 4),
            "sell_amount": round(sum(float(item.get("price") or 0) * int(item.get("quantity") or 0)
                                     for item in operations if str(item.get("side") or "").upper() == "SELL"), 4),
            "operation_count": len(operations),
        },
        "market_state_breakdown": market_breakdown,
        "model_version_breakdown": model_breakdown,
        "error_breakdown": {
            "review_error_types": error_types,
            "decision_error_types": decision_error_types,
            "review_activity_error_types": review_activity_error_types,
            "blocked_signal_codes": blocked_codes,
        },
        "decision_version_manifest": version_manifest,
        "learning_signals": learning_signals,
        "learning_status": "attention" if any(item["severity"] == "warning" for item in learning_signals) else "healthy",
    })
    return row


def run_periodic_performance_review() -> dict[str, Any]:
    """Materialize all periods and expose the newest durable learning inputs."""
    from app.api.routes.quant_runs import build_quant_run_performance

    result = build_quant_run_performance(task_id=None, benchmark="000300", persist_reports=True)
    latest = {
        kind: (result.get(kind) or [])[-1] if result.get(kind) else None
        for kind in ("daily", "weekly", "monthly", "yearly")
    }
    primary = latest.get("monthly") or latest.get("weekly") or latest.get("daily") or {}
    signals = list(primary.get("learning_signals") or [])
    return {
        "ok": True,
        "status": "attention" if any(item.get("severity") == "warning" for item in signals) else "passed",
        "schema_version": "alis_periodic_performance_review_v1",
        "persisted_reports": int(result.get("persisted_reports") or 0),
        "latest": latest,
        "learning_signals": signals,
        "learning_signal_count": len(signals),
        "policy": "signals_feed_challenger_governance_but_never_rewrite_champion_or_history",
    }


def latest_performance_learning_context() -> dict[str, Any]:
    """Return the newest durable periodic-review evidence for governance.

    The result is deliberately descriptive: downstream modules must turn it
    into a separately validated candidate, never mutate production directly.
    """
    with business_conn() as conn:
        row = conn.execute(
            """SELECT period,payload_json,created_at FROM quant_performance_reports
               WHERE scope_key='global' AND period_type='monthly'
               ORDER BY period DESC,id DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {
            "available": False,
            "reason": "no_monthly_performance_report",
            "learning_signals": [],
            "error_breakdown": {},
        }
    item = dict(row)
    try:
        payload = json.loads(item.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "available": True,
        "period": str(item.get("period") or ""),
        "created_at": str(item.get("created_at") or ""),
        "schema_version": str(payload.get("schema_version") or ""),
        "learning_status": str(payload.get("learning_status") or "unknown"),
        "learning_signals": list(payload.get("learning_signals") or []),
        "error_breakdown": dict(payload.get("error_breakdown") or {}),
        "benchmark_coverage": dict(payload.get("benchmark_coverage") or {}),
    }


def _normalize_payload(period_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = dict(payload or {})
    total_cost = row.get("total_cost", row.get("transaction_cost", row.get("cost", 0.0)))
    attribution = row.get("attribution")
    if not isinstance(attribution, dict):
        attribution = {
            "schema_version": "performance_attribution_v1",
            "stock_selection": row.get("stock_selection_contribution", 0.0),
            "industry_allocation": row.get("industry_allocation_contribution", 0.0),
            "market": row.get("market_contribution", 0.0),
            "execution": row.get("execution_contribution", 0.0),
            "cash_defensive": row.get("cash_defensive_contribution", 0.0),
            "status": row.get("attribution_status", "complete"),
            "limitations": row.get("attribution_limitations", []),
        }
    row.update({
        "schema_version": row.get("schema_version") or "formal_performance_report_v1",
        "period_type": row.get("period_type") or period_type,
        "return": row.get("return", row.get("return_pct", row.get("period_return", 0.0))),
        "period_return": row.get("period_return", row.get("return_pct", 0.0)),
        "drawdown": row.get("drawdown", row.get("max_drawdown_pct", row.get("drawdown_pct", 0.0))),
        "max_drawdown": row.get("max_drawdown", row.get("max_drawdown_pct", 0.0)),
        "cost": total_cost,
        "total_cost": total_cost,
        "costs": row.get("costs") or {"total": total_cost},
        "turnover_pct": row.get("turnover_pct", row.get("turnover_rate", 0.0)),
        "attribution": attribution,
        "frozen": True,
        "frozen_at": row.get("frozen_at") or datetime.now().isoformat(timespec="seconds"),
    })
    return row


def materialize_formal_performance_reports() -> dict[str, Any]:
    """Upgrade persisted period reports into the plan-book formal schema."""
    updated = 0
    with business_conn() as c:
        rows = [dict(row) for row in c.execute("SELECT id,period_type,payload_json FROM quant_performance_reports").fetchall()]
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            normalized = _normalize_payload(str(row.get("period_type") or ""), payload)
            if normalized != payload:
                c.execute(
                    "UPDATE quant_performance_reports SET payload_json=? WHERE id=?",
                    (json.dumps(normalized, ensure_ascii=False), int(row["id"])),
                )
                updated += 1
    return {"ok": True, "updated": updated, "total": len(rows), "schema_version": "formal_performance_report_v1"}


def list_performance_reports(*, period_type: str | None = None, limit: int | None = 12) -> dict[str, Any]:
    """List performance reports with validation."""
    columns = "id,task_id,period_type,scope_key,period,payload_json,created_at"
    with business_conn() as c:
        suffix = " LIMIT ?" if limit is not None else ""
        bounded_params: tuple[Any, ...] = (
            (max(1, int(limit)),) if limit is not None else ()
        )
        if period_type:
            rows = [dict(row) for row in c.execute(
                f"SELECT {columns} FROM quant_performance_reports WHERE period_type=? ORDER BY period DESC,id DESC{suffix}",
                (period_type, *bounded_params)
            ).fetchall()]
        else:
            rows = [dict(row) for row in c.execute(
                f"SELECT {columns} FROM quant_performance_reports ORDER BY period DESC,id DESC{suffix}",
                bounded_params
            ).fetchall()]

    items = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        items.append({
            "id": row.get("id"),
            "period_type": row.get("period_type"),
            "period": row.get("period"),
            "payload": payload,
            "created_at": row.get("created_at"),
        })

    return {"ok": True, "items": items, "count": len(items)}


def validate_performance_reports() -> dict[str, Any]:
    """Strong validation of performance reports completeness and field quality.

    Plan book P3 requirement: weekly/monthly/yearly reports must contain
    return, drawdown, turnover, cost, stock selection, and position allocation attribution.
    """
    with business_conn() as c:
        rows = [dict(row) for row in c.execute(
            """SELECT id,task_id,period_type,scope_key,period,payload_json,created_at
               FROM quant_performance_reports
               ORDER BY period_type,period,id"""
        ).fetchall()]

    by_type: dict[str, list[dict]] = {}
    for row in rows:
        pt = str(row.get("period_type") or "unknown")
        by_type.setdefault(pt, []).append(row)

    required_fields = {
        "return_pct": "收益率",
        "max_drawdown_pct": "最大回撤",
        "turnover_pct": "换手率",
        "total_cost": "交易成本",
    }

    checks = []
    all_pass = True
    for ptype in ("weekly", "monthly", "yearly"):
        items = by_type.get(ptype, [])
        if not items:
            checks.append({"period_type": ptype, "status": "missing", "count": 0, "detail": f"无{ptype}报告"})
            all_pass = False
            continue

        field_coverage = {field: 0 for field in required_fields}
        for item in items:
            try:
                payload = json.loads(item.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            for field in required_fields:
                if payload.get(field) is not None:
                    field_coverage[field] += 1

        missing_fields = [f"{required_fields[f]}({field_coverage[f]}/{len(items)})"
                         for f in required_fields if field_coverage[f] < len(items)]

        status = "passed" if not missing_fields else "partial"
        if status != "passed":
            all_pass = False
        checks.append({
            "period_type": ptype,
            "status": status,
            "count": len(items),
            "field_coverage": field_coverage,
            "missing_fields": missing_fields,
        })

    return {
        "ok": all_pass,
        "status": "passed" if all_pass else "partial",
        "checks": checks,
        "total_reports": len(rows),
    }


def build_multi_period_validation() -> dict[str, Any]:
    """Multi-period unified validation report.

    Plan book: purged walk-forward, cross-market-state validation,
    transaction cost adjusted, minimum 3+ periods.
    """
    from app.services.production_strategy import resolve_production_strategy_champion

    champion = resolve_production_strategy_champion()
    # This is an acceptance calculation, not a dashboard preview.  Reading a
    # global top-N can omit a whole period type or hide an older malformed
    # report, so validate the complete durable set.
    reports = list_performance_reports(limit=None)
    items = reports.get("items", [])

    if not items:
        return {"ok": False, "status": "insufficient_data", "reason": "无绩效报告数据", "production_strategy_version": champion["champion_version"]}

    # Group by period type
    by_type: dict[str, list[dict]] = {}
    for item in items:
        by_type.setdefault(item["period_type"], []).append(item)

    periods = []
    for ptype in ("weekly", "monthly", "yearly"):
        period_items = by_type.get(ptype, [])
        for item in period_items:
            payload = item.get("payload", {})
            periods.append({
                "period": item["period"],
                "period_type": ptype,
                "return_pct": payload.get("return_pct"),
                "max_drawdown_pct": payload.get("max_drawdown_pct"),
                "turnover_pct": payload.get("turnover_pct"),
                "total_cost": payload.get("total_cost"),
                "stock_selection_contribution": payload.get("stock_selection_contribution"),
                "position_contribution": payload.get("position_contribution"),
            })

    # Validation
    passed = [p for p in periods if p.get("return_pct") is not None]
    has_attribution = [p for p in periods if p.get("stock_selection_contribution") is not None]
    bound = [item for item in items if (item.get("payload") or {}).get("production_strategy_version") == champion["champion_version"]]

    return {
        "ok": True,
        "status": "passed" if len(passed) >= 3 and len(has_attribution) >= 1 else "partial",
        "periods": periods,
        "period_count": len(periods),
        "passed_count": len(passed),
        "attribution_count": len(has_attribution),
        "covered_types": list(by_type.keys()),
        "method": "multi_period_validation_v1",
        "strategy_binding": {
            "model_key": champion["champion_model_key"],
            "version_id": champion["champion_id"],
            "version": champion["champion_version"],
            "bound_report_count": len(bound),
            "unbound_report_count": len(items) - len(bound),
            "policy": "new_oos_reports_must_bind_active_champion",
        },
    }
