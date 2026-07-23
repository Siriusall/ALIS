from app.services.execution_cost_calibration import _calibration_metrics


def test_execution_cost_calibration_uses_side_specific_conservative_percentile():
    rows = []
    for index in range(30):
        rows.append({
            "side": "BUY" if index < 15 else "SELL",
            "price": 10,
            "quantity": 100,
            "commission": 1 if index < 15 else 2,
            "transfer_fee": 0,
            "stamp_tax": 0 if index < 15 else 1,
            "slippage_bps": 5 if index % 3 else 8,
        })
    metrics = _calibration_metrics(rows)
    assert metrics["operation_count"] == 30
    assert metrics["buy_rate"] >= 0.0015
    assert metrics["sell_rate"] > metrics["buy_rate"]
    assert metrics["round_trip_rate"] == round(
        metrics["buy_rate"] + metrics["sell_rate"], 8
    )
