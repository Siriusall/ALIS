import json
import sqlite3
from contextlib import contextmanager

from app.data_platform.services import derived_limit_price
from app.data_platform.services.derived_limit_price import calculate_limit_prices


def _calculate(**overrides):
    values = {
        "ts_code": "600000.SH",
        "trade_date": "20260728",
        "pre_close": "10.00",
        "list_date": "19991110",
        "listing_open_session_number": 100,
        "is_st": False,
        "name": "浦发银行",
    }
    values.update(overrides)
    return calculate_limit_prices(**values)


def test_main_board_uses_ten_percent_after_2026_rule_change_even_when_st():
    result = _calculate(pre_close="35.05", is_st=True, name="ST示例")

    assert result["ok"] is True
    assert result["limit_rate"] == 0.1
    assert result["up_limit"] == 38.56
    assert result["down_limit"] == 31.55


def test_main_board_st_uses_five_percent_before_2026_rule_change():
    result = _calculate(trade_date="20260703", is_st=True, name="*ST示例")

    assert result["ok"] is True
    assert result["limit_rate"] == 0.05
    assert result["up_limit"] == 10.5
    assert result["down_limit"] == 9.5


def test_growth_star_and_beijing_boards_use_their_exchange_rates():
    assert _calculate(ts_code="300001.SZ")["limit_rate"] == 0.2
    assert _calculate(ts_code="688001.SH")["limit_rate"] == 0.2
    assert _calculate(ts_code="920001.BJ")["limit_rate"] == 0.3


def test_legacy_s_share_keeps_five_percent_rate():
    result = _calculate(ts_code="600182.SH", name="S佳通")

    assert result["ok"] is True
    assert result["limit_rate"] == 0.05


def test_initial_no_limit_window_and_unknown_board_are_rejected():
    assert _calculate(listing_open_session_number=5)["reason"] == "initial_no_limit_window"
    assert _calculate(ts_code="900001.SH")["reason"] == "unsupported_security_board"


def test_rule_calibration_accepts_a_tushare_proxy_reference_slice(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, source_code TEXT, trade_date TEXT, ts_code TEXT, payload_json TEXT
        );
        CREATE TABLE dc_stock_master (ts_code TEXT, name TEXT, list_date TEXT);
        CREATE TABLE dc_trade_calendar (cal_date TEXT, is_open INTEGER);
    """)
    connection.execute("INSERT INTO dc_stock_master VALUES(?,?,?)", ("600000.SH", "浦发银行", "19991110"))
    connection.execute("INSERT INTO dc_trade_calendar VALUES(?,?)", ("20260814", 1))
    connection.execute(
        "INSERT INTO dc_tushare_raw_records VALUES(?,?,?,?,?)",
        ("daily", "tushare_proxy", "20260814", "600000.SH", json.dumps({"pre_close": 10})),
    )
    connection.execute(
        "INSERT INTO dc_tushare_raw_records VALUES(?,?,?,?,?)",
        ("stk_limit", "tushare_proxy", "20260814", "600000.SH",
         json.dumps({"up_limit": 11, "down_limit": 9})),
    )

    @contextmanager
    def open_connection():
        yield connection

    monkeypatch.setattr(derived_limit_price, "_conn", open_connection)
    monkeypatch.setattr(derived_limit_price, "MIN_CALIBRATION_OBSERVATIONS", 1)
    monkeypatch.setattr(derived_limit_price, "_load_st_codes", lambda _date: (set(), {"ok": True}))
    monkeypatch.setattr(derived_limit_price, "_listing_session_number", lambda *_args: 100)

    result = derived_limit_price.calibrate_rule_engine("20260817")

    assert result["ok"] is True
    assert result["reference_trade_date"] == "20260814"
