from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services import profit_loop, quant_task_runner
from app.db import store


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
    monkeypatch.setattr(
        quant_task_runner,
        "run_for_trade_date",
        lambda **_kwargs: events.append("ledger_mutated") or SimpleNamespace(execution_id=91),
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
    monkeypatch.setattr(
        quant_task_runner,
        "run_for_trade_date",
        lambda **_kwargs: events.append("ledger_mutated") or SimpleNamespace(execution_id=91),
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
