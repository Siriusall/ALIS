import pytest

from app.data_platform.services import backfill


def test_statement_timeout_is_a_deferred_step_but_unknown_errors_still_raise(monkeypatch):
    monkeypatch.setattr(
        backfill,
        "_run_step",
        lambda _step: (_ for _ in ()).throw(
            RuntimeError("canceling statement due to statement timeout")
        ),
    )

    result = backfill._run_step_resumable({"key": "date_api_moneyflow"})

    assert result["ok"] is False
    assert result["deferred"] is True
    assert result["error_category"] == "database_statement_timeout"

    monkeypatch.setattr(
        backfill,
        "_run_step",
        lambda _step: (_ for _ in ()).throw(RuntimeError("contract bug")),
    )
    with pytest.raises(RuntimeError, match="contract bug"):
        backfill._run_step_resumable({"key": "date_api_moneyflow"})


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


def test_plan_model_moneyflow_backfill_requeues_without_global_plan(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return self

        def fetchall(self):
            return [{"cal_date": f"20260{index:03d}"} for index in range(1, 91)]

    captured = {}
    states = []
    monkeypatch.setattr(backfill, "_conn", lambda: Connection())
    monkeypatch.setattr(
        backfill,
        "sync_tushare_date_batch",
        lambda api_name, **kwargs: captured.update({"api_name": api_name, **kwargs})
        or {"ok": True, "total": 60, "success": 60},
    )
    monkeypatch.setattr(
        backfill,
        "enqueue_plan_model_data_backfill",
        lambda **kwargs: {"ok": True, "message_id": "next-1", **kwargs},
    )
    monkeypatch.setattr(
        backfill,
        "_set_model_data_backfill_state",
        lambda **kwargs: states.append(kwargs) or kwargs,
    )

    result = backfill._process_plan_model_data_backfill({"phase": "moneyflow"})

    assert result["ok"] is True
    assert captured["api_name"] == "moneyflow"
    assert captured["limit"] == 60
    assert captured["require_rows"] is True
    assert captured["require_point_in_time"] is True
    assert states[-1]["next_message_id"] == "next-1"


def test_moneyflow_available_time_normalization_never_fabricates_observation_clock(monkeypatch):
    batches = []

    class Result:
        rowcount = 5

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, params):
            batches.append(list(params))
            return Result()

    monkeypatch.setattr(backfill, "_conn", lambda: Connection())
    dates = [f"202601{index:02d}" for index in range(1, 26)]

    normalized = backfill._normalize_moneyflow_available_time(
        dates,
        chunk_size=10,
    )

    assert batches == []
    assert normalized == 0


def test_event_disclosure_normalization_stops_when_id_cursor_cannot_advance(monkeypatch):
    class Connection:
        _is_postgres = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return self

        def fetchall(self):
            return [{"cal_date": "20260101"}]

    monkeypatch.setattr(backfill, "_conn", lambda: Connection())

    assert backfill._normalize_event_disclosure_available_time(chunk_size=1_000) == 0


def test_moneyflow_main_cursor_requeues_exact_residual_before_news(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return self

        def fetchall(self):
            return []

    queued = []
    states = []
    monkeypatch.setattr(backfill, "_conn", lambda: Connection())
    monkeypatch.setattr(
        backfill,
        "_moneyflow_required_coverage",
        lambda: (
            ["20200102", "20200103"],
            {"20200102"},
            ["20200103"],
        ),
    )
    monkeypatch.setattr(
        backfill,
        "enqueue_plan_model_data_backfill",
        lambda **kwargs: queued.append(kwargs) or {"ok": True, "message_id": "residual-1"},
    )
    monkeypatch.setattr(
        backfill,
        "_set_model_data_backfill_state",
        lambda **kwargs: states.append(kwargs) or kwargs,
    )

    result = backfill._process_plan_model_data_backfill(
        {"phase": "moneyflow", "cursor": "20260729"}
    )

    assert result["next_phase"] == "moneyflow_residual"
    assert queued[0]["trade_dates"] == ["20200103"]
    assert states[-1]["residual_count"] == 1


def test_plan_model_data_stream_task_routes_to_dedicated_handler(monkeypatch):
    monkeypatch.setattr(
        backfill,
        "_process_plan_model_data_backfill",
        lambda payload: {"ok": True, "payload": payload},
    )

    result = backfill.process_stream_task(
        {
            "task_type": backfill.MODEL_DATA_BACKFILL_TASK,
            "payload_json": '{"phase":"major_news","cursor":"20200107"}',
        }
    )

    assert result["payload"] == {"phase": "major_news", "cursor": "20200107"}


def test_plan_model_data_stream_failure_is_persisted_for_retry(monkeypatch):
    states = []
    monkeypatch.setattr(
        backfill,
        "_process_plan_model_data_backfill",
        lambda _payload: {"ok": False, "error": "upstream_timeout"},
    )
    monkeypatch.setattr(
        backfill,
        "_set_model_data_backfill_state",
        lambda **kwargs: states.append(kwargs) or kwargs,
    )

    result = backfill.process_stream_task(
        {
            "task_type": backfill.MODEL_DATA_BACKFILL_TASK,
            "payload_json": '{"phase":"moneyflow","cursor":"20240507"}',
        }
    )

    assert result["ok"] is False
    assert states[-1]["status"] == "retry_wait"
    assert states[-1]["error"] == "upstream_timeout"


def test_plan_model_data_work_order_exposes_checklist_retry_and_intervention(monkeypatch):
    backfill._model_backfill_status_cache.update({"at": 0.0, "value": None})
    monkeypatch.setattr(
        backfill,
        "get_json",
        lambda _key: {
            "status": "running",
            "phase": "moneyflow",
            "cursor": "20240731",
            "next_message_id": "message-1",
            "updated_at": "2026-07-29T20:13:23",
            "error": "",
        },
    )
    monkeypatch.setattr(
        backfill,
        "_model_moneyflow_checklist",
        lambda: {
            "id": "capital_moneyflow_history",
            "label": "资金持续模型",
            "status": "running",
            "target_raw_sessions": 1607,
            "covered_raw_sessions": 1559,
            "missing_count": 48,
            "window_start": "",
            "window_end": "",
        },
    )
    monkeypatch.setattr(
        backfill,
        "_news_source_progress",
        lambda **_kwargs: {
            "id": "event_major_news_history",
            "label": "事件影响评估",
            "status": "pending",
            "progress": 0.0,
        },
    )
    monkeypatch.setattr(backfill, "task_attempts", lambda _message_id: 1)
    monkeypatch.setattr(
        backfill,
        "stream_health",
        lambda: {"available": True, "pending": 1, "dead_letter": 0},
    )

    result = backfill.get_plan_model_data_backfill_status(cache_seconds=1)

    assert result["schema_version"] == "alis_model_data_backfill_work_order_v3"
    assert result["status"] == "retry_wait"
    assert result["attempt"] == 2
    assert result["checklist"][0]["missing_count"] == 48
    assert "不得伪造" in result["intervention"]["forbidden"]


def test_completed_model_data_work_order_exposes_persisted_contract_audit(monkeypatch):
    backfill._model_backfill_status_cache.update({"at": 0.0, "value": None})
    audit = {
        "schema_version": "alis_plan_model_data_contract_audit_v1",
        "status": "completed",
        "models": {
            "capital_persistence_multitask_v1": {
                "effective_sessions": 1577,
                "formal_history_ready": True,
            },
            "event_impact_assessor_v2": {
                "effective_sessions": 381,
                "formal_history_ready": False,
            },
        },
    }
    monkeypatch.setattr(
        backfill,
        "get_json",
        lambda _key: {
            "status": "completed",
            "contract": backfill.MODEL_DATA_BACKFILL_CONTRACT,
            "phase": "major_news",
            "cursor": "20260729",
            "post_backfill_audit": audit,
        },
    )
    monkeypatch.setattr(
        backfill,
        "_model_moneyflow_checklist",
        lambda: {
            "window_start": "20191211",
            "window_end": "20260729",
            "target_raw_sessions": 1607,
        },
    )
    monkeypatch.setattr(
        backfill,
        "_news_source_progress",
        lambda **_kwargs: {"progress": 1.0, "status": "completed"},
    )
    monkeypatch.setattr(backfill, "task_attempts", lambda _message_id: 0)
    monkeypatch.setattr(backfill, "stream_health", lambda: {"pending": 0})

    result = backfill.get_plan_model_data_backfill_status(cache_seconds=1)

    audit_item = result["checklist"][4]
    assert result["status"] == "completed"
    assert audit_item["status"] == "completed"
    assert audit_item["result"] == audit


def test_major_news_completion_runs_model_data_contract_audit(monkeypatch):
    states = []
    audit = {
        "schema_version": "alis_plan_model_data_contract_audit_v1",
        "status": "completed",
        "all_formal_history_ready": False,
    }
    monkeypatch.setattr(
        backfill,
        "_moneyflow_required_coverage",
        lambda: (["20260729"], {"20260729"}, []),
    )
    monkeypatch.setattr(
        backfill,
        "backfill_major_news_history",
        lambda **_kwargs: {
            "ok": True,
            "cursor": "20260729",
            "has_more": False,
            "failed": 0,
        },
    )
    monkeypatch.setattr(
        backfill,
        "enqueue_plan_model_data_backfill",
        lambda **kwargs: {"ok": True, "message_id": "event-msg", **kwargs},
    )
    monkeypatch.setattr(
        backfill,
        "audit_plan_model_data_contract",
        lambda: audit,
    )
    monkeypatch.setattr(
        backfill,
        "_set_model_data_backfill_state",
        lambda **kwargs: states.append(kwargs) or kwargs,
    )

    result = backfill._process_plan_model_data_backfill(
        {"phase": "major_news", "cursor": "20260728"}
    )

    assert result["post_backfill_audit"] == {}
    assert states[-1]["status"] == "running"
    assert states[-1]["phase"] == "event_research"


def test_event_research_completion_enqueues_financial_disclosures(monkeypatch):
    states = []
    monkeypatch.setattr(backfill, "normalize_report_rc_record_keys", lambda: {"changed": 3})
    monkeypatch.setattr(
        backfill,
        "backfill_event_research_history",
        lambda **_kwargs: {"ok": True, "cursor": "20260731", "has_more": False},
    )
    monkeypatch.setattr(
        backfill,
        "enqueue_plan_model_data_backfill",
        lambda **kwargs: {"ok": True, "message_id": "financial-msg", **kwargs},
    )
    monkeypatch.setattr(
        backfill,
        "_set_model_data_backfill_state",
        lambda **kwargs: states.append(kwargs) or kwargs,
    )

    result = backfill._process_plan_model_data_backfill(
        {"phase": "event_research", "cursor": "20181231"}
    )

    assert result["post_backfill_audit"] == {}
    assert states[-1]["status"] == "running"
    assert states[-1]["phase"] == "event_financial_disclosures"


def test_event_financial_completion_runs_model_data_contract_audit(monkeypatch):
    states = []
    audit = {"schema_version": "alis_plan_model_data_contract_audit_v1", "status": "completed"}
    monkeypatch.setattr(backfill, "_event_financial_periods", lambda: ["20190331"])
    monkeypatch.setattr(backfill, "_event_financial_period_complete", lambda *_args: False)
    monkeypatch.setattr(
        backfill,
        "sync_tushare_stock_batch",
        lambda api_name, **_kwargs: {"ok": True, "api": api_name, "rows_written": 5},
    )
    monkeypatch.setattr(backfill, "audit_plan_model_data_contract", lambda: audit)
    monkeypatch.setattr(
        backfill,
        "_set_model_data_backfill_state",
        lambda **kwargs: states.append(kwargs) or kwargs,
    )

    result = backfill._process_plan_model_data_backfill(
        {"phase": "event_financial_disclosures", "cursor": "20181231"}
    )

    assert result["post_backfill_audit"] == audit
    assert states[-1]["status"] == "completed"
    assert states[-1]["post_backfill_audit"] == audit


def test_ensure_plan_model_data_backfill_deduplicates_active_work_order(monkeypatch):
    monkeypatch.setattr(
        backfill,
        "get_plan_model_data_backfill_status",
        lambda **_kwargs: {"status": "running", "work_order_id": "existing"},
    )
    result = backfill.ensure_plan_model_data_backfill()
    assert result["created"] is False
    assert result["reason"] == "already_active"
