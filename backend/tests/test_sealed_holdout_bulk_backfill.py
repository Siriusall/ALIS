from app.services import sealed_strategy_holdout


def test_adj_factor_bulk_backfill_advances_in_batches(monkeypatch):
    states = [
        {"homogeneous": True, "eligible_sessions": 21, "required_sessions": 126,
         "repair_dates": [f"2019{i:04d}" for i in range(25)]},
        {"homogeneous": True, "eligible_sessions": 41, "required_sessions": 126,
         "repair_dates": [f"2020{i:04d}" for i in range(5)]},
    ]
    audits = [
        {"status": "blocked", "eligible_sessions": 41, "required_sessions": 126},
        {"status": "ready", "eligible_sessions": 126, "required_sessions": 126},
    ]
    repaired = []
    heartbeats = []
    monkeypatch.setattr(sealed_strategy_holdout, "sealed_holdout_adj_factor_only_gap", lambda: states.pop(0))
    monkeypatch.setattr(
        sealed_strategy_holdout,
        "repair_oos_hard_data_batch",
        lambda dates, date_gaps=None: repaired.append(list(dates)) or {"ok": True},
    )
    monkeypatch.setattr(sealed_strategy_holdout, "audit_sealed_strategy_holdout", lambda include_dates=False: audits.pop(0))

    result = sealed_strategy_holdout.run_sealed_holdout_adj_factor_backfill(
        task_id=7,
        batch_size=20,
        heartbeat=lambda *args, **kwargs: heartbeats.append((args, kwargs)),
        ensure_active=lambda _task_id: None,
    )

    assert result["status"] == "ready"
    assert [len(batch) for batch in repaired] == [20, 5]
    assert heartbeats[0][1]["checkpoint"]["eligible_sessions"] == 41
    assert heartbeats[-1][1]["checkpoint"]["eligible_sessions"] == 126


def test_adj_factor_bulk_backfill_stops_when_gap_becomes_mixed(monkeypatch):
    monkeypatch.setattr(
        sealed_strategy_holdout,
        "sealed_holdout_adj_factor_only_gap",
        lambda: {"homogeneous": False},
    )
    monkeypatch.setattr(
        sealed_strategy_holdout,
        "audit_sealed_strategy_holdout",
        lambda include_dates=False: {"status": "blocked", "eligible_sessions": 21},
    )
    try:
        sealed_strategy_holdout.run_sealed_holdout_adj_factor_backfill(
            task_id=7, batch_size=20,
            heartbeat=lambda *args, **kwargs: None,
            ensure_active=lambda _task_id: None,
        )
    except RuntimeError as exc:
        assert "不再是单一 adj_factor" in str(exc)
    else:
        raise AssertionError("mixed gaps must stop the bulk task")
