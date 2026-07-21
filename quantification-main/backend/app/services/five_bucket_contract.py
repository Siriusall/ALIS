from __future__ import annotations

from datetime import datetime
from typing import Any


BUCKETS: dict[str, dict[str, Any]] = {
    "medium": {"name": "中期景气趋势仓", "risk_group": "stable", "target": 0.35, "range": [0.25, 0.45], "holding_period": "20-90个交易日"},
    "defensive": {"name": "防御与流动性仓", "risk_group": "stable", "target": 0.15, "range": [0.05, 0.25], "holding_period": "动态"},
    "narrative": {"name": "主线叙事仓", "risk_group": "offensive", "target": 0.25, "range": [0.15, 0.30], "holding_period": "3-20个交易日"},
    "leader": {"name": "龙头趋势仓", "risk_group": "offensive", "target": 0.15, "range": [0.10, 0.20], "holding_period": "5-30个交易日"},
    "event": {"name": "事件与预期差仓", "risk_group": "offensive", "target": 0.10, "range": [0.05, 0.15], "holding_period": "1-15个交易日"},
}


def infer_function_bucket(*, symbol: str, score: float = 0, evidence: dict[str, Any] | None = None) -> str:
    payload = evidence or {}
    if payload.get("defensive_asset") or str(symbol).startswith(("511", "518", "510880", "512890")):
        return "defensive"
    lifecycle = str(payload.get("narrative_lifecycle") or payload.get("lifecycle") or "")
    event_score = float(payload.get("event_score") or 0)
    leader_score = float(payload.get("leader_score") or score or 0)
    if event_score >= 65 or payload.get("event_id"):
        return "event"
    if leader_score >= 80 and lifecycle in {"启动", "扩散", "加速", "景气上行"}:
        return "leader"
    if payload.get("medium_horizon") or lifecycle in {"景气上行", "稳定扩张"}:
        return "medium"
    if lifecycle and lifecycle not in {"退潮", "沉寂", "无主线"}:
        return "narrative"
    return "narrative" if score >= 70 else "medium"


def build_target_weight_contract(
    *,
    symbol: str,
    target_weight: float,
    current_weight: float = 0.0,
    score: float = 0.0,
    evidence: dict[str, Any] | None = None,
    data_status: str = "ready",
) -> dict[str, Any]:
    payload = dict(evidence or {})
    bucket_key = infer_function_bucket(symbol=symbol, score=score, evidence=payload)
    bucket = BUCKETS[bucket_key]
    target = max(0.0, float(target_weight or 0.0))
    current = max(0.0, float(current_weight or 0.0))
    lifecycle = str(payload.get("narrative_lifecycle") or payload.get("lifecycle") or "未识别")
    funding = payload.get("funding_confirmation")
    if funding is None:
        funding = "已确认" if float(payload.get("capital_persistence", 0) or 0) >= 0.55 else "待确认"
    source = str(payload.get("return_source") or bucket["name"])
    exit_conditions = list(payload.get("exit_conditions") or [])
    if not exit_conditions:
        exit_conditions = [
            "触发风险硬否决",
            "数据状态降级为blocked/missing",
            "目标权重低于当前权重且成本后风险收益不再占优",
        ]
        if bucket_key in {"narrative", "leader", "event"}:
            exit_conditions.append("叙事生命周期进入退潮或资金确认连续转弱")
    source_status = {
        "function_bucket": "model" if payload.get("bucket_model") else "rule",
        "return_source": "model" if payload.get("return_model") else "thesis" if payload.get("return_source") else "rule",
        "holding_period": "model" if payload.get("holding_period_model") else "policy",
        "narrative_lifecycle": "model" if payload.get("narrative_model") else "thesis" if lifecycle != "未识别" else "proxy",
        "funding_confirmation": "model" if payload.get("capital_model") else "capital_proxy" if "capital_persistence" in payload else "proxy",
        "exit_conditions": "model" if payload.get("exit_model") else "risk_policy",
    }
    try:
        from app.services.model_evidence_contract import EVIDENCE_BINDINGS
        evidence_bindings = {key: EVIDENCE_BINDINGS.get(key, {}) for key in source_status}
    except Exception:
        evidence_bindings = {}
    model_evidence = {
        "bucket_model": payload.get("bucket_model") or "five_bucket_rule_v1",
        "return_model": payload.get("return_model") or payload.get("model_key") or "thesis_proxy_v1",
        "capital_model": payload.get("capital_model") or "capital_persistence_proxy_v1",
        "narrative_model": payload.get("narrative_model") or "narrative_lifecycle_proxy_v1",
        "exit_model": payload.get("exit_model") or "risk_exit_policy_v1",
    }
    evidence_quality = "model_backed" if all(value == "model" for value in source_status.values()) else "hybrid" if any(value == "model" for value in source_status.values()) else "proxy_backed"
    return {
        "schema_version": "five_bucket_target_weight_contract_v1",
        "symbol": symbol,
        "function_bucket": bucket_key,
        "function_bucket_name": bucket["name"],
        "risk_group": bucket["risk_group"],
        "bucket_target_weight": bucket["target"],
        "bucket_range": bucket["range"],
        "target_weight": round(target, 6),
        "current_weight": round(current, 6),
        "delta": round(target - current, 6),
        "return_source": source,
        "holding_period": str(payload.get("holding_period") or bucket["holding_period"]),
        "narrative_lifecycle": lifecycle,
        "funding_confirmation": funding,
        "exit_conditions": exit_conditions,
        "data_status": data_status,
        "evidence_quality": evidence_quality,
        "evidence_source_status": source_status,
        "model_evidence": model_evidence,
        "evidence_bindings": evidence_bindings,
        "requires_model_upgrade": evidence_quality != "model_backed",
        "evidence": payload,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def summarize_bucket_contracts(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    by_bucket = {
        key: {"name": value["name"], "target": value["target"], "actual": 0.0, "count": 0, "range": value["range"]}
        for key, value in BUCKETS.items()
    }
    for item in contracts:
        key = str(item.get("function_bucket") or "liquidity")
        by_bucket.setdefault(key, {"name": key, "target": 0.0, "actual": 0.0, "count": 0, "range": [0, 1]})
        by_bucket[key]["actual"] += float(item.get("target_weight") or 0)
        by_bucket[key]["count"] += 1
    for item in by_bucket.values():
        item["actual"] = round(float(item["actual"]), 6)
        low, high = item.get("range") or [0, 1]
        item["status"] = "within_range" if float(low) <= item["actual"] <= float(high) else "outside_range"
    stable = sum(v["actual"] for k, v in by_bucket.items() if BUCKETS.get(k, {}).get("risk_group") == "stable")
    offensive = sum(v["actual"] for k, v in by_bucket.items() if BUCKETS.get(k, {}).get("risk_group") == "offensive")
    return {"buckets": by_bucket, "risk_groups": {"stable": round(stable, 6), "offensive": round(offensive, 6)}}
