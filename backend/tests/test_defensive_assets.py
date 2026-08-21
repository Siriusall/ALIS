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
        "items": [{**item, "status": "ready", "momentum_20d": 0.0} for item in defensive_assets.DEFENSIVE_ASSETS]
    })
    policy = defensive_assets.defensive_allocation_for_state("S9", as_of_date="20260715", equity_target_weight=.35)
    assert policy["status"] == "partial"
    assert abs(policy["target_weight"] - .45) < .001
    assert abs(sum(policy["target_weights"].values()) - .45) < .001
    assert policy["unallocated_cash_weight"] == .20
    assert policy["full_investment_target"] == .80
    assert max(policy["target_weights"].values()) <= .15


def test_no_equity_candidates_allocate_full_account_to_defensive_assets(monkeypatch):
    monkeypatch.setattr(defensive_assets, "list_defensive_assets", lambda as_of_date=None: {
        "items": [{**item, "status": "ready", "momentum_20d": 0.0} for item in defensive_assets.DEFENSIVE_ASSETS]
    })
    policy = defensive_assets.defensive_allocation_for_state("S10", as_of_date="20260715", equity_target_weight=0)
    assert policy["status"] == "partial"
    # Three true defensive assets cannot absorb 100% under the 15% hard cap.
    assert abs(sum(policy["target_weights"].values()) - .45) < .001
    assert max(policy["target_weights"].values()) <= .15
    assert policy["full_investment_target"] == .45
    assert set(policy["target_weights"]) == {"511880.SH", "511010.SH", "518880.SH"}
    assert set(policy["excluded_equity_like_assets"]) == {"510880.SH", "512890.SH"}


def test_registered_defensive_assets_distinguish_equity_like_etfs():
    money = defensive_assets.defensive_asset_meta("511880")
    dividend = defensive_assets.defensive_asset_meta("510880.SH")

    assert money == {
        "ts_code": "511880.SH", "name": "银华日利ETF", "asset_class": "money",
        "risk_group": "true_defensive", "equity_exposure": "0",
    }
    assert dividend is not None
    assert dividend["risk_group"] == "equity_like"
    assert dividend["equity_exposure"] == "1"


def test_defensive_asset_bar_uses_latest_point_in_time_record(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260723', '{"close": 1.12}', '2026-07-23T15:00:00+08:00'),
            ('fund_daily', '510880.SH', '20260724', '{"close": 1.13}', '2026-07-24T16:00:00+08:00');
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


def test_defensive_series_exact_date_does_not_use_previous_bar(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260723', '{"close": 1.12}', '2026-07-23T15:00:00+08:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    bar, returns = defensive_assets.defensive_asset_series(
        "510880.SH", "20260724", exact_date=True,
        available_as_of_time="2026-07-24T15:00:00+08:00",
    )

    assert bar is None
    assert returns == []


def test_defensive_series_exact_date_keeps_prior_history(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260723', '{"close": 1.12}', '2026-07-23T15:00:00+08:00'),
            ('fund_daily', '510880.SH', '20260724', '{"close": 1.13}', '2026-07-24T15:00:00+08:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    bar, returns = defensive_assets.defensive_asset_series(
        "510880.SH", "20260724", exact_date=True,
        available_as_of_time="2026-07-24T15:00:00+08:00",
    )

    assert bar is not None
    assert bar["trade_date"] == "20260724"
    assert len(returns) == 1


def test_defensive_series_batch_bounds_each_code_history_window(monkeypatch):
    calls = []

    def fake_load(_connection, _api_name, **kwargs):
        calls.append(dict(kwargs))
        code = kwargs["ts_codes"][0]
        return [
            {
                "ts_code": code,
                "trade_date": f"202607{24 - index:02d}",
                "payload_json": '{"close": 100}',
                "observed_at": "2026-07-24T15:00:00+08:00",
            }
            for index in range(8)
        ]

    monkeypatch.setattr(defensive_assets, "load_raw_record_versions", fake_load)

    result = defensive_assets.defensive_asset_series_batch(
        ["510880.SH", "511010.SH"],
        "20260724",
        limit=3,
        connection=object(),
        available_as_of_time="2026-07-24T15:00:00+08:00",
    )

    assert [call["ts_codes"] for call in calls] == [["510880.SH"], ["511010.SH"]]
    assert all(call["trade_date_start"] == "20260624" for call in calls)
    assert all(call["trade_date_end"] == "20260724" for call in calls)
    assert all(len(result[code][1]) == 3 for code in ("510880.SH", "511010.SH"))


def test_defensive_asset_reader_rejects_naive_observation_cutoff(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260724', '{"close": 1.13}', '2026-07-24T15:00:00+08:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    assert defensive_assets.defensive_asset_bar(
        "510880.SH", "20260724", exact_date=True,
        available_as_of_time="2026-07-24T15:00:00",
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
             '{"open": 3.2, "high": 3.3, "low": 3.0, "close": 3.1, "pre_close": 3.1, "vol": 1000, "amount": 3000}', '2026-07-31T15:00:00+08:00'),
            ('fund_daily', '511010.SH', '20260730',
             '{"open": 140, "high": 141, "low": 139, "close": 140, "pre_close": 139, "vol": 1000, "amount": 140000}', '2026-07-30T15:00:00+08:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    status = defensive_assets.defensive_asset_execution_status(
        "20260731",
        ["510880.SH", "511010.SH"],
    )

    assert status["ok"] is False
    assert status["ready"] == ["510880.SH"]
    assert status["missing"] == ["511010.SH"]
    assert status["schema_version"] == defensive_assets.DEFENSIVE_ASSET_EXECUTION_CONTRACT


def test_defensive_execution_status_requires_full_tradable_bar(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260731',
             '{"open": 3.2, "high": 3.3, "low": 3.0, "close": 3.1, "pre_close": 3.1, "vol": 1000}', '2026-07-31T15:00:00+08:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    status = defensive_assets.defensive_asset_execution_status("20260731", ["510880.SH"])

    assert status["ok"] is False
    assert status["missing"] == ["510880.SH"]
    assert status["items"][0]["record_present"] is True
    assert status["items"][0]["missing_fields"] == ["amount"]


def test_defensive_execution_status_accepts_only_data_observed_by_current_check(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, ts_code TEXT, trade_date TEXT, payload_json TEXT, available_time TEXT
        );
        INSERT INTO dc_tushare_raw_records VALUES
            ('fund_daily', '510880.SH', '20260731',
             '{"open": 3.2, "high": 3.3, "low": 3.0, "close": 3.1, "pre_close": 3.1, "vol": 1000, "amount": 3000}', '2026-07-31T20:15:00+08:00');
    """)
    monkeypatch.setattr(defensive_assets, "_conn", _connection_context(connection))

    before = defensive_assets.defensive_asset_execution_status(
        "20260731", ["510880.SH"], observed_at="2026-07-31T20:14:59+08:00"
    )
    after = defensive_assets.defensive_asset_execution_status(
        "20260731", ["510880.SH"], observed_at="2026-07-31T20:15:00+08:00"
    )

    assert before["ok"] is False
    assert after["ok"] is True
