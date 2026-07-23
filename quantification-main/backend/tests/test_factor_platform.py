from contextlib import contextmanager

from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services import factor_platform
from app.services.factor_platform import (
    CORE_ACTIVE_KEYS,
    SPEC_BY_KEY,
    compute_registered_factor,
    is_factor_governance_eligible,
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


def test_industry_candidate_factor_uses_only_supplied_point_in_time_value():
    row = _row()
    assert compute_registered_factor(
        "industry_ppi_yoy_proxy", row, [], {}, industry_inputs={"industry_ppi_yoy_proxy": 5.5},
    ) == 5.5
    assert compute_registered_factor("industry_ppi_yoy_proxy", row, [], {}) == 0.0
    assert "industry_demand_proxy" in SPEC_BY_KEY
    assert "industry_inventory_proxy" in SPEC_BY_KEY
    assert "beta_60" in SPEC_BY_KEY


def test_daily_selection_registry_covers_every_versioned_factor():
    assert set(SPEC_BY_KEY).issubset(FACTOR_REGISTRY)


def test_trade_constraint_factors_cannot_be_governed_as_alpha_signals():
    assert is_factor_governance_eligible(SPEC_BY_KEY["momentum_60"]) is True
    assert is_factor_governance_eligible(SPEC_BY_KEY["is_st"]) is False
    assert is_factor_governance_eligible(SPEC_BY_KEY["is_limit_up"]) is False


def test_factor_evaluation_replaces_same_future_window_and_skips_constraints(monkeypatch):
    definitions = [
        {"id": 1, "factor_key": "momentum_60", "category": "动量"},
        {"id": 2, "factor_key": "is_st", "category": "交易约束"},
    ]
    rows = [_row() for _ in range(5)]
    for index, row in enumerate(rows):
        row.symbol = f"00000{index + 1}"
        row.close = 10.0
    forward_rows = [_row() for _ in range(5)]
    for index, row in enumerate(forward_rows):
        row.symbol = f"00000{index + 1}"
        row.close = 10.0 + index

    class DataSource:
        def load_snapshot(self, trade_date):
            return rows if trade_date == "20260722" else forward_rows

    class Cursor:
        def __init__(self, values=None):
            self.values = values or []

        def fetchall(self):
            return self.values

    calls = []

    class Connection:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))
            if "SELECT ts_code,standardized_value" in sql:
                return Cursor([
                    {"ts_code": f"00000{index + 1}", "standardized_value": float(index)}
                    for index in range(5)
                ])
            return Cursor()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(factor_platform, "ensure_factor_registry", lambda: definitions)
    monkeypatch.setattr(factor_platform, "_conn", fake_conn)
    result = factor_platform.evaluate_materialized_factors(
        trade_date="20260722",
        forward_date="20260723",
        data_source=DataSource(),
    )

    assert result["results"][1] == {
        "factor_key": "is_st",
        "definition_id": 2,
        "status": "not_applicable_constraint",
    }
    delete_index = next(index for index, (sql, _) in enumerate(calls) if sql.startswith("DELETE FROM factor_evaluations"))
    insert_index = next(index for index, (sql, _) in enumerate(calls) if sql.startswith("INSERT INTO factor_evaluations"))
    assert delete_index < insert_index
    assert calls[delete_index][1] == (1, "20260722", "20260723")
