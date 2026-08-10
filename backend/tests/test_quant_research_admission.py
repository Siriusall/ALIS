from app.services import decision_store, hsmm_shadow, market_context, quant_run_engine, research_engine, research_workflow, theme_engine, thesis_store


def test_model_rank_cannot_bypass_core_research_pool(monkeypatch):
    monkeypatch.setattr(decision_store, "list_pool_members", lambda pool: [{"ts_code": "000001.SZ", "entered_at": "2020-01-01T00:00:00"}] if pool == "core" else [])
    monkeypatch.setattr(research_workflow, "evaluate_core_pool_eligibility", lambda code, as_of_time=None: {"eligible_for_core": code == "000001.SZ", "gates": []})

    admitted, blocked = quant_run_engine._apply_research_admission(["000001", "600000"])

    assert admitted == ["000001"]
    assert blocked[0]["ts_code"] == "600000.SH"
    assert blocked[0]["code"] == "not_in_core_pool"


def test_deteriorated_core_candidate_is_blocked_before_order(monkeypatch):
    monkeypatch.setattr(decision_store, "list_pool_members", lambda _pool: [{"ts_code": "000001.SZ", "entered_at": "2020-01-01T00:00:00"}])
    monkeypatch.setattr(research_workflow, "evaluate_core_pool_eligibility", lambda _code, as_of_time=None: {
        "eligible_for_core": False,
        "gates": [{"label": "风险硬门控", "passed": False}],
    })

    admitted, blocked = quant_run_engine._apply_research_admission(["000001"])

    assert admitted == []
    assert blocked[0]["code"] == "core_evidence_gate_failed"
    assert "风险硬门控" in blocked[0]["message"]


def test_frozen_decision_evidence_excludes_future_thesis(monkeypatch):
    monkeypatch.setattr(research_engine, "build_stock_research", lambda code, **kwargs: {"ts_code": code, "profile": {}, "dimensions": {}, "evidence": [], "as_of_time": kwargs.get("as_of_time")})
    monkeypatch.setattr(research_engine, "build_valuation_state", lambda code, **kwargs: {"ts_code": code, "status": "ready", "scenarios": {"base": 10}})
    monkeypatch.setattr(thesis_store, "list_theses", lambda **_kwargs: [{"id": 9, "created_at": "2026-07-16T10:00:00", "derived_status": "valid"}])

    research, valuation = quant_run_engine._frozen_research_evidence("000001", as_of_time="2025-07-15T23:59:59")

    assert research["ts_code"] == "000001.SZ"
    assert research["thesis"] is None
    assert research["as_of_time"] == "2025-07-15T23:59:59"
    assert valuation["as_of_time"] == "2025-07-15T23:59:59"


def test_dynamic_gate_is_applied_to_research_admitted_candidate(monkeypatch):
    monkeypatch.setattr(theme_engine, "best_theme_for_stock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(market_context, "market_context_as_of", lambda _cutoff: {
        "market_state": {"state": "bear"}, "sentiment": {"score": 40},
    })
    monkeypatch.setattr(research_engine, "build_stock_research", lambda *_args, **_kwargs: {
        "risk": {"eligible": True},
        "research_state": {"confidence": .8},
        "dimensions": {
            "quality": {"status": "ready", "score": 80},
            "growth": {"status": "ready", "score": 75},
            "cashflow": {"status": "ready", "score": 80},
            "expectation": {"status": "ready", "score": 70},
            "valuation": {"status": "ready", "score": 70},
        },
        "linked_states": {"valuation": {"current_price": 10, "scenarios": {"base": 11.2}}},
    })

    admitted, blocked, summary = quant_run_engine._apply_dynamic_strategy_gate(
        ["000001"], as_of_time="2025-07-15T23:59:59"
    )

    assert admitted == []
    assert blocked[0]["code"] == "dynamic_strategy_gate_failed"
    assert summary["risk_budget_multiplier"] == 0


def test_champion_hsmm_state_replaces_transparent_state_at_same_cutoff(monkeypatch):
    monkeypatch.setattr(market_context, "market_context_as_of", lambda _cutoff: {
        "ok": True,
        "market_state": {"state": "S1", "model": "transparent_four_state_v1"},
        "observations": {"breadth": 0.2, "average_return": -0.01, "volume_ratio": 0.8},
        "model_governance": {"production_model": "transparent_four_state_v1"},
    })
    monkeypatch.setattr(hsmm_shadow, "infer_hsmm_state_as_of", lambda binding, *, as_of_time: {
        "ok": True,
        "status": "ready",
        "state": "S2",
        "confidence": 0.79,
        "version_id": binding["id"],
    })

    result = quant_run_engine._production_market_context_as_of(
        "2026-07-24T15:00:00+08:00",
        market_state_binding={
            "production_model": "market_regime_hsmm_v1",
            "change_model": "bocpd_gaussian_v1",
            "hsmm_shadow": {"id": 41},
        },
    )

    assert result["market_state"]["state"] == "S2"
    assert result["market_profile"]["parent_state"] == "S2"
    assert result["market_state_runtime"] == {
        "requested_model": "market_regime_hsmm_v1",
        "effective_model": "market_regime_hsmm_v1",
        "transparent_state": "S1",
        "effective_state": "S2",
        "fallback_reason": "",
        "hsmm_evidence": {
            "ok": True,
            "status": "ready",
            "state": "S2",
            "confidence": 0.79,
            "version_id": 41,
        },
        "hsmm_sidecar": {},
        "hsmm_sidecar_controls_risk_budget": False,
    }


def test_champion_hsmm_failure_keeps_transparent_state(monkeypatch):
    monkeypatch.setattr(market_context, "market_context_as_of", lambda _cutoff: {
        "ok": True,
        "market_state": {"state": "S0", "model": "transparent_four_state_v1"},
        "observations": {},
    })
    monkeypatch.setattr(hsmm_shadow, "infer_hsmm_state_as_of", lambda _binding, *, as_of_time: {
        "ok": False,
        "status": "fallback",
        "reason": "hsmm_version_created_after_cutoff",
    })

    result = quant_run_engine._production_market_context_as_of(
        "2026-07-24T15:00:00+08:00",
        market_state_binding={
            "production_model": "market_regime_hsmm_v1",
            "hsmm_shadow": {"id": 41},
        },
    )

    assert result["market_state"]["state"] == "S0"
    assert result["market_state_runtime"]["effective_model"] == "transparent_four_state_v1"
    assert result["market_state_runtime"]["fallback_reason"] == "hsmm_version_created_after_cutoff"


def test_existing_portfolio_methods_are_selected_without_changing_algorithms(monkeypatch):
    returns = {
        "000001": [0.01, -0.01, 0.02],
        "000002": [0.00, 0.01, -0.01],
    }
    equal, equal_name = quant_run_engine._base_portfolio_weights(
        returns, max_single=0.6, method="equal_weight",
    )
    monkeypatch.setattr(
        quant_run_engine,
        "hrp_fallback_weights",
        lambda values, max_single: {symbol: 0.5 for symbol in values},
    )
    hrp, hrp_name = quant_run_engine._base_portfolio_weights(
        returns, max_single=0.6, method="hrp",
    )

    assert equal == {"000001": 0.5, "000002": 0.5}
    assert equal_name == "equal_weight_baseline_v1"
    assert hrp == {"000001": 0.5, "000002": 0.5}
    assert hrp_name == "hrp_inverse_volatility_v1"
