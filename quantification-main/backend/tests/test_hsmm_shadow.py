import numpy as np

from app.services import hsmm_shadow
from app.services.hsmm_shadow import _decode, _fit


def test_hsmm_uses_four_states_student_t_and_bounded_negative_binomial_duration():
    rng = np.random.default_rng(7)
    values = np.vstack([
        rng.normal([1, 1, -1, 1, 1, -1, 1, 1, -1], .2, size=(20, 9)),
        rng.normal([0, 0, 0, 0, 0, 0, 0, 0, 0], .2, size=(20, 9)),
        rng.normal([-1, -1, 1, -1, -1, 1, -1, -1, 1], .2, size=(20, 9)),
        rng.normal([.5, 0, 1, .5, 0, 1, .5, 0, 1], .2, size=(20, 9)),
    ])
    model = _fit(values)
    decoded, likelihood = _decode(values, model)
    assert len(model["emissions"]) == 4
    assert all(item["df"] == 6 for item in model["emissions"])
    assert all(item["r"] > 0 and 0 < item["p"] < 1 for item in model["durations"])
    assert len(decoded) == len(values)
    assert np.isfinite(likelihood)


def test_training_refresh_calls_only_three_compact_index_apis(monkeypatch):
    calls = []
    monkeypatch.setattr("app.data_platform.services.data_center.sync_tushare_api_raw", lambda api, params: calls.append((api, params)) or {"rows_written": 10})
    monkeypatch.setattr("app.services.market_context.materialize_market_training_history", lambda: {"written": 20})
    result = hsmm_shadow.refresh_compact_training_history_from_api(years=10)
    assert len(calls) == 3
    assert {api for api, _ in calls} == {"index_daily"}
    assert {params["ts_code"] for _, params in calls} == {"000300.SH", "000905.SH", "000852.SH"}
    assert result["materialized"]["written"] == 20
