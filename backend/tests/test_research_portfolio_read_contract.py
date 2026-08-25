from __future__ import annotations

from contextlib import contextmanager
import json

from app.api.routes import research
from app.services import portfolio_console


def test_portfolio_console_projects_only_competition_evidence_on_postgres(monkeypatch) -> None:
    captured = {}

    class Result:
        @staticmethod
        def fetchone():
            return {
                "id": 47,
                "trade_date": "20260825",
                "version": 1,
                "status": "planned",
                "generated_at": "2026-08-25T19:04:56+08:00",
                "proposal_json": json.dumps({
                    "portfolio_method_competition": {
                        "ok": True,
                        "selected_method": "hrp",
                        "evaluations": [{"method": "hrp", "eligible": True}],
                    },
                    "risk_budget": {"status": "ready"},
                }),
            }

    class Connection:
        _is_postgres = True

        def execute(self, sql, _params=()):
            captured["sql"] = str(sql)
            return Result()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(portfolio_console, "business_conn", connection)

    result = portfolio_console.build_portfolio_console_research()

    assert "jsonb_build_object" in captured["sql"]
    assert "selection_snapshot" not in captured["sql"]
    assert result["portfolio_optimization"]["optimizer"]["method_competition"]["selected_method"] == "hrp"


def test_live_portfolio_risk_does_not_fall_back_to_a_prior_ready_session(monkeypatch) -> None:
    @contextmanager
    def fake_conn():
        yield object()

    research._portfolio_risk_cache.clear()
    monkeypatch.setattr(research, "data_conn", fake_conn)
    monkeypatch.setattr(research, "target_trade_date", lambda _conn: "20260817")
    monkeypatch.setattr(research, "decision_data_contract", lambda *_args, **_kwargs: {
        "ok": False,
        "issues": [{"code": "snapshot_missing", "message": "target session incomplete"}],
    })
    monkeypatch.setattr(
        research,
        "build_live_portfolio_risk",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not price a prior session")),
    )

    result = research.research_live_portfolio_risk(lookback=120)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["data_available"] is False
    assert result["target_trade_date"] == "20260817"
    assert result["error"]["code"] == "decision_data_not_ready"
