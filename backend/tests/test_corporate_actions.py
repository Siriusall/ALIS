from app.services.corporate_actions import (
    _equivalent_no_entitlement_revision,
    build_replay_corporate_actions,
    quantity_entitled,
)


def test_quantity_entitlement_uses_record_date_ledger():
    operations = [
        {"symbol": "000001", "side": "BUY", "quantity": 1000, "at": "2026-06-01T09:30:00"},
        {"symbol": "000001", "side": "SELL", "quantity": 400, "at": "2026-06-20T09:30:00"},
        {"symbol": "000001", "side": "SELL", "quantity": 200, "at": "2026-06-25T09:30:00"},
    ]

    assert quantity_entitled(
        operations,
        symbol="000001.SZ",
        record_date="20260622",
    ) == 600


def test_replay_actions_credit_cash_and_stock_once_from_frozen_entitlement():
    operations = [
        {"symbol": "000001", "side": "BUY", "quantity": 1000, "at": "2026-06-01T09:30:00"},
        {"symbol": "000001", "side": "SELL", "quantity": 500, "at": "2026-06-24T09:30:00"},
    ]
    events = [
        {
            "symbol": "000001",
            "record_date": "20260622",
            "ex_date": "20260623",
            "pay_date": "20260625",
            "cash_per_share": 0.2,
            "share_ratio": 0.1,
            "source_hash": "event-1",
        }
    ]

    actions = build_replay_corporate_actions(operations, events)

    assert actions == [
        {
            "symbol": "000001",
            "action_type": "stock_dividend",
            "event_date": "20260623",
            "record_date": "20260622",
            "quantity_entitled": 1000,
            "cash_amount": 0.0,
            "share_quantity": 100,
            "source_hash": "event-1",
        },
        {
            "symbol": "000001",
            "action_type": "cash_dividend",
            "event_date": "20260625",
            "record_date": "20260622",
            "quantity_entitled": 1000,
            "cash_amount": 200.0,
            "share_quantity": 0,
            "source_hash": "event-1",
        },
    ]


def test_prior_stock_dividend_changes_later_record_date_entitlement():
    operations = [
        {"symbol": "000001", "side": "BUY", "quantity": 1000, "at": "2026-01-01T09:30:00"},
    ]
    events = [
        {
            "symbol": "000001",
            "record_date": "20260301",
            "ex_date": "20260302",
            "pay_date": "20260305",
            "cash_per_share": 0.0,
            "share_ratio": 0.1,
            "source_hash": "event-1",
        },
        {
            "symbol": "000001",
            "record_date": "20260601",
            "ex_date": "20260602",
            "pay_date": "20260605",
            "cash_per_share": 0.1,
            "share_ratio": 0.0,
            "source_hash": "event-2",
        },
    ]

    actions = build_replay_corporate_actions(operations, events)

    assert actions[0]["share_quantity"] == 100
    assert actions[1]["quantity_entitled"] == 1100
    assert actions[1]["cash_amount"] == 110.0


def test_equivalent_no_entitlement_source_revision_is_safe_to_reconcile():
    existing = {
        "status": "no_entitlement", "quantity_entitled": 0,
        "cash_per_share": 0.2, "share_ratio": 0.1,
        "cash_amount": 0.0, "share_quantity": 0,
    }
    event = {"cash_per_share": 0.2, "share_ratio": 0.1}

    assert _equivalent_no_entitlement_revision(
        existing, event, entitled=0, cash_amount=0.0, share_quantity=0,
    )


def test_applied_or_economically_changed_revision_stays_blocked():
    existing = {
        "status": "applied", "quantity_entitled": 100,
        "cash_per_share": 0.2, "share_ratio": 0.0,
        "cash_amount": 20.0, "share_quantity": 0,
    }
    assert not _equivalent_no_entitlement_revision(
        existing, {"cash_per_share": 0.2, "share_ratio": 0.0},
        entitled=100, cash_amount=20.0, share_quantity=0,
    )
    assert not _equivalent_no_entitlement_revision(
        {**existing, "status": "no_entitlement", "quantity_entitled": 0, "cash_amount": 0},
        {"cash_per_share": 0.3, "share_ratio": 0.0},
        entitled=0, cash_amount=0.0, share_quantity=0,
    )
