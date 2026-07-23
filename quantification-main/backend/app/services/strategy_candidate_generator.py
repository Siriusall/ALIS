from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Any

from app.db.store import (
    _conn,
    create_model_version,
    create_strategy,
    get_model_version,
    get_strategy,
    list_model_versions,
    list_strategies,
    update_strategy,
)
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.factor_platform import active_factor_bindings
from app.services.strategy_config import get_strategy_factor_names
from app.services.experiment_memory import find_rejected_hypothesis, upsert_experiment_memory


CHALLENGER_MODEL_KEY = "alis_strategy_challenger"
DYNAMIC_EXPOSURE_NAME = "ALIS Challenger 低风险ETF动态暴露 V2.2"
DUAL_TREND_NAME = "ALIS Challenger 双周期趋势确认 V2.3"
ADAPTIVE_NAME_PREFIX = "ALIS Challenger 自适应攻防"
SELECTION_ALLOCATION_NAME_PREFIX = "ALIS Challenger 高置信 Top8 选择"
SELECTION_ALLOCATION_TOP_N = 8


def _stable_evidence_hash(value: dict[str, Any]) -> str:
    """Fingerprint the persisted OOS evidence used to create a candidate."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_selection_allocation_challenger(
    *,
    failure_attribution: dict[str, Any],
    source_model_version_id: int,
) -> dict[str, Any] | None:
    """Create one bounded Top-N successor for a persisted residual diagnosis.

    This intentionally changes only ``rebalance.top_n`` from the failed
    strategy. It is a falsifiable diversification/concentration hypothesis,
    not an assertion that the residual has already been explained.
    """
    primary_cause = str(failure_attribution.get("primary_cause") or "")
    confidence = str(failure_attribution.get("confidence") or "")
    if primary_cause != "selection_or_allocation_residual" or confidence not in {"medium", "high"}:
        return None

    frozen_attribution = deepcopy(failure_attribution)
    # Routing metadata is not an OOS observation. Do not put it into the
    # fingerprint or present it as part of the attribution result.
    frozen_attribution.pop("_routing_source", None)
    attribution_hash = _stable_evidence_hash(frozen_attribution)
    memory_hypothesis = {
        "template": "selection_allocation_top_n_v1",
        "source_attribution_hash": attribution_hash,
        "changed_fields": [{"path": "rebalance.top_n", "to": SELECTION_ALLOCATION_TOP_N}],
    }
    if find_rejected_hypothesis("strategy", memory_hypothesis):
        # The same source evidence and parameter change already failed formal
        # validation. A new OOS attribution is required before retrying it.
        return None

    source = get_model_version(int(source_model_version_id)) or {}
    source_config = source.get("config") if isinstance(source.get("config"), dict) else {}
    source_strategy_id = int(source_config.get("strategy_id") or 0)
    source_strategy = get_strategy(source_strategy_id) if source_strategy_id else None
    source_content = source_config.get("selection_strategy")
    if not isinstance(source_content, dict):
        source_content = (source_strategy or {}).get("content")
    if not isinstance(source_content, dict):
        return None

    source_content = deepcopy(source_content)
    rebalance = source_content.get("rebalance") if isinstance(source_content.get("rebalance"), dict) else {}
    original_top_n = max(1, int(rebalance.get("top_n") or 20))
    if original_top_n <= SELECTION_ALLOCATION_TOP_N:
        return None

    for version in list_model_versions(CHALLENGER_MODEL_KEY, limit=100):
        config = version.get("config") if isinstance(version.get("config"), dict) else {}
        contract = config.get("evidence_contract") if isinstance(config.get("evidence_contract"), dict) else {}
        if (
            str(config.get("generation") or "") == "automatic_failure_attribution"
            and str(contract.get("source_attribution_hash") or "") == attribution_hash
        ):
            strategy = get_strategy(int(config.get("strategy_id") or 0))
            return {
                "strategy_id": int(config.get("strategy_id") or 0),
                "model_version_id": int(version["id"]),
                "name": str((strategy or {}).get("name") or version.get("version") or SELECTION_ALLOCATION_NAME_PREFIX),
                "version": str(version.get("version") or ""),
                "status": str((version.get("validation") or {}).get("status") or version.get("status") or ""),
                "generated_by": "automatic_failure_attribution",
            }

    champion = resolve_production_strategy_champion()
    content = deepcopy(source_content)
    content["schema_version"] = "alis_challenger_selection_allocation_top_n_v1"
    content["rebalance"] = {**rebalance, "top_n": SELECTION_ALLOCATION_TOP_N}
    evidence_contract = {
        "schema_version": "alis_selection_allocation_candidate_evidence_v1",
        "source_model_version_id": int(source_model_version_id),
        "source_model_version": str(source.get("version") or ""),
        "source_strategy_id": source_strategy_id,
        "source_attribution_hash": attribution_hash,
        "source_failure_attribution": frozen_attribution,
        "hypothesis": "将候选池限制为排名前8名，检验更高置信度选择能否缩小成本和执行项扣除后的残差。",
        "experiment_memory": {
            "target": "strategy",
            "hypothesis": memory_hypothesis,
        },
        "changed_fields": [{
            "path": "rebalance.top_n",
            "from": original_top_n,
            "to": SELECTION_ALLOCATION_TOP_N,
        }],
        "non_changes": [
            "score_factors",
            "market_exposure",
            "execution_contract",
            "transaction_cost_contract",
            "strategy_controller",
        ],
    }
    name = f"{SELECTION_ALLOCATION_NAME_PREFIX} V1-源版本{int(source_model_version_id)}"
    existing = next((item for item in list_strategies() if item.get("name") == name), None)
    if not existing:
        _ensure_strategy_sequence()
    strategy = (
        update_strategy(int(existing["id"]), name, "formula", content)
        if existing else create_strategy(name, "formula", content)
    )
    version_id = create_model_version(
        model_key=CHALLENGER_MODEL_KEY,
        version=f"alis-challenger-selection-allocation-top8-v1-{attribution_hash[:12]}",
        status="candidate",
        artifact_path="builtin://alis/challenger/selection-allocation-top8-v1",
        config={
            "role": "challenger",
            "generation": "automatic_failure_attribution",
            "diagnosis": "selection_or_allocation_residual",
            "strategy_id": int(strategy["id"]),
            "champion_reference": champion["champion_version"],
            "selection_strategy": content,
            "factor_bindings": active_factor_bindings(
                get_strategy_factor_names(content, "formula")
            ),
            "evidence_contract": evidence_contract,
            "experiment_memory": evidence_contract["experiment_memory"],
        },
        metrics={},
        validation={
            "status": "pending_oos",
            "production_eligible": False,
            "next_gate": "quick_diagnostic",
        },
        is_active=False,
    )
    version = next(
        item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=100)
        if int(item["id"]) == int(version_id)
    )
    upsert_experiment_memory(
        target="strategy",
        hypothesis=memory_hypothesis,
        status="observing",
        source_model_version_id=int(version_id),
        outcome_stage="candidate_created",
        evidence={"candidate_version": str(version.get("version") or ""), "source_attribution_hash": attribution_hash},
    )
    return {
        "strategy_id": int(strategy["id"]),
        "model_version_id": int(version["id"]),
        "name": name,
        "version": str(version.get("version") or ""),
        "status": "pending_oos",
        "generated_by": "automatic_failure_attribution",
    }


def _ensure_adaptive_parameter_challenger(
    base_content: dict[str, Any],
    champion: dict[str, Any],
    versions: list[dict[str, Any]],
) -> dict[str, Any]:
    adaptive = [
        item for item in versions
        if str((item.get("config") or {}).get("generation") or "") == "automatic_parameter_search"
    ]
    latest = adaptive[0] if adaptive else None
    latest_status = str((latest.get("validation") or {}).get("status") or "") if latest else ""
    if latest and latest_status not in {
        "quick_diagnostic_failed", "formal_oos_rejected", "formal_oos_failed",
        "shadow_rejected", "rejected", "rolled_back",
    }:
        config = latest.get("config") or {}
        strategy_id = int(config.get("strategy_id") or 0)
        strategy = get_strategy(strategy_id)
        return {
            "strategy_id": strategy_id,
            "model_version_id": int(latest["id"]),
            "name": str((strategy or {}).get("name") or f"{ADAPTIVE_NAME_PREFIX} V2.4"),
            "version": str(latest.get("version") or ""),
            "status": latest_status,
            "generated_by": "automatic_parameter_search",
        }

    generation = len(adaptive) + 1
    templates = (
        {"positive_floor": .85, "neutral_cap": .10, "negative_cap": .02, "s0": .85, "s1": .20, "s2": .02, "s3": .12},
        {"positive_floor": .92, "neutral_cap": .08, "negative_cap": .02, "s0": .92, "s1": .15, "s2": .02, "s3": .08},
        {"positive_floor": .78, "neutral_cap": .15, "negative_cap": .05, "s0": .78, "s1": .25, "s2": .05, "s3": .18},
        {"positive_floor": .88, "neutral_cap": .05, "negative_cap": .01, "s0": .88, "s1": .12, "s2": .01, "s3": .05},
    )
    params = templates[(generation - 1) % len(templates)]
    content = deepcopy(base_content)
    content["schema_version"] = f"alis_challenger_adaptive_defense_v2_4_g{generation}"
    content.setdefault("rebalance", {})["freq"] = "weekly"
    controls = content.setdefault("challenger_controls", {})
    controls.update({
        "max_single_weight": .08,
        "max_rebalance_turnover": .35,
        "min_weight_change": .02,
        "max_entry_1d_return": .07,
        "max_entry_5d_return": .20,
        "initial_deployment_turnover_exempt": True,
    })
    content["dynamic_market_exposure"] = {
        "schema_version": f"adaptive_dual_trend_parameter_search_g{generation}",
        "state_equity_weights": {
            "S0": params["s0"], "S1": params["s1"],
            "S2": params["s2"], "S3": params["s3"],
        },
        "default_equity_weight": params["neutral_cap"],
        "etf_fallback": True,
        "etf_universe": ["511880.SH", "511010.SH", "518880.SH", "512890.SH", "510880.SH"],
        "risk_off_etf_classes": ["money", "bond", "gold"],
        "benchmark_lag_guard": "T_close_only",
        "benchmark_trend_overlay": {
            "lookback_sessions": 20,
            "confirmation_lookback_sessions": 60,
            "positive_threshold": .02,
            "confirmation_positive_threshold": .03,
            "negative_threshold": -.02,
            "confirmation_negative_threshold": -.03,
            "positive_equity_floor": params["positive_floor"],
            "neutral_equity_cap": params["neutral_cap"],
            "negative_equity_cap": params["negative_cap"],
        },
    }
    name = f"{ADAPTIVE_NAME_PREFIX} V2.4-G{generation}"
    existing = next((item for item in list_strategies() if item.get("name") == name), None)
    if not existing:
        _ensure_strategy_sequence()
    strategy = (
        update_strategy(int(existing["id"]), name, "formula", content)
        if existing else create_strategy(name, "formula", content)
    )
    version_id = create_model_version(
        model_key=CHALLENGER_MODEL_KEY,
        version=f"alis-challenger-adaptive-v2-4-g{generation}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        status="candidate",
        artifact_path=f"builtin://alis/challenger/adaptive-defense-v2-4/g{generation}",
        config={
            "role": "challenger",
            "generation": "automatic_parameter_search",
            "generation_index": generation,
            "diagnosis": "dual_trend_candidate_failed_quick_gate",
            "strategy_id": int(strategy["id"]),
            "champion_reference": champion["champion_version"],
            "selection_strategy": content,
            "factor_bindings": active_factor_bindings(
                get_strategy_factor_names(content, "formula")
            ),
            "parameter_search": params,
        },
        metrics={},
        validation={
            "status": "pending_oos",
            "production_eligible": False,
            "next_gate": "quick_diagnostic",
        },
        is_active=False,
    )
    version = next(
        item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=100)
        if int(item["id"]) == int(version_id)
    )
    return {
        "strategy_id": int(strategy["id"]),
        "model_version_id": int(version["id"]),
        "name": name,
        "version": str(version.get("version") or ""),
        "status": "pending_oos",
        "generated_by": "automatic_parameter_search",
    }


def _ensure_strategy_sequence() -> None:
    with _conn() as conn:
        if conn._is_postgres:
            conn.execute(
                """SELECT setval(pg_get_serial_sequence('strategies','id'),
                                 GREATEST(COALESCE((SELECT MAX(id) FROM strategies),1),1),true)"""
            )


def _ensure_dual_trend_challenger(
    base_content: dict[str, Any],
    champion: dict[str, Any],
    versions: list[dict[str, Any]],
) -> dict[str, Any]:
    content = deepcopy(base_content)
    content["schema_version"] = "alis_challenger_dual_trend_confirmation_v2_3"
    content["dynamic_market_exposure"] = {
        "schema_version": "dual_trend_market_exposure_with_safe_etf_v1",
        "state_equity_weights": {"S0": 0.75, "S1": 0.35, "S2": 0.10, "S3": 0.30},
        "default_equity_weight": 0.25,
        "etf_fallback": True,
        "etf_universe": ["511880.SH", "511010.SH", "518880.SH", "512890.SH", "510880.SH"],
        "risk_off_etf_classes": ["money", "bond", "gold"],
        "benchmark_lag_guard": "T_close_only",
        "benchmark_trend_overlay": {
            "lookback_sessions": 20,
            "confirmation_lookback_sessions": 60,
            "positive_threshold": 0.02,
            "confirmation_positive_threshold": 0.03,
            "negative_threshold": -0.02,
            "confirmation_negative_threshold": -0.03,
            "positive_equity_floor": 0.65,
            "neutral_equity_cap": 0.20,
            "negative_equity_cap": 0.05,
        },
    }
    existing = next((item for item in list_strategies() if item.get("name") == DUAL_TREND_NAME), None)
    if not existing:
        _ensure_strategy_sequence()
    strategy = (
        update_strategy(int(existing["id"]), DUAL_TREND_NAME, "formula", content)
        if existing else create_strategy(DUAL_TREND_NAME, "formula", content)
    )
    version = next(
        (item for item in versions if int((item.get("config") or {}).get("strategy_id") or 0) == int(strategy["id"])),
        None,
    )
    if not version:
        version_id = create_model_version(
            model_key=CHALLENGER_MODEL_KEY,
            version=f"alis-challenger-dual-trend-v2-3-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            status="candidate",
            artifact_path="builtin://alis/challenger/dual-trend-confirmation-v2-3",
            config={
                "role": "challenger",
                "generation": "automatic_diagnosis",
                "diagnosis": "short_trend_false_positive",
                "strategy_id": int(strategy["id"]),
                "champion_reference": champion["champion_version"],
                "selection_strategy": content,
                "factor_bindings": active_factor_bindings(
                    get_strategy_factor_names(content, "formula")
                ),
            },
            metrics={},
            validation={"status": "pending_oos", "production_eligible": False, "next_gate": "quick_diagnostic"},
            is_active=False,
        )
        version = next(item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=100) if int(item["id"]) == version_id)
    if str((version.get("validation") or {}).get("status") or "") in {
        "quick_diagnostic_failed", "formal_oos_rejected", "formal_oos_failed",
        "shadow_rejected", "rejected", "rolled_back",
    }:
        return _ensure_adaptive_parameter_challenger(
            content, champion, list_model_versions(CHALLENGER_MODEL_KEY, limit=100)
        )
    return {
        "strategy_id": int(strategy["id"]),
        "model_version_id": int(version["id"]),
        "name": DUAL_TREND_NAME,
        "version": str(version.get("version") or ""),
        "status": str((version.get("validation") or {}).get("status") or version.get("status") or ""),
        "generated_by": "automatic_diagnosis",
    }


def ensure_dynamic_exposure_challenger() -> dict[str, Any]:
    """Generate the deterministic V2 candidate selected by the V1 diagnosis."""
    champion = resolve_production_strategy_champion()
    versions = list_model_versions(CHALLENGER_MODEL_KEY, limit=100)
    v1 = next(
        (
            item for item in versions
            if str((item.get("config") or {}).get("role") or "") == "challenger"
            and not (item.get("config") or {}).get("generation")
        ),
        None,
    )
    base_strategy_id = int((v1.get("config") or {}).get("strategy_id") or 0) if v1 else 0
    base_strategy = get_strategy(base_strategy_id) if base_strategy_id else None
    base_content = (
        deepcopy(base_strategy.get("content"))
        if base_strategy and isinstance(base_strategy.get("content"), dict)
        else deepcopy((champion.get("strategy") or {}).get("content") or {})
    )
    base_content["schema_version"] = "alis_challenger_low_risk_etf_dynamic_exposure_v2_2"
    base_content.setdefault("rebalance", {})["freq"] = "weekly"
    base_content["challenger_controls"] = {
        "max_single_weight": 0.08,
        "max_rebalance_turnover": 0.50,
        "min_weight_change": 0.02,
        "max_entry_1d_return": 0.07,
        "max_entry_5d_return": 0.20,
        "initial_deployment_turnover_exempt": True,
        "rationale": [
            "保留 V1 单票、周频与追高控制",
            "首次建仓不受20%换手限制，避免每个OOS窗口长期低仓位",
            "按市场状态动态控制股票仓位，剩余资金配置可交易ETF",
        ],
    }
    base_content["dynamic_market_exposure"] = {
        "schema_version": "dynamic_market_exposure_with_etf_fallback_v1",
        "state_equity_weights": {"S0": 0.90, "S1": 0.60, "S2": 0.20, "S3": 0.50},
        "default_equity_weight": 0.50,
        "etf_fallback": True,
        "etf_universe": ["511880.SH", "511010.SH", "518880.SH", "512890.SH", "510880.SH"],
        "benchmark_lag_guard": "T_close_only",
        "risk_off_etf_classes": ["money", "bond", "gold"],
        "benchmark_trend_overlay": {
            "lookback_sessions": 20,
            "positive_threshold": 0.02,
            "negative_threshold": -0.02,
            "positive_equity_floor": 0.80,
            "neutral_equity_cap": 0.35,
            "negative_equity_cap": 0.15,
        },
    }
    existing = next((item for item in list_strategies() if item.get("name") == DYNAMIC_EXPOSURE_NAME), None)
    if not existing:
        _ensure_strategy_sequence()
    strategy = (
        update_strategy(int(existing["id"]), DYNAMIC_EXPOSURE_NAME, "formula", base_content)
        if existing else create_strategy(DYNAMIC_EXPOSURE_NAME, "formula", base_content)
    )
    version = next(
        (item for item in versions if int((item.get("config") or {}).get("strategy_id") or 0) == int(strategy["id"])),
        None,
    )
    if not version:
        version_id = create_model_version(
            model_key=CHALLENGER_MODEL_KEY,
            version=f"alis-challenger-low-risk-etf-exposure-v2-2-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            status="candidate",
            artifact_path="builtin://alis/challenger/low-risk-etf-dynamic-exposure-v2-2",
            config={
                "role": "challenger",
                "generation": "automatic_diagnosis",
                "diagnosis": "market_exposure_with_equity_like_etf_overexposure",
                "strategy_id": int(strategy["id"]),
                "champion_reference": champion["champion_version"],
                "selection_strategy": base_content,
                "factor_bindings": active_factor_bindings(
                    get_strategy_factor_names(base_content, "formula")
                ),
            },
            metrics={},
            validation={"status": "pending_oos", "production_eligible": False, "next_gate": "formal_oos"},
            is_active=False,
        )
        version = next(item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=100) if int(item["id"]) == version_id)
    if str((version.get("validation") or {}).get("status") or "") in {"quick_diagnostic_failed", "rejected"}:
        return _ensure_dual_trend_challenger(base_content, champion, list_model_versions(CHALLENGER_MODEL_KEY, limit=100))
    return {
        "strategy_id": int(strategy["id"]),
        "model_version_id": int(version["id"]),
        "name": DYNAMIC_EXPOSURE_NAME,
        "version": str(version.get("version") or ""),
        "status": str((version.get("validation") or {}).get("status") or version.get("status") or ""),
        "generated_by": "automatic_diagnosis",
    }
