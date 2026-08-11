from app.services import stock_screener


def test_daily_screener_surfaces_thesis_and_promotion_exceptions(monkeypatch):
    monkeypatch.setattr(stock_screener, "_get_stock_universe", lambda _date: [{"ts_code": "000001.SZ"}])
    monkeypatch.setattr(stock_screener, "_prefilter", lambda rows: [{"stock": rows[0], "pre_score": 30}])
    monkeypatch.setattr(stock_screener, "build_stock_research", lambda *args, **kwargs: {
        "ok": True,
        "research_state": {"scores": {"quality": 80, "growth": 80, "valuation": 80}, "confidence": 0.8, "eligible": True},
        "classification": {"lifecycle": "growth"},
    })
    monkeypatch.setattr(stock_screener, "upsert_pool_member", lambda **kwargs: None)
    monkeypatch.setattr(stock_screener, "generate_thesis_for_stock", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("thesis write failed")))
    monkeypatch.setattr(stock_screener, "list_pool_members", lambda pool: [{"ts_code": "000001.SZ", "reason": "score=80 conf=0.80"}] if pool == "watch" else [])
    monkeypatch.setattr(stock_screener, "transition_pool_member", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("promotion write failed")))
    monkeypatch.setattr(stock_screener, "build_live_portfolio_risk", lambda: {"ok": True})

    result = stock_screener.run_daily_screener(trade_date="20260811", top_n=1, research_limit=1)

    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["error_count"] == 2
    assert any(value.startswith("thesis_000001.SZ") for value in result["errors"])
    assert any(value.startswith("promotion_000001.SZ") for value in result["errors"])
    assert result["portfolio_risk"] == {"ok": True}
