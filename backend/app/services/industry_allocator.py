from __future__ import annotations

import math
from typing import Any

from app.data_platform.db import _conn
from app.services.industry_event_engine import list_industries
from app.services.theme_engine import list_themes


WEAK_MARKET_STATES = {"S2", "S7", "S8", "S9", "S10", "bear", "stress"}
from app.services.risk_limit_contract import DEFAULT_INDUSTRY_LIMIT

MAX_INDUSTRY_WEIGHT = DEFAULT_INDUSTRY_LIMIT
MAX_WEAK_MARKET_OPPORTUNITY_WEIGHT = .10


def _symbol(code: str) -> str:
    return str(code or "").split(".")[0].strip()


def _industry_members(symbols: list[str] | None = None) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    normalized = sorted({_symbol(value) for value in (symbols or []) if _symbol(value)})
    where = ""
    params: tuple[Any, ...] = ()
    if normalized:
        where = f" WHERE split_part(ts_code,'.',1) IN ({','.join('?' for _ in normalized)})"
        params = tuple(normalized)
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            f"SELECT ts_code,name,industry FROM dc_stock_master{where} ORDER BY industry,ts_code", params,
        ).fetchall()]
    by_symbol: dict[str, str] = {}
    by_industry: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        symbol = _symbol(str(row.get("ts_code") or ""))
        industry = str(row.get("industry") or "未分类").strip() or "未分类"
        by_symbol[symbol] = industry
        by_industry.setdefault(industry, []).append({"symbol": symbol, "name": str(row.get("name") or "")})
    return by_symbol, by_industry


def _quality(state: dict[str, Any]) -> tuple[float, float]:
    score = max(0.0, min(100.0, float(state.get("score") or 0)))
    coverage = max(0.0, min(1.0, float(state.get("coverage") or 0)))
    evidence = max(0, int(state.get("evidence_points") or 0))
    status_factor = 1.0 if state.get("status") == "ready" else .72
    evidence_factor = min(1.0, math.log1p(evidence) / math.log(101)) if evidence else .35
    confidence = coverage * .65 + evidence_factor * .35
    return round(score * (.55 + .45 * confidence) * status_factor, 4), round(confidence, 4)


def _capped_weights(scores: dict[str, float], total: float, cap: float) -> dict[str, float]:
    remaining = max(0.0, float(total))
    active = {key: max(.001, float(value)) for key, value in scores.items()}
    result = {key: 0.0 for key in active}
    while active and remaining > 1e-9:
        score_total = sum(active.values())
        saturated = []
        for key, score in active.items():
            proposed = remaining * score / score_total
            room = max(0.0, cap - result[key])
            if proposed >= room - 1e-9:
                result[key] += room
                remaining -= room
                saturated.append(key)
        if not saturated:
            for key, score in active.items():
                result[key] += remaining * score / score_total
            remaining = 0.0
        for key in saturated:
            active.pop(key, None)
    return {key: round(value, 6) for key, value in result.items() if value > 0}


def build_industry_allocation(
    *,
    symbols: list[str] | None = None,
    symbol_scores: dict[str, float] | None = None,
    equity_budget: float = 1.0,
    market_state: str = "unknown",
    as_of_time: str | None = None,
) -> dict[str, Any]:
    """Rank industries and allocate the equity sleeve before stock weighting."""
    score_by_symbol = {_symbol(key): float(value or 0) for key, value in (symbol_scores or {}).items()}
    # A page-level allocation over all industries does not need the stock
    # membership map. Only production calls with an explicit symbol universe
    # need that table, so the read-only console path avoids scanning it.
    by_symbol, candidates_by_industry = _industry_members(symbols) if symbols else ({}, {})
    research = list_industries(as_of_time=as_of_time).get("items") or []
    # Allocation only needs the narrative score and lifecycle. Company mapping
    # evidence is already exposed by the research page's themes endpoint.
    themes = list_themes(
        as_of_time=as_of_time,
        limit=300,
        include_companies=False,
        compact=True,
        include_company_counts=False,
    ).get("items") or []
    narrative_by_industry = {
        str((item.get("payload") or {}).get("knowledge_graph", {}).get("industry") or str(item.get("theme_code") or "").removeprefix("industry:")): item
        for item in themes
    }
    state_by_name = {str(item.get("industry_name") or ""): item for item in research}
    weak_market = str(market_state) in WEAK_MARKET_STATES
    rows = []
    candidate_industries = sorted(set(by_symbol.values())) if symbols else sorted(candidates_by_industry)
    if not symbols:
        candidate_industries = sorted(state_by_name)
    for industry in candidate_industries:
        research_item = state_by_name.get(industry) or {}
        state = research_item.get("state") or {}
        quality, confidence = _quality(state)
        narrative = narrative_by_industry.get(industry) or {}
        narrative_score = float(narrative.get("score") or 0)
        narrative_lifecycle = str(narrative.get("lifecycle") or "无主线")
        narrative_promotable = (
            bool(narrative.get("promotable"))
            and narrative_lifecycle not in {"衰退", "退潮", "沉寂"}
        )
        members = candidates_by_industry.get(industry, [])
        member_scores = [score_by_symbol.get(item["symbol"], 0.0) for item in members if item["symbol"] in score_by_symbol]
        selection_score = sum(member_scores) / len(member_scores) if member_scores else 0.0
        combined = narrative_score * .60 + quality * .20 + max(0.0, selection_score) * .20
        strong_in_weak_market = narrative_promotable and narrative_score >= 60
        eligible = bool(members if symbols else True) and narrative_promotable and (not weak_market or strong_in_weak_market)
        blockers = []
        if not narrative_promotable:
            blockers.append("短期叙事尚未确认或已经退潮")
        if weak_market and not strong_in_weak_market:
            blockers.append("弱市中未达到强行业逆势准入门")
        rows.append({
            "industry": industry,
            "raw_score": round(float(state.get("score") or 0), 4),
            "quality_score": quality,
            "confidence": confidence,
            "coverage": round(float(state.get("coverage") or 0), 4),
            "lifecycle": str(state.get("lifecycle") or "证据不足"),
            "status": str(state.get("status") or "insufficient_data"),
            "selection_score": round(selection_score, 4),
            "narrative_score": round(narrative_score, 4),
            "narrative_lifecycle": narrative_lifecycle,
            "narrative_promotable": narrative_promotable,
            "combined_score": round(combined, 4),
            "eligible": eligible,
            "strong_in_weak_market": strong_in_weak_market,
            "blockers": blockers,
            "candidates": sorted(members, key=lambda item: score_by_symbol.get(item["symbol"], 0), reverse=True)[:8],
        })
    eligible_rows = [row for row in rows if row["eligible"]]
    requested_budget = max(0.0, min(1.0, float(equity_budget)))
    cap = min(MAX_INDUSTRY_WEIGHT, MAX_WEAK_MARKET_OPPORTUNITY_WEIGHT if weak_market else MAX_INDUSTRY_WEIGHT)
    targets = _capped_weights({row["industry"]: row["combined_score"] for row in eligible_rows}, requested_budget, cap)
    for row in rows:
        row["target_weight"] = targets.get(row["industry"], 0.0)
    rows.sort(key=lambda row: (row["eligible"], row["combined_score"]), reverse=True)
    allocated = round(sum(targets.values()), 6)
    return {
        "ok": bool(rows),
        "schema_version": "alis_industry_allocation_v1",
        "as_of_time": as_of_time or "",
        "market_state": market_state,
        "weak_market": weak_market,
        "requested_equity_budget": requested_budget,
        "allocated_equity_budget": allocated,
        "unallocated_equity_budget": round(max(0.0, requested_budget - allocated), 6),
        "max_industry_weight": cap,
        "industry_targets": targets,
        "symbol_industries": by_symbol,
        "items": rows,
        "method": "market_then_industry_then_stock_v1",
    }


def apply_industry_targets(
    weights: dict[str, float], allocation: dict[str, Any], *, equity_budget: float,
) -> dict[str, float]:
    industries = allocation.get("symbol_industries") or {}
    targets = allocation.get("industry_targets") or {}
    grouped: dict[str, dict[str, float]] = {}
    for symbol, weight in weights.items():
        grouped.setdefault(str(industries.get(_symbol(symbol)) or "未分类"), {})[_symbol(symbol)] = max(0.0, float(weight or 0))
    result: dict[str, float] = {}
    for industry, members in grouped.items():
        target = float(targets.get(industry) or 0)
        total = sum(members.values())
        if target <= 0 or total <= 0:
            continue
        for symbol, weight in members.items():
            result[symbol] = weight / total * target
    total = sum(result.values())
    budget = max(0.0, min(1.0, float(equity_budget)))
    if total > budget > 0:
        result = {symbol: weight / total * budget for symbol, weight in result.items()}
    return {symbol: round(weight, 8) for symbol, weight in result.items() if weight > 0}
