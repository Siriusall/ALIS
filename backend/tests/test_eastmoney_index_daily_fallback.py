import json
import sqlite3
from contextlib import contextmanager

from app.data_platform.services import eastmoney_index_daily_fallback as fallback


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE data_sources (
            code TEXT PRIMARY KEY, name TEXT, source_type TEXT, priority INTEGER,
            status TEXT, description TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE dc_tushare_raw_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, api_name TEXT, record_key TEXT,
            ts_code TEXT, trade_date TEXT, period TEXT, ann_date TEXT,
            payload_json TEXT, source_code TEXT, available_time TEXT, updated_at TEXT,
            UNIQUE(api_name, record_key)
        );
    """)
    return connection


def _connection_context(connection):
    @contextmanager
    def _open():
        yield connection

    return _open


def _insert_prior(connection, close=4694.44):
    connection.execute(
        """INSERT INTO dc_tushare_raw_records
           (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
            source_code,available_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "index_daily", "000300.SH|20260807", "000300.SH", "20260807", "", "",
            json.dumps({"close": close}), "tushare", "2026-08-07T15:00:00", "2026-08-07T16:00:00",
        ),
    )


def _bar(pre_close=4694.44):
    return {
        "ts_code": "000300.SH", "trade_date": "20260810", "open": 4698.82,
        "high": 4714.46, "low": 4659.47, "close": 4702.02,
        "pre_close": pre_close, "change": 7.58, "pct_chg": 0.16,
        "vol": 213075996.0, "amount": 733103396912.2,
        "name": "沪深300", "source_url": fallback.HISTORY_URL,
        "source_code": fallback.SOURCE_EASTMONEY_INDEX,
    }


def test_verified_fallback_records_economic_and_observation_times(monkeypatch):
    connection = _db()
    _insert_prior(connection)
    connection.commit()
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))
    monkeypatch.setattr(fallback, "_now", lambda: "2026-08-11T10:30:00+08:00")
    monkeypatch.setattr(fallback, "fetch_eastmoney_hs300_bar", lambda _date: _bar())

    result = fallback.repair_hs300_index_daily_from_eastmoney("20260810")

    assert result["ok"] is True
    row = dict(connection.execute(
        "SELECT * FROM dc_tushare_raw_records WHERE trade_date='20260810'"
    ).fetchone())
    assert row["source_code"] == fallback.SOURCE_EASTMONEY_INDEX
    assert row["available_time"] == "2026-08-10T15:00:00"
    payload = json.loads(row["payload_json"])
    assert payload["observed_at"] == "2026-08-11T10:30:00+08:00"
    assert payload["pending_primary_reconciliation"] is True


def test_fallback_never_overwrites_and_fails_closed_on_continuity(monkeypatch):
    connection = _db()
    _insert_prior(connection, close=4600.0)
    connection.commit()
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))
    monkeypatch.setattr(fallback, "fetch_eastmoney_hs300_bar", lambda _date: _bar())

    result = fallback.repair_hs300_index_daily_from_eastmoney("20260810")
    assert result["status"] == "previous_close_cross_check_failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM dc_tushare_raw_records WHERE trade_date='20260810'"
    ).fetchone()[0] == 0


class _Response:
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "rc": 0,
            "data": {
                "name": "沪深300", "preKPrice": 4694.44,
                "klines": ["2026-08-10,4698.82,4702.02,4714.46,4659.47,213075996,733103396912.20,1.17,0.16,7.58,0.64"],
            },
        }


def test_fetch_parses_only_exact_internally_consistent_bar(monkeypatch):
    monkeypatch.setattr(fallback, "_http_get", lambda *_args, **_kwargs: _Response())
    bar = fallback.fetch_eastmoney_hs300_bar("20260810")
    assert bar is not None
    assert bar["close"] == 4702.02
    assert fallback.fetch_eastmoney_hs300_bar("20260811") is None


class _SinaResponse:
    text = 'var _=([{"day":"2026-08-10","open":"4698.816","high":"4714.465","low":"4659.470","close":"4702.025","volume":"21307599600"}]);'

    def raise_for_status(self):
        return None


def test_sina_secondary_provider_normalizes_volume(monkeypatch):
    monkeypatch.setattr(fallback, "_http_get", lambda *_args, **_kwargs: _SinaResponse())
    bar = fallback.fetch_sina_hs300_bar("20260810")
    assert bar is not None
    assert bar["source_code"] == fallback.SOURCE_SINA_INDEX
    assert bar["vol"] == 213075996.0
