import pytest

from app.services import position_console
from app.api.routes import positions
from fastapi import HTTPException


def test_bare_szse_etf_uses_sz_suffix(monkeypatch):
    requested = []

    def fake_batch(codes, _trade_date, *, limit, exact_date=False, available_as_of_time=None):
        requested.extend(codes)
        assert exact_date is True
        assert available_as_of_time
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


def test_position_price_resolver_keeps_beijing_stock_namespace(monkeypatch):
    captured = {}

    class FakeConn:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, query, params):
            captured["params"] = params
            class Result:
                def fetchall(self):
                    return []
            return Result()

    monkeypatch.setattr(position_console, "data_conn", lambda: FakeConn())
    position_console.resolve_stock_position_prices(["430001"], "20260811")
    assert "430001.BJ" in captured["params"]


def test_positions_route_rejects_missing_exact_session_price(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(positions, "get_positions_store", lambda: [{
        "id": 1, "symbol": "000001", "name": "测试证券", "price": 10.0,
        "quantity": 100, "side": "BUY", "bought_at": "2026-08-14T09:30:00+08:00",
    }])
    monkeypatch.setattr(positions, "data_conn", lambda: Connection())
    monkeypatch.setattr(positions, "target_trade_date", lambda _c: "20260814")
    monkeypatch.setattr(position_console, "resolve_position_prices", lambda *_args, **_kwargs: ({}, ""))

    with pytest.raises(HTTPException) as raised:
        positions.get_positions()

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "position_market_data_not_ready"
    assert raised.value.detail["missing_symbols"] == ["000001"]
