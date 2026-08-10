from app.services import llm_proposal_router as router


def test_router_only_invokes_whitelisted_high_confidence_routes(monkeypatch):
    calls = []
    monkeypatch.setattr(router, "_ensure_tables", lambda: None)
    monkeypatch.setattr(router, "find_rejected_hypothesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(router, "upsert_experiment_memory", lambda **_kwargs: {})
    monkeypatch.setattr(router, "assess_llm_research_proposals", lambda: {"assessed_count": 1, "accepted_by_proposal": {7: {0}}})
    monkeypatch.setattr(router, "_routes", lambda: {"strategy": ("strategy_policy_whitelist", lambda: calls.append("strategy") or {"ok": True})})

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=()):
            class Result:
                def fetchall(self):
                    return [{"id": 7, "proposal_key": "key", "source_period": "2026-07", "payload_json": '{"hypotheses":[{"target":"strategy","title":"x","confidence":0.8},{"target":"trade","confidence":1}]}'}] if "SELECT * FROM llm_research_proposals" in sql else []
                def fetchone(self): return None
            return Result()
    monkeypatch.setattr(router, "_conn", lambda: Conn())
    result = router.route_llm_research_proposals()
    assert calls == ["strategy"]
    assert result["routed_count"] == 1
    assert result["skipped"][0]["reason"] == "proposal_quality_gate_rejected"


def test_router_does_not_retry_formally_rejected_llm_hypothesis(monkeypatch):
    calls = []
    monkeypatch.setattr(router, "_ensure_tables", lambda: None)
    monkeypatch.setattr(router, "assess_llm_research_proposals", lambda: {"assessed_count": 1, "accepted_by_proposal": {7: {0}}})
    monkeypatch.setattr(router, "find_rejected_hypothesis", lambda *_args, **_kwargs: {"outcome_stage": "shadow"})
    monkeypatch.setattr(router, "_routes", lambda: {"strategy": ("strategy_policy_whitelist", lambda: calls.append("strategy") or {"ok": True})})

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=()):
            class Result:
                def fetchall(self):
                    return [{"id": 7, "proposal_key": "key", "source_period": "2026-07", "payload_json": '{"hypotheses":[{"target":"strategy","title":"Top8","proposed_experiment":"top_n=8","confidence":0.8}]}'}] if "SELECT * FROM llm_research_proposals" in sql else []
            return Result()
    monkeypatch.setattr(router, "_conn", lambda: Conn())

    result = router.route_llm_research_proposals()

    assert calls == []
    assert result["routed_count"] == 0
    assert result["skipped"] == [{"proposal_id": 7, "hypothesis_index": 0, "reason": "formally_rejected_hypothesis"}]


def test_router_scans_past_already_routed_old_proposals(monkeypatch):
    calls = []
    inserted = []
    monkeypatch.setattr(router, "_ensure_tables", lambda: None)
    monkeypatch.setattr(router, "find_rejected_hypothesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(router, "upsert_experiment_memory", lambda **_kwargs: {})
    monkeypatch.setattr(
        router,
        "assess_llm_research_proposals",
        lambda: {
            "assessed_count": 4,
            "accepted_by_proposal": {1: {0}, 2: {0}, 3: {0}, 8: {0}},
        },
    )
    monkeypatch.setattr(
        router,
        "_routes",
        lambda: {
            "model": (
                "model_challenger_cycle",
                lambda: calls.append("model") or {"ok": True},
            )
        },
    )
    proposals = [
        {
            "id": proposal_id,
            "proposal_key": f"key-{proposal_id}",
            "source_period": "2026-07",
            "payload_json": (
                '{"hypotheses":[{"target":"model","title":"x",'
                '"confidence":0.8}]}'
            ),
        }
        for proposal_id in (1, 2, 3, 8)
    ]

    class Result:
        def __init__(self, *, rows=None, row=None):
            self.rows = rows or []
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            if "SELECT * FROM llm_research_proposals" in sql:
                return Result(rows=proposals)
            if "SELECT id,status FROM llm_research_routes" in sql:
                return Result(row={"id": params[0]} if int(params[0]) < 8 else None)
            if "INSERT INTO llm_research_routes" in sql:
                inserted.append(tuple(params))
            return Result()

    monkeypatch.setattr(router, "_conn", lambda: Conn())

    result = router.route_llm_research_proposals()

    assert calls == ["model"]
    assert result["handled_proposal_count"] == 1
    assert result["routed"][0]["proposal_id"] == 8
    assert inserted[0][0] == 8


def test_factor_route_uses_persistent_evolution_task(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.services.evolution_orchestrator.create_evolution_task",
        lambda **kwargs: captured.update(kwargs) or {"id": 2400, "status": "queued"},
    )

    result = router._enqueue_factor_daily_evaluation()

    assert captured["task_type"] == "factor_daily_cycle"
    assert captured["dedupe_key"].startswith("factor_daily_cycle:")
    assert captured["payload"] == {"trigger": "llm_proposal_router"}
    assert result == {
        "ok": True,
        "status": "queued",
        "task_id": 2400,
        "persistent": True,
        "production_change_forbidden": True,
    }
