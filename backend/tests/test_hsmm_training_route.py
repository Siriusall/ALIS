from app.api.routes import model_strategy


def test_hsmm_training_route_runs_the_shadow_trainer(monkeypatch) -> None:
    expected = {"ok": True, "version": "hsmm-shadow-test", "validation": {"status": "passed"}}
    monkeypatch.setattr(model_strategy, "train_hsmm_shadow", lambda: expected)

    assert model_strategy.model_strategy_train_hsmm_shadow() == expected
