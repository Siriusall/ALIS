from __future__ import annotations

import copy
import threading
import time
from typing import Any

import httpx

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.data_platform.db import _conn as data_conn
from app.data_platform.services.decision_data_contract import decision_data_contract
from app.data_platform.services.market_calendar import target_trade_date
from app.db.store import get_positions
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
from app.services.thesis_store import create_thesis, generate_thesis_for_stock, generate_watch_pool_theses, get_thesis, list_theses, review_due_theses, review_thesis, update_hypothesis
from app.services.industry_event_engine import industry_detail, list_event_followups, list_events, list_industries, materialize_event_followups, materialize_industry_state_history, materialize_industry_state_snapshots, review_linked_event_theses, score_event, score_industry, update_event_followup
from app.services.industry_allocator import build_industry_allocation
from app.services.market_context import live_market_context
from app.services.decision_store import build_review_outcome_suggestion, create_decision_snapshot, create_review, diff_decision_snapshots, get_decision_snapshot, list_decision_snapshot_summaries, list_decision_snapshots, list_pool_members, list_position_state_history, list_position_states, list_reviews, remove_pool_member, transition_pool_member, transition_position, upsert_pool_member, verify_decision_snapshot
from app.services.operations_overview import build_decision_audit, build_governance_overview, build_live_portfolio_risk
from app.services.position_valuation import build_position_market_series_cache
from app.services.stock_pool_sync import stage_candidates_in_watch_pool
from app.services.research_workflow import build_console_stock_evidence, evaluate_core_pool_eligibility, process_watch_pool
from app.services.advanced_controls import bayesian_update, freeze_evidence, trade_attribution
from app.services.theme_engine import get_theme, list_themes, materialize_theme_snapshots
from app.services.manual_review_queue import build_manual_review_queue, submit_manual_review_action
from app.services.llm_review_assistant import generate_review_draft, llm_token_budget_status
from app.services.llm_research_agent import list_llm_research_proposals
from app.services.review_learning import materialize_decision_outcomes, review_learning_overview
from app.services.manual_review_triage import run_manual_review_triage_once, triage_status
from app.services.profit_loop import build_portfolio_candles, build_profit_loop
from app.services.portfolio_console import build_portfolio_console_research
from app.core.read_only_diagnostics import degraded_read

router = APIRouter()

_PORTFOLIO_RISK_CACHE_TTL_SECONDS = 5.0
_portfolio_risk_cache_lock = threading.Lock()
_portfolio_risk_cache: dict[int, tuple[float, dict[str, Any]]] = {}


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


class EventFollowupUpdateRequest(BaseModel):
    status: str
    next_action: str = Field(default="", max_length=2000)
    actor: str = Field(default="user", max_length=128)


class PoolMemberRequest(BaseModel):
    ts_code: str = Field(min_length=6,max_length=16); pool_type: str; reason: str = Field(min_length=3,max_length=2000)


class PoolTransitionRequest(BaseModel):
    ts_code: str = Field(min_length=6, max_length=16)
    from_pool: str
    to_pool: str
    reason: str = Field(min_length=3, max_length=2000)


class DecisionSnapshotRequest(BaseModel):
    ts_code:str;decision_type:str;decision_time:str|None=None;price:float|None=None;thesis_id:int|None=None;execution_id:int|None=None;operation_id:int|None=None
    data_as_of_time:str;model_version:str;market_state:dict[str,Any]=Field(default_factory=dict);research_state:dict[str,Any]=Field(default_factory=dict)
    valuation:dict[str,Any]=Field(default_factory=dict);portfolio_context:dict[str,Any]=Field(default_factory=dict);reason:str=Field(min_length=3);invalidation_conditions:list[str]=Field(default_factory=list)


class ReviewRequest(BaseModel):
    decision_snapshot_id:int|None=None;review_time:str|None=None;outcome:dict[str,Any]=Field(default_factory=dict);attribution:dict[str,Any]=Field(default_factory=dict);error_type:str|None=None;lessons:str=""


class ManualReviewActionRequest(BaseModel):
    review_key: str
    item_kind: str
    action: str
    error_type: str = ""
    comment: str = ""
    llm_draft: dict[str, Any] = Field(default_factory=dict)


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
    rule_version:str="position_state_v1";model_version:str="";automatic_decision:dict[str,Any]=Field(default_factory=dict)
    intervention_type:str="override";intervention_actor:str="user"


class PositionStateConfirmRequest(PositionTransitionRequest):
    confirmed: bool = False


@router.get("/research/capabilities")
def research_capabilities() -> dict[str, Any]:
    items = capability_registry()
    return {"ok": True, "items": items, "counts": {status: sum(1 for x in items if x["status"] == status) for status in ("ready", "partial", "building", "missing")}}


@router.get("/research/llm/proposals")
def research_llm_proposals(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    try:
        return list_llm_research_proposals(limit=limit)
    except Exception as exc:  # noqa: BLE001 - optional research evidence must degrade safely
        return degraded_read("llm_research_proposals", exc, count=0, items=[], quality_report={})


@router.get("/research/themes")
def research_themes(
    as_of_time: str = "", promotable_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    compact: bool = False,
) -> dict[str, Any]:
    return list_themes(
        as_of_time=as_of_time or None,
        promotable_only=promotable_only,
        limit=limit,
        include_companies=not compact,
        compact=compact,
    )


@router.get("/research/themes/{theme_code:path}")
def research_theme_detail(theme_code: str, as_of_date: str = "") -> dict[str, Any]:
    item = get_theme(theme_code, as_of_date=as_of_date)
    if not item:
        raise HTTPException(status_code=404, detail="主题快照不存在")
    return {"ok": True, "item": item}


@router.post("/research/themes/materialize")
def research_themes_materialize(as_of_time: str = "", lookback: int = Query(default=20, ge=5, le=60)) -> dict[str, Any]:
    return materialize_theme_snapshots(as_of_time=as_of_time or None, lookback=lookback)


@router.get("/research/contracts")
def research_contracts() -> dict[str, Any]:
    return {"ok": True, "dimensions": {key: {"weights": value.weights, "minimum_coverage": value.minimum_coverage} for key, value in CONTRACTS.items()}}


@router.get("/research/stocks/{ts_code}")
def research_stock(ts_code: str) -> dict[str, Any]:
    return build_stock_research(ts_code, persist=False)


@router.get("/research/stocks/{ts_code}/console-evidence")
def research_console_stock_evidence(ts_code: str) -> dict[str, Any]:
    try:
        return build_console_stock_evidence(ts_code)
    except Exception as exc:  # noqa: BLE001 - read-only panel must degrade without HTTP 500
        return degraded_read("research_stock_evidence", exc, ts_code=str(ts_code).strip().upper(), company={}, expectations={}, valuation={}, eligibility={}, financialCompleteness={})


@router.get("/research/expectations/{ts_code}")
def research_expectation(ts_code: str) -> dict[str, Any]:
    return build_expectation_state(ts_code, persist_baseline=False)


@router.get("/research/valuations/{ts_code}")
def research_valuation(ts_code: str) -> dict[str, Any]:
    return build_valuation_state(ts_code, persist=False)


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
    try:
        # The research page only renders thesis identity/status fields. Full
        # hypothesis evidence remains available from the per-thesis endpoint.
        return {
            "ok": True,
            "items": list_theses(
                ts_code=ts_code.strip().upper(),
                limit=limit,
                include_hypotheses=False,
            ),
        }
    except Exception as exc:  # noqa: BLE001 - read-only evidence must terminate safely
        return degraded_read("research_theses", exc, items=[])


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


@router.post("/research/theses/generate/{ts_code}")
def research_thesis_generate(ts_code: str, force: bool = False) -> dict[str, Any]:
    return generate_thesis_for_stock(ts_code, force=force)


@router.post("/research/theses/generate-watch-pool")
def research_thesis_generate_watch_pool(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return generate_watch_pool_theses(limit=limit)


@router.post("/research/theses/{thesis_id}/review")
def research_thesis_review(thesis_id: int) -> dict[str, Any]:
    item = review_thesis(thesis_id)
    if not item:
        raise HTTPException(status_code=404, detail="投资逻辑不存在")
    return {"ok": True, "item": item}


@router.get("/research/industries")
def research_industries(as_of_time: str = "", compact: bool = False) -> dict[str, Any]:
    """Read latest or an explicit point-in-time industry-state snapshot."""
    return list_industries(as_of_time=as_of_time or None, compact=compact)


@router.get("/research/industries/allocation")
def research_industry_allocation(
    as_of_time: str = "",
    market_state: str = "",
    equity_budget: float = Query(default=-1, ge=-1, le=1),
    compact: bool = False,
) -> dict[str, Any]:
    """Expose the market -> industry budget layer used by daily selection."""
    try:
        context = live_market_context()
        state = market_state or str((context.get("market_state") or {}).get("state") or "unknown")
        if equity_budget < 0:
            defaults = {"S0": .85, "S1": .75, "S2": .70, "S3": .65, "S4": .50, "S5": .55, "S6": .50, "S7": .45, "S8": .35, "S9": .25, "S10": .10, "S11": .40}
            equity_budget = defaults.get(state, .50)
        result = build_industry_allocation(
            equity_budget=equity_budget, market_state=state, as_of_time=as_of_time or None,
        )
        if not compact:
            return result
        row_fields = {
            "industry", "raw_score", "quality_score", "confidence", "coverage",
            "lifecycle", "status", "selection_score", "narrative_score",
            "narrative_lifecycle", "narrative_promotable", "combined_score",
            "eligible", "strong_in_weak_market", "blockers", "target_weight",
            "evidence_status",
        }
        return {
            **{key: value for key, value in result.items() if key not in {"items", "symbol_industries", "industry_targets"}},
            "items": [{key: row.get(key) for key in row_fields if key in row} for row in result.get("items", [])],
        }
    except Exception as exc:  # noqa: BLE001 - optional industry evidence must degrade safely
        return degraded_read("industry_allocation", exc, market_state=market_state or "unknown", items=[])


@router.get("/research/industries/{industry_code}")
def research_industry_detail(industry_code: str) -> dict[str, Any]:
    return industry_detail(industry_code)


@router.post("/research/industries/score")
def research_industry_score(req: IndustryScoreRequest) -> dict[str, Any]:
    return {"ok": True, **score_industry(req.observations, prior_score=req.prior_score, history_points=req.history_points)}


@router.post("/research/industries/materialize")
def research_industry_materialize(as_of_time: str = "") -> dict[str, Any]:
    return materialize_industry_state_snapshots(as_of_time=as_of_time or None)


@router.post("/research/industries/materialize-history")
def research_industry_materialize_history(
    anchor_sessions: int = Query(default=20, ge=5, le=60),
    max_points: int = Query(default=36, ge=1, le=120),
) -> dict[str, Any]:
    return materialize_industry_state_history(anchor_sessions=anchor_sessions, max_points=max_points)


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


@router.post("/research/events/followups/materialize")
def research_event_followups_materialize(limit: int = Query(default=500, ge=1, le=2000)) -> dict[str, Any]:
    return materialize_event_followups(limit=limit)


@router.get("/research/events/followups")
def research_event_followups(
    status: str = "", ts_code: str = "", limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    return list_event_followups(status=status, ts_code=ts_code.strip().upper(), limit=limit)


@router.patch("/research/events/followups/{event_key:path}")
def research_event_followup_update(event_key: str, req: EventFollowupUpdateRequest) -> dict[str, Any]:
    try:
        item = update_event_followup(event_key, status=req.status, next_action=req.next_action, actor=req.actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="event_followup_not_found")
    return {"ok": True, "item": item}


@router.get("/research/pools")
def research_pools(pool_type:str="")->dict[str,Any]: return {"ok":True,"items":list_pool_members(pool_type)}


@router.get("/research/pools/eligibility/{ts_code}")
def research_pool_eligibility(ts_code: str) -> dict[str, Any]:
    try:
        return evaluate_core_pool_eligibility(ts_code, persist_research=False)
    except Exception as exc:  # noqa: BLE001 - read-only gate evidence must degrade safely
        return degraded_read("research_pool_eligibility", exc, ts_code=str(ts_code).strip().upper(), gates=[], eligible_for_core=None)


@router.post("/research/pools")
def research_pool_add(req:PoolMemberRequest)->dict[str,Any]:
    if req.pool_type == "core":
        eligibility = evaluate_core_pool_eligibility(req.ts_code)
        if not eligibility["eligible_for_core"]:
            failed = [item["label"] for item in eligibility["gates"] if not item["passed"]]
            raise HTTPException(status_code=409, detail={"code": "core_pool_gate_failed", "failed_gates": failed, "eligibility": eligibility})
    try:return {"ok":True,"item":upsert_pool_member(ts_code=req.ts_code.strip().upper(),pool_type=req.pool_type,reason=req.reason)}
    except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc


@router.post("/research/pools/transition")
def research_pool_transition(req: PoolTransitionRequest) -> dict[str, Any]:
    if req.to_pool == "core":
        eligibility = evaluate_core_pool_eligibility(req.ts_code)
        if not eligibility["eligible_for_core"]:
            failed = [item["label"] for item in eligibility["gates"] if not item["passed"]]
            raise HTTPException(status_code=409, detail={"code": "core_pool_gate_failed", "failed_gates": failed, "eligibility": eligibility})
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


@router.post("/research/pools/process-watch")
def research_pool_process_watch(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return process_watch_pool(limit=limit)


@router.delete("/research/pools/{member_id}")
def research_pool_remove(member_id:int)->dict[str,Any]: return {"ok":remove_pool_member(member_id)}


@router.get("/research/decisions")
def research_decisions(ts_code:str="",limit:int=Query(100,ge=1,le=500),compact: bool = Query(True))->dict[str,Any]:
    try:
        rows = list_decision_snapshot_summaries(ts_code.strip().upper(), limit) if compact else list_decision_snapshots(ts_code.strip().upper(), limit)
        return {"ok":True,"items":rows,"compact":compact}
    except Exception as exc:  # noqa: BLE001 - audit evidence is optional read-only data
        return degraded_read("decisions", exc, items=[])


@router.get("/research/decisions/{decision_snapshot_id}")
def research_decision_detail(decision_snapshot_id: int) -> dict[str, Any]:
    try:
        item = get_decision_snapshot(decision_snapshot_id)
    except Exception as exc:  # noqa: BLE001 - optional audit evidence must degrade safely
        return degraded_read("decision_detail", exc, id=int(decision_snapshot_id))
    if not item:
        raise HTTPException(status_code=404, detail="decision_snapshot_not_found")
    return item


@router.get("/research/evidence/verify/{decision_snapshot_id}")
def research_evidence_verify(decision_snapshot_id: int) -> dict[str, Any]:
    try:
        item = verify_decision_snapshot(decision_snapshot_id)
    except Exception as exc:  # noqa: BLE001 - optional audit evidence must degrade safely
        return degraded_read(
            "decision_evidence_verification",
            exc,
            id=int(decision_snapshot_id),
            verified=False,
        )
    if not item:
        raise HTTPException(status_code=404, detail="decision_snapshot_not_found")
    return item


@router.get("/research/evidence/diff")
def research_evidence_diff(left_id: int = Query(ge=1), right_id: int = Query(ge=1)) -> dict[str, Any]:
    item = diff_decision_snapshots(left_id, right_id)
    if not item:
        raise HTTPException(status_code=404, detail="decision_snapshot_not_found")
    return item


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
def research_reviews(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    try:
        return {"ok": True, "items": list_reviews(limit)}
    except Exception as exc:  # noqa: BLE001 - optional review evidence must degrade safely
        return degraded_read("reviews", exc, items=[])


@router.get("/research/reviews/learning-overview")
def research_review_learning_overview() -> dict[str, Any]:
    try:
        return review_learning_overview()
    except Exception as exc:  # noqa: BLE001 - optional learning evidence must degrade safely
        return degraded_read("review_learning", exc, items=[], summary={})


@router.post("/research/reviews/materialize-outcomes")
def research_materialize_review_outcomes(limit: int = Query(default=1000, ge=1, le=5000)) -> dict[str, Any]:
    return materialize_decision_outcomes(limit=limit)


@router.get("/research/profit-loop")
def research_profit_loop(compact: bool = Query(False)) -> dict[str, Any]:
    try:
        if compact:
            return build_portfolio_console_research()
        return build_profit_loop(persist=False)
    except Exception as exc:  # noqa: BLE001 - optional research evidence must degrade safely
        return degraded_read("profit_loop", exc, overview={}, sell_labels=[])


@router.get("/research/portfolio-candles")
def research_portfolio_candles(limit: int = Query(90, ge=20, le=250)) -> dict[str, Any]:
    try:
        return build_portfolio_candles(limit)
    except Exception as exc:  # noqa: BLE001 - optional portfolio evidence must degrade safely
        return degraded_read(
            "portfolio_candles",
            exc,
            series=[],
            position_count=0,
            unit="indexed_account_equity",
            base=100,
            data_as_of_date="",
            benchmark={"symbol": "000300.SH", "name": "沪深300", "available": False},
            latest_comparison={},
        )


@router.get("/research/manual-review/queue")
def research_manual_review_queue(limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    result = build_manual_review_queue(limit)
    result["ai_triage"] = {**(result.get("ai_triage") or {}), **triage_status()}
    result["llm_budget"] = llm_token_budget_status()
    return result


@router.get("/research/llm/token-budget")
def research_llm_token_budget() -> dict[str, Any]:
    return llm_token_budget_status()


@router.post("/research/manual-review/triage")
def research_manual_review_triage(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return run_manual_review_triage_once(limit=limit)


@router.get("/research/manual-review/triage/status")
def research_manual_review_triage_status() -> dict[str, Any]:
    return {"ok": True, **triage_status()}


@router.post("/research/manual-review/action")
def research_manual_review_action(req: ManualReviewActionRequest) -> dict[str, Any]:
    try:
        return {"ok": True, "item": submit_manual_review_action(**req.model_dump(), actor="authenticated_user")}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/research/reviews/suggestion/{decision_snapshot_id}")
def research_review_suggestion(decision_snapshot_id: int) -> dict[str, Any]:
    item = build_review_outcome_suggestion(decision_snapshot_id)
    if not item:
        raise HTTPException(status_code=404, detail="决策快照不存在")
    return item


@router.post("/research/reviews/llm-draft/{decision_snapshot_id}")
def research_review_llm_draft(decision_snapshot_id: int) -> dict[str, Any]:
    try:
        return generate_review_draft(decision_snapshot_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=503 if detail == "deepseek_api_key_not_configured" else 404, detail=detail) from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc


@router.post("/research/reviews")
def research_review_create(req:ReviewRequest)->dict[str,Any]:return {"ok":True,"item":create_review(req.model_dump())}


@router.post("/research/position-state")
def research_position_transition(req:PositionTransitionRequest)->dict[str,Any]:
    try:return {"ok":True,"item":transition_position(ts_code=req.ts_code.strip().upper(),to_state=req.to_state,reason=req.reason,thesis_id=req.thesis_id,evidence=req.evidence,rule_version=req.rule_version,model_version=req.model_version,automatic_decision=req.automatic_decision,intervention_type=req.intervention_type,intervention_actor=req.intervention_actor)}
    except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc


@router.post("/research/position-state/confirm")
def research_position_state_confirm(req: PositionStateConfirmRequest) -> dict[str, Any]:
    if not req.confirmed:
        raise HTTPException(status_code=422, detail="explicit_confirmation_required")
    try:
        item = transition_position(
            ts_code=req.ts_code.strip().upper(), to_state=req.to_state,
            reason=req.reason, thesis_id=req.thesis_id, evidence=req.evidence,
            rule_version=req.rule_version, model_version=req.model_version,
            automatic_decision=req.automatic_decision,
            intervention_type=req.intervention_type, intervention_actor=req.intervention_actor,
        )
        return {"ok": True, "confirmed": True, "item": item}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/research/position-state")
def research_position_states() -> dict[str, Any]:
    return {"ok": True, "items": list_position_states()}


@router.get("/research/position-state/history")
def research_position_state_history(
    ts_code: str = Query("", max_length=16), limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    return {"ok": True, "items": list_position_state_history(ts_code.strip().upper(), limit)}


@router.get("/research/portfolio-risk/live")
def research_live_portfolio_risk(lookback: int = Query(120, ge=20, le=252)) -> dict[str, Any]:
    try:
        cache_key = max(20, min(int(lookback or 120), 252))
        with data_conn() as conn:
            as_of = target_trade_date(conn)
            contract = decision_data_contract(as_of, conn=conn)
        if not bool(contract.get("ok")):
            return {
                "ok": False,
                "status": "blocked",
                "data_available": False,
                "component": "portfolio_risk",
                "target_trade_date": as_of,
                "error": {
                    "code": "decision_data_not_ready",
                    "message": "目标交易日决策数据未就绪，风险页不使用上一交易日持仓估值。",
                    "issues": list(contract.get("issues") or []),
                },
                "target_weights": [],
                "exposures": [],
                "breaches": [],
            }
        now = time.monotonic()
        with _portfolio_risk_cache_lock:
            cached = _portfolio_risk_cache.get(cache_key)
        if cached and now - cached[0] < _PORTFOLIO_RISK_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

        positions = get_positions()
        valuation_cache = build_position_market_series_cache(
            positions,
            as_of=as_of,
            lookback=cache_key,
        )
        result = build_live_portfolio_risk(
            lookback=cache_key,
            positions=positions,
            as_of=as_of,
            valuation_cache=valuation_cache,
        )
        with _portfolio_risk_cache_lock:
            _portfolio_risk_cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
        return result
    except Exception as exc:  # noqa: BLE001 - optional portfolio evidence must degrade safely
        return degraded_read("portfolio_risk", exc, target_weights=[], exposures=[], breaches=[])


@router.get("/research/governance/overview")
def research_governance_overview() -> dict[str, Any]:
    return build_governance_overview()


@router.get("/research/decision-audit")
def research_decision_audit(limit: int = Query(100, ge=1, le=500), compact: bool = Query(False)) -> dict[str, Any]:
    try:
        return build_decision_audit(limit=limit, compact=compact)
    except Exception as exc:  # noqa: BLE001 - optional audit evidence must degrade safely
        return degraded_read("decision_audit", exc, decisions=[], reviews=[])
