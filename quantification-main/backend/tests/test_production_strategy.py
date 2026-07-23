from __future__ import annotations

from app.services import production_strategy


def _bundle() -> dict:
    return {
        "schema_version": production_strategy.CHAMPION_SCHEMA_VERSION,
        "name": "ALIS Champion 综合生产策略",
        "role": "champion",
        "strategy_id": 1,
        "selection_strategy": {"id": 1, "name": "ALIS Champion 综合策略", "type": "formula", "content": {}},
        "strategy_controller": {"id": 31, "version": "controller-v1"},
        "model_bindings": {},
        "portfolio_optimizer": {"implementation": "empirical_cvar_slsqp_v2"},
        "execution_contract": {"signal": "T_close", "execution": "T_plus_1_open"},
    }


def test_ensure_champion_reuses_identical_active_binding(monkeypatch):
    active = {"id": 40, "version": "champion-v1", "config": _bundle(), "is_active": True}
    monkeypatch.setattr(production_strategy, "_build_bundle_config", _bundle)
    monkeypatch.setattr(production_strategy, "get_active_model_version", lambda key: active)
    monkeypatch.setattr(production_strategy, "create_model_version", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")))

    assert production_strategy.ensure_production_strategy_champion()["id"] == 40


def test_force_rebind_creates_new_champion_version(monkeypatch):
    old = _bundle()
    old["strategy_controller"] = {"id": 30, "version": "controller-v0"}
    monkeypatch.setattr(production_strategy, "_build_bundle_config", _bundle)
    monkeypatch.setattr(production_strategy, "get_active_model_version", lambda key: {"id": 39, "config": old})
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return 41

    monkeypatch.setattr(production_strategy, "create_model_version", create)
    monkeypatch.setattr(production_strategy, "get_model_version", lambda version_id: {"id": version_id, "version": captured["version"], "config": captured["config"], "is_active": True})
    monkeypatch.setattr(production_strategy, "update_model_version_status", lambda *args, **kwargs: None)

    result = production_strategy.ensure_production_strategy_champion(force_rebind=True)
    assert result["id"] == 41
    assert captured["model_key"] == production_strategy.CHAMPION_MODEL_KEY
    assert captured["is_active"] is True
    assert captured["config"]["strategy_controller"]["id"] == 31
