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


def test_research_proposal_only_keeps_exact_frozen_evidence_refs():
    context = {
        "period": "2026-07",
        "learning_signals": [{"code": "high_turnover", "value": 1.2}],
        "error_breakdown": {"review_error_types": {"数据质量": 2}},
        "benchmark_coverage": {"points": 10},
    }
    evidence = agent._frozen_evidence(context)
    supported = "learning_signals.high_turnover=1.2"
    proposal = agent._normalise_proposal(
        {
            "hypotheses": [
                {
                    "target": "strategy",
                    "title": "换手审计",
                    "rationale": "x",
                    "proposed_experiment": "y",
                    "evidence_refs": [supported, "不存在的证据=99"],
                    "confidence": 0.8,
                }
            ]
        },
        context,
        evidence_catalog=set(evidence["evidence_catalog"]),
    )

    hypothesis = proposal["hypotheses"][0]
    assert hypothesis["evidence_refs"] == [supported]
    assert hypothesis["unsupported_evidence_refs"] == ["不存在的证据=99"]


def test_research_proposal_audit_json_is_fail_closed():
    assert agent._json_object('{"status":"accepted"}') == {"status": "accepted"}
    assert agent._json_object("not-json") == {}
    assert agent._json_object("[]") == {}


def test_research_agent_retries_malformed_json_once_and_fails_closed(monkeypatch):
    monkeypatch.setattr(agent, "_ensure_tables", lambda: None)
    monkeypatch.setattr(agent, "get_env_value", lambda *_args, **_kwargs: "key")
    monkeypatch.setattr(
        agent,
        "latest_performance_learning_context",
        lambda: {"period": "2026-08", "learning_signals": []},
    )
    monkeypatch.setattr(agent, "llm_token_budget_status", lambda: {"status": "available"})

    class Cursor:
        def fetchone(self):
            return None

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return Cursor()

    monkeypatch.setattr(agent, "_conn", Conn)
    purposes = []

    def malformed(**kwargs):
        purposes.append(kwargs["purpose"])
        raise ValueError("llm_response_not_json")

    monkeypatch.setattr(agent, "_call_deepseek_json", malformed)

    result = agent.run_llm_research_agent()

    assert result["ok"] is True
    assert result["status"] == "skipped_invalid_response"
    assert result["retry_count"] == 1
    assert result["production_change_forbidden"] is True
    assert purposes == ["research_proposal", "research_proposal_json_retry"]
