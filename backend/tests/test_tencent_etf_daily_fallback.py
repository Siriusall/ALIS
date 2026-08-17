import json
import sqlite3
from contextlib import contextmanager

from app.data_platform.services import tencent_etf_daily_fallback as fallback


def _quote_line(
    code: str,
    *,
    trade_date: str = "20260731",
    open_price: float = 1.19,
    pre_close: float = 1.207,
    close: float = 1.204,
    high: float = 1.204,
    low: float = 1.177,
    volume: int = 9_250_903,
    amount_yuan: int = 1_099_392_199,
) -> str:
    fields = [""] * 49
    fields[0] = "1"
    fields[1] = "测试ETF"
    fields[2] = code
    fields[3] = str(close)
    fields[4] = str(pre_close)
    fields[5] = str(open_price)
    fields[30] = f"{trade_date}161433"
    fields[31] = str(round(close - pre_close, 6))
    fields[32] = str(round((close / pre_close - 1) * 100, 6))
    fields[33] = str(high)
    fields[34] = str(low)
    fields[35] = f"{close}/{volume}/{amount_yuan}"
    fields[36] = str(volume)
    fields[37] = str(round(amount_yuan / 10_000))
    return f'v_sh{code}="{"~".join(fields)}";'


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


def _insert_bar(connection, code, trade_date, payload, source="tushare"):
    connection.execute(
        """INSERT INTO dc_tushare_raw_records
           (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
            source_code,available_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "fund_daily", f"{code}|{trade_date}", code, trade_date, "", "",
            json.dumps(payload), source, f"2026-07-31T15:00:00", "2026-07-31T16:00:00",
        ),
    )


def test_parse_etf_quote_requires_exact_date_and_complete_positive_bar():
    valid = fallback._parse_quotes(_quote_line("512890"), "20260731")
    assert valid["512890.SH"]["amount"] == 1_099_392.199
    assert valid["512890.SH"]["vol"] == 9_250_903
    assert fallback._parse_quotes(_quote_line("512890", trade_date="20260730"), "20260731") == {}
    assert fallback._parse_quotes(_quote_line("512890", volume=0), "20260731") == {}


def test_verified_fallback_fills_only_missing_rows_and_preserves_tushare(monkeypatch):
    connection = _db()
    reference_payload = {
        "open": 1.19, "pre_close": 1.207, "close": 1.204,
        "high": 1.204, "low": 1.177, "vol": 9_250_903,
        "amount": 1_099_392.199,
    }
    _insert_bar(connection, "511880.SH", "20260731", reference_payload)
    _insert_bar(connection, "512890.SH", "20260731", reference_payload)
    _insert_bar(connection, "510880.SH", "20260730", {
        "open": 3.31, "high": 3.32, "low": 3.30, "close": 3.312,
        "pre_close": 3.31, "vol": 1_000, "amount": 3_312,
    })
    connection.commit()
    quotes = fallback._parse_quotes(
        "\n".join([
            _quote_line("511880"),
            _quote_line("512890"),
            _quote_line(
                "510880", open_price=3.281, pre_close=3.312, close=3.304,
                high=3.309, low=3.248, volume=2_640_779, amount_yuan=865_046_293,
            ),
        ]),
        "20260731",
    )
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))
    monkeypatch.setattr(fallback, "_now", lambda: "2026-07-31T20:15:00+08:00")
    monkeypatch.setattr(fallback, "fetch_tencent_etf_quotes", lambda *_args: quotes)

    result = fallback.repair_fund_daily_from_tencent(
        "20260731", ["511880.SH", "512890.SH", "510880.SH"],
    )

    assert result["ok"] is True
    assert result["rows_written"] == 1
    rows = connection.execute(
        "SELECT ts_code,source_code,payload_json FROM dc_tushare_raw_records WHERE trade_date='20260731' ORDER BY ts_code"
    ).fetchall()
    assert len(rows) == 3
    assert dict(rows[0])["source_code"] == "tencent_etf_quote_fallback"
    assert all(dict(row)["source_code"] == "tushare" for row in rows[1:])
    payload = json.loads(dict(rows[0])["payload_json"])
    assert payload["verification_status"] == "same_day_and_previous_close_cross_checked"
    stored = connection.execute(
        "SELECT available_time FROM dc_tushare_raw_records WHERE ts_code='510880.SH' AND trade_date='20260731'"
    ).fetchone()
    assert stored["available_time"] == "2026-07-31T20:15:00+08:00"


def test_verified_fallback_accepts_tushare_proxy_same_day_references(monkeypatch):
    connection = _db()
    reference_payload = {
        "open": 1.19, "pre_close": 1.207, "close": 1.204,
        "high": 1.204, "low": 1.177, "vol": 9_250_903,
        "amount": 1_099_392.199,
    }
    _insert_bar(connection, "511880.SH", "20260731", reference_payload, source="tushare_proxy")
    _insert_bar(connection, "512890.SH", "20260731", reference_payload, source="tushare_proxy")
    _insert_bar(connection, "510880.SH", "20260730", {
        "open": 3.31, "high": 3.32, "low": 3.30, "close": 3.312,
        "pre_close": 3.31, "vol": 1_000, "amount": 3_312,
    }, source="tushare_proxy")
    connection.commit()
    quotes = fallback._parse_quotes(
        "\n".join([
            _quote_line("511880"),
            _quote_line("512890"),
            _quote_line(
                "510880", open_price=3.281, pre_close=3.312, close=3.304,
                high=3.309, low=3.248, volume=2_640_779, amount_yuan=865_046_293,
            ),
        ]),
        "20260731",
    )
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))
    monkeypatch.setattr(fallback, "fetch_tencent_etf_quotes", lambda *_args: quotes)

    result = fallback.repair_fund_daily_from_tencent(
        "20260731", ["511880.SH", "512890.SH", "510880.SH"],
    )

    assert result["ok"] is True
    assert result["rows_written"] == 1


def test_fallback_fails_closed_when_same_day_mapping_or_previous_close_mismatches(monkeypatch):
    connection = _db()
    reference_payload = {
        "open": 1.19, "pre_close": 1.207, "close": 1.204,
        "high": 1.204, "low": 1.177, "vol": 9_250_903,
        "amount": 1_099_392.199,
    }
    _insert_bar(connection, "511880.SH", "20260731", reference_payload)
    _insert_bar(connection, "512890.SH", "20260731", reference_payload)
    _insert_bar(connection, "510880.SH", "20260730", {
        "open": 3.10, "high": 3.11, "low": 3.09, "close": 3.100,
        "pre_close": 3.09, "vol": 1_000, "amount": 3_100,
    })
    connection.commit()
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))
    bad_reference_quotes = fallback._parse_quotes(
        "\n".join([_quote_line("511880", close=1.25), _quote_line("512890"), _quote_line("510880")]),
        "20260731",
    )
    monkeypatch.setattr(fallback, "fetch_tencent_etf_quotes", lambda *_args: bad_reference_quotes)
    result = fallback.repair_fund_daily_from_tencent(
        "20260731", ["511880.SH", "512890.SH", "510880.SH"],
    )
    assert result["status"] == "same_day_cross_check_failed"

    good_references_bad_continuity = fallback._parse_quotes(
        "\n".join([_quote_line("511880"), _quote_line("512890"), _quote_line("510880")]),
        "20260731",
    )
    monkeypatch.setattr(fallback, "fetch_tencent_etf_quotes", lambda *_args: good_references_bad_continuity)
    result = fallback.repair_fund_daily_from_tencent(
        "20260731", ["511880.SH", "512890.SH", "510880.SH"],
    )
    assert result["status"] == "previous_close_cross_check_failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM dc_tushare_raw_records WHERE source_code='tencent_etf_quote_fallback'"
    ).fetchone()[0] == 0


def test_fallback_accepts_validated_previous_fallback_close(monkeypatch):
    connection = _db()
    _insert_bar(connection, "511880.SH", "20260731", {
        "open": 100.70, "high": 100.71, "low": 100.69, "close": 100.70,
        "pre_close": 100.69, "vol": 1_000, "amount": 100_000,
        "verification_status": "provider_contract_and_previous_close_cross_checked",
    }, source="tencent_etf_quote_fallback")
    connection.commit()
    quotes = fallback._parse_quotes(
        _quote_line(
            "511880", trade_date="20260803", open_price=100.70,
            pre_close=100.70, close=100.71, high=100.72, low=100.69,
        ),
        "20260803",
    )
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))
    monkeypatch.setattr(fallback, "fetch_tencent_etf_quotes", lambda *_args: quotes)

    result = fallback.repair_fund_daily_from_tencent("20260803", ["511880.SH"])

    assert result["ok"] is True
    assert result["rows_written"] == 1


def test_fallback_uses_sina_only_for_missing_previous_close(monkeypatch):
    connection = _db()
    connection.commit()
    quotes = fallback._parse_quotes(
        _quote_line(
            "512890", trade_date="20260814", open_price=1.157,
            pre_close=1.157, close=1.155, high=1.161, low=1.151,
        ),
        "20260814",
    )
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))
    monkeypatch.setattr(fallback, "fetch_tencent_etf_quotes", lambda *_args: quotes)
    monkeypatch.setattr(
        fallback,
        "fetch_sina_etf_previous_closes",
        lambda *_args: {
            "512890.SH": {
                "close": 1.157,
                "source_code": fallback.SOURCE_SINA_ETF_HISTORY,
            },
        },
    )

    result = fallback.repair_fund_daily_from_tencent("20260814", ["512890.SH"])

    assert result["ok"] is True
    row = connection.execute(
        "SELECT payload_json FROM dc_tushare_raw_records WHERE ts_code='512890.SH'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["previous_close_source"] == fallback.SOURCE_SINA_ETF_HISTORY


def test_fallback_preserves_incomplete_primary_row_as_retryable_evidence(monkeypatch):
    connection = _db()
    _insert_bar(connection, "510880.SH", "20260731", {
        "open": 3.2, "high": 3.3, "low": 3.1, "close": 3.2,
        "pre_close": 3.1, "vol": 10_000,
    })
    connection.commit()
    monkeypatch.setattr(fallback, "_conn", _connection_context(connection))

    result = fallback.repair_fund_daily_from_tencent("20260731", ["510880.SH"])

    assert result["ok"] is False
    assert result["status"] == "primary_execution_fields_invalid"
    assert result["retryable"] is True
    assert result["malformed"] == [{
        "ts_code": "510880.SH", "missing_fields": ["amount"], "record_count": 1,
    }]
    row = connection.execute(
        "SELECT source_code,payload_json FROM dc_tushare_raw_records WHERE ts_code='510880.SH'"
    ).fetchone()
    assert row["source_code"] == "tushare"
    payload = json.loads(row["payload_json"])
    assert "amount" not in payload
