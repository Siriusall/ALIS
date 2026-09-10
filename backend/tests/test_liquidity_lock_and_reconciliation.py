"""Unit tests for liquidity lock constraints and cash reconciliation."""

from __future__ import annotations

import pytest
from app.core.trading_constraints import check_open_trade_constraints, is_limit_down, is_limit_up, is_suspended
from app.services.simulation_ledger_audit import reconcile_current_projection


def test_liquidity_lock_limit_down_sell_blocked():
    # Normal tradable sell
    res_normal = check_open_trade_constraints(
        side="SELL", symbol="000001", name="????",
        open_price=10.0, close_price=9.8, change_pct=-2.0, volume=100000.0,
    )
    assert res_normal.ok is True
    assert res_normal.code == "tradable"

    # Limit-down sell blocked (liquidity lock)
    res_locked = check_open_trade_constraints(
        side="SELL", symbol="000001", name="????",
        open_price=9.0, close_price=9.0, change_pct=-10.0, volume=500.0,
    )
    assert res_locked.ok is False
    assert res_locked.code == "limit_down_sell_blocked"


def test_suspended_liquidity_lock():
    res_suspended = check_open_trade_constraints(
        side="SELL", symbol="000001", name="????",
        open_price=0.0, close_price=0.0, change_pct=0.0, volume=0.0,
    )
    assert res_suspended.ok is False
    assert res_suspended.code == "suspended_or_no_open_price"


def test_st_and_consecutive_limit_down_sell_are_blocked():
    st_result = check_open_trade_constraints(
        side="SELL", symbol="000001", name="*ST示例",
        open_price=5.0, close_price=5.0, change_pct=-4.9, volume=1000.0,
    )
    assert st_result.code == "limit_down_sell_blocked"

    consecutive_result = check_open_trade_constraints(
        side="SELL", symbol="000002", name="普通股票",
        open_price=9.1, close_price=9.1, change_pct=-9.1, volume=1000.0,
        consecutive_limit_down_days=2,
    )
    assert consecutive_result.code == "limit_down_sell_blocked"


def test_cash_reconciliation_consistent_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.simulation_ledger_audit.audit_simulation_ledger", lambda **kwargs: {
        "ok": True,
        "cash_drift": 0.0,
        "position_deltas": [],
    })

    result = reconcile_current_projection(reason="daily_reconciliation_check", execute=False)
    assert result["ok"] is True
    assert result["status"] == "already_consistent"
