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
    counts = {"daily_n": 996, "valuation_n": 0, "limit_n": 990}

    assert _execution_slice_ready(stock_count, counts)
    assert not _market_slice_ready(stock_count, counts)


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
