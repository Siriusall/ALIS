from app.services import daily_rebalance


def test_current_price_tries_szse_for_bare_etf(monkeypatch):
    requested = []

    def fake_bar(code, _trade_date):
        requested.append(code)
        return {"close": 1.23} if code == "159001.SZ" else None

    monkeypatch.setattr(daily_rebalance, "defensive_asset_bar", fake_bar)

    assert daily_rebalance._current_price("159001", "20260811") == 1.23
    assert requested == ["159001.SH", "159001.SZ"]


def test_batch_quotes_queries_both_exchanges_for_bare_etf(monkeypatch):
    requested = []

    def fake_bars(codes, _trade_date):
        requested.extend(sorted(codes))
        return {"159001.SZ": {"close": 1.23}}

    monkeypatch.setattr(daily_rebalance, "defensive_asset_bars", fake_bars)

    prices, _names = daily_rebalance._batch_quotes_and_names(["159001"], "20260811")

    assert requested == ["159001.SH", "159001.SZ"]
    assert prices == {"159001": 1.23}
