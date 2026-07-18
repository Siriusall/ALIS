from app.services import defensive_assets


def test_s2_defensive_policy_allocates_forty_five_percent(monkeypatch):
    monkeypatch.setattr(defensive_assets, "list_defensive_assets", lambda as_of_date=None: {
        "items": [
            {"ts_code": "511880.SH", "asset_class": "money", "status": "ready"},
            {"ts_code": "511010.SH", "asset_class": "bond", "status": "ready"},
            {"ts_code": "518880.SH", "asset_class": "gold", "status": "ready"},
        ]
    })
    policy = defensive_assets.defensive_allocation_for_state("S2", as_of_date="20260715")
    assert policy["status"] == "ready"
    assert policy["target_weight"] == .45
    assert policy["target_weights"]["511880.SH"] == .20
