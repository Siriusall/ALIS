from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.store import create_model_train_run, get_active_model_version, get_model_version, list_model_versions, set_active_model_version
from app.services.model_registry import compare_champion_challenger, evaluate_model_drift, get_model_constitution, get_model_status, get_train_run_status
from app.services.model_training import run_model_training
from app.services.quant_risk import bocpd_change_probability, constrained_cvar_weights, garch11_forecast, historical_cvar, hrp_fallback_weights, hsmm_regime, kalman_level, ledoit_wolf_covariance
from app.services.market_context import live_market_context, score_market_sentiment, score_theme_narratives
from app.services.strategy_gate import evaluate_strategy_gate

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


@router.get("/model-strategy/status")
def model_strategy_status() -> dict:
    return get_model_status()


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
    if str(validation.get("status")) != "passed" or float(metrics.get("lift", 0) or 0) <= 0 or float(metrics.get("walk_forward_lift", 0) or 0) <= 0:
        raise HTTPException(status_code=409, detail="模型未同时通过 holdout 与 walk-forward 晋升门槛")
    set_active_model_version(model_version_id)
    return {"ok": True, "model_version_id": model_version_id, "status": "active"}


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
    champion = get_active_model_version("main")
    candidates = [x for x in list_model_versions("main", limit=50) if not x.get("is_active") and str(x.get("status")) in {"candidate", "watch"}]
    if not champion:
        return {"decision": "no_champion", "evaluated": 0}
    evaluations = [compare_champion_challenger(champion, candidate) for candidate in candidates]
    winner = next((x for x in evaluations if x.get("decision") == "promote_challenger"), None)
    if winner:
        set_active_model_version(int(winner["challenger_id"]))
        return {"decision": "promoted", "evaluated": len(evaluations), "winner": winner, "champion_id": champion.get("id")}
    return {"decision": "keep_champion", "evaluated": len(evaluations), "evaluations": evaluations, "champion_id": champion.get("id")}


@router.get("/model-strategy/train-runs/{run_id}")
def model_strategy_train_run(run_id: int) -> dict:
    row = get_train_run_status(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="训练任务不存在")
    return row


@router.post("/model-strategy/train")
def model_strategy_train(req: ModelTrainRequest) -> dict:
    run_id = create_model_train_run("训练任务已创建")

    def _worker() -> None:
        run_model_training(
            run_id=run_id,
            start_date=req.start_date,
            end_date=req.end_date,
            exchange_group=req.exchange_group,
        )

    threading.Thread(target=_worker, daemon=True, name=f"model-train-{run_id}").start()
    return {"ok": True, "run_id": run_id, "status": "running"}
