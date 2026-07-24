from app.services.simulation_ledger_audit import replay_simulation_ledger


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
