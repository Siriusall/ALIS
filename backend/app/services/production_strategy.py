from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.db.store import (
    create_model_version,
    get_active_model_version,
    get_default_strategy_id,
    get_model_version,
    get_strategy,
    list_model_versions,
    set_active_model_version,
    update_model_version_metrics,
    update_model_version_status,
    update_model_version_validation,
)

CHAMPION_MODEL_KEY = "alis_production_strategy"
CHAMPION_SCHEMA_VERSION = "alis_production_strategy_bundle_v1"
CONTROLLER_MODEL_KEY = "strategy_controller"
MODEL_BINDING_KEYS = (
    "main",
    "medium_horizon_lambdarank_v1",
    "capital_persistence_multitask_v1",
    "leader_lambdarank_v1",
    "event_surprise_lgbm_v1",
    "market_regime_hsmm_v1",
    "narrative_transition_multitask_v1",
)


def _model_ref(model_key: str) -> dict[str, Any] | None:
    version = get_active_model_version(model_key)
    if not version:
        return None
    validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
    return {
        "id": int(version["id"]),
        "model_key": model_key,
        "version": str(version.get("version") or ""),
        "status": str(version.get("status") or ""),
        "production_eligible": bool(validation.get("production_eligible")),
    }


def _build_bundle_config() -> dict[str, Any]:
    strategy_id = int(get_default_strategy_id() or 0)
    strategy = get_strategy(strategy_id) if strategy_id else None
    if not strategy:
        raise RuntimeError("ALIS Champion 选股策略不存在，请先执行数据库初始化")
    controller = get_active_model_version(CONTROLLER_MODEL_KEY)
    if not controller:
        from app.services.strategy_controller import ensure_default_strategy_policy_version

        controller = ensure_default_strategy_policy_version()
    from app.services.factor_platform import active_factor_bindings
    from app.services.strategy_config import get_strategy_factor_names

    factor_keys = get_strategy_factor_names(strategy.get("content"), strategy.get("type", "formula"))
    return {
        "schema_version": CHAMPION_SCHEMA_VERSION,
        "name": "ALIS Champion 综合生产策略",
        "role": "champion",
        "strategy_id": strategy_id,
        "selection_strategy": {
            "id": strategy_id,
            "name": strategy.get("name"),
            "type": strategy.get("type"),
            "content": strategy.get("content"),
        },
        "strategy_controller": {
            "id": int(controller["id"]),
            "version": str(controller.get("version") or ""),
        },
        "model_bindings": {key: _model_ref(key) for key in MODEL_BINDING_KEYS},
        "factor_bindings": active_factor_bindings(factor_keys),
        "portfolio_optimizer": {
            "schema_version": "portfolio_cvar_optimizer_v2",
            "implementation": "empirical_cvar_slsqp_v2",
            "fallback": "hierarchical_risk_parity_fallback_v2",
        },
        "execution_contract": {
            "signal": "T_close",
            "execution": "T_plus_1_open",
            "fees": "a_share_fee_schedule",
            "constraints": ["cash", "lot_size", "suspension", "limit_up_down", "liquidity", "no_oversell"],
        },
    }


def ensure_production_strategy_champion(*, force_rebind: bool = False) -> dict[str, Any]:
    active = get_active_model_version(CHAMPION_MODEL_KEY)
    if active and not force_rebind:
        return active
    config = _build_bundle_config()
    version = f"alis-champion-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    model_refs = [value for value in config["model_bindings"].values() if value]
    eligible_refs = [value for value in model_refs if value.get("production_eligible")]
    version_id = create_model_version(
        model_key=CHAMPION_MODEL_KEY,
        version=version,
        status="active",
        artifact_path="builtin://alis/production-strategy/champion",
        config=config,
        metrics={"bound_models": len(model_refs), "production_eligible_models": len(eligible_refs)},
        validation={
            "status": "passed",
            "production_eligible": True,
            "role": "champion",
            "limitations": [key for key, value in config["model_bindings"].items() if value is None],
            "binding_policy": "single_active_champion_per_model_key",
        },
        is_active=True,
    )
    if active and int(active.get("id") or 0) != int(version_id):
        update_model_version_status(int(active["id"]), status="superseded", is_active=False)
    return get_model_version(version_id) or {"id": version_id, "version": version, "config": config, "is_active": True}


def resolve_production_strategy_champion() -> dict[str, Any]:
    champion = ensure_production_strategy_champion()
    config = champion.get("config") if isinstance(champion.get("config"), dict) else {}
    strategy_id = int(config.get("strategy_id") or 0)
    strategy = config.get("selection_strategy") if isinstance(config.get("selection_strategy"), dict) else None
    controller_id = int((config.get("strategy_controller") or {}).get("id") or 0)
    controller = get_model_version(controller_id) if controller_id else None
    if not strategy or not controller:
        raise RuntimeError("ALIS Champion 绑定的选股策略或 StrategyController 版本不存在")
    return {
        "champion_id": int(champion["id"]),
        "champion_version": str(champion.get("version") or ""),
        "champion_model_key": CHAMPION_MODEL_KEY,
        "strategy_id": int((strategy or {}).get("id") or 0),
        "strategy": strategy,
        "strategy_controller_version_id": int((controller or {}).get("id") or 0),
        "strategy_controller_version": str((controller or {}).get("version") or ""),
        "strategy_policy": ((controller or {}).get("config") or {}).get("policy") or {},
        "model_bindings": config.get("model_bindings") or {},
        "factor_bindings": config.get("factor_bindings") or {},
        "portfolio_optimizer": config.get("portfolio_optimizer") or {},
        "execution_contract": config.get("execution_contract") or {},
        "metrics": champion.get("metrics") or {},
        "validation": champion.get("validation") or {},
        "is_active": bool(champion.get("is_active")),
    }


def production_strategy_status() -> dict[str, Any]:
    champion = resolve_production_strategy_champion()
    versions = list_model_versions(CHAMPION_MODEL_KEY, limit=20)
    return {
        "ok": True,
        "schema_version": "alis_production_strategy_status_v1",
        "champion": champion,
        "active_champion_count": sum(1 for version in versions if version.get("is_active")),
        "version_count": len(versions),
    }


def champion_execution_gate() -> dict[str, Any]:
    """Return the fail-closed execution policy for the active Champion."""
    champion = resolve_production_strategy_champion()
    metrics = champion.get("metrics") if isinstance(champion.get("metrics"), dict) else {}
    formal = metrics.get("formal_oos") if isinstance(metrics.get("formal_oos"), dict) else {}
    failed = (
        str(formal.get("status") or "") == "failed"
        and int(formal.get("production_strategy_version_id") or 0) == int(champion.get("champion_id") or 0)
        and str(formal.get("evaluation_role") or "") == "champion"
    )
    if not failed:
        return {
            "status": "allowed", "mode": "normal",
            "champion_id": int(champion.get("champion_id") or 0),
            "reason": "no_persisted_failed_formal_oos_for_active_champion",
        }
    summary = formal.get("summary") if isinstance(formal.get("summary"), dict) else {}
    return {
        "status": "quarantined", "mode": "research_only_sell_reduce",
        "champion_id": int(champion.get("champion_id") or 0),
        "champion_version": str(champion.get("champion_version") or ""),
        "reason": "active_champion_failed_formal_oos",
        "formal_oos": {
            "outperform_window_count": int(summary.get("outperform_window_count") or 0),
            "valid_window_count": int(summary.get("valid_window_count") or 0),
            "aggregate_excess_return": summary.get("aggregate_excess_return"),
            "blocking_reasons": list(formal.get("blocking_reasons") or []),
        },
        "defensive_continuity": {
            "enabled": True,
            "maximum_total_weight": 0.30,
            "asset_scope": "registered_defensive_etf_only",
            "individual_equity_buys_allowed": False,
        },
        "policy": "block_equity_new_and_add_allow_bounded_defensive_etf_reduce_sell_and_audit",
    }


def record_champion_formal_oos_result(result: dict[str, Any]) -> dict[str, Any]:
    """Persist conclusive formal OOS evidence for the active Champion only."""
    if not isinstance(result, dict) or str(result.get("evaluation_role") or "") != "champion":
        return {"recorded": False, "reason": "not_a_champion_evaluation"}
    if str(result.get("status") or "") not in {"passed", "failed"}:
        return {"recorded": False, "reason": "formal_oos_not_conclusive"}
    champion = resolve_production_strategy_champion()
    champion_id = int(champion.get("champion_id") or 0)
    if champion_id <= 0 or int(result.get("production_strategy_version_id") or 0) != champion_id:
        return {"recorded": False, "reason": "evaluation_not_bound_to_active_champion"}
    current = get_model_version(champion_id) or {}
    metrics = dict(current.get("metrics") if isinstance(current.get("metrics"), dict) else {})
    formal = {
        "schema_version": "alis_champion_formal_oos_v1",
        "status": str(result.get("status") or ""),
        "evaluation_role": "champion",
        "production_strategy_version_id": champion_id,
        "production_strategy_version": str(champion.get("champion_version") or ""),
        "summary": result.get("summary") if isinstance(result.get("summary"), dict) else {},
        "promotion_gates": result.get("promotion_gates") if isinstance(result.get("promotion_gates"), dict) else {},
        "blocking_reasons": list(result.get("blocking_reasons") or []),
        "failure_attribution": result.get("failure_attribution") if isinstance(result.get("failure_attribution"), dict) else {},
        "recorded_at": datetime.now().astimezone().isoformat(),
    }
    metrics["formal_oos"] = formal
    update_model_version_metrics(champion_id, metrics=metrics)
    validation = dict(current.get("validation") if isinstance(current.get("validation"), dict) else {})
    validation.update({
        "formal_oos_status": formal["status"],
        "execution_mode": "research_only_sell_reduce" if formal["status"] == "failed" else "normal",
        "execution_gate_reason": "active_champion_failed_formal_oos" if formal["status"] == "failed" else "formal_oos_passed",
    })
    update_model_version_validation(champion_id, validation=validation)
    return {"recorded": True, "champion_id": champion_id, "status": formal["status"], "execution_gate": champion_execution_gate()}


def champion_strategy_policy_context() -> dict[str, Any]:
    """Return the controller policy pinned by the active production Champion."""
    champion = resolve_production_strategy_champion()
    return {
        "strategy_policy": champion["strategy_policy"],
        "strategy_policy_version": champion["strategy_controller_version"],
        "strategy_policy_version_id": champion["strategy_controller_version_id"],
        "production_strategy_model_key": champion["champion_model_key"],
        "production_strategy_version": champion["champion_version"],
        "production_strategy_version_id": champion["champion_id"],
    }


def promote_strategy_candidate(
    candidate_model_version_id: int,
    *,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Idempotently bind a validated strategy candidate as the sole simulation Champion."""
    candidate = get_model_version(int(candidate_model_version_id))
    if not candidate or str(candidate.get("model_key") or "") != "alis_strategy_challenger":
        raise ValueError("候选策略版本不存在或类型不正确")
    validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
    if str(validation.get("status") or "") != "shadow_passed" or not bool(validation.get("production_eligible")):
        raise ValueError("候选尚未通过正式 OOS 与影子观察硬门")
    active = ensure_production_strategy_champion()
    active_config = active.get("config") if isinstance(active.get("config"), dict) else {}
    active_promotion = active_config.get("promotion") if isinstance(active_config.get("promotion"), dict) else {}
    if int(active_promotion.get("candidate_model_version_id") or 0) == int(candidate_model_version_id):
        return {
            "ok": True,
            "event": "already_promoted",
            "from_champion_version_id": int(active_promotion.get("from_champion_version_id") or 0),
            "to_champion_version_id": int(active["id"]),
            "candidate_model_version_id": int(candidate_model_version_id),
            "version": str(active.get("version") or ""),
        }
    candidate_config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
    strategy_id = int(candidate_config.get("strategy_id") or 0)
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise ValueError("候选绑定的策略不存在")

    from app.services.factor_platform import active_factor_bindings
    from app.services.strategy_config import get_strategy_factor_names

    active_config = deepcopy(active_config)
    active_config.update({
        "schema_version": CHAMPION_SCHEMA_VERSION,
        "name": str(strategy.get("name") or "ALIS Champion 综合生产策略"),
        "role": "champion",
        "strategy_id": strategy_id,
        "selection_strategy": {
            "id": strategy_id,
            "name": strategy.get("name"),
            "type": strategy.get("type"),
            "content": strategy.get("content"),
        },
        "factor_bindings": active_factor_bindings(
            get_strategy_factor_names(strategy.get("content"), strategy.get("type", "formula"))
        ),
        "promotion": {
            "mode": "automatic_deterministic_gates",
            "candidate_model_version_id": int(candidate_model_version_id),
            "candidate_version": str(candidate.get("version") or ""),
            "from_champion_version_id": int(active["id"]),
            "from_champion_version": str(active.get("version") or ""),
            "promoted_at": datetime.now().astimezone().isoformat(),
            "evidence": evidence,
        },
    })
    version = f"alis-champion-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    new_id = create_model_version(
        model_key=CHAMPION_MODEL_KEY,
        version=version,
        status="active",
        artifact_path=f"builtin://alis/production-strategy/candidate/{candidate_model_version_id}",
        config=active_config,
        metrics={"promotion_evidence": evidence},
        validation={
            "status": "passed",
            "production_eligible": True,
            "role": "champion",
            "promotion_mode": "automatic_deterministic_gates",
            "rollback_target_version_id": int(active["id"]),
            "candidate_model_version_id": int(candidate_model_version_id),
            "cooling_status": "observing",
        },
        is_active=True,
    )
    update_model_version_status(int(active["id"]), status="superseded", is_active=False)
    return {
        "ok": True,
        "event": "promoted",
        "from_champion_version_id": int(active["id"]),
        "to_champion_version_id": int(new_id),
        "candidate_model_version_id": int(candidate_model_version_id),
        "version": version,
    }


def rollback_strategy_champion(
    target_champion_version_id: int,
    *,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Return simulation traffic to a previously eligible production bundle."""
    current = ensure_production_strategy_champion()
    target = get_model_version(int(target_champion_version_id))
    target_validation = target.get("validation") if target and isinstance(target.get("validation"), dict) else {}
    if (
        not target
        or str(target.get("model_key") or "") != CHAMPION_MODEL_KEY
        or not bool(target_validation.get("production_eligible"))
    ):
        raise ValueError("回退目标不是可用的历史 Champion")
    set_active_model_version(int(target["id"]))
    update_model_version_status(int(current["id"]), status="rolled_back", is_active=False)
    return {
        "ok": True,
        "event": "rolled_back",
        "from_champion_version_id": int(current["id"]),
        "to_champion_version_id": int(target["id"]),
        "evidence": evidence,
    }
