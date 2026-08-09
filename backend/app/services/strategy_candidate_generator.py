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
from app.services.strategy_config import get_replay_required_factor_names
from app.services.experiment_memory import find_rejected_hypothesis, upsert_experiment_memory


CHALLENGER_MODEL_KEY = "alis_strategy_challenger"
DYNAMIC_EXPOSURE_NAME = "ALIS Challenger 低风险ETF动态暴露 V2.2"
DUAL_TREND_NAME = "ALIS Challenger 双周期趋势确认 V2.3"
ADAPTIVE_NAME_PREFIX = "ALIS Challenger 自适应攻防"
SELECTION_ALLOCATION_NAME_PREFIX = "ALIS Challenger 价值低波反转"
SELECTION_ALLOCATION_TOP_N = 15
DIVERSIFIED_SELECTION_NAME_PREFIX = "ALIS Challenger 价值低波反转分散化"
REGIME_SELECTION_NAME_PREFIX = "ALIS Challenger 价值低波状态风控"
REGIME_FACTOR_NAME_PREFIX = "ALIS Challenger 价值低波状态因子"
MAX_AUTONOMOUS_LINEAGE_TRIALS_PER_ROOT = 8
MAX_ADAPTIVE_PARAMETER_TRIALS = 4


def _active_replay_factor_bindings(
    content: dict[str, Any],
    *,
    model_bindings: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Freeze only active factors that the replay will actually consume."""
    required = get_replay_required_factor_names(
        content,
        "formula",
        model_bindings=model_bindings,
    )
    bindings = active_factor_bindings(required)
    missing = sorted(required - set(bindings))
    if missing:
        raise ValueError(
            "候选策略未通过冻结因子合同，缺少当前 active 绑定："
            + ",".join(missing)
        )
    return bindings


def _stable_evidence_hash(value: dict[str, Any]) -> str:
    """Fingerprint the persisted OOS evidence used to create a candidate."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_selection_allocation_challenger(
    *,
    failure_attribution: dict[str, Any],
    source_model_version_id: int,
) -> dict[str, Any] | None:
    """Create a research-backed, bounded selector for a persisted residual diagnosis."""
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
        "template": "china_value_lowvol_reversal_v1",
        "source_attribution_hash": attribution_hash,
        "changed_fields": [
            {"path": "universe.exclude_smallest_float_mv_pct", "to": 0.30},
            {"path": "score", "to": "earnings_yield_low_downside_volatility_short_reversal"},
            {"path": "rebalance", "to": "weekly_rank_buffer"},
        ],
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
    source_universe = source_content.get("universe") if isinstance(source_content.get("universe"), dict) else {}
    original_top_n = max(1, int(rebalance.get("top_n") or 20))

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
    content["schema_version"] = "alis_challenger_china_value_lowvol_reversal_v1"
    content["universe"] = {
        **source_universe,
        "exchange_group": "HS",
        "exclude_st": True,
        "exclude_suspended": True,
        "exclude_smallest_float_mv_pct": 0.30,
    }
    content["filters"] = [
        {"factor": "is_st", "op": "==", "value": 0},
        {"factor": "is_suspended", "op": "==", "value": 0},
        {"factor": "earnings_yield", "op": ">", "value": 0},
    ]
    content["score"] = {
        "normalization": "rank_percentile",
        "factors": {
            "earnings_yield": 0.45,
            "downside_volatility_60": -0.20,
            "max_drawdown_60": 0.20,
            "momentum_5": -0.15,
        },
    }
    content["rebalance"] = {**rebalance, "freq": "weekly", "top_n": SELECTION_ALLOCATION_TOP_N}
    content["lookback_days"] = 120
    content["expected_return"] = {"source": "ranking_score", "scale": 0.06}
    content["challenger_controls"] = {
        "max_single_weight": 0.08,
        "max_rebalance_turnover": 0.20,
        "min_weight_change": 0.02,
        "max_entry_1d_return": 0.07,
        "max_entry_5d_return": 0.20,
        "initial_deployment_turnover_exempt": True,
        "buy_rank_max": 12,
        "hold_rank_max": 18,
        "rationale": [
            "排除壳价值污染最强的最小市值30%",
            "只保留正盈利公司并用盈利收益率替代负PE排序",
            "使用低下行波动、较小历史回撤和短期反转，停止5/20日直接追涨",
            "周频、20%双边换手上限和买入/持有排名缓冲降低交易成本",
        ],
    }
    content.pop("dynamic_market_exposure", None)
    evidence_contract = {
        "schema_version": "alis_selection_allocation_candidate_evidence_v2",
        "source_model_version_id": int(source_model_version_id),
        "source_model_version": str(source.get("version") or ""),
        "source_strategy_id": source_strategy_id,
        "source_attribution_hash": attribution_hash,
        "source_failure_attribution": frozen_attribution,
        "hypothesis": "A股残差主要来自追涨、负估值误排与极值敏感排序；排除最小市值30%、正盈利过滤、盈利收益率、低下行波动、短期反转和买入/持有排名缓冲应提高成本后样本外收益。",
        "research_sources": [
            {"doi": "10.1016/j.jfineco.2019.03.008", "finding": "排除最小市值30%，盈利收益率优于账面市值比"},
            {"doi": "10.1016/j.pacfin.2021.101607", "finding": "A股价值、风险与残差反转证据较强，传统过去收益证据较弱"},
            {"doi": "10.1093/rfs/hhv063", "finding": "买入门高于持有门和低换手是最有效的成本缓解方法"},
        ],
        "experiment_memory": {
            "target": "strategy",
            "hypothesis": memory_hypothesis,
        },
        "changed_fields": [
            {"path": "universe.exclude_smallest_float_mv_pct", "from": source_universe.get("exclude_smallest_float_mv_pct"), "to": 0.30},
            {"path": "filters", "from": source_content.get("filters") or [], "to": content["filters"]},
            {"path": "score", "from": source_content.get("score") or {}, "to": content["score"]},
            {"path": "rebalance.freq", "from": rebalance.get("freq") or "daily", "to": "weekly"},
            {"path": "rebalance.top_n", "from": original_top_n, "to": SELECTION_ALLOCATION_TOP_N},
            {"path": "challenger_controls", "from": source_content.get("challenger_controls") or {}, "to": content["challenger_controls"]},
            {"path": "expected_return", "from": source_content.get("expected_return") or {"source": "momentum_20"}, "to": content["expected_return"]},
        ],
        "non_changes": [
            "execution_contract",
            "transaction_cost_contract",
            "strategy_controller",
            "point_in_time_contract",
            "formal_oos_and_shadow_promotion_gates",
        ],
    }
    name = f"{SELECTION_ALLOCATION_NAME_PREFIX} V3-源版本{int(source_model_version_id)}"
    existing = next((item for item in list_strategies() if item.get("name") == name), None)
    if not existing:
        _ensure_strategy_sequence()
    strategy = (
        update_strategy(int(existing["id"]), name, "formula", content)
        if existing else create_strategy(name, "formula", content)
    )
    version_id = create_model_version(
        model_key=CHALLENGER_MODEL_KEY,
        version=f"alis-challenger-china-value-lowvol-reversal-v1-{attribution_hash[:12]}",
        status="candidate",
        artifact_path="builtin://alis/challenger/china-value-lowvol-reversal-v1",
        config={
            "role": "challenger",
            "generation": "automatic_failure_attribution",
            "diagnosis": "selection_or_allocation_residual",
            "strategy_id": int(strategy["id"]),
            "champion_reference": champion["champion_version"],
            "selection_strategy": content,
            "factor_bindings": _active_replay_factor_bindings(content),
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


def ensure_diversified_selection_challenger(*, source_model_version_id: int) -> dict[str, Any] | None:
    """Broaden a near-miss value selector once, without changing its alpha thesis."""
    source = get_model_version(int(source_model_version_id)) or {}
    source_config = source.get("config") if isinstance(source.get("config"), dict) else {}
    source_metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    formal = source_metrics.get("formal_oos") if isinstance(source_metrics.get("formal_oos"), dict) else {}
    if (
        str(formal.get("status") or "") != "failed"
        or int(formal.get("valid_windows") or 0) < 5
        or int(formal.get("outperform_windows") or 0) < 3
    ):
        return None
    excess = float(formal.get("aggregate_excess_return") or 0.0)
    if not (-0.02 <= excess < 0.0):
        return None
    source_strategy_id = int(source_config.get("strategy_id") or 0)
    source_strategy = get_strategy(source_strategy_id) if source_strategy_id else None
    source_content = source_config.get("selection_strategy")
    if not isinstance(source_content, dict):
        source_content = (source_strategy or {}).get("content")
    if not isinstance(source_content, dict):
        return None
    source_controls = source_content.get("challenger_controls") if isinstance(source_content.get("challenger_controls"), dict) else {}
    source_rebalance = source_content.get("rebalance") if isinstance(source_content.get("rebalance"), dict) else {}
    formal_hash = _stable_evidence_hash(formal)
    memory_hypothesis = {
        "template": "china_value_lowvol_reversal_diversified_v2",
        "source_model_version_id": int(source_model_version_id),
        "source_formal_hash": formal_hash,
        "changed_fields": [
            {"path": "rebalance.top_n", "to": 30},
            {"path": "challenger_controls.max_single_weight", "to": 0.04},
            {"path": "challenger_controls.buy_rank_max", "to": 24},
            {"path": "challenger_controls.hold_rank_max", "to": 36},
        ],
    }
    if find_rejected_hypothesis("strategy", memory_hypothesis):
        return None
    for version in list_model_versions(CHALLENGER_MODEL_KEY, limit=100):
        config = version.get("config") if isinstance(version.get("config"), dict) else {}
        experiment = config.get("experiment_memory") if isinstance(config.get("experiment_memory"), dict) else {}
        if experiment.get("hypothesis") == memory_hypothesis:
            strategy = get_strategy(int(config.get("strategy_id") or 0))
            return {
                "strategy_id": int(config.get("strategy_id") or 0),
                "model_version_id": int(version["id"]),
                "name": str((strategy or {}).get("name") or version.get("version") or DIVERSIFIED_SELECTION_NAME_PREFIX),
                "version": str(version.get("version") or ""),
                "status": str((version.get("validation") or {}).get("status") or version.get("status") or ""),
                "generated_by": "automatic_near_miss_diversification",
            }

    content = deepcopy(source_content)
    content["schema_version"] = "alis_challenger_china_value_lowvol_reversal_diversified_v2"
    content["rebalance"] = {**source_rebalance, "freq": "weekly", "top_n": 30}
    content["challenger_controls"] = {
        **source_controls,
        "max_single_weight": 0.04,
        "buy_rank_max": 24,
        "hold_rank_max": 36,
        "rationale": [
            *list(source_controls.get("rationale") or []),
            "正式五窗口已3/5跑赢且复合超额仅差2%以内；扩大持仓并降低单票上限以减少价值行业集中风险",
        ],
    }
    evidence_contract = {
        "schema_version": "alis_near_miss_diversification_candidate_evidence_v1",
        "source_model_version_id": int(source_model_version_id),
        "source_model_version": str(source.get("version") or ""),
        "source_strategy_id": source_strategy_id,
        "source_formal_oos": deepcopy(formal),
        "source_formal_hash": formal_hash,
        "hypothesis": "保持已验证的价值、低下行波动和短期反转排序，仅把15只/8%扩为30只/4%，检验分散化能否修复上涨窗口和后段回撤造成的微弱复合超额缺口。",
        "changed_fields": memory_hypothesis["changed_fields"],
        "non_changes": [
            "universe_and_positive_earnings_filter",
            "score_factors_and_rank_normalization",
            "expected_return_source",
            "weekly_frequency_and_turnover_limit",
            "execution_cost_point_in_time_oos_shadow_and_promotion_gates",
        ],
        "experiment_memory": {"target": "strategy", "hypothesis": memory_hypothesis},
    }
    name = f"{DIVERSIFIED_SELECTION_NAME_PREFIX} V4-源版本{int(source_model_version_id)}"
    existing = next((item for item in list_strategies() if item.get("name") == name), None)
    if not existing:
        _ensure_strategy_sequence()
    strategy = (
        update_strategy(int(existing["id"]), name, "formula", content)
        if existing else create_strategy(name, "formula", content)
    )
    champion = resolve_production_strategy_champion()
    version_id = create_model_version(
        model_key=CHALLENGER_MODEL_KEY,
        version=f"alis-challenger-china-value-lowvol-reversal-diversified-v2-{formal_hash[:12]}",
        status="candidate",
        artifact_path="builtin://alis/challenger/china-value-lowvol-reversal-diversified-v2",
        config={
            "role": "challenger",
            "generation": "automatic_near_miss_diversification",
            "diagnosis": "near_zero_aggregate_excess_with_majority_window_outperformance",
            "strategy_id": int(strategy["id"]),
            "champion_reference": champion["champion_version"],
            "selection_strategy": content,
            "factor_bindings": _active_replay_factor_bindings(content),
            "evidence_contract": evidence_contract,
            "experiment_memory": evidence_contract["experiment_memory"],
        },
        metrics={},
        validation={"status": "pending_oos", "production_eligible": False, "next_gate": "quick_diagnostic"},
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
        evidence={"candidate_version": str(version.get("version") or ""), "source_formal_hash": formal_hash},
    )
    return {
        "strategy_id": int(strategy["id"]),
        "model_version_id": int(version["id"]),
        "name": name,
        "version": str(version.get("version") or ""),
        "status": "pending_oos",
        "generated_by": "automatic_near_miss_diversification",
    }


def ensure_regime_aware_selection_challengers(*, source_model_version_id: int) -> list[dict[str, Any]]:
    """Create a bounded three-hypothesis risk competition on top of a passed selector."""
    source = get_model_version(int(source_model_version_id)) or {}
    source_config = source.get("config") if isinstance(source.get("config"), dict) else {}
    source_metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    formal = source_metrics.get("formal_oos") if isinstance(source_metrics.get("formal_oos"), dict) else {}
    if str(formal.get("status") or "") != "passed" or int(formal.get("valid_windows") or 0) < 5:
        return []
    source_strategy_id = int(source_config.get("strategy_id") or 0)
    source_strategy = get_strategy(source_strategy_id) if source_strategy_id else None
    source_content = source_config.get("selection_strategy")
    if not isinstance(source_content, dict):
        source_content = (source_strategy or {}).get("content")
    if not isinstance(source_content, dict):
        return []

    templates = (
        {
            "key": "state_ceiling",
            "label": "四状态风险上限",
            "policy": {
                "schema_version": "transparent_four_state_profile_ceiling_v1",
                "risk_model": "transparent_four_state_v1",
                "state_equity_weights": {"S0": 1.00, "S1": 0.97, "S2": 0.70, "S3": 0.90},
                "default_equity_weight": 0.97,
                "use_profile_risk_multiplier": True,
                "use_change_alarm": False,
                "minimum_equity_weight": 0.60,
                "maximum_equity_weight": 1.00,
                "etf_fallback": False,
                "benchmark_lag_guard": "T_close_only",
            },
            "hypothesis": "只在透明四状态识别到风险收缩或拥挤子状态时降低暴露，其余时间保持近满仓，避免旧风控候选的牛市现金拖累。",
        },
        {
            "key": "dual_trend_change_alarm",
            "label": "双趋势与变点防守",
            "policy": {
                "schema_version": "dual_trend_change_alarm_v1",
                "risk_model": "transparent_four_state_v1+bocpd_gaussian_v1",
                "state_equity_weights": {"S0": 1.00, "S1": 1.00, "S2": 0.75, "S3": 0.95},
                "default_equity_weight": 1.00,
                "use_profile_risk_multiplier": False,
                "use_change_alarm": True,
                "minimum_equity_weight": 0.55,
                "maximum_equity_weight": 1.00,
                "etf_fallback": False,
                "benchmark_lag_guard": "T_close_only",
                "benchmark_trend_overlay": {
                    "lookback_sessions": 20,
                    "confirmation_lookback_sessions": 60,
                    "positive_threshold": 0.02,
                    "confirmation_positive_threshold": 0.03,
                    "negative_threshold": -0.02,
                    "confirmation_negative_threshold": -0.03,
                    "positive_equity_floor": 1.00,
                    "negative_equity_cap": 0.60,
                },
            },
            "hypothesis": "中短双趋势仅对明确下行做仓位上限，不再对无趋势状态默认降仓；变点高置信报警时再附加减风险。",
        },
        {
            "key": "volatility_managed",
            "label": "波动率管理",
            "policy": {
                "schema_version": "regime_volatility_managed_v1",
                "risk_model": "transparent_four_state_v1+realized_volatility_v1",
                "state_equity_weights": {"S0": 1.00, "S1": 0.98, "S2": 0.75, "S3": 0.90},
                "default_equity_weight": 0.98,
                "use_profile_risk_multiplier": True,
                "use_change_alarm": False,
                "minimum_equity_weight": 0.60,
                "maximum_equity_weight": 1.00,
                "volatility_overlay": {
                    "lookback_sessions": 20,
                    "target_annualized": 0.22,
                    "minimum_scale": 0.65,
                    "maximum_scale": 1.00,
                },
                "etf_fallback": False,
                "benchmark_lag_guard": "T_close_only",
            },
            "hypothesis": "在指数实现波动率高于22%时线性降低风险，低波动阶段不加杠杆，检验是否能降低尾部回撤并保留价值低波Alpha。",
        },
    )
    research_sources = [
        {"doi": "10.1111/jofi.12513", "finding": "高波动时降低风险暴露可提高多类股票因子的Sharpe，且不依赖预期收益同比变化"},
        {"doi": "10.1016/j.frl.2020.101652", "finding": "中国A股羊群行为存在状态切换，风险不应用单一静态参数表示"},
        {"doi": "10.1016/j.jempfin.2017.04.001", "finding": "中国市场情绪对时序动量具有预测信息，可作为趋势风险的独立确认"},
        {"doi": "10.1016/j.jfineco.2014.11.010", "finding": "动量风险随时间变化，波动管理能减少崩溃风险；本项目只借用风险管理方法，不恢复追涨选股"},
    ]
    champion = resolve_production_strategy_champion()
    versions = list_model_versions(CHALLENGER_MODEL_KEY, limit=200)
    strategies = list_strategies()
    outcomes: list[dict[str, Any]] = []
    for index, template in enumerate(templates, start=1):
        memory_hypothesis = {
            "template": f"china_value_lowvol_regime_competition_{template['key']}_v1",
            "source_model_version_id": int(source_model_version_id),
            "dynamic_market_exposure": template["policy"],
        }
        existing_version = next(
            (
                version for version in versions
                if ((version.get("config") or {}).get("experiment_memory") or {}).get("hypothesis") == memory_hypothesis
            ),
            None,
        )
        if existing_version:
            config = existing_version.get("config") or {}
            strategy = get_strategy(int(config.get("strategy_id") or 0))
            outcomes.append({
                "strategy_id": int(config.get("strategy_id") or 0),
                "model_version_id": int(existing_version["id"]),
                "name": str((strategy or {}).get("name") or existing_version.get("version") or ""),
                "version": str(existing_version.get("version") or ""),
                "status": str((existing_version.get("validation") or {}).get("status") or existing_version.get("status") or ""),
                "generated_by": "automatic_regime_competition",
            })
            continue
        if find_rejected_hypothesis("strategy", memory_hypothesis):
            continue
        content = deepcopy(source_content)
        content["schema_version"] = f"alis_challenger_value_lowvol_regime_v5_{template['key']}"
        content["dynamic_market_exposure"] = deepcopy(template["policy"])
        controls = content.get("challenger_controls") if isinstance(content.get("challenger_controls"), dict) else {}
        content["challenger_controls"] = {
            **controls,
            "rationale": [*list(controls.get("rationale") or []), str(template["hypothesis"])],
        }
        evidence_contract = {
            "schema_version": "alis_regime_aware_selection_candidate_evidence_v1",
            "source_model_version_id": int(source_model_version_id),
            "source_model_version": str(source.get("version") or ""),
            "source_strategy_id": source_strategy_id,
            "source_formal_oos": deepcopy(formal),
            "hypothesis_key": template["key"],
            "hypothesis": template["hypothesis"],
            "research_sources": research_sources,
            "changed_fields": [{"path": "dynamic_market_exposure", "from": source_content.get("dynamic_market_exposure"), "to": template["policy"]}],
            "non_changes": [
                "value_lowvol_reversal_selection_alpha",
                "positive_earnings_and_small_cap_exclusion",
                "weekly_rank_buffer_and_turnover_controls",
                "transaction_cost_point_in_time_formal_oos_and_shadow_gates",
            ],
            "experiment_memory": {"target": "strategy", "hypothesis": memory_hypothesis},
        }
        name = f"{REGIME_SELECTION_NAME_PREFIX} V5{chr(64 + index)}-{template['label']}"
        existing_strategy = next((item for item in strategies if item.get("name") == name), None)
        if not existing_strategy:
            _ensure_strategy_sequence()
        strategy = (
            update_strategy(int(existing_strategy["id"]), name, "formula", content)
            if existing_strategy else create_strategy(name, "formula", content)
        )
        version_id = create_model_version(
            model_key=CHALLENGER_MODEL_KEY,
            version=f"alis-challenger-value-lowvol-regime-v5-{template['key']}-{_stable_evidence_hash(memory_hypothesis)[:12]}",
            status="candidate",
            artifact_path=f"builtin://alis/challenger/value-lowvol-regime-v5/{template['key']}",
            config={
                "role": "challenger",
                "generation": "automatic_regime_competition",
                "generation_index": 5,
                "lineage": {
                    "root_model_version_id": int(source_model_version_id),
                    "parent_model_version_id": int(source_model_version_id),
                    "generation_index": 5,
                    "hypothesis_family": "regime_exposure",
                    "applied_mutations": [str(template["key"])],
                },
                "diagnosis": "passed_selector_thin_excess_requires_regime_risk_competition",
                "strategy_id": int(strategy["id"]),
                "champion_reference": champion["champion_version"],
                "selection_strategy": content,
                "factor_bindings": _active_replay_factor_bindings(content),
                "evidence_contract": evidence_contract,
                "experiment_memory": evidence_contract["experiment_memory"],
            },
            metrics={},
            validation={"status": "pending_oos", "production_eligible": False, "next_gate": "quick_diagnostic"},
            is_active=False,
        )
        version = next(item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=200) if int(item["id"]) == int(version_id))
        upsert_experiment_memory(
            target="strategy",
            hypothesis=memory_hypothesis,
            status="observing",
            source_model_version_id=int(version_id),
            outcome_stage="candidate_created",
            evidence={"candidate_version": str(version.get("version") or ""), "source_model_version_id": int(source_model_version_id)},
        )
        outcomes.append({
            "strategy_id": int(strategy["id"]),
            "model_version_id": int(version["id"]),
            "name": name,
            "version": str(version.get("version") or ""),
            "status": "pending_oos",
            "generated_by": "automatic_regime_competition",
        })
    return outcomes


def ensure_regime_factor_selection_challengers(*, source_model_version_id: int) -> list[dict[str, Any]]:
    """Keep V4 fully invested while testing three bounded state-dependent factor mixes."""
    source = get_model_version(int(source_model_version_id)) or {}
    source_config = source.get("config") if isinstance(source.get("config"), dict) else {}
    source_metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    formal = source_metrics.get("formal_oos") if isinstance(source_metrics.get("formal_oos"), dict) else {}
    if str(formal.get("status") or "") != "passed" or int(formal.get("valid_windows") or 0) < 5:
        return []
    source_strategy_id = int(source_config.get("strategy_id") or 0)
    source_strategy = get_strategy(source_strategy_id) if source_strategy_id else None
    source_content = source_config.get("selection_strategy")
    if not isinstance(source_content, dict):
        source_content = (source_strategy or {}).get("content")
    if not isinstance(source_content, dict):
        return []
    base_score = deepcopy(source_content.get("score") or {})
    base_factors = deepcopy(base_score.get("factors") or {})
    if not base_factors:
        return []
    selection_regime = {
        "method": "benchmark_dual_trend_v1",
        "short_lookback_sessions": 20,
        "long_lookback_sessions": 60,
        "positive_short_threshold": 0.03,
        "positive_long_threshold": 0.06,
        "negative_short_threshold": -0.03,
        "negative_long_threshold": -0.06,
        "lag_guard": "T_close_only",
    }
    templates = (
        {
            "key": "balanced_rotation",
            "label": "均衡轮动",
            "regime_scores": {
                "S0": {"normalization": "rank_percentile", "factors": {"earnings_yield": .35, "relative_strength_60": .25, "beta_60": .10, "downside_volatility_60": -.10, "max_drawdown_60": .10, "momentum_5": -.05}},
                "S1": base_score,
                "S2": {"normalization": "rank_percentile", "factors": {"earnings_yield": .45, "downside_volatility_60": -.25, "max_drawdown_60": .25, "momentum_5": -.10}},
                "S3": {"normalization": "rank_percentile", "factors": {"earnings_yield": .30, "relative_strength_60": .25, "beta_60": .15, "downside_volatility_60": -.05, "max_drawdown_60": .10, "momentum_5": -.05}},
            },
            "hypothesis": "上涨和修复状态加入中期相对强度与适度Beta参与，收缩状态加强低下行波动与小回撤，始终保持V4仓位与交易约束。",
        },
        {
            "key": "bull_participation",
            "label": "上涨参与",
            "regime_scores": {
                "S0": {"normalization": "rank_percentile", "factors": {"earnings_yield": .25, "relative_strength_60": .30, "beta_60": .20, "downside_volatility_60": -.05, "max_drawdown_60": .10, "momentum_5": -.05}},
                "S1": base_score,
                "S2": base_score,
                "S3": {"normalization": "rank_percentile", "factors": {"earnings_yield": .25, "relative_strength_60": .30, "beta_60": .20, "downside_volatility_60": -.05, "max_drawdown_60": .10, "momentum_5": -.05}},
            },
            "hypothesis": "只在20/60日双趋势确认上涨或修复时提高相对强度和Beta权重，其余状态完全保留V4，专门修复第4个强上涨窗口的参与不足。",
        },
        {
            "key": "recovery_only",
            "label": "修复确认",
            "regime_scores": {
                "S0": base_score,
                "S1": base_score,
                "S2": base_score,
                "S3": {"normalization": "rank_percentile", "factors": {"earnings_yield": .30, "relative_strength_60": .30, "beta_60": .15, "downside_volatility_60": -.05, "max_drawdown_60": .10, "momentum_5": -.05}},
            },
            "hypothesis": "最小变更假设：仅在双趋势从收缩转正的修复状态加入相对强度与Beta，降低因子来回切换和样本内过拟合。",
        },
    )
    research_sources = [
        {"doi": "10.1016/j.iref.2017.04.003", "finding": "中国股市动量收益与市场状态和动态相关，不应将动量作为全状态静态权重"},
        {"doi": "10.3905/jpm.2019.45.3.013", "finding": "因子近期表现具有延续性，因子轮动应作为组合层而非单股追涨"},
        {"doi": "10.1111/jofi.12021", "finding": "价值与动量溢价跨市场存在且负相关，联合可以改善单一价值组合的状态偏离"},
        {"doi": "10.1016/j.pacfin.2021.101607", "finding": "A股传统过去收益证据较弱，因此只在双趋势确认状态加入相对强度，不替换价值低波核心"},
    ]
    champion = resolve_production_strategy_champion()
    versions = list_model_versions(CHALLENGER_MODEL_KEY, limit=300)
    strategies = list_strategies()
    outcomes: list[dict[str, Any]] = []
    for index, template in enumerate(templates, start=1):
        memory_hypothesis = {
            "template": f"china_value_lowvol_regime_factor_{template['key']}_v1",
            "source_model_version_id": int(source_model_version_id),
            "selection_regime": selection_regime,
            "regime_scores": template["regime_scores"],
        }
        existing_version = next(
            (version for version in versions if ((version.get("config") or {}).get("experiment_memory") or {}).get("hypothesis") == memory_hypothesis),
            None,
        )
        if existing_version:
            config = existing_version.get("config") or {}
            strategy = get_strategy(int(config.get("strategy_id") or 0))
            outcomes.append({
                "strategy_id": int(config.get("strategy_id") or 0), "model_version_id": int(existing_version["id"]),
                "name": str((strategy or {}).get("name") or existing_version.get("version") or ""),
                "version": str(existing_version.get("version") or ""),
                "status": str((existing_version.get("validation") or {}).get("status") or existing_version.get("status") or ""),
                "generated_by": "automatic_regime_factor_competition",
            })
            continue
        if find_rejected_hypothesis("strategy", memory_hypothesis):
            continue
        content = deepcopy(source_content)
        content["schema_version"] = f"alis_challenger_value_lowvol_regime_factor_v6_{template['key']}"
        content.pop("dynamic_market_exposure", None)
        content["selection_regime"] = deepcopy(selection_regime)
        content["regime_scores"] = deepcopy(template["regime_scores"])
        controls = content.get("challenger_controls") if isinstance(content.get("challenger_controls"), dict) else {}
        content["challenger_controls"] = {**controls, "rationale": [*list(controls.get("rationale") or []), str(template["hypothesis"]) ]}
        evidence_contract = {
            "schema_version": "alis_regime_factor_candidate_evidence_v1",
            "source_model_version_id": int(source_model_version_id),
            "source_model_version": str(source.get("version") or ""),
            "source_strategy_id": source_strategy_id,
            "source_formal_oos": deepcopy(formal),
            "hypothesis_key": template["key"], "hypothesis": template["hypothesis"],
            "research_sources": research_sources,
            "changed_fields": [
                {"path": "selection_regime", "from": source_content.get("selection_regime"), "to": selection_regime},
                {"path": "regime_scores", "from": source_content.get("regime_scores"), "to": template["regime_scores"]},
            ],
            "non_changes": [
                "full_investment_and_portfolio_optimizer", "positive_earnings_and_small_cap_exclusion",
                "weekly_rank_buffer_and_turnover_controls", "transaction_cost_point_in_time_formal_oos_and_shadow_gates",
            ],
            "experiment_memory": {"target": "strategy", "hypothesis": memory_hypothesis},
        }
        name = f"{REGIME_FACTOR_NAME_PREFIX} V6{chr(64 + index)}-{template['label']}"
        existing_strategy = next((item for item in strategies if item.get("name") == name), None)
        if not existing_strategy:
            _ensure_strategy_sequence()
        strategy = update_strategy(int(existing_strategy["id"]), name, "formula", content) if existing_strategy else create_strategy(name, "formula", content)
        version_id = create_model_version(
            model_key=CHALLENGER_MODEL_KEY,
            version=f"alis-challenger-value-lowvol-regime-factor-v6-{template['key']}-{_stable_evidence_hash(memory_hypothesis)[:12]}",
            status="candidate", artifact_path=f"builtin://alis/challenger/value-lowvol-regime-factor-v6/{template['key']}",
            config={
                "role": "challenger", "generation": "automatic_regime_factor_competition", "generation_index": 6,
                "lineage": {
                    "root_model_version_id": int(source_model_version_id),
                    "parent_model_version_id": int(source_model_version_id),
                    "generation_index": 6,
                    "hypothesis_family": "regime_factor",
                    "applied_mutations": [str(template["key"])],
                },
                "diagnosis": "thin_excess_and_bull_window_participation_gap",
                "strategy_id": int(strategy["id"]), "champion_reference": champion["champion_version"],
                "selection_strategy": content,
                "factor_bindings": _active_replay_factor_bindings(content),
                "evidence_contract": evidence_contract, "experiment_memory": evidence_contract["experiment_memory"],
            },
            metrics={}, validation={"status": "pending_oos", "production_eligible": False, "next_gate": "quick_diagnostic"}, is_active=False,
        )
        version = next(item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=300) if int(item["id"]) == int(version_id))
        upsert_experiment_memory(
            target="strategy", hypothesis=memory_hypothesis, status="observing",
            source_model_version_id=int(version_id), outcome_stage="candidate_created",
            evidence={"candidate_version": str(version.get("version") or ""), "source_model_version_id": int(source_model_version_id)},
        )
        outcomes.append({
            "strategy_id": int(strategy["id"]), "model_version_id": int(version["id"]), "name": name,
            "version": str(version.get("version") or ""), "status": "pending_oos",
            "generated_by": "automatic_regime_factor_competition",
        })
    return outcomes


def ensure_stabilized_regime_successor(*, source_model_version_id: int) -> dict[str, Any] | None:
    """Create at most one V7 after the full V6 family has produced formal evidence."""
    source = get_model_version(int(source_model_version_id)) or {}
    source_config = source.get("config") if isinstance(source.get("config"), dict) else {}
    source_strategy_id = int(source_config.get("strategy_id") or 0)
    source_strategy = get_strategy(source_strategy_id) if source_strategy_id else None
    source_content = source_config.get("selection_strategy")
    if not isinstance(source_content, dict):
        source_content = (source_strategy or {}).get("content")
    if not isinstance(source_content, dict):
        return None
    versions = list_model_versions(CHALLENGER_MODEL_KEY, limit=300)
    family = [
        version for version in versions
        if str((version.get("config") or {}).get("generation") or "") == "automatic_regime_factor_competition"
        and int((((version.get("config") or {}).get("evidence_contract") or {}).get("source_model_version_id") or 0)) == int(source_model_version_id)
    ]
    if len(family) < 3:
        return None
    terminal_statuses = {"quick_diagnostic_failed", "formal_oos_failed", "formal_oos_rejected", "rejected", "shadow_rejected", "rolled_back"}
    if any(str((version.get("validation") or {}).get("status") or "") not in terminal_statuses for version in family):
        return None
    if any(str((((version.get("metrics") or {}).get("formal_oos") or {}).get("status") or "")) == "passed" for version in family):
        return None
    evaluated = [
        version for version in family
        if isinstance((version.get("metrics") or {}).get("formal_oos"), dict)
    ]
    if not evaluated:
        return None
    best = max(
        evaluated,
        key=lambda version: float((((version.get("metrics") or {}).get("formal_oos") or {}).get("aggregate_excess_return") or -999.0)),
    )
    best_formal = ((best.get("metrics") or {}).get("formal_oos") or {})
    best_excess = float(best_formal.get("aggregate_excess_return") or 0.0)
    best_outperform = int(best_formal.get("outperform_windows") or 0)
    if best_excess < -0.15 or best_outperform < 2:
        return None
    blend_ratio = 0.50 if best_excess >= -0.05 and best_outperform >= 3 else 0.25
    best_config = best.get("config") if isinstance(best.get("config"), dict) else {}
    best_content = best_config.get("selection_strategy") if isinstance(best_config.get("selection_strategy"), dict) else {}
    best_regime_scores = best_content.get("regime_scores") if isinstance(best_content.get("regime_scores"), dict) else {}
    base_score = deepcopy(source_content.get("score") or {})

    def blend_score(candidate_score: Any) -> dict[str, Any]:
        candidate_score = candidate_score if isinstance(candidate_score, dict) else base_score
        base_factors = base_score.get("factors") if isinstance(base_score.get("factors"), dict) else {}
        candidate_factors = candidate_score.get("factors") if isinstance(candidate_score.get("factors"), dict) else {}
        factors = {
            factor: (1.0 - blend_ratio) * float(base_factors.get(factor) or 0.0) + blend_ratio * float(candidate_factors.get(factor) or 0.0)
            for factor in set(base_factors) | set(candidate_factors)
        }
        return {"normalization": "rank_percentile", "factors": factors}

    regime_scores = {state: blend_score(best_regime_scores.get(state)) for state in ("S0", "S1", "S2", "S3")}
    selection_regime = deepcopy(best_content.get("selection_regime") or {})
    selection_regime.update({
        "positive_short_threshold": max(0.04, float(selection_regime.get("positive_short_threshold") or 0.03)),
        "positive_long_threshold": max(0.08, float(selection_regime.get("positive_long_threshold") or 0.06)),
        "negative_short_threshold": min(-0.04, float(selection_regime.get("negative_short_threshold") or -0.03)),
        "negative_long_threshold": min(-0.08, float(selection_regime.get("negative_long_threshold") or -0.06)),
        "stabilized_from_v6": int(best["id"]),
    })
    formal_hash = _stable_evidence_hash(best_formal)
    memory_hypothesis = {
        "template": "china_value_lowvol_regime_factor_stabilized_v7",
        "source_model_version_id": int(source_model_version_id),
        "best_v6_model_version_id": int(best["id"]),
        "best_v6_formal_hash": formal_hash,
        "blend_ratio": blend_ratio,
        "selection_regime": selection_regime,
        "regime_scores": regime_scores,
    }
    for version in versions:
        experiment = (version.get("config") or {}).get("experiment_memory") or {}
        if experiment.get("hypothesis") == memory_hypothesis:
            config = version.get("config") or {}
            strategy = get_strategy(int(config.get("strategy_id") or 0))
            return {
                "strategy_id": int(config.get("strategy_id") or 0), "model_version_id": int(version["id"]),
                "name": str((strategy or {}).get("name") or version.get("version") or ""), "version": str(version.get("version") or ""),
                "status": str((version.get("validation") or {}).get("status") or version.get("status") or ""),
                "generated_by": "automatic_stabilized_regime_successor",
            }
    if find_rejected_hypothesis("strategy", memory_hypothesis):
        return None
    content = deepcopy(source_content)
    content["schema_version"] = "alis_challenger_value_lowvol_regime_factor_v7_stabilized"
    content.pop("dynamic_market_exposure", None)
    content["selection_regime"] = selection_regime
    content["regime_scores"] = regime_scores
    controls = content.get("challenger_controls") if isinstance(content.get("challenger_controls"), dict) else {}
    content["challenger_controls"] = {
        **controls,
        "rationale": [
            *list(controls.get("rationale") or []),
            f"V6全家族正式验证后只允许一个稳定化V7；以{blend_ratio:.0%}比例混合最优V6与V4，同时收紧状态触发门。",
        ],
    }
    evidence_contract = {
        "schema_version": "alis_stabilized_regime_successor_evidence_v1",
        "source_model_version_id": int(source_model_version_id),
        "best_v6_model_version_id": int(best["id"]),
        "best_v6_formal_oos": deepcopy(best_formal),
        "best_v6_formal_hash": formal_hash,
        "blend_ratio": blend_ratio,
        "hypothesis": "保留V4已通过的主体，只吸收最优V6的少量状态因子信号并收紧触发门，检验是否能在不增加换手的前提下扩大复合超额。",
        "changed_fields": [
            {"path": "selection_regime", "from": source_content.get("selection_regime"), "to": selection_regime},
            {"path": "regime_scores", "from": source_content.get("regime_scores"), "to": regime_scores},
        ],
        "non_changes": ["full_investment", "weekly_rank_buffer", "turnover_and_weight_limits", "all_validation_and_promotion_gates"],
        "experiment_memory": {"target": "strategy", "hypothesis": memory_hypothesis},
    }
    name = f"{REGIME_FACTOR_NAME_PREFIX} V7-稳定化后继{int(best['id'])}"
    existing_strategy = next((item for item in list_strategies() if item.get("name") == name), None)
    if not existing_strategy:
        _ensure_strategy_sequence()
    strategy = update_strategy(int(existing_strategy["id"]), name, "formula", content) if existing_strategy else create_strategy(name, "formula", content)
    champion = resolve_production_strategy_champion()
    version_id = create_model_version(
        model_key=CHALLENGER_MODEL_KEY,
        version=f"alis-challenger-value-lowvol-regime-factor-v7-{formal_hash[:12]}", status="candidate",
        artifact_path="builtin://alis/challenger/value-lowvol-regime-factor-v7/stabilized",
        config={
            "role": "challenger", "generation": "automatic_stabilized_regime_successor", "generation_index": 7,
            "lineage": {
                "root_model_version_id": int(source_model_version_id),
                "parent_model_version_id": int(best["id"]),
                "generation_index": 7,
                "hypothesis_family": "stabilized_regime_blend",
                "applied_mutations": ["stabilized_regime_blend"],
            },
            "diagnosis": "v6_family_failed_bounded_stabilization", "strategy_id": int(strategy["id"]),
            "champion_reference": champion["champion_version"], "selection_strategy": content,
            "factor_bindings": _active_replay_factor_bindings(content),
            "evidence_contract": evidence_contract, "experiment_memory": evidence_contract["experiment_memory"],
        },
        metrics={}, validation={"status": "pending_oos", "production_eligible": False, "next_gate": "quick_diagnostic"}, is_active=False,
    )
    version = next(item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=300) if int(item["id"]) == int(version_id))
    upsert_experiment_memory(
        target="strategy", hypothesis=memory_hypothesis, status="observing", source_model_version_id=int(version_id),
        outcome_stage="candidate_created", evidence={"candidate_version": str(version.get("version") or ""), "best_v6_model_version_id": int(best["id"])},
    )
    return {
        "strategy_id": int(strategy["id"]), "model_version_id": int(version["id"]), "name": name,
        "version": str(version.get("version") or ""), "status": "pending_oos",
        "generated_by": "automatic_stabilized_regime_successor",
    }


def _lineage_generation(version: dict[str, Any], root_model_version_id: int) -> int:
    config = version.get("config") if isinstance(version.get("config"), dict) else {}
    lineage = config.get("lineage") if isinstance(config.get("lineage"), dict) else {}
    if int(lineage.get("root_model_version_id") or 0) == int(root_model_version_id):
        return int(lineage.get("generation_index") or config.get("generation_index") or 0)
    evidence = config.get("evidence_contract") if isinstance(config.get("evidence_contract"), dict) else {}
    if int(evidence.get("source_model_version_id") or 0) != int(root_model_version_id):
        return 0
    generation = str(config.get("generation") or "")
    return {
        "automatic_regime_competition": 5,
        "automatic_regime_factor_competition": 6,
        "automatic_stabilized_regime_successor": 7,
    }.get(generation, int(config.get("generation_index") or 0))


def _blend_score_config(base_score: dict[str, Any], tilt: dict[str, float], ratio: float) -> dict[str, Any]:
    base_factors = base_score.get("factors") if isinstance(base_score.get("factors"), dict) else {}
    factors = {
        factor: float(base_factors.get(factor) or 0.0) + float(ratio) * float(tilt.get(factor) or 0.0)
        for factor in set(base_factors) | set(tilt)
    }
    return {"normalization": str(base_score.get("normalization") or "rank_percentile"), "factors": factors}


def ensure_autonomous_lineage_successor(*, root_model_version_id: int) -> dict[str, Any] | None:
    """Generate one evidence-driven successor without depending on a display version number."""
    root = get_model_version(int(root_model_version_id)) or {}
    root_config = root.get("config") if isinstance(root.get("config"), dict) else {}
    root_strategy_id = int(root_config.get("strategy_id") or 0)
    root_strategy = get_strategy(root_strategy_id) if root_strategy_id else None
    root_content = root_config.get("selection_strategy")
    if not isinstance(root_content, dict):
        root_content = (root_strategy or {}).get("content")
    if not isinstance(root_content, dict):
        return None
    versions = list_model_versions(CHALLENGER_MODEL_KEY, limit=500)
    lineage_versions = [
        version for version in versions
        if _lineage_generation(version, int(root_model_version_id)) > 0
    ]
    if not lineage_versions:
        return None
    if len(lineage_versions) >= MAX_AUTONOMOUS_LINEAGE_TRIALS_PER_ROOT:
        return None
    active_statuses = {
        "pending_oos", "quick_diagnostic_passed", "formal_oos_passed",
        "shadow_observation", "shadow_passed", "promoted_cooling", "promoted_stable",
    }
    if any(str((version.get("validation") or {}).get("status") or "") in active_statuses for version in lineage_versions):
        return None
    formal_versions = [
        version for version in lineage_versions
        if isinstance((version.get("metrics") or {}).get("formal_oos"), dict)
    ]
    if not formal_versions:
        return None
    highest_generation = max(_lineage_generation(version, int(root_model_version_id)) for version in lineage_versions)
    parent_pool = [
        version for version in formal_versions
        if _lineage_generation(version, int(root_model_version_id)) == highest_generation
    ] or formal_versions
    parent = max(
        parent_pool,
        key=lambda version: float((((version.get("metrics") or {}).get("formal_oos") or {}).get("aggregate_excess_return") or -999.0)),
    )
    parent_formal = ((parent.get("metrics") or {}).get("formal_oos") or {})
    attribution = parent_formal.get("failure_attribution") if isinstance(parent_formal.get("failure_attribution"), dict) else {}
    primary_cause = str(attribution.get("primary_cause") or "unknown")
    applied_families = {
        str(((version.get("config") or {}).get("lineage") or {}).get("hypothesis_family") or "")
        for version in lineage_versions
    }
    family_order = {
        "transaction_costs": ["turnover_hysteresis", "rank_buffer_expansion", "stabilized_factor_tilt"],
        "execution_constraints": ["capacity_diversification", "rank_buffer_expansion", "turnover_hysteresis"],
        "market_exposure": ["bull_participation_factor", "state_factor_smoothing", "stabilized_factor_tilt"],
        "selection_or_allocation_residual": ["stabilized_factor_tilt", "defensive_quality_tilt", "rank_buffer_expansion"],
        "unknown": ["stabilized_factor_tilt", "turnover_hysteresis", "capacity_diversification"],
    }
    mutation_family = next(
        (family for family in family_order.get(primary_cause, family_order["unknown"]) if family not in applied_families),
        None,
    )
    if not mutation_family:
        return None
    from app.services.strategy_research import search_strategy_research

    research = search_strategy_research(primary_cause)
    parent_config = parent.get("config") if isinstance(parent.get("config"), dict) else {}
    parent_content = parent_config.get("selection_strategy")
    if not isinstance(parent_content, dict):
        parent_strategy = get_strategy(int(parent_config.get("strategy_id") or 0))
        parent_content = (parent_strategy or {}).get("content")
    if not isinstance(parent_content, dict):
        return None
    content = deepcopy(parent_content)
    content.pop("dynamic_market_exposure", None)
    controls = content.get("challenger_controls") if isinstance(content.get("challenger_controls"), dict) else {}
    rebalance = content.get("rebalance") if isinstance(content.get("rebalance"), dict) else {}
    base_score = content.get("score") if isinstance(content.get("score"), dict) else deepcopy(root_content.get("score") or {})
    changed_fields: list[dict[str, Any]] = []
    if mutation_family == "turnover_hysteresis":
        next_controls = {
            **controls,
            "max_rebalance_turnover": max(0.10, float(controls.get("max_rebalance_turnover") or 0.20) * 0.80),
            "min_weight_change": min(0.04, float(controls.get("min_weight_change") or 0.02) + 0.005),
            "buy_rank_max": max(1, int(round(float(controls.get("buy_rank_max") or rebalance.get("top_n") or 30) * 0.90))),
            "hold_rank_max": max(1, int(round(float(controls.get("hold_rank_max") or 36) * 1.15))),
        }
        changed_fields.append({"path": "challenger_controls", "from": controls, "to": next_controls})
        content["challenger_controls"] = next_controls
    elif mutation_family in {"rank_buffer_expansion", "capacity_diversification"}:
        old_top_n = int(rebalance.get("top_n") or 30)
        next_top_n = min(60, old_top_n + (10 if mutation_family == "capacity_diversification" else 5))
        next_rebalance = {**rebalance, "top_n": next_top_n, "freq": "weekly"}
        next_controls = {
            **controls,
            "max_single_weight": min(float(controls.get("max_single_weight") or .04), max(.02, 1.20 / next_top_n)),
            "buy_rank_max": max(1, int(round(next_top_n * .80))),
            "hold_rank_max": max(next_top_n, int(round(next_top_n * 1.20))),
        }
        changed_fields.extend([
            {"path": "rebalance", "from": rebalance, "to": next_rebalance},
            {"path": "challenger_controls", "from": controls, "to": next_controls},
        ])
        content["rebalance"] = next_rebalance
        content["challenger_controls"] = next_controls
    else:
        selection_regime = content.get("selection_regime") if isinstance(content.get("selection_regime"), dict) else {
            "method": "benchmark_dual_trend_v1", "short_lookback_sessions": 20, "long_lookback_sessions": 60,
            "positive_short_threshold": .04, "positive_long_threshold": .08,
            "negative_short_threshold": -.04, "negative_long_threshold": -.08, "lag_guard": "T_close_only",
        }
        regime_scores = content.get("regime_scores") if isinstance(content.get("regime_scores"), dict) else {
            state: deepcopy(base_score) for state in ("S0", "S1", "S2", "S3")
        }
        if mutation_family == "bull_participation_factor":
            tilt = {"relative_strength_60": .10, "beta_60": .05, "earnings_yield": -.05}
            for state in ("S0", "S3"):
                regime_scores[state] = _blend_score_config(regime_scores.get(state) or base_score, tilt, 0.50)
        elif mutation_family == "defensive_quality_tilt":
            tilt = {"earnings_yield": .08, "downside_volatility_60": -.08, "max_drawdown_60": .05, "beta_60": -.03}
            regime_scores["S2"] = _blend_score_config(regime_scores.get("S2") or base_score, tilt, 0.50)
        elif mutation_family == "state_factor_smoothing":
            selection_regime = {
                **selection_regime,
                "positive_short_threshold": max(.05, float(selection_regime.get("positive_short_threshold") or .03)),
                "positive_long_threshold": max(.10, float(selection_regime.get("positive_long_threshold") or .06)),
                "negative_short_threshold": min(-.05, float(selection_regime.get("negative_short_threshold") or -.03)),
                "negative_long_threshold": min(-.10, float(selection_regime.get("negative_long_threshold") or -.06)),
            }
        else:  # stabilized_factor_tilt
            root_score = root_content.get("score") if isinstance(root_content.get("score"), dict) else base_score
            regime_scores = {
                state: _blend_score_config(root_score, {
                    factor: float(((regime_scores.get(state) or {}).get("factors") or {}).get(factor) or 0.0)
                    - float((root_score.get("factors") or {}).get(factor) or 0.0)
                    for factor in set((root_score.get("factors") or {})) | set(((regime_scores.get(state) or {}).get("factors") or {}))
                }, 0.25)
                for state in ("S0", "S1", "S2", "S3")
            }
        changed_fields.extend([
            {"path": "selection_regime", "from": content.get("selection_regime"), "to": selection_regime},
            {"path": "regime_scores", "from": content.get("regime_scores"), "to": regime_scores},
        ])
        content["selection_regime"] = selection_regime
        content["regime_scores"] = regime_scores
    next_generation = highest_generation + 1
    content["schema_version"] = f"alis_autonomous_strategy_lineage_g{next_generation}_{mutation_family}"
    controls = content.get("challenger_controls") if isinstance(content.get("challenger_controls"), dict) else {}
    content["challenger_controls"] = {
        **controls,
        "rationale": [
            *list(controls.get("rationale") or []),
            f"谱系第{next_generation}代根据父版本{int(parent['id'])}的{primary_cause}归因，自动选择尚未试验的{mutation_family}白名单变换。",
        ],
    }
    parent_lineage = parent_config.get("lineage") if isinstance(parent_config.get("lineage"), dict) else {}
    applied_mutations = list(parent_lineage.get("applied_mutations") or [])
    if mutation_family not in applied_mutations:
        applied_mutations.append(mutation_family)
    hypothesis = {
        "template": "alis_autonomous_strategy_lineage_successor_v1",
        "root_model_version_id": int(root_model_version_id),
        "parent_model_version_id": int(parent["id"]),
        "generation_index": next_generation,
        "mutation_family": mutation_family,
        "parent_formal_hash": _stable_evidence_hash(parent_formal),
        "research_signature": str(research.get("research_signature") or ""),
        "changed_fields": changed_fields,
    }
    if find_rejected_hypothesis("strategy", hypothesis):
        return None
    for version in versions:
        experiment = (version.get("config") or {}).get("experiment_memory") or {}
        if experiment.get("hypothesis") == hypothesis:
            config = version.get("config") or {}
            strategy = get_strategy(int(config.get("strategy_id") or 0))
            return {
                "strategy_id": int(config.get("strategy_id") or 0), "model_version_id": int(version["id"]),
                "name": str((strategy or {}).get("name") or version.get("version") or ""), "version": str(version.get("version") or ""),
                "status": str((version.get("validation") or {}).get("status") or version.get("status") or ""),
                "generated_by": "automatic_strategy_lineage",
            }
    evidence_contract = {
        "schema_version": "alis_autonomous_strategy_lineage_evidence_v1",
        "root_model_version_id": int(root_model_version_id), "parent_model_version_id": int(parent["id"]),
        "generation_index": next_generation, "mutation_family": mutation_family,
        "parent_formal_oos": deepcopy(parent_formal), "failure_attribution": deepcopy(attribution),
        "network_research": research, "changed_fields": changed_fields,
        "non_changes": ["point_in_time_contract", "transaction_cost_contract", "formal_oos_shadow_and_promotion_gates"],
        "experiment_memory": {"target": "strategy", "hypothesis": hypothesis},
    }
    name = f"ALIS Challenger 自治谱系 G{next_generation}-{mutation_family}-父{int(parent['id'])}"
    existing_strategy = next((item for item in list_strategies() if item.get("name") == name), None)
    if not existing_strategy:
        _ensure_strategy_sequence()
    strategy = update_strategy(int(existing_strategy["id"]), name, "formula", content) if existing_strategy else create_strategy(name, "formula", content)
    champion = resolve_production_strategy_champion()
    version_id = create_model_version(
        model_key=CHALLENGER_MODEL_KEY,
        version=f"alis-autonomous-lineage-g{next_generation}-{mutation_family}-{_stable_evidence_hash(hypothesis)[:12]}",
        status="candidate", artifact_path=f"builtin://alis/challenger/autonomous-lineage/{mutation_family}",
        config={
            "role": "challenger", "generation": "automatic_strategy_lineage", "generation_index": next_generation,
            "diagnosis": primary_cause, "strategy_id": int(strategy["id"]), "champion_reference": champion["champion_version"],
            "selection_strategy": content,
            "factor_bindings": _active_replay_factor_bindings(content),
            "lineage": {
                "root_model_version_id": int(root_model_version_id), "parent_model_version_id": int(parent["id"]),
                "generation_index": next_generation, "hypothesis_family": mutation_family,
                "applied_mutations": applied_mutations,
            },
            "evidence_contract": evidence_contract, "experiment_memory": evidence_contract["experiment_memory"],
        },
        metrics={}, validation={"status": "pending_oos", "production_eligible": False, "next_gate": "quick_diagnostic"}, is_active=False,
    )
    version = next(item for item in list_model_versions(CHALLENGER_MODEL_KEY, limit=500) if int(item["id"]) == int(version_id))
    upsert_experiment_memory(
        target="strategy", hypothesis=hypothesis, status="observing", source_model_version_id=int(version_id),
        outcome_stage="candidate_created", evidence={
            "candidate_version": str(version.get("version") or ""), "parent_model_version_id": int(parent["id"]),
            "network_research_signature": research.get("research_signature"),
        },
    )
    return {
        "strategy_id": int(strategy["id"]), "model_version_id": int(version["id"]), "name": name,
        "version": str(version.get("version") or ""), "status": "pending_oos", "generated_by": "automatic_strategy_lineage",
    }


def ensure_autonomous_strategy_candidates() -> dict[str, Any]:
    """Seed and advance every strategy lineage; the scheduler calls this idempotently."""
    versions = list_model_versions(CHALLENGER_MODEL_KEY, limit=500)
    root = next(
        (
            version for version in versions
            if str((((version.get("metrics") or {}).get("formal_oos") or {}).get("status") or "")) == "passed"
            and isinstance((version.get("config") or {}).get("selection_strategy"), dict)
        ),
        None,
    )
    if not root:
        return {"status": "waiting_for_formal_root", "root_model_version_id": None, "generated": []}
    root_id = int(root["id"])
    generated: list[dict[str, Any]] = []
    generated.extend(ensure_regime_aware_selection_challengers(source_model_version_id=root_id))
    generated.extend(ensure_regime_factor_selection_challengers(source_model_version_id=root_id))
    stabilized = ensure_stabilized_regime_successor(source_model_version_id=root_id)
    if stabilized:
        generated.append(stabilized)
    successor = ensure_autonomous_lineage_successor(root_model_version_id=root_id)
    if successor:
        generated.append(successor)
    return {
        "schema_version": "alis_autonomous_strategy_lineage_cycle_v1",
        "status": "ready",
        "root_model_version_id": root_id,
        "generated": generated,
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
    if generation > min(MAX_ADAPTIVE_PARAMETER_TRIALS, len(templates)):
        config = latest.get("config") if latest and isinstance(latest.get("config"), dict) else {}
        strategy_id = int(config.get("strategy_id") or 0)
        strategy = get_strategy(strategy_id) if strategy_id else None
        return {
            "strategy_id": strategy_id,
            "model_version_id": int((latest or {}).get("id") or 0),
            "name": str((strategy or {}).get("name") or f"{ADAPTIVE_NAME_PREFIX} V2.4"),
            "version": str((latest or {}).get("version") or ""),
            "status": "search_budget_exhausted",
            "generated_by": "automatic_parameter_search",
            "search_budget": {
                "trial_count": len(adaptive),
                "maximum_trials": MAX_ADAPTIVE_PARAMETER_TRIALS,
            },
        }
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
            "factor_bindings": _active_replay_factor_bindings(content),
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
                "factor_bindings": _active_replay_factor_bindings(content),
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
                "factor_bindings": _active_replay_factor_bindings(base_content),
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
