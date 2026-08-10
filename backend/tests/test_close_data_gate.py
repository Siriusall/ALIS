from app.services import quant_task_runner
from app.services.quant_task_runner import _execution_slice_ready, _market_slice_ready
from datetime import datetime


def test_close_data_gate_requires_all_target_day_market_fields() -> None:
    stock_count = 1_000
    assert _market_slice_ready(stock_count, {"daily_n": 996, "valuation_n": 950, "limit_n": 990})
    assert not _market_slice_ready(stock_count, {"daily_n": 996, "valuation_n": 0, "limit_n": 990})
    assert not _market_slice_ready(stock_count, {"daily_n": 996, "valuation_n": 950, "limit_n": 989})


def test_close_data_gate_does_not_treat_an_empty_slice_as_ready() -> None:
    assert not _market_slice_ready(5_534, {"daily_n": 0, "valuation_n": 0, "limit_n": 0})


def test_t1_execution_slice_does_not_require_new_decision_valuation_data() -> None:
    stock_count = 1_000
    counts = {"daily_n": 996, "execution_price_n": 996, "valuation_n": 0, "limit_n": 990}

    assert _execution_slice_ready(stock_count, counts)
    assert not _market_slice_ready(stock_count, counts)


def test_frozen_t1_worker_uses_execution_gate_without_daily_basic(monkeypatch) -> None:
    monkeypatch.setattr(
        quant_task_runner,
        "_snapshot_data_status",
        lambda trade_date="": (
            "20260731",
            {
                "stock_count": 1_000,
                "daily_n": 996,
                "execution_price_n": 996,
                "valuation_n": 0,
                "limit_n": 990,
            },
        ),
    )

    decision = quant_task_runner._evaluate_frozen_execution_data_gate("2026-07-31")

    assert decision.should_pause is False
    assert quant_task_runner._requires_replay_opinion_gates("autonomous_daily") is False
    assert quant_task_runner._requires_replay_opinion_gates("pragmatic_recover") is True


def test_frozen_t1_worker_blocks_incomplete_limit_slice(monkeypatch) -> None:
    monkeypatch.setattr(
        quant_task_runner,
        "_snapshot_data_status",
        lambda trade_date="": (
            "20260731",
            {
                "stock_count": 1_000,
                "daily_n": 996,
                "execution_price_n": 996,
                "valuation_n": 950,
                "limit_n": 989,
            },
        ),
    )

    decision = quant_task_runner._evaluate_frozen_execution_data_gate("20260731")

    assert decision.should_pause is True
    assert decision.code == "paused_frozen_execution_data_not_ready"
    assert decision.evidence["daily_basic_required"] is False


def test_frozen_t1_worker_requires_open_and_previous_close(monkeypatch) -> None:
    monkeypatch.setattr(
        quant_task_runner,
        "_snapshot_data_status",
        lambda trade_date="": (
            "20260731",
            {
                "stock_count": 1_000,
                "daily_n": 996,
                "execution_price_n": 994,
                "valuation_n": 950,
                "limit_n": 990,
            },
        ),
    )

    decision = quant_task_runner._evaluate_frozen_execution_data_gate("20260731")

    assert decision.should_pause is True
    assert decision.evidence["required_fields"] == [
        "daily.open", "daily.pre_close", "stk_limit",
    ]


def test_frozen_t1_worker_blocks_stale_defensive_asset_slice(monkeypatch) -> None:
    monkeypatch.setattr(
        quant_task_runner,
        "_snapshot_data_status",
        lambda trade_date="": (
            "20260731",
            {
                "stock_count": 1_000,
                "daily_n": 996,
                "execution_price_n": 996,
                "valuation_n": 0,
                "limit_n": 990,
            },
        ),
    )
    monkeypatch.setattr(
        "app.services.defensive_assets.defensive_asset_execution_status",
        lambda trade_date, ts_codes: {
            "ok": False,
            "trade_date": "20260731",
            "required": list(ts_codes),
            "ready": [],
            "missing": ["510880.SH"],
            "items": [],
        },
    )

    decision = quant_task_runner._evaluate_frozen_execution_data_gate(
        "20260731",
        ["510880.SH"],
    )

    assert decision.should_pause is True
    assert decision.evidence["schema_version"] == "alis_frozen_t1_execution_data_gate_v2"
    assert decision.evidence["defensive_assets"]["missing"] == ["510880.SH"]


def test_frozen_t1_worker_accepts_complete_defensive_asset_slice(monkeypatch) -> None:
    monkeypatch.setattr(
        quant_task_runner,
        "_snapshot_data_status",
        lambda trade_date="": (
            "20260731",
            {
                "stock_count": 1_000,
                "daily_n": 996,
                "execution_price_n": 996,
                "valuation_n": 0,
                "limit_n": 990,
            },
        ),
    )
    monkeypatch.setattr(
        "app.services.defensive_assets.defensive_asset_execution_status",
        lambda trade_date, ts_codes: {
            "ok": True,
            "trade_date": "20260731",
            "required": list(ts_codes),
            "ready": list(ts_codes),
            "missing": [],
            "items": [],
        },
    )

    decision = quant_task_runner._evaluate_frozen_execution_data_gate(
        "20260731",
        ["510880.SH", "511010.SH", "511880.SH", "512890.SH", "518880.SH"],
    )

    assert decision.should_pause is False


def test_frozen_t1_refresh_only_requests_missing_defensive_assets(monkeypatch) -> None:
    statuses = iter([
        {"ok": False, "missing": ["510880.SH"], "required": ["510880.SH", "511010.SH"]},
        {"ok": True, "missing": [], "required": ["510880.SH", "511010.SH"]},
    ])
    captured = {}
    quant_task_runner._defensive_execution_refresh_attempts.clear()
    monkeypatch.setattr(
        "app.services.defensive_assets.defensive_asset_execution_status",
        lambda *_args, **_kwargs: next(statuses),
    )
    monkeypatch.setattr(
        "app.services.defensive_assets.materialize_defensive_asset_history",
        lambda **kwargs: captured.update(kwargs) or {"ok": True},
    )

    result = quant_task_runner._refresh_missing_frozen_defensive_assets(
        "20260731",
        ["510880.SH", "511010.SH"],
    )

    assert result["ok"] is True
    assert result["refresh"] == "attempted"
    assert captured == {
        "start_date": "20260731",
        "end_date": "20260731",
        "ts_codes": ["510880.SH"],
    }
    assert result["fallback_result"] is None


def test_frozen_t1_refresh_uses_verified_fallback_after_primary_stays_partial(monkeypatch) -> None:
    statuses = iter([
        {"ok": False, "missing": ["510880.SH"], "required": ["510880.SH", "511010.SH"]},
        {"ok": False, "missing": ["510880.SH"], "required": ["510880.SH", "511010.SH"]},
        {"ok": True, "missing": [], "required": ["510880.SH", "511010.SH"]},
    ])
    captured = {}
    quant_task_runner._defensive_execution_refresh_attempts.clear()
    monkeypatch.setattr(
        "app.services.defensive_assets.defensive_asset_execution_status",
        lambda *_args, **_kwargs: next(statuses),
    )
    monkeypatch.setattr(
        "app.services.defensive_assets.materialize_defensive_asset_history",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "app.data_platform.services.tencent_etf_daily_fallback.repair_fund_daily_from_tencent",
        lambda trade_date, ts_codes: captured.update({"trade_date": trade_date, "ts_codes": ts_codes})
        or {"ok": True, "rows_written": 1},
    )

    result = quant_task_runner._refresh_missing_frozen_defensive_assets(
        "20260731", ["510880.SH", "511010.SH"],
    )

    assert result["ok"] is True
    assert result["fallback_result"]["rows_written"] == 1
    assert captured == {
        "trade_date": "20260731",
        "ts_codes": ["510880.SH", "511010.SH"],
    }


def test_scheduler_refreshes_missing_decision_data_without_blocking_due_t1_plan(
    monkeypatch,
) -> None:
    started = []
    monkeypatch.setattr(
        quant_task_runner,
        "_close_data_status",
        lambda: (
            "20260727",
            False,
            {
                "stock_count": 1_000,
                "daily_n": 996,
                "valuation_n": 0,
                "limit_n": 990,
            },
        ),
    )
    monkeypatch.setattr(
        "app.data_platform.services.daily_update.start_daily_update",
        lambda: started.append(True) or {"ok": True},
    )

    target, execution_ready = quant_task_runner._ensure_close_data_ready()

    assert target == "20260727"
    assert execution_ready is True
    assert started == [True]


def test_weekend_wall_clock_closes_prior_day_plan_without_waiting_for_new_market_date(
    monkeypatch,
) -> None:
    captured = []
    monkeypatch.setattr(
        "app.services.profit_loop.mark_missed_daily_rebalance_execution_inputs",
        lambda cutoff: captured.append(cutoff) or 1,
    )

    changed = quant_task_runner._reconcile_missed_plans_by_wall_clock(
        datetime(2026, 8, 1, 9, 0),
    )

    assert changed == 1
    assert captured == ["20260801"]
