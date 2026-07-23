from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services.factor_platform import (
    CORE_ACTIVE_KEYS,
    SPEC_BY_KEY,
    compute_registered_factor,
)
from app.services.factor_engine import FACTOR_REGISTRY


def _bars(count: int, *, start: float = 10.0, step: float = 0.1) -> list[HistBar]:
    return [
        HistBar(
            date=f"2026-01-{index + 1:02d}",
            open=start + index * step,
            high=start + index * step,
            low=start + index * step,
            close=start + index * step,
            volume=1000 + index * 10,
            turnover=10000,
        )
        for index in range(count)
    ]


def _row(close: float = 22.0) -> SnapshotRow:
    return SnapshotRow(
        symbol="000001",
        date="2026-07-22",
        open=close,
        high=close,
        low=close,
        close=close,
        prev_close=close - 0.1,
        change_pct=1.2,
        volume=2000,
        turnover=30000,
        name="平安银行",
    )


def test_registered_factor_formula_covers_new_medium_horizon_and_risk_factors():
    row = _row()
    bars = _bars(120)
    daily_basic = {
        "000001": {
            "float_mv": 100,
            "total_mv": 200,
            "pe_ttm": 10,
            "pb": 2,
            "turnover_rate": 3,
        }
    }
    assert compute_registered_factor("momentum_60", row, bars, daily_basic) > 0
    assert compute_registered_factor("momentum_120", row, bars, daily_basic) > 0
    assert compute_registered_factor("downside_volatility_60", row, bars, daily_basic) == 0
    assert compute_registered_factor("max_drawdown_60", row, bars, daily_basic) == 0
    assert compute_registered_factor("earnings_yield", row, bars, daily_basic) == 0.1
    assert compute_registered_factor("book_to_price", row, bars, daily_basic) == 0.5
    benchmark_returns = [0.005 + (index % 4) * 0.001 for index in range(60)]
    stock_closes = [10.0]
    for value in benchmark_returns:
        stock_closes.append(stock_closes[-1] * (1.0 + value * 1.5))
    beta_bars = [
        HistBar(
            date=f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
            open=value,
            high=value,
            low=value,
            close=value,
            volume=1000,
            turnover=10000,
        )
        for index, value in enumerate(stock_closes)
    ]
    assert 1.49 < compute_registered_factor(
        "beta_60",
        _row(close=stock_closes[-1]),
        beta_bars,
        daily_basic,
        benchmark_returns=benchmark_returns,
    ) < 1.51


def test_new_factor_candidates_are_registered_but_not_bootstrap_active():
    assert "momentum_20" in CORE_ACTIVE_KEYS
    assert "momentum_60" in SPEC_BY_KEY
    assert "momentum_60" not in CORE_ACTIVE_KEYS
    assert "beta_60" in SPEC_BY_KEY


def test_daily_selection_registry_covers_every_versioned_factor():
    assert set(SPEC_BY_KEY).issubset(FACTOR_REGISTRY)
