"""Validation and normalization for claimed daily rebalance plan inputs."""

from __future__ import annotations

import math
from typing import Any

from app.services.quant_run_contracts import ReplayRiskGateError


def normalize_authoritative_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ReplayRiskGateError("正式调仓计划格式无效", code="invalid_authoritative_plan")
    required = ("plan_id", "plan_version", "input_hash", "execution_claim_token")
    missing = [key for key in required if not str(plan.get(key) or "").strip()]
    if missing:
        raise ReplayRiskGateError(
            "正式调仓计划缺少冻结字段：" + ",".join(missing),
            code="invalid_authoritative_plan",
        )
    raw_symbols = [str(value or "").strip() for value in plan.get("target_symbols") or []]
    symbols = [value for value in raw_symbols if value]
    if len(symbols) != len(set(symbols)):
        raise ReplayRiskGateError("正式调仓计划包含重复标的", code="invalid_authoritative_plan")
    raw_weights = plan.get("target_weights") if isinstance(plan.get("target_weights"), dict) else {}
    weights: dict[str, float] = {}
    for symbol, value in raw_weights.items():
        key = str(symbol or "").strip()
        try:
            weight = float(value)
        except (TypeError, ValueError):
            raise ReplayRiskGateError(f"正式调仓计划权重无效：{key}", code="invalid_authoritative_plan") from None
        if not key or not math.isfinite(weight) or weight < 0:
            raise ReplayRiskGateError(f"正式调仓计划权重非法：{key}", code="invalid_authoritative_plan")
        if weight > 0:
            weights[key] = weight
    if set(weights) - set(symbols):
        raise ReplayRiskGateError("正式调仓计划权重包含目标池外标的", code="invalid_authoritative_plan")
    if sum(weights.values()) > 1.000001:
        raise ReplayRiskGateError("正式调仓计划目标权重超过100%", code="invalid_authoritative_plan")
    proposal = [item for item in plan.get("proposal") or [] if isinstance(item, dict)]
    return {
        **plan,
        "target_symbols": symbols,
        "target_weights": weights,
        "proposal": proposal,
        "risk_budget": plan.get("risk_budget") if isinstance(plan.get("risk_budget"), dict) else {},
        "decision_version_manifest": (
            plan.get("decision_version_manifest")
            if isinstance(plan.get("decision_version_manifest"), dict) else {}
        ),
        "plan_provenance": {
            "source": "daily_rebalance_plans",
            "plan_id": int(plan["plan_id"]),
            "plan_version": str(plan["plan_version"]),
            "input_hash": str(plan["input_hash"]),
            "execution_claim_token": str(plan["execution_claim_token"]),
        },
    }


def build_authoritative_selection_detail(
    plan: dict[str, Any], *, signal_date: str, exchange_group: str,
) -> dict[str, Any]:
    target_symbols = set(plan["target_symbols"])
    return {
        "signal_date": signal_date,
        "exchange_group": exchange_group,
        "strategy_template": {"id": "authoritative_daily_plan", "name": "统一日调仓计划"},
        "risk_budget": plan["risk_budget"],
        "selections": [
            {
                "symbol": str(item.get("symbol") or ""),
                "name": str(item.get("name") or ""),
                "score": float(item.get("expected_20d_return") or 0) * 100,
            }
            for item in plan["proposal"]
            if str(item.get("symbol") or "") in target_symbols
        ],
        "blocked_events": [],
        "sell_triggers": [],
        "plan_provenance": plan["plan_provenance"],
        "decision_version_manifest": plan["decision_version_manifest"],
    }
