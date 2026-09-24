"""Read-only portfolio console projections."""

from __future__ import annotations

import json
from typing import Any

from app.db.store import _conn as business_conn


def build_portfolio_console_research() -> dict[str, Any]:
    """Read frozen portfolio competition evidence without rerunning research."""
    with business_conn() as c:
        row = c.execute(
            """SELECT p.id,r.trade_date,r.version,r.status,r.generated_at,
                      p.risk_budget_json
                 FROM daily_rebalance_plan_projection p
                 JOIN daily_rebalance_plans r ON r.id=p.plan_id
                ORDER BY r.trade_date DESC,r.version DESC,r.id DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {
            "ok": True,
            "schema_version": "profit_loop_console_v1",
            "status": "not_generated",
            "read_only": True,
            "evidence_available": False,
            "portfolio_optimization": {"optimizer": {"method_competition": {"evaluations": []}}},
        }
    item = dict(row)
    try:
        contract = json.loads(item.get("risk_budget_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        contract = {}
    competition = contract.get("portfolio_method_competition")
    if not isinstance(competition, dict):
        competition = {}
    fields = (
        "method", "objective", "cvar_20d", "cvar", "turnover",
        "estimated_total_cost_cny", "estimated_one_way_cost", "eligible",
        "executable_order_count", "sample_count", "volatility_20d",
        "max_drawdown_history",
    )
    evaluations = [
        {field: evaluation[field] for field in fields if field in evaluation}
        for evaluation in competition.get("evaluations", [])
        if isinstance(evaluation, dict)
    ]
    winner = competition.get("winner") if isinstance(competition.get("winner"), dict) else {}
    compact_competition = {
        "ok": bool(competition.get("ok")),
        "schema_version": str(competition.get("schema_version") or ""),
        "selected_method": str(competition.get("selected_method") or ""),
        "evaluations": evaluations,
        "winner": {field: winner[field] for field in fields if field in winner},
        "target_budget": competition.get("target_budget"),
        "policy": competition.get("policy") or {},
    }
    return {
        "ok": True,
        "schema_version": "profit_loop_console_v1",
        "status": "ready" if evaluations else "evidence_unavailable",
        "read_only": True,
        "evidence_available": bool(evaluations),
        "source": {
            "kind": "frozen_daily_rebalance_plan",
            "plan_id": int(item.get("id") or 0),
            "trade_date": str(item.get("trade_date") or ""),
            "version": int(item.get("version") or 0),
            "plan_status": str(item.get("status") or ""),
            "generated_at": item.get("generated_at"),
        },
        "portfolio_optimization": {"optimizer": {"method_competition": compact_competition}},
        "risk_budget": contract.get("risk_budget") or contract,
    }
