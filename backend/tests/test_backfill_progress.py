from app.data_platform.services import backfill


def test_market_batch_exposes_per_date_progress(monkeypatch):
    updates = []
    synced = []

    monkeypatch.setattr(backfill, "_set", lambda **kwargs: updates.append(kwargs))
    monkeypatch.setattr(
        backfill,
        "sync_market_bundle",
        lambda trade_date, **kwargs: synced.append((trade_date, kwargs)) or {"ok": True, "trade_date": trade_date},
    )

    result = backfill._run_step(
        {
            "action": "sync_market_batch",
            "params": {
                "trade_dates": ["20240102", "20240103"],
                "missing_apis": {"20240102": ["daily"]},
            },
        }
    )

    assert result["ok"] is True
    assert result["dates"] == ["20240102", "20240103"]
    assert synced[0] == ("20240102", {"include_suspend": False, "apis": ["daily"]})
    assert updates[0]["activity"]["date_index"] == 1
    assert updates[0]["activity"]["date_total"] == 2
    assert updates[1]["activity"]["date_index"] == 2
    assert updates[1]["activity"]["apis"] == ["daily", "daily_basic", "stk_limit"]


def test_blocked_only_plan_is_not_reported_as_fully_complete(monkeypatch):
    monkeypatch.setattr(
        backfill,
        "build_backfill_plan",
        lambda **_kwargs: {
            "summary": {"steps_needed": 0, "steps_blocked": 1, "steps_done": 21},
            "steps": [{"key": "etf_constituents_vip", "status": "blocked"}],
        },
    )
    monkeypatch.setattr(backfill, "set_json", lambda *_args, **_kwargs: None)
    backfill._state.clear()
    backfill._state.update({"running": False, "phase": "idle"})

    result = backfill.start_backfill(enqueue_stream=False)

    assert result["reason"] == "blocked_only"
    assert result["phase"] == "completed_with_gaps"
    assert "1 项无法自动处理的缺口" in result["message"]
