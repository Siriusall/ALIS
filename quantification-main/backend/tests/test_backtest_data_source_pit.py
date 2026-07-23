from app.services.backtest_data_source import BacktestDataSource


def test_point_in_time_audit_accumulates_repeated_source_batches():
    source = BacktestDataSource()
    source.set_as_of_time("2026-07-22T15:00:00")

    source._record_pit_read(  # noqa: SLF001 - audit aggregation contract
        "snapshot.history",
        [
            {"available_time": "2026-07-21T15:00:00"},
            {"available_time": "2026-07-22T15:00:00"},
        ],
    )
    source._record_pit_read(  # noqa: SLF001 - audit aggregation contract
        "snapshot.history",
        [{"available_time": "2026-07-20T15:00:00"}],
    )

    audit = source.point_in_time_sources()[0]
    assert audit["read_count"] == 3
    assert audit["batch_count"] == 2
    assert audit["max_available_time"] == "2026-07-22T15:00:00"
    assert audit["passed"] is True
