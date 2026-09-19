from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.services import console_snapshots as snapshots


def test_business_503_becomes_degraded_section_and_commits(monkeypatch):
    monkeypatch.setattr(
        snapshots,
        "_readers",
        lambda _page: {
            "decision": lambda: (_ for _ in ()).throw(
                HTTPException(
                    status_code=503,
                    detail={
                        "code": "position_market_data_not_ready",
                        "trade_date": "20260917",
                        "missing_symbols": ["600000.SH"],
                        "message": "target session is incomplete",
                    },
                )
            ),
            "stable": lambda: {"ok": True, "items": [1]},
        },
    )
    monkeypatch.setattr(
        snapshots,
        "_ensure_snapshot_record",
        lambda _page: {
            "page_key": "decision",
            "version": "old",
            "status": "ready",
            "invalidated_at": None,
            "refresh_generation": 7,
        },
    )
    saved: list[tuple[object, ...]] = []
    failed: list[tuple[object, ...]] = []
    monkeypatch.setattr(snapshots, "_save_durable", lambda *args: saved.append(args) or True)
    monkeypatch.setattr(snapshots, "_record_failed_build", lambda *args: failed.append(args) or True)

    payload = snapshots.build("decision")

    assert payload["status"] == "degraded"
    assert payload["data"]["decision"]["status"] == "degraded"
    assert payload["data"]["decision"]["error"]["code"] == "position_market_data_not_ready"
    assert payload["data"]["decision"]["error"]["retryable"] is True
    assert payload["sections"]["decision"]["kind"] == "business_data_block"
    assert len(saved) == 1
    assert failed == []


def test_worker_runtime_status_distinguishes_expired_heartbeat(monkeypatch):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        snapshots,
        "_read_worker_runtime",
        lambda: {
            "worker_key": "console_snapshot_worker",
            "status": "running",
            "heartbeat_at": (now - timedelta(seconds=181)).isoformat(),
        },
    )

    result = snapshots.worker_runtime_status(now=now)

    assert result["status"] == "worker_unavailable"
    assert result["healthy"] is False
    assert result["reason"] == "snapshot_worker_heartbeat_expired"


def test_worker_runtime_status_distinguishes_live_but_degraded_worker(monkeypatch):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        snapshots,
        "_read_worker_runtime",
        lambda: {
            "worker_key": "console_snapshot_worker",
            "status": "degraded",
            "heartbeat_at": (now - timedelta(seconds=3)).isoformat(),
            "last_error_code": "snapshot_build_unavailable",
        },
    )

    result = snapshots.worker_runtime_status(now=now)

    assert result["status"] == "degraded"
    assert result["operational_status"] == "degraded"
    assert result["degraded"] is True
    assert result["heartbeat_healthy"] is True
    assert result["live"] is True
    assert result["healthy"] is False
    assert result["reason"] == "snapshot_build_unavailable"


def test_snapshot_health_reports_live_degraded_worker_even_when_pages_are_ready(monkeypatch):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        snapshots,
        "worker_runtime_status",
        lambda *, now: {
            "status": "degraded",
            "operational_status": "degraded",
            "healthy": False,
            "heartbeat_healthy": True,
            "live": True,
            "degraded": True,
            "reason": "snapshot_build_unavailable",
            "heartbeat_age_seconds": 1,
        },
    )
    rows = [
        {
            "page_key": page_key,
            "version": f"{page_key}-v1",
            "generated_at": now.isoformat(),
            "as_of": now.isoformat(),
            "status": "ready",
            "invalidated_at": None,
            "refresh_generation": 3,
            "last_attempt_at": None,
            "last_error_code": "",
        }
        for page_key in snapshots.PAGE_KEYS
    ]

    class Result:
        def fetchall(self):
            return rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_read_conn", lambda: Connection())

    result = snapshots.snapshot_health(now=now)

    assert result["status"] == "degraded"
    assert result["healthy"] is False
    assert result["degraded"] is True
    assert result["reason"] == "snapshot_build_unavailable"
    assert result["worker"]["heartbeat_healthy"] is True


def test_snapshot_health_reports_degraded_pages_without_losing_worker_signal(monkeypatch):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        snapshots,
        "worker_runtime_status",
        lambda *, now: {
            "status": "ready",
            "healthy": True,
            "heartbeat_age_seconds": 1,
        },
    )
    rows = [
        {
            "page_key": "overview",
            "version": "overview-v1",
            "generated_at": now.isoformat(),
            "as_of": now.isoformat(),
            "status": "degraded",
            "invalidated_at": None,
            "refresh_generation": 3,
            "last_attempt_at": None,
            "last_error_code": "",
        },
    ]

    class Result:
        def fetchall(self):
            return rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_read_conn", lambda: Connection())

    result = snapshots.snapshot_health(now=now)

    assert result["status"] == "stale"
    assert result["ok"] is False
    assert result["pages"]["overview"]["status"] == "degraded"
    assert result["summary"]["unavailable"] == len(snapshots.PAGE_KEYS) - 1


def test_snapshot_health_does_not_treat_normal_refresh_due_as_worker_failure(monkeypatch):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        snapshots,
        "worker_runtime_status",
        lambda *, now: {"status": "ready", "healthy": True, "heartbeat_age_seconds": 1},
    )
    rows = [
        {
            "page_key": page_key,
            "version": f"{page_key}-v1",
            "generated_at": (now - timedelta(seconds=snapshots._ttl(page_key) + 1)).isoformat(),
            "as_of": now.isoformat(),
            "status": "ready",
            "invalidated_at": None,
            "refresh_generation": 3,
            "last_attempt_at": None,
            "last_error_code": "",
        }
        for page_key in snapshots.PAGE_KEYS
    ]

    class Result:
        def fetchall(self):
            return rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(snapshots, "_ensure_table", lambda: None)
    monkeypatch.setattr(snapshots, "_read_conn", lambda: Connection())

    result = snapshots.snapshot_health(now=now)

    assert result["status"] == "ready"
    assert result["healthy"] is True
    assert result["pages"]["runtime"]["fresh"] is False
    assert result["pages"]["runtime"]["refresh_due"] is True
    assert result["pages"]["runtime"]["status"] == "ready"


def test_read_exposes_worker_refresh_availability(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    metadata = {
        "page_key": "models",
        "version": "models-v1",
        "generated_at": now,
        "status": "ready",
        "invalidated_at": None,
        "refresh_generation": 2,
    }
    cached = {
        "page": "models",
        "version": "models-v1",
        "generated_at": now,
        "status": "ready",
        "refresh_generation": 2,
        "data": {},
    }
    monkeypatch.setattr(snapshots, "_snapshot_metadata", lambda _page: metadata)
    monkeypatch.setattr(snapshots, "get_json", lambda _key: cached)
    monkeypatch.setattr(
        snapshots,
        "_cached_worker_runtime_status",
        lambda: {"status": "worker_unavailable", "healthy": False, "reason": "snapshot_worker_heartbeat_expired"},
    )

    result = snapshots.read("models")

    assert result["refresh_available"] is False
    assert result["snapshot_worker_status"] == "worker_unavailable"
    assert result["worker_status"] == "worker_unavailable"
    assert result["refresh_error_code"] == "snapshot_worker_heartbeat_expired"
    assert result["refresh_retry_after_seconds"] == snapshots._failure_retry_seconds()


def test_worker_loop_persists_lifecycle_around_degraded_build(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        snapshots,
        "record_worker_heartbeat",
        lambda **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(snapshots, "_pages_due_for_refresh", lambda: ["decision"])
    monkeypatch.setattr(snapshots, "_snapshot_build_should_yield", lambda _page: False)
    monkeypatch.setattr(
        snapshots,
        "_build_with_heartbeat_pulse",
        lambda _page: {"status": "degraded"},
    )

    class StopAfterBuild:
        def is_set(self):
            return False

        def wait(self, _seconds):
            return True

    snapshots.run_snapshot_worker_forever(StopAfterBuild())

    assert events[0]["status"] == "starting"
    assert any(item.get("current_page") == "decision" for item in events)
    assert any(item.get("last_success_page") == "decision" for item in events)
    assert events[-1]["status"] == "stopped"
