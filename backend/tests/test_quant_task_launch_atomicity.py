from __future__ import annotations

import threading
from contextlib import nullcontext
from datetime import datetime

from app.services import production_strategy
from app.services import quant_task_runner as runner
from app.db import store
from app.data_platform.services.market_calendar import TradeCalendarNotReady, target_trade_date


def test_execution_scheduler_fails_closed_when_exchange_calendar_cannot_be_read(monkeypatch):
    class BrokenConnection:
        def __enter__(self):
            raise RuntimeError("calendar database unavailable")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(runner, "data_conn", lambda: BrokenConnection())

    assert runner._is_exchange_trade_day(datetime(2026, 8, 17, 16, 30)) is False


def test_target_trade_date_does_not_guess_weekday_when_calendar_is_empty():
    class EmptyCursor:
        def fetchone(self):
            return {"value": None}

    class EmptyConnection:
        def execute(self, *_args, **_kwargs):
            return EmptyCursor()

    try:
        target_trade_date(EmptyConnection(), now=datetime(2026, 8, 17, 16, 30))
    except TradeCalendarNotReady as exc:
        assert exc.code == "calendar_not_ready"
    else:  # pragma: no cover - assertion below reports the contract break
        raise AssertionError("empty exchange calendar must not be converted to a weekday")


def test_run_quant_task_async_reserves_task_before_starting_worker(monkeypatch):
    """A second launch request must lose before either worker can run.

    The pre-start window is the dangerous one: checking ``_is_running`` and
    only marking it inside the worker lets two scheduler ticks each create a
    thread.  The implementation must reserve the task synchronously before
    ``Thread.start``.
    """
    task_id = 9001
    starts: list[str] = []

    class DeferredThread:
        def __init__(self, *, target, name=None, **_kwargs):
            self.target = target
            self.name = name

        def start(self):
            starts.append(str(self.name))
            # Deliberately do not enter the worker.  A concurrent scheduler
            # call during this interval must still see the reservation.

    monkeypatch.setattr(runner.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        runner,
        "get_quant_run_task",
        lambda requested_id: {"id": requested_id, "status": "pending"},
    )
    monkeypatch.setattr(runner, "claim_quant_run_task_start", lambda *_args, **_kwargs: True)
    with runner._task_lock:
        runner._running_task_ids.discard(task_id)

    try:
        assert runner.run_quant_task_async(task_id) is True
        assert runner.run_quant_task_async(task_id) is False
        assert starts == [f"quant-task-{task_id}"]
    finally:
        with runner._task_lock:
            runner._running_task_ids.discard(task_id)


def test_ensure_autonomous_simulation_task_concurrent_calls_reuse_one_creation(monkeypatch):
    """Concurrent startup hooks must converge on one autonomous task record."""
    state_lock = threading.Lock()
    task_ids: list[int] = []

    champion = {
        "strategy_id": 7,
        "champion_version": "champion-v1",
        "strategy": {"content": {"rebalance": {"top_n": 12}}},
        "strategy_policy": {},
    }

    def active_tasks():
        with state_lock:
            rows = [
                {
                    "id": task_id,
                    "status": "pending",
                    "strategy_id": 7,
                    "top_n": 12,
                    "replay_mode": "autonomous_daily",
                }
                for task_id in task_ids
            ]
        return rows

    def get_task(task_id):
        return {
            "id": task_id,
            "status": "pending",
            "strategy_id": 7,
            "top_n": 12,
            "replay_mode": "autonomous_daily",
        }

    monkeypatch.setattr(production_strategy, "resolve_production_strategy_champion", lambda: champion)
    monkeypatch.setattr(runner, "list_active_quant_run_tasks", active_tasks)
    monkeypatch.setattr(runner, "list_consolidated_trade_dates", lambda: ["20260813"])
    def create_autonomous_task(**_kwargs):
        with state_lock:
            if task_ids:
                return task_ids[0], False
            next_id_local = 72
            task_ids.append(next_id_local)
            return next_id_local, True

    monkeypatch.setattr(runner, "create_or_reuse_autonomous_quant_run_task", create_autonomous_task)
    monkeypatch.setattr(runner, "get_quant_run_task", get_task)
    monkeypatch.setattr(runner, "update_quant_run_task", lambda *_args, **_kwargs: True)

    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def invoke():
        try:
            barrier.wait(timeout=2)
            results.append(runner.ensure_autonomous_simulation_task())
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert len(results) == 2
    assert task_ids == [72]
    assert {result["task"]["id"] for result in results} == {72}
    assert sum(bool(result["created"]) for result in results) == 1


def test_postgres_autonomous_creation_locks_before_select(monkeypatch):
    calls: list[tuple[str, tuple]] = []

    class Cursor:
        rowcount = 1

        def fetchone(self):
            return None

    class Connection:
        _is_postgres = True
        _is_mysql = True

        def execute(self, sql, params=()):
            calls.append((str(sql), tuple(params or ())))
            return Cursor()

        def last_insert_id(self):
            return 991

    monkeypatch.setattr(store, "_conn", lambda: nullcontext(Connection()))

    task_id, created = store.create_or_reuse_autonomous_quant_run_task(
        strategy_id=7,
        top_n=12,
        budget_ratio=1.0,
    )

    assert (task_id, created) == (991, True)
    assert "pg_advisory_xact_lock" in calls[0][0]
    assert calls[0][1] == (1095522645,)
    assert "SELECT id FROM quant_run_tasks" in calls[1][0]


def test_quant_task_claim_is_conditional_and_reports_loser(monkeypatch):
    class Cursor:
        def __init__(self, rowcount):
            self.rowcount = rowcount

    class Connection:
        rowcount = 1
        _is_postgres = True
        _is_mysql = True

        def __init__(self):
            self.next_rowcount = 1
            self.sql = ""
            self.params = ()

        def execute(self, sql, params=()):
            self.sql = str(sql)
            self.params = tuple(params or ())
            return Cursor(self.next_rowcount)

    connection = Connection()
    monkeypatch.setattr(store, "_conn", lambda: nullcontext(connection))

    assert store.claim_quant_run_task_start(991, replay_status="planning") is True
    assert "status IN ('pending', 'paused')" in connection.sql
    assert "COALESCE(stop_requested, 0) = 0" in connection.sql

    connection.next_rowcount = 0
    assert store.claim_quant_run_task_start(991, replay_status="planning") is False
