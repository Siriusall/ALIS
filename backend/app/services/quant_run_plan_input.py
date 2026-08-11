"""Validation and normalization for claimed daily rebalance plan inputs."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from app.services.quant_run_contracts import ReplayRiskGateError
from app.services.risk_limit_contract import RISK_LIMIT_CONTRACT_SCHEMA_VERSION

UNIFIED_DAILY_DECISION_SCHEMA_VERSION = "alis_unified_daily_decision_v2"
LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION = "alis_unified_daily_decision_v1"
UNIFIED_SELECTION_SCHEMA_VERSION = "alis_unified_stock_selection_v1"
UNIFIED_EXECUTION_SAFETY_SCHEMA_VERSION = "alis_t1_execution_safety_v2"
LEGACY_UNIFIED_EXECUTION_SAFETY_SCHEMA_VERSION = "alis_t1_execution_safety_v1"
PRETRADE_CONTROLLER_SCHEMA_VERSION = "alis_strategy_controller_pretrade_v2"
LEGACY_PRETRADE_CONTROLLER_SCHEMA_VERSION = "alis_strategy_controller_pretrade_v1"
INDUSTRY_ALLOCATION_SCHEMA_VERSION = "alis_industry_allocation_v1"
PORTFOLIO_COMPETITION_SCHEMA_VERSION = "portfolio_method_competition_v3"
LEGACY_PORTFOLIO_COMPETITION_SCHEMA_VERSION = "portfolio_method_competition_v2"
EXPECTED_RETURN_SCHEMA_VERSION = "alis_a_share_expected_return_20d_v1"
DECISION_MANIFEST_SCHEMA_VERSION = "alis_decision_version_manifest_v2"
UNIFIED_T1_ALLOWED_VETOES = {
    "missing_open_price",
    "suspension",
    "st_status",
    "limit_up_down",
    "lot_size",
    "liquidity_capacity",
    "cash_no_overdraft",
    "no_oversell",
    "t1_sell_restriction",
    "ledger_consistency",
    "overnight_champion_restriction_only",
}


def _canonical_hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finite_weight(value: Any, *, field: str) -> float:
    parsed = _finite_number(value, field=field)
    if parsed < 0:
        raise ReplayRiskGateError(f"正式调仓计划权重非法：{field}", code="invalid_authoritative_plan")
    return parsed


def _finite_number(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ReplayRiskGateError(f"正式调仓计划数值无效：{field}", code="invalid_authoritative_plan") from None
    if not math.isfinite(parsed):
        raise ReplayRiskGateError(f"正式调仓计划数值非法：{field}", code="invalid_authoritative_plan")
    return parsed


def _unique_symbols(values: Any, *, field: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(values, list):
        raise ReplayRiskGateError(f"统一计划字段格式无效：{field}", code="invalid_authoritative_plan")
    symbols = [str(value or "").strip() for value in values]
    if any(not value for value in symbols) or len(symbols) != len(set(symbols)):
        raise ReplayRiskGateError(f"统一计划标的为空或重复：{field}", code="invalid_authoritative_plan")
    if not allow_empty and not symbols:
        raise ReplayRiskGateError(f"统一计划标的缺失：{field}", code="invalid_authoritative_plan")
    return symbols


def _weight_map(value: Any, *, field: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ReplayRiskGateError(f"统一计划权重格式无效：{field}", code="invalid_authoritative_plan")
    result: dict[str, float] = {}
    for symbol, weight in value.items():
        key = str(symbol or "").strip()
        if not key or key in result:
            raise ReplayRiskGateError(f"统一计划权重标的无效：{field}", code="invalid_authoritative_plan")
        result[key] = _finite_weight(weight, field=f"{field}.{key}")
    return result


def _same_weights(left: Any, right: Any, *, left_field: str, right_field: str) -> bool:
    left_weights = _weight_map(left, field=left_field)
    right_weights = _weight_map(right, field=right_field)
    return set(left_weights) == set(right_weights) and all(
        abs(left_weights[symbol] - right_weights[symbol]) <= 1e-8
        for symbol in left_weights
    )


def _validate_selection_snapshot(snapshot: dict[str, Any]) -> None:
    if str(snapshot.get("schema_version") or "") != UNIFIED_SELECTION_SCHEMA_VERSION:
        raise ReplayRiskGateError("统一计划缺少选股合同版本", code="invalid_authoritative_plan")
    expected_hash = str(snapshot.get("selection_hash") or "")
    payload = {key: value for key, value in snapshot.items() if key != "selection_hash"}
    if not expected_hash or _canonical_hash(payload) != expected_hash:
        raise ReplayRiskGateError("统一计划选股快照哈希校验失败", code="invalid_authoritative_plan_hash")
    if (
        int(snapshot.get("champion_id") or 0) <= 0
        or int(snapshot.get("strategy_id") or 0) <= 0
        or not str(snapshot.get("champion_version") or "")
    ):
        raise ReplayRiskGateError("统一计划选股快照缺少 Champion 绑定", code="invalid_authoritative_plan")
    ranked = _unique_symbols(snapshot.get("ranked_symbols"), field="selection.ranked_symbols", allow_empty=False)
    if len(ranked) > 20:
        raise ReplayRiskGateError("统一计划 Top K 超过冻结上限20", code="invalid_authoritative_plan")
    research_admitted = _unique_symbols(
        snapshot.get("research_admitted_symbols"),
        field="selection.research_admitted_symbols",
    )
    admitted = _unique_symbols(snapshot.get("admitted_symbols"), field="selection.admitted_symbols")
    holdings = _unique_symbols(snapshot.get("holding_symbols"), field="selection.holding_symbols")
    holding_admitted = _unique_symbols(
        snapshot.get("holding_admitted_symbols"),
        field="selection.holding_admitted_symbols",
    )
    if not set(admitted) <= set(ranked) & set(research_admitted):
        raise ReplayRiskGateError("统一计划正式入选标的不属于 Top K 研究准入交集", code="invalid_authoritative_plan")
    if not set(holding_admitted) <= set(holdings) & set(research_admitted):
        raise ReplayRiskGateError("统一计划持仓复核结果与研究准入不一致", code="invalid_authoritative_plan")
    authority = snapshot.get("authority") if isinstance(snapshot.get("authority"), dict) else {}
    if (
        str(authority.get("formal_candidate_authority") or "") != "unified_selection_contract"
        or authority.get("legacy_pool_membership_required") is not False
        or authority.get("legacy_pool_mutation_may_change_contract") is not False
    ):
        raise ReplayRiskGateError("统一计划选股权限边界不完整", code="invalid_authoritative_plan")
    environment = snapshot.get("environment_snapshot") if isinstance(snapshot.get("environment_snapshot"), dict) else {}
    if str(environment.get("schema_version") or "") != "alis_selection_environment_snapshot_v1":
        raise ReplayRiskGateError("统一计划缺少冻结环境切片", code="invalid_authoritative_plan")
    factor_snapshot = snapshot.get("factor_expert_snapshot") if isinstance(snapshot.get("factor_expert_snapshot"), dict) else {}
    if str(factor_snapshot.get("schema_version") or "") != "alis_factor_expert_selection_v1":
        raise ReplayRiskGateError("统一计划缺少因子专家路由快照", code="invalid_authoritative_plan")
    selected_factors = _unique_symbols(
        factor_snapshot.get("selected_factors"),
        field="selection.factor_expert_snapshot.selected_factors",
        allow_empty=False,
    )
    factor_bindings = (
        factor_snapshot.get("factor_bindings")
        if isinstance(factor_snapshot.get("factor_bindings"), dict) else {}
    )
    if not factor_bindings or not set(selected_factors) <= set(factor_bindings):
        raise ReplayRiskGateError("统一计划因子公式未绑定到冻结版本", code="invalid_authoritative_plan")
    factor_weights = (
        factor_snapshot.get("factor_weights")
        if isinstance(factor_snapshot.get("factor_weights"), dict) else {}
    )
    if set(factor_weights) != set(selected_factors):
        raise ReplayRiskGateError("统一计划因子公式权重与专家选择不一致", code="invalid_authoritative_plan")
    frozen_factor_weights = {
        str(key): _finite_number(
            value,
            field=f"selection.factor_expert_snapshot.factor_weights.{key}",
        )
        for key, value in factor_weights.items()
    }
    model_snapshot = snapshot.get("model_signal_snapshot") if isinstance(snapshot.get("model_signal_snapshot"), dict) else {}
    if str(model_snapshot.get("schema_version") or "") != "alis_model_signal_snapshot_v1":
        raise ReplayRiskGateError("统一计划缺少逐标的模型输出快照", code="invalid_authoritative_plan")
    fusion = model_snapshot.get("fusion") if isinstance(model_snapshot.get("fusion"), dict) else {}
    if str(fusion.get("schema_version") or "") != "model_signal_fusion_v2":
        raise ReplayRiskGateError("统一计划模型融合合同版本无效", code="invalid_authoritative_plan")
    production_models = [
        item for item in (fusion.get("production_models") or [])
        if isinstance(item, dict)
    ]
    model_keys = [str(item.get("model_key") or "") for item in production_models]
    if (
        not model_keys
        or any(not key for key in model_keys)
        or len(model_keys) != len(set(model_keys))
        or any(
            not isinstance(item.get("consumed"), bool)
            or not isinstance(item.get("point_in_time_available"), bool)
            or item.get("consumed") != item.get("point_in_time_available")
            for item in production_models
        )
    ):
        raise ReplayRiskGateError("统一计划生产模型绑定缺失或重复", code="invalid_authoritative_plan")
    model_by_key = {
        str(item.get("model_key") or ""): item
        for item in production_models
    }
    consumed_model_keys = {
        key for key, item in model_by_key.items()
        if item.get("consumed") is True
    }
    effective_weights = (
        (fusion.get("policy") or {}).get("effective_model_weights")
        if isinstance(fusion.get("policy"), dict)
        and isinstance((fusion.get("policy") or {}).get("effective_model_weights"), dict)
        else {}
    )
    if set(effective_weights) != set(model_keys):
        raise ReplayRiskGateError("统一计划模型融合权重没有覆盖全部生产模型", code="invalid_authoritative_plan")
    for key, value in effective_weights.items():
        weight = _finite_number(value, field=f"selection.model_fusion.{key}.effective_weight")
        if weight < 0 or (key not in consumed_model_keys and abs(weight) > 1e-12):
            raise ReplayRiskGateError("统一计划模型融合权重与消费状态不一致", code="invalid_authoritative_plan")
    signal_symbols = model_snapshot.get("symbols") if isinstance(model_snapshot.get("symbols"), dict) else {}
    if not (set(ranked) | set(holdings)) <= set(signal_symbols):
        raise ReplayRiskGateError("统一计划缺少 Top K 或持仓的模型/因子解释", code="invalid_authoritative_plan")
    for symbol in ranked:
        signal = signal_symbols.get(symbol) if isinstance(signal_symbols.get(symbol), dict) else {}
        selection = signal.get("selection") if isinstance(signal.get("selection"), dict) else {}
        contributions = [
            item for item in (selection.get("factor_contributions") or [])
            if isinstance(item, dict)
        ]
        contribution_factors = [str(item.get("factor") or "") for item in contributions]
        if (
            str(signal.get("status") or "") != "scored"
            or str(selection.get("symbol") or "") != symbol
            or not contributions
            or any(not factor for factor in contribution_factors)
            or len(contribution_factors) != len(set(contribution_factors))
            or set(contribution_factors) != set(selected_factors)
        ):
            raise ReplayRiskGateError(f"统一计划缺少已实现因子贡献：{symbol}", code="invalid_authoritative_plan")
        for index, contribution in enumerate(contributions):
            factor = str(contribution.get("factor") or "")
            weight = _finite_number(
                contribution.get("weight"),
                field=f"{symbol}.factor_contributions.{index}.weight",
            )
            raw_value = _finite_number(
                contribution.get("raw_value"),
                field=f"{symbol}.factor_contributions.{index}.raw_value",
            )
            contribution_value = _finite_number(
                contribution.get("contribution"),
                field=f"{symbol}.factor_contributions.{index}.contribution",
            )
            if (
                abs(weight - frozen_factor_weights[factor]) > 1e-8
                or abs(contribution_value - round(weight * raw_value, 8)) > 1e-10
            ):
                raise ReplayRiskGateError(
                    f"统一计划逐股因子公式或贡献计算不一致：{symbol}/{factor}",
                    code="invalid_authoritative_plan",
                )
        model_signals = (
            selection.get("model_signals")
            if isinstance(selection.get("model_signals"), dict) else {}
        )
        per_model = (
            model_signals.get("models")
            if isinstance(model_signals.get("models"), dict) else {}
        )
        trigger_models = [
            str(key or "") for key in (model_signals.get("trigger_models") or [])
        ]
        if (
            str(model_signals.get("schema_version") or "") != "model_signal_fusion_v2"
            or set(per_model) != set(model_keys)
            or any(not key for key in trigger_models)
            or len(trigger_models) != len(set(trigger_models))
            or set(trigger_models) != consumed_model_keys
        ):
            raise ReplayRiskGateError(f"统一计划逐股模型消费快照不完整：{symbol}", code="invalid_authoritative_plan")
        for key, model_binding in model_by_key.items():
            model_value = per_model.get(key) if isinstance(per_model.get(key), dict) else {}
            expected_version_id = model_binding.get("version_id")
            actual_version_id = model_value.get("version_id")
            if (
                actual_version_id != expected_version_id
                or str(model_value.get("version") or "") != str(model_binding.get("version") or "")
                or model_value.get("point_in_time_available") is not model_binding.get("consumed")
            ):
                raise ReplayRiskGateError(
                    f"统一计划逐股模型版本或消费状态不一致：{symbol}/{key}",
                    code="invalid_authoritative_plan",
                )
            if model_binding.get("consumed") is True:
                if expected_version_id is None:
                    raise ReplayRiskGateError(
                        f"统一计划已消费模型缺少冻结版本：{symbol}/{key}",
                        code="invalid_authoritative_plan",
                    )
                # V2 snapshots distinguish a model being online for the
                # cross-section from a sparse model producing a score for this
                # particular symbol. Event models must not invent a zero score
                # when no eligible event exists, while dense consumed models
                # remain fail-closed if their per-symbol score is absent.
                score_available = model_value.get("score_available")
                if score_available is None:
                    # Old frozen contracts did not carry this explicit field;
                    # preserve their original strict validation semantics.
                    score_available = model_value.get("raw_score") is not None
                if score_available is not True:
                    if key != "event_impact_assessor_v2" or model_value.get("raw_score") is not None:
                        raise ReplayRiskGateError(
                            f"统一计划已消费模型缺少逐标的评分：{symbol}/{key}",
                            code="invalid_authoritative_plan",
                        )
                    continue
                _finite_number(model_value.get("raw_score"), field=f"{symbol}.models.{key}.raw_score")
                percentile = _finite_number(
                    model_value.get("percentile"),
                    field=f"{symbol}.models.{key}.percentile",
                )
                if percentile < 0 or percentile > 1:
                    raise ReplayRiskGateError(
                        f"统一计划模型截面分位无效：{symbol}/{key}",
                        code="invalid_authoritative_plan",
                    )
    research_evidence = snapshot.get("research_evidence") if isinstance(snapshot.get("research_evidence"), dict) else {}
    for symbol in set(admitted) | set(holding_admitted):
        evidence = research_evidence.get(symbol) if isinstance(research_evidence.get(symbol), dict) else {}
        thesis = evidence.get("thesis_snapshot") if isinstance(evidence.get("thesis_snapshot"), dict) else {}
        hypotheses = [
            item for item in (thesis.get("hypotheses") or [])
            if isinstance(item, dict)
        ]
        gates = [
            item for item in (evidence.get("gates") or [])
            if isinstance(item, dict)
        ]
        gate_keys = {str(item.get("key") or "") for item in gates}
        if (
            not evidence.get("eligible_for_core")
            or int(thesis.get("id") or 0) <= 0
            or not str(thesis.get("core_thesis") or "")
            or not hypotheses
            or any(
                not str(item.get("statement") or "")
                or not str(item.get("metric") or "")
                or not isinstance(item.get("expected"), dict)
                or not item.get("expected")
                for item in hypotheses
            )
            or not {"stock_master", "risk", "financial_fields", "research", "valuation", "thesis"} <= gate_keys
            or any(item.get("passed") is not True for item in gates)
        ):
            raise ReplayRiskGateError(f"统一计划缺少研究硬门或 Thesis 快照：{symbol}", code="invalid_authoritative_plan")
    dynamic_gate = snapshot.get("dynamic_gate") if isinstance(snapshot.get("dynamic_gate"), dict) else {}
    dynamic_items = [
        item for item in (dynamic_gate.get("items") or [])
        if isinstance(item, dict)
    ]
    dynamic_symbols = [str(item.get("symbol") or "") for item in dynamic_items]
    dynamic_admitted = {
        str(item.get("symbol") or "")
        for item in dynamic_items
        if isinstance(item.get("gate"), dict) and item["gate"].get("admitted") is True
    }
    if (
        str(dynamic_gate.get("as_of_time") or "") != str(snapshot.get("data_as_of_time") or "")
        or set(dynamic_symbols) != set(research_admitted)
        or len(dynamic_symbols) != len(set(dynamic_symbols))
        or set(admitted) != set(ranked) & dynamic_admitted
        or set(holding_admitted) != set(holdings) & dynamic_admitted
    ):
        raise ReplayRiskGateError("统一计划动态准入门快照与入选结果不一致", code="invalid_authoritative_plan")
    coverage = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}
    coverage_gates = coverage.get("gates") if isinstance(coverage.get("gates"), dict) else {}
    research_errors = [
        item for item in (snapshot.get("research_errors") or [])
        if isinstance(item, dict)
    ]
    research_targets = list(dict.fromkeys(ranked + holdings))
    expected_error_ratio = len(research_errors) / max(1, len(research_targets))
    expected_coverage_gates = {
        "full_market_snapshot": int(coverage.get("universe_count") or 0) >= 500,
        "research_recall_nonempty": bool(ranked),
        "research_evaluation_coverage": bool(ranked) and expected_error_ratio <= 0.20,
        "at_least_one_research_evaluation": len(research_targets) - len(research_errors) > 0,
    }
    if (
        coverage_gates != expected_coverage_gates
        or int(coverage.get("ranked_count") or 0) != len(ranked)
        or int(coverage.get("holding_review_count") or 0) != len(holdings)
        or int(coverage.get("research_error_count") or 0) != len(research_errors)
        or int(coverage.get("evaluated_count") or 0) != len(research_targets) - len(research_errors)
        or abs(_finite_number(coverage.get("research_error_ratio"), field="selection.coverage.research_error_ratio") - expected_error_ratio) > 1e-8
        or not all(value is True for value in coverage_gates.values())
    ):
        raise ReplayRiskGateError("统一计划全市场或研究截面覆盖门未通过", code="invalid_authoritative_plan")


def validate_unified_decision_contract(contract: dict[str, Any]) -> None:
    """Validate the only contract allowed to create a new formal plan."""
    schema_version = str(contract.get("schema_version") or "") if isinstance(contract, dict) else ""
    if schema_version not in {
        UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
    }:
        raise ReplayRiskGateError("新正式计划必须使用统一日决策合同", code="invalid_authoritative_plan")
    legacy = schema_version == LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION
    expected_safety_schema = (
        LEGACY_UNIFIED_EXECUTION_SAFETY_SCHEMA_VERSION if legacy
        else UNIFIED_EXECUTION_SAFETY_SCHEMA_VERSION
    )
    expected_controller_schema = (
        LEGACY_PRETRADE_CONTROLLER_SCHEMA_VERSION if legacy
        else PRETRADE_CONTROLLER_SCHEMA_VERSION
    )
    expected_competition_schema = (
        LEGACY_PORTFOLIO_COMPETITION_SCHEMA_VERSION if legacy
        else PORTFOLIO_COMPETITION_SCHEMA_VERSION
    )
    required_dicts = (
        "selection_snapshot",
        "pretrade_controller",
        "industry_allocation",
        "expected_return_contract",
        "portfolio_method_competition",
        "execution_safety_contract",
        "decision_version_manifest",
    ) + (() if legacy else ("risk_limit_contract",))
    missing = [key for key in required_dicts if not isinstance(contract.get(key), dict) or not contract.get(key)]
    if missing:
        raise ReplayRiskGateError(
            "统一计划缺少冻结合同：" + ",".join(missing),
            code="invalid_authoritative_plan",
        )
    risk_limits = contract.get("risk_limit_contract") if isinstance(contract.get("risk_limit_contract"), dict) else {}
    if not legacy:
        if (
            str(risk_limits.get("schema_version") or "") != RISK_LIMIT_CONTRACT_SCHEMA_VERSION
            or not risk_limits.get("contract_hash")
            or not risk_limits.get("tightening_only")
            or abs(_finite_weight(risk_limits.get("global_single_asset_limit"), field="risk.global") - 0.15) > 1e-8
        ):
            raise ReplayRiskGateError("统一计划风险上限合同无效", code="invalid_authoritative_plan")
        expected_risk_hash = _canonical_hash({key: value for key, value in risk_limits.items() if key != "contract_hash"})
        if expected_risk_hash != str(risk_limits.get("contract_hash") or ""):
            raise ReplayRiskGateError("统一计划风险上限合同哈希不一致", code="invalid_authoritative_plan")
    _validate_selection_snapshot(contract["selection_snapshot"])
    selection = contract["selection_snapshot"]
    signal_date = str(contract.get("signal_date") or "").replace("-", "")[:8]
    trade_date = str(contract.get("trade_date") or "").replace("-", "")[:8]
    if (
        len(signal_date) != 8
        or signal_date != trade_date
        or signal_date != str(selection.get("signal_date") or "").replace("-", "")[:8]
    ):
        raise ReplayRiskGateError("统一计划信号日、交易日与选股快照不一致", code="invalid_authoritative_plan")
    planned_execute_date = str(contract.get("planned_execute_date") or "").replace("-", "")[:8]
    if len(planned_execute_date) != 8 or planned_execute_date <= signal_date:
        raise ReplayRiskGateError("统一计划执行日无效", code="invalid_authoritative_plan")
    if str(contract.get("data_as_of_time") or "") != str(selection.get("data_as_of_time") or ""):
        raise ReplayRiskGateError("统一计划数据截止时间与选股快照不一致", code="invalid_authoritative_plan")
    safety = contract["execution_safety_contract"]
    if (
        str(safety.get("schema_version") or "") != expected_safety_schema
        or not safety.get("investment_opinion_frozen_on_signal_date")
        or not safety.get("t1_rerank_forbidden")
        or not safety.get("t1_research_refresh_forbidden")
        or not safety.get("t1_industry_reallocation_forbidden")
        or not safety.get("t1_controller_regeneration_forbidden")
        or not safety.get("t1_portfolio_reoptimization_forbidden")
        or (not legacy and not safety.get("t1_quantity_recalculation_forbidden"))
        or (not legacy and not safety.get("frozen_quantity_must_execute_exactly_or_be_vetoed"))
        or set(str(value or "") for value in (safety.get("allowed_vetoes") or []))
        != UNIFIED_T1_ALLOWED_VETOES
    ):
        raise ReplayRiskGateError("统一计划 T+1 安全合同不完整", code="invalid_authoritative_plan")
    expected_return = contract["expected_return_contract"]
    if (
        str(expected_return.get("schema_version") or "") != EXPECTED_RETURN_SCHEMA_VERSION
        or int(expected_return.get("horizon_sessions") or 0) != 20
        or not isinstance(expected_return.get("optimizer_expected_returns"), dict)
        or not isinstance(expected_return.get("items"), list)
    ):
        raise ReplayRiskGateError("统一计划预期收益合同版本无效", code="invalid_authoritative_plan")
    controller = contract["pretrade_controller"]
    if (
        str(controller.get("schema_version") or "") != expected_controller_schema
        or str(controller.get("generated_at") or "") != str(selection.get("data_as_of_time") or "")
        or int(controller.get("strategy_policy_version_id") or 0) <= 0
        or not str(controller.get("strategy_policy_version") or "")
    ):
        raise ReplayRiskGateError("统一计划 StrategyController 合同版本无效", code="invalid_authoritative_plan")
    if not legacy and controller.get("risk_limit_contract") != risk_limits:
        raise ReplayRiskGateError("Controller 未使用冻结风险上限合同", code="invalid_authoritative_plan")
    actions = [item for item in controller.get("actions") or [] if isinstance(item, dict)]
    action_symbols = [str(item.get("symbol") or "") for item in actions]
    if not action_symbols or len(action_symbols) != len(set(action_symbols)):
        raise ReplayRiskGateError("统一计划 Controller 动作缺失或重复", code="invalid_authoritative_plan")
    required_controller_symbols = set(selection.get("admitted_symbols") or []) | set(
        selection.get("holding_symbols") or []
    )
    if (
        not required_controller_symbols <= set(action_symbols)
        or any(
            str(item.get("symbol") or "") not in set(selection.get("ranked_symbols") or [])
            | set(selection.get("holding_symbols") or [])
            and item.get("is_defensive_asset") is not True
            for item in actions
        )
    ):
        raise ReplayRiskGateError("统一计划 Controller 未覆盖全部候选、持仓或含未知标的", code="invalid_authoritative_plan")
    bounds = controller.get("weight_bounds") if isinstance(controller.get("weight_bounds"), dict) else {}
    if set(bounds) != set(action_symbols):
        raise ReplayRiskGateError("统一计划 Controller 动作与权重边界标的不一致", code="invalid_authoritative_plan")
    allowed_actions = {
        "buy_candidate", "add_position", "reduce_position",
        "sell_exit", "hold", "defensive_switch",
    }
    for item in actions:
        symbol = str(item.get("symbol") or "")
        bound = bounds.get(symbol) if isinstance(bounds.get(symbol), dict) else {}
        if str(item.get("action") or "") not in allowed_actions:
            raise ReplayRiskGateError(f"统一计划 Controller 动作无效：{symbol}", code="invalid_authoritative_plan")
        if not isinstance(item.get("permission_allowed"), bool) or not isinstance(bound.get("permission_allowed"), bool):
            raise ReplayRiskGateError(f"统一计划 Controller 权限格式无效：{symbol}", code="invalid_authoritative_plan")
        if not legacy and (
            not isinstance(item.get("permission"), dict)
            or not isinstance(item.get("obligation"), dict)
            or not isinstance(bound.get("permission"), dict)
            or not isinstance(bound.get("obligation"), dict)
            or str(item.get("intent") or "") != str(bound.get("intent") or "")
            or item.get("permission") != bound.get("permission")
            or item.get("obligation") != bound.get("obligation")
        ):
            raise ReplayRiskGateError(f"统一计划 Controller V2 权限或义务不完整：{symbol}", code="invalid_authoritative_plan")
        if (
            str(bound.get("action") or "") != str(item.get("action") or "")
            or bool(bound.get("permission_allowed")) != bool(item.get("permission_allowed"))
            or abs(_finite_weight(bound.get("min_weight"), field=f"{symbol}.bound.min") - _finite_weight(item.get("min_weight"), field=f"{symbol}.action.min")) > 1e-10
            or abs(_finite_weight(bound.get("max_weight"), field=f"{symbol}.bound.max") - _finite_weight(item.get("max_weight"), field=f"{symbol}.action.max")) > 1e-10
        ):
            raise ReplayRiskGateError(f"统一计划 Controller 动作与边界内容不一致：{symbol}", code="invalid_authoritative_plan")
        _finite_weight(item.get("current_weight"), field=f"{symbol}.action.current_weight")
        if not legacy:
            risk_item = (risk_limits.get("items") or {}).get(symbol) or {}
            final_limit = _finite_weight(risk_item.get("final_limit"), field=f"risk.{symbol}.final_limit")
            if (
                abs(_finite_weight(item.get("position_weight_cap"), field=f"{symbol}.position_weight_cap") - final_limit) > 1e-8
                or _finite_weight(item.get("max_weight"), field=f"{symbol}.action.max") > final_limit + 1e-8
            ):
                raise ReplayRiskGateError(f"Controller 标的上限未绑定统一风险合同：{symbol}", code="invalid_authoritative_plan")
    proposal = [item for item in contract.get("proposal") or [] if isinstance(item, dict)]
    proposal_symbols = [str(item.get("symbol") or "") for item in proposal]
    if len(proposal_symbols) != len(set(proposal_symbols)):
        raise ReplayRiskGateError("统一计划提案包含重复标的", code="invalid_authoritative_plan")
    if set(proposal_symbols) - set(action_symbols):
        raise ReplayRiskGateError("统一计划存在未经过 Controller 的标的", code="invalid_authoritative_plan")
    final_weights = _weight_map(contract.get("final_target_weights"), field="final_target_weights")
    if set(final_weights) - set(action_symbols):
        raise ReplayRiskGateError("统一计划最终权重包含未经过 Controller 的标的", code="invalid_authoritative_plan")
    contract_symbols = [str(value or "") for value in contract.get("target_symbols") or []]
    target_weights = _weight_map(contract.get("target_weights"), field="target_weights")
    if (
        len(contract_symbols) != len(set(contract_symbols))
        or set(target_weights) != set(contract_symbols)
        or {symbol for symbol, value in final_weights.items() if _finite_weight(value, field=str(symbol)) > 1e-12}
        != set(contract_symbols)
    ):
        raise ReplayRiskGateError("统一计划目标标的合同不一致", code="invalid_authoritative_plan")
    if sum(_finite_weight(value, field=str(symbol)) for symbol, value in target_weights.items()) > 1.000001:
        raise ReplayRiskGateError("统一计划目标权重超过100%", code="invalid_authoritative_plan")
    for symbol in contract_symbols:
        if abs(
            _finite_weight(target_weights.get(symbol), field=f"{symbol}.target_weight")
            - _finite_weight(final_weights.get(symbol), field=f"{symbol}.final_target_weight")
        ) > 1e-8:
            raise ReplayRiskGateError("统一计划执行权重与最终冻结权重不一致", code="invalid_authoritative_plan")
    for symbol in action_symbols:
        bound = bounds.get(symbol) if isinstance(bounds.get(symbol), dict) else {}
        lower = _finite_weight(bound.get("min_weight"), field=f"{symbol}.min_weight")
        upper = _finite_weight(bound.get("max_weight"), field=f"{symbol}.max_weight")
        weight = _finite_weight(final_weights.get(symbol, 0.0), field=f"{symbol}.final_target_weight")
        action_item = next(item for item in actions if str(item.get("symbol") or "") == symbol)
        action = str(action_item.get("action") or "")
        current = _finite_weight(action_item.get("current_weight"), field=f"{symbol}.current_weight")
        proposal_item = next(
            (item for item in (contract.get("proposal") or []) if isinstance(item, dict) and str(item.get("symbol") or "") == symbol),
            {},
        )
        residual = proposal_item.get("residual_breach") if isinstance(proposal_item.get("residual_breach"), dict) else {}
        residual_allowed = bool(residual.get("active")) and any(
            str(order.get("symbol") or "") == symbol
            and str(order.get("side") or "").upper() == "SELL"
            and int(order.get("planned_quantity") or 0) >= 100
            for order in (contract.get("execution_orders") or []) if isinstance(order, dict)
        )
        if lower > upper + 1e-10 or weight < lower - 1e-8 or (weight > upper + 1e-8 and not residual_allowed):
            raise ReplayRiskGateError(f"统一计划违反 Controller 权重边界：{symbol}", code="invalid_authoritative_plan")
        direction_invalid = (
            action == "buy_candidate" and current > 1e-8
            or action in {"buy_candidate", "add_position"} and weight < current - 1e-8
            or action in {"reduce_position", "sell_exit"} and weight > current + 1e-8
            or action == "hold" and abs(weight - current) > 1e-8
            or action == "sell_exit" and weight > 1e-8
            or not bool(action_item.get("permission_allowed")) and abs(weight - current) > 1e-8
        )
        bound_direction_invalid = (
            action == "buy_candidate" and lower > 1e-8
            or action == "add_position" and lower < current - 1e-8
            or action == "reduce_position" and upper > current + 1e-8
            or action == "sell_exit" and upper > 1e-8
            or action == "hold" and (
                abs(lower - current) > 1e-8 or abs(upper - current) > 1e-8
            )
            or not bool(action_item.get("permission_allowed")) and (
                abs(lower - current) > 1e-8 or abs(upper - current) > 1e-8
            )
        )
        if direction_invalid or bound_direction_invalid:
            raise ReplayRiskGateError(f"统一计划改变了 Controller 动作方向：{symbol}", code="invalid_authoritative_plan")
    for item in proposal:
        symbol = str(item.get("symbol") or "")
        if abs(
            _finite_weight(item.get("target_weight"), field=f"proposal.{symbol}.target_weight")
            - final_weights.get(symbol, 0.0)
        ) > 1e-8:
            raise ReplayRiskGateError(f"统一计划提案与最终权重不一致：{symbol}", code="invalid_authoritative_plan")
    proposal_by_symbol = {str(item.get("symbol") or ""): item for item in proposal}
    action_by_symbol = {str(item.get("symbol") or ""): item for item in actions}
    for item in actions:
        symbol = str(item.get("symbol") or "")
        current = _finite_weight(item.get("current_weight"), field=f"{symbol}.current_weight")
        target = final_weights.get(symbol, 0.0)
        if abs(target - current) > 1e-8 and symbol not in proposal_by_symbol:
            raise ReplayRiskGateError(f"统一计划缺少权重变化提案：{symbol}", code="invalid_authoritative_plan")
    for symbol, item in proposal_by_symbol.items():
        if abs(
            _finite_weight(item.get("current_weight"), field=f"proposal.{symbol}.current_weight")
            - _finite_weight(action_by_symbol[symbol].get("current_weight"), field=f"controller.{symbol}.current_weight")
        ) > 1e-8:
            raise ReplayRiskGateError(f"统一计划提案与 Controller 当前权重不一致：{symbol}", code="invalid_authoritative_plan")
    expected_map = {
        str(symbol): _finite_number(value, field=f"expected_return.{symbol}")
        for symbol, value in expected_return.get("optimizer_expected_returns", {}).items()
        if str(symbol)
    }
    expected_items = [
        item for item in expected_return.get("items") or []
        if isinstance(item, dict)
    ]
    expected_item_symbols = [str(item.get("symbol") or "") for item in expected_items]
    if (
        set(expected_map) != set(action_symbols)
        or set(expected_item_symbols) != set(action_symbols)
        or len(expected_item_symbols) != len(set(expected_item_symbols))
    ):
        raise ReplayRiskGateError("统一计划预期收益没有覆盖全部 Controller 标的", code="invalid_authoritative_plan")
    for item in expected_items:
        symbol = str(item.get("symbol") or "")
        components = item.get("components") if isinstance(item.get("components"), dict) else {}
        penalties = item.get("penalties") if isinstance(item.get("penalties"), dict) else {}
        if (
            abs(
                _finite_number(
                    item.get("expected_20d_return_before_trade_cost"),
                    field=f"expected_return.items.{symbol}",
                )
                - expected_map[symbol]
            ) > 1e-8
            or not components
            or not penalties
        ):
            raise ReplayRiskGateError(f"统一计划逐股预期收益合同不完整：{symbol}", code="invalid_authoritative_plan")
        for key, value in components.items():
            _finite_number(value, field=f"expected_return.items.{symbol}.components.{key}")
        for key, value in penalties.items():
            _finite_number(value, field=f"expected_return.items.{symbol}.penalties.{key}")
        if abs(
            _finite_number(
                action_by_symbol[symbol].get("expected_20d_return_before_trade_cost"),
                field=f"controller.{symbol}.expected_20d_return",
            )
            - expected_map[symbol]
        ) > 1e-8:
            raise ReplayRiskGateError(f"统一计划 Controller 与预期收益输入不一致：{symbol}", code="invalid_authoritative_plan")
    for symbol, item in proposal_by_symbol.items():
        if abs(
            _finite_number(item.get("expected_20d_return"), field=f"proposal.{symbol}.expected_20d_return")
            - expected_map[symbol]
        ) > 1e-8:
            raise ReplayRiskGateError(f"统一计划提案预期收益与优化器输入不一致：{symbol}", code="invalid_authoritative_plan")
    frozen_environment = selection.get("environment_snapshot") or {}
    if str(expected_return.get("market_state") or "") != str(
        frozen_environment.get("selection_market_state")
        or frozen_environment.get("production_market_state")
        or ""
    ):
        raise ReplayRiskGateError("统一计划预期收益环境与冻结切片不一致", code="invalid_authoritative_plan")
    competition = contract["portfolio_method_competition"]
    if str(competition.get("schema_version") or "") != expected_competition_schema or competition.get("ok") is not True:
        raise ReplayRiskGateError("统一计划组合竞赛合同版本或状态无效", code="invalid_authoritative_plan")
    selected_method = str(competition.get("selected_method") or "")
    if not legacy and competition.get("risk_limit_contract") != risk_limits:
        raise ReplayRiskGateError("组合竞赛未使用冻结风险上限合同", code="invalid_authoritative_plan")
    if not legacy and (contract.get("decision_version_manifest") or {}).get("portfolio", {}).get("risk_limit_contract") != risk_limits:
        raise ReplayRiskGateError("版本清单未冻结风险上限合同", code="invalid_authoritative_plan")
    allowed_methods = {"empirical_cvar", "equal_weight", "hrp", "hold_current"} if legacy else {
        "empirical_cvar", "equal_weight", "hrp", "minimum_turnover_feasible",
    }
    if selected_method not in allowed_methods:
        raise ReplayRiskGateError("统一计划组合竞赛赢家方法无效", code="invalid_authoritative_plan")
    winner = competition.get("winner")
    winner_weights = winner.get("weights") if isinstance(winner, dict) and isinstance(winner.get("weights"), dict) else {}
    if (
        str((winner or {}).get("method") or "") != selected_method
        or not _same_weights(competition.get("weights"), winner_weights, left_field="competition.weights", right_field="competition.winner.weights")
        or not _same_weights(final_weights, winner_weights, left_field="final_target_weights", right_field="competition.winner.weights")
        or winner not in [item for item in (competition.get("evaluations") or []) if isinstance(item, dict)]
    ):
        raise ReplayRiskGateError("统一计划最终权重、竞赛方法与赢家不一致", code="invalid_authoritative_plan")
    competition_bounds = competition.get("weight_bounds") if isinstance(competition.get("weight_bounds"), dict) else {}
    for symbol in action_symbols:
        candidate = competition_bounds.get(symbol) if isinstance(competition_bounds.get(symbol), dict) else {}
        bound = bounds[symbol]
        if (
            str(candidate.get("action") or "") != str(bound.get("action") or "")
            or abs(_finite_weight(candidate.get("min_weight"), field=f"{symbol}.competition.min") - _finite_weight(bound.get("min_weight"), field=f"{symbol}.controller.min")) > 1e-10
            or abs(_finite_weight(candidate.get("max_weight"), field=f"{symbol}.competition.max") - _finite_weight(bound.get("max_weight"), field=f"{symbol}.controller.max")) > 1e-10
        ):
            raise ReplayRiskGateError(f"统一计划竞赛没有使用 Controller 边界：{symbol}", code="invalid_authoritative_plan")
    if not legacy:
        execution_orders = [
            item for item in (contract.get("execution_orders") or [])
            if isinstance(item, dict)
        ]
        order_symbols = [str(item.get("symbol") or "") for item in execution_orders]
        if any(not symbol for symbol in order_symbols) or len(order_symbols) != len(set(order_symbols)):
            raise ReplayRiskGateError("统一计划冻结订单标的为空或重复", code="invalid_authoritative_plan")
        if execution_orders != competition.get("execution_orders") or execution_orders != (winner or {}).get("execution_orders"):
            raise ReplayRiskGateError("统一计划冻结订单与竞赛赢家不一致", code="invalid_authoritative_plan")
        constraint_hash = str(competition.get("constraint_hash") or "")
        evaluations = [item for item in (competition.get("evaluations") or []) if isinstance(item, dict)]
        if (
            not constraint_hash
            or len(evaluations) != 4
            or any(str(item.get("constraint_hash") or "") != constraint_hash for item in evaluations)
        ):
            raise ReplayRiskGateError("组合竞赛四种方法没有共享同一约束合同", code="invalid_authoritative_plan")
        cost_contract = contract.get("execution_cost_contract") if isinstance(contract.get("execution_cost_contract"), dict) else {}
        if (
            cost_contract.get("includes_fees_and_slippage") is not True
            or not isinstance(cost_contract.get("component_policy"), dict)
            or cost_contract != competition.get("execution_cost_contract")
            or cost_contract != (contract.get("decision_version_manifest") or {}).get("execution_cost")
        ):
            raise ReplayRiskGateError("统一计划未冻结完整手续费与滑点合同", code="invalid_authoritative_plan")
        orders_by_symbol = {str(item.get("symbol") or ""): item for item in execution_orders}
        for item in execution_orders:
            symbol = str(item.get("symbol") or "")
            side = str(item.get("side") or "").upper()
            try:
                quantity = int(item.get("planned_quantity"))
            except (TypeError, ValueError):
                quantity = 0
            if quantity <= 0 or side not in {"BUY", "SELL"}:
                raise ReplayRiskGateError(f"冻结订单方向或数量无效：{symbol}", code="invalid_authoritative_plan")
            if side == "BUY" and quantity % 100 != 0:
                raise ReplayRiskGateError(f"冻结买单不是整手：{symbol}", code="invalid_authoritative_plan")
            if side == "SELL" and quantity % 100 != 0 and item.get("full_position_exit") is not True:
                raise ReplayRiskGateError(f"冻结卖单零股数量仅允许完整退出：{symbol}", code="invalid_authoritative_plan")
            allowed_sides = set(str(value or "").upper() for value in ((action_by_symbol[symbol].get("permission") or {}).get("allowed_sides") or []))
            if side not in allowed_sides:
                raise ReplayRiskGateError(f"冻结订单违反 Controller 方向权限：{symbol}", code="invalid_authoritative_plan")
            post_weight = _finite_weight(item.get("post_lot_target_weight"), field=f"order.{symbol}.post_weight")
            if abs(post_weight - final_weights.get(symbol, 0.0)) > 1e-8:
                raise ReplayRiskGateError(f"冻结订单与整手后目标权重不一致：{symbol}", code="invalid_authoritative_plan")
            cost = item.get("cost") if isinstance(item.get("cost"), dict) else {}
            for key in (
                "gross_amount", "commission", "transfer_fee", "stamp_tax",
                "slippage_cost", "calibration_buffer", "total_cost",
            ):
                if _finite_number(cost.get(key), field=f"order.{symbol}.cost.{key}") < 0:
                    raise ReplayRiskGateError(f"冻结订单成本为负：{symbol}", code="invalid_authoritative_plan")
        for symbol, action_item in action_by_symbol.items():
            current = _finite_weight(action_item.get("current_weight"), field=f"controller.{symbol}.current_weight")
            changed = abs(final_weights.get(symbol, 0.0) - current) > 1e-8
            if changed != (symbol in orders_by_symbol):
                raise ReplayRiskGateError(f"冻结订单没有完整覆盖权重变化：{symbol}", code="invalid_authoritative_plan")
            obligation = action_item.get("obligation") if isinstance(action_item.get("obligation"), dict) else {}
            if obligation.get("required"):
                target_max = _finite_weight(obligation.get("target_max_weight"), field=f"controller.{symbol}.obligation.target")
                order = orders_by_symbol.get(symbol) or {}
                residual = (proposal_by_symbol.get(symbol) or {}).get("residual_breach") or {}
                residual_allowed = bool(residual.get("active")) and int(order.get("planned_quantity") or 0) >= 100
                if residual_allowed:
                    remaining = final_weights.get(symbol, 0.0) - target_max
                    if (
                        str(obligation.get("type") or "") != "mandatory_reduce"
                        or residual.get("requires_next_day_review") is not True
                        or not str(residual.get("reason_code") or "")
                        or abs(_finite_weight(residual.get("required_max_weight"), field=f"residual.{symbol}.required") - target_max) > 1e-8
                        or abs(_finite_weight(residual.get("projected_post_weight"), field=f"residual.{symbol}.projected") - final_weights.get(symbol, 0.0)) > 1e-8
                        or abs(_finite_weight(residual.get("remaining_excess_weight"), field=f"residual.{symbol}.remaining") - remaining) > 1e-8
                        or int(residual.get("planned_repair_quantity") or 0) != int(order.get("planned_quantity") or 0)
                        or residual != (order.get("residual_breach") or {})
                        or residual != (competition.get("residual_breaches") or {}).get(symbol)
                    ):
                        raise ReplayRiskGateError(f"剩余风险证据与冻结订单不一致：{symbol}", code="invalid_authoritative_plan")
                if (
                    str(order.get("side") or "").upper() != "SELL"
                    or int(order.get("planned_quantity") or 0) <= 0
                    or (final_weights.get(symbol, 0.0) > target_max + 1e-8 and not residual_allowed)
                ):
                    raise ReplayRiskGateError(f"冻结订单未完成 Controller 强制义务：{symbol}", code="invalid_authoritative_plan")
        for symbol, proposal_item in proposal_by_symbol.items():
            order = orders_by_symbol.get(symbol) or {}
            planned_quantity = int(proposal_item.get("planned_quantity") or 0)
            if planned_quantity != int(order.get("planned_quantity") or 0):
                raise ReplayRiskGateError(f"页面提案数量与冻结订单不一致：{symbol}", code="invalid_authoritative_plan")
            if planned_quantity <= 0 and str(proposal_item.get("action") or "") not in {"hold", "observe"}:
                raise ReplayRiskGateError(f"零数量提案不得显示交易动作：{symbol}", code="invalid_authoritative_plan")
    industry = contract["industry_allocation"]
    if (
        str(industry.get("schema_version") or "") != INDUSTRY_ALLOCATION_SCHEMA_VERSION
        or not isinstance(industry.get("industry_targets"), dict)
        or not isinstance(industry.get("symbol_industries"), dict)
        or not isinstance(industry.get("items"), list)
        or str(industry.get("method") or "") != "market_then_industry_then_stock_v1"
        or str(industry.get("as_of_time") or "") != str(selection.get("data_as_of_time") or "")
    ):
        raise ReplayRiskGateError("统一计划行业预算合同不完整", code="invalid_authoritative_plan")
    competition_industry = (
        competition.get("industry_budget")
        if isinstance(competition.get("industry_budget"), dict) else {}
    )
    if (
        str(competition_industry.get("schema_version") or "") != "alis_portfolio_industry_budget_v1"
        or competition_industry.get("symbol_industries") != industry.get("symbol_industries")
        or competition_industry.get("industry_targets") != {
            str(key): round(_finite_weight(value, field=f"industry.{key}"), 8)
            for key, value in (industry.get("industry_targets") or {}).items()
        }
        or str(competition_industry.get("application") or "")
        != "soft_deviation_penalty_applied_to_every_competing_method"
    ):
        raise ReplayRiskGateError("统一计划组合竞赛未消费冻结行业预算", code="invalid_authoritative_plan")
    checks = [item for item in contract.get("execution_checks") or [] if isinstance(item, dict)]
    check_keys = [str(item.get("key") or "") for item in checks]
    if (
        not check_keys
        or any(not key for key in check_keys)
        or len(check_keys) != len(set(check_keys))
        or any(str(item.get("status") or "") not in {"pass", "warn", "block"} for item in checks)
    ):
        raise ReplayRiskGateError("统一计划执行检查快照不完整", code="invalid_authoritative_plan")
    decision_gate = contract.get("decision_data_gate") if isinstance(contract.get("decision_data_gate"), dict) else {}
    if str(decision_gate.get("target_trade_date") or "").replace("-", "")[:8] != signal_date:
        raise ReplayRiskGateError("统一计划数据门日期与信号日不一致", code="invalid_authoritative_plan")
    derived_authorized = bool(decision_gate.get("decision_ready")) and not any(
        str(item.get("status") or "") == "block" for item in checks
    )
    if bool(contract.get("execution_authorized")) != derived_authorized:
        raise ReplayRiskGateError("统一计划执行授权与冻结检查不一致", code="invalid_authoritative_plan")
    champion_id = int(selection.get("champion_id") or 0)
    manifest_strategy = contract["decision_version_manifest"].get("strategy")
    if (
        not isinstance(manifest_strategy, dict)
        or int(manifest_strategy.get("model_version_id") or 0) != champion_id
        or str(manifest_strategy.get("version") or "") != str(selection.get("champion_version") or "")
        or int(manifest_strategy.get("strategy_id") or 0) != int(selection.get("strategy_id") or 0)
        or str(manifest_strategy.get("pretrade_controller_schema") or "") != expected_controller_schema
        or int(manifest_strategy.get("strategy_controller_version_id") or 0)
        != int(controller.get("strategy_policy_version_id") or 0)
        or str(manifest_strategy.get("strategy_controller_version") or "")
        != str(controller.get("strategy_policy_version") or "")
    ):
        raise ReplayRiskGateError("统一计划 Champion 绑定不一致", code="invalid_authoritative_plan")
    manifest = contract["decision_version_manifest"]
    if str(manifest.get("schema_version") or "") != DECISION_MANIFEST_SCHEMA_VERSION:
        raise ReplayRiskGateError("统一计划决策版本清单版本无效", code="invalid_authoritative_plan")
    if str(manifest.get("trade_date") or "").replace("-", "")[:8] != signal_date:
        raise ReplayRiskGateError("统一计划版本清单日期与信号日不一致", code="invalid_authoritative_plan")
    release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    if (
        int(release.get("id") or 0) <= 0
        or not str(release.get("release_key") or "")
        or len(str(release.get("manifest_hash") or "")) != 64
        or str(release.get("status") or "") != "active"
    ):
        raise ReplayRiskGateError("统一计划未冻结有效系统发布版本", code="missing_active_system_release")
    if not legacy and (
        (manifest.get("execution_contract") or {}).get("decision_schema") != UNIFIED_DAILY_DECISION_SCHEMA_VERSION
        or (manifest.get("execution_contract") or {}).get("t1_safety_schema") != UNIFIED_EXECUTION_SAFETY_SCHEMA_VERSION
        or (manifest.get("execution_contract") or {}).get("quantity_policy")
        != "t_day_frozen_exact_quantity_or_t1_safety_veto"
    ):
        raise ReplayRiskGateError("统一计划执行版本清单不完整", code="invalid_authoritative_plan")
    manifest_selection = manifest.get("selection") if isinstance(manifest.get("selection"), dict) else {}
    if (
        str(manifest_selection.get("schema_version") or "") != UNIFIED_SELECTION_SCHEMA_VERSION
        or str(manifest_selection.get("selection_hash") or "") != str(selection.get("selection_hash") or "")
        or manifest_selection.get("environment_snapshot") != selection.get("environment_snapshot")
        or manifest_selection.get("factor_expert_snapshot") != selection.get("factor_expert_snapshot")
    ):
        raise ReplayRiskGateError("统一计划选股快照与版本清单不一致", code="invalid_authoritative_plan")
    factor_contract = manifest.get("factor_contract") if isinstance(manifest.get("factor_contract"), dict) else {}
    factor_bindings = (selection.get("factor_expert_snapshot") or {}).get("factor_bindings") or {}
    if (
        str(factor_contract.get("schema_version") or "") != "alis_factor_registry_v1"
        or factor_contract.get("bindings") != factor_bindings
    ):
        raise ReplayRiskGateError("统一计划因子版本清单与实际选股不一致", code="invalid_authoritative_plan")
    fusion_models = {
        str(item.get("model_key") or ""): {
            "id": int(item.get("version_id") or 0),
            "version": str(item.get("version") or ""),
        }
        for item in (((selection.get("model_signal_snapshot") or {}).get("fusion") or {}).get("production_models") or [])
        if isinstance(item, dict) and item.get("version_id") is not None
    }
    manifest_models = manifest.get("model_versions") if isinstance(manifest.get("model_versions"), dict) else {}
    manifest_model_identity = {
        str(key): {"id": int(item.get("id") or 0), "version": str(item.get("version") or "")}
        for key, item in manifest_models.items()
        if isinstance(item, dict)
    }
    # A fully quarantined model family is a valid production state: every
    # model slot remains frozen in the signal snapshot as unavailable, while
    # the version manifest is intentionally empty and ranking falls back to
    # factors and deterministic rules.  Equality still rejects any missing,
    # stale or extra consumed model binding.
    if manifest_model_identity != fusion_models:
        raise ReplayRiskGateError("统一计划模型版本清单与实际融合不一致", code="invalid_authoritative_plan")
    manifest_portfolio = manifest.get("portfolio") if isinstance(manifest.get("portfolio"), dict) else {}
    if (
        str(manifest_portfolio.get("selected_method") or "") != selected_method
        or str(manifest_portfolio.get("competition_schema") or "") != expected_competition_schema
    ):
        raise ReplayRiskGateError("统一计划组合竞赛与版本清单不一致", code="invalid_authoritative_plan")
    manifest_hash = str(manifest.get("manifest_hash") or "")
    manifest_payload = {
        key: value for key, value in manifest.items()
        if key != "manifest_hash"
    }
    if not manifest_hash or _canonical_hash(manifest_payload) != manifest_hash:
        raise ReplayRiskGateError("统一计划决策版本清单哈希校验失败", code="invalid_authoritative_plan_hash")

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
    decision_contract = plan.get("decision_contract") if isinstance(plan.get("decision_contract"), dict) else {}
    contract_schema = str(decision_contract.get("schema_version") or "")
    unified = contract_schema in {
        UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
    }
    if contract_schema.startswith("alis_unified_") and not unified:
        raise ReplayRiskGateError("统一正式计划合同版本不受支持", code="invalid_authoritative_plan")
    if unified:
        if _canonical_hash(decision_contract) != str(plan.get("input_hash") or ""):
            raise ReplayRiskGateError("统一正式计划完整哈希校验失败", code="invalid_authoritative_plan_hash")
        validate_unified_decision_contract(decision_contract)
    raw_symbols = [str(value or "").strip() for value in plan.get("target_symbols") or []]
    symbols = [value for value in raw_symbols if value]
    if len(symbols) != len(set(symbols)):
        raise ReplayRiskGateError("正式调仓计划包含重复标的", code="invalid_authoritative_plan")
    raw_weights = plan.get("target_weights") if isinstance(plan.get("target_weights"), dict) else {}
    weights: dict[str, float] = {}
    for symbol, value in raw_weights.items():
        key = str(symbol or "").strip()
        weight = _finite_weight(value, field=key)
        if not key:
            raise ReplayRiskGateError(f"正式调仓计划权重非法：{key}", code="invalid_authoritative_plan")
        if weight > 0:
            weights[key] = weight
    if set(weights) - set(symbols):
        raise ReplayRiskGateError("正式调仓计划权重包含目标池外标的", code="invalid_authoritative_plan")
    if sum(weights.values()) > 1.000001:
        raise ReplayRiskGateError("正式调仓计划目标权重超过100%", code="invalid_authoritative_plan")
    if unified:
        contract_symbols = [
            str(value or "").strip()
            for value in decision_contract.get("target_symbols") or []
            if str(value or "").strip()
        ]
        contract_weights = {
            str(symbol or "").strip(): _finite_weight(value, field=str(symbol))
            for symbol, value in (decision_contract.get("target_weights") or {}).items()
        }
        if symbols != contract_symbols or set(weights) != set(contract_weights) or any(
            abs(weights[symbol] - contract_weights[symbol]) > 1e-8 for symbol in weights
        ):
            raise ReplayRiskGateError(
                "统一正式计划外层目标与冻结合同不一致",
                code="invalid_authoritative_plan",
            )
    proposal = [item for item in plan.get("proposal") or [] if isinstance(item, dict)]
    if unified and proposal != decision_contract.get("proposal"):
        raise ReplayRiskGateError("统一正式计划外层提案与冻结合同不一致", code="invalid_authoritative_plan")
    if unified and plan.get("decision_version_manifest") != decision_contract.get("decision_version_manifest"):
        raise ReplayRiskGateError("统一正式计划外层版本清单与冻结合同不一致", code="invalid_authoritative_plan")
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
        "decision_contract": decision_contract,
        "is_unified_frozen_plan": unified,
        "pretrade_controller": decision_contract.get("pretrade_controller") if unified else {},
        "execution_safety_contract": decision_contract.get("execution_safety_contract") if unified else {},
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
    decision_contract = plan.get("decision_contract") if isinstance(plan.get("decision_contract"), dict) else {}
    selection_snapshot = decision_contract.get("selection_snapshot") if isinstance(decision_contract.get("selection_snapshot"), dict) else {}
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
        "selection_snapshot": selection_snapshot,
        "research_admission": {
            "admitted": list(selection_snapshot.get("research_admitted_symbols") or []),
            "blocked": list(selection_snapshot.get("research_blocks") or []),
            "source": "t_day_frozen_contract" if plan.get("is_unified_frozen_plan") else "legacy_execution_compatibility",
        },
        "industry_allocation": decision_contract.get("industry_allocation") or {},
        "expected_return_contract": decision_contract.get("expected_return_contract") or {},
        "pretrade_controller": decision_contract.get("pretrade_controller") or {},
        "plan_provenance": plan["plan_provenance"],
        "decision_version_manifest": plan["decision_version_manifest"],
    }
