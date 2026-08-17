from app.services import stock_selector


def test_stock_selector_does_not_silently_fallback_to_realtime(monkeypatch):
    class Cursor:
        def fetchone(self):
            return {"value": "20260814"}
        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, *_args, **_kwargs):
            return Cursor()

    monkeypatch.setattr(stock_selector, "data_conn", lambda: Connection())
    monkeypatch.setattr(stock_selector, "_current_selection_trade_date", lambda: "20260814")
    assert stock_selector.select_affordable_stocks() == []
    # The compatibility flag is intentionally ignored: no realtime source is
    # allowed to synthesize an exact T-day snapshot.
    assert stock_selector.select_affordable_stocks(allow_realtime_fallback=True) == []
