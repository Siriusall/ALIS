import json
import sqlite3

from app.data_platform.services import tencent_limit_fallback as fallback
from app.data_platform.services.tencent_limit_fallback import _parse_quotes


def test_tushare_proxy_rows_are_eligible_limit_cross_check_references(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE dc_tushare_raw_records (
            api_name TEXT, trade_date TEXT, source_code TEXT, ts_code TEXT, payload_json TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO dc_tushare_raw_records VALUES(?,?,?,?,?)",
        ("stk_limit", "20260814", "tushare_proxy", "600000.SH",
         json.dumps({"up_limit": 11, "down_limit": 9})),
    )

    assert fallback._reference_values(connection, "20260814") == {"600000.SH": (11.0, 9.0)}


def test_parse_tencent_quote_extracts_same_day_limit_prices():
    fields = [""] * 49
    fields[0] = "1"
    fields[2] = "603001"
    fields[30] = "20260724161453"
    fields[47] = "6.62"
    fields[48] = "5.42"

    parsed = _parse_quotes(f'v_sh603001="{"~".join(fields)}";', "20260724")

    assert parsed["603001.SH"]["up_limit"] == 6.62
    assert parsed["603001.SH"]["down_limit"] == 5.42
    assert parsed["603001.SH"]["source_code"] == "tencent_quote_fallback"


def test_parse_tencent_quote_rejects_wrong_trade_date():
    fields = [""] * 49
    fields[0] = "1"
    fields[2] = "603001"
    fields[30] = "20260723161453"
    fields[47] = "6.62"
    fields[48] = "5.42"

    assert _parse_quotes(f'v_sh603001="{"~".join(fields)}";', "20260724") == {}
