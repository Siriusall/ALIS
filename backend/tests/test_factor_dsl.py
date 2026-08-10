import pytest

from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services.factor_dsl import (
    FactorDSLValidationError,
    evaluate_expression,
    expression_hash,
    expression_inputs,
    expression_max_window,
    validate_expression,
)


def _row() -> SnapshotRow:
    return SnapshotRow(
        symbol="000001", date="20260724", open=12.0, high=13.0, low=11.5,
        close=12.5, prev_close=12.0, change_pct=4.0, volume=2000,
        turnover=25000, name="测试",
    )


def _history() -> list[HistBar]:
    return [
        HistBar(
            date=f"202607{index + 1:02d}", open=10 + index * 0.1,
            high=10.5 + index * 0.1, low=9.5 + index * 0.1,
            close=10 + index * 0.1, volume=1000 + index * 50,
            turnover=10000 + index * 500,
        )
        for index in range(20)
    ]


def test_factor_dsl_evaluates_nested_point_in_time_expression():
    expression = {
        "op": "sub",
        "args": [
            {"op": "returns", "args": [{"op": "field", "name": "close"}], "window": 5},
            {"op": "returns", "args": [{"op": "field", "name": "close"}], "window": 10},
        ],
    }

    value = evaluate_expression(expression, _row(), _history(), {})

    assert value != 0
    assert expression_inputs(expression) == ["close"]
    assert expression_max_window(expression) == 10
    assert expression_hash(expression) == expression_hash(expression)


def test_factor_dsl_rejects_future_or_unbounded_operators():
    with pytest.raises(FactorDSLValidationError):
        validate_expression({"op": "lead", "args": [{"op": "field", "name": "close"}]})
    with pytest.raises(FactorDSLValidationError):
        validate_expression({
            "op": "ts_mean",
            "args": [{"op": "field", "name": "close"}],
            "window": 9999,
        })


def test_factor_dsl_reads_point_in_time_daily_basic_fields():
    expression = {
        "op": "div",
        "args": [
            {"op": "constant", "value": 1.0, "args": []},
            {"op": "field", "name": "pe_ttm"},
        ],
    }

    assert evaluate_expression(expression, _row(), _history(), {"pe_ttm": 10}) == 0.1
