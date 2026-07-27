from types import SimpleNamespace

import pytest

from app.services import quant_run_engine, quant_run_primitives as primitives
from app.services.quant_run_contracts import HistoricalDataMissingError


def test_date_primitives_resolve_next_session_and_latest_fallback():
    loader = lambda: ["2026-07-23", "2026-07-24"]
    assert primitives.resolve_execute_date("2026-07-23", date_loader=loader) == ("2026-07-24", False)
    assert primitives.resolve_execute_date("2026-07-24", date_loader=loader) == ("2026-07-24", True)
    with pytest.raises(HistoricalDataMissingError):
        primitives.require_consolidated_trade_dates(3, date_loader=loader)


def test_engine_date_wrapper_preserves_monkeypatch_boundary(monkeypatch):
    monkeypatch.setattr(quant_run_engine, "list_consolidated_trade_dates", lambda: ["2026-07-23", "2026-07-24"])
    assert quant_run_engine.resolve_execute_date("2026-07-23") == ("2026-07-24", False)
    assert quant_run_engine.require_consolidated_trade_dates(2) == ["2026-07-23", "2026-07-24"]


def test_price_and_risk_budget_primitives_are_bounded():
    row = SimpleNamespace(open=10.0, close=11.0)
    assert primitives.row_trade_price(row) == 10.0
    assert primitives.row_trade_price(row, prefer_open=False) == 11.0
    assert primitives.parse_risk_budget({"target_position_pct": 2, "max_single_position_pct": -.1}) == (1.0, 0.0)
    assert primitives.parse_risk_budget({"target_position_pct": "bad"}) == (1.0, .15)
