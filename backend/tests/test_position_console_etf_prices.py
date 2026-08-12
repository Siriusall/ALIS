from app.services import position_console
from app.api.routes import positions


def test_bare_szse_etf_uses_sz_suffix(monkeypatch):
    requested = []

    def fake_batch(codes, _trade_date, *, limit):
        requested.extend(codes)
        return {"159001.SZ": ({"close": 1.23, "available_time": "2026-08-11T15:00:00"}, [])}

    monkeypatch.setattr(position_console, "defensive_asset_series_batch", fake_batch)

    prices, source_time = position_console.resolve_position_prices(["159001"], "20260811")

    assert requested == ["159001.SZ"]
    assert prices == {"159001": 1.23}
    assert source_time == "2026-08-11T15:00:00"


def test_bare_sse_etf_keeps_sh_suffix():
    assert position_console._etf_ts_code("510300") == "510300.SH"


def test_positions_fallback_uses_szse_for_bare_etf():
    assert positions._etf_ts_code("159001") == "159001.SZ"
