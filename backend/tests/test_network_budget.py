from __future__ import annotations

from datetime import datetime

from app.services import network_budget


def test_network_budget_accumulates_across_process_restarts(monkeypatch, tmp_path):
    monkeypatch.setenv("ALIS_NETWORK_BUDGET_ENABLED", "1")
    monkeypatch.setenv("ALIS_NETWORK_DAILY_BUDGET_MB", "128")
    monkeypatch.setenv("ALIS_NETWORK_BUDGET_STATE_PATH", str(tmp_path / "budget.json"))

    first = network_budget.network_budget_status(
        now=datetime.fromisoformat("2026-08-29T08:00:00+08:00"),
        counter_reader=lambda _interface, _direction: 10_000,
    )
    second = network_budget.network_budget_status(
        now=datetime.fromisoformat("2026-08-29T09:00:00+08:00"),
        counter_reader=lambda _interface, _direction: 10_000 + 64 * 1024 * 1024,
    )
    after_reboot = network_budget.network_budget_status(
        now=datetime.fromisoformat("2026-08-29T10:00:00+08:00"),
        counter_reader=lambda _interface, _direction: 500,
    )
    exhausted = network_budget.network_budget_status(
        now=datetime.fromisoformat("2026-08-29T11:00:00+08:00"),
        counter_reader=lambda _interface, _direction: 500 + 65 * 1024 * 1024,
    )

    assert first["used_bytes"] == 0
    assert second["used_bytes"] == 64 * 1024 * 1024
    assert after_reboot["used_bytes"] == second["used_bytes"]
    assert exhausted["exhausted"] is True


def test_network_budget_resets_on_new_local_day(monkeypatch, tmp_path):
    monkeypatch.setenv("ALIS_NETWORK_BUDGET_ENABLED", "1")
    monkeypatch.setenv("ALIS_NETWORK_DAILY_BUDGET_MB", "128")
    monkeypatch.setenv("ALIS_NETWORK_BUDGET_STATE_PATH", str(tmp_path / "budget.json"))

    network_budget.network_budget_status(
        now=datetime.fromisoformat("2026-08-29T23:59:00+08:00"),
        counter_reader=lambda _interface, _direction: 99_000,
    )
    result = network_budget.network_budget_status(
        now=datetime.fromisoformat("2026-08-30T00:01:00+08:00"),
        counter_reader=lambda _interface, _direction: 120_000,
    )

    assert result["day"] == "2026-08-30"
    assert result["used_bytes"] == 0
    assert result["exhausted"] is False


def test_network_budget_fails_closed_when_counter_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("ALIS_NETWORK_BUDGET_ENABLED", "1")
    monkeypatch.setenv("ALIS_NETWORK_BUDGET_STATE_PATH", str(tmp_path / "budget.json"))

    def unavailable(_interface, _direction):
        raise OSError("missing interface")

    result = network_budget.network_budget_status(counter_reader=unavailable)

    assert result["available"] is False
    assert result["exhausted"] is True
    assert result["error_code"] == "network_counter_unavailable"
