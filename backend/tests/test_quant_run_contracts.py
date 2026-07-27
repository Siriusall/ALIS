import pytest

from app.services import quant_run_contracts as contracts
from app.services import quant_run_engine


def test_quant_run_engine_preserves_public_contract_imports():
    assert quant_run_engine.HistoricalDataMissingError is contracts.HistoricalDataMissingError
    assert quant_run_engine.ReplayRiskGateError is contracts.ReplayRiskGateError
    assert quant_run_engine.QuantRunExecResult is contracts.QuantRunExecResult


def test_ledger_contract_blocks_mutation_with_a_stable_error(monkeypatch):
    monkeypatch.setattr(
        "app.services.simulation_ledger_audit.audit_simulation_ledger",
        lambda **_kwargs: {"ok": False, "issues": [{"message": "cash drift"}]},
    )
    with pytest.raises(contracts.ReplayRiskGateError) as exc:
        contracts.require_consistent_simulation_ledger()
    assert exc.value.code == "paused_simulation_ledger_drift"
    assert "cash drift" in str(exc.value)
