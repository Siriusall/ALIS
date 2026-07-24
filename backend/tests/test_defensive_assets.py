from app.services import defensive_assets


def test_defensive_policy_fills_exact_complement_of_equity_target(monkeypatch):
    monkeypatch.setattr(defensive_assets, "list_defensive_assets", lambda as_of_date=None: {
        "items": [
            {"ts_code": "511880.SH", "asset_class": "money", "status": "ready"},
            {"ts_code": "511010.SH", "asset_class": "bond", "status": "ready"},
            {"ts_code": "518880.SH", "asset_class": "gold", "status": "ready"},
            {"ts_code": "512890.SH", "asset_class": "low_volatility", "status": "ready"},
            {"ts_code": "510880.SH", "asset_class": "dividend", "status": "ready"},
        ]
    })
    policy = defensive_assets.defensive_allocation_for_state("S9", as_of_date="20260715", equity_target_weight=.35)
    assert policy["status"] == "ready"
    assert abs(policy["target_weight"] - .65) < .001
    assert abs(sum(policy["target_weights"].values()) - .65) < .001
    assert policy["full_investment_target"] == 1.0
    assert max(policy["target_weights"].values()) <= .45


def test_no_equity_candidates_allocate_full_account_to_defensive_assets(monkeypatch):
    monkeypatch.setattr(defensive_assets, "list_defensive_assets", lambda as_of_date=None: {
        "items": [{**item, "status": "ready", "momentum_20d": 0.0} for item in defensive_assets.DEFENSIVE_ASSETS]
    })
    policy = defensive_assets.defensive_allocation_for_state("S10", as_of_date="20260715", equity_target_weight=0)
    assert policy["status"] == "ready"
    assert abs(sum(policy["target_weights"].values()) - 1.0) < .001
    assert max(policy["target_weights"].values()) <= .45
