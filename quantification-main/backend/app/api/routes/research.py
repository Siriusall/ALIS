from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.research_engine import (
    CONTRACTS,
    capability_registry,
    build_stock_research,
    build_expectation_state,
    build_valuation_state,
    classify_company,
    evaluate_risk,
    research_state,
    score_dimension,
)
from app.services.thesis_store import create_thesis, get_thesis, list_theses, review_due_theses, review_thesis, update_hypothesis
from app.services.industry_event_engine import industry_detail, list_events, list_industries, review_linked_event_theses, score_event, score_industry
from app.services.decision_store import build_review_outcome_suggestion, create_decision_snapshot, create_review, list_decision_snapshots, list_pool_members, list_position_states, list_reviews, remove_pool_member, transition_pool_member, transition_position, upsert_pool_member
from app.services.operations_overview import build_decision_audit, build_governance_overview, build_live_portfolio_risk
from app.services.stock_pool_sync import stage_candidates_in_watch_pool
from app.services.advanced_controls import bayesian_update, freeze_evidence, trade_attribution

router = APIRouter()


class ResearchEvaluationRequest(BaseModel):
    ts_code: str = ""
    industry: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    risk_flags: dict[str, Any] = Field(default_factory=dict)
    dimensions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class HypothesisInput(BaseModel):
    statement: str = Field(min_length=3, max_length=1000)
    metric: str = Field(default="", max_length=128)
    necessary: bool = False
    expected: dict[str, Any] = Field(default_factory=dict)


class ThesisCreateRequest(BaseModel):
    ts_code: str = Field(min_length=6, max_length=16)
    title: str = Field(min_length=3, max_length=255)
    thesis_type: str = Field(default="quality_growth", max_length=64)
    core_thesis: str = Field(default="", max_length=5000)
    next_review_at: str | None = None
    hypotheses: list[HypothesisInput] = Field(default_factory=list, max_length=30)


class HypothesisUpdateRequest(BaseModel):
    status: str
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class IndustryScoreRequest(BaseModel):
    observations: dict[str, Any] = Field(default_factory=dict)
    prior_score: float | None = None
    history_points: int = Field(default=0, ge=0)


class EventScoreRequest(BaseModel):
    event_type: str
    impact_direction: str = "neutral"
    severity: str = "medium"
    confidence: float = Field(default=.5, ge=0, le=1)
    source_code: str = ""
    event_date: str = ""


class PoolMemberRequest(BaseModel):
    ts_code: str = Field(min_length=6,max_length=16); pool_type: str; reason: str = Field(min_length=3,max_length=2000)


class PoolTransitionRequest(BaseModel):
    ts_code: str = Field(min_length=6, max_length=16)
    from_pool: str
    to_pool: str
    reason: str = Field(min_length=3, max_length=2000)


class DecisionSnapshotRequest(BaseModel):
    ts_code:str;decision_type:str;decision_time:str|None=None;price:float|None=None;thesis_id:int|None=None
    data_as_of_time:str;model_version:str;market_state:dict[str,Any]=Field(default_factory=dict);research_state:dict[str,Any]=Field(default_factory=dict)
    valuation:dict[str,Any]=Field(default_factory=dict);portfolio_context:dict[str,Any]=Field(default_factory=dict);reason:str=Field(min_length=3);invalidation_conditions:list[str]=Field(default_factory=list)


class ReviewRequest(BaseModel):
    decision_snapshot_id:int|None=None;review_time:str|None=None;outcome:dict[str,Any]=Field(default_factory=dict);attribution:dict[str,Any]=Field(default_factory=dict);error_type:str|None=None;lessons:str=""


class BayesianReviewRequest(BaseModel):
    prior: float = Field(.5, gt=0, lt=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class TradeAttributionRequest(BaseModel):
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    benchmark_return: float = 0.0
    industry_return: float = 0.0
    execution_cost: float = Field(0, ge=0)


class EvidenceFreezeRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = "evidence_freeze_v1"


class PositionTransitionRequest(BaseModel):
    ts_code:str;to_state:str;reason:str=Field(min_length=3);thesis_id:int|None=None;evidence:list[dict[str,Any]]=Field(default_factory=list)


class PositionStateConfirmRequest(BaseModel):
    ts_code: str
    to_state: str
    reason: str = Field(min_length=3)
    thesis_id: int | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confirmed: bool = False


@router.get("/research/capabilities")
def research_capabilities() -> dict[str, Any]:
    items = capability_registry()
    return {"ok": True, "items": items, "counts": {status: sum(1 for x in items if x["status"] == status) for status in ("ready", "partial", "building", "missing")}}


@router.get("/research/contracts")
def research_contracts() -> dict[str, Any]:
    return {"ok": True, "dimensions": {key: {"weights": value.weights, "minimum_coverage": value.minimum_coverage} for key, value in CONTRACTS.items()}}


@router.get("/research/stocks/{ts_code}")
def research_stock(ts_code: str) -> dict[str, Any]:
    return build_stock_research(ts_code)


@router.get("/research/expectations/{ts_code}")
def research_expectation(ts_code: str) -> dict[str, Any]:
    return build_expectation_state(ts_code)


@router.get("/research/valuations/{ts_code}")
def research_valuation(ts_code: str) -> dict[str, Any]:
    return build_valuation_state(ts_code)


@router.post("/research/evaluate")
def research_evaluate(req: ResearchEvaluationRequest) -> dict[str, Any]:
    risk = evaluate_risk(req.risk_flags)
    dimensions = {name: score_dimension(name, req.dimensions.get(name, {})) for name in CONTRACTS}
    return {
        "ok": True,
        "ts_code": req.ts_code,
        "classification": classify_company(industry=req.industry, metrics=req.metrics),
        "risk": risk,
        "dimensions": dimensions,
        "research_state": research_state(dimensions, risk),
        "disclaimer": "分数仅在输入证据覆盖足够时可用于研究准入；缺失项不会按零分伪装成完整评价。",
    }


@router.get("/research/theses")
def research_thesis_list(ts_code: str = "", limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"ok": True, "items": list_theses(ts_code=ts_code.strip().upper(), limit=limit)}


@router.post("/research/theses")
def research_thesis_create(req: ThesisCreateRequest) -> dict[str, Any]:
    payload = req.model_dump()
    payload["ts_code"] = req.ts_code.strip().upper()
    return {"ok": True, "item": create_thesis(payload)}


@router.get("/research/theses/{thesis_id}")
def research_thesis_get(thesis_id: int) -> dict[str, Any]:
    item = get_thesis(thesis_id)
    if not item:
        raise HTTPException(status_code=404, detail="投资逻辑不存在")
    return {"ok": True, "item": item}


@router.patch("/research/hypotheses/{hypothesis_id}")
def research_hypothesis_update(hypothesis_id: int, req: HypothesisUpdateRequest) -> dict[str, Any]:
    if req.status not in {"unverified", "verified", "warning", "failed", "invalidated"}:
        raise HTTPException(status_code=422, detail="非法假设状态")
    item = update_hypothesis(hypothesis_id, status=req.status, evidence=req.evidence)
    if not item:
        raise HTTPException(status_code=404, detail="投资假设不存在")
    return {"ok": True, "item": item}


@router.post("/research/theses/review-due")
def research_thesis_review_due(
    limit: int = Query(default=100, ge=1, le=500),
    interval_days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    return review_due_theses(limit=limit, review_interval_days=interval_days)


@router.post("/research/theses/{thesis_id}/review")
def research_thesis_review(thesis_id: int) -> dict[str, Any]:
    item = review_thesis(thesis_id)
    if not item:
        raise HTTPException(status_code=404, detail="投资逻辑不存在")
    return {"ok": True, "item": item}


@router.get("/research/industries")
def research_industries() -> dict[str, Any]:
    return list_industries()


@router.get("/research/industries/{industry_code}")
def research_industry_detail(industry_code: str) -> dict[str, Any]:
    return industry_detail(industry_code)


@router.post("/research/industries/score")
def research_industry_score(req: IndustryScoreRequest) -> dict[str, Any]:
    return {"ok": True, **score_industry(req.observations, prior_score=req.prior_score, history_points=req.history_points)}


@router.get("/research/events")
def research_events(ts_code: str = "", limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return list_events(ts_code=ts_code.strip().upper(), limit=limit)


@router.post("/research/events/score")
def research_event_score(req: EventScoreRequest) -> dict[str, Any]:
    return {"ok": True, **score_event(req.model_dump())}


@router.post("/research/events/review-linked")
def research_event_review_linked(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    """Run controlled reviews for theses linked to actionable negative events."""
    return review_linked_event_theses(limit=limit)


@router.get("/research/pools")
def research_pools(pool_type:str="")->dict[str,Any]: return {"ok":True,"items":list_pool_members(pool_type)}


@router.post("/research/pools")
def research_pool_add(req:PoolMemberRequest)->dict[str,Any]:
    try:return {"ok":True,"item":upsert_pool_member(ts_code=req.ts_code.strip().upper(),pool_type=req.pool_type,reason=req.reason)}
    except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc


@router.post("/research/pools/transition")
def research_pool_transition(req: PoolTransitionRequest) -> dict[str, Any]:
    try:
        item = transition_pool_member(
            ts_code=req.ts_code.strip().upper(), from_pool=req.from_pool,
            to_pool=req.to_pool, reason=req.reason,
        )
        return {"ok": True, "item": item}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/research/pools/stage-candidates")
def research_pool_stage_candidates(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """Stage scored candidates into the watch pool with their score evidence."""
    from app.api.routes.candidates import get_candidates
    result = get_candidates(limit=limit, strategy_id=None, budget=None, exchange="HS")
    return stage_candidates_in_watch_pool(list(result.candidates))


@router.delete("/research/pools/{member_id}")
def research_pool_remove(member_id:int)->dict[str,Any]: return {"ok":remove_pool_member(member_id)}


@router.get("/research/decisions")
def research_decisions(ts_code:str="",limit:int=Query(100,ge=1,le=500))->dict[str,Any]:return {"ok":True,"items":list_decision_snapshots(ts_code.strip().upper(),limit)}


@router.post("/research/theses/bayesian-review")
def research_bayesian_review(req: BayesianReviewRequest) -> dict[str, Any]:
    return bayesian_update(req.prior, req.evidence)


@router.post("/research/evidence/freeze")
def research_evidence_freeze(req: EvidenceFreezeRequest) -> dict[str, Any]:
    return freeze_evidence(req.payload, schema_version=req.schema_version)


@router.post("/research/attribution/trade")
def research_trade_attribution(req: TradeAttributionRequest) -> dict[str, Any]:
    return trade_attribution(**req.model_dump())


@router.post("/research/decisions")
def research_decision_create(req:DecisionSnapshotRequest)->dict[str,Any]:
    payload=req.model_dump();payload["ts_code"]=req.ts_code.strip().upper()
    try:return {"ok":True,"item":create_decision_snapshot(payload)}
    except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc


@router.get("/research/reviews")
def research_reviews(limit:int=Query(100,ge=1,le=500))->dict[str,Any]:return {"ok":True,"items":list_reviews(limit)}


@router.get("/research/reviews/suggestion/{decision_snapshot_id}")
def research_review_suggestion(decision_snapshot_id: int) -> dict[str, Any]:
    item = build_review_outcome_suggestion(decision_snapshot_id)
    if not item:
        raise HTTPException(status_code=404, detail="决策快照不存在")
    return item


@router.post("/research/reviews")
def research_review_create(req:ReviewRequest)->dict[str,Any]:return {"ok":True,"item":create_review(req.model_dump())}


@router.post("/research/position-state")
def research_position_transition(req:PositionTransitionRequest)->dict[str,Any]:
    try:return {"ok":True,"item":transition_position(ts_code=req.ts_code.strip().upper(),to_state=req.to_state,reason=req.reason,thesis_id=req.thesis_id,evidence=req.evidence)}
    except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc


@router.post("/research/position-state/confirm")
def research_position_state_confirm(req: PositionStateConfirmRequest) -> dict[str, Any]:
    if not req.confirmed:
        raise HTTPException(status_code=422, detail="explicit_confirmation_required")
    try:
        item = transition_position(
            ts_code=req.ts_code.strip().upper(), to_state=req.to_state,
            reason=req.reason, thesis_id=req.thesis_id, evidence=req.evidence,
        )
        return {"ok": True, "confirmed": True, "item": item}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/research/position-state")
def research_position_states() -> dict[str, Any]:
    return {"ok": True, "items": list_position_states()}


@router.get("/research/portfolio-risk/live")
def research_live_portfolio_risk(lookback: int = Query(120, ge=20, le=252)) -> dict[str, Any]:
    return build_live_portfolio_risk(lookback=lookback)


@router.get("/research/governance/overview")
def research_governance_overview() -> dict[str, Any]:
    return build_governance_overview()


@router.get("/research/decision-audit")
def research_decision_audit(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return build_decision_audit(limit=limit)
