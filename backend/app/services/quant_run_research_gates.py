"""Research and market-state gates used before quant portfolio construction."""

from __future__ import annotations

from typing import Any, Dict, List

from app.services.point_in_time import normalize_explicit_timestamp


def normalize_ts_code(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if "." in value:
        return value
    return (
        f"{value}.SH" if value[:1] in {"5", "6", "9"}
        else f"{value}.BJ" if value[:1] in {"4", "8"}
        else f"{value}.SZ"
    )


def production_market_context_as_of(
    as_of_time: str,
    *,
    trade_date: str | None = None,
    market_state_binding: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    from app.services import market_context as market_context_service
    from app.db.store import list_model_versions
    from app.services.hsmm_shadow import infer_hsmm_state_as_of
    from app.services.production_strategy import resolve_production_strategy_champion

    try:
        market = dict(
            market_context_service.market_context_as_of(
                as_of_time,
                trade_date=trade_date,
            ) or {}
        )
    except TypeError:
        # Keep isolated legacy fixtures that monkeypatch the one-argument
        # reader compatible while production uses the split clocks.
        market = dict(market_context_service.market_context_as_of(as_of_time) or {})
    # A blocked market-context read is itself a hard PIT failure. Do not try to
    # synthesize a transparent/HSMM fallback from another clock or silently
    # turn the missing state into an executable ``unknown`` regime.
    if market.get("ok") is False:
        market.setdefault("as_of_time", as_of_time)
        market.setdefault("market_state_runtime", {
            "requested_model": "",
            "effective_model": "",
            "transparent_state": "",
            "effective_state": "",
            "fallback_reason": str(market.get("reason") or market.get("status") or "market_context_unavailable"),
            "hsmm_evidence": {},
            "hsmm_sidecar": {},
            "hsmm_sidecar_controls_risk_budget": False,
        })
        return market
    binding = (
        dict(market_state_binding)
        if isinstance(market_state_binding, dict)
        else dict(resolve_production_strategy_champion().get("market_state_binding") or {})
    )
    requested_model = str(binding.get("production_model") or "transparent_four_state_v1")
    transparent_payload = (
        dict(market.get("market_state") or {})
        if isinstance(market.get("market_state"), dict)
        else {}
    )
    transparent_state = str(transparent_payload.get("state") or "unknown")
    effective_model = "transparent_four_state_v1"
    fallback_reason = ""
    hsmm_evidence: Dict[str, Any] = {}
    hsmm_sidecar: Dict[str, Any] = {}
    try:
        latest_hsmm = next(
            (
                item for item in list_model_versions("market_regime", limit=10)
                if str(item.get("artifact_path") or "")
                and str(item.get("status") or "") not in {"superseded", "rolled_back"}
            ),
            None,
        )
        if latest_hsmm:
            hsmm_sidecar = infer_hsmm_state_as_of(
                {
                    "id": int(latest_hsmm["id"]),
                    "artifact_path": str(latest_hsmm.get("artifact_path") or ""),
                    "created_at": str(latest_hsmm.get("created_at") or ""),
                },
                as_of_time=as_of_time,
            )
            hsmm_sidecar.update({
                "version": str(latest_hsmm.get("version") or ""),
                "validation_status": str(
                    (latest_hsmm.get("validation") or {}).get("status") or ""
                ),
                "authorization_scope": "observation_only",
                "controls_risk_budget": False,
            })
    except Exception as exc:
        hsmm_sidecar = {
            "ok": False,
            "status": "unavailable",
            "reason": f"hsmm_sidecar_inference_failed:{str(exc)[:160]}",
            "authorization_scope": "observation_only",
            "controls_risk_budget": False,
        }
    if requested_model == "market_regime_hsmm_v1":
        hsmm_binding = binding.get("hsmm_shadow")
        hsmm_evidence = infer_hsmm_state_as_of(
            hsmm_binding if isinstance(hsmm_binding, dict) else None,
            as_of_time=as_of_time,
        )
        hsmm_state = str(hsmm_evidence.get("state") or "")
        if bool(hsmm_evidence.get("ok")) and hsmm_state in {"S0", "S1", "S2", "S3"}:
            market["market_state"] = {
                "state": hsmm_state,
                "label": market_context_service.REGIME_LABELS.get(hsmm_state, hsmm_state),
                "confidence": round(float(hsmm_evidence.get("confidence") or 0.0), 6),
                "model": "market_regime_hsmm_v1",
                "reason": "Champion 冻结 HSMM 点时状态",
                "version_id": int(hsmm_evidence.get("version_id") or 0),
            }
            market["market_profile"] = market_context_service.classify_market_profile(
                hsmm_state,
                market.get("observations")
                if isinstance(market.get("observations"), dict)
                else {},
            )
            effective_model = "market_regime_hsmm_v1"
        else:
            fallback_reason = str(
                hsmm_evidence.get("reason") or "hsmm_point_in_time_state_unavailable"
            )
    elif requested_model != "transparent_four_state_v1":
        fallback_reason = f"unsupported_market_state_model:{requested_model}"
    effective_state = str(
        ((market.get("market_state") or {}).get("state") or transparent_state)
        if isinstance(market.get("market_state"), dict)
        else transparent_state
    )
    market["market_state_binding"] = binding
    market["market_state_runtime"] = {
        "requested_model": requested_model,
        "effective_model": effective_model,
        "transparent_state": transparent_state,
        "effective_state": effective_state,
        "fallback_reason": fallback_reason,
        "hsmm_evidence": hsmm_evidence,
        "hsmm_sidecar": hsmm_sidecar,
        "hsmm_sidecar_controls_risk_budget": False,
    }
    governance = (
        dict(market.get("model_governance") or {})
        if isinstance(market.get("model_governance"), dict)
        else {}
    )
    governance["requested_production_model"] = requested_model
    governance["effective_production_model"] = effective_model
    governance["fallback_reason"] = fallback_reason
    market["model_governance"] = governance
    return market


def apply_research_admission(
    target_symbols: List[str],
    *,
    as_of_time: str | None = None,
) -> tuple[List[str], List[Dict[str, Any]]]:
    """Apply the legacy/manual core-pool gate; frozen unified plans bypass it."""
    from app.services.decision_store import list_pool_members
    from app.services.research_workflow import evaluate_core_pool_eligibility

    core_codes = {
        str(item.get("ts_code") or "").strip().upper()
        for item in list_pool_members("core")
        if not as_of_time
        or (item.get("entered_at") and str(item.get("entered_at")) <= str(as_of_time))
    }
    admitted: List[str] = []
    blocked: List[Dict[str, Any]] = []
    for symbol in target_symbols:
        code = normalize_ts_code(symbol)
        if code not in core_codes:
            blocked.append({
                "symbol": symbol,
                "ts_code": code,
                "side": "BUY",
                "code": "not_in_core_pool",
                "message": "模型排名不能绕过核心研究池准入",
            })
            continue
        eligibility = evaluate_core_pool_eligibility(code, as_of_time=as_of_time)
        if not eligibility.get("eligible_for_core"):
            failed = [
                gate.get("label")
                for gate in eligibility.get("gates") or []
                if not gate.get("passed")
            ]
            blocked.append({
                "symbol": symbol,
                "ts_code": code,
                "side": "BUY",
                "code": "core_evidence_gate_failed",
                "message": f"核心候选证据已失效：{'、'.join(str(item) for item in failed)}",
                "eligibility": eligibility,
            })
            continue
        admitted.append(symbol)
    return admitted, blocked


def apply_dynamic_strategy_gate(
    target_symbols: List[str],
    *,
    as_of_time: str,
) -> tuple[List[str], List[Dict[str, Any]], Dict[str, Any]]:
    """Apply environment, expectation and valuation gates to admitted names."""
    from app.services.defensive_assets import defensive_allocation_for_state
    from app.services.research_engine import build_stock_research
    from app.services.strategy_gate import evaluate_strategy_gate
    from app.services.theme_engine import best_theme_for_stock

    market = production_market_context_as_of(as_of_time)
    market_state_payload = (
        market.get("market_state")
        if isinstance(market.get("market_state"), dict)
        else {}
    )
    if not market.get("ok", True) or not str(market_state_payload.get("state") or ""):
        reason = str(
            market.get("reason") or market.get("status") or "market_context_state_missing"
        )
        blocked = [
            {
                "symbol": symbol,
                "ts_code": normalize_ts_code(symbol),
                "side": "BUY",
                "code": "market_context_pit_unavailable",
                "message": f"市场状态点时证据不可用：{reason}",
                "market_context": market,
            }
            for symbol in target_symbols
        ]
        return [], blocked, {
            "as_of_time": as_of_time,
            "market_context": market,
            "items": [],
            "risk_budget_multiplier": 0.0,
            "cash_target": 1.0,
            "defensive_allocation": defensive_allocation_for_state(
                "unknown",
                as_of_date=str(as_of_time)[:10],
                equity_target_weight=0.0,
            ),
            "status": "blocked",
            "reason": reason,
        }
    market_state = str((market.get("market_state") or {}).get("state") or "unknown")
    market_profile_multiplier = float(
        (market.get("market_profile") or {}).get("risk_budget_multiplier") or 1.0
    )
    sentiment_score = float((market.get("sentiment") or {}).get("score") or 50)
    admitted: List[str] = []
    blocked: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []
    for symbol in target_symbols:
        code = normalize_ts_code(symbol)
        research = build_stock_research(code, persist=True, as_of_time=as_of_time)
        dimensions = research.get("dimensions") or {}
        research_state = research.get("research_state") or {}
        ready_scores = [
            float(value.get("score") or 0)
            for key, value in dimensions.items()
            if key not in {"expectation", "valuation"} and value.get("status") == "ready"
        ]
        research_score = sum(ready_scores) / len(ready_scores) if ready_scores else 0.0
        expectation_score = float((dimensions.get("expectation") or {}).get("score") or 0)
        valuation_score = float((dimensions.get("valuation") or {}).get("score") or 0)
        valuation = (research.get("linked_states") or {}).get("valuation") or {}
        scenarios = valuation.get("scenarios") or {}
        current_price = float(valuation.get("current_price") or 0)
        valuation_upside = (
            float(scenarios.get("base")) / current_price - 1.0
            if current_price > 0 and scenarios.get("base") is not None
            else None
        )
        theme = best_theme_for_stock(code, as_of_time=as_of_time)
        gate = evaluate_strategy_gate(
            research_score=research_score,
            confidence=float(research_state.get("confidence") or 0),
            data_quality=100.0 if research.get("risk", {}).get("eligible") else 0.0,
            risk_eligible=bool(research.get("risk", {}).get("eligible")),
            market_state=market_state,
            sentiment_score=sentiment_score,
            expectation_score=expectation_score,
            valuation_score=valuation_score,
            valuation_upside=valuation_upside,
            theme_score=float((theme or {}).get("score") or 0),
            theme_verified=bool(theme and theme.get("promotable")),
            change_alarm_probability=float(
                (market.get("change_alarm") or {}).get("p_change_1d") or 0
            ),
            market_profile_multiplier=market_profile_multiplier,
        )
        item = {
            "symbol": symbol,
            "ts_code": code,
            "gate": gate,
            "theme": theme,
            "risk": research.get("risk") or {},
        }
        items.append(item)
        if gate.get("admitted"):
            admitted.append(symbol)
        else:
            blocked.append({
                "symbol": symbol,
                "ts_code": code,
                "side": "BUY",
                "code": "dynamic_strategy_gate_failed",
                "message": "；".join(
                    str(item) for item in gate.get("blockers") or ["动态策略门控未通过"]
                ),
                "gate": gate,
            })
    multipliers = [
        float(item["gate"].get("risk_budget_multiplier") or 0)
        for item in items
        if item["gate"].get("admitted")
    ]
    equity_target_weight = min(multipliers) if multipliers else 0.0
    summary = {
        "as_of_time": as_of_time,
        "market_context": market,
        "items": items,
        "risk_budget_multiplier": equity_target_weight,
        "cash_target": max(
            (float(item["gate"].get("cash_target") or 0) for item in items),
            default=1.0,
        ),
        "defensive_allocation": defensive_allocation_for_state(
            market_state,
            as_of_date=str(as_of_time)[:10],
            equity_target_weight=equity_target_weight,
        ),
    }
    return admitted, blocked, summary


def frozen_research_evidence(
    symbol: str,
    *,
    as_of_time: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build replay-safe research and valuation evidence for a trade decision."""
    from app.services.research_engine import build_stock_research, build_valuation_state
    from app.services.thesis_store import list_theses

    code = normalize_ts_code(symbol)
    cutoff = normalize_explicit_timestamp(as_of_time)
    research = build_stock_research(code, persist=True, as_of_time=as_of_time)
    valuation = build_valuation_state(code, persist=True, as_of_time=as_of_time)

    def visible_timestamp(value: Any) -> bool:
        normalized = normalize_explicit_timestamp(value)
        return bool(cutoff and normalized and normalized <= cutoff)

    theses = [
        item
        for item in list_theses(ts_code=code, limit=20)
        if visible_timestamp(item.get("created_at"))
    ]
    thesis = next(
        (
            item
            for item in theses
            if item.get("derived_status") not in {"invalidated", "failed"}
        ),
        theses[0] if theses else None,
    )
    if thesis:
        thesis = dict(thesis)
        thesis["hypotheses"] = [
            {
                **hypothesis,
                "evidence": [
                    evidence
                    for evidence in hypothesis.get("evidence") or []
                    if visible_timestamp(evidence.get("checked_at"))
                ],
            }
            for hypothesis in thesis.get("hypotheses") or []
            if visible_timestamp(hypothesis.get("created_at"))
        ]
    research_payload = {
        "ts_code": code,
        "as_of_time": as_of_time,
        "profile": research.get("profile"),
        "classification": research.get("classification"),
        "risk": research.get("risk"),
        "dimensions": research.get("dimensions"),
        "research_state": research.get("research_state"),
        "raw_metrics": research.get("raw_metrics"),
        "evidence": research.get("evidence"),
        "thesis": None
        if not thesis
        else {
            "id": thesis.get("id"),
            "title": thesis.get("title"),
            "thesis_type": thesis.get("thesis_type"),
            "derived_status": thesis.get("derived_status"),
            "derived_confidence": thesis.get("derived_confidence"),
            "hypotheses": thesis.get("hypotheses"),
        },
    }
    valuation_payload = {
        key: valuation.get(key)
        for key in (
            "ts_code",
            "status",
            "method",
            "trade_date",
            "current_price",
            "current_multiple",
            "history_percentile",
            "industry_percentile",
            "scenarios",
            "dimension",
            "evidence",
            "limitations",
        )
    }
    valuation_payload["as_of_time"] = as_of_time
    return research_payload, valuation_payload
