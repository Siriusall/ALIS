from app.services import quant_task_runner
from app.services.quant_task_runner import _execution_slice_ready, _market_slice_ready


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
