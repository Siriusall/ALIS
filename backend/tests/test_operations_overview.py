from app.services import operations_overview


def test_live_portfolio_risk_separates_equity_like_etfs_from_true_defense(monkeypatch):
    monkeypatch.setattr(operations_overview, "get_positions", lambda: [
        {"symbol": "600000", "name": "浦发银行", "quantity": 100, "price": 10},
        {"symbol": "510880", "name": "红利ETF", "quantity": 100, "price": 10},
        {"symbol": "511010", "name": "国债ETF", "quantity": 100, "price": 10},
    ])
    monkeypatch.setattr(operations_overview, "get_account_balance", lambda: 1000.0)
    monkeypatch.setattr(operations_overview, "get_account_baseline", lambda: 4000.0)
    monkeypatch.setattr(operations_overview, "get_account_high_water_mark", lambda: 4000.0)
    monkeypatch.setattr(
        operations_overview,
        "point_in_time_position_market_series",
        lambda **_: {
            "price": 10.0,
            "returns": [0.01, -0.01],
            "price_source": "stock_daily_point_in_time_close",
            "trade_date": "20260812",
        },
    )
    monkeypatch.setattr(operations_overview, "list_theses_for_symbols", lambda _: [])
    monkeypatch.setattr(operations_overview, "list_position_states", lambda: [])
    monkeypatch.setattr(operations_overview, "constrained_cvar_weights", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(operations_overview, "hrp_fallback_weights", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(operations_overview, "historical_cvar", lambda _: {})
    monkeypatch.setattr(operations_overview, "ledoit_wolf_covariance", lambda _: {})

    result = operations_overview.build_live_portfolio_risk(as_of="20260812")

    summary = result["underlying_risk_exposure"]
    assert summary["stock_exposure"] == 0.25
    assert summary["equity_like_etf_exposure"] == 0.25
    assert summary["true_defensive_exposure"] == 0.25
    assert summary["cash_exposure"] == 0.25
    assert summary["equity_exposure"] == 0.5
from app.services.review_learning import _outcome_needs_benchmark_recompute


def test_repaired_benchmark_outcome_is_refreshable_once():
    assert _outcome_needs_benchmark_recompute({
        "benchmark_return": None,
        "attribution_json": '{"limitations":["缺少沪深300同期收盘价"]}',
    }) is True
    assert _outcome_needs_benchmark_recompute({
        "benchmark_return": 0.01,
        "attribution_json": '{"limitations":["缺少沪深300同期收盘价"]}',
    }) is False


def test_unrelated_incomplete_outcome_is_not_overwritten():
    assert _outcome_needs_benchmark_recompute({
        "benchmark_return": None,
        "attribution_json": '{"limitations":["缺少行业同期横截面"]}',
    }) is False


def test_normal_autonomous_waiting_task_is_not_an_exception():
    assert operations_overview._is_actionable_paused_task({
        "status": "paused",
        "replay_mode": "autonomous_daily",
        "last_message": "自动任务参数已与当前 Champion 同步",
        "error": None,
    }) is False


def test_paused_task_with_error_remains_an_exception():
    assert operations_overview._is_actionable_paused_task({
        "status": "paused",
        "replay_mode": "autonomous_daily",
        "last_message": "等待下一份正式计划",
        "error": "数据门失败",
    }) is True


def test_governance_overview_degrades_one_failed_domain(monkeypatch):
    monkeypatch.setattr(operations_overview, "build_data_health_report", lambda **_: {"ok": True, "issues": []})
    monkeypatch.setattr(operations_overview, "_cached_readiness_overview", lambda: (_ for _ in ()).throw(RuntimeError("redis unavailable")))
    monkeypatch.setattr(operations_overview, "get_observability", lambda **_: {"logs": []})
    monkeypatch.setattr(operations_overview, "get_model_status", lambda: {"active_version": {"version": "v1"}, "latest_train_run": None})
    monkeypatch.setattr(operations_overview, "capability_registry", lambda: [{"status": "ready"}])
    monkeypatch.setattr(operations_overview, "list_quant_run_tasks", lambda **_: [])
    monkeypatch.setattr(operations_overview, "list_model_versions", lambda **_: [])

    result = operations_overview.build_governance_overview()

    assert result["ok"] is False
    assert result["errors"] == [{"domain": "data_readiness", "error": "redis unavailable"}]
    readiness_gate = next(item for item in result["gates"] if item["key"] == "data_readiness")
    assert readiness_gate["status"] == "unknown"
