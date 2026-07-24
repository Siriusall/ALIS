import pytest

from app.services import research_workflow


@pytest.fixture(autouse=True)
def complete_research_fields(monkeypatch):
    monkeypatch.setattr(research_workflow, "financial_research_completeness", lambda *_args, **_kwargs: {
        "status": "complete", "coverage": 1.0, "details": [],
    })


def _research(*, eligible=True, ready=3):
    dimensions = {
        f"dimension_{index}": {"status": "ready" if index < ready else "insufficient_data"}
        for index in range(7)
    }
    return {
        "ok": True,
        "risk": {"eligible": eligible, "hard_reasons": [] if eligible else ["重大治理风险"]},
        "dimensions": dimensions,
        "research_state": {"classification": "高质量合理估值"},
    }


def test_core_pool_eligibility_requires_all_plan_gates(monkeypatch):
    monkeypatch.setattr(research_workflow, "build_stock_research", lambda _code, **_kwargs: _research())
    monkeypatch.setattr(research_workflow, "build_valuation_state", lambda _code, **_kwargs: {"ok": True, "status": "ready", "method": "PE", "scenarios": {"base": 12}})
    monkeypatch.setattr(research_workflow, "list_theses", lambda **_kwargs: [{"derived_status": "valid"}])

    result = research_workflow.evaluate_core_pool_eligibility("000001.sz")

    assert result["ts_code"] == "000001.SZ"
    assert result["eligible_for_core"] is True
    assert all(gate["passed"] for gate in result["gates"])


def test_core_pool_eligibility_blocks_missing_thesis_and_valuation(monkeypatch):
    monkeypatch.setattr(research_workflow, "build_stock_research", lambda _code, **_kwargs: _research())
    monkeypatch.setattr(research_workflow, "build_valuation_state", lambda _code, **_kwargs: {"ok": False, "reason": "历史估值不足"})
    monkeypatch.setattr(research_workflow, "list_theses", lambda **_kwargs: [])

    result = research_workflow.evaluate_core_pool_eligibility("000001.SZ")
    gates = {gate["key"]: gate for gate in result["gates"]}

    assert result["eligible_for_core"] is False
    assert gates["valuation"]["passed"] is False
    assert gates["thesis"]["passed"] is False


def test_core_pool_eligibility_blocks_risk_hard_veto(monkeypatch):
    monkeypatch.setattr(research_workflow, "build_stock_research", lambda _code, **_kwargs: _research(eligible=False))
    monkeypatch.setattr(research_workflow, "build_valuation_state", lambda _code, **_kwargs: {"ok": True, "scenarios": {"base": 12}})
    monkeypatch.setattr(research_workflow, "list_theses", lambda **_kwargs: [{"derived_status": "valid"}])

    result = research_workflow.evaluate_core_pool_eligibility("000001.SZ")
    risk_gate = next(gate for gate in result["gates"] if gate["key"] == "risk")

    assert result["eligible_for_core"] is False
    assert risk_gate["detail"] == "重大治理风险"


def test_core_pool_blocks_row_complete_but_field_incomplete_financials(monkeypatch):
    monkeypatch.setattr(research_workflow, "build_stock_research", lambda _code, **_kwargs: _research())
    monkeypatch.setattr(research_workflow, "build_valuation_state", lambda _code, **_kwargs: {"ok": True, "scenarios": {"base": 12}})
    monkeypatch.setattr(research_workflow, "list_theses", lambda **_kwargs: [{"derived_status": "valid"}])
    monkeypatch.setattr(research_workflow, "financial_research_completeness", lambda *_args, **_kwargs: {
        "status": "partial", "coverage": .5, "details": [{"missing_fields": ["inventories"]}],
    })

    result = research_workflow.evaluate_core_pool_eligibility("000001.SZ")

    gate = next(item for item in result["gates"] if item["key"] == "financial_fields")
    assert gate["passed"] is False
    assert result["eligible_for_core"] is False


def test_candidate_workflow_promotes_only_eligible_candidates(monkeypatch):
    transitions = []
    monkeypatch.setattr(research_workflow, "list_pool_members", lambda _pool: [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"}])
    monkeypatch.setattr(research_workflow, "generate_thesis_for_stock", lambda code: {"ok": True, "ts_code": code})
    monkeypatch.setattr(research_workflow, "evaluate_core_pool_eligibility", lambda code: {
        "eligible_for_core": code == "000001.SZ",
        "gates": [{"key": "risk", "passed": code == "000001.SZ"}],
    })
    monkeypatch.setattr(research_workflow, "transition_pool_member", lambda **kwargs: transitions.append(kwargs) or kwargs)

    result = research_workflow.process_watch_pool()

    assert result["candidates"] == 2
    assert [item["ts_code"] for item in result["promoted"]] == ["000001.SZ"]
    assert [item["ts_code"] for item in result["blocked"]] == ["000002.SZ"]
    assert transitions[0]["from_pool"] == "candidate"
    assert transitions[0]["to_pool"] == "core"
    assert "automatic_research_workflow_v2" in transitions[0]["reason"]
