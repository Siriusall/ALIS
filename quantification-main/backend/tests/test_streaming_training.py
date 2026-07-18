from app.services import streaming_training
from app.services import model_training
from app.services.streaming_training import build_training_slices, temporary_dates_for_cleanup


def test_training_slices_keep_feature_and_label_overlap() -> None:
    dates = [f"2024{month:02d}{day:02d}" for month in range(1, 7) for day in range(1, 21)]
    slices = build_training_slices(dates, chunk_sessions=60, lookback_sessions=80, label_horizon=20)
    assert len(slices) == 2
    assert len(slices[0].core_dates) == 60
    assert len(slices[0].fetch_dates) == 80
    assert slices[1].fetch_dates[0] == dates[0]
    assert slices[1].fetch_dates[-1] == dates[-1]


def test_cleanup_never_targets_online_window() -> None:
    assert temporary_dates_for_cleanup(
        fetched_dates=["20231229", "20240102", "20240103"], online_window_start="20240102",
    ) == ["20231229"]


def test_slice_fetches_then_consumes_then_always_cleans(monkeypatch) -> None:
    calls = []
    def fetch(trade_date):
        calls.append(("vip_fetch", trade_date))
        return {"ok": True, "mode": "tushare_vip_trade_date_batch"}

    def snapshot(date):
        calls.append(("snapshot", date))
        return {"ok": True}

    monkeypatch.setattr("app.data_platform.services.data_center.sync_training_market_date_vip", fetch)
    monkeypatch.setattr("app.data_platform.services.data_center.training_market_date_ready", lambda *_: False)
    monkeypatch.setattr("app.data_platform.services.data_center.build_stock_daily_snapshot", snapshot)
    monkeypatch.setattr("app.data_platform.services.data_center.purge_temporary_training_market_data", lambda **kwargs: calls.append(("clean", tuple(kwargs["fetched_dates"]))))
    part = streaming_training.TrainingSlice(core_dates=("20240102",), fetch_dates=("20240101", "20240102"))

    result = streaming_training.consume_api_training_slice(part, online_window_start="20250101", consume=lambda core: calls.append(("consume", core)) or "ok")

    assert result == "ok"
    assert calls[-1] == ("clean", ("20240101", "20240102"))
    assert calls[-2] == ("consume", ("20240102",))


def test_failed_api_slice_is_cleaned(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("app.data_platform.services.data_center.sync_training_market_date_vip", lambda *_: {"ok": False})
    monkeypatch.setattr("app.data_platform.services.data_center.training_market_date_ready", lambda *_: False)
    monkeypatch.setattr("app.data_platform.services.data_center.build_stock_daily_snapshot", lambda *_: {"ok": True})
    monkeypatch.setattr("app.data_platform.services.data_center.purge_temporary_training_market_data", lambda **kwargs: calls.append(kwargs["fetched_dates"]))
    part = streaming_training.TrainingSlice(core_dates=("20240102",), fetch_dates=("20240101", "20240102"))

    try:
        streaming_training.consume_api_training_slice(part, online_window_start="20250101", consume=lambda _: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed upstream fetch must stop the slice")
    assert calls == [["20240101", "20240102"]]


def test_historical_builder_keeps_only_core_samples_and_uses_online_label_bridge(monkeypatch) -> None:
    calls = []

    def fake_consume(part, *, online_window_start, consume, retain_history=False):
        calls.append((part, online_window_start))
        assert retain_history is True
        return consume(part.core_dates)

    def fake_dataset(**kwargs):
        dates = kwargs["dates"]
        # One row from the first date and one from the bridge date: only the
        # historical core row may enter the training result.
        return [[1.0], [2.0]], [0.1, 0.2], [
            {"date": dates[0], "symbol": "000001", "future_date": dates[-1]},
            {"date": dates[-1], "symbol": "000001", "future_date": dates[-1]},
        ]

    monkeypatch.setattr(model_training, "consume_api_training_slice", fake_consume)
    monkeypatch.setattr(model_training, "_build_dataset", fake_dataset)
    out = model_training._stream_historical_datasets(
        historical_dates=["20231228", "20231229"], label_bridge_dates=["20240102", "20240103"],
        online_window_start="20240102", horizons=[5], exchange_group="ALL", progress=lambda *_: None,
    )

    assert calls[0][0].fetch_dates[-1] == "20240103"
    assert out[5][1] == [0.1]
    assert out[5][2][0]["date"] == "2023-12-28"


def test_retained_training_slice_does_not_cleanup(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("app.data_platform.services.data_center.training_market_date_ready", lambda *_: True)
    monkeypatch.setattr("app.data_platform.services.data_center.purge_temporary_training_market_data", lambda **kwargs: calls.append("clean"))
    part = streaming_training.TrainingSlice(core_dates=("20240102",), fetch_dates=("20240102",))

    result = streaming_training.consume_api_training_slice(
        part, online_window_start="20250101", consume=lambda _: "ok", retain_history=True,
    )

    assert result == "ok"
    assert calls == []
