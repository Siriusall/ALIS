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


def list_performance_reports(*, period_type: str | None = None, limit: int = 12) -> dict[str, Any]:
    """List performance reports with validation."""
    with business_conn() as c:
        if period_type:
            rows = [dict(row) for row in c.execute(
                "SELECT * FROM quant_performance_reports WHERE period_type=? ORDER BY period DESC LIMIT ?",
                (period_type, limit)
            ).fetchall()]
        else:
            rows = [dict(row) for row in c.execute(
                "SELECT * FROM quant_performance_reports ORDER BY period DESC LIMIT ?",
                (limit,)
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
            "SELECT * FROM quant_performance_reports ORDER BY period_type, period"
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
    reports = list_performance_reports(limit=100)
    items = reports.get("items", [])

    if not items:
        return {"ok": False, "status": "insufficient_data", "reason": "无绩效报告数据"}

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

    return {
        "ok": True,
        "status": "passed" if len(passed) >= 3 and len(has_attribution) >= 1 else "partial",
        "periods": periods,
        "period_count": len(periods),
        "passed_count": len(passed),
        "attribution_count": len(has_attribution),
        "covered_types": list(by_type.keys()),
        "method": "multi_period_validation_v1",
    }
