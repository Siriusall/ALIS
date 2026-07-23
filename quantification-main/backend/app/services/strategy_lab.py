from __future__ import annotations

from typing import Any

from app.db.store import get_strategy, list_model_versions
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_lifecycle import (
    list_shadow_observations,
    list_strategy_promotion_events,
    shadow_promotion_gate,
)


CHALLENGER_MODEL_KEY = "alis_strategy_challenger"
BASELINE_MODEL_KEY = "alis_strategy_baseline"
BASELINE_NAMES = {
    "multifactor_equal_weight": "多因子等权",
    "simple_momentum": "简单动量",
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _shadow_observations(model_version_id: int) -> list[dict[str, Any]]:
    try:
        return list_shadow_observations(model_version_id)
    except Exception:
        return []


def _promotion_events() -> list[dict[str, Any]]:
    try:
        return list_strategy_promotion_events(limit=20)
    except Exception:
        return []


def _challenger_card(version: dict[str, Any]) -> dict[str, Any]:
    config = version.get("config") if isinstance(version.get("config"), dict) else {}
    selection = config.get("selection_strategy") if isinstance(config.get("selection_strategy"), dict) else {}
    controls = selection.get("challenger_controls") if isinstance(selection.get("challenger_controls"), dict) else {}
    rebalance = selection.get("rebalance") if isinstance(selection.get("rebalance"), dict) else {}
    formal = (version.get("metrics") or {}).get("formal_oos")
    formal = formal if isinstance(formal, dict) else {}
    validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
    window_returns = formal.get("window_returns") if isinstance(formal.get("window_returns"), list) else []
    strategy_id = int(config.get("strategy_id") or 0)
    strategy = get_strategy(strategy_id) if strategy_id else None
    observations = _shadow_observations(int(version["id"]))
    shadow_gate = (
        validation.get("shadow_gate")
        if isinstance(validation.get("shadow_gate"), dict)
        else shadow_promotion_gate(observations)
    )
    quick = (version.get("metrics") or {}).get("quick_diagnostic")
    quick = quick if isinstance(quick, dict) else {}
    failure_attribution = formal.get("failure_attribution")
    failure_attribution = failure_attribution if isinstance(failure_attribution, dict) else {}
    return {
        "model_version_id": int(version["id"]),
        "strategy_id": strategy_id,
        "name": str((strategy or {}).get("name") or "ALIS Challenger 换手追高控制 V1"),
        "version": str(version.get("version") or ""),
        "role": "challenger",
        "status": str(validation.get("status") or version.get("status") or ""),
        "production_eligible": bool(validation.get("production_eligible")),
        "champion_reference": str(config.get("champion_reference") or ""),
        "created_at": str(version.get("created_at") or ""),
        "updated_at": str(version.get("updated_at") or ""),
        "policy": {
            "rebalance_frequency": str(rebalance.get("freq") or ""),
            "top_n": int(rebalance.get("top_n") or 0),
            "max_single_weight": _number(controls.get("max_single_weight")),
            "max_rebalance_turnover": _number(controls.get("max_rebalance_turnover")),
            "min_weight_change": _number(controls.get("min_weight_change")),
            "max_entry_1d_return": _number(controls.get("max_entry_1d_return")),
            "max_entry_5d_return": _number(controls.get("max_entry_5d_return")),
            "rationale": controls.get("rationale") if isinstance(controls.get("rationale"), list) else [],
        },
        "formal_oos": {
            "status": str(formal.get("status") or "pending"),
            "valid_windows": int(formal.get("valid_windows") or 0),
            "outperform_windows": int(formal.get("outperform_windows") or 0),
            "outperform_ratio": _number(formal.get("outperform_ratio")),
            "aggregate_cost_after_return": _number(formal.get("aggregate_cost_after_return")),
            "aggregate_benchmark_return": _number(formal.get("aggregate_benchmark_return")),
            "aggregate_excess_return": _number(formal.get("aggregate_excess_return")),
            "worst_window_drawdown": _number(formal.get("worst_window_drawdown")),
            "total_fees": _number(formal.get("total_fees")),
            "windows": [
                {"window": index + 1, "return": _number(value)}
                for index, value in enumerate(window_returns)
            ],
            "failure_attribution": {
                "status": str(failure_attribution.get("status") or "pending"),
                "primary_cause": str(failure_attribution.get("primary_cause") or "unknown"),
                "confidence": str(failure_attribution.get("confidence") or "low"),
                "evidence": failure_attribution.get("evidence") if isinstance(failure_attribution.get("evidence"), dict) else {},
                "limitations": failure_attribution.get("limitations") if isinstance(failure_attribution.get("limitations"), list) else [],
            },
        },
        "passed_gates": validation.get("passed_gates") if isinstance(validation.get("passed_gates"), list) else [],
        "blocking_reasons": validation.get("blocking_reasons") if isinstance(validation.get("blocking_reasons"), list) else [],
        "next_gate": str(validation.get("next_gate") or ""),
        "quick_diagnostic": quick,
        "shadow": {
            "status": str(validation.get("status") or ""),
            "session_count": len(observations),
            "minimum_sessions": int(validation.get("shadow_minimum_sessions") or 20),
            "started_after": str(validation.get("shadow_started_after") or ""),
            "gate": shadow_gate,
            "latest_trade_date": str(observations[-1].get("trade_date") or "") if observations else "",
        },
    }


def build_strategy_lab() -> dict[str, Any]:
    champion = resolve_production_strategy_champion()
    versions = list_model_versions(CHALLENGER_MODEL_KEY, limit=20)
    challengers = [_challenger_card(version) for version in versions]
    baseline_versions = list_model_versions(BASELINE_MODEL_KEY, limit=20)
    baselines = []
    for version in baseline_versions:
        card = _challenger_card(version)
        config = version.get("config") if isinstance(version.get("config"), dict) else {}
        baseline_key = str(config.get("baseline_key") or "")
        card.update({
            "name": BASELINE_NAMES.get(baseline_key, baseline_key or "诊断基线"),
            "role": "baseline",
            "baseline_key": baseline_key,
        })
        baselines.append(card)
    return {
        "ok": True,
        "schema_version": "alis_strategy_lab_v1",
        "champion": {
            "model_version_id": int(champion["champion_id"]),
            "strategy_id": int(champion["strategy_id"]),
            "name": str((champion.get("strategy") or {}).get("name") or "ALIS Champion"),
            "version": str(champion.get("champion_version") or ""),
            "role": "champion",
            "is_active": bool(champion.get("is_active")),
        },
        "challengers": challengers,
        "baselines": baselines,
        "promotion_events": _promotion_events(),
        "summary": {
            "challenger_count": len(challengers),
            "baseline_count": len(baselines),
            "production_eligible_count": sum(1 for item in challengers if item["production_eligible"]),
            "display_policy": "autonomous_deterministic_promotion_and_rollback",
        },
    }
