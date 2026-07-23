from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.store import get_active_model_version, get_model_version, set_active_model_version, update_model_version_validation
from app.services.model_registry import compare_champion_challenger, evaluate_model_drift, get_model_constitution, get_model_status, get_train_run_status, model_artifact_status
from app.services.model_governance_scheduler import (
    auto_tune_models_once,
    bootstrap_candidate_eligible,
    mark_bootstrap_promotion,
    request_model_training,
)
from app.services.quant_risk import bocpd_change_probability, garch11_forecast, historical_cvar, hsmm_regime, kalman_level, ledoit_wolf_covariance
from app.services.training_protocol import training_protocol_contract, validate_feature_frame_contract, validate_training_run_manifest
from app.services.model_evidence_contract import evidence_binding_registry, model_output_contracts, normalize_model_output
from app.services.two_group_optimizer import two_group_cvar_optimizer
from app.services.market_context import (
    live_market_context,
    market_context_history,
    market_state_model_governance,
    materialize_market_training_history,
    rebuild_market_context_history,
    score_market_sentiment,
    score_theme_narratives,
)
from app.services.strategy_gate import evaluate_strategy_gate
from app.services.advanced_controls import black_litterman_weights, evaluate_slo, multi_period_oos_report
from app.services.performance_reports import list_performance_reports, validate_performance_reports, build_multi_period_validation, materialize_formal_performance_reports
from app.services.production_strategy import production_strategy_status
from app.services.walk_forward_validation import WalkForwardConfig, run_purged_walk_forward_validation
from app.services.news_theme_cluster import cluster_news_themes
from app.services.analyst_consensus import build_analyst_consensus, batch_analyst_consensus
from app.services.acceptance_evidence import (
    evaluate_and_record_slo,
    list_backend_acceptance_runs,
    list_slo_evaluations,
    run_and_record_backend_acceptance,
)
from app.services.auto_pipeline import run_daily_maintenance
from app.services.defensive_assets import defensive_allocation_for_state, list_defensive_assets, materialize_defensive_asset_history
from app.services.hsmm_shadow import train_hsmm_shadow
from app.services.backend_acceptance import backend_acceptance_matrix
from app.services.strategy_workbench import build_strategy_workbench
from app.services.strategy_lab import build_strategy_lab
from app.services.system_autonomy import latest_owner_summary
from app.services.evolution_orchestrator import (
    cancel_evolution_task,
    create_evolution_task,
    evolution_schedule_status,
    get_evolution_task,
    list_evolution_tasks,
)
from app.services.plan_model_control import model_family_status, request_plan_model_training, review_plan_model_version, train_all_trainable_plan_models, review_all_candidate_plan_models, score_plan_model_features
from app.services.plan_model_auto_optimizer import build_auto_optimization_status, run_auto_optimization_once
from app.services.strategy_controller import (
    create_strategy_policy_candidate,
    evaluate_strategy_policy_version,
    review_strategy_policy_version,
    rollback_strategy_policy_version,
    run_strategy_policy_auto_optimization,
    strategy_policy_replay_preview,
    strategy_controller_status,
)

router = APIRouter()


class ModelTrainRequest(BaseModel):
    start_date: str | None = Field(None, description="YYYY-MM-DD，可选")
    end_date: str | None = Field(None, description="YYYY-MM-DD，可选")
    exchange_group: str = Field("HS", description="ALL|HS|CYB|BJ")


class ModelDiagnosticsRequest(BaseModel):
    returns: list[float] = Field(default_factory=list, max_length=5000)


class MarketContextRequest(BaseModel):
    breadth: float = Field(0.5, ge=0, le=1)
    average_return: float = 0.0
    volume_ratio: float = 1.0
    limit_up_ratio: float = 0.0
    limit_down_ratio: float = 0.0
    northbound_flow_z: float = 0.0
    leader_persistence: float = 0.0
    price_fundamental_divergence: float = 0.0
    crowding: float = Field(0.0, ge=0, le=1)
    themes: list[dict] = Field(default_factory=list, max_length=200)


class PortfolioRiskRequest(BaseModel):
    returns_by_symbol: dict[str, list[float]] = Field(default_factory=dict)
    max_single: float = Field(0.25, gt=0, le=1)
    expected_returns: dict[str, float] = Field(default_factory=dict)
    buckets: dict[str, str] = Field(default_factory=dict)
    current_weights: dict[str, float] = Field(default_factory=dict)
    trading_costs: dict[str, float] = Field(default_factory=dict)
    crowding: dict[str, float] = Field(default_factory=dict)


class ModelDriftRequest(BaseModel):
    expected: dict[str, list[float]] = Field(default_factory=dict)
    actual: dict[str, list[float]] = Field(default_factory=dict)


class StrategyGateRequest(BaseModel):
    research_score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    data_quality: float = Field(ge=0, le=100)
    risk_eligible: bool = True
    market_state: str = "unknown"
    sentiment_score: float = Field(50, ge=0, le=100)
    expectation_score: float = Field(50, ge=0, le=100)
    valuation_score: float = Field(50, ge=0, le=100)
    theme_score: float = Field(0, ge=0, le=100)
    theme_verified: bool = False
    market_profile_multiplier: float = Field(1.0, ge=.85, le=1.0)


class BlackLittermanRequest(BaseModel):
    prior_weights: dict[str, float] = Field(default_factory=dict)
    views: dict[str, float] = Field(default_factory=dict)
    confidences: dict[str, float] = Field(default_factory=dict)
    max_weight: float = Field(.25, gt=0, le=1)


class SLORequest(BaseModel):
    samples: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)
    freshness_seconds: float = Field(86400, gt=0)
    success_target: float = Field(.99, gt=0, le=1)
    scope: str = Field("model-strategy", max_length=128)
    persist: bool = True


class OOSReportRequest(BaseModel):
    periods: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class PurgedWalkForwardRequest(BaseModel):
    strategy_id: int | None = Field(None, ge=1)
    start_date: str | None = None
    end_date: str | None = None
    train_sessions: int = Field(756, ge=60, le=3000)
    validation_sessions: int = Field(126, ge=20, le=1000)
    test_sessions: int = Field(126, ge=20, le=1000)
    purge_sessions: int = Field(20, ge=1, le=252)
    embargo_sessions: int = Field(5, ge=1, le=126)
    step_sessions: int = Field(126, ge=1, le=1000)
    minimum_windows: int = Field(5, ge=1, le=20)
    max_windows: int | None = Field(None, ge=1, le=20)
    execute: bool = True


class EvolutionTaskRequest(BaseModel):
    max_windows: int = Field(5, ge=1, le=20)
    max_candidates: int = Field(6, ge=1, le=20)
    force: bool = False


class HsmmReviewRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=120)
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = Field("", max_length=2000)


class PlanModelTrainRequest(BaseModel):
    model_key: str = Field(..., min_length=3, max_length=128)


class PlanModelReviewRequest(BaseModel):
    reviewer: str = Field("operator", min_length=1, max_length=120)
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = Field("", max_length=2000)
    waive_live_observation: bool = False


class PlanModelScoreRequest(BaseModel):
    model_key: str = Field(..., min_length=3, max_length=128)
    features: dict[str, Any] = Field(default_factory=dict)


class FeatureFrameContractRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)
    feature_names: list[str] = Field(default_factory=list, max_length=500)
    require_source_time: bool = True


class TrainingManifestRequest(BaseModel):
    manifest: dict[str, Any] = Field(default_factory=dict)


class NormalizeModelOutputRequest(BaseModel):
    model_key: str = Field(..., min_length=3, max_length=128)
    output: dict[str, Any] = Field(default_factory=dict)


class PlanModelAutoOptimizationRequest(BaseModel):
    force: bool = False


class StrategyPolicyCandidateRequest(BaseModel):
    policy_patch: dict[str, Any] = Field(default_factory=dict)
    comment: str = Field("", max_length=2000)


class StrategyPolicyReviewRequest(BaseModel):
    reviewer: str = Field("operator", min_length=1, max_length=120)
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = Field("", max_length=2000)


class StrategyPolicyRollbackRequest(BaseModel):
    reviewer: str = Field("operator", min_length=1, max_length=120)
    comment: str = Field("", max_length=2000)


class StrategyPolicyReplayRequest(BaseModel):
    version_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    limit: int = Field(120, ge=1, le=1000)


def _passed_cost_after_metrics(metrics: dict[str, Any]) -> bool:
    net_lift = float(metrics.get("net_lift", metrics.get("lift", 0)) or 0)
    walk_forward = float(metrics.get("walk_forward_lift", metrics.get("net_lift", metrics.get("lift", 0))) or 0)
    worst_window = float(metrics.get("worst_window_net_lift", walk_forward) or 0)
    return net_lift > 0 and walk_forward > 0 and worst_window > 0


@router.get("/model-strategy/status")
def model_strategy_status() -> dict:
    return get_model_status()


@router.get("/model-strategy/plan-models")
def model_strategy_plan_models() -> dict:
    return model_family_status()


@router.post("/model-strategy/plan-models/train")
def model_strategy_train_plan_model(req: PlanModelTrainRequest) -> dict:
    result = request_plan_model_training(req.model_key)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/model-strategy/plan-models/train-all")
def model_strategy_train_all_plan_models() -> dict:
    return train_all_trainable_plan_models()


@router.get("/model-strategy/auto-optimization")
def model_strategy_auto_optimization() -> dict:
    return build_auto_optimization_status()


@router.post("/model-strategy/auto-optimization/run")
def model_strategy_run_auto_optimization(req: PlanModelAutoOptimizationRequest) -> dict:
    return run_auto_optimization_once(force=req.force)


@router.get("/model-strategy/strategy-controller")
def model_strategy_strategy_controller() -> dict:
    return strategy_controller_status()


@router.post("/model-strategy/strategy-controller/candidates")
def model_strategy_create_strategy_policy_candidate(req: StrategyPolicyCandidateRequest) -> dict:
    return create_strategy_policy_candidate(req.policy_patch, comment=req.comment)


@router.post("/model-strategy/strategy-controller/auto-optimize")
def model_strategy_auto_optimize_strategy_policy(req: PlanModelAutoOptimizationRequest) -> dict:
    return run_strategy_policy_auto_optimization(force=req.force)


@router.post("/model-strategy/strategy-controller/replay-preview")
def model_strategy_replay_strategy_policy(req: StrategyPolicyReplayRequest) -> dict:
    result = strategy_policy_replay_preview(
        version_id=req.version_id,
        start_date=req.start_date,
        end_date=req.end_date,
        limit=req.limit,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/model-strategy/strategy-controller/review/{version_id}")
def model_strategy_review_strategy_policy(version_id: int, req: StrategyPolicyReviewRequest) -> dict:
    result = review_strategy_policy_version(
        version_id,
        decision=req.decision,
        reviewer=req.reviewer,
        comment=req.comment,
    )
    if not result.get("ok"):
        status_code = 409 if result.get("error") == "strategy_policy_validation_failed" else 404
        raise HTTPException(status_code=status_code, detail=result)
    return result


@router.post("/model-strategy/strategy-controller/rollback/{version_id}")
def model_strategy_rollback_strategy_policy(version_id: int, req: StrategyPolicyRollbackRequest) -> dict:
    result = rollback_strategy_policy_version(version_id, reviewer=req.reviewer, comment=req.comment)
    if not result.get("ok"):
        raise HTTPException(status_code=409 if result.get("error") == "strategy_policy_not_rollback_eligible" else 404, detail=result)
    return result


@router.post("/model-strategy/strategy-controller/validate/{version_id}")
def model_strategy_validate_strategy_policy(version_id: int) -> dict:
    result = evaluate_strategy_policy_version(version_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/model-strategy/plan-models/review/{model_version_id}")
def model_strategy_review_plan_model(model_version_id: int, req: PlanModelReviewRequest) -> dict:
    result = review_plan_model_version(
        model_version_id,
        reviewer=req.reviewer,
        decision=req.decision,
        comment=req.comment,
        waive_live_observation=req.waive_live_observation,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/model-strategy/plan-models/review-candidates")
def model_strategy_review_candidate_plan_models(req: PlanModelReviewRequest) -> dict:
    if req.decision != "approved":
        raise HTTPException(status_code=400, detail="批量评审只支持approved；驳回请逐个处理")
    return review_all_candidate_plan_models(reviewer=req.reviewer, comment=req.comment, waive_live_observation=req.waive_live_observation)


@router.post("/model-strategy/plan-models/score")
def model_strategy_score_plan_model(req: PlanModelScoreRequest) -> dict:
    return score_plan_model_features(req.model_key, req.features)


@router.get("/model-strategy/training-protocol")
def model_strategy_training_protocol(model_key: str | None = None) -> dict:
    return training_protocol_contract(model_key)


@router.post("/model-strategy/training-protocol/validate-manifest")
def model_strategy_validate_training_manifest(req: TrainingManifestRequest) -> dict:
    return validate_training_run_manifest(req.manifest)


@router.post("/model-strategy/training-protocol/validate-features")
def model_strategy_validate_feature_contract(req: FeatureFrameContractRequest) -> dict:
    return validate_feature_frame_contract(
        req.rows,
        feature_names=req.feature_names,
        require_source_time=req.require_source_time,
    )


@router.get("/model-strategy/model-output-contracts")
def model_strategy_model_output_contracts() -> dict:
    return model_output_contracts()


@router.post("/model-strategy/model-output-contracts/normalize")
def model_strategy_normalize_model_output(req: NormalizeModelOutputRequest) -> dict:
    return normalize_model_output(req.model_key, req.output)


@router.get("/model-strategy/evidence-bindings")
def model_strategy_evidence_bindings() -> dict:
    return evidence_binding_registry()


@router.get("/model-strategy/backend-acceptance")
def model_strategy_backend_acceptance() -> dict:
    return backend_acceptance_matrix()


@router.post("/model-strategy/performance-reports/materialize-formal")
def model_strategy_materialize_formal_performance_reports() -> dict:
    return materialize_formal_performance_reports()


@router.post("/model-strategy/backend-acceptance/run")
def model_strategy_backend_acceptance_run(suite: str = "plan-book-backend") -> dict:
    return run_and_record_backend_acceptance(suite=suite)


@router.get("/model-strategy/backend-acceptance/runs")
def model_strategy_backend_acceptance_runs(suite: str | None = None, limit: int = 20) -> dict:
    return list_backend_acceptance_runs(suite=suite, limit=limit)


@router.get("/model-strategy/workbench")
def model_strategy_workbench(limit: int = 10, include_details: bool = False) -> dict:
    return build_strategy_workbench(limit=limit, include_details=include_details)


@router.get("/model-strategy/constitution")
def model_strategy_constitution() -> dict:
    """Expose the fixed plan model stack and implementation gaps."""
    return get_model_constitution()


@router.post("/model-strategy/diagnostics")
def model_strategy_diagnostics(req: ModelDiagnosticsRequest) -> dict:
    """Run the fixed risk/regime components on an explicit return series."""
    values = [float(x) for x in req.returns]
    return {
        "observations": len(values),
        "regime": {"hsmm": hsmm_regime(values), "bocpd": bocpd_change_probability(values)},
        "dynamic_factor": kalman_level(values),
        "volatility": garch11_forecast(values),
        "tail_risk": historical_cvar(values),
    }


@router.post("/model-strategy/market-context")
def model_strategy_market_context(req: MarketContextRequest) -> dict:
    """Score sentiment and themes from normalized, provenance-tracked inputs."""
    return {
        "sentiment": score_market_sentiment(
            breadth=req.breadth,
            average_return=req.average_return,
            volume_ratio=req.volume_ratio,
            limit_up_ratio=req.limit_up_ratio,
            limit_down_ratio=req.limit_down_ratio,
            northbound_flow_z=req.northbound_flow_z,
            leader_persistence=req.leader_persistence,
            price_fundamental_divergence=req.price_fundamental_divergence,
            crowding=req.crowding,
        ),
        "themes": score_theme_narratives(req.themes),
    }


@router.get("/model-strategy/market-context/live")
def model_strategy_live_market_context() -> dict:
    return live_market_context()


@router.get("/model-strategy/market-context/history")
def model_strategy_market_context_history(limit: int = 120) -> dict:
    return market_context_history(limit=max(1, min(500, limit)))


@router.post("/model-strategy/market-context/rebuild-history")
def model_strategy_rebuild_market_context_history(limit: int = 120) -> dict:
    return rebuild_market_context_history(limit=limit)


@router.get("/model-strategy/market-context/governance")
def model_strategy_market_context_governance() -> dict:
    """Report the production regime model and the evidence for promotion gates."""
    return market_state_model_governance()


@router.post("/model-strategy/market-context/materialize-training-history")
def model_strategy_materialize_market_training_history() -> dict:
    """Refresh the point-in-time 6-10 year index feature layer used by regime models."""
    return materialize_market_training_history()


@router.get("/model-strategy/defensive-assets")
def model_strategy_defensive_assets(as_of_date: str | None = None) -> dict:
    return list_defensive_assets(as_of_date=as_of_date)


@router.post("/model-strategy/defensive-assets/materialize")
def model_strategy_materialize_defensive_assets(start_date: str | None = None, end_date: str | None = None) -> dict:
    return materialize_defensive_asset_history(start_date=start_date, end_date=end_date)


@router.post("/model-strategy/market-context/hsmm/train-shadow")
def model_strategy_train_hsmm_shadow() -> dict:
    """Fit and OOS-validate the fixed four-state HSMM without promoting it."""
    return train_hsmm_shadow()


@router.post("/model-strategy/market-context/hsmm/review/{model_version_id}")
def model_strategy_review_hsmm_shadow(model_version_id: int, req: HsmmReviewRequest) -> dict:
    version = get_model_version(model_version_id)
    if not version or str(version.get("model_key") or "") != "market_regime":
        raise HTTPException(status_code=404, detail="HSMM影子版本不存在")
    validation = dict(version.get("validation") or {})
    if str(validation.get("status") or "") != "passed":
        raise HTTPException(status_code=409, detail="只有通过OOS验证的HSMM影子版本才能进入上线评审")
    governance = market_state_model_governance()
    hsmm = governance.get("hsmm") or {}
    if str(hsmm.get("latest_shadow_version") or "") != str(version.get("version") or ""):
        raise HTTPException(status_code=409, detail="仅允许评审最新HSMM影子版本")
    if str(hsmm.get("status") or "") not in {"shadow_live_observation_review_ready", "shadow_review_rejected", "shadow_promoted_ready"}:
        raise HTTPException(status_code=409, detail="HSMM影子观察期尚未达标，暂不能进入上线评审")
    review_payload = {
        "status": req.decision,
        "reviewer": req.reviewer.strip(),
        "comment": req.comment.strip(),
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }
    validation["review"] = review_payload
    validation["production_eligible"] = req.decision == "approved"
    update_model_version_validation(model_version_id, validation=validation)
    return {
        "ok": True,
        "model_version_id": model_version_id,
        "version": version.get("version"),
        "review": review_payload,
        "production_eligible": validation["production_eligible"],
        "governance": market_state_model_governance(),
    }


@router.get("/model-strategy/defensive-assets/allocation")
def model_strategy_defensive_allocation(market_state: str, as_of_date: str) -> dict:
    return defensive_allocation_for_state(market_state, as_of_date=as_of_date)


@router.post("/model-strategy/gate")
def model_strategy_gate(req: StrategyGateRequest) -> dict:
    return evaluate_strategy_gate(**req.model_dump())


@router.post("/model-strategy/portfolio-risk")
def model_strategy_portfolio_risk(req: PortfolioRiskRequest) -> dict:
    """Expose the exact portfolio optimizer and explicit fallback decision."""
    observations = {str(k): [float(x) for x in v] for k, v in req.returns_by_symbol.items()}
    optimizer_result = two_group_cvar_optimizer(
        observations,
        expected_returns=req.expected_returns,
        buckets=req.buckets,
        current_weights=req.current_weights,
        trading_costs=req.trading_costs,
        crowding=req.crowding,
    )
    covariance = ledoit_wolf_covariance(observations)
    weights = optimizer_result.get("weights") or {}
    optimizer = str(optimizer_result.get("optimizer") or "unavailable")
    return {
        "optimizer": optimizer,
        "weights": weights,
        "covariance": covariance,
        "weight_sum": round(sum(weights.values()), 8),
        "portfolio_optimizer": optimizer_result,
        "two_group_optimizer": optimizer_result,
        "deprecated_inputs": {"max_single": "策略桶固定上限已取消；账户与订单安全约束由执行层处理"},
    }


@router.post("/model-strategy/black-litterman")
@router.get("/model-strategy/performance-reports")
def model_strategy_performance_reports(period_type: str | None = None, limit: int = 12) -> dict:
    return list_performance_reports(period_type=period_type, limit=limit)


@router.get("/model-strategy/performance-reports/validate")
def model_strategy_validate_reports() -> dict:
    return validate_performance_reports()


@router.get("/model-strategy/validation/multi-period")
def model_strategy_multi_period_validation() -> dict:
    return build_multi_period_validation()


@router.get("/model-strategy/production-strategy")
def model_strategy_production_strategy() -> dict:
    return production_strategy_status()



@router.post("/model-strategy/themes/cluster-news")
def model_strategy_cluster_news() -> dict:
    return cluster_news_themes()


@router.get("/model-strategy/analyst-consensus/{ts_code}")
def model_strategy_analyst_consensus(ts_code: str) -> dict:
    return build_analyst_consensus(ts_code)


@router.get("/model-strategy/analyst-consensus/batch")
def model_strategy_analyst_consensus_batch(limit: int = 100) -> dict:
    return batch_analyst_consensus(limit=limit)



@router.post("/model-strategy/daily-maintenance")
def model_strategy_daily_maintenance() -> dict:
    return run_daily_maintenance()



def model_strategy_black_litterman(req: BlackLittermanRequest) -> dict:
    return black_litterman_weights(req.prior_weights, req.views, req.confidences, max_weight=req.max_weight)


@router.post("/model-strategy/slo/evaluate")
def model_strategy_slo(req: SLORequest) -> dict:
    if req.persist:
        return evaluate_and_record_slo(
            req.samples,
            scope=req.scope,
            freshness_seconds=req.freshness_seconds,
            success_target=req.success_target,
        )
    return evaluate_slo(req.samples, freshness_seconds=req.freshness_seconds, success_target=req.success_target)


@router.get("/model-strategy/slo/history")
def model_strategy_slo_history(scope: str | None = None, limit: int = 100) -> dict:
    return list_slo_evaluations(scope=scope, limit=limit)


@router.post("/model-strategy/oos/report")
def model_strategy_oos_report(req: OOSReportRequest) -> dict:
    return multi_period_oos_report(req.periods)


@router.post("/model-strategy/validation/purged-walk-forward")
def model_strategy_purged_walk_forward(req: PurgedWalkForwardRequest) -> dict:
    config = WalkForwardConfig(
        train_sessions=req.train_sessions, validation_sessions=req.validation_sessions,
        test_sessions=req.test_sessions, purge_sessions=req.purge_sessions,
        embargo_sessions=req.embargo_sessions, step_sessions=req.step_sessions,
        minimum_windows=req.minimum_windows,
    )
    try:
        return run_purged_walk_forward_validation(
            start_date=req.start_date, end_date=req.end_date, config=config,
            max_windows=req.max_windows, strategy_id=req.strategy_id, execute=req.execute,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/model-strategy/strategy-lab")
def model_strategy_lab() -> dict:
    """Read-only Champion/Challenger performance view for the investor UI."""
    return build_strategy_lab()


@router.get("/model-strategy/evolution/tasks")
def model_strategy_evolution_tasks(limit: int = 20) -> dict:
    return {
        "ok": True,
        "items": list_evolution_tasks(limit=limit),
        "schedule": evolution_schedule_status(),
    }


@router.get("/model-strategy/evolution/schedule")
def model_strategy_evolution_schedule() -> dict:
    return {"ok": True, **evolution_schedule_status()}


@router.get("/model-strategy/evolution/owner-summary")
def model_strategy_owner_summary() -> dict:
    return latest_owner_summary()


@router.get("/model-strategy/evolution/ledger-audit")
def model_strategy_ledger_audit() -> dict:
    from app.services.simulation_ledger_audit import audit_simulation_ledger
    return audit_simulation_ledger(persist=False)


@router.get("/model-strategy/evolution/tasks/{task_id}")
def model_strategy_evolution_task(task_id: int) -> dict:
    task = get_evolution_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="evolution task not found")
    return task


@router.post("/model-strategy/evolution/tasks")
def model_strategy_create_evolution_task(req: EvolutionTaskRequest) -> dict:
    return create_evolution_task(
        task_type="strategy_evolution_cycle",
        payload={
            "max_windows": req.max_windows,
            "max_candidates": req.max_candidates,
            "trigger": "api",
        },
        dedupe_key=f"strategy_evolution_cycle:{datetime.now().date().isoformat()}",
        force=req.force,
    )


@router.post("/model-strategy/evolution/tasks/{task_id}/cancel")
def model_strategy_cancel_evolution_task(task_id: int) -> dict:
    task = cancel_evolution_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="evolution task not found")
    return {"ok": True, "task": task}


@router.post("/model-strategy/drift")
def model_strategy_drift(req: ModelDriftRequest) -> dict:
    return evaluate_model_drift(req.expected, req.actual)


@router.post("/model-strategy/promote/{model_version_id}")
def model_strategy_promote(model_version_id: int) -> dict:
    """Promote only a candidate that passed both validation gates."""
    version = get_model_version(model_version_id)
    if not version:
        raise HTTPException(status_code=404, detail="模型版本不存在")
    validation = version.get("validation") or {}
    metrics = version.get("metrics") or {}
    champion = get_active_model_version("main")
    bootstrap = champion is None and str(version.get("model_key") or "") == "main"
    artifact = model_artifact_status(version)
    if not artifact.get("ok"):
        raise HTTPException(status_code=409, detail={"code": "model_artifact_unavailable", **artifact})
    if not bool(validation.get("production_eligible")) and not (bootstrap and bootstrap_candidate_eligible(version)):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_not_production_eligible",
                "production_eligibility": validation.get("production_eligibility") or {},
            },
        )
    if str(validation.get("status")) != "passed" or not _passed_cost_after_metrics(metrics):
        raise HTTPException(status_code=409, detail="模型未同时通过费用后测试集、walk-forward 与最差窗口晋升门槛")
    from app.services.review_learning import model_feedback_gate
    feedback = model_feedback_gate(str(version.get("version") or ""))
    if not bootstrap and not feedback.get("passed"):
        raise HTTPException(status_code=409, detail={"code": "review_feedback_gate_blocked", "review_feedback": feedback})
    if bootstrap:
        mark_bootstrap_promotion(version)
    set_active_model_version(model_version_id)
    return {
        "ok": True, "model_version_id": model_version_id, "status": "active",
        "review_feedback": feedback,
        "promotion_policy": "bootstrap_offline_gates" if bootstrap else "challenger_full_gates",
    }


@router.post("/model-strategy/rollback/{model_version_id}")
def model_strategy_rollback(model_version_id: int) -> dict:
    """Return to a previously validated version without bypassing promotion gates."""
    target = get_model_version(model_version_id)
    if not target:
        raise HTTPException(status_code=404, detail="模型版本不存在")
    validation = target.get("validation") or {}
    metrics = target.get("metrics") or {}
    artifact = model_artifact_status(target)
    if not artifact.get("ok"):
        raise HTTPException(status_code=409, detail={"code": "model_artifact_unavailable", **artifact})
    if not bool(validation.get("production_eligible")):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_not_production_eligible",
                "production_eligibility": validation.get("production_eligibility") or {},
            },
        )
    if str(validation.get("status")) != "passed" or not _passed_cost_after_metrics(metrics):
        raise HTTPException(status_code=409, detail="只能回退到费用后测试集、walk-forward 与最差窗口均通过的模型版本")
    previous = get_active_model_version(str(target.get("model_key") or "main"))
    set_active_model_version(model_version_id)
    return {"ok": True, "status": "rolled_back", "previous_model_version_id": previous.get("id") if previous else None, "model_version_id": model_version_id}


@router.post("/model-strategy/compare")
def model_strategy_compare(champion_id: int, challenger_id: int) -> dict:
    champion = get_model_version(champion_id)
    challenger = get_model_version(challenger_id)
    if not champion or not challenger:
        raise HTTPException(status_code=404, detail="Champion或Challenger模型不存在")
    return compare_champion_challenger(champion, challenger)


@router.post("/model-strategy/auto-tune")
def model_strategy_auto_tune() -> dict:
    """Evaluate candidate versions and promote at most one safe challenger."""
    return auto_tune_models_once()


@router.get("/model-strategy/train-runs/{run_id}")
def model_strategy_train_run(run_id: int) -> dict:
    row = get_train_run_status(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="训练任务不存在")
    return row


@router.post("/model-strategy/train")
def model_strategy_train(req: ModelTrainRequest) -> dict:
    return request_model_training(
        start_date=req.start_date,
        end_date=req.end_date,
        exchange_group=req.exchange_group,
        force=True,
    )
