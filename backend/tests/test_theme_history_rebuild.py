from app.services import news_theme_cluster, theme_engine


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return _Rows(self.handler(sql, params))


def test_news_history_uses_publish_and_available_time_instead_of_empty_trade_date(monkeypatch):
    connection = _Connection(lambda _sql, _params: [])
    monkeypatch.setattr(news_theme_cluster, "_conn", lambda: connection)
    monkeypatch.setattr(news_theme_cluster, "_load_stock_names", lambda: {})

    result = news_theme_cluster.cluster_news_themes(
        as_of_time="2024-02-01T23:59:59"
    )

    sql, params = connection.calls[0]
    assert "payload_json::jsonb->>'pub_time'" in sql
    assert "available_time<=?" in sql
    assert "trade_date<=?" not in sql
    assert params == (
        "2024-02-01T23:59:59",
        "2024-02-01T23:59:59",
    )
    assert result["reason"] == "no_news_data"


def test_current_news_snapshot_uses_latest_market_session(monkeypatch):
    def handler(sql, _params):
        if "MAX(trade_date)" in sql:
            return [{"value": "20260828"}]
        return []

    connection = _Connection(handler)
    monkeypatch.setattr(news_theme_cluster, "_conn", lambda: connection)
    monkeypatch.setattr(news_theme_cluster, "_load_stock_names", lambda: {})

    result = news_theme_cluster.cluster_news_themes()

    assert result["reason"] == "no_news_data"
    assert connection.calls[1][1] == ("20260831",)


def test_theme_history_accepts_full_walk_forward_session_target(monkeypatch):
    def handler(sql, _params):
        if "MAX(trade_date)" in sql:
            return [{"value": "20260727"}]
        if "HAVING SUM" in sql:
            return [{"value": "20260727"}]
        if "FROM dc_trade_calendar" in sql:
            return []
        return []

    connection = _Connection(handler)
    monkeypatch.setattr(theme_engine, "_conn", lambda: connection)

    result = theme_engine.materialize_theme_snapshot_history(
        target_sessions=1577,
        limit_dates=1,
    )

    assert result["target_sessions"] == 1577


def test_news_company_matching_prefers_embedded_stock_codes(monkeypatch):
    monkeypatch.setattr(
        news_theme_cluster,
        "_load_stock_names",
        lambda: {"300750.SZ": "宁德时代", "600000.SH": "浦发银行"},
    )

    result = news_theme_cluster._match_companies(
        '<a href="/stock/300750">公司公告</a>'
    )

    assert result == [{"ts_code": "300750.SZ", "name": "宁德时代"}]
