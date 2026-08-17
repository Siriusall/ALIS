from datetime import date

import pytest
from fastapi import HTTPException

from app.api.routes import equity
from app.services import equity_curve
from app.services.daily_data import DailyBar, DailyCacheNotReadyError


def _bar(day: str, close: float) -> DailyBar:
    return DailyBar(day, close, close, close, close, 1.0, 1.0)


@pytest.mark.parametrize("value", ["", "not-a-date", "2026-13-40", "abcdefgh"])
def test_equity_curve_requires_a_valid_explicit_execution_date(value: str) -> None:
    with pytest.raises(DailyCacheNotReadyError):
        equity_curve._parse_date(value)


def test_equity_curve_rejects_missing_target_close_for_any_position(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(equity_curve, "get_last_trade_date", lambda **_kwargs: date(2026, 8, 15))
    monkeypatch.setattr(
        equity_curve,
        "get_daily_bars_lookback",
        lambda *_args, **_kwargs: {
            "000001": [_bar("2026-08-14", 10.0), _bar("2026-08-15", 10.5)],
            "000002": [_bar("2026-08-14", 20.0)],
        },
    )

    with pytest.raises(DailyCacheNotReadyError, match="共同覆盖的目标交易日"):
        equity_curve.compute_equity_curve(
            [{"symbol": "000001", "quantity": 100}, {"symbol": "000002", "quantity": 100}],
            "2026-08-14T15:00:00+08:00",
        )


def test_equity_curve_uses_only_common_exact_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(equity_curve, "get_last_trade_date", lambda **_kwargs: date(2026, 8, 15))
    monkeypatch.setattr(
        equity_curve,
        "get_daily_bars_lookback",
        lambda *_args, **_kwargs: {
            "000001": [_bar("2026-08-13", 9.5), _bar("2026-08-14", 10.0), _bar("2026-08-15", 10.5)],
            "000002": [_bar("2026-08-14", 20.0), _bar("2026-08-15", 21.0)],
        },
    )

    result = equity_curve.compute_equity_curve(
        [{"symbol": "000001", "quantity": 100}, {"symbol": "000002", "quantity": 10}],
        "2026-08-14",
    )

    assert result == [
        {"date": "2026-08-14", "total_value": 1200.0, "by_symbol": {"000001": 1000.0, "000002": 200.0}},
        {"date": "2026-08-15", "total_value": 1260.0, "by_symbol": {"000001": 1050.0, "000002": 210.0}},
    ]


def test_live_positions_curve_rejects_missing_position_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        equity,
        "get_positions",
        lambda: [{"symbol": "000001", "quantity": 100, "price": 10.0, "updated_at": ""}],
    )
    captured: dict[str, str] = {}

    def unavailable(_orders, executed_at: str):
        captured["executed_at"] = executed_at
        raise DailyCacheNotReadyError(0, "", 0, "权益曲线缺少执行日期")

    monkeypatch.setattr(equity, "compute_equity_curve", unavailable)

    with pytest.raises(HTTPException) as exc:
        equity.get_positions_equity_curve()

    assert captured == {"executed_at": ""}
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "equity_curve_data_not_ready"
