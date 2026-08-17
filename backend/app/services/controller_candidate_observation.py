"""Full isolated portfolio observation for StrategyController candidates."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db.store import get_model_version, list_model_versions, update_model_version_validation
from app.services.backtest_data_source import BacktestDataSource
from app.services.candidate_simulation_ledger import persist_candidate_replay
from app.services.data_health import assess_latest_decision_data_health
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.factor_platform import registered_factor_bindings
from app.services.strategy_config import get_replay_required_factor_names
from app.services.model_signal_fusion import MODEL_KEYS


CONTRACT = "alis_controller_candidate_immediate_observation_v1"


def _observation_allowed(version: dict[str, Any]) -> bool:
    if bool(version.get("is_active")) or str(version.get("status") or "") != "candidate":
        return False
    validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
    review = validation.get("review") if isinstance(validation.get("review"), dict) else {}
    blocked = {str(item) for item in validation.get("blocked_reasons") or []}
    return (
        str(validation.get("status") or "") != "rejected"
        and str(review.get("status") or "") != "rejected"
        and "operator_rejected" not in blocked
        and "human_review_rejected" not in blocked
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _latest_ready_date() -> str:
    health = assess_latest_decision_data_health()
    if not bool(health.get("experiment_ready")):
        return ""
    readiness = health.get("readiness") if isinstance(health.get("readiness"), dict) else {}
    return str(readiness.get("target_trade_date") or "").replace("-", "")[:8]


def run_controller_candidate_observation(
    model_version_id: int,
    *,
    through_trade_date: str,
) -> dict[str, Any]:
    from app.services.strategy_replay import run_strategy_replay

    version = get_model_version(int(model_version_id))
    if (
        not version
        or str(version.get("model_key") or "") != "strategy_controller"
        or bool(version.get("is_active"))
    ):
        raise ValueError("StrategyController observation requires an inactive candidate")
    champion = resolve_production_strategy_champion()
    through = str(through_trade_date or "").replace("-", "")[:8]
    created_date = str(version.get("created_at") or "")[:10].replace("-", "")
    dates = BacktestDataSource().get_available_trade_dates(created_date, through)
    if len(dates) < 2:
        return {
            "ok": True,
            "status": "awaiting_realized_future_return",
            "model_version_id": int(model_version_id),
            "production_change_forbidden": True,
        }
    strategy = champion.get("strategy") or {}
    factor_bindings = dict(champion.get("factor_bindings") or {})
    champion_model_bindings = dict(champion.get("model_bindings") or {})
    replay_model_bindings = {
        key: champion_model_bindings.get(key)
        for key in MODEL_KEYS
    }
    required_factors = get_replay_required_factor_names(
        strategy.get("content") or {},
        str(strategy.get("type") or "formula"),
        model_bindings=replay_model_bindings,
    )
    research_bindings = registered_factor_bindings(
        required_factors,
        statuses=("active", "candidate"),
    )
    for factor_key in required_factors:
        if not isinstance(factor_bindings.get(factor_key), dict):
            binding = research_bindings.get(factor_key)
            if isinstance(binding, dict):
                factor_bindings[factor_key] = binding
    missing_factors = sorted(
        key for key in required_factors
        if not isinstance(factor_bindings.get(key), dict)
    )
    if missing_factors:
        raise ValueError(
            "Controller observation missing replay factor bindings: "
            + ",".join(missing_factors)
        )
    frozen = {
        "schema_version": CONTRACT,
        "candidate_version_id": int(model_version_id),
        "champion_id": int(champion["champion_id"]),
        "strategy_id": int(champion["strategy_id"]),
        "factor_bindings": factor_bindings,
        "model_bindings": replay_model_bindings,
        "source_model_binding_keys": sorted(champion_model_bindings),
        "market_state_binding": champion.get("market_state_binding") or {},
        "execution_contract": champion.get("execution_contract") or {},
    }
    contract_hash = _hash(frozen)
    replay = run_strategy_replay(
        strategy_id=int(champion["strategy_id"]),
        start_date=dates[0],
        end_date=dates[-1],
        factor_bindings=factor_bindings,
        model_bindings=replay_model_bindings,
        market_state_binding=dict(champion.get("market_state_binding") or {}),
        strategy_controller_version_id=int(model_version_id),
        strategy_content=strategy.get("content") or {},
        strategy_name=str(strategy.get("name") or ""),
        research_package_hash=contract_hash,
        data_source=BacktestDataSource(),
    )
    audit = persist_candidate_replay(int(model_version_id), replay)
    if not bool(audit.get("ok")):
        raise RuntimeError("controller candidate isolated ledger audit failed")
    latest = get_model_version(int(model_version_id)) or version
    validation = dict(latest.get("validation") or {})
    validation["controller_simulation_observation"] = {
        "schema_version": CONTRACT,
        "status": "observing",
        "start_trade_date": dates[0],
        "through_trade_date": dates[-1],
        "observed_sessions": max(0, len(dates) - 1),
        "contract_hash": contract_hash,
        "ledger_audit": audit,
        "production_change_forbidden": True,
    }
    validation["production_eligible"] = False
    update_model_version_validation(int(model_version_id), validation=validation)
    return {
        "ok": True,
        "status": "observing",
        "model_version_id": int(model_version_id),
        "observed_sessions": max(0, len(dates) - 1),
        "ledger_audit": audit,
        "production_change_forbidden": True,
    }


def ensure_controller_candidate_observation_tasks() -> dict[str, Any]:
    from app.services.evolution_orchestrator import create_evolution_task

    through = _latest_ready_date()
    if not through:
        return {"ok": True, "status": "blocked_by_decision_data", "queued": []}
    queued = []
    for version in list_model_versions("strategy_controller", limit=200):
        if not _observation_allowed(version):
            continue
        task = create_evolution_task(
            task_type="controller_candidate_observation",
            payload={
                "model_version_id": int(version["id"]),
                "through_trade_date": through,
                "production_change_forbidden": True,
            },
            dedupe_key=f"controller_candidate_observation:v3:{int(version['id'])}:{through}",
            priority=69,
        )
        queued.append({"model_version_id": int(version["id"]), "task_id": int(task["id"])})
    return {"ok": True, "status": "ready", "through_trade_date": through, "queued": queued}
