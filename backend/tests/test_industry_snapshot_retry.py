from __future__ import annotations

import pytest

from app.services import industry_event_engine as service


def test_industry_snapshot_retries_only_statement_timeout(monkeypatch) -> None:
    calls = 0

    def materialize(*, as_of_time=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("canceling statement due to statement timeout")
        return {"ok": True, "written": 111, "ready": 111}

    monkeypatch.setattr(service, "_materialize_industry_state_snapshots_once", materialize)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)

    result = service.materialize_industry_state_snapshots(as_of_time="2026-08-11T15:00:00")

    assert result["query_attempts"] == 2
    assert result["written"] == 111


def test_industry_snapshot_does_not_retry_contract_failure(monkeypatch) -> None:
    calls = 0

    def materialize(*, as_of_time=None):
        nonlocal calls
        calls += 1
        raise ValueError("invalid industry contract")

    monkeypatch.setattr(service, "_materialize_industry_state_snapshots_once", materialize)

    with pytest.raises(ValueError, match="invalid industry contract"):
        service.materialize_industry_state_snapshots()

    assert calls == 1
