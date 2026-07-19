from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.store import get_active_model_version, get_model_version, set_active_model_version, update_model_version_validation
from app.services.model_registry import compare_champion_challenger, evaluate_model_drift, get_model_constitution, get_model_status, get_train_run_status, model_artifact_status
from app.services.model_governance_scheduler import auto_tune_models_once, request_model_training
from app.services.quant_risk import bocpd_change_probability, constrained_cvar_weights, garch11_forecast, historical_cvar, hrp_fallback_weights, hsmm_regime, kalman_level, ledoit_wolf_covariance
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
from app.services.performance_reports import list_performance_reports, validate_performance_reports, build_multi_period_validation
from app.services.news_theme_cluster import cluster_news_themes
from app.services.analyst_consensus import build_analyst_consensus, batch_analyst_consensus
from app.services.auto_pipeline import run_daily_maintenance
from app.services.defensive_assets import defensive_allocation_for_state, list_defensive_assets, materialize_defensive_asset_history
from app.services.hsmm_shadow import train_hsmm_shadow
from app.services.backend_acceptance import backend_acceptance_matrix
from app.services.strategy_workbench import build_strategy_workbench

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


class OOSReportRequest(BaseModel):
    periods: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class HsmmReviewRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=120)
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = Field("", max_length=2000)


@router.get("/model-strategy/status")
def model_strategy_status() -> dict:
    return get_model_status()


@router.get("/model-strategy/backend-acceptance")
def model_strategy_backend_acceptance() -> dict:
    return backend_acceptance_matrix()


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
        "reviewed_at": datetime.utcnow().isoformat(timespec="seconds"),
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
    covariance = ledoit_wolf_covariance(observations)
    weights = constrained_cvar_weights(observations, max_single=req.max_single)
    optimizer = "ledoit_wolf_shrinkage_plus_constrained_cvar_v1"
    if not weights:
        weights = hrp_fallback_weights(observations, max_single=req.max_single)
        optimizer = "hrp_inverse_volatility_fallback_v1"
    return {"optimizer": optimizer, "weights": weights, "covariance": covariance, "weight_sum": round(sum(weights.values()), 8)}


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
    return evaluate_slo(req.samples, freshness_seconds=req.freshness_seconds, success_target=req.success_target)


@router.post("/model-strategy/oos/report")
def model_strategy_oos_report(req: OOSReportRequest) -> dict:
    return multi_period_oos_report(req.periods)


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
    artifact = model_artifact_status(version)
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
    if str(validation.get("status")) != "passed" or float(metrics.get("lift", 0) or 0) <= 0 or float(metrics.get("walk_forward_lift", 0) or 0) <= 0:
        raise HTTPException(status_code=409, detail="模型未同时通过 holdout 与 walk-forward 晋升门槛")
    from app.services.review_learning import model_feedback_gate
    feedback = model_feedback_gate(str(version.get("version") or ""))
    if not feedback.get("passed"):
        raise HTTPException(status_code=409, detail={"code": "review_feedback_gate_blocked", "review_feedback": feedback})
    set_active_model_version(model_version_id)
    return {"ok": True, "model_version_id": model_version_id, "status": "active", "review_feedback": feedback}


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
    if str(validation.get("status")) != "passed" or float(metrics.get("lift", 0) or 0) <= 0 or float(metrics.get("walk_forward_lift", 0) or 0) <= 0:
        raise HTTPException(status_code=409, detail="只能回退到已通过验证的模型版本")
    previous = get_active_model_version("main")
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
