from app.services import stock_selector


def test_stock_selector_does_not_silently_fallback_to_realtime(monkeypatch):
    monkeypatch.setattr(stock_selector, "read_consolidated_cache", lambda **_kwargs: None)
    calls = []
    monkeypatch.setattr(
        stock_selector,
        "fetch_a_share_spot",
        lambda **kwargs: calls.append(kwargs) or [],
    )

    assert stock_selector.select_affordable_stocks() == []
    assert calls == []

    stock_selector.select_affordable_stocks(allow_realtime_fallback=True)
    assert calls == [{"limit": None}]
