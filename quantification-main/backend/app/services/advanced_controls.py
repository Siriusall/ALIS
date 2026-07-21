"""Plan-book controls that are deterministic, explainable and testable."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from statistics import quantiles
from typing import Any


def black_litterman_weights(
    prior_weights: dict[str, float], views: dict[str, float], confidences: dict[str, float] | None = None,
    *, max_weight: float = 0.25,
) -> dict[str, Any]:
    """A transparent diagonal BL posterior for portfolio views.

    ``views`` are active-return views in weight space; confidence blends each
    view with the market prior.  The result is capped and renormalized, with
    every adjustment returned for audit.  A full covariance posterior can be
    layered on later without changing the contract.
    """
    keys = sorted(set(prior_weights) | set(views))
    if not keys:
        return {"weights": {}, "adjustments": {}, "method": "black_litterman_diagonal_v1"}
    prior = {k: max(0.0, float(prior_weights.get(k, 0) or 0)) for k in keys}
    total = sum(prior.values()) or 1.0
    prior = {k: v / total for k, v in prior.items()}
    conf = confidences or {}
    raw = {}
    for key in keys:
        c = max(0.0, min(1.0, float(conf.get(key, 0) or 0)))
        view = float(views.get(key, 0) or 0)
        raw[key] = max(0.0, prior[key] + c * view)
    cap = max(0.0, float(max_weight))
    weights = {k: min(cap, v) for k, v in raw.items()}
    total = sum(weights.values())
    if total > 1.0:
        weights = {k: v / total for k, v in weights.items()}
    else:
        remainder = 1.0 - total
        eligible = [key for key in keys if weights[key] < cap - 1e-12]
        while remainder > 1e-10 and eligible:
            addition = remainder / len(eligible)
            progressed = 0.0
            next_eligible = []
            for key in eligible:
                increment = min(addition, cap - weights[key])
                weights[key] += increment
                progressed += increment
                if weights[key] < cap - 1e-12:
                    next_eligible.append(key)
            remainder -= progressed
            if progressed <= 1e-12:
                break
            eligible = next_eligible
    weights = {k: round(v, 8) for k, v in weights.items()}
    cash_weight = round(max(0.0, 1.0 - sum(weights.values())), 8)
    return {"weights": weights, "cash_weight": cash_weight, "prior_weights": prior, "adjustments": {k: round(weights[k] - prior[k], 8) for k in keys}, "method": "black_litterman_diagonal_v1", "max_weight": max_weight}


def bayesian_update(prior: float, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Update a thesis probability from explicit likelihood ratios."""
    probability = max(1e-6, min(1 - 1e-6, float(prior)))
    applied = []
    for item in evidence:
        ratio = float(item.get("likelihood_ratio", 1.0) or 1.0)
        if not math.isfinite(ratio) or ratio <= 0:
            continue
        odds = probability / (1 - probability)
        odds *= ratio
        probability = odds / (1 + odds)
        applied.append({"source": item.get("source"), "likelihood_ratio": ratio, "checked_at": item.get("checked_at") or datetime.now().isoformat(timespec="seconds")})
    return {"prior": float(prior), "posterior": round(probability, 8), "evidence_applied": applied, "method": "bayes_odds_update_v1"}


def trade_attribution(*, entry_price: float, exit_price: float, quantity: float, benchmark_return: float = 0.0, industry_return: float = 0.0, execution_cost: float = 0.0) -> dict[str, Any]:
    gross = (float(exit_price) - float(entry_price)) * float(quantity)
    capital = max(1e-9, float(entry_price) * float(quantity))
    total_return = (float(exit_price) / float(entry_price) - 1.0) if float(entry_price) else 0.0
    return {"gross_pnl": round(gross, 4), "total_return": round(total_return, 8), "market_attribution": round(float(benchmark_return) * capital, 4), "industry_attribution": round((float(industry_return) - float(benchmark_return)) * capital, 4), "selection_attribution": round((total_return - float(industry_return)) * capital, 4), "execution_attribution": round(-float(execution_cost), 4), "method": "single_lot_brinson_proxy_v1", "limitations": ["需要真实成交批次和基准/行业同步价格才能作为正式归因"]}


def evaluate_slo(samples: list[dict[str, Any]], *, freshness_seconds: float = 86400.0, success_target: float = .99) -> dict[str, Any]:
    latencies = sorted(float(x.get("latency_ms", 0) or 0) for x in samples if x.get("latency_ms") is not None)
    successes = sum(1 for x in samples if str(x.get("status") or "").lower() in {"ok", "success", "ready"})
    fresh = sum(1 for x in samples if float(x.get("freshness_seconds", 0) or 0) <= freshness_seconds)
    n = len(samples)
    p95 = latencies[min(len(latencies) - 1, max(0, math.ceil(len(latencies) * .95) - 1))] if latencies else 0.0
    success_rate = successes / n if n else 0.0
    freshness_rate = fresh / n if n else 0.0
    return {"sample_count": n, "success_rate": round(success_rate, 6), "freshness_rate": round(freshness_rate, 6), "p95_latency_ms": round(p95, 3), "status": "ready" if n and success_rate >= success_target and freshness_rate >= success_target else "degraded" if n else "unknown", "targets": {"success_rate": success_target, "freshness_rate": success_target, "freshness_seconds": freshness_seconds}}


def multi_period_oos_report(periods: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate fixed-period out-of-sample reports without hiding failures."""
    normalized = []
    for item in periods:
        observations = int(item.get("observations", 0) or 0)
        cost_after_return = item.get("cost_after_return")
        market_state = str(item.get("market_state") or "")
        blocked_reasons = []
        if observations <= 0:
            blocked_reasons.append("missing_observations")
        if not market_state:
            blocked_reasons.append("missing_market_state_slice")
        if cost_after_return is None:
            blocked_reasons.append("missing_cost_after_return")
        normalized.append({
            "period": item.get("period"),
            "horizon": item.get("horizon"),
            "market_state": market_state,
            "holdout_lift": float(item.get("holdout_lift", 0) or 0),
            "walk_forward_lift": float(item.get("walk_forward_lift", 0) or 0),
            "cost_after_return": None if cost_after_return is None else float(cost_after_return),
            "observations": observations,
            "purge_gap_days": int(item.get("purge_gap_days", 0) or 0),
            "status": "blocked_by_data" if blocked_reasons else "passed" if float(item.get("holdout_lift", 0) or 0) > 0 and float(item.get("walk_forward_lift", 0) or 0) > 0 and float(cost_after_return) > 0 else "failed",
            "blocked_reasons": blocked_reasons,
        })
    passed = [x for x in normalized if x["status"] == "passed"]
    blocked = [x for x in normalized if x["status"] == "blocked_by_data"]
    failed = [x for x in normalized if x["status"] == "failed"]
    status = "insufficient_data" if not normalized else "blocked_by_data" if blocked else "failed" if failed else "passed"
    return {
        "periods": normalized,
        "period_count": len(normalized),
        "passed_count": len(passed),
        "blocked_by_data_count": len(blocked),
        "failed_count": len(failed),
        "status": status,
        "method": "purged_walk_forward_market_state_oos_protocol_v1",
        "requires_all_periods_pass": True,
        "required_fields": ["observations", "market_state", "holdout_lift", "walk_forward_lift", "cost_after_return"],
    }


def freeze_evidence(payload: dict[str, Any], *, schema_version: str = "evidence_freeze_v1") -> dict[str, Any]:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"schema_version": schema_version, "evidence_hash": digest, "frozen_at": datetime.now().isoformat(timespec="seconds"), "payload": payload}
