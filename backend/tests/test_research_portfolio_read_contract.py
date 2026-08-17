from __future__ import annotations

from contextlib import contextmanager

from app.api.routes import research


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
