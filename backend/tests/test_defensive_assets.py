import sqlite3
from contextlib import contextmanager

from app.services import defensive_assets


def _connection_context(connection):
    @contextmanager
    def _open():
        yield connection

    return _open


def test_defensive_policy_fills_exact_complement_of_equity_target(monkeypatch):
    monkeypatch.setattr(defensive_assets, "list_defensive_assets", lambda as_of_date=None: {
        "items": [
            {"ts_code": "511880.SH", "asset_class": "money", "status": "ready"},
            {"ts_code": "511010.SH", "asset_class": "bond", "status": "ready"},
            {"ts_code": "518880.SH", "asset_class": "gold", "status": "ready"},
            {"ts_code": "512890.SH", "asset_class": "low_volatility", "status": "ready"},
            {"ts_code": "510880.SH", "asset_class": "dividend", "status": "ready"},
        ]
    })
    policy = defensive_assets.defensive_allocation_for_state("S9", as_of_date="20260715", equity_target_weight=.35)
    assert policy["status"] == "ready"
    assert abs(policy["target_weight"] - .65) < .001
    assert abs(sum(policy["target_weights"].values()) - .65) < .001
    assert policy["full_investment_target"] == 1.0
    assert max(policy["target_weights"].values()) <= .45


def test_no_equity_candidates_allocate_full_account_to_defensive_assets(monkeypatch):
    monkeypatch.setattr(defensive_assets, "list_defensive_assets", lambda as_of_date=None: {
        "items": [{**item, "status": "ready", "momentum_20d": 0.0} for item in defensive_assets.DEFENSIVE_ASSETS]
    })
    policy = defensive_assets.defensive_allocation_for_state("S10", as_of_date="20260715", equity_target_weight=0)
    assert policy["status"] == "ready"
    assert abs(sum(policy["target_weights"].values()) - 1.0) < .001
    assert max(policy["target_weights"].values()) <= .45


def test_defensive_asset_bar_uses_latest_point_in_time_record(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260723', '{"close": 1.12}', '2026-07-23T15:00:00'),
            ('fund_daily', '510880.SH', '20260724', '{"close": 1.13}', '2026-07-24T16:00:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    bar = defensive_assets.defensive_asset_bar("510880.SH", "20260724")

    assert bar is not None
    assert bar["close"] == 1.12
    assert bar["trade_date"] == "20260723"
    assert defensive_assets.defensive_asset_bar(
        "510880.SH",
        "20260724",
        exact_date=True,
    ) is None


def test_defensive_execution_status_requires_exact_complete_session(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260731',
             '{"open": 3.2, "pre_close": 3.1, "vol": 1000}', '2026-07-31T15:00:00'),
            ('fund_daily', '511010.SH', '20260730',
             '{"open": 140, "pre_close": 139, "vol": 1000}', '2026-07-30T15:00:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    status = defensive_assets.defensive_asset_execution_status(
        "20260731",
        ["510880.SH", "511010.SH"],
    )

    assert status["ok"] is False
    assert status["ready"] == ["510880.SH"]
    assert status["missing"] == ["511010.SH"]
