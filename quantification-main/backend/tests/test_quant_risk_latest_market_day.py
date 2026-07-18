from __future__ import annotations

from app.services import quant_risk


def test_latest_market_day_gate_pauses_when_daily_bundle_is_incomplete(monkeypatch):
    class _Cursor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _sql: str, _params=()):
            self.calls += 1
            return self

        def fetchone(self):
            if self.calls == 1:
                return {"latest_trade_date": "20260717"}
            if self.calls == 3:
                return {"count": 0}
            return {"latest_trade_date": ""}

        def fetchall(self):
            return [{"api_name": "daily", "rows": 5000}]

    class _Conn:
        def __enter__(self):
            return _Cursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("app.data_platform.db._conn", lambda: _Conn())

    decision = quant_risk.evaluate_latest_market_day_gate()

    assert decision.should_pause is True
    assert decision.code == "paused_latest_market_day_not_ready"
    assert decision.evidence["latest_trade_date"] == "20260717"
    assert decision.evidence["missing_apis"] == ["daily_basic", "stk_limit"]


def test_latest_market_day_gate_passes_when_daily_bundle_and_snapshot_exist(monkeypatch):
    class _Cursor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _sql: str, _params=()):
            self.calls += 1
            return self

        def fetchone(self):
            if self.calls == 1:
                return {"latest_trade_date": "20260717"}
            if self.calls == 3:
                return {"count": 5100}
            return {"latest_trade_date": ""}

        def fetchall(self):
            return [
                {"api_name": "daily", "rows": 5000},
                {"api_name": "daily_basic", "rows": 5000},
                {"api_name": "stk_limit", "rows": 5000},
            ]

    class _Conn:
        def __enter__(self):
            return _Cursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("app.data_platform.db._conn", lambda: _Conn())

    decision = quant_risk.evaluate_latest_market_day_gate()

    assert decision.should_pause is False
    assert decision.code == "ok"
