from __future__ import annotations

import json
import hashlib
import os
import pickle
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.metrics import f1_score

from app.db.store import (
    _conn as business_conn,
    create_model_train_run,
    create_model_version,
    get_active_model_version,
    get_model_train_run,
    get_model_version,
    list_model_train_runs,
    list_active_model_train_runs,
    list_model_versions,
    set_active_model_version,
    update_model_train_run,
    update_model_version_metrics,
    update_model_version_status,
    update_model_version_validation,
)
from app.services.model_registry import models_root
from app.services.model_artifacts import resolve_model_artifact_path

_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
from app.services.model_evaluation_contracts import (
    SCHEMA_VERSION as MODEL_EVALUATION_CONTRACT,
    evaluate_capital_multitask,
    evaluate_event_impact,
    evaluate_learning_to_rank,
    evaluate_narrative_transition,
    event_response_group_keys,
    event_impact_target_labels,
    fit_event_probability_baseline,
    apply_event_probability_baseline,
    fit_event_quantile_baseline,
    apply_event_quantile_baseline,
    evaluation_contract,
    supported_model_contracts,
)
from app.services.model_evidence_contract import MODEL_OUTPUT_CONTRACTS
from app.services.training_protocol import training_protocol_contract, validate_training_run_manifest
from app.services.event_impact_features import (
    ANALYST_CONSENSUS_CONTRACT,
    EVENT_FEATURE_CONTRACT,
    EVENT_TYPE_GROUPS,
    STRUCTURED_DISCLOSURE_APIS,
    build_analyst_consensus_index,
    disclosure_expectation_gap,
    materialize_event_features,
    merge_event_candidates,
    payload_dict,
    point_in_time_analyst_consensus,
    report_period_key,
    structured_event_candidate,
)
from app.services.event_feature_contribution import (
    EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT,
    aggregate_event_feature_permutation_audit,
    fold_event_feature_permutation_audit,
)
from app.services.event_conditional_failure import (
    EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT,
    aggregate_event_conditional_failure_audit,
    fold_event_conditional_failure_audit,
)
from app.services.narrative_lifecycle_contract import (
    ALLOWED_TRANSITIONS as NARRATIVE_V3_ALLOWED_TRANSITIONS,
    CONFIRMATION_SESSIONS as NARRATIVE_V3_CONFIRMATION_SESSIONS,
    MIN_STATE_SESSIONS as NARRATIVE_V3_MIN_STATE_SESSIONS,
    SCHEMA_VERSION as NARRATIVE_V3_SCHEMA_VERSION,
    STATES as NARRATIVE_V3_STATES,
    candidate_state as narrative_candidate_state,
    transition_state as narrative_transition_state,
)


LIGHTGBM_PARAMS = {
    "learning_rate": 0.03,
    "num_leaves": 31,
    "max_depth": 6,
    "n_estimators": 2000,
    "min_child_samples": 100,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "reg_alpha": 0.5,
    "reg_lambda": 2.0,
    "max_bin": 255,
    "random_state": 20260719,
    "verbosity": -1,
    "early_stopping_rounds": 100,
}
EVENT_PRIMARY_SOURCES = ("announcement", "structured_disclosure", "major_news")
EVENT_STRUCTURED_SOURCES = (*EVENT_PRIMARY_SOURCES, "top_list")
EVENT_IMPACT_CONTRACT = "alis_conditional_event_impact_assessment_v4"
EVENT_TARGET_CONTRACT = "conditional_market_adjusted_event_impact_v3"
EVENT_TRANSACTION_COST_RATE = 0.004
EVENT_DISTRIBUTION_LABEL_AUDIT_CONTRACT = (
    "alis_event_family_market_state_label_stability_audit_v2"
)
EVENT_RESEARCH_COHORT_CONTRACT = (
    "alis_event_formal_oos_research_cohort_governance_v1"
)
EVENT_AUDIT_MINIMUM_GROUP_SUPPORT = 20

TRAINING_PROTOCOL = {
    "schema_version": "alis_task_native_plan_model_training_protocol_v1",
    "minimum_train_sessions": 756,
    "validation_sessions": 126,
    "test_sessions": 126,
    "roll_step_sessions": 126,
    "purge_sessions": 60,
    "embargo_sessions": 5,
    "walk_forward_windows": 5,
}
EVENT_TRAINING_PROTOCOL = {
    "schema_version": "alis_event_impact_training_protocol_v4",
    "minimum_train_sessions": 200,
    "validation_sessions": 10,
    "test_sessions": 10,
    "roll_step_sessions": 10,
    "purge_sessions": 5,
    "embargo_sessions": 5,
    "walk_forward_windows": 5,
    "calendar_unit": "event_observation_trade_date",
    "event_sources": list(EVENT_PRIMARY_SOURCES),
    "market_response_sources": ["top_list"],
}
NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT = (
    "supervised_narrative_lifecycle_rank_and_exit_v2"
)
NARRATIVE_TASK_EVALUATION_CONTRACT = (
    "narrative_two_stage_lifecycle_task_native_v3"
)
TRAINING_SYMBOL_SAMPLE_CONTRACT = "historical_anchor_sha256_symbol_sample_v1"
TRAINING_FAILURE_RECOVERY_CONTRACT = "alis_plan_model_training_failure_recovery_v1"
TRAINING_FAILURE_MAX_RETRIES = 2
CONTROLLED_STRATEGY_AB_CONTRACT = "alis_controlled_model_strategy_ab_v4"
CONTROLLED_STRATEGY_AB_COMPARISON_CONTRACT = (
    "point_in_time_fold_model_vs_disabled_ablation_cost_after_v2"
)
CONTROLLED_STRATEGY_AB_COST_CONTRACT = "alis_shared_a_share_replay_cost_contract_v1"
CONTROLLED_STRATEGY_AB_REQUIRED_GATES = (
    "five_complete_windows",
    "all_replays_completed",
    "paired_dates_complete",
    "candidate_fold_model_actually_consumed",
    "candidate_fold_model_actually_effective",
    "shared_fee_slippage_contract_frozen",
    "strategy_returns_are_cost_after",
    "majority_windows_improved",
    "mean_return_delta_positive",
    "paired_daily_bootstrap_lower_positive",
    "worst_drawdown_not_materially_worse",
    "turnover_not_explosive",
)


class PlanModelTrainingDataError(RuntimeError):
    """Typed training-data failure that must not be degraded into empty rows."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


def _deterministic_training_symbol_sample(
    symbols: list[str],
    *,
    limit: int,
) -> list[str]:
    """Select one stable cross-history universe without using database hash scans."""

    unique = {
        str(symbol).strip()
        for symbol in symbols
        if str(symbol).strip()
    }
    return sorted(
        unique,
        key=lambda symbol: (hashlib.sha256(symbol.encode("utf-8")).hexdigest(), symbol),
    )[:max(1, int(limit))]


def _training_symbol_sample(
    conn: Any,
    trade_dates: list[str],
    *,
    limit: int,
    anchor_stride: int = 20,
) -> tuple[list[str], dict[str, Any]]:
    dates = sorted({str(value) for value in trade_dates if str(value)})
    stride = max(1, int(anchor_stride))
    anchor_dates = dates[::stride]
    if dates and dates[-1] not in anchor_dates:
        anchor_dates.append(dates[-1])
    if not anchor_dates:
        raise PlanModelTrainingDataError(
            "training_symbol_anchor_missing",
            "训练日历没有可用于历史证券抽样的锚点",
        )
    placeholders = ",".join("?" for _ in anchor_dates)
    try:
        rows = conn.execute(
            f"""SELECT DISTINCT ts_code
                FROM dc_stock_daily_snapshot
                WHERE trade_date IN ({placeholders})
                  AND ts_code IS NOT NULL AND ts_code<>''""",
            tuple(anchor_dates),
        ).fetchall()
    except Exception as exc:
        raise PlanModelTrainingDataError(
            "training_symbol_anchor_query_failed",
            str(exc)[:300],
        ) from exc
    eligible = [str(dict(row).get("ts_code") or "") for row in rows]
    selected = _deterministic_training_symbol_sample(eligible, limit=limit)
    if not selected:
        raise PlanModelTrainingDataError(
            "training_symbol_sample_empty",
            "历史锚点截面没有可用证券",
        )
    evidence = {
        "contract": TRAINING_SYMBOL_SAMPLE_CONTRACT,
        "anchor_stride_sessions": stride,
        "anchor_date_count": len(anchor_dates),
        "anchor_start": anchor_dates[0],
        "anchor_end": anchor_dates[-1],
        "eligible_symbol_count": len(set(eligible)),
        "selected_symbol_count": len(selected),
        "selected_symbols_sha256": hashlib.sha256(
            "\n".join(selected).encode("utf-8")
        ).hexdigest(),
        "survivorship_policy": "union_of_periodic_historical_snapshots_before_deterministic_selection",
    }
    return selected, evidence


def _event_training_symbol_sample(
    conn: Any,
    *,
    start_date: str,
    end_date: str,
    cutoff: str,
    limit: int,
) -> tuple[list[str], dict[str, Any]]:
    """Select event-bearing symbols while preserving point-in-time source rules."""

    symbol_dates: dict[str, set[str]] = {}
    structured_rows: list[Any] = []
    try:
        announcement_rows = conn.execute(
            """SELECT ts_code,publish_time,available_time
               FROM dc_announcement_raw
               WHERE ts_code IS NOT NULL AND ts_code<>''
                 AND available_time<=?
                 AND REPLACE(SUBSTRING(publish_time,1,10),'-','')>=?
                 AND REPLACE(SUBSTRING(publish_time,1,10),'-','')<=?""",
            (cutoff, start_date, end_date),
        ).fetchall()
        for api_name in STRUCTURED_DISCLOSURE_APIS:
            structured_rows.extend(
                conn.execute(
                    """SELECT api_name,ts_code,ann_date,available_time,payload_json
                       FROM dc_tushare_raw_records
                       WHERE api_name=?
                         AND ts_code IS NOT NULL AND ts_code<>''
                         AND available_time<=?
                         AND available_time>=?""",
                    (
                        api_name,
                        cutoff,
                        f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
                    ),
                ).fetchall()
            )
        news_rows = conn.execute(
            """SELECT available_time,payload_json
               FROM dc_tushare_raw_records
               WHERE api_name='major_news'
                 AND available_time<=?
                 AND available_time>=?
               ORDER BY available_time""",
            (
                cutoff,
                f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
            ),
        ).fetchall()
    except Exception as exc:
        raise PlanModelTrainingDataError(
            "event_symbol_sample_query_failed",
            str(exc)[:300],
        ) from exc
    for row in announcement_rows:
        item = dict(row)
        symbol = str(item.get("ts_code") or "")
        event_date = str(item.get("publish_time") or "")[:10].replace("-", "")
        if symbol and event_date:
            symbol_dates.setdefault(symbol, set()).add(event_date)
    for row in structured_rows:
        item = dict(row)
        payload = _payload_dict(item.get("payload_json"))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        event_date = str(
            item.get("ann_date")
            or payload.get("ann_date")
            or item.get("available_time")
            or ""
        )[:10].replace("-", "")
        if symbol and event_date:
            symbol_dates.setdefault(symbol, set()).add(event_date)
    if news_rows:
        from app.services.news_theme_cluster import _match_companies

        for row in news_rows:
            item = dict(row)
            payload = _payload_dict(item.get("payload_json"))
            text = f"{payload.get('title') or ''} {payload.get('content') or ''}".strip()
            event_date = str(
                payload.get("pub_time") or item.get("available_time") or ""
            )[:10].replace("-", "")
            if not text or not event_date:
                continue
            for company in _match_companies(text):
                symbol = str(company.get("ts_code") or "")
                if symbol:
                    symbol_dates.setdefault(symbol, set()).add(event_date)
    selected = sorted(
        symbol_dates,
        key=lambda symbol: (
            -len(symbol_dates[symbol]),
            hashlib.sha256(symbol.encode("utf-8")).hexdigest(),
            symbol,
        ),
    )[:max(1, int(limit))]
    if not selected:
        raise PlanModelTrainingDataError(
            "event_symbol_sample_empty",
            "真实公告和严格映射重大新闻没有产生证券样本",
        )
    covered_dates = set().union(*(symbol_dates[symbol] for symbol in selected))
    return selected, {
        "contract": "point_in_time_event_bearing_symbol_frequency_sample_v1",
        "eligible_symbol_count": len(symbol_dates),
        "selected_symbol_count": len(selected),
        "selected_event_date_count": len(covered_dates),
        "selection_order": "event_date_frequency_desc_then_sha256",
        "primary_event_sources": list(EVENT_PRIMARY_SOURCES),
        "top_list_excluded_from_primary_sample": True,
        "selected_symbols_sha256": hashlib.sha256(
            "\n".join(selected).encode("utf-8")
        ).hexdigest(),
    }


def _load_training_moneyflow_rows(
    conn: Any,
    *,
    model_key: str,
    start_date: str,
    end_date: str,
    symbols: list[str],
) -> list[Any]:
    if model_key == "narrative_transition_multitask_v1":
        return []
    if not symbols:
        raise PlanModelTrainingDataError(
            "moneyflow_symbol_sample_empty",
            "资金流训练查询缺少冻结证券集合",
        )
    placeholders = ",".join("?" for _ in symbols)
    try:
        return conn.execute(
            f"""SELECT ts_code,trade_date,payload_json,available_time
                FROM dc_tushare_raw_records
                WHERE api_name='moneyflow'
                  AND trade_date>=? AND trade_date<=?
                  AND ts_code IN ({placeholders})
                ORDER BY ts_code,trade_date""",
            (start_date, end_date, *symbols),
        ).fetchall()
    except Exception as exc:
        code = (
            "capital_moneyflow_query_failed"
            if model_key == "capital_persistence_multitask_v1"
            else "moneyflow_query_failed"
        )
        raise PlanModelTrainingDataError(code, str(exc)[:300]) from exc


def controlled_strategy_ab_passed_current_contract(
    model_key: str,
    impact: dict[str, Any] | None,
) -> bool:
    evidence = impact if isinstance(impact, dict) else {}
    gates = evidence.get("gates") if isinstance(evidence.get("gates"), dict) else {}
    frozen = evidence.get("frozen_contract") if isinstance(
        evidence.get("frozen_contract"), dict
    ) else {}
    cost_contract = frozen.get("execution_cost_contract") if isinstance(
        frozen.get("execution_cost_contract"), dict
    ) else {}
    windows = evidence.get("windows") if isinstance(evidence.get("windows"), list) else []
    if not (
        str(evidence.get("status") or "") == "passed"
        and str(evidence.get("schema_version") or "")
        == CONTROLLED_STRATEGY_AB_CONTRACT
        and str(evidence.get("comparison_contract") or "")
        == CONTROLLED_STRATEGY_AB_COMPARISON_CONTRACT
        and str(cost_contract.get("schema_version") or "")
        == CONTROLLED_STRATEGY_AB_COST_CONTRACT
        and len(windows) == 5
        and all(bool(gates.get(key)) for key in CONTROLLED_STRATEGY_AB_REQUIRED_GATES)
    ):
        return False
    expected_effect_contract = (
        NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
        if model_key == "narrative_transition_multitask_v1"
        else "weighted_model_signal_fusion_v1"
    )
    if str(evidence.get("model_effect_contract") or "") != expected_effect_contract:
        return False
    return bool(
        model_key != "narrative_transition_multitask_v1"
        or gates.get("candidate_fold_model_effect_contract_current")
    )


PLAN_MODELS: dict[str, dict[str, Any]] = {
    "medium_horizon_lambdarank_v1": {
        "name": "中期景气排序模型",
        "family": "LightGBM LambdaRank",
        "role": "稳定底仓与中期股票排序",
        "required_data": ["1512日价格", "财务景气", "行业状态", "任务原生OOS"],
        "minimum_sessions": 252 * 4,
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [5, 10, 20],
        "label_gain": [0, 1, 3, 7, 15],
    },
    "capital_persistence_multitask_v1": {
        "name": "资金持续多任务模型",
        "family": "LightGBM direction+quantile+horizon-failure bundle",
        "role": "确认叙事资金认可度与加减仓门",
        "required_data": ["个股真实资金流", "大单净流入", "5/20日资金持续率", "未来10日资金持续标签"],
        "minimum_sessions": 252 * 2,
        "tasks": [
            "direction_classifier",
            "p10_p50_p90_quantile",
            "horizon_failure_probability",
        ],
    },
    "leader_lambdarank_v1": {
        "name": "龙头排序模型",
        "family": "LightGBM LambdaRank",
        "role": "已确认主线内部龙头选择",
        "required_data": ["主题/行业query", "相对板块收益", "资金持续", "席位质量", "可成交性"],
        "minimum_sessions": 252 * 2,
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [1, 3, 5],
        "lambdarank_truncation_level": 8,
    },
    "event_impact_assessor_v2": {
        "name": "事件影响评估器",
        "family": "点时结构化事件 + 预期/规模/已定价条件化 + LightGBM多头概率/校准分位模型",
        "role": "评估事件相关性、已定价程度、成本后方向、延续/反转和持有期",
        "required_data": [
            "公告与重大新闻", "严格公司映射", "真实可见时间",
            "事件类型与披露数值", "相对公司规模的重要性", "可观测预期差",
            "分析师预期上下文", "同期市场反应", "龙虎榜确认/拥挤", "未来异常收益标签",
        ],
        "minimum_sessions": 250,
        "tasks": [
            "positive_impact_probability",
            "priced_in_probability",
            "continuation_probability",
            "reversal_probability",
            "direction_realization_probability",
            "abnormal_return_q10_q50_q90",
        ],
        "status_note": "只评估已经发生事件的条件影响，不预测事件是否发生或精确收益点值",
    },
    "narrative_transition_multitask_v1": {
        "name": "叙事状态转移模型",
        "family": "LightGBM transition-hazard + constrained-destination lifecycle bundle",
        "role": "主线生命周期加减仓/退出",
        "required_data": ["250日主题快照", "板块广度", "拥挤度", "状态年龄", "点时转移标签"],
        "minimum_sessions": 250,
        "status_note": "主题快照不足250日不得晋升",
    },
}

_locks: set[str] = set()
_lock = threading.Lock()
_status_cache: dict[str, Any] = {"at": 0.0, "value": None}
_operations_cache: dict[str, Any] = {"at": 0.0, "value": None}
_base_coverage_cache: dict[str, Any] = {"at": 0.0, "value": None}
_base_coverage_cache_lock = threading.Lock()
_status_refresh_lock = threading.Lock()


def _serialize_status_refresh(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with _status_refresh_lock:
            return func(*args, **kwargs)

    return wrapped
PLAN_MODEL_FEATURES = [
    "momentum_20",
    "momentum_60",
    "volatility_20",
    "turnover_slope",
    "capital_persistence",
    "narrative_strength",
    "event_surprise",
    "event_text_sentiment",
    "event_text_confidence",
    "event_market_reaction",
    "event_crowding",
    "event_source_announcement",
    "event_source_structured_disclosure",
    "event_source_major_news",
    "event_source_top_list",
    "event_type_earnings",
    "event_type_capital_return",
    "event_type_contract_operation",
    "event_type_governance_risk",
    "event_type_other",
    "event_magnitude",
    "event_magnitude_observed",
    "event_materiality",
    "event_materiality_observed",
    "event_expectation_gap",
    "event_expectation_observed",
    "event_consensus_org_count_log",
    "event_consensus_dispersion",
    "event_consensus_dispersion_observed",
    "event_consensus_freshness",
    "event_consensus_freshness_observed",
    "event_company_size_log",
    "event_company_size_observed",
    "event_pre_event_runup",
    "event_priced_in_proxy",
    "event_analyst_revision",
    "event_analyst_revision_observed",
    "event_target_price_upside",
    "event_target_price_observed",
    "event_count_log",
    "event_numeric_evidence_log",
    "market_state_s0",
    "market_state_s1",
    "market_state_s2",
    "market_state_s3",
    "narrative_lifecycle_code",
    "narrative_transition_pressure",
    "narrative_breadth",
    "narrative_crowding",
    "narrative_score_slope",
    "narrative_breadth_slope",
    "narrative_state_age",
]
EVENT_IMPACT_FEATURES = (
    "momentum_20",
    "momentum_60",
    "volatility_20",
    "turnover_slope",
    "capital_persistence",
    "narrative_strength",
    "event_surprise",
    "event_text_sentiment",
    "event_text_confidence",
    "event_market_reaction",
    "event_crowding",
    "event_source_announcement",
    "event_source_structured_disclosure",
    "event_source_major_news",
    "event_source_top_list",
    "event_type_earnings",
    "event_type_capital_return",
    "event_type_contract_operation",
    "event_type_governance_risk",
    "event_type_other",
    "event_magnitude",
    "event_magnitude_observed",
    "event_materiality",
    "event_materiality_observed",
    "event_expectation_gap",
    "event_expectation_observed",
    "event_consensus_org_count_log",
    "event_consensus_dispersion",
    "event_consensus_dispersion_observed",
    "event_consensus_freshness",
    "event_consensus_freshness_observed",
    "event_company_size_log",
    "event_company_size_observed",
    "event_pre_event_runup",
    "event_priced_in_proxy",
    "event_analyst_revision",
    "event_analyst_revision_observed",
    "event_target_price_upside",
    "event_target_price_observed",
    "event_count_log",
    "event_numeric_evidence_log",
    "market_state_s0",
    "market_state_s1",
    "market_state_s2",
    "market_state_s3",
)
EVENT_PROBABILITY_HEADS = (
    "positive_impact",
    "priced_in",
    "continuation",
    "reversal",
    "direction_realization",
)
EVENT_HEAD_CONTEXT_FEATURES = (
    "event_source_announcement",
    "event_source_structured_disclosure",
    "event_source_major_news",
    "event_source_top_list",
    "event_type_earnings",
    "event_type_capital_return",
    "event_type_contract_operation",
    "event_type_governance_risk",
    "event_type_other",
)
EVENT_MARKET_STATE_CONTEXT_FEATURES = (
    "market_state_s0",
    "market_state_s1",
    "market_state_s2",
    "market_state_s3",
)
EVENT_HEAD_FEATURE_SUBSET_CANDIDATE_V1 = {
    "contract": "alis_event_task_specific_head_feature_subsets_v1",
    "source_model_version_id": 272,
    "source_audit_contract": EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT,
    "quantile_head_policy": "unchanged_full_event_feature_set",
    "model_parameter_changes": False,
    "evaluation_gate_changes": False,
    "subsets": {
        "positive_impact": (
            "momentum_20",
            "momentum_60",
            "capital_persistence",
            "event_market_reaction",
            "event_pre_event_runup",
            "event_materiality",
            "event_materiality_observed",
            "event_count_log",
            "event_text_confidence",
            *EVENT_HEAD_CONTEXT_FEATURES,
        ),
        "priced_in": (
            "momentum_60",
            "volatility_20",
            "event_market_reaction",
            "event_priced_in_proxy",
            "event_crowding",
            "event_company_size_log",
            "event_company_size_observed",
            "event_materiality",
            "event_materiality_observed",
            *EVENT_HEAD_CONTEXT_FEATURES,
        ),
        "continuation": (
            "momentum_20",
            "momentum_60",
            "capital_persistence",
            "event_market_reaction",
            "event_pre_event_runup",
            "event_count_log",
            "event_text_confidence",
            *EVENT_HEAD_CONTEXT_FEATURES,
        ),
        "reversal": (
            "volatility_20",
            "turnover_slope",
            "capital_persistence",
            "event_market_reaction",
            "event_pre_event_runup",
            "event_materiality",
            "event_materiality_observed",
            "event_company_size_log",
            "event_company_size_observed",
            "event_text_confidence",
            *EVENT_HEAD_CONTEXT_FEATURES,
        ),
        "direction_realization": (
            "momentum_60",
            "capital_persistence",
            "event_surprise",
            "event_text_sentiment",
            "event_text_confidence",
            "event_market_reaction",
            "event_pre_event_runup",
            "event_materiality",
            "event_materiality_observed",
            *EVENT_HEAD_CONTEXT_FEATURES,
        ),
    },
}
EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1 = {
    "contract": "alis_event_task_specific_heads_with_market_state_context_v1",
    "source_model_version_id": 273,
    "source_audit_contract": EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT,
    "quantile_head_policy": "full_event_features_with_point_in_time_market_state",
    "model_parameter_changes": False,
    "evaluation_gate_changes": False,
    "subsets": {
        head: tuple(features) + EVENT_MARKET_STATE_CONTEXT_FEATURES
        for head, features in EVENT_HEAD_FEATURE_SUBSET_CANDIDATE_V1["subsets"].items()
    },
}
EVENT_PRICED_REVERSAL_REFINEMENT_CANDIDATE_V1 = {
    "contract": "alis_event_priced_in_reversal_frozen_oos_refinement_v1",
    "source_model_version_id": 274,
    "source_audit_contract": EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT,
    "quantile_head_policy": "full_event_features_with_point_in_time_market_state",
    "model_parameter_changes": False,
    "evaluation_gate_changes": False,
    "subsets": {
        **{
            head: tuple(features)
            for head, features in EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1[
                "subsets"
            ].items()
            if head not in {"priced_in", "reversal"}
        },
        "priced_in": (
            "event_market_reaction",
            "event_priced_in_proxy",
            "event_crowding",
            "event_materiality",
            "event_materiality_observed",
            *EVENT_HEAD_CONTEXT_FEATURES,
            *EVENT_MARKET_STATE_CONTEXT_FEATURES,
        ),
        "reversal": (
            "capital_persistence",
            "event_market_reaction",
            "event_text_confidence",
            "event_materiality_observed",
            *EVENT_HEAD_CONTEXT_FEATURES,
            *EVENT_MARKET_STATE_CONTEXT_FEATURES,
        ),
    },
    "removed_from_priced_in": (
        "momentum_60",
        "volatility_20",
        "event_company_size_log",
        "event_company_size_observed",
    ),
    "removed_from_reversal": (
        "volatility_20",
        "turnover_slope",
        "event_pre_event_runup",
        "event_materiality",
        "event_company_size_log",
        "event_company_size_observed",
    ),
}
EVENT_BASELINE_RESIDUAL_SHRINKAGE_CANDIDATE_V1 = {
    "contract": "alis_event_validation_selected_strong_baseline_residual_v1",
    "source_model_version_id": 275,
    "source_audit_contract": EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT,
    "head_feature_subset_contract": EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1[
        "contract"
    ],
    "quantile_head_policy": "full_event_features_with_point_in_time_market_state",
    "model_parameter_changes": False,
    "evaluation_gate_changes": False,
    "test_label_selection": False,
    "subsets": {
        head: tuple(features)
        for head, features in EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1[
            "subsets"
        ].items()
    },
}
EVENT_VALIDATION_SELECTED_HEAD_CANDIDATE_V1 = {
    "contract": "alis_event_validation_selected_broad_or_refined_heads_v1",
    "source_model_version_id": 277,
    "source_audit_contract": (
        "alis_event_fixed_subset_tradeoff_formal_oos_274_275_276_277_v1"
    ),
    "primary_head_feature_subset_contract": (
        EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1["contract"]
    ),
    "baseline_shrinkage_contract": (
        EVENT_BASELINE_RESIDUAL_SHRINKAGE_CANDIDATE_V1["contract"]
    ),
    "primary_subsets": {
        head: tuple(features)
        for head, features in EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1[
            "subsets"
        ].items()
    },
    "alternative_subsets": {
        "priced_in": tuple(
            EVENT_PRICED_REVERSAL_REFINEMENT_CANDIDATE_V1["subsets"][
                "priced_in"
            ]
        ),
        "reversal": tuple(
            EVENT_PRICED_REVERSAL_REFINEMENT_CANDIDATE_V1["subsets"][
                "reversal"
            ]
        ),
    },
    "selection_data": "validation_only",
    "test_label_selection": False,
    "model_parameter_changes": False,
    "evaluation_gate_changes": False,
}
NARRATIVE_LIFECYCLE_CLASSES = NARRATIVE_V3_STATES
NARRATIVE_ALLOWED_DESTINATIONS: dict[int, tuple[int, ...]] = {
    index: tuple(NARRATIVE_LIFECYCLE_CLASSES.index(destination) for destination in destinations)
    for index, state in enumerate(NARRATIVE_LIFECYCLE_CLASSES)
    for destinations in [NARRATIVE_V3_ALLOWED_TRANSITIONS[state]]
}


def _narrative_reachable_destinations(
    source: int,
    *,
    steps: int = 3,
) -> tuple[int, ...]:
    frontier = {int(source)}
    reachable: set[int] = set()
    for _ in range(max(1, int(steps))):
        frontier = {
            destination
            for state in frontier
            for destination in (
                (state,) + NARRATIVE_ALLOWED_DESTINATIONS[state]
            )
        }
        reachable.update(frontier)
    reachable.discard(int(source))
    return tuple(sorted(reachable))


NARRATIVE_HORIZON_DESTINATIONS = {
    state: _narrative_reachable_destinations(state, steps=3)
    for state in range(len(NARRATIVE_LIFECYCLE_CLASSES))
}
NARRATIVE_MODEL_FEATURES = (
    "capital_persistence",
    "narrative_strength",
    "narrative_lifecycle_code",
    "narrative_transition_pressure",
    "narrative_breadth",
    "narrative_crowding",
    "narrative_score_slope",
    "narrative_breadth_slope",
    "narrative_state_age",
)
CAPITAL_FLOW_HISTORY_SESSIONS = 20
EVENT_POSITIVE_TERMS = ("中标", "增持", "回购", "预增", "扭亏", "签订", "获批", "突破", "上调")
EVENT_NEGATIVE_TERMS = ("减持", "立案", "处罚", "亏损", "下修", "终止", "违约", "退市", "诉讼")
TOP_LIST_NET_RATE_SCALE = 10.0
TOP_LIST_PRICE_CHANGE_SCALE = 10.0


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_news_direction(text: str) -> tuple[str, str, float]:
    positive = sum(1 for term in EVENT_POSITIVE_TERMS if term in text)
    negative = sum(1 for term in EVENT_NEGATIVE_TERMS if term in text)
    if positive == negative:
        return "neutral", "medium", 0.55
    direction = "positive" if positive > negative else "negative"
    strength = abs(positive - negative)
    return direction, ("high" if strength >= 2 else "medium"), min(0.85, 0.6 + strength * 0.1)


def _event_usable_trade_date(
    event_time: str,
    available_time: str,
    trade_dates: list[str],
) -> str:
    source_time = str(event_time or available_time or "")
    available = str(available_time or source_time)
    source_day = source_time[:10].replace("-", "")
    available_day = available[:10].replace("-", "")
    candidate_day = max(source_day, available_day)
    clock = available[11:19] if len(available) >= 19 else ""
    if candidate_day in trade_dates and (not clock or clock <= "15:00:00"):
        return candidate_day
    index = bisect_right(trade_dates, candidate_day)
    return trade_dates[index] if index < len(trade_dates) else ""


def _moneyflow_observations(
    rows: list[Any],
    trade_dates: list[str],
) -> dict[tuple[str, str], dict[str, float]]:
    by_symbol: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        item = dict(row)
        payload = _payload_dict(item.get("payload_json"))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        trade_date = str(item.get("trade_date") or payload.get("trade_date") or "")
        if not symbol or not trade_date:
            continue
        net_amount = float(payload.get("net_mf_amount") or 0.0)
        buy_large = float(payload.get("buy_lg_vol") or 0.0)
        sell_large = float(payload.get("sell_lg_vol") or 0.0)
        volume_scale = max(1.0, abs(buy_large) + abs(sell_large))
        by_symbol.setdefault(symbol, {})[trade_date] = {
            "net_amount": net_amount,
            "large_order_balance": (buy_large - sell_large) / volume_scale,
        }

    observations: dict[tuple[str, str], dict[str, float]] = {}
    date_index = {value: index for index, value in enumerate(trade_dates)}
    for symbol, values in by_symbol.items():
        for trade_date, current in values.items():
            index = date_index.get(trade_date)
            if index is None:
                continue
            history_dates = trade_dates[max(0, index - CAPITAL_FLOW_HISTORY_SESSIONS + 1):index + 1]
            history = [values[value]["net_amount"] for value in history_dates if value in values]
            if len(history) < CAPITAL_FLOW_HISTORY_SESSIONS:
                continue
            future_dates = trade_dates[index + 1:index + 11]
            future = [values[value]["net_amount"] for value in future_dates if value in values]
            if len(future) < 10:
                continue
            scale = max(1.0, float(np.mean(np.abs(history))))
            observations[(symbol, trade_date)] = {
                "net_flow_z": current["net_amount"] / scale,
                "positive_ratio_5": sum(value > 0 for value in history[-5:]) / 5.0,
                "positive_ratio_20": sum(value > 0 for value in history) / 20.0,
                "large_order_balance": current["large_order_balance"],
                "future_persistence": float(np.mean(future)) / scale
                + (sum(value > 0 for value in future) / 10.0 - 0.5),
            }
    return observations


def _plan_model_weights(model_key: str) -> dict[str, float]:
    presets = {
        "medium_horizon_lambdarank_v1": {"momentum_60": .28, "volatility_20": -.16, "capital_persistence": .20, "narrative_strength": .18, "turnover_slope": .12},
        "capital_persistence_multitask_v1": {"capital_persistence": .42, "turnover_slope": .24, "momentum_20": .12, "volatility_20": -.10},
        "leader_lambdarank_v1": {"momentum_20": .26, "capital_persistence": .26, "narrative_strength": .24, "turnover_slope": .14, "volatility_20": -.08},
        "event_impact_assessor_v2": {"event_surprise": .38, "narrative_strength": .22, "turnover_slope": .16, "capital_persistence": .12},
        "narrative_transition_multitask_v1": {"narrative_strength": .36, "capital_persistence": .22, "momentum_20": .14, "volatility_20": -.12},
    }
    raw = presets.get(model_key, {})
    total = sum(abs(v) for v in raw.values()) or 1.0
    return {feature: round(float(raw.get(feature, 0.0)) / total, 8) for feature in PLAN_MODEL_FEATURES}


def _load_artifact(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        payload = json.loads(resolve_model_artifact_path(path).read_text(encoding="utf-8"))
        model_path = str(payload.get("model_path") or "")
        if model_path:
            payload["model_path"] = str(resolve_model_artifact_path(model_path))
        return payload
    except Exception:
        return None


def _artifact_summary(version: dict[str, Any] | None) -> dict[str, Any]:
    artifact = _load_artifact((version or {}).get("artifact_path"))
    if not artifact:
        return {"artifact_ready": False, "artifact_type": "missing"}
    model_path = str(artifact.get("model_path") or "")
    return {
        "artifact_ready": True,
        "artifact_type": artifact.get("artifact_type"),
        "model_type": artifact.get("model_type"),
        "model_path_exists": bool(model_path and resolve_model_artifact_path(model_path).is_file()),
        "features": artifact.get("features") or [],
        "training_dataset": artifact.get("training_dataset") or {},
        "predict_contract": artifact.get("predict_contract") or {},
        "output_contract": artifact.get("output_contract") or {},
        "training_protocol": artifact.get("training_protocol") or {},
    }


def _coerce_feature_vector(features: dict[str, Any], feature_names: list[str]) -> list[float]:
    return [float(features.get(feature, 0.0) or 0.0) for feature in feature_names]


def _estimator_input(estimator: Any, values: Any) -> Any:
    """Preserve fitted feature names without changing numeric values."""

    try:
        feature_names = [
            str(value)
            for value in list(getattr(estimator, "feature_names_in_", []))
        ]
    except Exception:
        feature_names = []
    if not feature_names or hasattr(values, "columns"):
        return values
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != len(feature_names):
        return values
    import pandas as pd

    return pd.DataFrame(matrix, columns=feature_names)


def _predict_values(model: Any, values: Any) -> np.ndarray:
    return np.asarray(
        model.predict(_estimator_input(model, values)),
        dtype=float,
    )


def _canonical_class_probabilities(
    classifier: Any,
    values: Any,
    *,
    class_count: int,
) -> np.ndarray:
    raw = np.asarray(
        classifier.predict_proba(_estimator_input(classifier, values)),
        dtype=float,
    )
    if raw.ndim == 1:
        raw = raw.reshape(-1, 1)
    classes = [
        int(value)
        for value in np.asarray(
            getattr(classifier, "classes_", np.arange(raw.shape[1])),
            dtype=int,
        ).tolist()
    ]
    probabilities = np.zeros((raw.shape[0], int(class_count)), dtype=float)
    for source_index, class_id in enumerate(classes):
        if 0 <= class_id < int(class_count) and source_index < raw.shape[1]:
            probabilities[:, class_id] = raw[:, source_index]
    return probabilities


def _model_score(model: Any, values: Any) -> np.ndarray:
    scores = _predict_values(model, values).reshape(-1)
    if len(scores) != len(values):
        raise ValueError("模型预测行数与输入不一致")
    return scores


class _ConstantProbabilityClassifier:
    """Pickle-safe fallback for a training slice containing one observed class."""

    def __init__(self, probabilities: list[float]):
        values = np.asarray(probabilities, dtype=float)
        values = np.maximum(values, 0.0)
        self._probabilities = values / max(float(values.sum()), 1e-12)
        self.classes_ = np.arange(len(values), dtype=int)

    def predict_proba(self, values: Any) -> np.ndarray:
        return np.tile(self._probabilities, (len(values), 1))


def _canonical_narrative_observation(
    *,
    lifecycle: str,
    score: float,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Recover the V2 lifecycle strictly from persisted observable evidence."""

    components = (
        payload.get("components")
        if isinstance(payload.get("components"), dict)
        else {}
    )
    contract = (
        payload.get("lifecycle_contract")
        if isinstance(payload.get("lifecycle_contract"), dict)
        else {}
    )
    observations = (
        contract.get("observations")
        if isinstance(contract.get("observations"), dict)
        else {}
    )
    evidence = (
        payload.get("evidence")
        if isinstance(payload.get("evidence"), dict)
        else {}
    )
    base_score = float(
        observations.get("base_score_before_crowding_penalty")
        or payload.get("base_score")
        or score
        or 0.0
    )
    crowding = float(
        observations.get("turnover_top_decile_concentration")
        or (float(payload.get("crowding_penalty") or 0.0) / 20.0)
    )
    breadth = float(
        observations.get("breadth_5_session")
        or components.get("breadth")
        or 0.0
    )
    persistence = float(
        observations.get("persistence_5_session")
        or components.get("capital_persistence")
        or 0.0
    )
    price_confirmation = float(components.get("price_confirmation") or 0.0)
    breadth_slope = float(observations.get("breadth_slope") or 0.0)
    score_slope = float(observations.get("score_slope") or 0.0)
    degenerate_legacy_snapshot = (
        not str(contract.get("schema_version") or "").startswith(
            "alis_narrative_lifecycle_observable_v2"
        )
        and breadth == 0.0
        and float(components.get("capital_persistence") or 0.0) == 0.0
        and int(evidence.get("source_types") or 0) == 0
    )
    direct = {
        name: index for index, name in enumerate(NARRATIVE_LIFECYCLE_CLASSES)
    }
    if str(contract.get("schema_version") or "").startswith(NARRATIVE_V3_SCHEMA_VERSION):
        candidate_name = narrative_candidate_state(
            base_score=base_score,
            momentum=float(observations.get("momentum_5_session") or 0.0),
            breadth=breadth,
            persistence=persistence,
            price_confirmation=price_confirmation,
            breadth_slope=breadth_slope,
            score_slope=score_slope,
        )
        candidate = int(direct[candidate_name])
    elif lifecycle in direct:
        candidate = int(direct[lifecycle])
    elif lifecycle in {"退潮", "沉寂"}:
        candidate = 3
    elif (
        base_score >= 55
        and crowding >= .60
        and price_confirmation >= .50
    ):
        candidate = 2
    elif (
        base_score >= 55
        and breadth >= .55
        and price_confirmation >= .50
    ):
        candidate = 1
    else:
        candidate = 0
    return {
        "candidate_state": candidate,
        "base_score": base_score,
        "score": float(score or 0.0),
        "breadth": breadth,
        "crowding": crowding,
        "price_confirmation": price_confirmation,
        "capital_persistence": float(
            components.get("capital_persistence") or 0.0
        ),
        "observable": not degenerate_legacy_snapshot,
    }


def _constrain_narrative_state(previous: int | None, candidate: int) -> int:
    """Compatibility graph constraint; duration/confirmation are applied by
    ``narrative_transition_state`` in the point-in-time label loop."""

    if previous is None or candidate == previous:
        return int(candidate)
    if candidate in NARRATIVE_ALLOWED_DESTINATIONS[int(previous)]:
        return int(candidate)
    # A daily observation cannot skip an intermediate lifecycle phase.
    if previous == 0 and candidate == 2:
        return 1
    if previous == 2 and candidate == 0:
        return 1
    if previous == 3 and candidate in {1, 2}:
        return 0
    return int(candidate)


def _narrative_two_stage_probabilities(
    model: dict[str, Any],
    values: np.ndarray,
    current_states: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    transition_values = _canonical_class_probabilities(
        model["transition_classifier"],
        values,
        class_count=2,
    )
    transition_values = _temperature_scale_probabilities(
        transition_values,
        float(model.get("transition_temperature") or 1.0),
    )
    transition = _apply_binary_logit_bias(
        transition_values[:, 1],
        float(model.get("transition_logit_bias") or 0.0),
    )
    destination = _canonical_class_probabilities(
        model["destination_classifier"],
        values,
        class_count=len(NARRATIVE_LIFECYCLE_CLASSES),
    )
    destination = _temperature_scale_probabilities(
        destination,
        float(model.get("destination_temperature") or 1.0),
    )
    final = np.zeros_like(destination)
    for index, current in enumerate(np.asarray(current_states, dtype=int)):
        current = int(np.clip(current, 0, len(NARRATIVE_LIFECYCLE_CLASSES) - 1))
        allowed = NARRATIVE_HORIZON_DESTINATIONS[current]
        masked = np.zeros(len(NARRATIVE_LIFECYCLE_CLASSES), dtype=float)
        masked[list(allowed)] = destination[index, list(allowed)]
        if float(masked.sum()) <= 1e-12:
            masked[list(allowed)] = 1.0
        masked /= float(masked.sum())
        change_probability = float(np.clip(transition[index], 0.0, 1.0))
        final[index] = change_probability * masked
        final[index, current] += 1.0 - change_probability
        destination[index] = masked
    return final, transition, destination


def _temperature_scale_probabilities(
    probabilities: np.ndarray,
    temperature: float,
) -> np.ndarray:
    values = np.clip(np.asarray(probabilities, dtype=float), 1e-12, 1.0)
    scaled = np.exp(np.log(values) / max(.25, min(20.0, temperature)))
    return scaled / np.maximum(scaled.sum(axis=1, keepdims=True), 1e-12)


def _apply_binary_logit_bias(
    probabilities: np.ndarray,
    bias: float,
) -> np.ndarray:
    values = np.clip(np.asarray(probabilities, dtype=float), 1e-9, 1 - 1e-9)
    logits = np.log(values / (1.0 - values)) + float(bias)
    return 1.0 / (1.0 + np.exp(-np.clip(logits, -40, 40)))


def _validation_temperature(
    probabilities: np.ndarray,
    actual: np.ndarray,
) -> float:
    if not len(actual):
        return 1.0
    candidates = (.50, .75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 12.0)
    losses = []
    for temperature in candidates:
        scaled = _temperature_scale_probabilities(
            probabilities,
            temperature,
        )
        actual_probability = scaled[
            np.arange(len(actual)),
            np.asarray(actual, dtype=int),
        ]
        losses.append(float(-np.mean(np.log(
            np.clip(actual_probability, 1e-12, 1.0)
        ))))
    return float(candidates[int(np.argmin(losses))])


def _validation_baseline_blend_weight(
    actual: np.ndarray,
    model_prediction: np.ndarray,
    baseline_prediction: np.ndarray,
    *,
    alpha: float | None = None,
) -> float:
    """Select model residual weight on validation rows only.

    Zero means deterministic fallback to the frozen training-only baseline;
    one means the calibrated model output.  Ties prefer the safer lower model
    weight.  Test labels never participate.
    """

    truth = np.asarray(actual, dtype=float)
    model_values = np.asarray(model_prediction, dtype=float)
    baseline_values = np.asarray(baseline_prediction, dtype=float)
    if not len(truth) or not (
        len(truth) == len(model_values) == len(baseline_values)
    ):
        return 0.0
    candidates = (0.0, 0.25, 0.5, 0.75, 1.0)
    losses: list[float] = []
    for weight in candidates:
        prediction = baseline_values + weight * (model_values - baseline_values)
        if alpha is None:
            loss = float(np.mean(np.square(prediction - truth)))
        else:
            error = truth - prediction
            loss = float(np.mean(np.maximum(alpha * error, (alpha - 1.0) * error)))
        losses.append(loss)
    return float(candidates[int(np.argmin(losses))])


def _event_impact_bundle_outputs(
    model: dict[str, Any],
    values: np.ndarray,
    *,
    event_sources: list[str] | None = None,
    event_types: list[str] | None = None,
    event_reaction: np.ndarray | None = None,
    event_direction_signal: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    temperatures = (
        model.get("temperature")
        if isinstance(model.get("temperature"), dict)
        else {}
    )
    outputs: dict[str, np.ndarray] = {}
    head_feature_indices = (
        model.get("head_feature_indices")
        if isinstance(model.get("head_feature_indices"), dict)
        else {}
    )
    for name in ("positive_impact", "priced_in", "continuation", "reversal"):
        indices = head_feature_indices.get(name)
        head_values = values[:, indices] if isinstance(indices, list) else values
        probabilities = _canonical_class_probabilities(
            model[name], head_values, class_count=2,
        )
        calibrated = _temperature_scale_probabilities(
            probabilities,
            float(temperatures.get(name) or 1.0),
        )
        outputs[name] = calibrated[:, 1]
    if "direction_realization" in model:
        indices = head_feature_indices.get("direction_realization")
        head_values = values[:, indices] if isinstance(indices, list) else values
        probabilities = _canonical_class_probabilities(
            model["direction_realization"], head_values, class_count=2,
        )
        calibrated = _temperature_scale_probabilities(
            probabilities,
            float(temperatures.get("direction_realization") or 1.0),
        )
        outputs["direction_realization"] = calibrated[:, 1]
    baseline_models = (
        model.get("event_baseline_models")
        if isinstance(model.get("event_baseline_models"), dict)
        else {}
    )
    blend_weights = (
        model.get("event_baseline_blend_weights")
        if isinstance(model.get("event_baseline_blend_weights"), dict)
        else {}
    )
    evaluation_groups: list[str] = []
    if baseline_models:
        row_count = len(values)
        if (
            event_sources is None
            or event_types is None
            or event_reaction is None
            or event_direction_signal is None
            or not all(
                len(item) == row_count
                for item in (
                    event_sources, event_types, event_reaction,
                    event_direction_signal,
                )
            )
        ):
            raise ValueError(
                "event baseline shrinkage requires source, type, reaction and direction metadata"
            )
        evaluation_groups = event_response_group_keys(
            event_sources,
            event_reaction,
            event_direction_signal,
            event_types,
        )
        probability_baselines = dict(
            baseline_models.get("probability") or {}
        )
        for name in EVENT_PROBABILITY_HEADS:
            if name not in outputs or name not in probability_baselines:
                continue
            baseline = apply_event_probability_baseline(
                probability_baselines[name], evaluation_groups,
            )
            weight = float(blend_weights.get(name) or 0.0)
            outputs[name] = baseline + weight * (outputs[name] - baseline)
    offsets = (
        model.get("quantile_offsets")
        if isinstance(model.get("quantile_offsets"), dict)
        else {}
    )
    quantile_outputs: list[np.ndarray] = []
    quantile_baselines = dict(baseline_models.get("quantile") or {})
    for name in ("q10", "q50", "q90"):
        prediction = _predict_values(model[name], values) + float(
            offsets.get(name) or 0.0
        )
        if evaluation_groups and name in quantile_baselines:
            baseline = apply_event_quantile_baseline(
                quantile_baselines[name], evaluation_groups,
            )
            weight = float(blend_weights.get(name) or 0.0)
            prediction = baseline + weight * (prediction - baseline)
        quantile_outputs.append(prediction)
    quantile_values = np.column_stack(quantile_outputs)
    quantile_values.sort(axis=1)
    for index, name in enumerate(("q10", "q50", "q90")):
        outputs[name] = quantile_values[:, index]
    return outputs


def score_plan_model_features(
    model_key: str,
    features: dict[str, Any],
    artifact: dict[str, Any] | None = None,
    *,
    allow_proxy: bool = False,
) -> dict[str, Any]:
    payload = artifact
    if payload is None:
        active = get_active_model_version(model_key)
        payload = _load_artifact((active or {}).get("artifact_path"))
    if payload is None:
        if not allow_proxy:
            return {
                "ok": False,
                "model_key": model_key,
                "score": None,
                "reason": "active_model_artifact_unavailable",
                "model_type": "unavailable",
            }
        payload = {
            "weights": _plan_model_weights(model_key),
            "features": PLAN_MODEL_FEATURES,
            "model_type": "plan_linear_ranker",
        }
    feature_names = list(payload.get("features") or PLAN_MODEL_FEATURES)
    model_path = payload.get("model_path")
    model_type = str(payload.get("model_type") or "plan_linear_ranker")
    if model_path or payload.get("_model") is not None:
        model = payload.get("_model")
        if model is None:
            with Path(str(model_path)).open("rb") as handle:
                model = pickle.load(handle)
        vector = np.asarray([_coerce_feature_vector(features, feature_names)], dtype=float)
        if isinstance(model, dict) and model.get("bundle_type") == "capital_persistence_multihead_v1":
            direction_probabilities = _canonical_class_probabilities(
                model["direction"], vector, class_count=5,
            )[0]
            direction_score = float(direction_probabilities @ np.linspace(-1.0, 1.0, len(direction_probabilities)))
            quantiles = {
                key: float(_predict_values(model[key], vector)[0])
                for key in ("q10", "q50", "q90")
            }
            hazard = float(_canonical_class_probabilities(
                model["hazard"], vector, class_count=2,
            )[0, 1])
            value = direction_score + quantiles["q50"] - hazard
            return {
                "ok": True, "model_key": model_key, "score": round(value, 8),
                "model_type": model_type, "artifact_version": payload.get("version"),
                "outputs": {
                    "direction_probability": round(float(direction_probabilities[-1]), 8),
                    "strength_p10": round(quantiles["q10"], 8),
                    "strength_p50": round(quantiles["q50"], 8),
                    "strength_p90": round(quantiles["q90"], 8),
                    "failure_hazard": round(hazard, 8),
                },
            }
        if isinstance(model, dict) and model.get("bundle_type") in {
            "event_impact_assessor_multihead_v2",
            "event_impact_assessor_multihead_v3",
            "event_impact_assessor_multihead_v4",
        }:
            event_outputs = _event_impact_bundle_outputs(
                model,
                vector,
                event_sources=[str(features.get("event_source_type") or "unknown")],
                event_types=[str(features.get("event_type_group") or "other")],
                event_reaction=np.asarray([
                    float(features.get("event_market_reaction") or 0.0)
                ]),
                event_direction_signal=np.asarray([
                    float(features.get("event_surprise") or 0.0)
                ]),
            )
            positive = float(event_outputs["positive_impact"][0])
            priced_in = float(event_outputs["priced_in"][0])
            continuation = float(event_outputs["continuation"][0])
            reversal = float(event_outputs["reversal"][0])
            direction_realization = (
                float(event_outputs["direction_realization"][0])
                if "direction_realization" in event_outputs
                else None
            )
            quantiles = {
                key: float(event_outputs[key][0])
                for key in ("q10", "q50", "q90")
            }
            holding_horizon = (
                0 if priced_in >= 0.65 or reversal >= 0.60
                else 5 if continuation >= 0.55
                else 2
            )
            hard_veto = bool(
                quantiles["q90"] < -EVENT_TRANSACTION_COST_RATE
                and positive < 0.20
            )
            value = positive + continuation - reversal - 0.25 * priced_in
            return {
                "ok": True,
                "model_key": model_key,
                "score": round(value, 8),
                "model_type": model_type,
                "artifact_version": payload.get("version"),
                "outputs": {
                    "impact_contract": str(
                        model.get("impact_contract") or EVENT_IMPACT_CONTRACT
                    ),
                    "event_relevance": round(
                        float(features.get("event_text_confidence") or 0.0),
                        8,
                    ),
                    "positive_impact_probability": round(positive, 8),
                    "priced_in_probability": round(priced_in, 8),
                    "continuation_probability": round(continuation, 8),
                    "reversal_probability": round(reversal, 8),
                    **(
                        {
                            "direction_realization_probability": round(
                                direction_realization,
                                8,
                            )
                        }
                        if direction_realization is not None
                        else {}
                    ),
                    "abnormal_return_q10": round(quantiles["q10"], 8),
                    "abnormal_return_q50": round(quantiles["q50"], 8),
                    "abnormal_return_q90": round(quantiles["q90"], 8),
                    "crowding_risk": round(
                        min(
                            1.0,
                            max(0.0, float(features.get("event_crowding") or 0.0)) / 3.0,
                        ),
                        8,
                    ),
                    "recommended_holding_horizon_sessions": holding_horizon,
                    "event_hard_veto": hard_veto,
                },
            }
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_transition_classifier_v1":
            probabilities = _canonical_class_probabilities(
                model["classifier"], vector, class_count=5,
            )[0]
            classes = [str(value) for value in model.get("classes") or range(len(probabilities))]
            probability_map = {key: round(float(value), 8) for key, value in zip(classes, probabilities)}
            exit_risk = sum(value for key, value in probability_map.items() if key in {"拥挤", "退潮", "沉寂"})
            value = 1.0 - float(exit_risk)
            return {
                "ok": True, "model_key": model_key, "score": round(value, 8),
                "model_type": model_type, "artifact_version": payload.get("version"),
                "outputs": {
                    "next_state_probabilities": probability_map,
                    "exit_risk": round(float(exit_risk), 8),
                    "confidence": round(float(max(probabilities, default=0.0)), 8),
                },
            }
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_lifecycle_two_stage_v2":
            current_state = int(np.clip(round(
                float(features.get("narrative_lifecycle_code") or 0.0)
                * (len(NARRATIVE_LIFECYCLE_CLASSES) - 1)
            ), 0, len(NARRATIVE_LIFECYCLE_CLASSES) - 1))
            probabilities, transition, destination = (
                _narrative_two_stage_probabilities(
                    model,
                    vector,
                    np.asarray([current_state], dtype=int),
                )
            )
            probability_map = {
                key: round(float(value), 8)
                for key, value in zip(
                    NARRATIVE_LIFECYCLE_CLASSES,
                    probabilities[0],
                )
            }
            exit_risk = probability_map["成熟/拥挤"] + probability_map["衰退"]
            lifecycle_authorization = model.get(
                "lifecycle_effect_authorization"
            ) if isinstance(
                model.get("lifecycle_effect_authorization"), dict
            ) else {}
            return {
                "ok": True,
                "model_key": model_key,
                "score": round(1.0 - float(exit_risk), 8),
                "model_type": model_type,
                "artifact_version": payload.get("version"),
                "outputs": {
                    "lifecycle_effect_contract": (
                        NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
                    ),
                    "lifecycle_effect_authorized": bool(
                        lifecycle_authorization.get("authorized")
                    ),
                    "lifecycle_effect_authorization": dict(
                        lifecycle_authorization
                    ),
                    "transition_probability": round(float(transition[0]), 8),
                    "conditional_destination_probabilities": {
                        key: round(float(value), 8)
                        for key, value in zip(
                            NARRATIVE_LIFECYCLE_CLASSES,
                            destination[0],
                        )
                    },
                    "next_state_probabilities": probability_map,
                    "exit_risk": round(float(exit_risk), 8),
                    "confidence": round(
                        float(max(probabilities[0], default=0.0)),
                        8,
                    ),
                },
            }
        value = float(_predict_values(model, vector)[0])
        result = {"ok": True, "model_key": model_key, "score": round(value, 8), "model_type": model_type, "artifact_version": payload.get("version")}
        return result
    weights = dict(payload.get("weights") or {})
    value = sum(float(weights.get(key, 0.0) or 0.0) * float(features.get(key, 0.0) or 0.0) for key in feature_names)
    return {"ok": True, "model_key": model_key, "score": round(value, 8), "model_type": model_type}


def score_plan_model_feature_batch(
    model_key: str,
    features_by_symbol: dict[str, dict[str, Any]],
    artifact: dict[str, Any] | None = None,
    *,
    allow_proxy: bool = False,
) -> dict[str, dict[str, Any]]:
    """Score one cross-section per estimator call without changing outputs."""

    symbols = list(features_by_symbol)
    if not symbols:
        return {}
    payload = artifact
    if payload is None:
        active = get_active_model_version(model_key)
        payload = _load_artifact((active or {}).get("artifact_path"))
    if payload is None:
        if not allow_proxy:
            return {
                symbol: {
                    "ok": False,
                    "model_key": model_key,
                    "score": None,
                    "reason": "active_model_artifact_unavailable",
                    "model_type": "unavailable",
                }
                for symbol in symbols
            }
        payload = {
            "weights": _plan_model_weights(model_key),
            "features": PLAN_MODEL_FEATURES,
            "model_type": "plan_linear_ranker",
        }
    feature_names = list(payload.get("features") or PLAN_MODEL_FEATURES)
    matrix = np.asarray(
        [_coerce_feature_vector(features_by_symbol[symbol], feature_names) for symbol in symbols],
        dtype=float,
    )
    model_path = payload.get("model_path")
    model_type = str(payload.get("model_type") or "plan_linear_ranker")
    if model_path or payload.get("_model") is not None:
        model = payload.get("_model")
        if model is None:
            with Path(str(model_path)).open("rb") as handle:
                model = pickle.load(handle)
        if isinstance(model, dict) and model.get("bundle_type") == "capital_persistence_multihead_v1":
            direction_probabilities = _canonical_class_probabilities(
                model["direction"], matrix, class_count=5,
            )
            direction_scores = direction_probabilities @ np.linspace(
                -1.0, 1.0, direction_probabilities.shape[1],
            )
            quantiles = {
                key: _predict_values(model[key], matrix)
                for key in ("q10", "q50", "q90")
            }
            hazards = _canonical_class_probabilities(
                model["hazard"], matrix, class_count=2,
            )[:, 1]
            values = direction_scores + quantiles["q50"] - hazards
            return {
                symbol: {
                    "ok": True,
                    "model_key": model_key,
                    "score": round(float(values[index]), 8),
                    "model_type": model_type,
                    "artifact_version": payload.get("version"),
                    "outputs": {
                        "direction_probability": round(float(direction_probabilities[index, -1]), 8),
                        "strength_p10": round(float(quantiles["q10"][index]), 8),
                        "strength_p50": round(float(quantiles["q50"][index]), 8),
                        "strength_p90": round(float(quantiles["q90"][index]), 8),
                        "failure_hazard": round(float(hazards[index]), 8),
                    },
                }
                for index, symbol in enumerate(symbols)
            }
        if isinstance(model, dict) and model.get("bundle_type") in {
            "event_impact_assessor_multihead_v2",
            "event_impact_assessor_multihead_v3",
            "event_impact_assessor_multihead_v4",
        }:
            event_outputs = _event_impact_bundle_outputs(
                model,
                matrix,
                event_sources=[
                    str(features_by_symbol[symbol].get("event_source_type") or "unknown")
                    for symbol in symbols
                ],
                event_types=[
                    str(features_by_symbol[symbol].get("event_type_group") or "other")
                    for symbol in symbols
                ],
                event_reaction=np.asarray([
                    float(features_by_symbol[symbol].get("event_market_reaction") or 0.0)
                    for symbol in symbols
                ]),
                event_direction_signal=np.asarray([
                    float(features_by_symbol[symbol].get("event_surprise") or 0.0)
                    for symbol in symbols
                ]),
            )
            result: dict[str, dict[str, Any]] = {}
            for index, symbol in enumerate(symbols):
                positive = float(event_outputs["positive_impact"][index])
                priced_in = float(event_outputs["priced_in"][index])
                continuation = float(event_outputs["continuation"][index])
                reversal = float(event_outputs["reversal"][index])
                direction_realization = (
                    float(event_outputs["direction_realization"][index])
                    if "direction_realization" in event_outputs
                    else None
                )
                q10 = float(event_outputs["q10"][index])
                q50 = float(event_outputs["q50"][index])
                q90 = float(event_outputs["q90"][index])
                holding_horizon = (
                    0 if priced_in >= 0.65 or reversal >= 0.60
                    else 5 if continuation >= 0.55
                    else 2
                )
                features = features_by_symbol[symbol]
                result[symbol] = {
                    "ok": True,
                    "model_key": model_key,
                    "score": round(
                        positive + continuation - reversal - 0.25 * priced_in,
                        8,
                    ),
                    "model_type": model_type,
                    "artifact_version": payload.get("version"),
                    "outputs": {
                        "impact_contract": str(
                            model.get("impact_contract") or EVENT_IMPACT_CONTRACT
                        ),
                        "event_relevance": round(
                            float(features.get("event_text_confidence") or 0.0),
                            8,
                        ),
                        "positive_impact_probability": round(positive, 8),
                        "priced_in_probability": round(priced_in, 8),
                        "continuation_probability": round(continuation, 8),
                        "reversal_probability": round(reversal, 8),
                        **(
                            {
                                "direction_realization_probability": round(
                                    direction_realization,
                                    8,
                                )
                            }
                            if direction_realization is not None
                            else {}
                        ),
                        "abnormal_return_q10": round(q10, 8),
                        "abnormal_return_q50": round(q50, 8),
                        "abnormal_return_q90": round(q90, 8),
                        "crowding_risk": round(
                            min(
                                1.0,
                                max(
                                    0.0,
                                    float(features.get("event_crowding") or 0.0),
                                ) / 3.0,
                            ),
                            8,
                        ),
                        "recommended_holding_horizon_sessions": holding_horizon,
                        "event_hard_veto": bool(
                            q90 < -EVENT_TRANSACTION_COST_RATE
                            and positive < 0.20
                        ),
                    },
                }
            return result
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_transition_classifier_v1":
            probabilities = _canonical_class_probabilities(
                model["classifier"], matrix, class_count=5,
            )
            classes = [str(value) for value in model.get("classes") or range(probabilities.shape[1])]
            result: dict[str, dict[str, Any]] = {}
            for index, symbol in enumerate(symbols):
                probability_map = {
                    key: round(float(value), 8)
                    for key, value in zip(classes, probabilities[index])
                }
                exit_risk = sum(
                    value for key, value in probability_map.items()
                    if key in {"拥挤", "退潮", "沉寂"}
                )
                result[symbol] = {
                    "ok": True,
                    "model_key": model_key,
                    "score": round(1.0 - float(exit_risk), 8),
                    "model_type": model_type,
                    "artifact_version": payload.get("version"),
                    "outputs": {
                        "next_state_probabilities": probability_map,
                        "exit_risk": round(float(exit_risk), 8),
                        "confidence": round(float(max(probabilities[index], default=0.0)), 8),
                    },
                }
            return result
        if isinstance(model, dict) and model.get("bundle_type") == "narrative_lifecycle_two_stage_v2":
            current_states = np.asarray([
                int(np.clip(round(
                    float(
                        features_by_symbol[symbol].get(
                            "narrative_lifecycle_code",
                            0.0,
                        )
                        or 0.0
                    )
                    * (len(NARRATIVE_LIFECYCLE_CLASSES) - 1)
                ), 0, len(NARRATIVE_LIFECYCLE_CLASSES) - 1))
                for symbol in symbols
            ], dtype=int)
            probabilities, transitions, destinations = (
                _narrative_two_stage_probabilities(
                    model,
                    matrix,
                    current_states,
                )
            )
            result = {}
            for index, symbol in enumerate(symbols):
                probability_map = {
                    key: round(float(value), 8)
                    for key, value in zip(
                        NARRATIVE_LIFECYCLE_CLASSES,
                        probabilities[index],
                    )
                }
                exit_risk = (
                    probability_map["成熟/拥挤"]
                    + probability_map["衰退"]
                )
                lifecycle_authorization = model.get(
                    "lifecycle_effect_authorization"
                ) if isinstance(
                    model.get("lifecycle_effect_authorization"), dict
                ) else {}
                result[symbol] = {
                    "ok": True,
                    "model_key": model_key,
                    "score": round(1.0 - float(exit_risk), 8),
                    "model_type": model_type,
                    "artifact_version": payload.get("version"),
                    "outputs": {
                        "lifecycle_effect_contract": (
                            NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
                        ),
                        "lifecycle_effect_authorized": bool(
                            lifecycle_authorization.get("authorized")
                        ),
                        "lifecycle_effect_authorization": dict(
                            lifecycle_authorization
                        ),
                        "transition_probability": round(
                            float(transitions[index]),
                            8,
                        ),
                        "conditional_destination_probabilities": {
                            key: round(float(value), 8)
                            for key, value in zip(
                                NARRATIVE_LIFECYCLE_CLASSES,
                                destinations[index],
                            )
                        },
                        "next_state_probabilities": probability_map,
                        "exit_risk": round(float(exit_risk), 8),
                        "confidence": round(
                            float(max(probabilities[index], default=0.0)),
                            8,
                        ),
                    },
                }
            return result
        values = _predict_values(model, matrix)
        result = {}
        for index, symbol in enumerate(symbols):
            item: dict[str, Any] = {
                "ok": True,
                "model_key": model_key,
                "score": round(float(values[index]), 8),
                "model_type": model_type,
                "artifact_version": payload.get("version"),
            }
            result[symbol] = item
        return result
    weights = dict(payload.get("weights") or {})
    return {
        symbol: {
            "ok": True,
            "model_key": model_key,
            "score": round(sum(
                float(weights.get(key, 0.0) or 0.0)
                * float(features_by_symbol[symbol].get(key, 0.0) or 0.0)
                for key in feature_names
            ), 8),
            "model_type": model_type,
        }
        for symbol in symbols
    }


def _plan_model_horizon(model_key: str) -> int:
    if model_key == "medium_horizon_lambdarank_v1":
        return 60
    if model_key == "leader_lambdarank_v1":
        return 20
    if model_key == "event_impact_assessor_v2":
        return 5
    if model_key == "narrative_transition_multitask_v1":
        return 3
    return 10


def _training_protocol_for_model(model_key: str | None = None) -> dict[str, Any]:
    if model_key == "event_impact_assessor_v2":
        return EVENT_TRAINING_PROTOCOL
    return TRAINING_PROTOCOL


def _required_training_sessions(model_key: str | None = None) -> int:
    protocol = _training_protocol_for_model(model_key)
    return (
        int(protocol["minimum_train_sessions"])
        + int(protocol["purge_sessions"])
        + int(protocol["validation_sessions"])
        + int(protocol["embargo_sessions"])
        + int(protocol["test_sessions"])
        + (int(protocol["walk_forward_windows"]) - 1)
        * int(protocol["roll_step_sessions"])
    )


def _event_walk_forward_date_windows(unique_dates: list[str]) -> list[dict[str, list[str]]]:
    """Build five chronological event-study windows on event dates.

    Event data is sparse by construction, so using the ordinary 756/126/126
    market calendar would reject a valid event history even when thousands of
    company-event observations are present.  Purge and embargo remain in
    event-date units and exceed the five-session target horizon.
    """
    protocol = EVENT_TRAINING_PROTOCOL
    dates = sorted(set(str(value) for value in unique_dates if str(value)))
    required = _required_training_sessions("event_impact_assessor_v2")
    if len(dates) < required:
        raise RuntimeError(
            f"真实事件日期不足以执行正式5窗：实际 {len(dates)}，至少需要 {required}"
        )
    train_min = int(protocol["minimum_train_sessions"])
    validation_size = int(protocol["validation_sessions"])
    test_size = int(protocol["test_sessions"])
    roll_step = int(protocol["roll_step_sessions"])
    purge = int(protocol["purge_sessions"])
    embargo = int(protocol["embargo_sessions"])
    window_count = int(protocol["walk_forward_windows"])
    first_test_start = len(dates) - test_size - (window_count - 1) * roll_step
    windows: list[dict[str, list[str]]] = []
    for offset in range(window_count):
        test_start = first_test_start + offset * roll_step
        validation_end = test_start - embargo
        validation_start = validation_end - validation_size
        train_end = validation_start - purge
        train_dates = dates[:train_end]
        validation_dates = dates[validation_start:validation_end]
        test_dates = dates[test_start:test_start + test_size]
        if (
            len(train_dates) < train_min
            or len(validation_dates) != validation_size
            or len(test_dates) != test_size
        ):
            raise RuntimeError("事件影响模型正式窗口无法满足200/10/10真实事件日期合同")
        windows.append({
            "train": train_dates,
            "validation": validation_dates,
            "test": test_dates,
        })
    return windows


def _event_research_cohort(
    windows: list[dict[str, list[str]]],
) -> dict[str, Any]:
    """Identify the formal event OOS labels independently of model choices."""
    test_windows = [
        {
            "fold": index,
            "test_start": str(window["test"][0]),
            "test_end": str(window["test"][-1]),
            "test_event_dates": len(window["test"]),
        }
        for index, window in enumerate(windows, start=1)
    ]
    payload = {
        "model_key": "event_impact_assessor_v2",
        "event_target_contract": EVENT_TARGET_CONTRACT,
        "training_protocol": EVENT_TRAINING_PROTOCOL["schema_version"],
        "test_windows": test_windows,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return {
        "contract": EVENT_RESEARCH_COHORT_CONTRACT,
        "cohort_key": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "test_windows": test_windows,
        "first_test_date": test_windows[0]["test_start"],
        "last_test_date": test_windows[-1]["test_end"],
    }


def _event_research_cohort_from_metrics(
    metrics: dict[str, Any] | None,
) -> dict[str, Any] | None:
    evidence = (metrics or {}).get("walk_forward_evidence")
    if not isinstance(evidence, list) or len(evidence) != int(
        EVENT_TRAINING_PROTOCOL["walk_forward_windows"]
    ):
        return None
    windows: list[dict[str, list[str]]] = []
    for item in evidence:
        if not isinstance(item, dict):
            return None
        test_start = str(item.get("test_start") or "")
        test_end = str(item.get("test_end") or "")
        test_sessions = int(item.get("test_sessions") or 0)
        if not test_start or not test_end or test_sessions <= 0:
            return None
        # Only the endpoints and count are used by _event_research_cohort.
        windows.append({
            "test": [test_start, *([test_start] * max(0, test_sessions - 2)), test_end]
        })
    return _event_research_cohort(windows)


def _event_targeted_research_cohort_guard(
    model_key: str,
    dataset: dict[str, Any],
    training_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Block adaptive reuse of formal event labels until the cohort advances."""
    context = dict(training_context or {})
    candidate_spec = context.get("model_candidate_spec")
    source_version_id = int(context.get("source_model_version_id") or 0)
    if (
        model_key != "event_impact_assessor_v2"
        or not isinstance(candidate_spec, dict)
        or not candidate_spec
        or source_version_id <= 0
    ):
        return {
            "contract": EVENT_RESEARCH_COHORT_CONTRACT,
            "allowed": True,
            "status": "not_adaptive_event_candidate",
        }
    source = get_model_version(source_version_id)
    if not source or str(source.get("model_key") or "") != model_key:
        return {
            "contract": EVENT_RESEARCH_COHORT_CONTRACT,
            "allowed": False,
            "status": "invalid_source_model_version",
            "source_model_version_id": source_version_id,
        }
    source_cohort = _event_research_cohort_from_metrics(source.get("metrics") or {})
    current_windows = _event_walk_forward_date_windows(
        list(dataset.get("all_dates") or [])
    )
    current_cohort = _event_research_cohort(current_windows)
    if source_cohort is None:
        return {
            "contract": EVENT_RESEARCH_COHORT_CONTRACT,
            "allowed": False,
            "status": "source_formal_oos_cohort_missing",
            "source_model_version_id": source_version_id,
            "current_cohort": current_cohort,
        }
    same_cohort = (
        str(source_cohort.get("cohort_key") or "")
        == str(current_cohort.get("cohort_key") or "")
    )
    source_last_test_date = str(source_cohort.get("last_test_date") or "")
    new_event_dates = sorted({
        str(value)
        for value in (dataset.get("all_dates") or [])
        if str(value) and str(value) > source_last_test_date
    })
    minimum_new_event_dates = int(EVENT_TRAINING_PROTOCOL["test_sessions"])
    complete_new_test_window = len(new_event_dates) >= minimum_new_event_dates
    allowed = bool(not same_cohort and complete_new_test_window)
    return {
        "contract": EVENT_RESEARCH_COHORT_CONTRACT,
        "allowed": allowed,
        "status": (
            "fresh_formal_oos_cohort"
            if allowed
            else "waiting_for_natural_oos_cohort_advance"
        ),
        "source_model_version_id": source_version_id,
        "source_cohort": source_cohort,
        "current_cohort": current_cohort,
        "formal_test_labels_reused": not allowed,
        "new_event_dates_after_source": len(new_event_dates),
        "minimum_new_event_dates": minimum_new_event_dates,
        "complete_new_test_window": complete_new_test_window,
        "policy": (
            "正式五窗结果一旦被用于设计后继候选，同一测试窗口只允许继续做"
            "诊断；至少形成一个完整的全新事件测试窗后才能重新取得正式评价资格。"
        ),
    }


def audit_event_research_cohort_governance(
    *,
    persist: bool = False,
) -> dict[str, Any]:
    """Seal adaptive event candidates that reused feedback from the same OOS cohort."""
    versions = list_model_versions("event_impact_assessor_v2", limit=200)
    cohorts_by_version: dict[int, dict[str, Any]] = {}
    for version in versions:
        cohort = _event_research_cohort_from_metrics(version.get("metrics") or {})
        if cohort:
            cohorts_by_version[int(version["id"])] = cohort
    audited: list[dict[str, Any]] = []
    adaptive_ids: list[int] = []
    for version in reversed(versions):
        version_id = int(version["id"])
        metrics = dict(version.get("metrics") or {})
        cohort = cohorts_by_version.get(version_id)
        if not cohort:
            continue
        context = metrics.get("targeted_training_context")
        context = context if isinstance(context, dict) else {}
        source_id = int(context.get("source_model_version_id") or 0)
        source_cohort = cohorts_by_version.get(source_id)
        same_source_cohort = bool(
            source_id > 0
            and source_cohort
            and str(source_cohort.get("cohort_key") or "")
            == str(cohort.get("cohort_key") or "")
        )
        governance = {
            "contract": EVENT_RESEARCH_COHORT_CONTRACT,
            "allowed": not same_source_cohort,
            "status": (
                "adaptive_same_cohort_non_promotable"
                if same_source_cohort
                else "formal_oos_cohort_not_reused_by_recorded_source"
            ),
            "model_version_id": version_id,
            "source_model_version_id": source_id or None,
            "cohort": cohort,
            "source_cohort": source_cohort or {},
            "formal_test_labels_reused": same_source_cohort,
            "production_change_forbidden": same_source_cohort,
            "policy": (
                "同一正式五窗可继续用于失败归因，但使用其结果设计的后继候选"
                "不得把该五窗再次作为独立上线证据。"
            ),
            "audited_at": datetime.now().astimezone().isoformat(),
        }
        if same_source_cohort:
            adaptive_ids.append(version_id)
        if persist:
            metrics["event_research_cohort"] = cohort
            metrics["event_research_cohort_governance"] = governance
            update_model_version_metrics(version_id, metrics=metrics)
            if same_source_cohort:
                validation = dict(version.get("validation") or {})
                eligibility = dict(validation.get("production_eligibility") or {})
                gates = dict(eligibility.get("gates") or {})
                gates["fresh_unconsumed_formal_oos_cohort"] = False
                blocked = list(eligibility.get("blocked_reasons") or [])
                reason = "event_formal_oos_cohort_reused_after_feedback"
                if reason not in blocked:
                    blocked.append(reason)
                eligibility.update({
                    "eligible": False,
                    "blocked_reasons": blocked,
                    "gates": gates,
                })
                validation["production_eligible"] = False
                validation["production_eligibility"] = eligibility
                update_model_version_validation(version_id, validation=validation)
        audited.append(governance)
    cohort_groups: dict[str, list[int]] = {}
    for version_id, cohort in cohorts_by_version.items():
        cohort_groups.setdefault(str(cohort["cohort_key"]), []).append(version_id)
    return {
        "ok": True,
        "contract": EVENT_RESEARCH_COHORT_CONTRACT,
        "persisted": bool(persist),
        "audited_versions": len(audited),
        "adaptive_same_cohort_version_ids": sorted(adaptive_ids),
        "cohorts": [
            {
                "cohort_key": key,
                "model_version_ids": sorted(version_ids),
                "candidate_count": len(version_ids),
            }
            for key, version_ids in sorted(cohort_groups.items())
        ],
        "versions": audited,
    }


def _walk_forward_date_windows(unique_dates: list[str]) -> list[dict[str, list[str]]]:
    dates = sorted(set(str(value) for value in unique_dates))
    required = _required_training_sessions()
    if len(dates) < required:
        raise RuntimeError(
            f"训练交易日不足以执行正式5窗：实际 {len(dates)}，至少需要 {required}"
        )
    train_min = int(TRAINING_PROTOCOL["minimum_train_sessions"])
    validation_size = int(TRAINING_PROTOCOL["validation_sessions"])
    test_size = int(TRAINING_PROTOCOL["test_sessions"])
    roll_step = int(TRAINING_PROTOCOL["roll_step_sessions"])
    purge = int(TRAINING_PROTOCOL["purge_sessions"])
    embargo = int(TRAINING_PROTOCOL["embargo_sessions"])
    window_count = int(TRAINING_PROTOCOL["walk_forward_windows"])
    first_test_start = len(dates) - test_size - (window_count - 1) * roll_step
    windows: list[dict[str, list[str]]] = []
    for offset in range(window_count):
        test_start = first_test_start + offset * roll_step
        validation_end = test_start - embargo
        validation_start = validation_end - validation_size
        train_end = validation_start - purge
        train_dates = dates[:train_end]
        validation_dates = dates[validation_start:validation_end]
        test_dates = dates[test_start:test_start + test_size]
        if (
            len(train_dates) < train_min
            or len(validation_dates) != validation_size
            or len(test_dates) != test_size
        ):
            raise RuntimeError("正式Walk-forward窗口无法满足756/126/126交易日合同")
        windows.append({
            "train": train_dates,
            "validation": validation_dates,
            "test": test_dates,
        })
    return windows


def _build_plan_training_dataset(
    model_key: str,
    *,
    max_sessions: int = 1_800,
    max_rows: int = 600_000,
    max_symbols_per_session: int = 350,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    from app.data_platform.db import _conn as data_conn

    horizon = _plan_model_horizon(model_key)
    if model_key == "event_impact_assessor_v2":
        # Event observations are sparse across symbols.  The ordinary
        # cross-sectional 350-symbol cap silently dropped valid event dates
        # from the five-window calendar, so the event contract uses a larger
        # deterministic sample while retaining a bounded query budget.
        max_symbols_per_session = max(500, int(max_symbols_per_session))
        max_rows = max(900_000, int(max_rows))
    date_limit = max_sessions + horizon + 5
    if on_progress:
        on_progress("dataset", 0.24, f"读取最近 {date_limit} 个交易日训练日历")
    coverage_news_rows: list[Any] = []
    with data_conn() as c:
        date_rows = c.execute(
            """SELECT trade_date
               FROM dc_available_trade_dates
               ORDER BY trade_date DESC
               LIMIT ?""",
            (date_limit,),
        ).fetchall()
        dates = [str(dict(row).get("trade_date") or "") for row in date_rows if dict(row).get("trade_date")]
        if len(dates) < max(80, horizon + 20):
            raise RuntimeError(f"训练日历不足：{len(dates)} sessions")
        start_date = min(dates)
        if on_progress:
            on_progress("dataset", 0.28, f"日历覆盖 {len(dates)} 天，起始 {start_date}，预测周期 {horizon} 天")
        ascending_dates = sorted(dates)
        mapped_dates = [(ascending_dates[idx], ascending_dates[idx + horizon]) for idx in range(0, max(0, len(ascending_dates) - horizon))]
        if not mapped_dates:
            raise RuntimeError(f"训练日历无法构造 {horizon} 天未来收益标签")
        required_sessions = _required_training_sessions(model_key)
        if len(mapped_dates) < required_sessions:
            raise RuntimeError(
                f"可标注训练交易日不足：实际 {len(mapped_dates)}，正式5窗至少需要 {required_sessions}"
            )
        daily_sample_limit = min(
            max(1, int(max_symbols_per_session)),
            max(1, int(max_rows) // len(mapped_dates)),
        )
        values_sql = ",".join(["(?, ?)"] * len(mapped_dates))
        values_params = [value for pair in mapped_dates for value in pair]
        # ``data_conn`` applies the shared 30s PostgreSQL statement budget on
        # every session.  Do not override it here: this dataset is intentionally
        # executed by the persistent research task in bounded retries, and a
        # two-minute session override lets one full-history scan monopolise
        # ``alis_data`` and starve the API/control-plane workload.  The task
        # checkpoint/retry contract is the recovery mechanism for a timeout;
        # callers must not turn this into an unbounded synchronous query.
        sampled_symbols: list[str] = []
        symbol_sampling = {
            "contract": "not_applicable_for_theme_level_training",
            "selected_symbol_count": 0,
        }
        if model_key != "narrative_transition_multitask_v1":
            if model_key == "event_impact_assessor_v2":
                sampled_symbols, symbol_sampling = _event_training_symbol_sample(
                    c,
                    start_date=start_date,
                    end_date=ascending_dates[-1],
                    cutoff=(
                        f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-"
                        f"{ascending_dates[-1][6:]}T23:59:59"
                    ),
                    limit=daily_sample_limit,
                )
            else:
                sampled_symbols, symbol_sampling = _training_symbol_sample(
                    c,
                    ascending_dates,
                    limit=daily_sample_limit,
                )
            symbol_placeholders = ",".join("?" for _ in sampled_symbols)
        if model_key == "narrative_transition_multitask_v1":
            raw_rows = c.execute(
                f"""
                WITH date_map(trade_date, future_date) AS (VALUES {values_sql}),
                theme_members AS (
                    SELECT m.trade_date, m.future_date, tm.theme_code, tm.ts_code
                    FROM date_map m
                    JOIN dc_theme_company_mapping tm
                      ON tm.as_of_date=m.trade_date
                ),
                current_theme AS (
                    SELECT x.trade_date, x.theme_code,
                           AVG(d.close) AS close,
                           AVG(COALESCE(d.change_pct,0)) AS change_pct,
                           AVG(COALESCE(d.turnover,0)) AS turnover
                    FROM theme_members x
                    JOIN dc_stock_daily_snapshot d
                      ON d.trade_date=x.trade_date AND d.ts_code=x.ts_code
                    GROUP BY x.trade_date,x.theme_code
                ),
                future_theme AS (
                    SELECT x.trade_date, x.future_date, x.theme_code,
                           AVG(d.close) AS future_close,
                           AVG(COALESCE(d.change_pct,0)) AS future_change_pct,
                           AVG(COALESCE(d.turnover,0)) AS future_turnover
                    FROM theme_members x
                    JOIN dc_stock_daily_snapshot d
                      ON d.trade_date=x.future_date AND d.ts_code=x.ts_code
                    GROUP BY x.trade_date,x.future_date,x.theme_code
                )
                SELECT
                    'THEME:' || s.theme_code AS ts_code,
                    s.as_of_date AS trade_date,
                    m.future_date,
                    COALESCE(ct.close, 0.0) AS close,
                    COALESCE(ct.change_pct, 0.0) AS change_pct,
                    COALESCE(ct.turnover, 0.0) AS turnover,
                    0.0 AS turnover_rate,
                    0.0 AS volume,
                    NULL AS pe_ttm,
                    NULL AS pb,
                    NULL AS total_mv,
                    100.0 AS data_quality_score,
                    COALESCE(ft.future_close, 0.0) AS future_close,
                    COALESCE(ft.future_change_pct, 0.0) AS future_change_pct,
                    0.0 AS future_turnover_rate,
                    0.0 AS momentum_20,
                    0.0 AS momentum_60,
                    0.0 AS volatility_20,
                    0.0 AS turnover_slope
                FROM dc_theme_snapshot s
                JOIN date_map m ON m.trade_date=s.as_of_date
                LEFT JOIN current_theme ct
                  ON ct.trade_date=s.as_of_date AND ct.theme_code=s.theme_code
                LEFT JOIN future_theme ft
                  ON ft.trade_date=s.as_of_date AND ft.future_date=m.future_date
                 AND ft.theme_code=s.theme_code
                WHERE s.as_of_date>=?
                ORDER BY s.as_of_date,s.theme_code
                """,
                (*values_params, start_date),
            ).fetchall()
        else:
            raw_rows = c.execute(
                f"""
                WITH date_map(trade_date, future_date) AS (VALUES {values_sql}),
                ordered AS (
                    SELECT
                        b.*,
                        LAG(b.close, 20) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                        ) AS close_20d_ago,
                        LAG(b.close, 60) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                        ) AS close_60d_ago,
                        STDDEV_POP(COALESCE(b.change_pct, 0) / 100.0) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                        ) AS volatility_20d,
                        AVG(COALESCE(b.turnover_rate, 0)) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                        ) AS turnover_avg_20d,
                        AVG(COALESCE(b.turnover_rate, 0)) OVER (
                            PARTITION BY b.ts_code ORDER BY b.trade_date
                            ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                        ) AS turnover_avg_60d
                    FROM dc_stock_daily_snapshot b
                    WHERE b.ts_code IN ({symbol_placeholders})
                      AND b.trade_date <= ?
                )
                SELECT
                    b.ts_code,
                    b.trade_date,
                    m.future_date,
                    b.close,
                    b.change_pct,
                    b.turnover,
                    b.turnover_rate,
                    b.volume,
                    b.pe_ttm,
                    b.pb,
                    b.total_mv,
                    b.data_quality_score,
                    f.close AS future_close,
                    f.change_pct AS future_change_pct,
                    f.turnover_rate AS future_turnover_rate,
                    CASE WHEN b.close_20d_ago > 0 THEN b.close / b.close_20d_ago - 1.0 END AS momentum_20,
                    CASE WHEN b.close_60d_ago > 0 THEN b.close / b.close_60d_ago - 1.0 END AS momentum_60,
                    b.volatility_20d AS volatility_20,
                    CASE WHEN b.turnover_avg_60d > 0
                         THEN b.turnover_avg_20d / b.turnover_avg_60d - 1.0
                         ELSE 0.0 END AS turnover_slope
                FROM ordered b
                JOIN date_map m ON m.trade_date = b.trade_date
                JOIN dc_stock_daily_snapshot f
                    ON f.ts_code = b.ts_code AND f.trade_date = m.future_date
                WHERE b.trade_date >= ?
                  AND b.close IS NOT NULL AND b.close > 0
                  AND f.close IS NOT NULL AND f.close > 0
                  AND COALESCE(b.is_suspended, 0) = 0
                ORDER BY b.trade_date ASC, b.ts_code ASC
                """,
                (
                    *values_params,
                    *sampled_symbols,
                    ascending_dates[-1],
                    start_date,
                ),
            ).fetchall()
        benchmark_rows = []
        if model_key == "event_impact_assessor_v2":
            try:
                benchmark_rows = c.execute(
                    """SELECT id,trade_date,payload_json,available_time
                       FROM dc_tushare_raw_records
                       WHERE api_name='index_daily'
                         AND ts_code IN ('000300.SH','000300')
                         AND trade_date>=? AND trade_date<=?
                       ORDER BY trade_date,ts_code,id""",
                    (ascending_dates[0], ascending_dates[-1]),
                ).fetchall()
            except Exception:
                benchmark_rows = []
        try:
            state_rows = c.execute(
                """SELECT trade_date,market_state,observations_json,result_json
                   FROM dc_market_context_snapshot
                   WHERE trade_date>=? AND trade_date<=?""",
                (ascending_dates[0], ascending_dates[-1]),
            ).fetchall()
        except Exception:
            state_rows = []
        try:
            market_feature_rows = c.execute(
                """SELECT feature_date,features_json
                   FROM model_training_history
                   WHERE feature_set='market_regime_full_market_v2'
                     AND feature_date>=? AND feature_date<=?
                   ORDER BY feature_date""",
                (ascending_dates[0], ascending_dates[-1]),
            ).fetchall()
        except Exception:
            market_feature_rows = []
        try:
            if model_key == "narrative_transition_multitask_v1":
                theme_rows = c.execute(
                    """SELECT as_of_date,'' AS ts_code,theme_code,
                              score/100.0 AS theme_score,
                              lifecycle,payload_json,model_version
                       FROM dc_theme_snapshot
                       WHERE as_of_date>=? AND as_of_date<=?
                         AND model_version=?
                       ORDER BY as_of_date,theme_code""",
                    (ascending_dates[0], ascending_dates[-1], "industry_price_fundamental_theme_v3"),
                ).fetchall()
            else:
                theme_symbol_placeholders = ",".join(
                    "?" for _ in sampled_symbols
                )
                theme_rows = c.execute(
                    f"""SELECT m.as_of_date,m.ts_code,m.theme_code,
                              s.score/100.0 AS theme_score,
                              s.lifecycle,s.payload_json,s.model_version
                       FROM dc_theme_company_mapping m
                       JOIN dc_theme_snapshot s
                         ON s.as_of_date=m.as_of_date AND s.theme_code=m.theme_code
                       WHERE m.as_of_date>=? AND m.as_of_date<=?
                         AND m.ts_code IN ({theme_symbol_placeholders})
                       ORDER BY m.as_of_date,m.ts_code,s.score DESC,m.theme_code""",
                    (
                        ascending_dates[0],
                        ascending_dates[-1],
                        *sampled_symbols,
                    ),
                ).fetchall()
        except Exception as exc:
            raise PlanModelTrainingDataError(
                "theme_mapping_query_failed",
                str(exc)[:300],
            ) from exc
        event_rows = []
        structured_event_rows: list[Any] = []
        news_rows = []
        top_list_rows = []
        analyst_report_rows: list[Any] = []
        if model_key == "event_impact_assessor_v2":
            try:
                event_rows = c.execute(
                    """SELECT r.ts_code,REPLACE(SUBSTRING(r.publish_time,1,10),'-','') AS event_date,
                              r.available_time,
                              r.title,r.raw_text,e.event_type,e.impact_direction,
                              e.severity,e.confidence,e.summary,e.metrics_json
                       FROM dc_announcement_event e
                       JOIN dc_announcement_raw r ON r.id=e.announcement_id
                       WHERE r.available_time<=? AND r.ts_code<>''
                       ORDER BY r.available_time,r.ts_code,r.id,e.id""",
                    (f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-{ascending_dates[-1][6:]}T23:59:59",),
                ).fetchall()
            except Exception:
                event_rows = []
            try:
                symbol_placeholders = ",".join("?" for _ in sampled_symbols)
                for api_name in STRUCTURED_DISCLOSURE_APIS:
                    structured_event_rows.extend(
                        c.execute(
                            f"""SELECT id,api_name,ts_code,trade_date,period,ann_date,
                                       available_time,payload_json
                                FROM dc_tushare_raw_records
                                WHERE api_name=?
                                  AND ts_code IN ({symbol_placeholders})
                                  AND available_time<=?
                                ORDER BY available_time,id""",
                            (
                                api_name,
                                *sampled_symbols,
                                f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-"
                                f"{ascending_dates[-1][6:]}T23:59:59",
                            ),
                        ).fetchall()
                    )
            except Exception:
                structured_event_rows = []
            try:
                news_rows = c.execute(
                    """SELECT payload_json,available_time
                       FROM dc_tushare_raw_records
                       WHERE api_name='major_news'
                         AND available_time>=?
                         AND available_time<=?
                       ORDER BY available_time,id""",
                    (
                        f"{ascending_dates[0][:4]}-{ascending_dates[0][4:6]}-{ascending_dates[0][6:]}",
                        f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-{ascending_dates[-1][6:]}T23:59:59",
                    ),
                ).fetchall()
            except Exception:
                news_rows = []
            try:
                symbol_placeholders = ",".join("?" for _ in sampled_symbols)
                top_list_rows = c.execute(
                    f"""SELECT id,ts_code,trade_date,available_time,payload_json
                        FROM dc_tushare_raw_records
                        WHERE api_name='top_list'
                          AND ts_code IN ({symbol_placeholders})
                          AND trade_date>=? AND trade_date<=?
                          AND available_time<=?
                        ORDER BY trade_date,ts_code,id""",
                    (
                        *sampled_symbols,
                        ascending_dates[0],
                        ascending_dates[-1],
                        f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-"
                        f"{ascending_dates[-1][6:]}T23:59:59",
                    ),
                ).fetchall()
            except Exception:
                top_list_rows = []
            try:
                analyst_report_start = (
                    datetime.strptime(ascending_dates[0], "%Y%m%d")
                    - timedelta(days=200)
                ).strftime("%Y%m%d")
                analyst_report_rows = c.execute(
                    f"""SELECT ts_code,trade_date,available_time,payload_json
                        FROM dc_tushare_raw_records
                        WHERE api_name='report_rc'
                          AND ts_code IN ({symbol_placeholders})
                          AND trade_date>=? AND trade_date<=?
                          AND available_time<=?
                        ORDER BY ts_code,trade_date,id""",
                    (
                        *sampled_symbols,
                        analyst_report_start,
                        ascending_dates[-1],
                        f"{ascending_dates[-1][:4]}-{ascending_dates[-1][4:6]}-"
                        f"{ascending_dates[-1][6:]}T23:59:59",
                    ),
                ).fetchall()
            except Exception:
                analyst_report_rows = []
        moneyflow_rows = _load_training_moneyflow_rows(
            c,
            model_key=model_key,
            start_date=ascending_dates[0],
            end_date=ascending_dates[-1],
            symbols=sampled_symbols,
        )
    market_state_by_date: dict[str, str] = {}
    market_profile_by_date: dict[str, str] = {}
    for row in state_rows:
        item = dict(row)
        trade_date = str(item.get("trade_date") or "")
        market_state_by_date[trade_date] = str(item.get("market_state") or "unknown")
        result_payload = item.get("result_json") or {}
        observations = item.get("observations_json") or {}
        for name, value in (("result", result_payload), ("observations", observations)):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    value = {}
            if name == "result":
                result_payload = value if isinstance(value, dict) else {}
            else:
                observations = value if isinstance(value, dict) else {}
        profile = (result_payload.get("market_profile") or {}) if isinstance(result_payload, dict) else {}
        profile_code = str((profile or {}).get("code") or "")
        if not profile_code and observations:
            try:
                from app.services.market_context import classify_market_profile
                profile_code = str(
                    classify_market_profile(market_state_by_date[trade_date], observations).get("code")
                    or ""
                )
            except Exception:
                profile_code = ""
        market_profile_by_date[trade_date] = profile_code or "unknown"
    if market_feature_rows:
        try:
            from app.services.market_context import (
                _transparent_regime,
                classify_market_profile,
                score_market_sentiment,
            )
            previous_state = ""
            for row in market_feature_rows:
                item = dict(row)
                trade_date = str(item.get("feature_date") or "")
                features = item.get("features_json") or {}
                if isinstance(features, str):
                    try:
                        features = json.loads(features)
                    except Exception:
                        features = {}
                if not isinstance(features, dict):
                    continue
                observations = {
                    "breadth": float(features.get("market_breadth") or 0.5),
                    "average_return": float(features.get("market_average_return") or 0.0),
                    "volume_ratio": float(features.get("market_amount_ratio_20d") or 1.0),
                    "limit_up_ratio": float(features.get("market_limit_up_ratio") or 0.0),
                    "limit_down_ratio": float(features.get("market_limit_down_ratio") or 0.0),
                    "northbound_flow_z": 0.0,
                    "leader_persistence": 0.0,
                    "price_fundamental_divergence": 0.0,
                    "crowding": 0.0,
                }
                sentiment = score_market_sentiment(**observations)
                state = _transparent_regime(
                    float(sentiment.get("score") or 50.0),
                    observations["average_return"],
                    previous_state,
                )
                profile = classify_market_profile(state, observations)
                market_state_by_date[trade_date] = state
                market_profile_by_date[trade_date] = str(profile.get("code") or "unknown")
                previous_state = state
        except Exception:
            pass
    benchmark_close_by_date: dict[str, float] = {}
    for row in benchmark_rows:
        item = dict(row)
        payload = _payload_dict(item.get("payload_json"))
        try:
            close = float(payload.get("close") or 0.0)
        except (TypeError, ValueError):
            close = 0.0
        if close > 0:
            benchmark_close_by_date[str(item.get("trade_date") or "")] = close
    benchmark_return_by_date: dict[str, float] = {}
    for trade_date, future_date in mapped_dates:
        current = benchmark_close_by_date.get(trade_date)
        future = benchmark_close_by_date.get(future_date)
        if current and future:
            benchmark_return_by_date[trade_date] = float(future / current - 1.0)
    benchmark_daily_return_by_date: dict[str, float] = {}
    for index, trade_date in enumerate(ascending_dates):
        if index == 0:
            continue
        previous = benchmark_close_by_date.get(ascending_dates[index - 1])
        current = benchmark_close_by_date.get(trade_date)
        if previous and current:
            benchmark_daily_return_by_date[trade_date] = float(current / previous - 1.0)

    primary_theme_by_symbol_date: dict[
        tuple[str, str], tuple[float, str, str]
    ] = {}
    lifecycle_by_theme_date: dict[tuple[str, str], str] = {}
    lifecycle_code_by_theme_date: dict[tuple[str, str], int] = {}
    theme_observation_by_theme_date: dict[tuple[str, str], dict[str, Any]] = {}
    theme_score_by_theme_date: dict[tuple[str, str], float] = {}
    distinct_theme_rows: dict[tuple[str, str], dict[str, Any]] = {}
    persisted_lifecycle_counts: Counter[str] = Counter()
    for row in theme_rows:
        item = dict(row)
        theme_date = str(item.get("as_of_date") or "")
        theme_code = str(item.get("theme_code") or "")
        theme_score = float(item.get("theme_score") or 0.0)
        key = (theme_code, theme_date)
        if key not in distinct_theme_rows:
            distinct_theme_rows[key] = item
            persisted_lifecycle_counts[str(item.get("lifecycle") or "")] += 1
        theme_score_by_theme_date[(theme_code, theme_date)] = theme_score
    candidate_lifecycle_counts: Counter[int] = Counter()
    canonical_lifecycle_counts: Counter[int] = Counter()
    path_adjustment_count = 0
    observability_by_date: dict[str, list[bool]] = {}
    previous_state_by_theme: dict[str, int] = {}
    previous_observation_by_theme: dict[str, dict[str, Any]] = {}
    state_age_by_theme: dict[str, int] = {}
    for (theme_code, theme_date), item in sorted(
        distinct_theme_rows.items(),
        key=lambda value: (value[0][0], value[0][1]),
    ):
        theme_score = float(item.get("theme_score") or 0.0)
        observation = _canonical_narrative_observation(
            lifecycle=str(item.get("lifecycle") or ""),
            score=theme_score * 100.0,
            payload=_payload_dict(item.get("payload_json")),
        )
        candidate = int(observation["candidate_state"])
        observability_by_date.setdefault(theme_date, []).append(
            bool(observation.get("observable"))
        )
        previous_state = previous_state_by_theme.get(theme_code)
        previous_observation = previous_observation_by_theme.get(theme_code) or {}
        previous_candidate = int(previous_observation.get("candidate_state") or previous_state or 0)
        candidate_streak = (
            int(previous_observation.get("candidate_streak") or 0) + 1
            if previous_candidate == candidate
            else 1
        )
        previous_age = int(state_age_by_theme.get(theme_code) or 0)
        canonical_name, transition_meta = narrative_transition_state(
            NARRATIVE_LIFECYCLE_CLASSES[previous_state]
            if previous_state is not None
            else None,
            NARRATIVE_LIFECYCLE_CLASSES[candidate],
            previous_age=previous_age,
            candidate_streak=candidate_streak,
        )
        canonical = NARRATIVE_LIFECYCLE_CLASSES.index(canonical_name)
        candidate_lifecycle_counts[candidate] += 1
        canonical_lifecycle_counts[canonical] += 1
        path_adjustment_count += int(canonical != candidate)
        if previous_state == canonical:
            state_age_by_theme[theme_code] = state_age_by_theme.get(theme_code, 0) + 1
        else:
            state_age_by_theme[theme_code] = 1
        observation.update({
            "state": canonical,
            "state_age": state_age_by_theme[theme_code],
            "candidate_state": candidate,
            "candidate_streak": candidate_streak,
            "transition_contract": {
                "schema_version": NARRATIVE_V3_SCHEMA_VERSION,
                "minimum_state_sessions": dict(NARRATIVE_V3_MIN_STATE_SESSIONS),
                "confirmation_sessions": NARRATIVE_V3_CONFIRMATION_SESSIONS,
                **transition_meta,
            },
            "score_slope": theme_score - float(
                previous_observation.get("theme_score") or theme_score
            ),
            "breadth_slope": float(observation["breadth"]) - float(
                previous_observation.get("breadth") or observation["breadth"]
            ),
            "theme_score": theme_score,
            "path_adjusted": canonical != candidate,
        })
        lifecycle_code_by_theme_date[(theme_code, theme_date)] = canonical
        lifecycle_by_theme_date[(theme_code, theme_date)] = (
            NARRATIVE_LIFECYCLE_CLASSES[canonical]
        )
        theme_observation_by_theme_date[(theme_code, theme_date)] = observation
        previous_state_by_theme[theme_code] = canonical
        previous_observation_by_theme[theme_code] = observation

    unobservable_theme_dates = {
        theme_date
        for theme_date, flags in observability_by_date.items()
        if flags and sum(not value for value in flags) / len(flags) >= .80
    }

    for (theme_code, theme_date), lifecycle in lifecycle_by_theme_date.items():
        primary_theme_by_symbol_date[(f"THEME:{theme_code}", theme_date)] = (
            float(theme_score_by_theme_date.get((theme_code, theme_date), 0.0)),
            lifecycle,
            theme_code,
        )

    for row in sorted(
        theme_rows,
        key=lambda value: (
            str(dict(value).get("as_of_date") or ""),
            str(dict(value).get("theme_code") or ""),
            -float(dict(value).get("theme_score") or 0.0),
        ),
    ):
        item = dict(row)
        theme_symbol = str(item.get("ts_code") or "")
        theme_date = str(item.get("as_of_date") or "")
        theme_code = str(item.get("theme_code") or "")
        theme_score = float(item.get("theme_score") or 0.0)
        lifecycle = lifecycle_by_theme_date.get((theme_code, theme_date), "")
        key = (theme_symbol, theme_date)
        previous = primary_theme_by_symbol_date.get(key)
        if previous is None or theme_score > float(previous[0]):
            primary_theme_by_symbol_date[key] = (
                theme_score,
                lifecycle,
                theme_code,
            )
    theme_pressure_by_theme_date = {
        key: max(-1.0, min(1.0, float(value.get("score_slope") or 0.0)))
        for key, value in theme_observation_by_theme_date.items()
    }
    theme_snapshot_dates = {
        str(dict(row).get("as_of_date") or "")
        for row in theme_rows
        if dict(row).get("as_of_date")
    }
    event_by_symbol_date: dict[tuple[str, str], dict[str, Any]] = {}
    top_list_by_symbol_date: dict[tuple[str, str], tuple[float, float]] = {}
    analyst_consensus_index = build_analyst_consensus_index(analyst_report_rows)
    for row in event_rows:
        item = dict(row)
        available_time = str(item.get("available_time") or "")
        event_date = str(item.get("event_date") or "")
        usable_date = _event_usable_trade_date(event_date, available_time, ascending_dates)
        key = (str(item.get("ts_code") or ""), usable_date)
        if not key[0] or not usable_date:
            continue
        candidate = structured_event_candidate(
            source="announcement",
            event_type=str(item.get("event_type") or ""),
            direction=str(item.get("impact_direction") or "neutral"),
            severity=str(item.get("severity") or "medium"),
            confidence=float(item.get("confidence") or 0.0),
            title=str(item.get("title") or item.get("summary") or ""),
            text=str(item.get("raw_text") or item.get("summary") or ""),
            metrics=item.get("metrics_json"),
        )
        event_by_symbol_date[key] = merge_event_candidates(
            event_by_symbol_date.get(key), candidate,
        )

    guidance_by_symbol_period: dict[
        tuple[str, str], dict[str, float | None]
    ] = {}
    for row in sorted(
        structured_event_rows,
        key=lambda value: (
            str(dict(value).get("available_time") or ""),
            str(dict(value).get("ts_code") or ""),
            str(dict(value).get("api_name") or ""),
            int(dict(value).get("id") or 0),
        ),
    ):
        item = dict(row)
        payload = payload_dict(item.get("payload_json"))
        api_name = str(item.get("api_name") or "")
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        available_time = str(item.get("available_time") or "")
        event_time = str(
            item.get("ann_date")
            or payload.get("ann_date")
            or available_time
        )
        usable_date = _event_usable_trade_date(
            event_time,
            available_time,
            ascending_dates,
        )
        if not symbol or not usable_date:
            continue
        period = str(
            item.get("period")
            or payload.get("end_date")
            or payload.get("period")
            or ""
        )
        period_key = report_period_key(period)
        prior_guidance = guidance_by_symbol_period.get((symbol, period_key))
        analyst_consensus = point_in_time_analyst_consensus(
            analyst_consensus_index,
            symbol=symbol,
            period=period_key,
            as_of_date=usable_date,
        )
        expectation_gap, expectation_basis, current_guidance = (
            disclosure_expectation_gap(
                api_name=api_name,
                payload=payload,
                consensus=analyst_consensus,
                prior_guidance=prior_guidance,
            )
        )
        if api_name == "forecast" and period_key:
            guidance_by_symbol_period[(symbol, period_key)] = current_guidance
        candidate = structured_event_candidate(
            source="structured_disclosure",
            api_name=api_name,
            confidence=0.98,
            title=api_name,
            payload=payload,
            expectation_gap=expectation_gap,
            expectation_basis=expectation_basis,
            analyst_consensus=analyst_consensus,
        )
        key = (symbol, usable_date)
        event_by_symbol_date[key] = merge_event_candidates(
            event_by_symbol_date.get(key), candidate,
        )
    if news_rows:
        from app.services.news_theme_cluster import _match_companies
        for row in news_rows:
            item = dict(row)
            payload = _payload_dict(item.get("payload_json"))
            title = str(payload.get("title") or "")
            content = str(payload.get("content") or "")
            text = f"{title} {content}".strip()
            if not text:
                continue
            event_time = str(payload.get("pub_time") or "")
            available_time = str(item.get("available_time") or event_time)
            usable_date = _event_usable_trade_date(event_time, available_time, ascending_dates)
            if not usable_date:
                continue
            direction, severity, confidence = _event_news_direction(text)
            for company in _match_companies(text):
                key = (str(company.get("ts_code") or ""), usable_date)
                if not key[0]:
                    continue
                candidate = structured_event_candidate(
                    source="major_news",
                    direction=direction,
                    severity=severity,
                    confidence=confidence,
                    title=title,
                    text=content,
                )
                event_by_symbol_date[key] = merge_event_candidates(
                    event_by_symbol_date.get(key), candidate,
                )

    analyst_reports_by_symbol: dict[str, list[dict[str, Any]]] = {}
    rating_scores = {"买入": 5.0, "增持": 4.0, "中性": 3.0, "减持": 2.0, "卖出": 1.0}
    for row in analyst_report_rows:
        item = dict(row)
        payload = payload_dict(item.get("payload_json"))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        report_date = str(item.get("trade_date") or "")[:8]
        available_date = str(item.get("available_time") or "")[:10].replace("-", "")
        rating = rating_scores.get(str(payload.get("rating") or ""))
        targets = [
            float(payload[key])
            for key in ("min_price", "max_price")
            if payload.get(key) is not None
        ]
        if not symbol or not report_date or available_date > report_date:
            continue
        analyst_reports_by_symbol.setdefault(symbol, []).append({
            "trade_date": report_date,
            "rating": rating,
            "target": sum(targets) / len(targets) if targets else None,
        })
    for rows in analyst_reports_by_symbol.values():
        rows.sort(key=lambda value: str(value.get("trade_date") or ""))

    def _analyst_context(symbol: str, trade_date: str) -> tuple[float | None, float | None]:
        rows = analyst_reports_by_symbol.get(symbol) or []
        cutoff_date = datetime.strptime(trade_date, "%Y%m%d").date()
        recent_start = (cutoff_date - timedelta(days=27)).strftime("%Y%m%d")
        prior_start = (cutoff_date - timedelta(days=55)).strftime("%Y%m%d")
        recent = [
            float(item["rating"])
            for item in rows
            if item.get("rating") is not None
            and recent_start <= str(item["trade_date"]) <= trade_date
        ]
        prior = [
            float(item["rating"])
            for item in rows
            if item.get("rating") is not None
            and prior_start <= str(item["trade_date"]) < recent_start
        ]
        revision = (
            sum(recent) / len(recent) - sum(prior) / len(prior)
            if recent and prior else None
        )
        targets = [
            float(item["target"])
            for item in rows
            if item.get("target") is not None
            and prior_start <= str(item["trade_date"]) <= trade_date
        ]
        return revision, (sum(targets) / len(targets) if targets else None)
    for row in top_list_rows:
        item = dict(row)
        payload = _payload_dict(item.get("payload_json"))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        event_time = str(item.get("trade_date") or payload.get("trade_date") or "")
        available_time = str(item.get("available_time") or "")
        usable_date = _event_usable_trade_date(
            event_time,
            available_time,
            ascending_dates,
        )
        if not symbol or not usable_date:
            continue
        try:
            net_rate = float(payload.get("net_rate") or 0.0)
        except (TypeError, ValueError):
            net_rate = 0.0
        try:
            pct_change = float(payload.get("pct_change") or 0.0)
        except (TypeError, ValueError):
            pct_change = 0.0
        # Structured top-list observations are available at the closing
        # timestamp.  The bounded signal uses only same-day net-flow and
        # price-reaction fields; it never reads the future return label.
        net_signal = float(np.tanh(net_rate / TOP_LIST_NET_RATE_SCALE))
        price_signal = float(np.tanh(pct_change / TOP_LIST_PRICE_CHANGE_SCALE))
        sentiment = max(-1.0, min(1.0, 0.7 * net_signal + 0.3 * price_signal))
        confidence = min(0.95, 0.70 + 0.25 * abs(net_signal))
        key = (symbol, usable_date)
        previous = top_list_by_symbol_date.get(key, (0.0, 0.0))
        if (confidence, abs(sentiment), sentiment) > (
            previous[1],
            abs(previous[0]),
            previous[0],
        ):
            top_list_by_symbol_date[key] = (sentiment, confidence)
    moneyflow_by_symbol_date = _moneyflow_observations(moneyflow_rows, ascending_dates)
    if on_progress:
        on_progress("dataset", 0.31, f"取回候选训练截面 {len(raw_rows)} 行，开始构造特征")
    samples: list[dict[str, Any]] = []
    for row in raw_rows:
        item = dict(row)
        close = float(item.get("close") or 0.0)
        future_close = float(item.get("future_close") or 0.0)
        if close <= 0 or future_close <= 0:
            continue
        if (
            item.get("momentum_20") is None
            or item.get("momentum_60") is None
            or item.get("volatility_20") is None
            or item.get("turnover_slope") is None
        ):
            continue
        momentum_20 = float(item.get("momentum_20") or 0.0)
        momentum_60 = float(item.get("momentum_60") or 0.0)
        volatility_20 = float(item.get("volatility_20") or 0.0)
        turnover_slope = float(item.get("turnover_slope") or 0.0)
        moneyflow = moneyflow_by_symbol_date.get((str(item.get("ts_code") or ""), str(item.get("trade_date") or "")))
        capital_persistence = (
            min(
                3.0,
                max(
                    -3.0,
                    float(moneyflow["net_flow_z"]) * 0.35
                    + float(moneyflow["positive_ratio_5"]) * 0.35
                    + float(moneyflow["positive_ratio_20"]) * 0.2
                    + float(moneyflow["large_order_balance"]) * 0.1,
                ),
            )
            if moneyflow
            else 0.0
        )
        narrative_strength = min(3.0, max(-3.0, momentum_60 - volatility_20))
        symbol = str(item.get("ts_code") or "")
        trade_date = str(item.get("trade_date") or "")
        future_date = str(item.get("future_date") or "")
        if (
            model_key == "narrative_transition_multitask_v1"
            and (
                trade_date not in theme_snapshot_dates
                or future_date not in theme_snapshot_dates
                or trade_date in unobservable_theme_dates
                or future_date in unobservable_theme_dates
            )
        ):
            continue
        theme_score, lifecycle, theme_code = primary_theme_by_symbol_date.get(
            (symbol, trade_date), (0.0, "", "")
        )
        if model_key == "leader_lambdarank_v1" and not theme_code:
            continue
        future_lifecycle = lifecycle_by_theme_date.get((theme_code, future_date), "")
        if model_key == "narrative_transition_multitask_v1" and (
            not theme_code
            or not lifecycle
            or not future_lifecycle
        ):
            continue
        if model_key == "narrative_transition_multitask_v1":
            narrative_strength = theme_score
        current_lifecycle_class = int(
            lifecycle_code_by_theme_date.get((theme_code, trade_date), 0)
        )
        lifecycle_code = float(current_lifecycle_class) / float(
            len(NARRATIVE_LIFECYCLE_CLASSES) - 1
        )
        future_lifecycle_code = int(
            lifecycle_code_by_theme_date.get(
                (theme_code, future_date),
                current_lifecycle_class,
            )
        )
        transition_pressure = theme_pressure_by_theme_date.get(
            (theme_code, trade_date),
            0.0,
        )
        narrative_observation = theme_observation_by_theme_date.get(
            (theme_code, trade_date),
            {},
        )
        if model_key == "narrative_transition_multitask_v1":
            capital_persistence = float(
                narrative_observation.get("capital_persistence") or 0.0
            )
        event_observation = event_by_symbol_date.get((symbol, trade_date)) or {}
        event_text_sentiment = float(event_observation.get("sentiment") or 0.0)
        event_text_confidence = float(event_observation.get("confidence") or 0.0)
        event_source_type = str(event_observation.get("source") or "")
        if model_key == "event_impact_assessor_v2" and event_text_confidence <= 0:
            continue
        event_surprise = min(
            3.0,
            max(-3.0, event_text_sentiment * max(0.0, event_text_confidence)),
        )
        event_market_reaction = (
            float(item.get("change_pct") or 0.0) / 100.0
            - float(benchmark_daily_return_by_date.get(trade_date) or 0.0)
        )
        top_list_signal, top_list_confidence = top_list_by_symbol_date.get(
            (symbol, trade_date),
            (0.0, 0.0),
        )
        event_crowding = max(
            0.0,
            min(
                3.0,
                abs(event_market_reaction)
                / max(0.005, float(volatility_20))
                + max(0.0, turnover_slope)
                + abs(float(top_list_signal)),
            ),
        ) if top_list_confidence > 0 else max(
            0.0,
            min(3.0, max(0.0, turnover_slope)),
        )
        event_source_flags = {
            source: float(
                source == event_source_type
                or source in set(event_observation.get("observed_sources") or [])
            )
            for source in EVENT_STRUCTURED_SOURCES
        }
        event_source_flags["top_list"] = float(top_list_confidence > 0)
        analyst_revision, analyst_target_price = (
            _analyst_context(symbol, trade_date)
            if model_key == "event_impact_assessor_v2"
            else (None, None)
        )
        structured_features = materialize_event_features(
            event_observation,
            total_mv=item.get("total_mv"),
            close=close,
            momentum_20=momentum_20,
            volatility_20=volatility_20,
            market_reaction=event_market_reaction,
            crowding=event_crowding,
            analyst_revision=analyst_revision,
            analyst_target_price=analyst_target_price,
        )
        future_return = future_close / close - 1.0
        benchmark_return = benchmark_return_by_date.get(trade_date)
        if model_key == "event_impact_assessor_v2" and benchmark_return is None:
            continue
        future_abnormal_return = (
            future_return - float(benchmark_return)
            if benchmark_return is not None
            else future_return
        )
        if model_key == "capital_persistence_multitask_v1" and not moneyflow:
            continue
        future_capital_persistence = min(
            3.0,
            max(-3.0, float((moneyflow or {}).get("future_persistence") or 0.0)),
        )
        current_market_state = str(
            market_state_by_date.get(str(item.get("trade_date") or ""), "unknown")
        ).upper()
        feature_values = {
            "momentum_20": momentum_20,
            "momentum_60": momentum_60,
            "volatility_20": volatility_20,
            "turnover_slope": turnover_slope,
            "capital_persistence": capital_persistence,
            "narrative_strength": narrative_strength,
            "event_surprise": event_surprise,
            "event_text_sentiment": event_text_sentiment,
            "event_text_confidence": event_text_confidence,
            "event_market_reaction": event_market_reaction,
            "event_crowding": event_crowding,
            "event_source_announcement": event_source_flags["announcement"],
            "event_source_structured_disclosure": event_source_flags["structured_disclosure"],
            "event_source_major_news": event_source_flags["major_news"],
            "event_source_top_list": event_source_flags["top_list"],
            **structured_features,
            **{
                f"market_state_s{state}": float(current_market_state == f"S{state}")
                for state in range(4)
            },
            "narrative_lifecycle_code": lifecycle_code,
            "narrative_transition_pressure": transition_pressure,
            "narrative_breadth": float(narrative_observation.get("breadth") or 0.0),
            "narrative_crowding": float(narrative_observation.get("crowding") or 0.0),
            "narrative_score_slope": float(narrative_observation.get("score_slope") or 0.0),
            "narrative_breadth_slope": float(narrative_observation.get("breadth_slope") or 0.0),
            "narrative_state_age": min(
                1.0,
                float(narrative_observation.get("state_age") or 0.0) / 60.0,
            ),
        }
        samples.append({
            "ts_code": symbol,
            "trade_date": trade_date,
            "features": [float(feature_values[name]) for name in PLAN_MODEL_FEATURES],
            "future_return": future_return,
            "future_abnormal_return": future_abnormal_return,
            "future_capital_persistence": future_capital_persistence,
            "event_source_type": event_source_type,
            "event_type_group": str(
                event_observation.get("event_type_group") or "other"
            ),
            "event_market_reaction": event_market_reaction,
            "event_direction_signal": event_surprise,
            "theme_code": theme_code,
            "market_state": current_market_state,
            "current_lifecycle_class": current_lifecycle_class,
            "future_lifecycle_class": future_lifecycle_code,
            "ranking_query": (
                f"{trade_date}:{theme_code}"
                if model_key == "leader_lambdarank_v1"
                else trade_date
            ),
            "market_profile": market_profile_by_date.get(trade_date, "unknown"),
            "environment_id": f"{market_profile_by_date.get(trade_date, 'unknown')}:{lifecycle or '无主题'}",
        })
    if model_key == "narrative_transition_multitask_v1":
        theme_samples: list[dict[str, Any]] = []
        by_theme_date: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in samples:
            key = (
                str(item.get("trade_date") or ""),
                str(item.get("theme_code") or ""),
            )
            if key[0] and key[1]:
                by_theme_date.setdefault(key, []).append(item)
        for (trade_date, theme_code), members in by_theme_date.items():
            first = members[0]
            theme_samples.append({
                **first,
                "ts_code": f"THEME:{theme_code}",
                "features": np.mean(
                    np.asarray([item["features"] for item in members], dtype=float),
                    axis=0,
                ).tolist(),
                "future_return": float(np.mean([
                    float(item["future_return"]) for item in members
                ])),
                "future_abnormal_return": float(np.mean([
                    float(item["future_abnormal_return"]) for item in members
                ])),
                "future_capital_persistence": float(np.mean([
                    float(item["future_capital_persistence"]) for item in members
                ])),
                "ranking_query": f"{trade_date}:{theme_code}",
                "theme_member_count": len(members),
            })
        samples = theme_samples
    if len(samples) < 500:
        raise RuntimeError(f"训练样本不足：{len(samples)} rows")
    if model_key == "leader_lambdarank_v1":
        query_counts = Counter(str(item.get("ranking_query") or "") for item in samples)
        samples = [
            item for item in samples
            if query_counts[str(item.get("ranking_query") or "")] >= 5
        ]
        if len(samples) < 500:
            raise RuntimeError(f"龙头模型主题内有效训练样本不足：{len(samples)} rows")
    if on_progress:
        on_progress("dataset", 0.34, f"有效样本 {len(samples)} 行，特征 {len(PLAN_MODEL_FEATURES)} 个")
    samples.sort(
        key=lambda x: (
            str(x["trade_date"]),
            str(x.get("ranking_query") or ""),
            str(x.get("ts_code") or ""),
        )
    )
    returns = np.asarray([float(x["future_return"]) for x in samples], dtype=float)
    abnormal_returns = np.asarray(
        [float(x["future_abnormal_return"]) for x in samples],
        dtype=float,
    )
    capital_targets = np.asarray(
        [float(x["future_capital_persistence"]) for x in samples],
        dtype=float,
    )
    labels = np.zeros(len(samples), dtype=int)
    daily_label_quantiles: dict[str, list[float]] = {}
    indices_by_date: dict[str, list[int]] = {}
    for index, sample in enumerate(samples):
        label_query = (
            str(sample.get("ranking_query") or "")
            if model_key == "leader_lambdarank_v1"
            else str(sample["trade_date"])
        )
        indices_by_date.setdefault(label_query, []).append(index)
    label_source = (
        capital_targets
        if model_key == "capital_persistence_multitask_v1"
        else abnormal_returns
        if model_key == "event_impact_assessor_v2"
        else returns
    )
    if model_key != "event_impact_assessor_v2":
        for sample_date, indices in indices_by_date.items():
            values = label_source[indices]
            if len(values) < 5:
                continue
            quantiles = np.quantile(values, [0.2, 0.4, 0.6, 0.8])
            labels[indices] = np.digitize(values, quantiles).astype(int)
            daily_label_quantiles[sample_date] = [
                float(value) for value in quantiles
            ]
    if on_progress:
        if model_key == "event_impact_assessor_v2":
            on_progress(
                "dataset",
                0.37,
                "条件影响目标完成：成本后正向、已定价、延续、反转概率与 q10/q50/q90 分位",
            )
        else:
            label_name = (
                "未来真实资金持续标签"
                if model_key == "capital_persistence_multitask_v1"
                else "未来收益标签"
            )
            on_progress(
                "dataset",
                0.37,
                f"{label_name}完成：按 20/40/60/80 分位分成 5 档",
            )
    x = np.asarray([x["features"] for x in samples], dtype=float)
    dates = [str(x["trade_date"]) for x in samples]
    ranking_queries = [str(x.get("ranking_query") or x["trade_date"]) for x in samples]
    symbols = [str(x.get("ts_code") or "") for x in samples]
    market_states = [str(x.get("market_state") or "unknown") for x in samples]
    environments = [str(x.get("environment_id") or "unknown") for x in samples]
    market_profiles = [str(x.get("market_profile") or "unknown") for x in samples]
    event_sources = [str(x.get("event_source_type") or "unknown") for x in samples]
    event_types = [str(x.get("event_type_group") or "other") for x in samples]
    event_reactions = np.asarray(
        [float(x.get("event_market_reaction") or 0.0) for x in samples],
        dtype=float,
    )
    event_direction_signals = np.asarray(
        [float(x.get("event_direction_signal") or 0.0) for x in samples],
        dtype=float,
    )
    current_lifecycle_labels = np.asarray(
        [int(x.get("current_lifecycle_class") or 0) for x in samples],
        dtype=int,
    )
    lifecycle_labels = np.asarray([int(x.get("future_lifecycle_class") or 0) for x in samples], dtype=int)
    narrative_future_outcome_audit: dict[str, dict[str, float | int]] = {}
    if model_key == "narrative_transition_multitask_v1":
        for state_index, state_name in enumerate(NARRATIVE_LIFECYCLE_CLASSES):
            mask = current_lifecycle_labels == state_index
            values = abnormal_returns[mask]
            narrative_future_outcome_audit[state_name] = {
                "sample_count": int(values.size),
                "mean_future_abnormal_return": round(float(np.mean(values)), 8) if values.size else 0.0,
                "positive_future_abnormal_return_rate": round(float(np.mean(values > 0)), 8) if values.size else 0.0,
                "used_for": "label_audit_only_not_model_features",
            }
    unique_dates = sorted(set(dates))
    n_dates = len(unique_dates)
    window_builder = (
        _event_walk_forward_date_windows
        if model_key == "event_impact_assessor_v2"
        else _walk_forward_date_windows
    )
    final_window = window_builder(unique_dates)[-1]
    train_dates_set = set(final_window["train"])
    validation_dates_set = set(final_window["validation"])
    test_dates_set = set(final_window["test"])
    train_mask = np.asarray([date in train_dates_set for date in dates], dtype=bool)
    validation_mask = np.asarray([date in validation_dates_set for date in dates], dtype=bool)
    test_mask = np.asarray([date in test_dates_set for date in dates], dtype=bool)
    train_end = final_window["train"][-1]
    validation_start = final_window["validation"][0]
    validation_end = final_window["validation"][-1]
    test_start = final_window["test"][0]
    if on_progress:
        on_progress("dataset", 0.40, f"训练/验证/测试切分完成：训练 {int(train_mask.sum())} 行，验证 {int(validation_mask.sum())} 行，测试 {int(test_mask.sum())} 行")
    def _groups_for(mask: np.ndarray) -> list[int]:
        counts = Counter(
            query
            for query, in_split in zip(ranking_queries, mask)
            if bool(in_split)
        )
        ordered_queries = list(dict.fromkeys(
            query for query, in_split in zip(ranking_queries, mask) if bool(in_split)
        ))
        return [counts[query] for query in ordered_queries if counts[query]]
    return {
        "x_all": x,
        "y_all_rank": labels,
        "y_all_return": returns,
        "y_all_abnormal_return": abnormal_returns,
        "y_all_task_target": label_source,
        "y_all_current_lifecycle": current_lifecycle_labels,
        "y_all_lifecycle": lifecycle_labels,
        "narrative_future_outcome_audit": narrative_future_outcome_audit,
        "all_dates": dates,
        "all_ranking_queries": ranking_queries,
        "all_symbols": symbols,
        "all_market_states": market_states,
        "all_market_profiles": market_profiles,
        "all_event_sources": event_sources,
        "all_event_types": event_types,
        "y_all_event_reaction": event_reactions,
        "y_all_event_direction_signal": event_direction_signals,
        "all_environments": environments,
        "_train_mask": train_mask,
        "_validation_mask": validation_mask,
        "_test_mask": test_mask,
        "x_train": x[train_mask],
        "y_train_rank": labels[train_mask],
        "y_train_return": returns[train_mask],
        "y_train_abnormal_return": abnormal_returns[train_mask],
        "y_train_task_target": label_source[train_mask],
        "y_train_current_lifecycle": current_lifecycle_labels[train_mask],
        "y_train_lifecycle": lifecycle_labels[train_mask],
        "y_train_event_reaction": event_reactions[train_mask],
        "y_train_event_direction_signal": event_direction_signals[train_mask],
        "train_event_sources": [
            source for source, in_split in zip(event_sources, train_mask) if bool(in_split)
        ],
        "train_event_types": [
            event_type for event_type, in_split in zip(event_types, train_mask) if bool(in_split)
        ],
        "x_val": x[validation_mask],
        "y_val_rank": labels[validation_mask],
        "y_val_return": returns[validation_mask],
        "y_val_abnormal_return": abnormal_returns[validation_mask],
        "y_val_task_target": label_source[validation_mask],
        "y_val_current_lifecycle": current_lifecycle_labels[validation_mask],
        "y_val_lifecycle": lifecycle_labels[validation_mask],
        "y_val_event_reaction": event_reactions[validation_mask],
        "y_val_event_direction_signal": event_direction_signals[validation_mask],
        "x_test": x[test_mask],
        "y_test_rank": labels[test_mask],
        "y_test_return": returns[test_mask],
        "y_test_abnormal_return": abnormal_returns[test_mask],
        "y_test_task_target": label_source[test_mask],
        "y_test_current_lifecycle": current_lifecycle_labels[test_mask],
        "y_test_lifecycle": lifecycle_labels[test_mask],
        "y_test_event_reaction": event_reactions[test_mask],
        "y_test_event_direction_signal": event_direction_signals[test_mask],
        "val_dates": [date for date, in_split in zip(dates, validation_mask) if bool(in_split)],
        "val_symbols": [symbol for symbol, in_split in zip(symbols, validation_mask) if bool(in_split)],
        "test_dates": [date for date, in_split in zip(dates, test_mask) if bool(in_split)],
        "test_symbols": [symbol for symbol, in_split in zip(symbols, test_mask) if bool(in_split)],
        "val_market_states": [state for state, in_split in zip(market_states, validation_mask) if bool(in_split)],
        "train_market_states": [state for state, in_split in zip(market_states, train_mask) if bool(in_split)],
        "test_market_states": [state for state, in_split in zip(market_states, test_mask) if bool(in_split)],
        "test_environments": [environment for environment, in_split in zip(environments, test_mask) if bool(in_split)],
        "test_market_profiles": [profile for profile, in_split in zip(market_profiles, test_mask) if bool(in_split)],
        "test_event_sources": [source for source, in_split in zip(event_sources, test_mask) if bool(in_split)],
        "test_event_types": [event_type for event_type, in_split in zip(event_types, test_mask) if bool(in_split)],
        "group_train": _groups_for(train_mask),
        "group_val": _groups_for(validation_mask),
        "sample_rows": len(samples),
        "raw_rows": len(raw_rows),
        "train_rows": int(train_mask.sum()),
        "validation_rows": int(validation_mask.sum()),
        "test_rows": int(test_mask.sum()),
        "train_trade_date_count": len(train_dates_set),
        "validation_trade_date_count": len(validation_dates_set),
        "test_trade_date_count": len(test_dates_set),
        "daily_sample_limit": daily_sample_limit,
        "symbol_sampling": symbol_sampling,
        "sampled_symbol_count": len(sampled_symbols),
        "moneyflow_raw_rows": len(moneyflow_rows),
        "capital_observation_count": len(moneyflow_by_symbol_date),
        "dates": {"start": unique_dates[0], "train_end": train_end, "validation_start": validation_start, "validation_end": validation_end, "test_start": test_start, "end": unique_dates[-1], "split": validation_start},
        "horizon": horizon,
        "label_quantiles": (
            {
                "contract": EVENT_TARGET_CONTRACT,
                "classification_heads": [
                    "positive_impact",
                    "priced_in",
                    "continuation",
                    "reversal",
                ],
                "quantile_heads": ["q10", "q50", "q90"],
                "cross_sectional_quintile_labels_used": False,
            }
            if model_key == "event_impact_assessor_v2"
            else {
                "contract": "per_trade_date_cross_sectional_quintiles_v1",
                "date_count": len(daily_label_quantiles),
            }
        ),
        "feature_count": len(PLAN_MODEL_FEATURES),
        "feature_computation_contract": {
            "momentum_20": "point_in_time_close_return_20_sessions_v1",
            "momentum_60": "point_in_time_close_return_60_sessions_v1",
            "volatility_20": "point_in_time_realized_return_volatility_20_sessions_v1",
            "turnover_slope": "point_in_time_turnover_average_20_vs_60_sessions_v1",
            "capital_persistence": "real_stock_moneyflow_5d_20d_persistence_and_large_order_balance_v2",
            "capital_target": "future_10_session_real_stock_moneyflow_persistence_v2",
            "capital_source_availability": "official_trade_date_close_time_v1_ingestion_time_retained_in_updated_at",
            "event_surprise": "point_in_time_announcement_and_major_news_direction_v4",
            "event_market_reaction": "same_day_stock_minus_csi300_return_available_at_decision_cutoff_v1",
            "event_crowding": "top_list_and_turnover_market_response_proxy_v1",
            "event_source": "one_hot_announcement_structured_disclosure_major_news_top_list_v2",
            "event_structured_features": EVENT_FEATURE_CONTRACT,
            "event_type": "observable_earnings_capital_return_contract_operation_governance_risk_v1",
            "event_magnitude": "reported_percentage_amount_or_event_specific_scale_with_observed_flag_v1",
            "event_materiality": "reported_amount_over_point_in_time_market_cap_with_observed_flag_v1",
            "event_expectation_gap": "exact_period_guidance_or_actual_vs_pit_independent_analyst_consensus_v2",
            "event_analyst_consensus": ANALYST_CONSENSUS_CONTRACT,
            "event_analyst_context": "point_in_time_4w_rating_revision_target_upside_and_independent_np_consensus_v3",
            "event_priced_in_proxy": "same_day_abnormal_reaction_pre_event_runup_volatility_and_crowding_v1",
            "event_target": EVENT_TARGET_CONTRACT,
            "event_transaction_cost_rate": EVENT_TRANSACTION_COST_RATE,
            "narrative_transition_pressure": "current_vs_previous_available_theme_snapshot_v1",
            "narrative_target": "same_theme_three_session_observable_four_state_transition_v3",
            "narrative_lifecycle": NARRATIVE_V3_SCHEMA_VERSION,
            "narrative_state_age": "same_theme_consecutive_canonical_state_sessions_capped_60_v1",
            "leader_ranking_query": "trade_date_x_available_theme_code_minimum_five_members_v1",
        },
        "event_feature_audit": {
            "contract": EVENT_FEATURE_CONTRACT,
            "source_counts": dict(Counter(event_sources)),
            "type_counts": dict(Counter(event_types)),
            "structured_disclosure_rows": len(structured_event_rows),
            "announcement_rows": len(event_rows),
            "major_news_rows": len(news_rows),
            "analyst_report_rows": len(analyst_report_rows),
            "expectation_observed_rows": int(np.sum(
                x[:, PLAN_MODEL_FEATURES.index("event_expectation_observed")]
            )),
            "expectation_basis_counts": dict(Counter(
                str(item.get("expectation_basis") or "unobservable")
                for item in event_by_symbol_date.values()
            )),
            "consensus_contract": ANALYST_CONSENSUS_CONTRACT,
            "consensus_observed_event_dates": sum(
                bool(item.get("analyst_consensus_observed"))
                for item in event_by_symbol_date.values()
            ),
            "consensus_minimum_independent_organizations": 3,
            "consensus_lookback_calendar_days": 180,
            "consensus_dispersion_observed_rows": int(np.sum(
                x[:, PLAN_MODEL_FEATURES.index("event_consensus_dispersion_observed")]
            )),
            "materiality_observed_rows": int(np.sum(
                x[:, PLAN_MODEL_FEATURES.index("event_materiality_observed")]
            )),
            "analyst_revision_observed_rows": int(np.sum(
                x[:, PLAN_MODEL_FEATURES.index("event_analyst_revision_observed")]
            )),
            "missing_values_are_zero_with_explicit_observed_flags": True,
        },
        "narrative_label_audit": {
            "schema_version": "alis_narrative_label_audit_v3",
            "states": list(NARRATIVE_LIFECYCLE_CLASSES),
            "persisted_lifecycle_counts": dict(persisted_lifecycle_counts),
            "observable_candidate_counts": {
                NARRATIVE_LIFECYCLE_CLASSES[index]: int(
                    candidate_lifecycle_counts.get(index, 0)
                )
                for index in range(len(NARRATIVE_LIFECYCLE_CLASSES))
            },
            "canonical_path_counts": {
                NARRATIVE_LIFECYCLE_CLASSES[index]: int(
                    canonical_lifecycle_counts.get(index, 0)
                )
                for index in range(len(NARRATIVE_LIFECYCLE_CLASSES))
            },
            "path_adjustment_count": int(path_adjustment_count),
            "unobservable_snapshot_date_count": len(
                unobservable_theme_dates
            ),
            "unobservable_snapshot_dates": sorted(
                unobservable_theme_dates
            )[:20],
            "allowed_destinations": {
                NARRATIVE_LIFECYCLE_CLASSES[source]: [
                    NARRATIVE_LIFECYCLE_CLASSES[destination]
                    for destination in destinations
                ]
                for source, destinations
                in NARRATIVE_ALLOWED_DESTINATIONS.items()
            },
            "three_session_reachable_destinations": {
                NARRATIVE_LIFECYCLE_CLASSES[source]: [
                    NARRATIVE_LIFECYCLE_CLASSES[destination]
                    for destination in destinations
                ]
                for source, destinations
                in NARRATIVE_HORIZON_DESTINATIONS.items()
            },
            "no_synthetic_samples": True,
            "oversampling_forbidden": True,
            "minimum_state_sessions": dict(NARRATIVE_V3_MIN_STATE_SESSIONS),
            "confirmation_sessions": NARRATIVE_V3_CONFIRMATION_SESSIONS,
            "crowding_policy": "成熟状态不要求拥挤；拥挤仅作为独立可观测风险字段",
            "future_return_confirmation": narrative_future_outcome_audit,
        },
        "trade_date_count": len(unique_dates),
        "training_protocol": _training_protocol_for_model(model_key),
    }


def _observation_counts(
    values: list[str],
    *,
    expected: list[str],
) -> dict[str, int]:
    """Record environment coverage without turning it into an alpha metric."""

    counts = Counter(str(value) for value in values)
    return {key: int(counts.get(key, 0)) for key in expected}


def _categorical_distribution_drift(
    train_values: list[str],
    test_values: list[str],
) -> dict[str, Any]:
    """Describe categorical drift without turning it into a promotion gate."""

    normalized_train = [str(value or "unknown") for value in train_values]
    normalized_test = [str(value or "unknown") for value in test_values]
    train_counts = Counter(normalized_train)
    test_counts = Counter(normalized_test)
    categories = sorted(set(train_counts) | set(test_counts))
    train_total = max(1, len(normalized_train))
    test_total = max(1, len(normalized_test))
    train_shares = np.asarray(
        [float(train_counts.get(category, 0)) / train_total for category in categories],
        dtype=float,
    )
    test_shares = np.asarray(
        [float(test_counts.get(category, 0)) / test_total for category in categories],
        dtype=float,
    )
    midpoint = 0.5 * (train_shares + test_shares)

    def _kl_divergence(values: np.ndarray, reference: np.ndarray) -> float:
        selected = values > 0
        if not bool(np.any(selected)):
            return 0.0
        return float(np.sum(
            values[selected] * np.log(values[selected] / reference[selected])
        ))

    js_divergence = 0.5 * (
        _kl_divergence(train_shares, midpoint)
        + _kl_divergence(test_shares, midpoint)
    )
    total_variation = 0.5 * float(np.sum(np.abs(train_shares - test_shares)))
    return {
        "train_rows": len(normalized_train),
        "test_rows": len(normalized_test),
        "categories": {
            category: {
                "train_count": int(train_counts.get(category, 0)),
                "train_share": round(float(train_shares[index]), 8),
                "test_count": int(test_counts.get(category, 0)),
                "test_share": round(float(test_shares[index]), 8),
                "share_shift": round(
                    float(test_shares[index] - train_shares[index]),
                    8,
                ),
            }
            for index, category in enumerate(categories)
        },
        "total_variation_distance": round(total_variation, 8),
        "jensen_shannon_divergence_nats": round(js_divergence, 8),
        "test_only_categories": sorted(set(test_counts) - set(train_counts)),
        "train_only_categories": sorted(set(train_counts) - set(test_counts)),
    }


def _event_audit_target_labels(dataset: dict[str, Any]) -> dict[str, Any]:
    return event_impact_target_labels(
        abnormal_return=dataset["y_all_abnormal_return"],
        event_reaction=dataset["y_all_event_reaction"],
        event_direction_signal=dataset["y_all_event_direction_signal"],
        transaction_cost_rate=EVENT_TRANSACTION_COST_RATE,
    )


def _numeric_distribution_summary(values: np.ndarray) -> dict[str, Any]:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return {"rows": 0, "mean": None, "q10": None, "q50": None, "q90": None}
    quantiles = np.quantile(finite, [0.10, 0.50, 0.90])
    return {
        "rows": int(len(finite)),
        "mean": round(float(np.mean(finite)), 8),
        "q10": round(float(quantiles[0]), 8),
        "q50": round(float(quantiles[1]), 8),
        "q90": round(float(quantiles[2]), 8),
    }


def _event_label_snapshot(
    targets: dict[str, Any],
    mask: np.ndarray,
) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    labels = targets["labels"]
    observable_masks = targets["observable_masks"]
    split_rows = int(mask.sum())
    for name, values in labels.items():
        observed_mask = mask & np.asarray(observable_masks[name], dtype=bool)
        selected = np.asarray(values, dtype=int)[observed_mask]
        positives = int(np.sum(selected == 1))
        negatives = int(np.sum(selected == 0))
        snapshot[name] = {
            "rows": int(len(selected)),
            "split_rows": split_rows,
            "unobservable_count": split_rows - int(len(selected)),
            "observed_rate": (
                round(float(len(selected) / split_rows), 8)
                if split_rows
                else None
            ),
            "positive_count": positives,
            "negative_count": negatives,
            "positive_rate": (
                round(float(positives / len(selected)), 8) if len(selected) else None
            ),
            "minority_class_count": min(positives, negatives),
            "minimum_descriptive_support": EVENT_AUDIT_MINIMUM_GROUP_SUPPORT,
            "minority_support_warning": (
                min(positives, negatives) < EVENT_AUDIT_MINIMUM_GROUP_SUPPORT
            ),
        }
    return snapshot


def _event_observability_snapshot(
    dataset: dict[str, Any],
    mask: np.ndarray,
) -> dict[str, dict[str, Any]]:
    observed_features = (
        "event_magnitude_observed",
        "event_materiality_observed",
        "event_expectation_observed",
        "event_consensus_dispersion_observed",
        "event_consensus_freshness_observed",
        "event_company_size_observed",
        "event_analyst_revision_observed",
        "event_target_price_observed",
    )
    values = np.asarray(dataset["x_all"], dtype=float)
    selected_rows = int(mask.sum())
    result: dict[str, dict[str, Any]] = {}
    for feature in observed_features:
        column = values[mask, PLAN_MODEL_FEATURES.index(feature)]
        observed = int(np.sum(column > 0.5))
        result[feature] = {
            "rows": selected_rows,
            "observed_count": observed,
            "observed_rate": (
                round(float(observed / selected_rows), 8)
                if selected_rows
                else None
            ),
        }
    return result


def _event_conditional_outcome_snapshot(
    groups: list[str],
    mask: np.ndarray,
    targets: dict[str, Any],
    abnormal_returns: np.ndarray,
) -> dict[str, dict[str, Any]]:
    selected_groups = np.asarray(groups, dtype=object)[mask]
    selected_returns = np.asarray(abnormal_returns, dtype=float)[mask]
    labels = targets["labels"]
    observable_masks = targets["observable_masks"]
    result: dict[str, dict[str, Any]] = {}
    for group in sorted({str(value) for value in selected_groups.tolist()}):
        group_mask = selected_groups == group
        rows = int(group_mask.sum())
        full_group_mask = np.zeros(len(mask), dtype=bool)
        full_group_mask[np.flatnonzero(mask)[group_mask]] = True
        label_rates: dict[str, float | None] = {}
        label_observed_rows: dict[str, int] = {}
        for name, values in labels.items():
            label_mask = full_group_mask & np.asarray(
                observable_masks[name],
                dtype=bool,
            )
            observed_rows = int(label_mask.sum())
            label_observed_rows[name] = observed_rows
            label_rates[name] = (
                round(float(np.mean(np.asarray(values)[label_mask])), 8)
                if observed_rows
                else None
            )
        result[group] = {
            "rows": rows,
            "descriptive_support_sufficient": (
                rows >= EVENT_AUDIT_MINIMUM_GROUP_SUPPORT
            ),
            "label_positive_rates": label_rates,
            "label_observed_rows": label_observed_rows,
            "abnormal_return": _numeric_distribution_summary(
                selected_returns[group_mask]
            ),
        }
    return result


def _event_distribution_label_fold_audit(
    dataset: dict[str, Any],
    train_mask: np.ndarray,
    test_mask: np.ndarray,
    *,
    fold: int,
) -> dict[str, Any]:
    targets = _event_audit_target_labels(dataset)
    labels = targets["labels"]
    abnormal_returns = np.asarray(dataset["y_all_abnormal_return"], dtype=float)
    dimensions = {
        "event_source": [str(value) for value in dataset.get("all_event_sources") or []],
        "event_family": [str(value) for value in dataset.get("all_event_types") or []],
        "market_state": [str(value) for value in dataset.get("all_market_states") or []],
        "market_profile": [str(value) for value in dataset.get("all_market_profiles") or []],
    }
    dimensions["event_family_x_market_state"] = [
        f"{family}|{state}"
        for family, state in zip(
            dimensions["event_family"],
            dimensions["market_state"],
        )
    ]

    distribution_drift = {
        name: _categorical_distribution_drift(
            np.asarray(values, dtype=object)[train_mask].tolist(),
            np.asarray(values, dtype=object)[test_mask].tolist(),
        )
        for name, values in dimensions.items()
    }
    train_labels = _event_label_snapshot(targets, train_mask)
    test_labels = _event_label_snapshot(targets, test_mask)
    label_rate_shift = {
        name: (
            round(
                float(
                    (test_labels[name]["positive_rate"] or 0.0)
                    - (train_labels[name]["positive_rate"] or 0.0)
                ),
                8,
            )
            if train_labels[name]["positive_rate"] is not None
            and test_labels[name]["positive_rate"] is not None
            else None
        )
        for name in labels
    }
    train_observability = _event_observability_snapshot(dataset, train_mask)
    test_observability = _event_observability_snapshot(dataset, test_mask)
    observability_shift = {
        feature: round(
            float(
                (test_observability[feature]["observed_rate"] or 0.0)
                - (train_observability[feature]["observed_rate"] or 0.0)
            ),
            8,
        )
        for feature in train_observability
    }
    conditional_dimensions = {
        name: values
        for name, values in dimensions.items()
        if name in {
            "event_family",
            "market_state",
            "event_family_x_market_state",
        }
    }
    return {
        "contract": EVENT_DISTRIBUTION_LABEL_AUDIT_CONTRACT,
        "fold": int(fold),
        "audit_only": True,
        "promotion_gate_impact": "none",
        "model_parameter_changes": False,
        "evaluation_gate_changes": False,
        "synthetic_samples": False,
        "minimum_descriptive_group_support": EVENT_AUDIT_MINIMUM_GROUP_SUPPORT,
        "samples": {
            "train_rows": int(train_mask.sum()),
            "test_rows": int(test_mask.sum()),
        },
        "distribution_drift": distribution_drift,
        "label_prevalence": {
            "train": train_labels,
            "test": test_labels,
            "test_minus_train_rate": label_rate_shift,
        },
        "abnormal_return_distribution": {
            "train": _numeric_distribution_summary(abnormal_returns[train_mask]),
            "test": _numeric_distribution_summary(abnormal_returns[test_mask]),
        },
        "feature_observability": {
            "train": train_observability,
            "test": test_observability,
            "test_minus_train_rate": observability_shift,
        },
        "conditional_outcomes": {
            dimension: {
                "train": _event_conditional_outcome_snapshot(
                    values,
                    train_mask,
                    targets,
                    abnormal_returns,
                ),
                "test": _event_conditional_outcome_snapshot(
                    values,
                    test_mask,
                    targets,
                    abnormal_returns,
                ),
            }
            for dimension, values in conditional_dimensions.items()
        },
    }


def _event_distribution_label_stability_audit(
    dataset: dict[str, Any],
    fold_date_windows: list[dict[str, list[str]]],
) -> dict[str, Any]:
    all_dates = np.asarray(dataset.get("all_dates") or [], dtype=object)
    folds: list[dict[str, Any]] = []
    for fold, window in enumerate(fold_date_windows, start=1):
        train_dates = set(str(value) for value in window["train"])
        test_dates = set(str(value) for value in window["test"])
        train_mask = np.asarray(
            [str(value) in train_dates for value in all_dates],
            dtype=bool,
        )
        test_mask = np.asarray(
            [str(value) in test_dates for value in all_dates],
            dtype=bool,
        )
        fold_audit = _event_distribution_label_fold_audit(
            dataset,
            train_mask,
            test_mask,
            fold=fold,
        )
        fold_audit["window"] = {
            "train_start": min(train_dates),
            "train_end": max(train_dates),
            "test_start": min(test_dates),
            "test_end": max(test_dates),
            "train_event_dates": len(train_dates),
            "test_event_dates": len(test_dates),
        }
        folds.append(fold_audit)

    dimension_names = list((folds[0].get("distribution_drift") or {}).keys())
    distribution_summary: dict[str, Any] = {}
    for dimension in dimension_names:
        tv_values = [
            float(fold["distribution_drift"][dimension]["total_variation_distance"])
            for fold in folds
        ]
        js_values = [
            float(
                fold["distribution_drift"][dimension][
                    "jensen_shannon_divergence_nats"
                ]
            )
            for fold in folds
        ]
        max_tv_index = int(np.argmax(tv_values))
        distribution_summary[dimension] = {
            "mean_total_variation_distance": round(float(np.mean(tv_values)), 8),
            "max_total_variation_distance": round(float(tv_values[max_tv_index]), 8),
            "max_total_variation_fold": int(folds[max_tv_index]["fold"]),
            "mean_jensen_shannon_divergence_nats": round(
                float(np.mean(js_values)),
                8,
            ),
            "max_jensen_shannon_divergence_nats": round(
                float(np.max(js_values)),
                8,
            ),
        }

    label_names = list(
        ((folds[0].get("label_prevalence") or {}).get("test") or {}).keys()
    )
    label_stability: dict[str, Any] = {}
    for label in label_names:
        test_rates = [
            float(fold["label_prevalence"]["test"][label]["positive_rate"])
            for fold in folds
            if fold["label_prevalence"]["test"][label]["positive_rate"]
            is not None
        ]
        rate_shifts = [
            float(fold["label_prevalence"]["test_minus_train_rate"][label])
            for fold in folds
            if fold["label_prevalence"]["test_minus_train_rate"][label]
            is not None
        ]
        observed_rates = [
            float(fold["label_prevalence"]["test"][label]["observed_rate"])
            for fold in folds
            if fold["label_prevalence"]["test"][label]["observed_rate"]
            is not None
        ]
        label_stability[label] = {
            "test_positive_rates": [round(value, 8) for value in test_rates],
            "test_rate_mean": round(float(np.mean(test_rates)), 8),
            "test_rate_min": round(float(np.min(test_rates)), 8),
            "test_rate_max": round(float(np.max(test_rates)), 8),
            "test_rate_range": round(
                float(np.max(test_rates) - np.min(test_rates)),
                8,
            ),
            "test_rate_std": round(float(np.std(test_rates)), 8),
            "max_absolute_train_test_rate_shift": round(
                float(np.max(np.abs(rate_shifts))),
                8,
            ) if rate_shifts else None,
            "test_observed_rates": [round(value, 8) for value in observed_rates],
            "minimum_test_observed_rate": (
                round(float(np.min(observed_rates)), 8)
                if observed_rates
                else None
            ),
            "minority_support_warning_folds": [
                int(fold["fold"])
                for fold in folds
                if bool(
                    fold["label_prevalence"]["test"][label][
                        "minority_support_warning"
                    ]
                )
            ],
        }

    observability_features = list(
        ((folds[0].get("feature_observability") or {}).get("test") or {}).keys()
    )
    observability_stability: dict[str, Any] = {}
    for feature in observability_features:
        test_rates = [
            float(fold["feature_observability"]["test"][feature]["observed_rate"])
            for fold in folds
        ]
        train_test_shifts = [
            float(fold["feature_observability"]["test_minus_train_rate"][feature])
            for fold in folds
        ]
        observability_stability[feature] = {
            "test_observed_rates": [round(value, 8) for value in test_rates],
            "test_rate_range": round(
                float(np.max(test_rates) - np.min(test_rates)),
                8,
            ),
            "max_absolute_train_test_rate_shift": round(
                float(np.max(np.abs(train_test_shifts))),
                8,
            ),
        }

    conditional_stability: dict[str, Any] = {}
    for dimension in (
        "event_family",
        "market_state",
        "event_family_x_market_state",
    ):
        group_names = sorted({
            group
            for fold in folds
            for group in fold["conditional_outcomes"][dimension]["test"]
        })
        conditional_stability[dimension] = {}
        for group in group_names:
            supported = [
                (
                    int(fold["fold"]),
                    fold["conditional_outcomes"][dimension]["test"][group],
                )
                for fold in folds
                if group in fold["conditional_outcomes"][dimension]["test"]
                and bool(
                    fold["conditional_outcomes"][dimension]["test"][group][
                        "descriptive_support_sufficient"
                    ]
                )
            ]
            label_ranges: dict[str, Any] = {}
            for label in label_names:
                label_supported = [
                    item
                    for item in supported
                    if int(item[1]["label_observed_rows"].get(label) or 0)
                    >= EVENT_AUDIT_MINIMUM_GROUP_SUPPORT
                    and item[1]["label_positive_rates"].get(label) is not None
                ]
                rates = [
                    float(item[1]["label_positive_rates"][label])
                    for item in label_supported
                ]
                label_ranges[label] = {
                    "supported_window_count": len(rates),
                    "supported_folds": [item[0] for item in label_supported],
                    "rate_min": round(float(np.min(rates)), 8) if rates else None,
                    "rate_max": round(float(np.max(rates)), 8) if rates else None,
                    "rate_range": (
                        round(float(np.max(rates) - np.min(rates)), 8)
                        if rates
                        else None
                    ),
                }
            conditional_stability[dimension][group] = {
                "supported_folds": [item[0] for item in supported],
                "insufficient_support_folds": [
                    int(fold["fold"])
                    for fold in folds
                    if group not in fold["conditional_outcomes"][dimension]["test"]
                    or not bool(
                        fold["conditional_outcomes"][dimension]["test"][group][
                            "descriptive_support_sufficient"
                        ]
                    )
                ],
                "labels": label_ranges,
            }

    largest_distribution_shifts = sorted(
        (
            {
                "dimension": dimension,
                "max_total_variation_distance": summary[
                    "max_total_variation_distance"
                ],
                "fold": summary["max_total_variation_fold"],
            }
            for dimension, summary in distribution_summary.items()
        ),
        key=lambda item: float(item["max_total_variation_distance"]),
        reverse=True,
    )
    largest_label_ranges = sorted(
        (
            {"label": label, "test_rate_range": summary["test_rate_range"]}
            for label, summary in label_stability.items()
        ),
        key=lambda item: float(item["test_rate_range"]),
        reverse=True,
    )
    target_definition = _event_audit_target_labels(dataset)
    return {
        "contract": EVENT_DISTRIBUTION_LABEL_AUDIT_CONTRACT,
        "target_contract": EVENT_TARGET_CONTRACT,
        "event_feature_contract": EVENT_FEATURE_CONTRACT,
        "audit_only": True,
        "promotion_gate_impact": "none",
        "model_parameter_changes": False,
        "evaluation_gate_changes": False,
        "synthetic_samples": False,
        "oversampling": False,
        "fold_count": len(folds),
        "minimum_descriptive_group_support": EVENT_AUDIT_MINIMUM_GROUP_SUPPORT,
        "label_definition_contract": target_definition["contract"],
        "label_definitions": target_definition["definitions"],
        "transaction_cost_rate": EVENT_TRANSACTION_COST_RATE,
        "direction_magnitude_multiplication_forbidden": True,
        "unobservable_direction_excluded": True,
        "distribution_summary": distribution_summary,
        "label_stability": label_stability,
        "feature_observability_stability": observability_stability,
        "conditional_test_label_stability": conditional_stability,
        "largest_distribution_shifts": largest_distribution_shifts,
        "largest_label_rate_ranges": largest_label_ranges,
        "folds": folds,
        "interpretation_policy": (
            "描述分布与标签证据，不定义新的通过阈值，不改变正式五窗、"
            "同任务强基线、A/B、Shadow或治理门"
        ),
    }


def _fit_lightgbm_plan_model(
    model_key: str,
    dataset: dict[str, Any],
    *,
    candidate_spec: dict[str, Any] | None = None,
    quick_diagnostic: bool = False,
) -> dict[str, Any]:
    try:
        import lightgbm as lgb
    except Exception as exc:
        raise RuntimeError(f"LightGBM不可用：{exc}") from exc

    candidate_spec = dict(candidate_spec or {})
    allowed_overrides = {
        "learning_rate", "num_leaves", "max_depth", "min_child_samples",
        "feature_fraction", "bagging_fraction", "reg_alpha", "reg_lambda",
    }
    params = {k: v for k, v in LIGHTGBM_PARAMS.items() if k != "early_stopping_rounds"}
    params.update({
        key: value
        for key, value in dict(candidate_spec.get("param_overrides") or {}).items()
        if key in allowed_overrides
    })
    default_features = (
        NARRATIVE_MODEL_FEATURES
        if model_key == "narrative_transition_multitask_v1"
        else EVENT_IMPACT_FEATURES
        if model_key == "event_impact_assessor_v2"
        else PLAN_MODEL_FEATURES
    )
    selected_features = [
        feature
        for feature in candidate_spec.get("feature_subset") or default_features
        if feature in PLAN_MODEL_FEATURES
    ]
    if (
        model_key != "narrative_transition_multitask_v1"
        and "momentum_20" not in selected_features
    ):
        selected_features.append("momentum_20")
    if (
        model_key == "narrative_transition_multitask_v1"
        and "narrative_lifecycle_code" not in selected_features
    ):
        selected_features.append("narrative_lifecycle_code")
    feature_indices = [PLAN_MODEL_FEATURES.index(feature) for feature in selected_features]
    event_head_feature_names: dict[str, list[str]] = {}
    event_head_feature_indices: dict[str, list[int]] = {}
    event_alternative_head_feature_names: dict[str, list[str]] = {}
    if model_key == "event_impact_assessor_v2":
        requested_head_subsets = candidate_spec.get("event_head_feature_subsets")
        if requested_head_subsets is None:
            event_head_feature_names = {
                head: list(selected_features) for head in EVENT_PROBABILITY_HEADS
            }
        elif not isinstance(requested_head_subsets, dict):
            raise ValueError("event_head_feature_subsets must be an object")
        else:
            missing_heads = sorted(
                set(EVENT_PROBABILITY_HEADS) - set(requested_head_subsets)
            )
            unknown_heads = sorted(
                set(requested_head_subsets) - set(EVENT_PROBABILITY_HEADS)
            )
            if missing_heads or unknown_heads:
                raise ValueError(
                    "event head feature subsets must cover exactly five heads: "
                    f"missing={missing_heads}, unknown={unknown_heads}"
                )
            for head in EVENT_PROBABILITY_HEADS:
                requested_features = requested_head_subsets.get(head)
                if not isinstance(requested_features, (list, tuple)):
                    raise ValueError(f"event head {head} feature subset must be a list")
                normalized = list(dict.fromkeys(
                    str(feature) for feature in requested_features if str(feature)
                ))
                invalid = sorted(set(normalized) - set(selected_features))
                if invalid:
                    raise ValueError(
                        f"event head {head} contains unavailable features: {invalid}"
                    )
                if len(normalized) < 5:
                    raise ValueError(
                        f"event head {head} requires at least five frozen features"
                    )
                event_head_feature_names[head] = normalized
        event_head_feature_indices = {
            head: [selected_features.index(feature) for feature in names]
            for head, names in event_head_feature_names.items()
        }
        requested_alternatives = candidate_spec.get(
            "event_alternative_head_feature_subsets"
        )
        if requested_alternatives is not None:
            if not isinstance(requested_alternatives, dict):
                raise ValueError(
                    "event_alternative_head_feature_subsets must be an object"
                )
            unknown_heads = sorted(
                set(requested_alternatives) - set(EVENT_PROBABILITY_HEADS)
            )
            if unknown_heads:
                raise ValueError(
                    f"event alternative head subsets contain unknown heads: {unknown_heads}"
                )
            for head, requested_features in requested_alternatives.items():
                if not isinstance(requested_features, (list, tuple)):
                    raise ValueError(
                        f"event alternative head {head} feature subset must be a list"
                    )
                normalized = list(dict.fromkeys(
                    str(feature) for feature in requested_features if str(feature)
                ))
                invalid = sorted(set(normalized) - set(selected_features))
                if invalid:
                    raise ValueError(
                        f"event alternative head {head} contains unavailable features: {invalid}"
                    )
                if len(normalized) < 5:
                    raise ValueError(
                        f"event alternative head {head} requires at least five frozen features"
                    )
                if normalized != event_head_feature_names[head]:
                    event_alternative_head_feature_names[head] = normalized
    requested_loss = str(candidate_spec.get("loss_function") or "")
    rank_objective = requested_loss if requested_loss in {"lambdarank", "rank_xendcg"} else "lambdarank"
    regression_objective = requested_loss if requested_loss in {"regression", "huber"} else "regression"
    multiclass_objective = requested_loss if requested_loss in {"multiclass", "multiclassova"} else "multiclass"

    def _select(values: Any) -> np.ndarray:
        return np.asarray(values, dtype=float)[:, feature_indices]

    x_train = _select(dataset["x_train"])
    x_val = _select(dataset["x_val"])
    sample_weight = dataset.get("train_sample_weight")

    def _fit_event_bundle(
        train_values: np.ndarray,
        train_returns: np.ndarray,
        train_reaction: np.ndarray,
        train_direction: np.ndarray,
        train_sources: list[str],
        train_types: list[str],
        *,
        fit_params: dict[str, Any],
        weights: np.ndarray | None = None,
        validation_values: np.ndarray | None = None,
        validation_returns: np.ndarray | None = None,
        validation_reaction: np.ndarray | None = None,
        validation_direction: np.ndarray | None = None,
        validation_sources: list[str] | None = None,
        validation_types: list[str] | None = None,
    ) -> dict[str, Any]:
        train_returns = np.asarray(train_returns, dtype=float)
        train_reaction = np.asarray(train_reaction, dtype=float)
        train_direction = np.asarray(train_direction, dtype=float)
        train_target_package = event_impact_target_labels(
            abnormal_return=train_returns,
            event_reaction=train_reaction,
            event_direction_signal=train_direction,
            transaction_cost_rate=EVENT_TRANSACTION_COST_RATE,
        )
        targets = {
            "positive_impact": train_target_package["labels"]["actionability"],
            "priced_in": train_target_package["labels"]["priced_in"],
            "continuation": train_target_package["labels"]["continuation"],
            "reversal": train_target_package["labels"]["reversal"],
            "direction_realization": train_target_package["labels"][
                "direction_realization"
            ],
        }
        target_masks = {
            "positive_impact": train_target_package["observable_masks"][
                "actionability"
            ],
            "priced_in": train_target_package["observable_masks"]["priced_in"],
            "continuation": train_target_package["observable_masks"][
                "continuation"
            ],
            "reversal": train_target_package["observable_masks"]["reversal"],
            "direction_realization": train_target_package["observable_masks"][
                "direction_realization"
            ],
        }
        validation_targets: dict[str, np.ndarray] = {}
        validation_masks: dict[str, np.ndarray] = {}
        baseline_shrinkage_enabled = bool(
            candidate_spec.get("event_baseline_shrinkage_contract")
        )
        train_groups = event_response_group_keys(
            train_sources,
            train_reaction,
            train_direction,
            train_types,
        )
        validation_groups: list[str] = []
        if (
            validation_returns is not None
            and validation_reaction is not None
            and validation_direction is not None
        ):
            val_returns = np.asarray(validation_returns, dtype=float)
            val_reaction = np.asarray(validation_reaction, dtype=float)
            val_direction = np.asarray(validation_direction, dtype=float)
            validation_target_package = event_impact_target_labels(
                abnormal_return=val_returns,
                event_reaction=val_reaction,
                event_direction_signal=val_direction,
                transaction_cost_rate=EVENT_TRANSACTION_COST_RATE,
            )
            validation_targets = {
                "positive_impact": validation_target_package["labels"][
                    "actionability"
                ],
                "priced_in": validation_target_package["labels"]["priced_in"],
                "continuation": validation_target_package["labels"][
                    "continuation"
                ],
                "reversal": validation_target_package["labels"]["reversal"],
                "direction_realization": validation_target_package["labels"][
                    "direction_realization"
                ],
            }
            validation_masks = {
                "positive_impact": validation_target_package[
                    "observable_masks"
                ]["actionability"],
                "priced_in": validation_target_package["observable_masks"][
                    "priced_in"
                ],
                "continuation": validation_target_package["observable_masks"][
                    "continuation"
                ],
                "reversal": validation_target_package["observable_masks"][
                    "reversal"
                ],
                "direction_realization": validation_target_package[
                    "observable_masks"
                ]["direction_realization"],
            }
            if validation_sources is not None and validation_types is not None:
                validation_groups = event_response_group_keys(
                    validation_sources,
                    val_reaction,
                    val_direction,
                    validation_types,
                )
        if baseline_shrinkage_enabled and (
            len(train_groups) != len(train_values)
            or validation_values is None
            or len(validation_groups) != len(validation_values)
        ):
            raise ValueError(
                "event baseline shrinkage requires complete train and validation metadata"
            )

        bundle_head_feature_names = {
            head: list(names) for head, names in event_head_feature_names.items()
        }
        bundle_head_feature_indices = {
            head: list(indices) for head, indices in event_head_feature_indices.items()
        }
        heads: dict[str, Any] = {}
        temperatures: dict[str, float] = {}
        support: dict[str, dict[str, Any]] = {}
        baseline_probability_models: dict[str, dict[str, Any]] = {}
        baseline_blend_weights: dict[str, float] = {}
        head_candidate_selection: dict[str, dict[str, Any]] = {}
        for name, target in targets.items():
            head_indices = bundle_head_feature_indices[name]
            head_train_values = train_values[:, head_indices]
            observed_mask = np.asarray(target_masks[name], dtype=bool)
            observed_target = np.asarray(target, dtype=int)[observed_mask]
            counts = np.bincount(observed_target, minlength=2).astype(float)
            support[name] = {
                "observed_rows": int(observed_mask.sum()),
                "unobservable_rows": int(len(observed_mask) - observed_mask.sum()),
                "class_counts": counts.astype(int).tolist(),
            }
            if int(np.count_nonzero(counts)) < 2:
                classifier: Any = _ConstantProbabilityClassifier(
                    (counts + 1.0).tolist()
                )
            else:
                classifier = lgb.LGBMClassifier(
                    objective="binary",
                    metric="binary_logloss",
                    **fit_params,
                )
                fit_kwargs: dict[str, Any] = {}
                if weights is not None:
                    fit_kwargs["sample_weight"] = np.asarray(weights)[observed_mask]
                validation_target = validation_targets.get(name)
                validation_length = (
                    len(validation_target)
                    if validation_target is not None
                    else 0
                )
                validation_mask = np.asarray(
                    validation_masks.get(
                        name,
                        np.zeros(validation_length, dtype=bool),
                    ),
                    dtype=bool,
                )
                observed_validation_target = (
                    np.asarray(validation_target, dtype=int)[validation_mask]
                    if validation_target is not None
                    else np.asarray([], dtype=int)
                )
                if (
                    validation_values is not None
                    and len(observed_validation_target)
                    and len(np.unique(observed_validation_target)) >= 2
                ):
                    fit_kwargs.update({
                        "eval_set": [(
                            validation_values[validation_mask][:, head_indices],
                            observed_validation_target,
                        )],
                        "callbacks": [
                            lgb.early_stopping(
                                int(
                                    LIGHTGBM_PARAMS.get(
                                        "early_stopping_rounds",
                                    )
                                    or 50
                                ),
                                verbose=False,
                            )
                        ],
                    })
                classifier.fit(
                    head_train_values[observed_mask],
                    observed_target,
                    **fit_kwargs,
                )
            heads[name] = classifier
            validation_target = validation_targets.get(name)
            validation_length = (
                len(validation_target)
                if validation_target is not None
                else 0
            )
            validation_mask = np.asarray(
                validation_masks.get(
                    name,
                    np.zeros(validation_length, dtype=bool),
                ),
                dtype=bool,
            )
            if (
                validation_values is not None
                and validation_target is not None
                and bool(np.any(validation_mask))
            ):
                raw = _canonical_class_probabilities(
                    classifier,
                    validation_values[validation_mask][:, head_indices],
                    class_count=2,
                )
                temperatures[name] = _validation_temperature(
                    raw,
                    np.asarray(validation_target, dtype=int)[validation_mask],
                )
            else:
                temperatures[name] = 1.0
            if baseline_shrinkage_enabled:
                training_group_values = np.asarray(
                    train_groups, dtype=object,
                )[observed_mask].tolist()
                baseline_probability_models[name] = fit_event_probability_baseline(
                    observed_target,
                    training_group_values,
                )
                if (
                    validation_values is not None
                    and validation_target is not None
                    and bool(np.any(validation_mask))
                ):
                    validation_model_probability = _temperature_scale_probabilities(
                        _canonical_class_probabilities(
                            classifier,
                            validation_values[validation_mask][:, head_indices],
                            class_count=2,
                        ),
                        float(temperatures[name]),
                    )[:, 1]
                    validation_group_values = np.asarray(
                        validation_groups, dtype=object,
                    )[validation_mask].tolist()
                    validation_baseline_probability = apply_event_probability_baseline(
                        baseline_probability_models[name],
                        validation_group_values,
                    )
                    baseline_blend_weights[name] = _validation_baseline_blend_weight(
                        np.asarray(validation_target, dtype=float)[validation_mask],
                        validation_model_probability,
                        validation_baseline_probability,
                    )
                else:
                    baseline_blend_weights[name] = 0.0

        for name, alternative_names in event_alternative_head_feature_names.items():
            target = targets[name]
            observed_mask = np.asarray(target_masks[name], dtype=bool)
            observed_target = np.asarray(target, dtype=int)[observed_mask]
            counts = np.bincount(observed_target, minlength=2).astype(float)
            validation_target = validation_targets.get(name)
            validation_length = len(validation_target) if validation_target is not None else 0
            validation_mask = np.asarray(
                validation_masks.get(
                    name, np.zeros(validation_length, dtype=bool),
                ),
                dtype=bool,
            )
            alternative_indices = [
                selected_features.index(feature) for feature in alternative_names
            ]
            if int(np.count_nonzero(counts)) < 2:
                alternative_classifier: Any = _ConstantProbabilityClassifier(
                    (counts + 1.0).tolist()
                )
            else:
                alternative_classifier = lgb.LGBMClassifier(
                    objective="binary",
                    metric="binary_logloss",
                    **fit_params,
                )
                alternative_fit: dict[str, Any] = {}
                if weights is not None:
                    alternative_fit["sample_weight"] = np.asarray(weights)[
                        observed_mask
                    ]
                observed_validation_target = (
                    np.asarray(validation_target, dtype=int)[validation_mask]
                    if validation_target is not None
                    else np.asarray([], dtype=int)
                )
                if (
                    validation_values is not None
                    and len(observed_validation_target)
                    and len(np.unique(observed_validation_target)) >= 2
                ):
                    alternative_fit.update({
                        "eval_set": [(
                            validation_values[validation_mask][:, alternative_indices],
                            observed_validation_target,
                        )],
                        "callbacks": [
                            lgb.early_stopping(
                                int(
                                    LIGHTGBM_PARAMS.get("early_stopping_rounds")
                                    or 50
                                ),
                                verbose=False,
                            )
                        ],
                    })
                alternative_classifier.fit(
                    train_values[observed_mask][:, alternative_indices],
                    observed_target,
                    **alternative_fit,
                )
            if (
                validation_values is None
                or validation_target is None
                or not bool(np.any(validation_mask))
            ):
                head_candidate_selection[name] = {
                    "selected": "primary",
                    "reason": "validation_rows_unavailable",
                }
                continue
            validation_actual = np.asarray(
                validation_target, dtype=float,
            )[validation_mask]
            primary_indices = bundle_head_feature_indices[name]
            primary_prediction = _temperature_scale_probabilities(
                _canonical_class_probabilities(
                    heads[name],
                    validation_values[validation_mask][:, primary_indices],
                    class_count=2,
                ),
                float(temperatures[name]),
            )[:, 1]
            alternative_raw = _canonical_class_probabilities(
                alternative_classifier,
                validation_values[validation_mask][:, alternative_indices],
                class_count=2,
            )
            alternative_temperature = _validation_temperature(
                alternative_raw,
                validation_actual.astype(int),
            )
            alternative_prediction = _temperature_scale_probabilities(
                alternative_raw,
                alternative_temperature,
            )[:, 1]
            primary_weight = float(
                baseline_blend_weights.get(name, 1.0)
                if baseline_shrinkage_enabled else 1.0
            )
            alternative_weight = 1.0
            if baseline_shrinkage_enabled:
                validation_group_values = np.asarray(
                    validation_groups, dtype=object,
                )[validation_mask].tolist()
                baseline_prediction = apply_event_probability_baseline(
                    baseline_probability_models[name],
                    validation_group_values,
                )
                primary_prediction = baseline_prediction + primary_weight * (
                    primary_prediction - baseline_prediction
                )
                alternative_weight = _validation_baseline_blend_weight(
                    validation_actual,
                    alternative_prediction,
                    baseline_prediction,
                )
                alternative_prediction = baseline_prediction + alternative_weight * (
                    alternative_prediction - baseline_prediction
                )
            primary_loss = float(np.mean(np.square(
                primary_prediction - validation_actual
            )))
            alternative_loss = float(np.mean(np.square(
                alternative_prediction - validation_actual
            )))
            selected = "alternative" if alternative_loss < primary_loss - 1e-12 else "primary"
            head_candidate_selection[name] = {
                "contract": "validation_only_head_feature_candidate_selection_v1",
                "selected": selected,
                "primary_features": list(bundle_head_feature_names[name]),
                "alternative_features": list(alternative_names),
                "primary_validation_brier": round(primary_loss, 10),
                "alternative_validation_brier": round(alternative_loss, 10),
                "primary_baseline_blend_weight": primary_weight,
                "alternative_baseline_blend_weight": alternative_weight,
                "test_labels_consumed": False,
            }
            if selected == "alternative":
                heads[name] = alternative_classifier
                temperatures[name] = alternative_temperature
                bundle_head_feature_names[name] = list(alternative_names)
                bundle_head_feature_indices[name] = alternative_indices
                if baseline_shrinkage_enabled:
                    baseline_blend_weights[name] = alternative_weight

        quantiles: dict[str, Any] = {}
        quantile_offsets: dict[str, float] = {}
        baseline_quantile_models: dict[str, dict[str, Any]] = {}
        for name, alpha in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
            quantile = lgb.LGBMRegressor(
                objective="quantile",
                alpha=alpha,
                metric="quantile",
                **fit_params,
            )
            fit_kwargs = {}
            if weights is not None:
                fit_kwargs["sample_weight"] = weights
            if validation_values is not None and validation_returns is not None:
                fit_kwargs.update({
                    "eval_set": [(validation_values, validation_returns)],
                    "callbacks": [
                        lgb.early_stopping(
                            int(
                                LIGHTGBM_PARAMS.get("early_stopping_rounds")
                                or 50
                            ),
                            verbose=False,
                        )
                    ],
                })
            quantile.fit(train_values, train_returns, **fit_kwargs)
            quantiles[name] = quantile
            if validation_values is not None and validation_returns is not None:
                validation_prediction = _predict_values(
                    quantile,
                    validation_values,
                )
                residuals = np.asarray(validation_returns, dtype=float) - validation_prediction
                quantile_offsets[name] = float(np.quantile(residuals, alpha))
            else:
                quantile_offsets[name] = 0.0
            if baseline_shrinkage_enabled:
                baseline_quantile_models[name] = fit_event_quantile_baseline(
                    train_returns,
                    train_groups,
                    alpha,
                )
                if validation_values is not None and validation_returns is not None:
                    validation_model_quantile = _predict_values(
                        quantile, validation_values,
                    ) + float(quantile_offsets[name])
                    validation_baseline_quantile = apply_event_quantile_baseline(
                        baseline_quantile_models[name],
                        validation_groups,
                    )
                    baseline_blend_weights[name] = _validation_baseline_blend_weight(
                        np.asarray(validation_returns, dtype=float),
                        validation_model_quantile,
                        validation_baseline_quantile,
                        alpha=alpha,
                    )
                else:
                    baseline_blend_weights[name] = 0.0
        return {
            "bundle_type": "event_impact_assessor_multihead_v4",
            "impact_contract": EVENT_IMPACT_CONTRACT,
            "target_contract": EVENT_TARGET_CONTRACT,
            "transaction_cost_rate": EVENT_TRANSACTION_COST_RATE,
            **heads,
            **quantiles,
            "temperature": temperatures,
            "quantile_offsets": quantile_offsets,
            "quantile_calibration": {
                "contract": "validation_residual_additive_quantile_calibration_v1",
                "no_test_labels_consumed": True,
                "monotonic_postprocess": "per_row_sorted_q10_q50_q90",
            },
            "training_support": support,
            "head_feature_names": bundle_head_feature_names,
            "head_feature_indices": bundle_head_feature_indices,
            "head_feature_subset_contract": str(
                candidate_spec.get("event_head_feature_subset_contract") or ""
            ),
            "event_baseline_shrinkage_contract": str(
                candidate_spec.get("event_baseline_shrinkage_contract") or ""
            ),
            "event_baseline_models": (
                {
                    "probability": baseline_probability_models,
                    "quantile": baseline_quantile_models,
                }
                if baseline_shrinkage_enabled else {}
            ),
            "event_baseline_blend_weights": (
                baseline_blend_weights if baseline_shrinkage_enabled else {}
            ),
            "event_baseline_blend_selection": (
                "validation_only_grid_0_025_050_075_100_tie_prefers_lower_model_weight"
                if baseline_shrinkage_enabled else "disabled"
            ),
            "event_head_candidate_selection": head_candidate_selection,
            "target_definition": {
                "contract": train_target_package["contract"],
                "definitions": train_target_package["definitions"],
                "direction_magnitude_multiplication_forbidden": True,
                "unobservable_direction_excluded": True,
            },
        }

    def _fit_narrative_bundle(
        train_values: np.ndarray,
        train_current: np.ndarray,
        train_future: np.ndarray,
        *,
        fit_params: dict[str, Any],
        weights: np.ndarray | None = None,
        validation_values: np.ndarray | None = None,
        validation_current: np.ndarray | None = None,
        validation_future: np.ndarray | None = None,
    ) -> dict[str, Any]:
        train_current = np.asarray(train_current, dtype=int)
        train_future = np.asarray(train_future, dtype=int)
        transition_target = (train_future != train_current).astype(int)
        transition_counts = np.bincount(transition_target, minlength=2).astype(float)
        if int(np.count_nonzero(transition_counts)) < 2:
            transition_classifier: Any = _ConstantProbabilityClassifier(
                (transition_counts + 1.0).tolist()
            )
        else:
            transition_classifier = lgb.LGBMClassifier(
                objective="binary",
                metric="binary_logloss",
                **fit_params,
            )
            transition_fit: dict[str, Any] = {}
            if weights is not None:
                transition_fit["sample_weight"] = weights
            if (
                validation_values is not None
                and validation_current is not None
                and validation_future is not None
            ):
                validation_transition = (
                    np.asarray(validation_future, dtype=int)
                    != np.asarray(validation_current, dtype=int)
                ).astype(int)
                if len(np.unique(validation_transition)) >= 2:
                    transition_fit.update({
                        "eval_set": [(validation_values, validation_transition)],
                        "callbacks": [
                            lgb.early_stopping(
                                int(
                                    LIGHTGBM_PARAMS.get(
                                        "early_stopping_rounds",
                                    )
                                    or 50
                                ),
                                verbose=False,
                            )
                        ],
                    })
            transition_classifier.fit(
                train_values,
                transition_target,
                **transition_fit,
            )

        changed = transition_target.astype(bool)
        destination_counts = np.bincount(
            train_future[changed],
            minlength=len(NARRATIVE_LIFECYCLE_CLASSES),
        ).astype(float)
        if (
            int(changed.sum()) < 20
            or int(np.count_nonzero(destination_counts)) < 2
        ):
            destination_classifier: Any = _ConstantProbabilityClassifier(
                (destination_counts + 1.0).tolist()
            )
        else:
            destination_classifier = lgb.LGBMClassifier(
                objective=multiclass_objective,
                num_class=len(NARRATIVE_LIFECYCLE_CLASSES),
                metric="multi_logloss",
                **fit_params,
            )
            destination_fit: dict[str, Any] = {}
            if weights is not None:
                destination_fit["sample_weight"] = weights[changed]
            if (
                validation_values is not None
                and validation_current is not None
                and validation_future is not None
            ):
                validation_changed = (
                    np.asarray(validation_future, dtype=int)
                    != np.asarray(validation_current, dtype=int)
                )
                validation_destinations = np.asarray(
                    validation_future,
                    dtype=int,
                )[validation_changed]
                if (
                    int(validation_changed.sum()) >= 20
                    and len(np.unique(validation_destinations)) >= 2
                ):
                    destination_fit.update({
                        "eval_set": [(
                            validation_values[validation_changed],
                            validation_destinations,
                        )],
                        "callbacks": [
                            lgb.early_stopping(
                                int(
                                    LIGHTGBM_PARAMS.get(
                                        "early_stopping_rounds",
                                    )
                                    or 50
                                ),
                                verbose=False,
                            )
                        ],
                    })
            destination_classifier.fit(
                train_values[changed],
                train_future[changed],
                **destination_fit,
            )
        bundle = {
            "bundle_type": "narrative_lifecycle_two_stage_v2",
            "transition_classifier": transition_classifier,
            "destination_classifier": destination_classifier,
            "classes": list(NARRATIVE_LIFECYCLE_CLASSES),
            "allowed_destinations": {
                int(source): list(destinations)
                for source, destinations
                in NARRATIVE_HORIZON_DESTINATIONS.items()
            },
            "daily_transition_graph": {
                int(source): list(destinations)
                for source, destinations
                in NARRATIVE_ALLOWED_DESTINATIONS.items()
            },
            "training_counts": {
                "observations": int(len(train_values)),
                "transitions": int(changed.sum()),
                "transition_rate": round(float(changed.mean()), 8),
                "destination_support": destination_counts.astype(int).tolist(),
            },
            "no_oversampling": True,
        }
        if (
            validation_values is not None
            and validation_current is not None
            and validation_future is not None
        ):
            validation_current_values = np.asarray(
                validation_current,
                dtype=int,
            )
            validation_future_values = np.asarray(
                validation_future,
                dtype=int,
            )
            validation_transition = (
                validation_future_values != validation_current_values
            ).astype(int)
            transition_probabilities = _canonical_class_probabilities(
                transition_classifier,
                validation_values,
                class_count=2,
            )
            bundle["transition_temperature"] = _validation_temperature(
                transition_probabilities,
                validation_transition,
            )
            validation_changed = validation_transition.astype(bool)
            if int(validation_changed.sum()) >= 20:
                destination_probabilities = _canonical_class_probabilities(
                    destination_classifier,
                    validation_values[validation_changed],
                    class_count=len(NARRATIVE_LIFECYCLE_CLASSES),
                )
                bundle["destination_temperature"] = _validation_temperature(
                    destination_probabilities,
                    validation_future_values[validation_changed],
                )
        bundle.setdefault("transition_temperature", 1.0)
        bundle.setdefault("destination_temperature", 1.0)
        bundle["transition_logit_bias"] = 0.0
        lifecycle_effect_authorization = {
            "contract": (
                "validation_only_final_state_vs_persistence_authorization_v1"
            ),
            "authorized": False,
            "reason": "validation_rows_unavailable",
            "model_macro_f1": None,
            "state_persistence_macro_f1": None,
            "test_labels_consumed": False,
        }
        if (
            validation_values is not None
            and validation_current is not None
            and validation_future is not None
            and len(validation_values) == len(validation_future)
            and len(validation_values) > 0
        ):
            validation_current_values = np.asarray(
                validation_current,
                dtype=int,
            )
            validation_future_values = np.asarray(
                validation_future,
                dtype=int,
            )
            validation_final, _, _ = _narrative_two_stage_probabilities(
                bundle,
                np.asarray(validation_values, dtype=float),
                validation_current_values,
            )
            labels = list(range(len(NARRATIVE_LIFECYCLE_CLASSES)))
            model_macro_f1 = float(f1_score(
                validation_future_values,
                np.argmax(validation_final, axis=1),
                labels=labels,
                average="macro",
                zero_division=0,
            ))
            persistence_macro_f1 = float(f1_score(
                validation_future_values,
                validation_current_values,
                labels=labels,
                average="macro",
                zero_division=0,
            ))
            authorized = model_macro_f1 > persistence_macro_f1
            lifecycle_effect_authorization.update({
                "authorized": authorized,
                "reason": (
                    "validation_final_state_beats_persistence"
                    if authorized
                    else "validation_final_state_does_not_beat_persistence"
                ),
                "model_macro_f1": round(model_macro_f1, 8),
                "state_persistence_macro_f1": round(
                    persistence_macro_f1,
                    8,
                ),
            })
        bundle["lifecycle_effect_authorization"] = (
            lifecycle_effect_authorization
        )
        bundle["calibration"] = {
            "contract": "validation_only_temperature_scaling_v2",
            "transition_temperature": bundle["transition_temperature"],
            "destination_temperature": bundle["destination_temperature"],
            "transition_logit_bias": 0.0,
        }
        return bundle

    if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
        model = lgb.LGBMRanker(objective=rank_objective, metric="ndcg", label_gain=PLAN_MODELS[model_key].get("label_gain", [0, 1, 3, 7, 15]), **params)
        fit_kwargs = {"group": dataset["group_train"], "eval_group": [dataset["group_val"]]}
        y_train = dataset["y_train_rank"]
        y_val = dataset["y_val_rank"]
    elif model_key == "capital_persistence_multitask_v1":
        direction = lgb.LGBMClassifier(objective=multiclass_objective, num_class=5, metric="multi_logloss", **params)
        direction.fit(x_train, dataset["y_train_rank"], eval_set=[(x_val, dataset["y_val_rank"])], callbacks=[lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)], **({"sample_weight": sample_weight} if sample_weight is not None else {}))
        quantile_models: dict[str, Any] = {}
        for name, alpha in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
            quantile = lgb.LGBMRegressor(objective="quantile", alpha=alpha, metric="quantile", **params)
            quantile.fit(x_train, dataset["y_train_task_target"], eval_set=[(x_val, dataset["y_val_task_target"])], callbacks=[lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)], **({"sample_weight": sample_weight} if sample_weight is not None else {}))
            quantile_models[name] = quantile
        hazard = lgb.LGBMClassifier(objective="binary", metric="binary_logloss", **params)
        hazard.fit(x_train, (np.asarray(dataset["y_train_task_target"]) < 0).astype(int), eval_set=[(x_val, (np.asarray(dataset["y_val_task_target"]) < 0).astype(int))], callbacks=[lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)], **({"sample_weight": sample_weight} if sample_weight is not None else {}))
        model = {"bundle_type": "capital_persistence_multihead_v1", "direction": direction, **quantile_models, "hazard": hazard}
        fit_kwargs = {}
        y_train = dataset["y_train_rank"]
        y_val = dataset["y_val_rank"]
    elif model_key == "event_impact_assessor_v2":
        model = _fit_event_bundle(
            x_train,
            dataset["y_train_abnormal_return"],
            dataset["y_train_event_reaction"],
            dataset["y_train_event_direction_signal"],
            list(dataset.get("train_event_sources") or []),
            list(dataset.get("train_event_types") or []),
            fit_params=params,
            weights=sample_weight,
            validation_values=x_val,
            validation_returns=dataset["y_val_abnormal_return"],
            validation_reaction=dataset["y_val_event_reaction"],
            validation_direction=dataset["y_val_event_direction_signal"],
            validation_sources=[
                str(value)
                for value, selected in zip(
                    dataset.get("all_event_sources") or [],
                    np.asarray(dataset["_validation_mask"], dtype=bool),
                )
                if bool(selected)
            ],
            validation_types=[
                str(value)
                for value, selected in zip(
                    dataset.get("all_event_types") or [],
                    np.asarray(dataset["_validation_mask"], dtype=bool),
                )
                if bool(selected)
            ],
        )
        fit_kwargs = {}
        y_train = dataset["y_train_abnormal_return"]
        y_val = dataset["y_val_abnormal_return"]
    elif model_key == "narrative_transition_multitask_v1":
        model = _fit_narrative_bundle(
            x_train,
            dataset["y_train_current_lifecycle"],
            dataset["y_train_lifecycle"],
            fit_params=params,
            weights=sample_weight,
            validation_values=x_val,
            validation_current=dataset["y_val_current_lifecycle"],
            validation_future=dataset["y_val_lifecycle"],
        )
        fit_kwargs = {}
        y_train = dataset["y_train_lifecycle"]
        y_val = dataset["y_val_lifecycle"]
    else:
        model = lgb.LGBMRegressor(objective=regression_objective, metric="l2", **params)
        fit_kwargs = {}
        y_train = (
            dataset["y_train_abnormal_return"]
            if model_key == "event_impact_assessor_v2"
            else dataset["y_train_return"]
        )
        y_val = (
            dataset["y_val_abnormal_return"]
            if model_key == "event_impact_assessor_v2"
            else dataset["y_val_return"]
        )
    if sample_weight is not None:
        fit_kwargs["sample_weight"] = sample_weight
    callbacks = [lgb.early_stopping(int(LIGHTGBM_PARAMS.get("early_stopping_rounds") or 50), verbose=False)]
    if not isinstance(model, dict) or model.get("bundle_type") == "narrative_transition_classifier_v1":
        fit_model = model["classifier"] if isinstance(model, dict) else model
        fit_model.fit(x_train, y_train, eval_set=[(x_val, y_val)], callbacks=callbacks, **fit_kwargs)

    def _task_native_evaluation(
        fitted: Any,
        train_mask: np.ndarray,
        evaluation_mask: np.ndarray,
    ) -> dict[str, Any]:
        all_values = np.asarray(dataset["x_all"], dtype=float)
        selected_values = _select(all_values)
        evaluation_values = selected_values[evaluation_mask]
        all_dates_values = list(dataset.get("all_dates") or [])
        evaluation_dates = [
            str(value)
            for value, selected in zip(all_dates_values, evaluation_mask)
            if bool(selected)
        ]
        train_dates = [
            str(value)
            for value, selected in zip(all_dates_values, train_mask)
            if bool(selected)
        ]
        if model_key in {
            "medium_horizon_lambdarank_v1",
            "leader_lambdarank_v1",
        }:
            all_queries = list(
                dataset.get("all_ranking_queries") or all_dates_values
            )
            return evaluate_learning_to_rank(
                model_key=model_key,
                relevance=np.asarray(dataset["y_all_rank"])[evaluation_mask],
                model_scores=_model_score(fitted, evaluation_values),
                baseline_scores=all_values[
                    evaluation_mask,
                    PLAN_MODEL_FEATURES.index("momentum_20"),
                ],
                groups=[
                    str(value)
                    for value, selected in zip(all_queries, evaluation_mask)
                    if bool(selected)
                ],
                eval_at=PLAN_MODELS[model_key].get("eval_at") or [5, 10, 20],
            )
        if model_key == "capital_persistence_multitask_v1":
            return evaluate_capital_multitask(
                direction_probabilities=_canonical_class_probabilities(
                    fitted["direction"],
                    evaluation_values,
                    class_count=5,
                ),
                actual_direction=np.asarray(dataset["y_all_rank"])[evaluation_mask],
                current_persistence=all_values[
                    evaluation_mask,
                    PLAN_MODEL_FEATURES.index("capital_persistence"),
                ],
                dates=evaluation_dates,
                train_current_persistence=all_values[
                    train_mask,
                    PLAN_MODEL_FEATURES.index("capital_persistence"),
                ],
                train_dates=train_dates,
                train_future_direction=np.asarray(dataset["y_all_rank"])[train_mask],
                actual_strength=np.asarray(
                    dataset["y_all_task_target"],
                    dtype=float,
                )[evaluation_mask],
                train_strength=np.asarray(
                    dataset["y_all_task_target"],
                    dtype=float,
                )[train_mask],
                q10=_predict_values(fitted["q10"], evaluation_values),
                q50=_predict_values(fitted["q50"], evaluation_values),
                q90=_predict_values(fitted["q90"], evaluation_values),
                failure_probability=_canonical_class_probabilities(
                    fitted["hazard"],
                    evaluation_values,
                    class_count=2,
                )[:, 1],
            )
        if model_key == "narrative_transition_multitask_v1":
            evaluation_current = np.asarray(
                dataset["y_all_current_lifecycle"],
                dtype=int,
            )[evaluation_mask]
            probabilities, transition_probability, destination_probabilities = (
                _narrative_two_stage_probabilities(
                    fitted,
                    evaluation_values,
                    evaluation_current,
                )
            )
            return evaluate_narrative_transition(
                probabilities=probabilities,
                transition_probability=transition_probability,
                destination_probabilities=destination_probabilities,
                actual_future=np.asarray(
                    dataset["y_all_lifecycle"],
                    dtype=int,
                )[evaluation_mask],
                current_state=evaluation_current,
                train_current_state=np.asarray(
                    dataset["y_all_current_lifecycle"],
                    dtype=int,
                )[train_mask],
                train_future_state=np.asarray(
                    dataset["y_all_lifecycle"],
                    dtype=int,
                )[train_mask],
            )
        if model_key == "event_impact_assessor_v2":
            all_event_sources = list(dataset.get("all_event_sources") or [])
            all_event_types = list(dataset.get("all_event_types") or [])
            evaluation_sources = [
                str(value)
                for value, selected in zip(all_event_sources, evaluation_mask)
                if bool(selected)
            ]
            evaluation_types = [
                str(value)
                for value, selected in zip(all_event_types, evaluation_mask)
                if bool(selected)
            ]
            evaluation_reaction = np.asarray(
                dataset["y_all_event_reaction"], dtype=float,
            )[evaluation_mask]
            evaluation_direction = np.asarray(
                dataset["y_all_event_direction_signal"], dtype=float,
            )[evaluation_mask]
            event_outputs = _event_impact_bundle_outputs(
                fitted,
                evaluation_values,
                event_sources=evaluation_sources,
                event_types=evaluation_types,
                event_reaction=evaluation_reaction,
                event_direction_signal=evaluation_direction,
            )
            return evaluate_event_impact(
                positive_impact_probability=event_outputs["positive_impact"],
                priced_in_probability=event_outputs["priced_in"],
                continuation_probability=event_outputs["continuation"],
                reversal_probability=event_outputs["reversal"],
                direction_realization_probability=event_outputs[
                    "direction_realization"
                ],
                q10=event_outputs["q10"],
                q50=event_outputs["q50"],
                q90=event_outputs["q90"],
                actual_abnormal_return=np.asarray(
                    dataset["y_all_abnormal_return"],
                    dtype=float,
                )[evaluation_mask],
                train_abnormal_return=np.asarray(
                    dataset["y_all_abnormal_return"],
                    dtype=float,
                )[train_mask],
                event_reaction=evaluation_reaction,
                train_event_reaction=np.asarray(
                    dataset["y_all_event_reaction"],
                    dtype=float,
                )[train_mask],
                event_direction_signal=evaluation_direction,
                train_event_direction_signal=np.asarray(
                    dataset["y_all_event_direction_signal"],
                    dtype=float,
                )[train_mask],
                event_sources=evaluation_sources,
                train_event_sources=[
                    str(value)
                    for value, selected in zip(
                        all_event_sources,
                        train_mask,
                    )
                    if bool(selected)
                ],
                event_types=evaluation_types,
                train_event_types=[
                    str(value)
                    for value, selected in zip(
                        all_event_types,
                        train_mask,
                    )
                    if bool(selected)
                ],
                transaction_cost_rate=EVENT_TRANSACTION_COST_RATE,
                target_contract=EVENT_TARGET_CONTRACT,
            )
        raise ValueError(f"unsupported task-native evaluation: {model_key}")

    task_metrics = _task_native_evaluation(
        model,
        np.asarray(dataset["_train_mask"], dtype=bool),
        np.asarray(dataset["_test_mask"], dtype=bool),
    )
    task_metric_passed = bool(task_metrics.get("passed"))
    market_state_observations = _observation_counts(
        list(dataset.get("test_market_states") or []),
        expected=["S0", "S1", "S2", "S3"],
    )
    market_profile_observations = _observation_counts(
        list(dataset.get("test_market_profiles") or []),
        expected=[f"P{index:02d}" for index in range(1, 13)],
    )
    market_state_coverage_complete = all(
        int(market_state_observations.get(state) or 0) > 0
        for state in ("S0", "S1", "S2", "S3")
    )
    covered_market_profiles = sum(
        1
        for index in range(1, 13)
        if int(market_profile_observations.get(f"P{index:02d}") or 0) > 0
    )
    market_profile_coverage_complete = covered_market_profiles >= 8
    all_dates = np.asarray(dataset.get("all_dates") or [], dtype=object)
    unique_all_dates = sorted({str(value) for value in all_dates.tolist()})
    fold_task_passes: list[bool] = []
    fold_task_baseline_passes: list[bool] = []
    fold_evidence: list[dict[str, Any]] = []
    fold_models: list[dict[str, Any]] = []
    fold_window_builder = (
        _event_walk_forward_date_windows
        if model_key == "event_impact_assessor_v2"
        else _walk_forward_date_windows
    )
    fold_date_windows = fold_window_builder(unique_all_dates)
    if quick_diagnostic:
        fold_date_windows = fold_date_windows[-2:]
    event_distribution_label_stability_audit = (
        _event_distribution_label_stability_audit(dataset, fold_date_windows)
        if model_key == "event_impact_assessor_v2"
        else {}
    )
    fold_params = {
        **params,
        "n_estimators": min(
            200 if quick_diagnostic else 500,
            int(params.get("n_estimators") or 500),
        ),
    }
    for fold_no, fold_window in enumerate(fold_date_windows, start=1):
        train_dates = set(fold_window["train"])
        validation_dates = set(fold_window["validation"])
        evaluation_set = set(fold_window["test"])
        train_mask_fold = np.asarray([str(value) in train_dates for value in all_dates], dtype=bool)
        validation_mask_fold = np.asarray(
            [str(value) in validation_dates for value in all_dates],
            dtype=bool,
        )
        test_mask_fold = np.asarray([str(value) in evaluation_set for value in all_dates], dtype=bool)
        fold_sample_weight = None
        target_states = {
            str(item) for item in candidate_spec.get("target_market_states") or [] if str(item)
        }
        if target_states:
            fold_sample_weight = np.asarray([
                2.0 if str(state) in target_states else 1.0
                for state, selected in zip(dataset.get("all_market_states") or [], train_mask_fold)
                if selected
            ], dtype=float)
        if int(train_mask_fold.sum()) < 100 or int(test_mask_fold.sum()) < 50:
            raise RuntimeError(f"Walk-forward第{fold_no}窗样本行不足")
        x_all = _select(dataset["x_all"])
        if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
            fold_model = lgb.LGBMRanker(objective=rank_objective, metric="ndcg", label_gain=PLAN_MODELS[model_key].get("label_gain", [0, 1, 3, 7, 15]), **fold_params)
            all_queries = list(dataset.get("all_ranking_queries") or all_dates.tolist())
            group_counts = Counter(
                str(value)
                for value, selected in zip(all_queries, train_mask_fold)
                if bool(selected)
            )
            ordered_queries = list(dict.fromkeys(
                str(value)
                for value, selected in zip(all_queries, train_mask_fold)
                if bool(selected)
            ))
            groups = [group_counts[query] for query in ordered_queries if group_counts[query]]
            fold_model.fit(
                x_all[train_mask_fold],
                np.asarray(dataset["y_all_rank"])[train_mask_fold],
                group=[value for value in groups if value],
                **({"sample_weight": fold_sample_weight} if fold_sample_weight is not None else {}),
            )
        elif model_key == "capital_persistence_multitask_v1":
            direction = lgb.LGBMClassifier(
                objective=multiclass_objective,
                num_class=5,
                metric="multi_logloss",
                **fold_params,
            )
            direction.fit(
                x_all[train_mask_fold],
                np.asarray(dataset["y_all_rank"])[train_mask_fold],
                **({"sample_weight": fold_sample_weight} if fold_sample_weight is not None else {}),
            )
            quantile_models: dict[str, Any] = {}
            all_task_target = np.asarray(dataset["y_all_task_target"], dtype=float)
            for name, alpha in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
                quantile = lgb.LGBMRegressor(
                    objective="quantile",
                    alpha=alpha,
                    metric="quantile",
                    **fold_params,
                )
                quantile.fit(
                    x_all[train_mask_fold],
                    all_task_target[train_mask_fold],
                    **(
                        {"sample_weight": fold_sample_weight}
                        if fold_sample_weight is not None else {}
                    ),
                )
                quantile_models[name] = quantile
            hazard = lgb.LGBMClassifier(
                objective="binary",
                metric="binary_logloss",
                **fold_params,
            )
            hazard.fit(
                x_all[train_mask_fold],
                (all_task_target[train_mask_fold] < 0).astype(int),
                **(
                    {"sample_weight": fold_sample_weight}
                    if fold_sample_weight is not None else {}
                ),
            )
            fold_model = {
                "bundle_type": "capital_persistence_multihead_v1",
                "direction": direction,
                **quantile_models,
                "hazard": hazard,
            }
        elif model_key == "event_impact_assessor_v2":
            fold_model = _fit_event_bundle(
                x_all[train_mask_fold],
                np.asarray(
                    dataset["y_all_abnormal_return"],
                    dtype=float,
                )[train_mask_fold],
                np.asarray(
                    dataset["y_all_event_reaction"],
                    dtype=float,
                )[train_mask_fold],
                np.asarray(
                    dataset["y_all_event_direction_signal"],
                    dtype=float,
                )[train_mask_fold],
                [
                    str(value)
                    for value, selected in zip(
                        dataset.get("all_event_sources") or [],
                        train_mask_fold,
                    )
                    if bool(selected)
                ],
                [
                    str(value)
                    for value, selected in zip(
                        dataset.get("all_event_types") or [],
                        train_mask_fold,
                    )
                    if bool(selected)
                ],
                fit_params=fold_params,
                weights=fold_sample_weight,
                validation_values=x_all[validation_mask_fold],
                validation_returns=np.asarray(
                    dataset["y_all_abnormal_return"],
                    dtype=float,
                )[validation_mask_fold],
                validation_reaction=np.asarray(
                    dataset["y_all_event_reaction"],
                    dtype=float,
                )[validation_mask_fold],
                validation_direction=np.asarray(
                    dataset["y_all_event_direction_signal"],
                    dtype=float,
                )[validation_mask_fold],
                validation_sources=[
                    str(value)
                    for value, selected in zip(
                        dataset.get("all_event_sources") or [],
                        validation_mask_fold,
                    )
                    if bool(selected)
                ],
                validation_types=[
                    str(value)
                    for value, selected in zip(
                        dataset.get("all_event_types") or [],
                        validation_mask_fold,
                    )
                    if bool(selected)
                ],
            )
        elif model_key == "narrative_transition_multitask_v1":
            fold_model = _fit_narrative_bundle(
                x_all[train_mask_fold],
                np.asarray(
                    dataset["y_all_current_lifecycle"],
                    dtype=int,
                )[train_mask_fold],
                np.asarray(dataset["y_all_lifecycle"])[train_mask_fold],
                fit_params=fold_params,
                weights=fold_sample_weight,
                validation_values=x_all[validation_mask_fold],
                validation_current=np.asarray(
                    dataset["y_all_current_lifecycle"],
                    dtype=int,
                )[validation_mask_fold],
                validation_future=np.asarray(
                    dataset["y_all_lifecycle"],
                    dtype=int,
                )[validation_mask_fold],
            )
        else:
            fold_model = lgb.LGBMRegressor(objective=regression_objective, metric="l2", **fold_params)
            fold_model.fit(
                x_all[train_mask_fold],
                (
                    np.asarray(dataset["y_all_abnormal_return"], dtype=float)[train_mask_fold]
                    if model_key == "event_impact_assessor_v2"
                    else np.asarray(dataset["y_all_return"], dtype=float)[train_mask_fold]
                ),
                **({"sample_weight": fold_sample_weight} if fold_sample_weight is not None else {}),
            )
        fold_task = _task_native_evaluation(
            fold_model,
            train_mask_fold,
            test_mask_fold,
        )
        fold_task_passes.append(bool(fold_task.get("passed")))
        fold_task_baseline_passes.append(
            bool(fold_task.get("baseline_outperformed"))
        )
        fold_states = [
            str(value)
            for value, selected in zip(dataset.get("all_market_states") or [], test_mask_fold)
            if selected
        ]
        if isinstance(fold_model, dict):
            importance_model = (
                fold_model.get("direction")
                if fold_model.get("bundle_type")
                == "capital_persistence_multihead_v1"
                else fold_model.get("positive_impact")
                if fold_model.get("bundle_type") in {
                    "event_impact_assessor_multihead_v2",
                    "event_impact_assessor_multihead_v3",
                    "event_impact_assessor_multihead_v4",
                }
                else fold_model.get("transition_classifier")
                if fold_model.get("bundle_type")
                == "narrative_lifecycle_two_stage_v2"
                else fold_model.get("classifier")
            )
        else:
            importance_model = fold_model
        importance_features = selected_features
        if (
            isinstance(fold_model, dict)
            and fold_model.get("bundle_type")
            == "event_impact_assessor_multihead_v4"
            and isinstance(fold_model.get("head_feature_names"), dict)
        ):
            importance_features = list(
                fold_model["head_feature_names"].get("positive_impact")
                or selected_features
            )
        raw_importance = getattr(importance_model, "feature_importances_", None)
        importance = (
            {
                feature: round(float(value), 6)
                for feature, value in zip(
                    importance_features,
                    np.asarray(raw_importance, dtype=float),
                )
            }
            if raw_importance is not None else {}
        )
        fold_evidence.append({
            "fold": fold_no,
            "train_start": min(train_dates),
            "train_end": max(train_dates),
            "train_sessions": len(train_dates),
            "validation_start": min(validation_dates),
            "validation_end": max(validation_dates),
            "validation_sessions": len(validation_dates),
            "purge_sessions": int(_training_protocol_for_model(model_key)["purge_sessions"]),
            "embargo_sessions": int(_training_protocol_for_model(model_key)["embargo_sessions"]),
            "test_start": min(evaluation_set),
            "test_end": max(evaluation_set),
            "test_sessions": len(evaluation_set),
            "train_rows": int(train_mask_fold.sum()),
            "test_rows": int(test_mask_fold.sum()),
            "task_native_evaluation": fold_task,
            "task_native_passed": bool(fold_task.get("passed")),
            "market_state_observations": _observation_counts(
                fold_states,
                expected=["S0", "S1", "S2", "S3"],
            ),
            "feature_importance": importance,
        })
        fold_models.append({
            "fold": fold_no,
            "model": fold_model,
        })
    model_training_protocol = _training_protocol_for_model(model_key)
    expected_windows = 2 if quick_diagnostic else int(model_training_protocol["walk_forward_windows"])
    if len(fold_task_passes) != expected_windows:
        raise RuntimeError(
            f"Walk-forward窗口不完整：实际 {len(fold_task_passes)}，要求 {expected_windows}"
        )
    minimum_task_windows = len(fold_task_passes) // 2 + 1
    task_native_oos_passed = (
        sum(fold_task_passes) >= minimum_task_windows
    )
    task_native_baseline_oos_passed = (
        sum(fold_task_baseline_passes) >= minimum_task_windows
    )
    model_type = (
        "plan_lightgbm_ranker"
        if model_key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}
        else "capital_persistence_multihead_v1"
        if model_key == "capital_persistence_multitask_v1"
        else "narrative_lifecycle_two_stage_v2"
        if model_key == "narrative_transition_multitask_v1"
        else "event_impact_assessor_multihead_v4"
    )
    return {
        "model": model,
        "fold_models": fold_models,
        "model_type": model_type,
        "metrics": {
            "model_key": model_key,
            "train_rows": dataset["train_rows"],
            "validation_rows": dataset["validation_rows"],
            "test_rows": dataset["test_rows"],
            "sample_rows": dataset["sample_rows"],
            "training_trade_date_count": dataset["train_trade_date_count"],
            "validation_trade_date_count": dataset["validation_trade_date_count"],
            "test_trade_date_count": dataset["test_trade_date_count"],
            "labeled_trade_date_count": dataset["trade_date_count"],
            "oos_windows": len(fold_task_passes),
            "training_protocol": _training_protocol_for_model(model_key),
            "model_evaluation_contract": MODEL_EVALUATION_CONTRACT,
            "event_impact_contract": (
                EVENT_IMPACT_CONTRACT
                if model_key == "event_impact_assessor_v2"
                else None
            ),
            "model_evaluation_definition": evaluation_contract(model_key),
            "task_metrics": task_metrics,
            "task_metric_final_window_passed": task_metric_passed,
            "task_metric_passed": task_native_oos_passed,
            "task_native_oos": {
                "contract": (
                    "expanding_event_date_purged_walk_forward_task_native_majority_v1"
                    if model_key == "event_impact_assessor_v2"
                    else "expanding_purged_walk_forward_task_native_majority_v1"
                ),
                "window_count": len(fold_task_passes),
                "minimum_passed_windows": minimum_task_windows,
                "passed_windows": sum(fold_task_passes),
                "baseline_outperformed_windows": sum(fold_task_baseline_passes),
                "passed": task_native_oos_passed,
                "baseline_outperformed": task_native_baseline_oos_passed,
                "policy_note": (
                    "指标定义来自模型任务标准；多数窗相对基线门是ALIS治理策略，"
                    "不是论文宣称的通用收益阈值"
                ),
            },
            "walk_forward_windows": len(fold_task_passes),
            "walk_forward_evidence": fold_evidence,
            "task_native_walk_forward_contract": (
                "expanding_200_event_date_train_10_validation_10_test_"
                "purged_embargoed_task_native_v1"
                if model_key == "event_impact_assessor_v2"
                else "expanding_756_train_126_validation_126_test_purged_task_native_v1"
            ),
            "evaluation_stage": "quick_two_window_diagnostic" if quick_diagnostic else "formal_five_window",
            "candidate_spec": candidate_spec,
            "selected_features": selected_features,
            "event_head_feature_subsets": (
                event_head_feature_names
                if model_key == "event_impact_assessor_v2"
                else {}
            ),
            "event_head_feature_subset_contract": (
                str(
                    candidate_spec.get(
                        "event_head_feature_subset_contract"
                    ) or ""
                )
                if model_key == "event_impact_assessor_v2"
                else ""
            ),
            "event_baseline_shrinkage_contract": (
                str(candidate_spec.get("event_baseline_shrinkage_contract") or "")
                if model_key == "event_impact_assessor_v2"
                else ""
            ),
            "event_baseline_blend_weights": (
                dict(model.get("event_baseline_blend_weights") or {})
                if model_key == "event_impact_assessor_v2"
                and isinstance(model, dict)
                else {}
            ),
            "event_baseline_blend_selection": (
                str(model.get("event_baseline_blend_selection") or "disabled")
                if model_key == "event_impact_assessor_v2"
                and isinstance(model, dict)
                else "disabled"
            ),
            "event_head_candidate_selection": (
                dict(model.get("event_head_candidate_selection") or {})
                if model_key == "event_impact_assessor_v2"
                and isinstance(model, dict)
                else {}
            ),
            "narrative_lifecycle_effect_authorization": (
                dict(model.get("lifecycle_effect_authorization") or {})
                if model_key == "narrative_transition_multitask_v1"
                and isinstance(model, dict)
                else {}
            ),
            "feature_computation_contract": dataset.get("feature_computation_contract") or {},
            "event_feature_audit": dataset.get("event_feature_audit") or {},
            "event_distribution_label_stability_audit": (
                event_distribution_label_stability_audit
            ),
            "narrative_label_audit": dataset.get("narrative_label_audit") or {},
            "market_state_observations": market_state_observations,
            "market_profile_observations": market_profile_observations,
            "environment_coverage": {
                "parent_state_count": 4,
                "covered_parent_states": sum(
                    int(value) > 0 for value in market_state_observations.values()
                ),
                "profile_count": 12,
                "minimum_required_profiles": 8,
                "covered_profiles": covered_market_profiles,
                "parent_states_complete": market_state_coverage_complete,
                "profiles_complete": market_profile_coverage_complete,
                "complete": market_state_coverage_complete and market_profile_coverage_complete,
                "contract": (
                    "point_in_time_four_parent_states_and_at_least_eight_"
                    "of_twelve_profiles_coverage_v1"
                ),
            },
            "task_baseline": {
                "contract": str(task_metrics.get("contract") or ""),
                "comparison_executed": True,
                "outperformed": task_native_baseline_oos_passed,
                "walk_forward_windows_outperformed": sum(
                    fold_task_baseline_passes
                ),
            },
            "horizon": dataset["horizon"],
            "date_start": dataset["dates"]["start"],
            "date_split": dataset["dates"]["split"],
            "date_end": dataset["dates"]["end"],
            "date_train_end": dataset["dates"].get("train_end"),
            "date_validation_start": dataset["dates"].get("validation_start"),
            "date_validation_end": dataset["dates"].get("validation_end"),
            "date_test_start": dataset["dates"].get("test_start"),
        },
    }


def _dataset_signature(model_key: str, dataset: dict[str, Any]) -> str:
    payload = {
        "model_key": model_key,
        "rows": {
            "sample": dataset.get("sample_rows"),
            "train": dataset.get("train_rows"),
            "validation": dataset.get("validation_rows"),
            "raw": dataset.get("raw_rows"),
        },
        "dates": dataset.get("dates") or {},
        "horizon": dataset.get("horizon"),
        "feature_count": dataset.get("feature_count"),
        "features": PLAN_MODEL_FEATURES,
        "feature_computation_contract": dataset.get("feature_computation_contract") or {},
        "symbol_sampling": dataset.get("symbol_sampling") or {},
        "moneyflow_evidence": {
            "raw_rows": dataset.get("moneyflow_raw_rows"),
            "capital_observation_count": dataset.get("capital_observation_count"),
        },
        "algorithm_contract": {
            "medium_horizon_lambdarank_v1": "grouped_ndcg_spearman_lambdarank_v4",
            "leader_lambdarank_v1": "within_theme_ndcg_spearman_lambdarank_v4",
            "capital_persistence_multitask_v1": "direction_quantile_hazard_task_native_v4",
            "event_impact_assessor_v2": "structured_conditional_event_impact_multihead_task_native_v4",
            "narrative_transition_multitask_v1": "three_session_two_stage_observable_lifecycle_task_native_v2",
        }.get(model_key),
        "model_evaluation_contract": MODEL_EVALUATION_CONTRACT,
        "trade_date_count": dataset.get("trade_date_count"),
        "label_quantiles": dataset.get("label_quantiles") or [],
        "event_feature_audit": dataset.get("event_feature_audit") or {},
        "narrative_label_audit": dataset.get("narrative_label_audit") or {},
        "training_protocol": TRAINING_PROTOCOL,
        "params": LIGHTGBM_PARAMS,
        "targeted_training_context": dataset.get("targeted_training_context") or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def audit_event_model_distribution_stability(
    model_version_id: int,
    *,
    persist: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Rebuild and attach read-only drift evidence to one frozen event candidate.

    The audit is persisted only when the rebuilt point-in-time dataset has the
    exact signature recorded by the candidate.  It never changes validation,
    status, activation, parameters, predictions, or release gates.
    """

    version = get_model_version(int(model_version_id))
    if not version:
        return {
            "ok": False,
            "error": "model_version_not_found",
            "model_version_id": int(model_version_id),
        }
    if str(version.get("model_key") or "") != "event_impact_assessor_v2":
        return {
            "ok": False,
            "error": "not_event_impact_model_version",
            "model_version_id": int(model_version_id),
            "model_key": str(version.get("model_key") or ""),
        }
    before_status = str(version.get("status") or "")
    before_active = bool(version.get("is_active"))
    before_validation = dict(version.get("validation") or {})
    metrics = dict(version.get("metrics") or {})
    expected_signature = str(metrics.get("training_data_signature") or "")
    if on_progress:
        on_progress("dataset", 0.05, "重建事件候选的点时训练数据以核对冻结指纹")
    dataset = _build_plan_training_dataset(
        "event_impact_assessor_v2",
        on_progress=on_progress,
    )
    training_context = metrics.get("targeted_training_context")
    if not isinstance(training_context, dict):
        training_context = (version.get("config") or {}).get(
            "targeted_training_context"
        )
    dataset["targeted_training_context"] = (
        dict(training_context) if isinstance(training_context, dict) else {}
    )
    rebuilt_signature = _dataset_signature(
        "event_impact_assessor_v2",
        dataset,
    )
    signature_matches = bool(
        expected_signature and rebuilt_signature == expected_signature
    )
    windows = _event_walk_forward_date_windows(
        sorted({str(value) for value in dataset.get("all_dates") or []})
    )
    if on_progress:
        on_progress("audit", 0.80, "计算五窗事件族、市场状态与标签稳定性")
    audit = {
        **_event_distribution_label_stability_audit(dataset, windows),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generation_stage": "post_training_read_only_audit",
        "candidate_binding": {
            "model_version_id": int(model_version_id),
            "version": str(version.get("version") or ""),
            "recorded_training_data_signature": expected_signature,
            "rebuilt_training_data_signature": rebuilt_signature,
            "training_data_signature_matches": signature_matches,
        },
    }
    persisted = False
    if persist and signature_matches:
        metrics["event_distribution_label_stability_audit"] = audit
        update_model_version_metrics(int(model_version_id), metrics=metrics)
        persisted = True
    if on_progress:
        on_progress(
            "completed",
            1.0,
            "审计完成并写入候选证据" if persisted else "审计完成但未改写候选证据",
        )
    return {
        "ok": True,
        "contract": EVENT_DISTRIBUTION_LABEL_AUDIT_CONTRACT,
        "model_version_id": int(model_version_id),
        "version": str(version.get("version") or ""),
        "persist_requested": bool(persist),
        "persisted": persisted,
        "persistence_blocked_reason": (
            None
            if persisted or not persist
            else "training_data_signature_mismatch"
        ),
        "training_data_signature_matches": signature_matches,
        "status_unchanged": before_status,
        "active_unchanged": before_active,
        "validation_unchanged": before_validation,
        "audit": audit,
    }


def audit_event_model_feature_contributions(
    model_version_id: int,
    *,
    persist: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Audit frozen OOS feature dependence by event family and market state.

    This is a read-only diagnostic.  It loads each point-in-time fold artifact,
    permutes one feature at a time only inside the held-out group, and measures
    the loss change for every native event task separately.  It never retrains,
    changes a release gate, or changes model status/activation.
    """

    version = get_model_version(int(model_version_id))
    if not version:
        return {
            "ok": False,
            "error": "model_version_not_found",
            "model_version_id": int(model_version_id),
        }
    if str(version.get("model_key") or "") != "event_impact_assessor_v2":
        return {
            "ok": False,
            "error": "not_event_impact_model_version",
            "model_version_id": int(model_version_id),
        }
    before_status = str(version.get("status") or "")
    before_active = bool(version.get("is_active"))
    before_validation = dict(version.get("validation") or {})
    metrics = dict(version.get("metrics") or {})
    artifact = _load_artifact(version.get("artifact_path"))
    if not artifact:
        return {
            "ok": False,
            "error": "event_model_artifact_missing",
            "model_version_id": int(model_version_id),
        }
    if str(artifact.get("model_type") or "") != "event_impact_assessor_multihead_v4":
        return {
            "ok": False,
            "error": "event_model_artifact_contract_not_current",
            "model_version_id": int(model_version_id),
            "model_type": str(artifact.get("model_type") or ""),
        }
    fold_bindings = list(artifact.get("walk_forward_fold_artifacts") or [])
    if [int(item.get("fold") or 0) for item in fold_bindings] != [1, 2, 3, 4, 5]:
        return {
            "ok": False,
            "error": "event_model_fold_artifacts_incomplete",
            "model_version_id": int(model_version_id),
        }
    if on_progress:
        on_progress("dataset", 0.05, "重建冻结事件点时数据并核对训练指纹")
    dataset = _build_plan_training_dataset(
        "event_impact_assessor_v2",
        on_progress=on_progress,
    )
    training_context = metrics.get("targeted_training_context")
    dataset["targeted_training_context"] = (
        dict(training_context) if isinstance(training_context, dict) else {}
    )
    expected_signature = str(metrics.get("training_data_signature") or "")
    rebuilt_signature = _dataset_signature("event_impact_assessor_v2", dataset)
    signature_matches = bool(
        expected_signature and expected_signature == rebuilt_signature
    )
    if not signature_matches:
        return {
            "ok": False,
            "error": "event_training_data_signature_mismatch",
            "model_version_id": int(model_version_id),
            "recorded_training_data_signature": expected_signature,
            "rebuilt_training_data_signature": rebuilt_signature,
            "status_unchanged": before_status,
            "active_unchanged": before_active,
        }
    feature_names = [str(value) for value in artifact.get("features") or []]
    feature_indices = [PLAN_MODEL_FEATURES.index(feature) for feature in feature_names]
    all_values = np.asarray(dataset["x_all"], dtype=float)[:, feature_indices]
    all_dates = np.asarray(dataset.get("all_dates") or [], dtype=object)
    all_returns = np.asarray(dataset["y_all_abnormal_return"], dtype=float)
    all_reaction = np.asarray(dataset["y_all_event_reaction"], dtype=float)
    all_direction = np.asarray(
        dataset["y_all_event_direction_signal"], dtype=float
    )
    all_sources = [str(value) for value in dataset.get("all_event_sources") or []]
    all_families = [str(value) for value in dataset.get("all_event_types") or []]
    all_states = [str(value) for value in dataset.get("all_market_states") or []]
    fold_audits: list[dict[str, Any]] = []
    verified_fold_artifacts: list[dict[str, Any]] = []
    for index, binding in enumerate(fold_bindings, start=1):
        fold = int(binding["fold"])
        fold_artifact = _load_artifact(binding.get("artifact_path"))
        if not fold_artifact:
            raise RuntimeError(f"事件特征贡献审计缺少第{fold}窗产物")
        model_path = resolve_model_artifact_path(fold_artifact["model_path"])
        actual_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
        expected_sha256 = str(binding.get("model_sha256") or "")
        if not expected_sha256 or actual_sha256 != expected_sha256:
            raise RuntimeError(f"事件特征贡献审计第{fold}窗产物哈希不一致")
        with model_path.open("rb") as handle:
            fold_model = pickle.load(handle)
        test_start = str(binding.get("test_start") or "")
        test_end = str(binding.get("test_end") or "")
        test_mask = np.asarray(
            [test_start <= str(value) <= test_end for value in all_dates],
            dtype=bool,
        )
        if not bool(np.any(test_mask)):
            raise RuntimeError(f"事件特征贡献审计第{fold}窗没有OOS样本")
        selected_indices = np.flatnonzero(test_mask)
        if on_progress:
            on_progress(
                "permutation_audit",
                0.45 + 0.1 * index,
                f"审计第{fold}/5窗事件族和市场状态特征贡献",
            )
        fold_audits.append(fold_event_feature_permutation_audit(
            predict_with_context=(
                lambda values, sources, types, reaction, direction,
                model=fold_model: _event_impact_bundle_outputs(
                    model,
                    values,
                    event_sources=sources,
                    event_types=types,
                    event_reaction=reaction,
                    event_direction_signal=direction,
                )
            ),
            values=all_values[test_mask],
            feature_names=feature_names,
            abnormal_returns=all_returns[test_mask],
            event_reaction=all_reaction[test_mask],
            event_direction_signal=all_direction[test_mask],
            event_families=[all_families[item] for item in selected_indices],
            market_states=[all_states[item] for item in selected_indices],
            event_sources=[all_sources[item] for item in selected_indices],
            event_types=[all_families[item] for item in selected_indices],
            fold=fold,
            transaction_cost_rate=EVENT_TRANSACTION_COST_RATE,
        ))
        verified_fold_artifacts.append({
            "fold": fold,
            "test_start": test_start,
            "test_end": test_end,
            "model_sha256": actual_sha256,
            "oos_rows": int(test_mask.sum()),
        })
    audit = {
        **aggregate_event_feature_permutation_audit(fold_audits),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generation_stage": "post_training_frozen_oos_read_only_audit",
        "candidate_binding": {
            "model_version_id": int(model_version_id),
            "version": str(version.get("version") or ""),
            "recorded_training_data_signature": expected_signature,
            "rebuilt_training_data_signature": rebuilt_signature,
            "training_data_signature_matches": True,
            "fold_artifacts": verified_fold_artifacts,
        },
    }
    persisted = False
    if persist:
        metrics["event_feature_contribution_audit"] = audit
        update_model_version_metrics(int(model_version_id), metrics=metrics)
        persisted = True
    after = get_model_version(int(model_version_id)) or {}
    if on_progress:
        on_progress("completed", 1.0, "事件族与市场状态特征贡献审计完成")
    return {
        "ok": True,
        "contract": EVENT_FEATURE_CONTRIBUTION_AUDIT_CONTRACT,
        "model_version_id": int(model_version_id),
        "persisted": persisted,
        "status_unchanged": str(after.get("status") or "") == before_status,
        "active_unchanged": bool(after.get("is_active")) == before_active,
        "validation_unchanged": dict(after.get("validation") or {})
        == before_validation,
        "audit": audit,
    }


def audit_event_model_conditional_failures(
    model_version_id: int,
    *,
    persist: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Audit frozen OOS failures by event family and market state.

    The formal training-only baseline and original five fold artifacts are
    reused unchanged.  This function is diagnostic only and never retrains or
    changes release status, activation, parameters, samples, or gates.
    """

    version = get_model_version(int(model_version_id))
    if not version:
        return {
            "ok": False,
            "error": "model_version_not_found",
            "model_version_id": int(model_version_id),
        }
    if str(version.get("model_key") or "") != "event_impact_assessor_v2":
        return {
            "ok": False,
            "error": "not_event_impact_model_version",
            "model_version_id": int(model_version_id),
        }
    before_status = str(version.get("status") or "")
    before_active = bool(version.get("is_active"))
    before_validation = dict(version.get("validation") or {})
    metrics = dict(version.get("metrics") or {})
    artifact = _load_artifact(version.get("artifact_path"))
    if not artifact:
        return {
            "ok": False,
            "error": "event_model_artifact_missing",
            "model_version_id": int(model_version_id),
        }
    if str(artifact.get("model_type") or "") != "event_impact_assessor_multihead_v4":
        return {
            "ok": False,
            "error": "event_model_artifact_contract_not_current",
            "model_version_id": int(model_version_id),
        }
    fold_bindings = list(artifact.get("walk_forward_fold_artifacts") or [])
    if [int(item.get("fold") or 0) for item in fold_bindings] != [1, 2, 3, 4, 5]:
        return {
            "ok": False,
            "error": "event_model_fold_artifacts_incomplete",
            "model_version_id": int(model_version_id),
        }
    if on_progress:
        on_progress("dataset", 0.05, "重建冻结事件点时数据并核对训练指纹")
    dataset = _build_plan_training_dataset(
        "event_impact_assessor_v2",
        on_progress=on_progress,
    )
    training_context = metrics.get("targeted_training_context")
    dataset["targeted_training_context"] = (
        dict(training_context) if isinstance(training_context, dict) else {}
    )
    expected_signature = str(metrics.get("training_data_signature") or "")
    rebuilt_signature = _dataset_signature("event_impact_assessor_v2", dataset)
    if not expected_signature or expected_signature != rebuilt_signature:
        return {
            "ok": False,
            "error": "event_training_data_signature_mismatch",
            "model_version_id": int(model_version_id),
            "recorded_training_data_signature": expected_signature,
            "rebuilt_training_data_signature": rebuilt_signature,
            "status_unchanged": before_status,
            "active_unchanged": before_active,
        }
    feature_names = [str(value) for value in artifact.get("features") or []]
    feature_indices = [PLAN_MODEL_FEATURES.index(feature) for feature in feature_names]
    all_values = np.asarray(dataset["x_all"], dtype=float)[:, feature_indices]
    all_dates = np.asarray(dataset.get("all_dates") or [], dtype=object)
    all_returns = np.asarray(dataset["y_all_abnormal_return"], dtype=float)
    all_reaction = np.asarray(dataset["y_all_event_reaction"], dtype=float)
    all_direction = np.asarray(
        dataset["y_all_event_direction_signal"], dtype=float,
    )
    all_sources = [str(value) for value in dataset.get("all_event_sources") or []]
    all_families = [str(value) for value in dataset.get("all_event_types") or []]
    all_states = [str(value) for value in dataset.get("all_market_states") or []]
    fold_evidence_by_fold = {
        int(item.get("fold") or 0): dict(item)
        for item in metrics.get("walk_forward_evidence") or []
    }
    fold_audits: list[dict[str, Any]] = []
    verified_fold_artifacts: list[dict[str, Any]] = []
    for index, binding in enumerate(fold_bindings, start=1):
        fold = int(binding["fold"])
        fold_artifact = _load_artifact(binding.get("artifact_path"))
        if not fold_artifact:
            raise RuntimeError(f"事件条件失败审计缺少第{fold}窗产物")
        model_path = resolve_model_artifact_path(fold_artifact["model_path"])
        actual_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
        expected_sha256 = str(binding.get("model_sha256") or "")
        if not expected_sha256 or actual_sha256 != expected_sha256:
            raise RuntimeError(f"事件条件失败审计第{fold}窗产物哈希不一致")
        with model_path.open("rb") as handle:
            fold_model = pickle.load(handle)
        train_start = str(binding.get("train_start") or "")
        train_end = str(binding.get("train_end") or "")
        test_start = str(binding.get("test_start") or "")
        test_end = str(binding.get("test_end") or "")
        train_mask = np.asarray([
            train_start <= str(value) <= train_end for value in all_dates
        ], dtype=bool)
        test_mask = np.asarray([
            test_start <= str(value) <= test_end for value in all_dates
        ], dtype=bool)
        if not bool(np.any(train_mask)) or not bool(np.any(test_mask)):
            raise RuntimeError(f"事件条件失败审计第{fold}窗缺少训练或OOS样本")
        train_indices = np.flatnonzero(train_mask)
        test_indices = np.flatnonzero(test_mask)
        if on_progress:
            on_progress(
                "conditional_failure_audit",
                0.45 + 0.1 * index,
                f"审计第{fold}/5窗事件族和市场状态条件失效",
            )
        outputs = _event_impact_bundle_outputs(
            fold_model,
            all_values[test_mask],
            event_sources=[all_sources[item] for item in test_indices],
            event_types=[all_families[item] for item in test_indices],
            event_reaction=all_reaction[test_mask],
            event_direction_signal=all_direction[test_mask],
        )
        formal_evaluation = (
            fold_evidence_by_fold.get(fold, {}).get("task_native_evaluation")
            or {}
        )
        fold_audits.append(fold_event_conditional_failure_audit(
            outputs=outputs,
            actual_abnormal_return=all_returns[test_mask],
            train_abnormal_return=all_returns[train_mask],
            event_reaction=all_reaction[test_mask],
            train_event_reaction=all_reaction[train_mask],
            event_direction_signal=all_direction[test_mask],
            train_event_direction_signal=all_direction[train_mask],
            event_sources=[all_sources[item] for item in test_indices],
            train_event_sources=[all_sources[item] for item in train_indices],
            event_families=[all_families[item] for item in test_indices],
            train_event_families=[all_families[item] for item in train_indices],
            market_states=[all_states[item] for item in test_indices],
            fold=fold,
            transaction_cost_rate=EVENT_TRANSACTION_COST_RATE,
            target_contract=EVENT_TARGET_CONTRACT,
            formal_evaluation=dict(formal_evaluation),
        ))
        verified_fold_artifacts.append({
            "fold": fold,
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "model_sha256": actual_sha256,
            "train_rows": int(train_mask.sum()),
            "oos_rows": int(test_mask.sum()),
        })
    audit = {
        **aggregate_event_conditional_failure_audit(fold_audits),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generation_stage": "post_training_frozen_oos_read_only_audit",
        "candidate_binding": {
            "model_version_id": int(model_version_id),
            "version": str(version.get("version") or ""),
            "recorded_training_data_signature": expected_signature,
            "rebuilt_training_data_signature": rebuilt_signature,
            "training_data_signature_matches": True,
            "fold_artifacts": verified_fold_artifacts,
        },
    }
    persisted = False
    if persist:
        metrics["event_conditional_failure_audit"] = audit
        update_model_version_metrics(int(model_version_id), metrics=metrics)
        persisted = True
    after = get_model_version(int(model_version_id)) or {}
    if on_progress:
        on_progress("completed", 1.0, "事件族与市场状态条件失败审计完成")
    return {
        "ok": True,
        "contract": EVENT_CONDITIONAL_FAILURE_AUDIT_CONTRACT,
        "model_version_id": int(model_version_id),
        "persisted": persisted,
        "status_unchanged": str(after.get("status") or "") == before_status,
        "active_unchanged": bool(after.get("is_active")) == before_active,
        "validation_unchanged": dict(after.get("validation") or {}) == before_validation,
        "audit": audit,
    }


def audit_narrative_strong_baseline_contract(
    model_version_id: int,
    *,
    persist: bool = False,
) -> dict[str, Any]:
    """Reapply the strongest deployable lifecycle baseline to frozen OOS evidence."""
    version = get_model_version(int(model_version_id))
    if not version or str(version.get("model_key") or "") != (
        "narrative_transition_multitask_v1"
    ):
        return {
            "ok": False,
            "error": "not_narrative_model_version",
            "model_version_id": int(model_version_id),
        }
    metrics = dict(version.get("metrics") or {})
    evidence = [
        dict(item)
        for item in metrics.get("walk_forward_evidence") or []
        if isinstance(item, dict)
    ]
    if len(evidence) != 5:
        return {
            "ok": False,
            "error": "incomplete_narrative_five_window_evidence",
            "model_version_id": int(model_version_id),
        }
    fold_results: list[dict[str, Any]] = []
    passed_windows = 0
    baseline_windows = 0
    for item in evidence:
        task = dict(item.get("task_native_evaluation") or {})
        model = task.get("model") if isinstance(task.get("model"), dict) else {}
        baselines = task.get("baselines") if isinstance(
            task.get("baselines"), dict
        ) else {}
        persistence = baselines.get("state_persistence") if isinstance(
            baselines.get("state_persistence"), dict
        ) else {}
        if "macro_f1" not in model or "macro_f1" not in persistence:
            return {
                "ok": False,
                "error": "narrative_strong_baseline_metrics_missing",
                "model_version_id": int(model_version_id),
                "fold": int(item.get("fold") or 0),
            }
        gates = dict(task.get("gates") or {})
        gates["final_state_macro_f1_beats_state_persistence"] = (
            float(model["macro_f1"]) > float(persistence["macro_f1"])
        )
        task["contract"] = NARRATIVE_TASK_EVALUATION_CONTRACT
        task["gates"] = gates
        task["passed"] = all(bool(value) for value in gates.values())
        task["baseline_outperformed"] = bool(
            gates.get("transition_brier_beats_train_only_hazard")
            and gates.get("conditional_destination_macro_f1_beats_empirical")
            and gates.get("final_state_macro_f1_beats_state_persistence")
            and gates.get("final_state_macro_f1_beats_empirical_transition")
            and gates.get("final_state_log_loss_beats_empirical_transition")
            and gates.get("final_state_brier_beats_empirical_transition")
        )
        item["task_native_evaluation"] = task
        item["task_native_passed"] = bool(
            task["passed"] and task["baseline_outperformed"]
        )
        passed_windows += int(bool(task["passed"]))
        baseline_windows += int(bool(task["baseline_outperformed"]))
        fold_results.append({
            "fold": int(item.get("fold") or 0),
            "model_macro_f1": float(model["macro_f1"]),
            "state_persistence_macro_f1": float(persistence["macro_f1"]),
            "macro_f1_delta": round(
                float(model["macro_f1"]) - float(persistence["macro_f1"]),
                8,
            ),
            "passed": bool(task["passed"]),
            "baseline_outperformed": bool(task["baseline_outperformed"]),
        })
    minimum_windows = 3
    formal_passed = passed_windows >= minimum_windows
    baseline_passed = baseline_windows >= minimum_windows
    audit = {
        "contract": NARRATIVE_TASK_EVALUATION_CONTRACT,
        "status": (
            "passed" if formal_passed and baseline_passed else "failed"
        ),
        "model_version_id": int(model_version_id),
        "window_count": len(evidence),
        "minimum_passed_windows": minimum_windows,
        "passed_windows": passed_windows,
        "baseline_outperformed_windows": baseline_windows,
        "folds": fold_results,
        "parameter_changes": False,
        "label_changes": False,
        "retraining_performed": False,
        "production_change_forbidden": True,
        "audited_at": datetime.now().astimezone().isoformat(),
        "reason": (
            "生命周期最终四状态输出必须跑赢可直接部署的状态持久性基线；"
            "转移检测通过不能替代最终状态识别。"
        ),
    }
    if persist:
        metrics["walk_forward_evidence"] = evidence
        metrics["task_metrics"] = evidence[-1]["task_native_evaluation"]
        metrics["task_metric_final_window_passed"] = bool(
            evidence[-1]["task_native_passed"]
        )
        metrics["task_metric_passed"] = formal_passed and baseline_passed
        metrics["task_native_oos"] = {
            **dict(metrics.get("task_native_oos") or {}),
            "minimum_passed_windows": minimum_windows,
            "passed_windows": passed_windows,
            "baseline_outperformed_windows": baseline_windows,
            "passed": formal_passed,
            "baseline_outperformed": baseline_passed,
        }
        metrics["task_baseline"] = {
            **dict(metrics.get("task_baseline") or {}),
            "contract": (
                "strongest_of_state_persistence_and_train_only_transition_"
                "hazard_and_conditional_destination_v1"
            ),
            "comparison_executed": True,
            "outperformed": baseline_passed,
            "walk_forward_windows_outperformed": baseline_windows,
        }
        metrics["narrative_strong_baseline_reaudit"] = audit
        update_model_version_metrics(int(model_version_id), metrics=metrics)
        validation = dict(version.get("validation") or {})
        eligibility = dict(validation.get("production_eligibility") or {})
        gates = dict(eligibility.get("gates") or {})
        gates["task_metric_passed"] = formal_passed and baseline_passed
        gates["task_native_walk_forward_oos"] = formal_passed
        gates["task_native_baseline_outperformed"] = baseline_passed
        eligibility["gates"] = gates
        eligibility["eligible"] = False
        eligibility["blocked_reasons"] = [
            key
            for key, passed in gates.items()
            if key not in {
                "human_review",
                "live_observation_20_sessions",
                "simulation_only_live_observation_waived",
            }
            and not bool(passed)
        ]
        validation["status"] = "failed"
        validation["production_eligible"] = False
        validation["production_eligibility"] = eligibility
        update_model_version_validation(int(model_version_id), validation=validation)
    return {
        "ok": True,
        "persisted": bool(persist),
        "status_unchanged": str(version.get("status") or "") == str(
            (get_model_version(int(model_version_id)) or {}).get("status") or ""
        ),
        "active_unchanged": bool(version.get("is_active")) == bool(
            (get_model_version(int(model_version_id)) or {}).get("is_active")
        ),
        "audit": audit,
    }


def _is_duplicate_training_dataset(model_key: str, dataset: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str]:
    signature = _dataset_signature(model_key, dataset)
    latest = next((item for item in list_model_versions(model_key, limit=5) if str(item.get("status") or "") != "rejected"), None)
    metrics = (latest or {}).get("metrics") or {}
    if metrics.get("training_data_signature") == signature:
        return True, latest, signature
    return False, latest, signature


def _read_base_coverage_snapshot() -> dict[str, int]:
    from app.data_platform.db import _conn as data_conn

    def count_distinct(c: Any, table: str, candidates: list[str]) -> int:
        try:
            rows = c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=?",
                (table,),
            ).fetchall()
            columns = {str(dict(row).get("column_name") or "") for row in rows}
        except Exception:
            columns = set(candidates)
        for column in candidates:
            if columns and column not in columns:
                continue
            try:
                row = c.execute(f"SELECT COUNT(DISTINCT {column}) count FROM {table}").fetchone()
                return int(dict(row).get("count") or 0)
            except Exception:
                return 0
        return 0

    # Parse request-ledger metadata after the SQL block; initialize the
    # accumulator before the event summary is assembled.
    news_dates: set[str] = set()
    compact_news_dates: set[str] | None = None
    with data_conn() as c:
        try:
            trade_dates = int(dict(c.execute("SELECT COUNT(*) count FROM dc_available_trade_dates").fetchone()).get("count") or 0)
        except Exception:
            trade_dates = 0
        if trade_dates <= 0:
            row = c.execute("SELECT COUNT(*) count FROM (SELECT 1 FROM dc_stock_daily_snapshot GROUP BY trade_date LIMIT 5000)").fetchone()
            trade_dates = int(dict(row).get("count") or 0)
        themes = count_distinct(c, "dc_theme_snapshot", ["snapshot_date", "as_of_date", "available_time"])
        try:
            announcement_date_rows = c.execute(
                """SELECT DISTINCT REPLACE(SUBSTRING(r.available_time,1,10),'-','') event_date
                   FROM dc_announcement_event e
                   JOIN dc_announcement_raw r ON r.id=e.announcement_id
                   WHERE r.ts_code<>'' AND r.available_time<>''"""
            ).fetchall()
        except Exception:
            announcement_date_rows = []
        announcement_dates = {
            str(dict(row).get("event_date") or "")
            for row in announcement_date_rows
            if dict(row).get("event_date")
        }
        announcement_events = len(announcement_dates)
        try:
            # Coverage reporting must use the request ledger, not rescan the
            # 67M-row raw payload table.  The ledger carries the same
            # point-in-time request date and row-count evidence without
            # transferring article bodies into the API process.
            if getattr(c, "_is_postgres", False):
                # The coverage ledger is compact evidence, but returning and
                # JSON-decoding every request row made the status page spend
                # several seconds in Python. Extract only the covered event
                # dates in PostgreSQL and keep the old path for fixtures.
                compact_news_dates = {
                    str(dict(row).get("event_date") or "")
                    for row in c.execute(
                        """SELECT DISTINCT
                                  REPLACE(
                                      SUBSTRING(
                                          COALESCE(
                                              NULLIF(params_json::jsonb->>'start_date',''),
                                              params_json::jsonb->>'end_date'
                                          ),
                                          1,
                                          10
                                      ),
                                      '-',
                                      ''
                                  ) AS event_date
                           FROM dc_tushare_request_coverage
                          WHERE api_name='major_news'
                            AND rows_read>0
                            AND result_status IN ('success','valid_empty')"""
                    ).fetchall()
                    if dict(row).get("event_date")
                }
                coverage_news_rows = []
            else:
                coverage_news_rows = c.execute(
                    """SELECT params_json,rows_read,result_status
                       FROM dc_tushare_request_coverage
                       WHERE api_name='major_news'
                         AND result_status IN ('success','valid_empty')"""
                ).fetchall()
        except Exception:
            coverage_news_rows = []
        try:
            top_list_rows = c.execute(
                """SELECT DISTINCT trade_date
                   FROM dc_tushare_raw_records
                   WHERE api_name='top_list'
                     AND trade_date<>''
                     AND available_time<>''"""
            ).fetchall()
        except Exception:
            top_list_rows = []
        top_list_dates = {
            str(dict(row).get("trade_date") or "")
            for row in top_list_rows
            if dict(row).get("trade_date")
        }
        try:
            capital = int(dict(c.execute(
                """SELECT COUNT(*) count
                   FROM dc_available_trade_dates dates
                   WHERE EXISTS (
                       SELECT 1
                       FROM dc_tushare_raw_records raw
                       WHERE raw.api_name='moneyflow'
                         AND raw.trade_date=dates.trade_date
                         AND raw.available_time<>''
                   )"""
            ).fetchone()).get("count") or 0)
        except Exception:
            capital = 0
        try:
            narrative_v3 = int(dict(c.execute(
                """SELECT COUNT(DISTINCT as_of_date) count
                   FROM dc_theme_snapshot
                   WHERE model_version=?""",
                ("industry_price_fundamental_theme_v3",),
            ).fetchone()).get("count") or 0)
        except Exception:
            narrative_v3 = 0
    # Parse only compact request metadata after releasing the data-platform
    # connection.  This is coverage evidence, not the event-feature path;
    # article/company matching remains in the point-in-time feature pipeline.
    if compact_news_dates is not None:
        news_dates.update(compact_news_dates)
    elif coverage_news_rows:
        for row in coverage_news_rows:
            item = dict(row)
            if int(item.get("rows_read") or 0) <= 0:
                continue
            payload = _payload_dict(item.get("params_json"))
            event_time = str(payload.get("start_date") or payload.get("end_date") or "")
            event_date = event_time[:10].replace("-", "")
            if event_date:
                news_dates.add(event_date)
    news_events = len(news_dates)
    events = len(announcement_dates | news_dates)
    return {
        "trade_dates": trade_dates,
        "themes": themes,
        "narrative_v3": narrative_v3,
        "events": events,
        "announcement_events": announcement_events,
        "major_news_events": news_events,
        "top_list_events": len(top_list_dates),
        "capital": max(0, capital - CAPITAL_FLOW_HISTORY_SESSIONS - 10),
        "capital_raw": capital,
    }


def _base_coverage_snapshot(*, cache_seconds: int = 30) -> dict[str, int]:
    """Reuse the compact cross-table coverage evidence during status reads."""
    now = time.monotonic()
    with _base_coverage_cache_lock:
        cached = _base_coverage_cache.get("value")
        if isinstance(cached, dict) and now - float(_base_coverage_cache.get("at") or 0) < max(1, cache_seconds):
            return cached
        value = _read_base_coverage_snapshot()
        _base_coverage_cache.update({"at": time.monotonic(), "value": value})
        return value


def _coverage_snapshot(model_key: str, base: dict[str, int] | None = None) -> dict[str, Any]:
    base = base or _base_coverage_snapshot()
    trade_dates = int(base.get("trade_dates") or 0)
    themes = int(base.get("themes") or 0)
    events = int(base.get("events") or 0)
    capital = int(base.get("capital") or 0)
    contract = PLAN_MODELS[model_key]
    narrative_v3_raw_sessions = int(base.get("narrative_v3") or 0)
    narrative_v3_labeled_sessions = max(
        0,
        narrative_v3_raw_sessions - _plan_model_horizon(
            "narrative_transition_multitask_v1"
        ),
    )
    sessions = (
        narrative_v3_labeled_sessions
        if "narrative" in model_key
        else trade_dates
    )
    if model_key == "event_impact_assessor_v2":
        sessions = min(trade_dates, events)
    if model_key == "capital_persistence_multitask_v1":
        sessions = min(trade_dates, capital)
    formal_required = _required_training_sessions(model_key)
    domain_minimum = int(contract["minimum_sessions"])
    formal_history_ready = sessions >= formal_required
    return {
        "trade_sessions": trade_dates,
        "theme_snapshot_sessions": themes,
        "narrative_v3_sessions": narrative_v3_raw_sessions,
        "narrative_v3_labeled_sessions": narrative_v3_labeled_sessions,
        "narrative_v3_label_horizon_sessions": _plan_model_horizon(
            "narrative_transition_multitask_v1"
        ),
        "event_point_in_time_sessions": events,
        "event_rows": events,
        "announcement_event_sessions": int(base.get("announcement_events") or 0),
        "major_news_event_sessions": int(base.get("major_news_events") or 0),
        "top_list_event_sessions": int(base.get("top_list_events") or 0),
        "event_source_sessions": {
            "announcement": int(base.get("announcement_events") or 0),
            "major_news": int(base.get("major_news_events") or 0),
            "top_list": int(base.get("top_list_events") or 0),
        },
        "capital_point_in_time_sessions": capital,
        "capital_coverage_slices": capital,
        "capital_raw_sessions": int(base.get("capital_raw") or capital),
        "effective_sessions": sessions,
        "minimum_sessions": domain_minimum,
        "formal_walk_forward_sessions": formal_required,
        "domain_minimum_ready": sessions >= domain_minimum,
        "formal_history_ready": formal_history_ready,
        "data_ready": sessions >= domain_minimum and formal_history_ready,
    }


def _latest_run_for_model(model_key: str, runs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    for run in (runs if runs is not None else list_model_train_runs(limit=100)):
        metrics = run.get("metrics") or {}
        if str(metrics.get("model_key") or "") == model_key:
            return run
    return None


def classify_plan_model_training_failure(error: Any) -> dict[str, Any]:
    raw = str(error or "").strip()
    message = raw.lower()
    if any(value in message for value in (
        "冻结策略包缺少因子绑定",
        "未知模型绑定",
        "incomplete_frozen_factor_bindings",
    )):
        category = "historical_contract_incompatible"
        retryable = False
        action = "quarantine_historical_contract"
    elif any(value in message for value in ("statement timeout", "query timeout", "canceling statement due to statement timeout")):
        category = "transient_query_timeout"
        retryable = True
        action = "bounded_retry"
    elif any(value in message for value in (
        "connection refused", "connection reset", "connection aborted",
        "server closed the connection", "could not connect", "network is unreachable",
        "temporary failure", "temporarily unavailable", "ssl eof", "remote end closed",
    )):
        category = "transient_external_dependency"
        retryable = True
        action = "bounded_retry"
    elif any(value in message for value in (
        "capital_moneyflow_query_failed",
        "moneyflow_query_failed",
        "training_symbol_anchor_query_failed",
        "theme_mapping_query_failed",
        "训练样本不足：0 rows",
        "训练样本不足: 0 rows",
    )):
        category = "dataset_construction_failure"
        retryable = True
        action = "bounded_retry"
    elif any(value in message for value in (
        "训练数据不足",
        "训练日历不足",
        "可标注训练交易日不足",
        "data coverage",
        "coverage gap",
    )):
        category = "data_coverage_gap"
        retryable = False
        action = "delegate_data_backfill"
    elif "影子重放" in raw and any(value in raw for value in (
        "缺少相邻两个权益点",
        "缺少当日权益点",
        "权益点无效",
    )):
        # A future Shadow observation needs two persisted equity points.  This
        # is evidence not yet formed, not a training/runtime defect and must
        # never be "repaired" by synthesising a historical return.
        category = "shadow_equity_evidence_gap"
        retryable = False
        action = "wait_for_next_natural_shadow_observation"
    elif any(value in message for value in (
        "nameerror",
        "not defined",
        "unboundlocalerror",
        "attributeerror",
        "typeerror",
        "syntaxerror",
    )):
        category = "code_contract_error"
        retryable = False
        action = "escalate_owner_review"
    elif any(value in message for value in (
        "stale_plan_model_training_run",
        "plan_model_training_runtime_budget_exceeded",
        "训练心跳超时",
        "训练超过单次总时限",
    )):
        category = "interrupted_runtime"
        retryable = True
        action = "bounded_retry"
    elif any(value in message for value in ("memoryerror", "out of memory", "cannot allocate memory")):
        category = "resource_exhaustion"
        retryable = True
        action = "bounded_retry"
    else:
        category = "unknown_training_failure"
        retryable = False
        action = "escalate_owner_review"
    return {
        "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
        "category": category,
        "retryable": retryable,
        "recommended_action": action,
        "error_excerpt": raw[:500],
    }


def _update_training_failure_recovery(
    run_id: int,
    patch: dict[str, Any],
    *,
    timeline_message: str | None = None,
) -> dict[str, Any]:
    run = get_model_train_run(int(run_id)) or {}
    metrics = dict(run.get("metrics") or {})
    recovery = dict(metrics.get("failure_recovery") or {})
    recovery.update(patch)
    recovery.setdefault("contract", TRAINING_FAILURE_RECOVERY_CONTRACT)
    metrics["failure_recovery"] = recovery
    if timeline_message:
        timeline = list(metrics.get("timeline") or [])
        timeline.append({
            "at": datetime.now().astimezone().isoformat(),
            "phase": "failure_recovery",
            "progress": 1.0,
            "message": str(timeline_message),
        })
        metrics["timeline"] = timeline[-20:]
    update_model_train_run(int(run_id), metrics=metrics)
    _operations_cache.update({"at": 0.0, "value": None})
    _status_cache.update({"at": 0.0, "value": None})
    return recovery


def _candidate_failure_takeover(model_key: str) -> dict[str, Any] | None:
    candidate = _plan_model_candidate_governance_blocker(model_key)
    if not candidate:
        return None
    validation = candidate.get("validation") if isinstance(
        candidate.get("validation"), dict
    ) else {}
    shadow = validation.get("model_shadow") if isinstance(
        validation.get("model_shadow"), dict
    ) else {}
    return {
        "model_version_id": int(candidate.get("id") or 0),
        "version": str(candidate.get("version") or ""),
        "validation_status": str(validation.get("status") or ""),
        "shadow_status": str(shadow.get("status") or ""),
        "observed_return_sessions": int(shadow.get("observed_return_sessions") or 0),
        "required_return_sessions": int(shadow.get("required_return_sessions") or 20),
    }


def _enqueue_plan_model_training_recovery(
    *,
    model_key: str,
    failed_run_id: int,
    root_run_id: int,
    retry_count: int,
    classification: dict[str, Any],
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import create_evolution_task

        recovery = {
            "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
            "model_key": model_key,
            "failed_run_id": int(failed_run_id),
            "root_run_id": int(root_run_id),
            "retry_count": max(0, int(retry_count)),
            "max_retries": TRAINING_FAILURE_MAX_RETRIES,
            "classification": dict(classification),
            "queued_at": datetime.now().astimezone().isoformat(),
        }
        task_kwargs = {
            "task_type": "plan_model_autonomy",
            "payload": {
                "trigger": "automatic_training_failure_recovery",
                "training_failure_recovery": recovery,
            },
            "dedupe_key": (
                f"plan_model_training_recovery:v1:{int(root_run_id)}:"
                f"{int(failed_run_id)}:{max(0, int(retry_count))}"
            ),
            "max_attempts": 3,
            "priority": 32,
        }
        task = create_evolution_task(**task_kwargs)
        if str(task.get("status") or "") in {"completed", "failed", "cancelled"}:
            recovery["previous_recovery_task_id"] = int(task.get("id") or 0) or None
            recovery["requeued_after_terminal_coordinator"] = True
            task_kwargs["payload"] = {
                "trigger": "automatic_training_failure_recovery",
                "training_failure_recovery": recovery,
            }
            task = create_evolution_task(**task_kwargs, force=True)
        status = str(task.get("status") or "queued")
        _update_training_failure_recovery(
            int(failed_run_id),
            {
                **recovery,
                "status": "queued_for_automatic_recovery",
                "recovery_task_id": int(task.get("id") or 0),
                "recovery_task_status": status,
            },
            timeline_message="训练故障已交给模型自治任务自动处理",
        )
        return {
            "ok": True,
            "status": status,
            "task_id": int(task.get("id") or 0),
            "recovery": recovery,
        }
    except Exception as exc:
        _update_training_failure_recovery(
            int(failed_run_id),
            {
                "status": "recovery_enqueue_failed",
                "enqueue_error": str(exc)[:300],
                "updated_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message="训练故障自动恢复任务创建失败，保留异常等待下一自治周期重建",
        )
        return {"ok": False, "status": "enqueue_failed", "error": str(exc)[:300]}


def process_plan_model_training_failure(
    recovery_request: dict[str, Any],
) -> dict[str, Any]:
    request = dict(recovery_request or {})
    failed_run_id = int(request.get("failed_run_id") or 0)
    run = get_model_train_run(failed_run_id) if failed_run_id > 0 else None
    if not run:
        return {"ok": False, "status": "failed_run_missing", "failed_run_id": failed_run_id}
    metrics = dict(run.get("metrics") or {})
    model_key = str(request.get("model_key") or metrics.get("model_key") or "")
    if model_key not in PLAN_MODELS:
        return {"ok": False, "status": "unknown_plan_model", "model_key": model_key}
    root_run_id = int(request.get("root_run_id") or failed_run_id)
    retry_count = max(0, int(request.get("retry_count") or 0))
    max_retries = max(0, int(request.get("max_retries") or TRAINING_FAILURE_MAX_RETRIES))
    classification = request.get("classification") if isinstance(
        request.get("classification"), dict
    ) else classify_plan_model_training_failure(run.get("error"))
    takeover = _candidate_failure_takeover(model_key)
    if takeover:
        recovery = _update_training_failure_recovery(
            failed_run_id,
            {
                "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                "status": "resolved_by_existing_candidate_observation",
                "classification": classification,
                "root_run_id": root_run_id,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "candidate_takeover": takeover,
                "resolved_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message=(
                f"已有候选版本 #{takeover['model_version_id']} 进入未来观察，"
                "本次训练故障不再重训"
            ),
        )
        return {
            "ok": True,
            "status": recovery["status"],
            "model_key": model_key,
            "failed_run_id": failed_run_id,
            "candidate_takeover": takeover,
            "started": [],
        }
    category = str(classification.get("category") or "unknown_training_failure")
    if category == "data_coverage_gap":
        try:
            from app.data_platform.services.backfill import ensure_plan_model_data_backfill

            work_order = ensure_plan_model_data_backfill()
            status = "delegated_to_data_backfill"
        except Exception as exc:
            work_order = {"ok": False, "error": str(exc)[:300]}
            status = "data_backfill_delegation_failed"
        _update_training_failure_recovery(
            failed_run_id,
            {
                "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                "status": status,
                "classification": classification,
                "root_run_id": root_run_id,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "data_backfill": work_order,
                "updated_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message="训练覆盖缺口已委托数据补全工单，数据就绪后再训练",
        )
        return {
            "ok": bool(work_order.get("ok", True)),
            "status": status,
            "model_key": model_key,
            "failed_run_id": failed_run_id,
            "data_backfill": work_order,
            "started": [],
        }
    retryable = bool(classification.get("retryable"))
    if retryable and retry_count < max_retries:
        next_retry_count = retry_count + 1
        result = request_plan_model_training(
            model_key,
            training_context={
                "trigger": "automatic_training_failure_recovery",
                "trigger_reason": f"训练故障自动恢复：{category}",
                "requested_by": "plan_model_failure_coordinator",
                "failure_recovery": {
                    "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                    "root_run_id": root_run_id,
                    "previous_run_id": failed_run_id,
                    "retry_count": next_retry_count,
                    "max_retries": max_retries,
                    "classification": classification,
                },
            },
        )
        if result.get("started"):
            status = "automatic_retry_started"
            timeline_message = f"训练故障已启动第 {next_retry_count}/{max_retries} 次自动重试"
        elif result.get("reason") == "candidate_governance_in_progress":
            status = "resolved_by_existing_candidate_observation"
            timeline_message = "候选治理已在进行，自动重试已抑制"
        else:
            status = "automatic_retry_not_started"
            timeline_message = "训练故障自动重试未启动，等待下一自治周期复核"
        _update_training_failure_recovery(
            failed_run_id,
            {
                "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                "status": status,
                "classification": classification,
                "root_run_id": root_run_id,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "retry_run_id": int(result.get("run_id") or 0) or None,
                "retry_result": result,
                "updated_at": datetime.now().astimezone().isoformat(),
            },
            timeline_message=timeline_message,
        )
        return {
            "ok": True,
            "status": status,
            "model_key": model_key,
            "failed_run_id": failed_run_id,
            "started": [result] if result.get("started") else [],
            "result": result,
        }
    status = (
        "automatic_retry_exhausted"
        if retryable
        else "escalated_owner_review"
    )
    _update_training_failure_recovery(
        failed_run_id,
        {
            "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
            "status": status,
            "classification": classification,
            "root_run_id": root_run_id,
            "retry_count": retry_count,
            "max_retries": max_retries,
            "owner_intervention_required": True,
            "resolved_at": datetime.now().astimezone().isoformat(),
        },
        timeline_message=(
            "训练故障自动重试已耗尽，已升级为所有者异常"
            if retryable
            else "训练故障属于代码或未知合同异常，已停止重复重试并升级"
        ),
    )
    return {
        "ok": False,
        "status": status,
        "model_key": model_key,
        "failed_run_id": failed_run_id,
        "owner_intervention_required": True,
        "started": [],
    }


def reconcile_unhandled_plan_model_training_failures() -> dict[str, Any]:
    latest_by_model: dict[str, dict[str, Any]] = {}
    for run in list_model_train_runs(limit=200):
        metrics = run.get("metrics") or {}
        model_key = str(metrics.get("model_key") or "")
        if model_key in PLAN_MODELS and model_key not in latest_by_model:
            latest_by_model[model_key] = run
    queued: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    terminal_statuses = {
        "resolved_by_existing_candidate_observation",
        "delegated_to_data_backfill",
        "automatic_retry_started",
        "automatic_retry_exhausted",
        "escalated_owner_review",
    }
    for model_key, run in latest_by_model.items():
        if str(run.get("status") or "") != "failed":
            continue
        metrics = dict(run.get("metrics") or {})
        existing = metrics.get("failure_recovery") if isinstance(
            metrics.get("failure_recovery"), dict
        ) else {}
        if str(existing.get("status") or "") in terminal_statuses or str(
            existing.get("status") or ""
        ) == "queued_for_automatic_recovery":
            continue
        takeover = _candidate_failure_takeover(model_key)
        if takeover:
            _update_training_failure_recovery(
                int(run["id"]),
                {
                    "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
                    "status": "resolved_by_existing_candidate_observation",
                    "classification": classify_plan_model_training_failure(run.get("error")),
                    "root_run_id": int(run["id"]),
                    "retry_count": 0,
                    "max_retries": TRAINING_FAILURE_MAX_RETRIES,
                    "candidate_takeover": takeover,
                    "resolved_at": datetime.now().astimezone().isoformat(),
                },
                timeline_message=(
                    f"已有候选版本 #{takeover['model_version_id']} 自治观察中，"
                    "历史训练故障已关闭"
                ),
            )
            resolved.append({
                "model_key": model_key,
                "failed_run_id": int(run["id"]),
                "candidate_takeover": takeover,
            })
            continue
        context = metrics.get("targeted_training_context") if isinstance(
            metrics.get("targeted_training_context"), dict
        ) else {}
        prior_recovery = context.get("failure_recovery") if isinstance(
            context.get("failure_recovery"), dict
        ) else {}
        root_run_id = int(prior_recovery.get("root_run_id") or run["id"])
        retry_count = int(prior_recovery.get("retry_count") or 0)
        classification = classify_plan_model_training_failure(run.get("error"))
        queued.append(_enqueue_plan_model_training_recovery(
            model_key=model_key,
            failed_run_id=int(run["id"]),
            root_run_id=root_run_id,
            retry_count=retry_count,
            classification=classification,
        ))
    return {
        "ok": True,
        "schema_version": TRAINING_FAILURE_RECOVERY_CONTRACT,
        "queued": queued,
        "resolved": resolved,
        "managed_model_keys": sorted({
            str((item.get("recovery") or {}).get("model_key") or "")
            for item in queued
            if str((item.get("recovery") or {}).get("model_key") or "")
        }),
    }


def _metrics_satisfy_current_training_protocol(metrics: dict[str, Any]) -> bool:
    model_key = str(metrics.get("model_key") or "")
    protocol = _training_protocol_for_model(model_key)
    dataset = metrics.get("dataset") or {}
    labeled_sessions = int(
        metrics.get("labeled_trade_date_count")
        or dataset.get("trade_date_count")
        or 0
    )
    event_contract_current = bool(
        model_key != "event_impact_assessor_v2"
        or (
            str(metrics.get("event_impact_contract") or "")
            == EVENT_IMPACT_CONTRACT
            and str(((metrics.get("event_feature_audit") or {}).get("contract") or ""))
            == EVENT_FEATURE_CONTRACT
        )
    )
    narrative_contract_current = bool(
        model_key != "narrative_transition_multitask_v1"
        or str(((metrics.get("task_metrics") or {}).get("contract") or ""))
        == NARRATIVE_TASK_EVALUATION_CONTRACT
    )
    return bool(
        str(metrics.get("model_evaluation_contract") or "") == MODEL_EVALUATION_CONTRACT
        and event_contract_current
        and narrative_contract_current
        and isinstance(metrics.get("task_native_oos"), dict)
        and int((metrics.get("task_native_oos") or {}).get("window_count") or 0)
        >= int(protocol["walk_forward_windows"])
        and str(metrics.get("evaluation_stage") or "") == "formal_five_window"
        and labeled_sessions >= _required_training_sessions(model_key)
        and int(metrics.get("training_trade_date_count") or dataset.get("train_trade_date_count") or 0)
        >= int(protocol["minimum_train_sessions"])
        and int(metrics.get("validation_trade_date_count") or dataset.get("validation_trade_date_count") or 0)
        >= int(protocol["validation_sessions"])
        and int(metrics.get("test_trade_date_count") or dataset.get("test_trade_date_count") or 0)
        >= int(protocol["test_sessions"])
    )


def _with_train_timeline(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    metrics = dict(run.get("metrics") or {})
    if not isinstance(metrics.get("timeline"), list) or not metrics.get("timeline"):
        metrics["timeline"] = [{
            "at": run.get("updated_at") or run.get("created_at"),
            "phase": run.get("phase") or run.get("status") or "queued",
            "progress": run.get("progress") or 0,
            "message": run.get("message") or "训练任务已创建，等待阶段更新",
        }]
    dataset = metrics.get("dataset") or {}
    labeled_sessions = int(
        metrics.get("labeled_trade_date_count")
        or dataset.get("trade_date_count")
        or 0
    )
    current_protocol = _metrics_satisfy_current_training_protocol(metrics)
    timeline = metrics.get("timeline") or []
    current_protocol_attempt_failed = bool(
        str(run.get("status") or "") == "failed"
        and any(
            str(item.get("phase") or "") == "dataset"
            and "日历覆盖" in str(item.get("message") or "")
            for item in timeline
            if isinstance(item, dict)
        )
    )
    model_key = str(metrics.get("model_key") or "")
    current_protocol_running = bool(
        str(run.get("status") or "") in {"queued", "running"}
        and model_key in PLAN_MODELS
        and str(metrics.get("contract") or "")
        == str(PLAN_MODELS[model_key].get("family") or "")
    )
    return {
        **run,
        "metrics": metrics,
        "training_protocol_status": (
            "current" if current_protocol
            else "current_running" if current_protocol_running
            else "current_failed" if current_protocol_attempt_failed
            else "legacy_invalid"
        ),
        "labeled_trade_date_count": labeled_sessions or None,
    }


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=_BUSINESS_TZ)
        return parsed.astimezone(_BUSINESS_TZ)
    except Exception:
        return None


def interrupt_plan_model_train_run(
    run_id: int,
    *,
    error_code: str,
    message: str,
    run_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Terminate one asynchronous run and prevent later candidate publication."""
    run = run_snapshot or get_model_train_run(int(run_id))
    if not run or str(run.get("status") or "") not in {"running", "queued"}:
        return run
    metrics = dict(run.get("metrics") or {})
    timeline = list(metrics.get("timeline") or [])
    now = datetime.now(_BUSINESS_TZ).isoformat()
    timeline.append({"at": now, "phase": "failed", "progress": 1.0, "message": message})
    metrics["timeline"] = timeline[-20:]
    update_model_train_run(
        int(run_id), status="failed", phase="failed", progress=1.0,
        message=message, metrics=metrics, error=error_code, finished_at=now,
    )
    model_key = str(metrics.get("model_key") or "")
    context = metrics.get("targeted_training_context") if isinstance(
        metrics.get("targeted_training_context"), dict
    ) else {}
    prior_recovery = context.get("failure_recovery") if isinstance(
        context.get("failure_recovery"), dict
    ) else {}
    if model_key in PLAN_MODELS:
        _enqueue_plan_model_training_recovery(
            model_key=model_key,
            failed_run_id=int(run_id),
            root_run_id=int(prior_recovery.get("root_run_id") or run_id),
            retry_count=int(prior_recovery.get("retry_count") or 0),
            classification=classify_plan_model_training_failure(error_code),
        )
    refreshed = dict(run)
    refreshed.update({
        "status": "failed", "phase": "failed", "progress": 1.0,
        "message": message, "metrics": metrics, "error": error_code,
        "finished_at": now,
    })
    return refreshed


def _mark_stale_train_run(run: dict[str, Any] | None, *, stale_minutes: int = 20) -> dict[str, Any] | None:
    if not run or str(run.get("status") or "") not in {"running", "queued"}:
        return run
    updated_at = _parse_dt(run.get("updated_at") or run.get("created_at"))
    if not updated_at or datetime.now(_BUSINESS_TZ) - updated_at < timedelta(minutes=max(5, stale_minutes)):
        return run
    return interrupt_plan_model_train_run(
        int(run["id"]),
        error_code="stale_plan_model_training_run",
        message="训练心跳超时，已标记为可重训",
        run_snapshot=run,
    )


def reconcile_stale_plan_model_runs(*, stale_minutes: int = 20) -> dict[str, Any]:
    """Close every orphaned run, not only the latest run of each model family."""
    reconciled: list[int] = []
    for run in list_active_model_train_runs(limit=1000):
        refreshed = _mark_stale_train_run(run, stale_minutes=stale_minutes)
        if refreshed and str(refreshed.get("error") or "") == "stale_plan_model_training_run":
            reconciled.append(int(refreshed["id"]))
    return {
        "ok": True,
        "schema_version": "plan_model_stale_run_reconciliation_v1",
        "reconciled_run_ids": reconciled,
        "count": len(reconciled),
    }


def _environment_coverage_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    recorded = metrics.get("environment_coverage")
    if isinstance(recorded, dict) and recorded:
        return recorded
    state_observations = metrics.get("market_state_observations") or {}
    profile_observations = metrics.get("market_profile_observations") or {}
    covered_states = sum(
        1 for state in ("S0", "S1", "S2", "S3")
        if int(state_observations.get(state) or 0) > 0
    )
    covered_profiles = sum(
        1 for index in range(1, 13)
        if int(profile_observations.get(f"P{index:02d}") or 0) > 0
    )
    return {
        "parent_state_count": 4,
        "covered_parent_states": covered_states,
        "profile_count": 12,
        "minimum_required_profiles": 8,
        "covered_profiles": covered_profiles,
        "parent_states_complete": covered_states == 4,
        "profiles_complete": covered_profiles >= 8,
        "complete": covered_states == 4 and covered_profiles >= 8,
        "contract": (
            "point_in_time_four_parent_states_and_at_least_eight_"
            "of_twelve_profiles_coverage_v1"
        ),
    }


def _current_production_gate_audit(
    version: dict[str, Any] | None,
    artifact_summary: dict[str, Any],
) -> dict[str, Any]:
    if not version:
        return {"status": "not_applicable", "passed": False, "blocked_reasons": ["no_active_version"]}
    metrics = version.get("metrics") or {}
    validation = version.get("validation") or {}
    environment_coverage = _environment_coverage_summary(metrics)
    task_native_oos = metrics.get("task_native_oos") or {}
    task_baseline = metrics.get("task_baseline") or {}
    strategy_impact = metrics.get("strategy_impact_ab") or {}
    eligibility = validation.get("production_eligibility") or {}
    recorded_gates = eligibility.get("gates") or {}
    model_key = str(version.get("model_key") or "")
    gates = {
        "artifact_ready": bool(
            artifact_summary.get("artifact_ready")
            and artifact_summary.get("model_path_exists")
        ),
        "data_ready": bool(recorded_gates.get("data_ready")),
        "current_task_native_evaluation_contract": (
            str(metrics.get("model_evaluation_contract") or "")
            == MODEL_EVALUATION_CONTRACT
        ),
        "current_model_specific_contract": (
            str(metrics.get("event_impact_contract") or "")
            == EVENT_IMPACT_CONTRACT
            and str(artifact_summary.get("model_type") or "")
            == "event_impact_assessor_multihead_v4"
            if model_key == "event_impact_assessor_v2"
            else str(((metrics.get("task_metrics") or {}).get("contract") or ""))
            == NARRATIVE_TASK_EVALUATION_CONTRACT
            if model_key == "narrative_transition_multitask_v1"
            else True
        ),
        "event_structured_feature_contract": (
            str(((metrics.get("event_feature_audit") or {}).get("contract") or ""))
            == EVENT_FEATURE_CONTRACT
            if model_key == "event_impact_assessor_v2"
            else True
        ),
        "fresh_unconsumed_formal_oos_cohort": (
            bool(
                (metrics.get("event_research_cohort_governance") or {}).get(
                    "allowed"
                )
            )
            if model_key == "event_impact_assessor_v2"
            else True
        ),
        "task_metric_passed": bool(metrics.get("task_metric_passed")),
        "task_native_walk_forward_oos": bool(task_native_oos.get("passed")),
        "task_native_oos_baseline_outperformed": bool(
            task_native_oos.get("baseline_outperformed")
        ),
        "task_baseline_comparison_executed": bool(
            task_baseline.get("comparison_executed")
        ),
        "task_native_baseline_outperformed": bool(
            task_baseline.get("outperformed")
        ),
        "environment_coverage_complete": bool(environment_coverage.get("complete")),
        "walk_forward_windows": int(
            metrics.get("walk_forward_windows") or metrics.get("oos_windows") or 0
        ) >= 5,
        "training_protocol": bool(recorded_gates.get("training_protocol")),
        "controlled_strategy_ab": controlled_strategy_ab_passed_current_contract(
            str(version.get("model_key") or ""),
            strategy_impact,
        ),
        "live_observation_20_sessions": bool(
            recorded_gates.get("live_observation_20_sessions")
        ),
        "human_review": bool(recorded_gates.get("human_review")),
        "recorded_production_eligible": bool(validation.get("production_eligible")),
        "recorded_eligibility_consistent": bool(eligibility.get("eligible")),
    }
    blocked = [key for key, passed in gates.items() if not passed]
    return {
        "schema_version": "plan_model_current_gate_audit_v3",
        "status": "current_gate_passed" if not blocked else "legacy_production_current_gate_not_passed",
        "passed": not blocked,
        "gates": gates,
        "blocked_reasons": blocked,
        "non_destructive": True,
    }


def audit_plan_model_production_eligibility(
    version: dict[str, Any] | None,
) -> dict[str, Any]:
    """Recompute production authority from current evidence, never from active state."""

    return _current_production_gate_audit(version, _artifact_summary(version))


def active_production_plan_model_bindings() -> dict[str, dict[str, Any] | None]:
    """Return only active versions that still pass every current production gate."""

    bindings: dict[str, dict[str, Any] | None] = {}
    for model_key in PLAN_MODELS:
        version = get_active_model_version(model_key)
        audit = audit_plan_model_production_eligibility(version)
        bindings[model_key] = (
            {
                "id": int(version["id"]),
                "model_key": model_key,
                "version": str(version.get("version") or ""),
                "status": str(version.get("status") or ""),
                "production_eligible": True,
            }
            if version and audit.get("passed")
            else None
        )
    return bindings


def reconcile_plan_model_production_gates() -> dict[str, Any]:
    """Deactivate active plan models whose current release evidence is incomplete."""

    items: list[dict[str, Any]] = []
    for model_key in PLAN_MODELS:
        version = get_active_model_version(model_key)
        if not version:
            items.append(
                {
                    "model_key": model_key,
                    "action": "no_active_version",
                    "active": False,
                }
            )
            continue
        audit = audit_plan_model_production_eligibility(version)
        if audit.get("passed"):
            items.append(
                {
                    "model_key": model_key,
                    "model_version_id": int(version["id"]),
                    "version": str(version.get("version") or ""),
                    "action": "kept_active",
                    "active": True,
                    "audit": audit,
                }
            )
            continue
        metrics = version.get("metrics") or {}
        legacy = (
            str(metrics.get("model_evaluation_contract") or "")
            != MODEL_EVALUATION_CONTRACT
        )
        validation = dict(version.get("validation") or {})
        eligibility = dict(validation.get("production_eligibility") or {})
        eligibility["eligible"] = False
        eligibility["blocked_reasons"] = sorted(
            set(
                [
                    *list(eligibility.get("blocked_reasons") or []),
                    *list(audit.get("blocked_reasons") or []),
                ]
            )
        )
        validation["production_eligible"] = False
        validation["production_eligibility"] = eligibility
        validation["production_gate_reconciliation"] = {
            "schema_version": "plan_model_production_gate_reconciliation_v1",
            "reconciled_at": datetime.now().isoformat(timespec="seconds"),
            "action": "retired_legacy" if legacy else "blocked_production_gate",
            "audit": audit,
        }
        update_model_version_validation(int(version["id"]), validation=validation)
        status = "retired_legacy" if legacy else "blocked_production_gate"
        update_model_version_status(
            int(version["id"]),
            status=status,
            is_active=False,
        )
        items.append(
            {
                "model_key": model_key,
                "model_version_id": int(version["id"]),
                "version": str(version.get("version") or ""),
                "action": status,
                "active": False,
                "audit": audit,
            }
        )
    _status_cache.update({"at": 0.0, "value": None})
    _operations_cache.update({"at": 0.0, "value": None})
    return {
        "ok": True,
        "schema_version": "plan_model_production_gate_reconciliation_v1",
        "deactivated_count": sum(
            item.get("action") in {"retired_legacy", "blocked_production_gate"}
            for item in items
        ),
        "active_count": sum(bool(item.get("active")) for item in items),
        "items": items,
    }


def reconcile_plan_model_live_health(
    health_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Quarantine repeatedly failing production models and rebind safely.

    Rollback is allowed only to an older artifact that still passes the current
    production gate.  If none exists, the model family is disabled and the
    Champion continues with deterministic factors.
    """
    from app.services.production_strategy import ensure_production_strategy_champion

    actions: list[dict[str, Any]] = []
    for health in health_rows:
        if str(health.get("status") or "") != "quarantined":
            continue
        model_key = str(health.get("model_key") or "")
        current = get_active_model_version(model_key) if model_key in PLAN_MODELS else None
        if not current or str(current.get("version") or "") != str(health.get("active_version") or ""):
            continue
        rollback = None
        for candidate in list_model_versions(model_key, limit=200):
            if int(candidate.get("id") or 0) >= int(current.get("id") or 0):
                continue
            if audit_plan_model_production_eligibility(candidate).get("passed"):
                rollback = candidate
                break

        validation = dict(current.get("validation") or {})
        validation["production_eligible"] = False
        validation["live_health_quarantine"] = {
            "schema_version": "plan_model_live_health_quarantine_v1",
            "quarantined_at": datetime.now(_BUSINESS_TZ).isoformat(),
            "reason": str(health.get("reason") or "continuous_live_health_failure"),
            "degraded_streak": int(health.get("degraded_streak") or 0),
            "rollback_model_version_id": int(rollback["id"]) if rollback else None,
        }
        update_model_version_validation(int(current["id"]), validation=validation)
        update_model_version_status(
            int(current["id"]),
            status="rolled_back_live_health" if rollback else "quarantined_live_health",
            is_active=False,
        )
        if rollback:
            set_active_model_version(int(rollback["id"]))
            action = {
                "model_key": model_key,
                "action": "rolled_back",
                "from_model_version_id": int(current["id"]),
                "to_model_version_id": int(rollback["id"]),
            }
        else:
            action = {
                "model_key": model_key,
                "action": "disabled_to_deterministic_fallback",
                "from_model_version_id": int(current["id"]),
                "to_model_version_id": None,
            }
        champion = ensure_production_strategy_champion(force_rebind=True)
        action["champion_id"] = int(champion.get("id") or 0)
        actions.append(action)
    return {
        "ok": True,
        "schema_version": "plan_model_live_health_reconciliation_v1",
        "items": actions,
        "count": len(actions),
    }


def _model_live_health_snapshot() -> dict[str, dict[str, Any]]:
    try:
        with business_conn() as c:
            rows = c.execute(
                """SELECT model_key,active_version,status,live_ic,hit_rate,turnover,
                          drawdown_contribution,decay,weight_multiplier,reason,metrics_json,updated_at
                     FROM model_live_health"""
            ).fetchall()
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        metrics = row.pop("metrics_json", None)
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except Exception:
                metrics = {}
        evidence_maturity = (metrics or {}).get("evidence_maturity") or {}
        result[str(row.get("model_key") or "")] = {
            **row,
            "metrics": metrics or {},
            "evidence_maturity": evidence_maturity,
        }
    return result


def _latest_market_state_code() -> str:
    """Return display-only market state without fabricating an S1 fallback."""
    try:
        from app.data_platform.db import _conn as data_conn
        with data_conn() as c:
            row = c.execute(
                """SELECT market_state FROM dc_market_context_snapshot
                   ORDER BY trade_date DESC LIMIT 1"""
            ).fetchone()
        return str(dict(row).get("market_state") or "") if row else ""
    except Exception:
        return ""


def _compact_plan_model_train_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the bounded state needed by the live polling console.

    Training metrics include immutable fold evidence and, for event models,
    distribution audits large enough to make a 15-second status poll hundreds
    of kilobytes. Those artifacts remain available through model-version
    evidence; the operations board only renders task state, failure recovery
    and its recent timeline.
    """
    if not isinstance(run, dict):
        return None
    fields = (
        "id", "model_key", "model_version_id", "status", "phase", "progress", "message", "error",
        "created_at", "started_at", "updated_at", "finished_at", "training_protocol_status",
        "labeled_trade_date_count",
    )
    metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
    compact_metrics = {
        key: metrics[key]
        for key in ("failure_recovery", "timeline")
        if key in metrics
    }
    return {
        **{field: run[field] for field in fields if field in run},
        "metrics": compact_metrics,
    }


def plan_model_operations_status(*, cache_seconds: int = 15) -> dict[str, Any]:
    """Lightweight polling contract: no coverage scans or artifact loading."""
    now_monotonic = time.monotonic()
    cached = _operations_cache.get("value")
    if isinstance(cached, dict) and now_monotonic - float(_operations_cache.get("at") or 0) < max(1, cache_seconds):
        return cached
    runs = list_model_train_runs(limit=200)
    health = _model_live_health_snapshot()
    latest_by_model = {
        key: _with_train_timeline(_latest_run_for_model(key, runs))
        for key in PLAN_MODELS
    }
    active_by_model = {key: get_active_model_version(key) for key in PLAN_MODELS}
    governance_by_model = {
        key: _select_plan_model_governance_version(
            list_model_versions(key, limit=10)
        )
        for key in PLAN_MODELS
    }
    try:
        from app.services.model_signal_fusion import (
            ENVIRONMENT_MODEL_MULTIPLIERS,
            LIMITED_LIVE_WEIGHT_SCALE,
            MODEL_WEIGHTS,
            _limited_live_authorization,
            _resolve_model_versions,
            _resolve_online_inference_versions,
        )
    except Exception:
        MODEL_WEIGHTS = {key: 0.0 for key in PLAN_MODELS}
        ENVIRONMENT_MODEL_MULTIPLIERS = {}
    state_code = _latest_market_state_code()
    environment_weights = ENVIRONMENT_MODEL_MULTIPLIERS.get(state_code, {})
    try:
        authorized_versions = _resolve_model_versions(None)
        online_versions = _resolve_online_inference_versions(authorized_versions, None)
    except Exception:
        authorized_versions = {key: None for key in PLAN_MODELS}
        online_versions = dict(authorized_versions)
    next_check_at = None
    items: list[dict[str, Any]] = []
    for key, contract in PLAN_MODELS.items():
        active = active_by_model.get(key)
        online_version = online_versions.get(key)
        limited_authorization = (
            _limited_live_authorization(online_version)
            if online_version and not active
            else {
                "authorized": False,
                "tier": "not_applicable",
                "weight_scale": 0.0,
                "reason": "production_authorized_or_unavailable",
                "observed_return_sessions": 0,
            }
        )
        limited_authorized = bool(limited_authorization["authorized"])
        live = health.get(key) or {}
        if active and str(live.get("active_version") or "") != str(active.get("version") or ""):
            live = {}
        maturity = live.get("evidence_maturity") or {}
        mature_outcomes = int(maturity.get("mature_20d_outcomes") or 0)
        minimum_required = int(maturity.get("minimum_required") or 5)
        raw_health_status = str(live.get("status") or "not_started")
        observation_status = (
            "not_applicable"
            if not active
            else raw_health_status
            if raw_health_status in {"degraded", "watch"}
            else "observing"
            if mature_outcomes < minimum_required
            else raw_health_status
        )
        production_observation = {
            "scope": "production_runtime",
            "status": observation_status,
            "production_active": bool(active),
            "active_version": (active or {}).get("version"),
            "mature_20d_outcomes": mature_outcomes,
            "minimum_required": minimum_required,
            "updated_at": live.get("updated_at") if active else None,
        }
        online_observation = {
            "scope": "online_inference",
            "status": "online_limited" if limited_authorized else "online_shadow" if online_version else "not_available",
            "version_id": int(online_version.get("id") or 0) or None if online_version else None,
            "version": online_version.get("version") if online_version else None,
            "trade_authorized": bool(active or limited_authorized),
            "risk_authorized": bool(active),
            "weight_cap": 0.20 if limited_authorized else 0.0,
            "authorization_tier": (
                "production" if active else str(limited_authorization["tier"])
                if limited_authorized else "shadow_only" if online_version else "unavailable"
            ),
            "authorization_reason": (
                "production_contract_passed" if active
                else str(limited_authorization["reason"])
            ),
            "future_observed_sessions": int(
                limited_authorization["observed_return_sessions"]
            ),
            "production_change_forbidden": not bool(active),
        }
        governance_version = governance_by_model.get(key) or {}
        governance_validation = governance_version.get("validation") if isinstance(
            governance_version.get("validation"), dict
        ) else {}
        shadow = governance_validation.get("model_shadow") if isinstance(
            governance_validation.get("model_shadow"), dict
        ) else {}
        latest_shadow_observation = (
            list(shadow.get("observations") or [])[-1]
            if shadow.get("observations")
            else {}
        )
        candidate_applicable = bool(
            governance_version
            and str(governance_version.get("status") or "") == "candidate"
            and shadow
        )
        candidate_observation = {
            "scope": "candidate_future_shadow",
            "status": (
                str(shadow.get("status") or "pending_enrollment")
                if candidate_applicable
                else "not_applicable"
            ),
            "candidate_active": candidate_applicable,
            "model_version_id": (
                int(governance_version.get("id") or 0) or None
                if candidate_applicable
                else None
            ),
            "version": (
                str(governance_version.get("version") or "") or None
                if candidate_applicable
                else None
            ),
            "signal_start_trade_date": shadow.get("signal_start_trade_date"),
            "observed_return_sessions": int(
                shadow.get("observed_return_sessions") or 0
            ),
            "required_return_sessions": int(
                shadow.get("required_return_sessions") or 20
            ),
            "minimum_effect_days": int(shadow.get("minimum_effect_days") or 4),
            "candidate_model_effect_days": int(
                latest_shadow_observation.get("candidate_model_effect_days") or 0
            ),
            "last_observed_trade_date": shadow.get("last_observed_trade_date"),
            "autonomous_management": bool(candidate_applicable),
            "manager": "model_shadow_observation" if candidate_applicable else None,
            "production_change_forbidden": bool(
                shadow.get("production_change_forbidden", True)
            ) if candidate_applicable else True,
        }
        health_multiplier = float(live.get("weight_multiplier") or 1.0) if active else 0.0
        maturity_multiplier = 0.85 if observation_status in {"observing", "watch"} else 1.0
        environment_multiplier = float(environment_weights.get(key, 1.0))
        base_weight = (
            float(MODEL_WEIGHTS.get(key, 0.0))
            if active
            else float(MODEL_WEIGHTS.get(key, 0.0)) * float(
                limited_authorization["weight_scale"]
            )
            if limited_authorized
            else 0.0
        )
        raw_contribution = base_weight * environment_multiplier * health_multiplier * maturity_multiplier
        run = latest_by_model.get(key)
        latest_run_metrics = (run or {}).get("metrics") or {}
        failure_recovery = latest_run_metrics.get("failure_recovery") if isinstance(
            latest_run_metrics.get("failure_recovery"), dict
        ) else {}
        active_metrics = (active or {}).get("metrics") or {}
        evaluation_metrics = latest_run_metrics or active_metrics
        context = ((run or {}).get("metrics") or {}).get("targeted_training_context") or {}
        trigger_reason = (
            context.get("trigger_reason")
            or context.get("reason")
            or ("首次训练" if not run else "定期模型治理")
        )
        items.append({
            "model_key": key,
            "name": contract.get("name"),
            "latest_train_run": _compact_plan_model_train_run(run),
            "trigger_reason": trigger_reason,
            "requested_by": context.get("requested_by") or ("autonomous_daily" if run else None),
            "next_check_at": next_check_at,
            "candidate_observation": candidate_observation,
            "production_observation": production_observation,
            "online_observation": online_observation,
            "observation": production_observation,
            "training_failure_recovery": failure_recovery,
            "evaluation": {
                "status": (
                    "current"
                    if str(
                        evaluation_metrics.get("model_evaluation_contract") or ""
                    )
                    == MODEL_EVALUATION_CONTRACT
                    else "outdated"
                    if evaluation_metrics
                    else "not_evaluated"
                ),
                "recorded_contract": evaluation_metrics.get(
                    "model_evaluation_contract"
                ),
                "current_contract": MODEL_EVALUATION_CONTRACT,
                "task_metric_passed": bool(
                    evaluation_metrics.get("task_metric_passed")
                ),
                "task_native_oos": evaluation_metrics.get(
                    "task_native_oos"
                ),
                "strategy_impact_status": str(
                    (
                        evaluation_metrics.get("strategy_impact_ab")
                        or {}
                    ).get("status")
                    or "not_run"
                ),
                "definition": evaluation_contract(key),
            },
            "weight_breakdown": {
                "base": round(base_weight, 4),
                "environment": round(environment_multiplier, 4),
                "health": round(health_multiplier, 4),
                "maturity": round(maturity_multiplier, 4),
                "raw_contribution": round(raw_contribution, 6),
                "market_state": state_code,
            },
        })
    raw_total = sum(float((item.get("weight_breakdown") or {}).get("raw_contribution") or 0.0) for item in items)
    scale = min(1.0, 0.55 / raw_total) if raw_total > 0 else 0.0
    for item in items:
        breakdown = item["weight_breakdown"]
        raw_contribution = float(breakdown.get("raw_contribution") or 0.0)
        breakdown["normalized_model_weight"] = round(
            raw_contribution / raw_total if raw_total > 0 else 0.0, 6
        )
        breakdown["final_fusion_weight"] = round(raw_contribution * scale, 6)
        breakdown["non_model_base_weight"] = round(max(0.45, 1.0 - raw_total * scale), 6)
    result = {
        "ok": True,
        "schema_version": "plan_model_operations_status_v3",
        "items": items,
        "scheduler": {
            "status": "retired",
            "heartbeat_at": None,
            "next_check_at": next_check_at,
            "interval_seconds": None,
            "replacement": "autonomous_daily_plan_model_governance",
        },
        "evaluation_contracts": supported_model_contracts(),
        "generated_at": datetime.now().isoformat(),
        "cache_seconds": max(1, cache_seconds),
    }
    _operations_cache.update({"at": now_monotonic, "value": result})
    return result


def _select_plan_model_governance_version(
    versions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    newest = versions[0] if versions else None
    shadow_candidate = next(
        (
            version for version in versions
            if str(version.get("status") or "") == "candidate"
            and str(
                (
                    (version.get("validation") or {}).get("model_shadow")
                    or {}
                ).get("status")
                or ""
            )
            in {
                "awaiting_first_future_signal",
                "observing",
                "extended_for_inconclusive_evidence",
                "passed",
            }
        ),
        None,
    )
    ab_passed_candidate = next(
        (
            version for version in versions
            if str(version.get("status") or "") == "candidate"
            and str(
                (
                    (version.get("metrics") or {}).get("strategy_impact_ab")
                    or {}
                ).get("status")
                or ""
            )
            == "passed"
            and str(
                (
                    (version.get("validation") or {}).get("review")
                    or {}
                ).get("status")
                or ""
            )
            != "rejected"
        ),
        None,
    )
    return shadow_candidate or ab_passed_candidate or newest


def _plan_model_candidate_governance_blocker(
    model_key: str,
) -> dict[str, Any] | None:
    for version in list_model_versions(model_key, limit=10):
        if str(version.get("status") or "") != "candidate":
            continue
        validation = version.get("validation") if isinstance(
            version.get("validation"), dict
        ) else {}
        review = validation.get("review") if isinstance(
            validation.get("review"), dict
        ) else {}
        if str(review.get("status") or "") == "rejected":
            continue
        model_shadow = validation.get("model_shadow") if isinstance(
            validation.get("model_shadow"), dict
        ) else {}
        impact = (version.get("metrics") or {}).get("strategy_impact_ab")
        if (
            str(validation.get("status") or "")
            in {"pending_strategy_ab", "pending_live_observation", "shadow_passed"}
            or str(model_shadow.get("status") or "")
            in {
                "awaiting_first_future_signal",
                "observing",
                "extended_for_inconclusive_evidence",
                "passed",
            }
            or (
                isinstance(impact, dict)
                and str(impact.get("status") or "") == "passed"
            )
        ):
            return version
    return None


@_serialize_status_refresh
def model_family_status() -> dict[str, Any]:
    now = time.monotonic()
    cached = _status_cache.get("value")
    if isinstance(cached, dict) and now - float(_status_cache.get("at") or 0) < 30:
        return cached
    items = []
    base_coverage = _base_coverage_snapshot()
    train_runs = list_model_train_runs(limit=100)
    live_health = _model_live_health_snapshot()
    for key, contract in PLAN_MODELS.items():
        versions = list_model_versions(key, limit=10)
        active = get_active_model_version(key)
        newest = versions[0] if versions else None
        latest = _select_plan_model_governance_version(versions)
        if active:
            active_metrics = dict(active.get("metrics") or {})
            active_metrics["environment_coverage"] = _environment_coverage_summary(active_metrics)
            active = {**active, "metrics": active_metrics}
        if latest:
            latest_metrics_payload = dict(latest.get("metrics") or {})
            latest_metrics_payload["environment_coverage"] = _environment_coverage_summary(latest_metrics_payload)
            latest = {**latest, "metrics": latest_metrics_payload}
        coverage = _coverage_snapshot(key, base_coverage)
        latest_metrics = (latest or {}).get("metrics") or {}
        latest_training_sessions = int(
            latest_metrics.get("labeled_trade_date_count")
            or latest_metrics.get("training_trade_date_count")
            or 0
        )
        coverage["available_sessions"] = coverage["effective_sessions"]
        coverage["latest_training_sessions"] = latest_training_sessions or None
        coverage["coverage_basis"] = "current_database_point_in_time_availability"
        run = _with_train_timeline(_mark_stale_train_run(_latest_run_for_model(key, train_runs)))
        if latest and not latest_training_sessions and run:
            run_training_sessions = int(run.get("labeled_trade_date_count") or 0)
            if run_training_sessions:
                latest_training_sessions = run_training_sessions
                coverage["latest_training_sessions"] = run_training_sessions
        trained = bool(latest)
        latest_uses_current_protocol = bool(
            latest and _metrics_satisfy_current_training_protocol(latest_metrics)
        )
        latest_validation = (latest or {}).get("validation") or {}
        latest_review = latest_validation.get("review") or {}
        rejected_candidate = bool(
            str(latest_validation.get("status") or "") == "failed"
            or str(latest_review.get("status") or "") == "rejected"
        )
        active_artifact = _artifact_summary(active)
        current_gate_audit = _current_production_gate_audit(active, active_artifact)
        production_ready = bool(active and current_gate_audit.get("passed"))
        if active and not production_ready:
            status = "legacy_production"
        elif production_ready:
            status = "production_current_standard"
        elif trained and not latest_uses_current_protocol:
            status = "legacy_invalid_candidate"
        elif trained and rejected_candidate:
            status = "rejected_candidate"
        elif trained:
            status = "candidate"
        elif coverage["data_ready"]:
            status = "trainable"
        else:
            status = "proxy"
        eligibility = latest_validation.get("production_eligibility") or {}
        blocked_reasons = list(eligibility.get("blocked_reasons") or [])
        domain_minimum_ready = bool(
            coverage.get(
                "domain_minimum_ready",
                int(coverage.get("effective_sessions") or 0)
                >= int(coverage.get("minimum_sessions") or 0),
            )
        )
        formal_history_ready = bool(
            coverage.get("formal_history_ready", coverage.get("data_ready"))
        )
        if not domain_minimum_ready:
            blocked_reasons.append(
                f"domain_coverage_{coverage['effective_sessions']}_lt_{coverage['minimum_sessions']}"
            )
        if not formal_history_ready:
            blocked_reasons.append(
                f"formal_walk_forward_coverage_{coverage['effective_sessions']}_lt_"
                f"{coverage.get('formal_walk_forward_sessions', coverage['minimum_sessions'])}"
            )
        if status == "legacy_invalid_candidate":
            if (
                str(latest_metrics.get("model_evaluation_contract") or "")
                != MODEL_EVALUATION_CONTRACT
            ):
                blocked_reasons.append("legacy_model_evaluation_contract_invalid")
            elif (
                key == "event_impact_assessor_v2"
                and str(
                    ((latest_metrics.get("event_feature_audit") or {}).get("contract") or "")
                )
                != EVENT_FEATURE_CONTRACT
            ):
                blocked_reasons.append("outdated_event_feature_contract_invalid")
            else:
                blocked_reasons.append("legacy_truncated_training_dataset_invalid")
        if status == "candidate" and not production_ready and not blocked_reasons:
            blocked_reasons.append("candidate_requires_review_or_live_observation")
        data_actions = []
        if key == "capital_persistence_multitask_v1":
            data_actions.append({"label": "补资金与席位历史", "api": "/api/data-center/backfill/capital-structure", "reason": "margin_detail/hk_hold/top_list/top_inst"})
        if key in {"event_impact_assessor_v2", "narrative_transition_multitask_v1"}:
            data_actions.append({"label": "补新闻/公告/主题历史", "api": "/api/data-center/backfill/major-news", "reason": "3年文本与250日主题快照"})
        if key in {"medium_horizon_lambdarank_v1", "leader_lambdarank_v1"}:
            data_actions.append({"label": "刷新市场与指数训练数据", "api": "/api/model-strategy/market-context/materialize-training-history", "reason": "滚动OOS与基准回放"})
        health = live_health.get(key) or {}
        if not active:
            health = {
                "model_key": key,
                "status": "not_applicable",
                "reason": "没有生产激活版本，不进入生产运行观察",
                "weight_multiplier": 0.0,
                "evidence_maturity": {
                    "status": "not_applicable",
                    "mature_20d_outcomes": 0,
                    "minimum_required": 5,
                },
            }
        elif (
            not health
            or str(health.get("active_version") or "") != str(active.get("version") or "")
        ):
            health = {
                "model_key": key,
                "active_version": active.get("version"),
                "status": "observing",
                "reason": "当前生产版本尚未形成足够的未来表现观察",
                "weight_multiplier": 1.0,
                "evidence_maturity": {
                    "status": "insufficient",
                    "mature_20d_outcomes": 0,
                    "minimum_required": 5,
                },
            }
        else:
            maturity = health.get("evidence_maturity") or {}
            mature_outcomes = int(maturity.get("mature_20d_outcomes") or 0)
            minimum_required = int(maturity.get("minimum_required") or 5)
            if str(health.get("status") or "") not in {"degraded", "watch"} and mature_outcomes < minimum_required:
                health = {
                    **health,
                    "status": "observing",
                    "reason": "生产版本未来20日成熟结果不足",
                }
        items.append({
            "model_key": key,
            **contract,
            "training_contract": {
                "family": contract.get("family"),
                "objective": contract.get("objective"),
                "metric": contract.get("metric"),
                "tasks": contract.get("tasks") or [],
                "eval_at": contract.get("eval_at") or [],
                "label_gain": contract.get("label_gain") or [],
                "params": LIGHTGBM_PARAMS if "LightGBM" in str(contract.get("family") or "") else {},
                "promotion_policy": "candidate -> human_review -> production；模拟系统可豁免20-session真实观察",
            },
            "evaluation_contract": evaluation_contract(key),
            "evaluation_contract_status": (
                "current"
                if str(latest_metrics.get("model_evaluation_contract") or "")
                == MODEL_EVALUATION_CONTRACT
                else "outdated"
                if latest_metrics
                else "not_evaluated"
            ),
            "status": status,
            "coverage": coverage,
            "active_version": active,
            "latest_version": latest,
            "newest_recorded_version": newest,
            "latest_training_protocol_status": (
                "current" if latest_uses_current_protocol else "legacy_invalid"
            ) if latest else "not_trained",
            "active_artifact": active_artifact,
            "latest_artifact": _artifact_summary(latest),
            "current_gate_audit": current_gate_audit,
            "live_health": health,
            "latest_train_run": run,
            "versions": versions,
            "blocked_reasons": blocked_reasons,
            "data_actions": data_actions,
        })
    counts = {
        status: sum(1 for item in items if item["status"] == status)
        for status in (
            "production_current_standard",
            "legacy_production",
            "candidate",
            "rejected_candidate",
            "trainable",
            "proxy",
            "legacy_invalid_candidate",
        )
    }
    result = {
        "ok": True,
        "schema_version": "plan_model_control_v2",
        "items": items,
        "counts": counts,
        "lightgbm_params": LIGHTGBM_PARAMS,
        "evaluation_contracts": supported_model_contracts(),
        # Status GETs are strictly read-only. Stale-run reconciliation is
        # owned by autonomous model maintenance and must not delay or mutate
        # a console read.
        "stale_reconciliation": {
            "status": "deferred_to_autonomous_model_maintenance",
            "read_only": True,
        },
    }
    _status_cache.update({"at": now, "value": result})
    return result


def _enqueue_continuous_research_for_model_version(
    *, model_key: str, model_version_id: int, version: str,
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import (
            enqueue_factor_strategy_research_event,
        )

        task = enqueue_factor_strategy_research_event(
            trigger="plan_model_training_completed",
            evidence_type="model_version",
            evidence_id=int(model_version_id),
            evidence={"model_key": model_key, "version": version},
            priority=75,
        )
        return {
            "status": str(task.get("status") or "queued"),
            "task_id": int(task.get("id") or 0),
        }
    except Exception as exc:  # Training evidence remains valid if the queue is unavailable.
        return {"status": "enqueue_failed", "error": str(exc)[:300]}


def _enqueue_model_candidate_research(
    *, model_key: str, model_version_id: int, version: str,
) -> dict[str, Any]:
    return {
        "status": "retired_legacy_evaluation_protocol",
        "model_key": model_key,
        "model_version_id": int(model_version_id),
        "version": version,
        "replacement": MODEL_EVALUATION_CONTRACT,
        "production_change_forbidden": True,
    }


def _enqueue_model_strategy_impact_validation(
    *, model_key: str, model_version_id: int, version: str,
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import create_evolution_task

        task = create_evolution_task(
            task_type="model_strategy_impact_validation",
            payload={
                "model_key": model_key,
                "model_version_id": int(model_version_id),
                "version": version,
                "production_change_forbidden": True,
            },
            dedupe_key=f"model_strategy_impact_validation:v2:{int(model_version_id)}",
            priority=76,
        )
        return {"status": str(task.get("status") or "queued"), "task_id": int(task.get("id") or 0)}
    except Exception as exc:
        return {"status": "enqueue_failed", "error": str(exc)[:300]}


def _enqueue_plan_model_autonomy_continuation(
    *, model_key: str, model_version_id: int,
) -> dict[str, Any]:
    try:
        from app.services.evolution_orchestrator import create_evolution_task

        task = create_evolution_task(
            task_type="plan_model_autonomy",
            payload={
                "trigger": "previous_task_native_model_training_completed",
                "predecessor_model_key": model_key,
                "predecessor_model_version_id": int(model_version_id),
            },
            dedupe_key=f"plan_model_autonomy:task-native-v1:after-{int(model_version_id)}",
            priority=73,
        )
        return {"status": str(task.get("status") or "queued"), "task_id": int(task.get("id") or 0)}
    except Exception as exc:
        return {"status": "enqueue_failed", "error": str(exc)[:300]}


def evaluate_plan_model_candidate_quick(
    model_key: str,
    candidate_spec: dict[str, Any],
    *,
    dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the cheap, non-promotable two-window model diagnostic."""
    dataset = dataset or _build_plan_training_dataset(model_key)
    target_states = {
        str(item) for item in candidate_spec.get("target_market_states") or [] if str(item)
    }
    if target_states:
        dataset["train_sample_weight"] = np.asarray([
            2.0 if str(state) in target_states else 1.0
            for state in dataset.get("train_market_states") or []
        ], dtype=float)
    trained = _fit_lightgbm_plan_model(
        model_key,
        dataset,
        candidate_spec=candidate_spec,
        quick_diagnostic=True,
    )
    return dict(trained.get("metrics") or {})


def prepare_plan_model_quick_evaluator(
    model_key: str,
) -> tuple[Callable[[str, dict[str, Any]], dict[str, Any]], dict[str, Any]]:
    """Freeze one point-in-time dataset for the complete quick tournament."""
    dataset = _build_plan_training_dataset(model_key)

    def evaluate(key: str, spec: dict[str, Any]) -> dict[str, Any]:
        if key != model_key:
            raise ValueError("快速诊断模型与冻结数据集不一致")
        return evaluate_plan_model_candidate_quick(key, spec, dataset=dataset)

    frozen = {
        "date_start": dataset["dates"]["start"],
        "date_end": dataset["dates"]["end"],
        "trade_date_count": dataset["trade_date_count"],
        "sample_rows": dataset["sample_rows"],
        "dataset_signature": _dataset_signature(model_key, dataset),
    }
    return evaluate, frozen


def _run_training_sync(
    model_key: str,
    run_id: int,
    training_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat()
    contract = PLAN_MODELS[model_key]
    timeline: list[dict[str, Any]] = []
    failure_response: dict[str, Any] | None = None

    def progress(phase: str, pct: float, message: str, metrics: dict[str, Any] | None = None) -> None:
        now = datetime.now().isoformat()
        timeline.append({"at": now, "phase": phase, "progress": pct, "message": message})
        previous = (get_model_train_run(run_id) or {}).get("metrics") or {}
        payload = {
            **previous,
            "model_key": model_key,
            "contract": contract["family"],
            **(metrics or {}),
            "timeline": timeline[-20:],
        }
        update_model_train_run(run_id, phase=phase, progress=pct, message=message, metrics=payload, started_at=started_at)

    try:
        progress("coverage", 0.1, "检查计划书数据覆盖")
        coverage = _coverage_snapshot(model_key)
        if not coverage["data_ready"]:
            raise RuntimeError(
                "训练数据不足："
                f"领域最低 {coverage['effective_sessions']}/{coverage['minimum_sessions']}，"
                f"正式 Walk-forward 历史 "
                f"{coverage['effective_sessions']}/{coverage['formal_walk_forward_sessions']} sessions"
            )
        progress("dataset", 0.22, "开始构造点时训练样本与query标签", {"coverage": coverage})
        dataset = _build_plan_training_dataset(model_key, on_progress=progress)
        context = dict(training_context or {})
        target_states = {
            str(item) for item in (
                ((context.get("training_objective") or {}).get("target_market_states") or [])
            ) if str(item)
        }
        if target_states:
            dataset["train_sample_weight"] = np.asarray([
                2.0 if str(state) in target_states else 1.0
                for state in (dataset.get("train_market_states") or [])
            ], dtype=float)
        dataset["targeted_training_context"] = context
        progress(
            "dataset",
            0.43,
            f"数据集就绪：{dataset['sample_rows']} 行，{dataset['trade_date_count']} 个交易日，{dataset['feature_count']} 个特征",
            {"coverage": coverage, "dataset": {
                "sample_rows": dataset["sample_rows"],
                "raw_rows": dataset["raw_rows"],
                "train_rows": dataset["train_rows"],
                "validation_rows": dataset["validation_rows"],
                "test_rows": dataset["test_rows"],
                "trade_date_count": dataset["trade_date_count"],
                "feature_count": dataset["feature_count"],
                "horizon": dataset["horizon"],
                "dates": dataset["dates"],
                "label_quantiles": dataset["label_quantiles"],
                "feature_computation_contract": dataset.get("feature_computation_contract") or {},
                "symbol_sampling": dataset.get("symbol_sampling") or {},
                "moneyflow_raw_rows": dataset.get("moneyflow_raw_rows"),
                "capital_observation_count": dataset.get("capital_observation_count"),
                "training_protocol": dataset["training_protocol"],
            }},
        )
        event_research_cohort = (
            _event_research_cohort(
                _event_walk_forward_date_windows(
                    list(dataset.get("all_dates") or [])
                )
            )
            if model_key == "event_impact_assessor_v2"
            else None
        )
        event_research_guard = _event_targeted_research_cohort_guard(
            model_key,
            dataset,
            context,
        )
        if not bool(event_research_guard.get("allowed", True)):
            status = str(event_research_guard.get("status") or "")
            phase = (
                "waiting_for_natural_oos_cohort_advance"
                if status == "waiting_for_natural_oos_cohort_advance"
                else "blocked_by_event_research_cohort_contract"
            )
            message = (
                "事件正式五窗已被用于后继设计；等待事件日期自然前移后再评价"
                if phase == "waiting_for_natural_oos_cohort_advance"
                else "事件候选缺少可核验的来源五窗，已按研究批次合同阻断"
            )
            update_model_train_run(
                run_id,
                status="completed",
                phase=phase,
                progress=1.0,
                message=message,
                metrics={
                    "model_key": model_key,
                    "coverage": coverage,
                    "targeted_training_context": context,
                    "event_research_cohort": event_research_cohort or {},
                    "event_research_cohort_governance": event_research_guard,
                    "production_change_forbidden": True,
                    "timeline": [
                        *timeline[-19:],
                        {
                            "at": datetime.now().isoformat(),
                            "phase": phase,
                            "progress": 1.0,
                            "message": message,
                        },
                    ],
                },
                finished_at=datetime.now().isoformat(),
            )
            return {
                "ok": True,
                "run_id": run_id,
                "skipped": True,
                "reason": phase,
                "event_research_cohort_governance": event_research_guard,
            }
        duplicate, duplicate_version, data_signature = _is_duplicate_training_dataset(model_key, dataset)
        if duplicate:
            duplicate_id = (duplicate_version or {}).get("id")
            duplicate_name = (duplicate_version or {}).get("version") or "最近版本"
            update_model_train_run(
                run_id,
                status="completed",
                phase="skipped_duplicate_dataset",
                progress=1.0,
                message=f"训练数据与 {duplicate_name} 完全一致，已跳过重复训练",
                model_version_id=int(duplicate_id) if duplicate_id else None,
                metrics={
                    "model_key": model_key,
                    "coverage": coverage,
                    "dataset": {
                        "sample_rows": dataset["sample_rows"],
                        "raw_rows": dataset["raw_rows"],
                        "train_rows": dataset["train_rows"],
                        "validation_rows": dataset["validation_rows"],
                        "test_rows": dataset["test_rows"],
                        "trade_date_count": dataset["trade_date_count"],
                        "feature_count": dataset["feature_count"],
                        "horizon": dataset["horizon"],
                        "dates": dataset["dates"],
                        "label_quantiles": dataset["label_quantiles"],
                        "symbol_sampling": dataset.get("symbol_sampling") or {},
                        "moneyflow_raw_rows": dataset.get("moneyflow_raw_rows"),
                        "capital_observation_count": dataset.get("capital_observation_count"),
                        "training_protocol": dataset["training_protocol"],
                    },
                    "training_data_signature": data_signature,
                    "duplicate_of_version_id": duplicate_id,
                    "timeline": [
                        *timeline[-19:],
                        {"at": datetime.now().isoformat(), "phase": "skipped_duplicate_dataset", "progress": 1.0, "message": f"数据指纹未变化，复用 {duplicate_name}，不生成新版本"},
                    ],
                },
                finished_at=datetime.now().isoformat(),
            )
            return {"ok": True, "run_id": run_id, "skipped": True, "reason": "duplicate_training_dataset", "duplicate_of_version_id": duplicate_id}
        progress("training", 0.55, f"训练真实LightGBM模型：训练 {dataset['train_rows']} 行，验证 {dataset['validation_rows']} 行", {"coverage": coverage, "train_rows": dataset["train_rows"], "validation_rows": dataset["validation_rows"]})
        candidate_spec = (
            context.get("model_candidate_spec")
            if isinstance(context.get("model_candidate_spec"), dict)
            else None
        )
        trained = _fit_lightgbm_plan_model(
            model_key,
            dataset,
            candidate_spec=candidate_spec,
        )
        progress("validation", 0.80, "执行任务原生滚动样本外评价")
        metrics = {
            "effective_sessions": dataset["trade_date_count"],
            "available_sessions": coverage["effective_sessions"],
            "training_data_signature": data_signature,
            "targeted_training_context": context,
            "event_research_cohort": event_research_cohort or {},
            "event_research_cohort_governance": event_research_guard,
            **trained["metrics"],
        }
        research_shadow_only = bool(
            model_key == "narrative_transition_multitask_v1"
            and str(context.get("evaluation_scope") or "")
            == "research_shadow_only"
        )
        protocol_validation = validate_training_run_manifest({
            "model_key": model_key,
            "calendar": {
                "train_sessions": metrics.get("training_trade_date_count", 0),
                "validation_sessions": metrics.get("validation_trade_date_count", 0),
                "test_sessions": metrics.get("test_trade_date_count", 0),
                "purge_sessions": int(_training_protocol_for_model(model_key)["purge_sessions"]),
                "embargo_sessions": int(_training_protocol_for_model(model_key)["embargo_sessions"]),
            },
            "oos_windows": metrics.get("oos_windows", 0),
            "model_evaluation_contract": metrics.get(
                "model_evaluation_contract"
            ),
            "task_native_metrics_recorded": bool(metrics.get("task_metrics")),
            "task_baseline_comparison_executed": bool(
                (metrics.get("task_baseline") or {}).get(
                    "comparison_executed"
                )
            ),
            "task_baseline_outperformed": bool(
                (metrics.get("task_baseline") or {}).get("outperformed")
            ),
            "calibration_checked": True,
        })
        metric_gates = {
            "data_ready": True,
            "task_metric_passed": bool(metrics.get("task_metric_passed")),
            "current_task_native_evaluation_contract": (
                str(metrics.get("model_evaluation_contract") or "") == MODEL_EVALUATION_CONTRACT
            ),
            "current_model_specific_contract": (
                str(metrics.get("event_impact_contract") or "")
                == EVENT_IMPACT_CONTRACT
                if model_key == "event_impact_assessor_v2"
                else str(((metrics.get("task_metrics") or {}).get("contract") or ""))
                == NARRATIVE_TASK_EVALUATION_CONTRACT
                if model_key == "narrative_transition_multitask_v1"
                else True
            ),
            "event_structured_feature_contract": (
                str(((metrics.get("event_feature_audit") or {}).get("contract") or ""))
                == EVENT_FEATURE_CONTRACT
                if model_key == "event_impact_assessor_v2"
                else True
            ),
            "fresh_unconsumed_formal_oos_cohort": bool(
                event_research_guard.get("allowed", True)
            ),
            "task_native_walk_forward_oos": bool(
                (metrics.get("task_native_oos") or {}).get("passed")
            ),
            "task_native_baseline_outperformed": bool(
                (metrics.get("task_native_oos") or {}).get(
                    "baseline_outperformed"
                )
            ),
            "task_baseline_comparison_executed": bool(
                (metrics.get("task_baseline") or {}).get(
                    "comparison_executed"
                )
            ),
            "environment_coverage_complete": bool(
                (metrics.get("environment_coverage") or {}).get("complete")
            ),
            "training_protocol": bool(protocol_validation.get("ok")),
            "controlled_strategy_ab": False,
            "human_review": False,
            "live_observation_20_sessions": False,
        }
        if research_shadow_only:
            metric_gates["fresh_unconsumed_narrative_formal_oos_cohort"] = False
            metric_gates["research_shadow_not_production_evidence"] = False
        metric_blocked = [key for key, passed in metric_gates.items() if key not in {"human_review", "live_observation_20_sessions"} and not passed]
        validation = {
            "status": (
                "research_shadow_only"
                if research_shadow_only
                else
                "pending_strategy_ab"
                if metric_blocked == ["controlled_strategy_ab"]
                else "failed"
            ),
            "production_eligible": False,
            "production_eligibility": {
                "eligible": False,
                "blocked_reasons": metric_blocked,
                "gates": metric_gates,
            },
        }
        if research_shadow_only:
            validation["research_shadow_governance"] = {
                "schema_version": "alis_narrative_research_shadow_v1",
                "production_change_forbidden": True,
                "formal_promotion_forbidden": True,
                "formal_promotion_block_reason": (
                    "waiting_for_complete_new_126_session_oos_cohort"
                ),
                "historical_oos_scope": "diagnostic_only",
            }
        validation["training_protocol"] = protocol_validation
        version = f"{model_key}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        root = models_root() / model_key / version
        root.mkdir(parents=True, exist_ok=True)
        model_path = root / "model.pkl"
        with model_path.open("wb") as handle:
            pickle.dump(trained["model"], handle)
        model_type = trained["model_type"]
        fold_models_by_number = {
            int(item["fold"]): item["model"]
            for item in trained.get("fold_models") or []
        }
        fold_artifacts: list[dict[str, Any]] = []
        for fold_evidence in metrics.get("walk_forward_evidence") or []:
            fold_no = int(fold_evidence.get("fold") or 0)
            fold_model = fold_models_by_number.get(fold_no)
            if fold_model is None:
                raise RuntimeError(f"Walk-forward第{fold_no}窗缺少冻结模型")
            fold_root = root / "walk_forward" / f"fold_{fold_no}"
            fold_root.mkdir(parents=True, exist_ok=True)
            fold_model_path = fold_root / "model.pkl"
            with fold_model_path.open("wb") as handle:
                pickle.dump(fold_model, handle)
            model_sha256 = hashlib.sha256(fold_model_path.read_bytes()).hexdigest()
            available_at = (
                f"{str(fold_evidence['validation_end'])[:4]}-"
                f"{str(fold_evidence['validation_end'])[4:6]}-"
                f"{str(fold_evidence['validation_end'])[6:8]}T15:00:00+08:00"
            )
            fold_artifact = {
                "artifact_type": "plan_model_walk_forward_fold_artifact",
                "binding_contract": "point_in_time_fold_model_v1",
                "model_key": model_key,
                "version": version,
                "fold": fold_no,
                "model_type": model_type,
                "model_path": str(fold_model_path),
                "model_sha256": model_sha256,
                "features": list(
                    trained["metrics"].get("selected_features") or PLAN_MODEL_FEATURES
                ),
                "training_dataset": {
                    "train_start": fold_evidence["train_start"],
                    "train_end": fold_evidence["train_end"],
                    "train_sessions": fold_evidence["train_sessions"],
                    "validation_start": fold_evidence["validation_start"],
                    "validation_end": fold_evidence["validation_end"],
                    "validation_sessions": fold_evidence["validation_sessions"],
                    "test_start": fold_evidence["test_start"],
                    "test_end": fold_evidence["test_end"],
                    "test_sessions": fold_evidence["test_sessions"],
                    "purge_sessions": fold_evidence["purge_sessions"],
                    "embargo_sessions": fold_evidence["embargo_sessions"],
                },
                "available_at": available_at,
                "created_at": datetime.now().isoformat(),
            }
            fold_artifact_path = fold_root / "artifact.json"
            fold_artifact_path.write_text(
                json.dumps(fold_artifact, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            frozen_binding = {
                "binding_contract": "point_in_time_fold_model_v1",
                "fold": fold_no,
                "artifact_path": str(fold_artifact_path),
                "model_sha256": model_sha256,
                "available_at": available_at,
                "train_end": fold_evidence["train_end"],
                "validation_end": fold_evidence["validation_end"],
                "test_start": fold_evidence["test_start"],
                "test_end": fold_evidence["test_end"],
            }
            fold_evidence["point_in_time_artifact"] = frozen_binding
            fold_artifacts.append(frozen_binding)
        artifact = {
            "model_key": model_key,
            "version": version,
            "contract": contract,
            "lightgbm_params": {
                **LIGHTGBM_PARAMS,
                **dict((candidate_spec or {}).get("param_overrides") or {}),
            },
            "features": list(
                trained["metrics"].get("selected_features") or PLAN_MODEL_FEATURES
            ),
            "event_head_feature_subsets": dict(
                trained["metrics"].get("event_head_feature_subsets") or {}
            ),
            "event_head_feature_subset_contract": str(
                trained["metrics"].get("event_head_feature_subset_contract")
                or ""
            ),
            "event_baseline_shrinkage_contract": str(
                trained["metrics"].get("event_baseline_shrinkage_contract") or ""
            ),
            "event_baseline_blend_weights": dict(
                trained["metrics"].get("event_baseline_blend_weights") or {}
            ),
            "event_head_candidate_selection": dict(
                trained["metrics"].get("event_head_candidate_selection") or {}
            ),
            "model_path": str(model_path),
            "model_type": model_type,
            "predict_contract": {
                "input": {
                    feature: "number"
                    for feature in trained["metrics"].get("selected_features") or PLAN_MODEL_FEATURES
                },
                "output": {"score": "number"},
                "scorer": "app.services.plan_model_control.score_plan_model_features",
            },
            "output_contract": MODEL_OUTPUT_CONTRACTS.get(model_key, {}),
            "training_protocol": training_protocol_contract(model_key),
            "training_runtime_protocol": TRAINING_PROTOCOL,
            "coverage": coverage,
            "metrics": metrics,
            "created_at": datetime.now().isoformat(),
            "artifact_type": "plan_model_lightgbm_artifact",
            "walk_forward_fold_artifacts": fold_artifacts,
            "training_dataset": {
                "horizon": dataset["horizon"],
                "rows": dataset["sample_rows"],
                "train_rows": dataset["train_rows"],
                "validation_rows": dataset["validation_rows"],
                "test_rows": dataset["test_rows"],
                "labeled_trade_date_count": dataset["trade_date_count"],
                "train_trade_date_count": dataset["train_trade_date_count"],
                "validation_trade_date_count": dataset["validation_trade_date_count"],
                "test_trade_date_count": dataset["test_trade_date_count"],
                "daily_sample_limit": dataset["daily_sample_limit"],
                "symbol_sampling": dataset.get("symbol_sampling") or {},
                "moneyflow_raw_rows": dataset.get("moneyflow_raw_rows"),
                "capital_observation_count": dataset.get("capital_observation_count"),
                "feature_computation_contract": dataset.get("feature_computation_contract") or {},
                "narrative_label_audit": dataset.get("narrative_label_audit") or {},
                **dataset["dates"],
            },
        }
        artifact_path = root / "artifact.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        terminal_guard = get_model_train_run(run_id) or {}
        if str(terminal_guard.get("status") or "") not in {"running", "queued"}:
            raise RuntimeError(str(terminal_guard.get("error") or "plan_model_training_run_interrupted"))
        model_version_id = create_model_version(
            model_key=model_key,
            version=version,
            status="candidate",
            artifact_path=str(artifact_path),
            config={
                "contract": contract,
                "lightgbm_params": {
                    **LIGHTGBM_PARAMS,
                    **dict((candidate_spec or {}).get("param_overrides") or {}),
                },
                "model_candidate_spec": candidate_spec or {},
                "targeted_training_context": context,
            },
            metrics=metrics,
            validation=validation,
            is_active=False,
        )
        research_trigger = (
            {
                "status": "not_enqueued_research_shadow_only",
                "production_change_forbidden": True,
            }
            if research_shadow_only
            else _enqueue_continuous_research_for_model_version(
                model_key=model_key,
                model_version_id=int(model_version_id),
                version=version,
            )
        )
        model_candidate_trigger = (
            _enqueue_model_candidate_research(
                model_key=model_key,
                model_version_id=int(model_version_id),
                version=version,
            )
            if validation["status"] == "failed"
            and str(context.get("evaluation_stage") or "") != "formal_five_window"
            else {"status": "not_required"}
        )
        strategy_impact_trigger = (
            _enqueue_model_strategy_impact_validation(
                model_key=model_key,
                model_version_id=int(model_version_id),
                version=version,
            )
            if validation["status"] == "pending_strategy_ab"
            and not research_shadow_only
            else {"status": "not_required"}
        )
        autonomy_continuation = _enqueue_plan_model_autonomy_continuation(
            model_key=model_key,
            model_version_id=int(model_version_id),
        )
        update_model_train_run(
            run_id,
            status="completed",
            phase="completed",
            progress=1.0,
            message="计划书专门模型真实LightGBM训练完成，等待上线观察与评审",
            model_version_id=model_version_id,
            metrics={
                "model_key": model_key,
                **metrics,
                "coverage": coverage,
                "continuous_research_trigger": research_trigger,
                "model_candidate_research_trigger": model_candidate_trigger,
                "model_strategy_impact_trigger": strategy_impact_trigger,
                "plan_model_autonomy_continuation": autonomy_continuation,
                "timeline": [
                    *timeline[-19:],
                    {"at": datetime.now().isoformat(), "phase": "completed", "progress": 1.0, "message": "训练完成，候选版本已生成"},
                ],
            },
            finished_at=datetime.now().isoformat(),
        )
        return {
            "ok": True,
            "run_id": run_id,
            "model_version_id": model_version_id,
            "version": version,
            "continuous_research_trigger": research_trigger,
            "model_candidate_research_trigger": model_candidate_trigger,
            "model_strategy_impact_trigger": strategy_impact_trigger,
            "plan_model_autonomy_continuation": autonomy_continuation,
        }
    except Exception as exc:
        current_run = get_model_train_run(run_id) or {}
        if (
            str(current_run.get("status") or "") == "failed"
            and str(current_run.get("error") or "") in {
                "stale_plan_model_training_run",
                "plan_model_training_runtime_budget_exceeded",
            }
        ):
            return {
                "ok": False,
                "run_id": run_id,
                "error": str(current_run.get("error") or exc),
                "automatic_recovery": {"status": "already_enqueued_by_watchdog"},
            }
        previous = current_run.get("metrics") or {}
        context = dict(training_context or {})
        prior_recovery = context.get("failure_recovery") if isinstance(
            context.get("failure_recovery"), dict
        ) else {}
        root_run_id = int(prior_recovery.get("root_run_id") or run_id)
        retry_count = int(prior_recovery.get("retry_count") or 0)
        classification = classify_plan_model_training_failure(exc)
        recovery = {
            "contract": TRAINING_FAILURE_RECOVERY_CONTRACT,
            "status": "detected",
            "classification": classification,
            "root_run_id": root_run_id,
            "failed_run_id": int(run_id),
            "retry_count": retry_count,
            "max_retries": int(
                prior_recovery.get("max_retries")
                or TRAINING_FAILURE_MAX_RETRIES
            ),
            "detected_at": datetime.now().astimezone().isoformat(),
        }
        update_model_train_run(
            run_id,
            status="failed",
            phase="failed",
            progress=1.0,
            message="计划书专门模型训练失败",
            metrics={
                **previous,
                "model_key": model_key,
                "targeted_training_context": context,
                "failure_recovery": recovery,
                "timeline": [
                    *timeline[-19:],
                    {"at": datetime.now().isoformat(), "phase": "failed", "progress": 1.0, "message": f"训练失败：{str(exc)[:160]}"},
                ],
            },
            error=str(exc)[:500],
            finished_at=datetime.now().isoformat(),
        )
        failure_response = {
            "ok": False,
            "run_id": run_id,
            "error": str(exc),
            "failure_recovery": recovery,
        }
    finally:
        _status_cache.update({"at": 0.0, "value": None})
        with _lock:
            _locks.discard(model_key)
    if failure_response is not None:
        recovery = failure_response["failure_recovery"]
        recovery_queue = _enqueue_plan_model_training_recovery(
            model_key=model_key,
            failed_run_id=int(run_id),
            root_run_id=int(recovery["root_run_id"]),
            retry_count=int(recovery["retry_count"]),
            classification=dict(recovery["classification"]),
        )
        failure_response["automatic_recovery"] = recovery_queue
        return failure_response
    return {"ok": False, "run_id": run_id, "error": "training_failed_without_evidence"}


def request_plan_model_training(
    model_key: str,
    *,
    training_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _status_cache.update({"at": 0.0, "value": None})
    _operations_cache.update({"at": 0.0, "value": None})
    if model_key not in PLAN_MODELS:
        return {"ok": False, "error": "unknown_plan_model", "model_key": model_key}
    blocker = _plan_model_candidate_governance_blocker(model_key)
    if blocker:
        return {
            "ok": True,
            "started": False,
            "reason": "candidate_governance_in_progress",
            "model_key": model_key,
            "model_version_id": int(blocker["id"]),
        }
    with _lock:
        if model_key in _locks:
            latest = _latest_run_for_model(model_key)
            return {"ok": True, "started": False, "reason": "training_in_progress", "run_id": (latest or {}).get("id")}
        _locks.add(model_key)
    run_id = create_model_train_run(f"计划书模型训练已创建：{model_key}")
    update_model_train_run(run_id, metrics={
        "model_key": model_key,
        "contract": PLAN_MODELS[model_key]["family"],
        "targeted_training_context": training_context or {},
    })
    threading.Thread(
        target=_run_training_sync,
        args=(model_key, run_id, training_context),
        daemon=True,
        name=f"plan-model-{model_key}",
    ).start()
    return {"ok": True, "started": True, "run_id": run_id, "model_key": model_key, "status": "running"}


def train_all_trainable_plan_models() -> dict[str, Any]:
    status = model_family_status()
    started = []
    skipped = []
    for item in status.get("items") or []:
        if item.get("status") != "trainable":
            skipped.append({"model_key": item.get("model_key"), "reason": f"status_{item.get('status')}"})
            continue
        result = request_plan_model_training(str(item.get("model_key") or ""))
        if result.get("started"):
            started.append(result)
        else:
            skipped.append({"model_key": item.get("model_key"), "reason": result.get("reason") or result.get("error")})
    return {"ok": True, "started": started, "skipped": skipped, "started_count": len(started), "skipped_count": len(skipped)}


def review_all_candidate_plan_models(*, reviewer: str = "operator", comment: str = "批量上线评审通过", waive_live_observation: bool = False) -> dict[str, Any]:
    reviewed = []
    skipped = []
    for item in model_family_status().get("items") or []:
        latest = item.get("latest_version") or {}
        if item.get("status") != "candidate" or not latest.get("id"):
            skipped.append({"model_key": item.get("model_key"), "reason": f"status_{item.get('status')}"})
            continue
        reviewed.append(review_plan_model_version(int(latest["id"]), reviewer=reviewer, decision="approved", comment=comment, waive_live_observation=waive_live_observation))
    return {"ok": True, "reviewed": reviewed, "skipped": skipped, "reviewed_count": len(reviewed), "skipped_count": len(skipped)}


def review_plan_model_version(
    model_version_id: int,
    *,
    reviewer: str,
    decision: str,
    comment: str = "",
    waive_live_observation: bool = False,
) -> dict[str, Any]:
    _status_cache.update({"at": 0.0, "value": None})
    version = get_model_version(model_version_id)
    if not version:
        return {"ok": False, "error": "model_version_not_found", "model_version_id": model_version_id}
    model_key = str(version.get("model_key") or "")
    if model_key not in PLAN_MODELS:
        return {"ok": False, "error": "not_plan_model_version", "model_key": model_key}
    if decision not in {"approved", "rejected"}:
        return {"ok": False, "error": "invalid_review_decision"}
    validation = dict(version.get("validation") or {})
    metrics = dict(version.get("metrics") or {})
    gates = dict((validation.get("production_eligibility") or {}).get("gates") or {})
    gates["current_model_evaluation_contract"] = (
        str(metrics.get("model_evaluation_contract") or "") == MODEL_EVALUATION_CONTRACT
    )
    gates["current_model_specific_contract"] = (
        str(metrics.get("event_impact_contract") or "")
        == EVENT_IMPACT_CONTRACT
        if model_key == "event_impact_assessor_v2"
        else str(((metrics.get("task_metrics") or {}).get("contract") or ""))
        == NARRATIVE_TASK_EVALUATION_CONTRACT
        if model_key == "narrative_transition_multitask_v1"
        else True
    )
    gates["event_structured_feature_contract"] = (
        str(((metrics.get("event_feature_audit") or {}).get("contract") or ""))
        == EVENT_FEATURE_CONTRACT
        if model_key == "event_impact_assessor_v2"
        else True
    )
    gates["controlled_strategy_ab"] = controlled_strategy_ab_passed_current_contract(
        model_key,
        metrics.get("strategy_impact_ab"),
    )
    gates["human_review"] = decision == "approved"
    gates["live_observation_20_sessions"] = bool(
        gates.get("live_observation_20_sessions")
    )
    gates["simulation_only_live_observation_waived"] = False
    review = {
        "status": decision,
        "reviewer": reviewer.strip() or "operator",
        "comment": comment.strip(),
        "waive_live_observation_requested": bool(waive_live_observation),
        "waive_live_observation": False,
        "waiver_policy": "forbidden",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "policy": "plan_model_release_review_v2",
    }
    blocked = []
    if decision != "approved":
        blocked.append("human_review_rejected")
    if waive_live_observation:
        blocked.append("live_observation_waiver_forbidden")
    for key, passed in gates.items():
        if key == "simulation_only_live_observation_waived":
            continue
        if not bool(passed):
            blocked.append(key)
    eligible = decision == "approved" and not blocked
    validation["review"] = review
    validation["production_eligible"] = eligible
    validation["production_eligibility"] = {
        "eligible": eligible,
        "blocked_reasons": blocked,
        "gates": gates,
        "review": review,
    }
    release_audit = audit_plan_model_production_eligibility(
        {**version, "validation": validation}
    )
    if eligible and not release_audit.get("passed"):
        blocked = sorted(
            set(
                [
                    *blocked,
                    *[
                        f"current_production_gate:{reason}"
                        for reason in release_audit.get("blocked_reasons") or []
                    ],
                ]
            )
        )
        eligible = False
        validation["production_eligible"] = False
        validation["production_eligibility"].update(
            {
                "eligible": False,
                "blocked_reasons": blocked,
            }
        )
    validation["production_eligibility"]["current_gate_audit"] = release_audit
    update_model_version_validation(model_version_id, validation=validation)
    if eligible:
        set_active_model_version(model_version_id)
    return {
        "ok": True,
        "model_version_id": model_version_id,
        "model_key": model_key,
        "version": version.get("version"),
        "review": review,
        "production_eligible": eligible,
        "production_eligibility": validation["production_eligibility"],
    }


def artifact_file_exists(path: str) -> bool:
    return bool(path) and Path(path).exists()
