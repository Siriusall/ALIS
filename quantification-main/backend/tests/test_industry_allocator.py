from app.services import industry_allocator


def _research():
    return {"items": [
        {"industry_name": "强行业", "state": {"score": 86, "coverage": .7, "evidence_points": 40, "status": "ready", "lifecycle": "景气上行"}},
        {"industry_name": "普通行业", "state": {"score": 58, "coverage": .8, "evidence_points": 50, "status": "ready", "lifecycle": "稳定"}},
    ]}


def _members(_symbols=None):
    return (
        {"000001": "强行业", "000002": "强行业", "000003": "普通行业"},
        {"强行业": [{"symbol": "000001", "name": "甲"}, {"symbol": "000002", "name": "乙"}], "普通行业": [{"symbol": "000003", "name": "丙"}]},
    )


def _themes(**_):
    return {"items": [
        {"theme_code": "industry:强行业", "score": 82, "promotable": 1, "lifecycle": "扩散", "payload": {"knowledge_graph": {"industry": "强行业"}}},
        {"theme_code": "industry:普通行业", "score": 50, "promotable": 0, "lifecycle": "退潮", "payload": {"knowledge_graph": {"industry": "普通行业"}}},
    ]}


def test_weak_market_only_allocates_to_strong_industry(monkeypatch):
    monkeypatch.setattr(industry_allocator, "list_industries", lambda **_: _research())
    monkeypatch.setattr(industry_allocator, "_industry_members", _members)
    monkeypatch.setattr(industry_allocator, "list_themes", _themes)
    result = industry_allocator.build_industry_allocation(
        symbols=["000001", "000002", "000003"], symbol_scores={"000001": 90, "000002": 70, "000003": 95},
        equity_budget=.4, market_state="S8",
    )
    assert result["industry_targets"] == {"强行业": .1}
    assert result["allocated_equity_budget"] == .1
    assert result["unallocated_equity_budget"] == .3


def test_stock_weights_follow_industry_budget(monkeypatch):
    monkeypatch.setattr(industry_allocator, "list_industries", lambda **_: _research())
    monkeypatch.setattr(industry_allocator, "_industry_members", _members)
    monkeypatch.setattr(industry_allocator, "list_themes", _themes)
    allocation = industry_allocator.build_industry_allocation(
        symbols=["000001", "000002", "000003"], symbol_scores={}, equity_budget=.5, market_state="S0",
    )
    weights = industry_allocator.apply_industry_targets(
        {"000001": .3, "000002": .1, "000003": .6}, allocation, equity_budget=.5,
    )
    assert round(weights["000001"] / weights["000002"], 6) == 3
    assert round(weights["000001"] + weights["000002"], 6) == .25
    assert "000003" not in weights
    assert round(sum(weights.values()), 6) == .25
