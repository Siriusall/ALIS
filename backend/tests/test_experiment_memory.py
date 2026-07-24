from app.services import experiment_memory as memory


def test_memory_fingerprint_is_stable_for_equivalent_hypothesis():
    assert memory.hypothesis_fingerprint("strategy", {"template": "top8", "source": "x"}) == memory.hypothesis_fingerprint(
        "strategy", {"source": "x", "template": "top8"}
    )


def test_only_formal_outcome_can_reject_candidate_memory(monkeypatch):
    captured = {}
    monkeypatch.setattr(memory, "upsert_experiment_memory", lambda **kwargs: captured.update(kwargs) or kwargs)
    version = {"id": 77, "config": {"experiment_memory": {"target": "strategy", "hypothesis": {"template": "top8"}}}}

    result = memory.record_model_experiment_outcome(version, stage="formal_oos", rejected=True, evidence={"status": "failed"})

    assert result["status"] == "rejected"
    assert captured["source_model_version_id"] == 77


def test_non_formal_outcome_cannot_reject_candidate_memory():
    version = {"id": 77, "config": {"experiment_memory": {"target": "strategy", "hypothesis": {"template": "top8"}}}}

    try:
        memory.record_model_experiment_outcome(version, stage="task_error", rejected=True, evidence={})
    except ValueError as exc:
        assert str(exc) == "only_formal_oos_shadow_or_cooling_may_reject_experiment"
    else:
        raise AssertionError("non-formal task error must not reject experiment memory")
