from __future__ import annotations

from types import SimpleNamespace

from app.services import simulation_ledger


class _UnexpectedNumericError:
    def __float__(self):
        raise RuntimeError("unexpected numeric failure")


def test_simulation_ledger_exposes_operations_fees_and_task_context(monkeypatch):
    monkeypatch.setattr(simulation_ledger, "list_executions", lambda limit=20: [
        {"id": 11, "strategy_id": 1, "executed_at": "2026-07-15T09:30:00", "orders": [{"symbol": "000001", "status": "filled"}]},
    ])
    monkeypatch.setattr(simulation_ledger, "get_execution", lambda execution_id: None)
    monkeypatch.setattr(simulation_ledger, "list_recent_quant_run_task_records", lambda limit=100: [
        {"id": 5, "task_id": 3, "signal_date": "2026-07-14", "execute_date": "2026-07-15", "execution_id": 11, "message": "ok"},
    ])
    monkeypatch.setattr(simulation_ledger, "list_quant_run_task_records", lambda task_id, limit=5000: [
        {
            "id": 5,
            "detail": {
                "strategy_template": {"id": "sideways_low_vol_v1"},
                "target_positions": [{"symbol": "000001", "target_weight": 0.1}],
                "current_positions": [],
                "rebalance_actions": {"blocked": [{"symbol": "000002", "code": "risk_gate"}]},
            },
        },
    ])
    monkeypatch.setattr(simulation_ledger, "list_operations", lambda execution_id, limit=5000: [
        {
            "id": 21, "execution_id": 11, "symbol": "000001", "side": "BUY", "price": 10.0,
            "quantity": 100, "at": "2026-07-15T09:30:00", "commission": 5.0,
            "transfer_fee": 0.1, "stamp_tax": 0.0, "slippage_bps": 10.0,
            "fee_schedule_version": "cn_a_share_fee_v1", "operation_uid": "abc",
        },
    ])

    class FakeDS:
        def load_snapshot(self, date_ymd):
            return [SimpleNamespace(symbol="000001", close=10.5)]

    monkeypatch.setattr(simulation_ledger, "BacktestDataSource", FakeDS)

    result = simulation_ledger.build_simulation_ledger(limit=5)
    item = result["items"][0]
    op = item["operations"][0]

    assert result["schema_version"] == "simulation_ledger_v1"
    assert item["source"] == "quant_task"
    assert item["strategy_version"] == "sideways_low_vol_v1"
    assert item["blocked_signals"][0]["code"] == "risk_gate"
    assert op["fees"]["total"] == 5.1
    assert op["assumption"]["slippage_bps"] == 10.0
    assert op["mark_to_market_pnl"] == 50.0
    assert item["links"]["task_run_detail"].endswith("execute_date=2026-07-15")
    assert result["evidence_warnings"] == []


def test_numeric_parsing_only_tolerates_conversion_errors():
    assert simulation_ledger._safe_float("invalid") == 0.0
    assert simulation_ledger._safe_int("invalid") == 0

    try:
        simulation_ledger._safe_float(_UnexpectedNumericError())
    except RuntimeError as exc:
        assert str(exc) == "unexpected numeric failure"
    else:
        raise AssertionError("unexpected numeric errors must remain visible")


def test_simulation_ledger_exposes_optional_evidence_failures(monkeypatch):
    monkeypatch.setattr(simulation_ledger, "list_executions", lambda limit=20: [
        {"id": 11, "strategy_id": 1, "executed_at": "2026-07-15T09:30:00", "orders": []},
    ])
    monkeypatch.setattr(simulation_ledger, "list_recent_quant_run_task_records", lambda limit=100: [
        {"id": 5, "task_id": 3, "signal_date": "2026-07-14", "execute_date": "2026-07-15", "execution_id": 11},
    ])
    monkeypatch.setattr(
        simulation_ledger,
        "list_quant_run_task_records",
        lambda task_id, limit=5000: (_ for _ in ()).throw(RuntimeError("task lookup failed")),
    )
    monkeypatch.setattr(simulation_ledger, "list_operations", lambda execution_id, limit=5000: [])

    class FailingDS:
        def load_snapshot(self, date_ymd):
            raise RuntimeError("snapshot lookup failed")

    monkeypatch.setattr(simulation_ledger, "BacktestDataSource", FailingDS)

    result = simulation_ledger.build_simulation_ledger(limit=5)

    assert result["ok"] is True
    assert [warning["code"] for warning in result["evidence_warnings"]] == [
        "task_context_unavailable",
        "execution_snapshot_unavailable",
    ]
    assert result["items"][0]["evidence_warnings"] == result["evidence_warnings"]
