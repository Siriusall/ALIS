from app.services import decision_store, market_context, quant_run_engine, research_engine, research_workflow, theme_engine, thesis_store


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
