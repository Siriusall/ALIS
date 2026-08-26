from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.db import store
from app.services import profit_loop, quant_run_engine, quant_task_runner


def _task():
    return {
        "strategy_id": 3,
        "top_n": 10,
        "budget_ratio": 1.0,
        "exchange_group": "HS",
        "replay_mode": "autonomous_daily",
    }


def test_formal_plan_ledger_and_terminal_status_share_one_atomic_scope(monkeypatch):
    events = []

    @contextmanager
    def atomic_scope():
        events.append("transaction_enter")
        try:
            yield
        except Exception:
            events.append("transaction_rollback")
            raise
        else:
            events.append("transaction_commit")

    monkeypatch.setattr(store, "atomic_business_transaction", atomic_scope)
    monkeypatch.setattr(
        profit_loop,
        "claim_daily_rebalance_execution_input",
        lambda *_args, **_kwargs: {"execution_claim_token": "quant-task:8:20260810:20260811"},
    )

    def run_in_engine_scope(**kwargs):
        with store.atomic_business_transaction():
            events.append("ledger_mutated")
            kwargs["finalize_execution"](91)
        return SimpleNamespace(execution_id=91)

    monkeypatch.setattr(
        quant_task_runner,
        "run_for_trade_date",
        run_in_engine_scope,
    )
    monkeypatch.setattr(
        profit_loop,
        "mark_daily_rebalance_plan_executed",
        lambda *_args, **_kwargs: events.append("plan_executed"),
    )

    result = quant_task_runner._execute_step_with_retry(
        task_id=8,
        task_live=_task(),
        signal_date="20260810",
        execute_date="20260811",
        progress_callback=lambda _phase: None,
        scoring_timeout_sec=60,
    )

    assert result.execution_id == 91
    assert events == [
        "transaction_enter",
        "ledger_mutated",
        "plan_executed",
        "transaction_commit",
    ]


def test_plan_terminal_write_failure_rolls_back_ledger_scope(monkeypatch):
    events = []

    @contextmanager
    def atomic_scope():
        events.append("transaction_enter")
        try:
            yield
        except Exception:
            events.append("transaction_rollback")
            raise
        else:
            events.append("transaction_commit")

    monkeypatch.setattr(store, "atomic_business_transaction", atomic_scope)
    monkeypatch.setattr(
        profit_loop,
        "claim_daily_rebalance_execution_input",
        lambda *_args, **_kwargs: {"execution_claim_token": "quant-task:8:20260810:20260811"},
    )

    def run_in_engine_scope(**kwargs):
        with store.atomic_business_transaction():
            events.append("ledger_mutated")
            kwargs["finalize_execution"](91)
        return SimpleNamespace(execution_id=91)

    monkeypatch.setattr(
        quant_task_runner,
        "run_for_trade_date",
        run_in_engine_scope,
    )

    def fail_plan_write(*_args, **_kwargs):
        events.append("plan_write_failed")
        raise RuntimeError("lost plan lease")

    monkeypatch.setattr(profit_loop, "mark_daily_rebalance_plan_executed", fail_plan_write)

    with pytest.raises(RuntimeError, match="lost plan lease"):
        quant_task_runner._execute_step_with_retry(
            task_id=8,
            task_live=_task(),
            signal_date="20260810",
            execute_date="20260811",
            progress_callback=lambda _phase: None,
            scoring_timeout_sec=60,
        )

    assert events == [
        "transaction_enter",
        "ledger_mutated",
        "plan_write_failed",
        "transaction_rollback",
    ]


def test_frozen_sell_preparation_does_not_mutate_account(monkeypatch):
    mutations = []
    blocked = []
    triggers = []
    prepared = []
    monkeypatch.setattr(
        quant_run_engine,
        "sell_position",
        lambda *_args, **_kwargs: mutations.append((_args, _kwargs)),
    )

    sold, remaining, code = quant_run_engine._sell_position_for_rebalance(
        pos={
            "id": 7,
            "symbol": "000001",
            "quantity": 500,
            "name": "测试股票",
            "bought_at": "2026-08-20",
        },
        quantity=100,
        price_row=SimpleNamespace(
            open=10.0,
            prev_close=10.0,
            change_pct=0.0,
            volume=100_000,
        ),
        execute_date="2026-08-26",
        executed_at="2026-08-26T09:30:00",
        reason="冻结卖出",
        detail="准备阶段",
        turnover_remaining=100_000,
        hard_risk_exit=False,
        blocked_events=blocked,
        sell_triggers=triggers,
        sold_ops_payload=prepared,
        persist=False,
    )

    assert sold is True
    assert code is None
    assert remaining < 100_000
    assert mutations == []
    assert prepared[0]["symbol"] == "000001"
    assert prepared[0]["quantity"] == 100


def test_frozen_order_audit_uses_autocommit_read_scope(monkeypatch):
    scopes = []

    @contextmanager
    def read_scope():
        scopes.append("read_enter")
        yield object()
        scopes.append("read_exit")

    monkeypatch.setattr(profit_loop, "data_read_conn", read_scope)
    monkeypatch.setattr(
        profit_loop,
        "data_conn",
        lambda: (_ for _ in ()).throw(AssertionError("write transaction must not be opened")),
    )
    monkeypatch.setattr(profit_loop, "_raw_record_for_execution", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(profit_loop, "_snapshot_record_for_execution", lambda *_args, **_kwargs: {})

    result = profit_loop.audit_frozen_execution_orders(
        {
            "execution_orders": [{
                "symbol": "000001",
                "side": "BUY",
                "planned_quantity": 100,
            }],
        },
        "20260826",
        observed_at="2026-08-26T09:30:00+08:00",
    )

    assert scopes == ["read_enter", "read_exit"]
    assert result["ok"] is False


def test_account_snapshot_fingerprint_detects_non_sell_position_change():
    prepared = [{
        "id": 1,
        "symbol": "000001",
        "quantity": 500,
        "price": 10.0,
        "bought_at": "2026-08-20",
    }, {
        "id": 2,
        "symbol": "600000",
        "quantity": 300,
        "price": 8.0,
        "bought_at": "2026-08-19",
    }]
    current = [dict(item) for item in prepared]
    current[1]["quantity"] = 400

    assert (
        quant_run_engine._account_position_fingerprint(current)
        != quant_run_engine._account_position_fingerprint(prepared)
    )


def test_not_due_formal_plan_is_not_quarantined(monkeypatch):
    blocked = []

    def not_due(*_args, **_kwargs):
        raise quant_task_runner.ReplayRiskGateError(
            "正式调仓计划尚未到达冻结执行时刻", code="formal_plan_not_due",
        )

    monkeypatch.setattr(profit_loop, "claim_daily_rebalance_execution_input", not_due)
    monkeypatch.setattr(profit_loop, "block_daily_rebalance_plan_contract", lambda *_args, **_kwargs: blocked.append(True))

    with pytest.raises(quant_task_runner.ReplayRiskGateError) as captured:
        quant_task_runner._execute_step_with_retry(
            task_id=8,
            task_live=_task(),
            signal_date="20260810",
            execute_date="20260811",
            progress_callback=lambda _phase: None,
            scoring_timeout_sec=60,
        )

    assert captured.value.code == "formal_plan_not_due"
    assert blocked == []


def test_frozen_order_data_gap_is_retryable_and_never_quarantined(monkeypatch):
    blocked = []
    released = []

    def data_gap(*_args, **_kwargs):
        error = quant_task_runner.ReplayRiskGateError(
            "冻结计划订单行情未就绪：000001",
            code="paused_frozen_execution_order_data_not_ready",
        )
        error.execution_data_evidence = {
            "ok": False,
            "missing": ["000001"],
            "next_probe_at": "2026-08-14T16:10:00+08:00",
        }
        raise error

    monkeypatch.setattr(profit_loop, "claim_daily_rebalance_execution_input", data_gap)
    monkeypatch.setattr(
        profit_loop,
        "block_daily_rebalance_plan_contract",
        lambda *_args, **_kwargs: blocked.append(True),
    )

    with pytest.raises(quant_task_runner.ReplayRiskGateError) as captured:
        quant_task_runner._execute_step_with_retry(
            task_id=8,
            task_live=_task(),
            signal_date="20260813",
            execute_date="20260814",
            progress_callback=lambda _phase: None,
            scoring_timeout_sec=60,
        )

    assert captured.value.code == "paused_frozen_execution_order_data_not_ready"
    assert blocked == []


def test_engine_second_gate_blocks_before_direct_execution_when_order_slice_missing(monkeypatch):
    events = []

    monkeypatch.setattr(
        profit_loop,
        "audit_frozen_execution_orders",
        lambda *_args, **_kwargs: {
            "ok": False,
            "missing": ["000001"],
            "orders": [{"symbol": "000001", "ready": False, "missing_fields": ["daily.open"]}],
            "next_probe_at": "2026-08-14T16:10:00+08:00",
        },
    )

    contract = {
        "schema_version": "alis_unified_daily_decision_v2",
        "data_as_of_time": "2026-08-13T15:00:00+08:00",
        "selection_snapshot": {"strategy_id": 3},
        "signal_date": "20260813",
        "planned_execute_date": "20260814",
        "execution_orders": [{"symbol": "000001", "side": "BUY", "planned_quantity": 100}],
    }
    normalized = {
        "is_unified_frozen_plan": True,
        "target_symbols": ["000001"],
        "target_weights": {"000001": 0.1},
        "pretrade_controller": {},
        "decision_contract": contract,
    }
    class DataSource:
        def set_as_of_time(self, _value):
            return None

        def load_snapshot(self, _value):
            events.append("market_read")
            return []

    monkeypatch.setattr(quant_task_runner, "run_for_trade_date", lambda **_kwargs: events.append("ledger_mutated"))
    monkeypatch.setattr("app.services.quant_run_engine.BacktestDataSource", DataSource)

    # The direct engine gate runs after the authoritative normalization. This
    # focused assertion exercises the shared gate contract without requiring a
    # full production decision contract fixture.
    from app.services import quant_run_engine
    monkeypatch.setattr(quant_run_engine, "normalize_authoritative_plan", lambda _plan: normalized)
    monkeypatch.setattr(quant_run_engine, "_require_consistent_simulation_ledger", lambda: None)
    monkeypatch.setattr(quant_run_engine, "champion_execution_gate", lambda: {"status": "active"})

    with pytest.raises(quant_task_runner.ReplayRiskGateError) as captured:
        quant_run_engine.run_for_trade_date(
            strategy_id=3,
            signal_date="20260813",
            execute_date="20260814",
            top_n=10,
            budget_ratio=1.0,
            authoritative_plan={
                "plan_id": 1,
                "plan_version": "20260813-v1",
                "input_hash": "x",
                "execution_claim_token": "token",
                **normalized,
            },
        )

    assert captured.value.code == "paused_frozen_execution_order_data_not_ready"
    assert events == []


def test_engine_rejects_missing_observation_cutoff_before_order_audit(monkeypatch):
    audit_calls = []
    normalized = {
        "is_unified_frozen_plan": True,
        "target_symbols": ["000001"],
        "target_weights": {"000001": 0.1},
        "pretrade_controller": {},
        "decision_contract": {
            "schema_version": "alis_unified_daily_decision_v2",
            "execution_orders": [{"symbol": "000001", "side": "BUY", "planned_quantity": 100}],
        },
    }
    from app.services import quant_run_engine

    monkeypatch.setattr(quant_run_engine, "normalize_authoritative_plan", lambda _plan: normalized)
    monkeypatch.setattr(quant_run_engine, "_require_consistent_simulation_ledger", lambda: None)
    monkeypatch.setattr(quant_run_engine, "champion_execution_gate", lambda: {"status": "active"})
    monkeypatch.setattr(
        profit_loop,
        "audit_frozen_execution_orders",
        lambda *_args, **_kwargs: audit_calls.append(True) or {"ok": True},
    )

    with pytest.raises(quant_task_runner.ReplayRiskGateError) as captured:
        quant_run_engine.run_for_trade_date(
            strategy_id=3,
            signal_date="20260813",
            execute_date="20260814",
            top_n=10,
            budget_ratio=1.0,
            authoritative_plan={
                "plan_id": 1,
                "plan_version": "20260813-v1",
                "input_hash": "x",
                "execution_claim_token": "token",
                **normalized,
            },
        )

    assert captured.value.code == "invalid_observation_cutoff"
    assert audit_calls == []
