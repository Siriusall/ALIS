from app.services import llm_research_agent as agent


def test_research_proposal_filters_untrusted_targets_and_bounds_fields():
    proposal = agent._normalise_proposal({
        "summary": "基于复盘提出受控实验。",
        "hypotheses": [
            {"title": "因子稳定性", "target": "factor", "rationale": "IC波动", "proposed_experiment": "观察20窗口", "evidence_refs": ["monthly_report"], "confidence": .8, "safety_notes": "不得直接上线"},
            {"title": "非法动作", "target": "trade", "rationale": "", "proposed_experiment": "买入", "confidence": 1},
        ],
        "limitations": ["样本不足"],
    }, {"period": "2026-07"})

    assert proposal["source_period"] == "2026-07"
    assert proposal["policy"].startswith("proposal_only")
    assert [item["target"] for item in proposal["hypotheses"]] == ["factor"]
    assert proposal["hypotheses"][0]["confidence"] == .8


def test_research_agent_skips_without_api_key(monkeypatch):
    monkeypatch.setattr(agent, "_ensure_tables", lambda: None)
    monkeypatch.setattr(agent, "get_env_value", lambda *_args, **_kwargs: "")
    assert agent.run_llm_research_agent() == {
        "ok": True,
        "status": "skipped_not_configured",
        "proposal_count": 0,
    }


def test_research_confidence_accepts_bounded_words():
    assert agent._confidence("medium") == .5
    assert agent._confidence("高") == .75
    assert agent._confidence("unknown") == 0.0


def test_research_lists_do_not_split_model_strings_into_characters():
    assert agent._string_list("样本不足", limit=8, item_limit=300) == ["样本不足"]


def test_research_proposal_audit_json_is_fail_closed():
    assert agent._json_object('{"status":"accepted"}') == {"status": "accepted"}
    assert agent._json_object("not-json") == {}
    assert agent._json_object("[]") == {}
