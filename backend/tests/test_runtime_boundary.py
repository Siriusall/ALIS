from app.services.runtime_boundary import (
    BACKGROUND_RUNTIME_LOCK_ID,
    acquire_background_runtime_lease,
)


class _Result:
    def __init__(self, acquired: bool):
        self.acquired = acquired

    def fetchone(self):
        return {"acquired": self.acquired}


class _Connection:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.closed = False
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            return _Result(self.acquired)
        return _Result(True)

    def close(self):
        self.closed = True


def test_disabled_runtime_never_connects():
    def unexpected_connect(*args, **kwargs):
        raise AssertionError("disabled process must not open a lease connection")

    lease = acquire_background_runtime_lease(mode="disabled", connect=unexpected_connect)

    assert lease.should_start is False
    assert lease.reason == "explicitly_disabled"


def test_only_advisory_lock_owner_starts_background_runtime():
    connection = _Connection(acquired=True)
    lease = acquire_background_runtime_lease(
        mode="enabled",
        database_url="postgresql+psycopg://example/db",
        connect=lambda *args, **kwargs: connection,
    )

    assert lease.should_start is True
    assert lease.reason == "acquired"
    assert connection.calls[0] == (
        "SELECT pg_try_advisory_lock(%s) AS acquired",
        (BACKGROUND_RUNTIME_LOCK_ID,),
    )

    lease.release()

    assert connection.closed is True
    assert lease.reason == "released"
    assert "pg_advisory_unlock" in connection.calls[-1][0]


def test_non_owner_remains_web_only_and_closes_connection():
    connection = _Connection(acquired=False)
    lease = acquire_background_runtime_lease(
        mode="enabled",
        connect=lambda *args, **kwargs: connection,
    )

    assert lease.should_start is False
    assert lease.reason == "held_by_another_process"
    assert connection.closed is True


def test_web_only_startup_skips_every_background_side_effect(monkeypatch):
    from app import main

    calls = []
    disabled = acquire_background_runtime_lease(mode="disabled")
    monkeypatch.setattr(main, "acquire_background_runtime_lease", lambda: disabled)
    monkeypatch.setattr(
        main,
        "ensure_production_strategy_champion",
        lambda: (_ for _ in ()).throw(AssertionError("background mutation must be skipped")),
    )

    main.startup()

    assert calls == []
    assert main._startup_components["background_runtime_boundary"]["result"]["reason"] == "explicitly_disabled"
