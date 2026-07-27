import math

import pytest

from app.services.simulation_ledger_audit import _ledger_money, _money, _quantity, replay_simulation_ledger


class _UnexpectedStringError:
    def __str__(self):
        raise RuntimeError("unexpected decimal failure")


def test_money_only_tolerates_decimal_conversion_errors():
    assert _money("invalid") == 0
    with pytest.raises(RuntimeError, match="unexpected decimal failure"):
        _money(_UnexpectedStringError())


def test_quantity_requires_a_finite_non_negative_integer():
    assert _quantity("100") == 100
    assert _quantity(0) == 0
    assert _quantity("1.5") is None
    assert _quantity(-1) is None
    assert _quantity(-1, allow_negative=True) == -1
    assert _quantity("NaN") is None
    assert _quantity("invalid") is None


def test_ledger_money_requires_finite_values_and_respects_sign_contract():
    assert _ledger_money("1.25") == 1.25
    assert _ledger_money(-1) is None
    assert _ledger_money(-1, allow_negative=True) == -1
    assert _ledger_money(0, positive=True) is None
    assert _ledger_money("Infinity") is None
    assert _ledger_money("invalid") is None


def test_replay_simulation_ledger_includes_fees_and_corporate_actions():
    operations = [
        {
            "id": 1, "symbol": "000001", "side": "BUY", "price": 10,
            "quantity": 100, "at": "2026-01-02T09:30:00",
            "commission": 5, "transfer_fee": 0.01, "stamp_tax": 0,
        },
        {
            "id": 2, "symbol": "000001", "side": "SELL", "price": 12,
            "quantity": 40, "at": "2026-02-02T09:30:00",
            "commission": 5, "transfer_fee": 0.01, "stamp_tax": 0.48,
        },
    ]
    actions = [
        {
            "id": 1, "symbol": "000001", "action_type": "stock_dividend",
            "event_date": "20260120", "share_quantity": 10, "cash_amount": 0,
        },
        {
            "id": 2, "symbol": "000001", "action_type": "cash_dividend",
            "event_date": "20260125", "share_quantity": 0, "cash_amount": 20,
        },
    ]

    result = replay_simulation_ledger(operations, actions, baseline_cash=200_000)

    assert result["cash"] == 199_489.5
    assert result["positions"] == {"000001": 70}
    assert result["total_fees"] == 10.5
    assert result["invalid_operations"] == []


def test_replay_simulation_ledger_applies_explicit_migration_adjustments():
    result = replay_simulation_ledger(
        [],
        [],
        baseline_cash=200_000,
        adjustments=[
            {"id": 1, "adjusted_at": "2026-01-01T00:00:00", "symbol": "", "cash_delta": -100, "quantity_delta": 0},
            {"id": 2, "adjusted_at": "2026-01-01T00:00:00", "symbol": "511880", "cash_delta": 0, "quantity_delta": 200},
        ],
    )

    assert result["cash"] == 199_900
    assert result["positions"] == {"511880": 200}
    assert result["adjustment_count"] == 2


def test_replay_simulation_ledger_reports_oversell_without_hiding_negative_quantity():
    result = replay_simulation_ledger(
        [{
            "id": 9, "symbol": "000001.SZ", "side": "SELL", "price": 10,
            "quantity": 100, "at": "2026-01-02T09:30:00",
            "commission": 5, "transfer_fee": 0, "stamp_tax": 1,
        }],
        [],
        baseline_cash=200_000,
    )

    assert result["cash"] == 200_994
    assert result["positions"] == {"000001": -100}
    assert result["invalid_operations"][0]["code"] == "oversell"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"operations": [{"id": "bad-id", "symbol": "000001", "side": "BUY", "price": 10, "quantity": -1, "at": "2026-01-02"}], "corporate_actions": []},
            "invalid_operation_quantity",
        ),
        (
            {"operations": [], "corporate_actions": [{"id": 2, "symbol": "000001", "action_type": "stock_dividend", "event_date": "20260102", "share_quantity": "1.5"}]},
            "invalid_corporate_action_quantity",
        ),
        (
            {"operations": [], "corporate_actions": [], "adjustments": [{"id": 3, "adjusted_at": "2026-01-02", "symbol": "000001", "cash_delta": -10, "quantity_delta": "invalid"}]},
            "invalid_adjustment_quantity",
        ),
    ],
)
def test_replay_reports_invalid_quantities_without_applying_partial_rows(kwargs, code):
    result = replay_simulation_ledger(baseline_cash=200_000, **kwargs)

    assert result["cash"] == 200_000
    assert result["positions"] == {}
    assert result["invalid_operations"][0]["code"] == code


@pytest.mark.parametrize(
    ("kwargs", "code", "field"),
    [
        (
            {"operations": [{"id": 1, "symbol": "000001", "side": "BUY", "price": "NaN", "quantity": 100, "at": "2026-01-02", "commission": 5, "transfer_fee": 0, "stamp_tax": 0}], "corporate_actions": []},
            "invalid_operation_money",
            "price",
        ),
        (
            {"operations": [{"id": 1, "symbol": "000001", "side": "BUY", "price": 10, "quantity": 100, "at": "2026-01-02", "commission": -1, "transfer_fee": 0, "stamp_tax": 0}], "corporate_actions": []},
            "invalid_operation_money",
            "commission",
        ),
        (
            {"operations": [], "corporate_actions": [{"id": 2, "symbol": "000001", "action_type": "cash_dividend", "event_date": "20260102", "cash_amount": -1}]},
            "invalid_corporate_action_cash",
            "cash_amount",
        ),
        (
            {"operations": [], "corporate_actions": [], "adjustments": [{"id": 3, "adjusted_at": "2026-01-02", "symbol": "000001", "cash_delta": "Infinity", "quantity_delta": 100}]},
            "invalid_adjustment_cash",
            "cash_delta",
        ),
    ],
)
def test_replay_reports_invalid_money_without_applying_partial_rows(kwargs, code, field):
    result = replay_simulation_ledger(baseline_cash=200_000, **kwargs)

    assert result["cash"] == 200_000
    assert result["positions"] == {}
    assert result["total_fees"] == 0
    assert result["invalid_operations"][0]["code"] == code
    assert result["invalid_operations"][0]["field"] == field


def test_replay_reports_nonfinite_baseline_without_emitting_nonfinite_results():
    result = replay_simulation_ledger([], [], baseline_cash=float("nan"))

    assert result["baseline_cash"] == 0
    assert result["cash"] == 0
    issue = result["invalid_operations"][0]
    assert issue["code"] == "invalid_baseline_cash"
    assert issue["field"] == "baseline_cash"
    assert math.isnan(issue["value"])
