import json

from app.services import theme_engine


class _Rows(list):
    def fetchall(self):
        return self


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, _params=()):
        if "dc_theme_snapshot" in sql:
            return _Rows(self.rows)
        raise AssertionError(sql)


def _payload(source_cutoff="2026-08-14T15:00:00+08:00"):
    return json.dumps({
        "point_in_time": {
            "contract": theme_engine._THEME_PIT_CONTRACT,
            "source_observation_cutoff": source_cutoff,
        },
        "knowledge_graph": {"industry": "电子"},
    })


def _row(
    *,
    created_at="2026-08-14T14:59:00+08:00",
    theme_code="industry:电子",
    as_of_date="20260814",
):
    return {
        "as_of_date": as_of_date,
        "theme_code": theme_code,
        "theme_name": "电子景气",
        "promotable": 1,
        "score": 80.0,
        "payload_json": _payload(),
        "available_time": "2026-08-14T15:00:00+08:00",
        "created_at": created_at,
        "updated_at": "2026-08-14T14:59:00+08:00",
    }


def test_theme_snapshot_requires_all_materialized_clocks_before_cutoff():
    cutoff = "2026-08-14T15:00:00+08:00"
    assert theme_engine._theme_snapshot_visible_at(
        json.loads(_payload()), cutoff, row=_row(),
    )
    assert not theme_engine._theme_snapshot_visible_at(
        json.loads(_payload()), cutoff,
        row=_row(created_at="2026-08-14T15:01:00+08:00"),
    )


def test_theme_snapshot_rejects_source_observed_after_cutoff():
    payload = json.loads(_payload("2026-08-14T15:01:00+08:00"))
    assert not theme_engine._theme_snapshot_visible_at(
        payload,
        "2026-08-14T15:00:00+08:00",
        row=_row(),
    )


def test_list_themes_filters_late_rows_before_applying_limit(monkeypatch):
    conn = _Connection([
        _row(created_at="2026-08-14T15:01:00+08:00", theme_code="industry:visible"),
        _row(theme_code="industry:visible", as_of_date="20260813"),
    ])
    monkeypatch.setattr(theme_engine, "_read_conn", lambda: conn)

    result = theme_engine.list_themes(
        as_of_time="2026-08-14T15:00:00+08:00",
        limit=1,
        include_companies=False,
        compact=True,
        include_company_counts=False,
    )

    assert result["count"] == 1
    assert result["total_count"] == 1
    assert result["promotable_count"] == 1
    assert result["items"][0]["theme_code"] == "industry:visible"


def test_list_themes_reports_missing_visible_snapshot_as_blocked(monkeypatch):
    conn = _Connection([_row(created_at="2026-08-14T15:01:00+08:00")])
    monkeypatch.setattr(theme_engine, "_read_conn", lambda: conn)

    result = theme_engine.list_themes(
        as_of_time="2026-08-14T15:00:00+08:00",
        include_companies=False,
        compact=True,
        include_company_counts=False,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "no_theme_snapshot_visible_at_cutoff"


def test_get_theme_exhausts_mapping_pages_without_truncation(monkeypatch):
    snapshot = _row()
    mappings = [
        {
            "ts_code": f"{index:06d}.SZ",
            "exposure_score": 1.0,
            "evidence_json": "{}",
            "updated_at": "2026-08-14T14:59:00+08:00",
        }
        for index in range(theme_engine._THEME_MAPPING_BATCH_SIZE + 5)
    ]

    class PagedConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            if "FROM dc_theme_snapshot" in sql:
                return _Rows([snapshot])
            if "FROM dc_theme_company_mapping" in sql:
                last_code = str(params[-2]) if "ts_code>?" in sql else ""
                page = [item for item in mappings if item["ts_code"] > last_code]
                return _Rows(page[:theme_engine._THEME_MAPPING_BATCH_SIZE])
            raise AssertionError(sql)

    monkeypatch.setattr(theme_engine, "_read_conn", lambda: PagedConnection())

    result = theme_engine.get_theme(
        "industry:电子",
        as_of_date="2026-08-14T15:00:00+08:00",
    )

    assert result is not None
    assert result["company_count"] == len(mappings)
    assert len(result["companies"]) == len(mappings)


def test_theme_mapping_requires_its_own_write_clock_at_formal_cutoff():
    cutoff = "2026-08-14T15:00:00+08:00"
    assert theme_engine._theme_mapping_visible_at(
        {"updated_at": "2026-08-14T14:59:00+08:00"}, cutoff,
    )
    assert not theme_engine._theme_mapping_visible_at(
        {"updated_at": "2026-08-14T15:01:00+08:00"}, cutoff,
    )
    assert not theme_engine._theme_mapping_visible_at({}, cutoff)


class _BestThemeConnection:
    def __init__(self, snapshot_row, mapping_updated_at):
        self.snapshot_row = snapshot_row
        self.mapping_updated_at = mapping_updated_at

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, _params=()):
        if "dc_theme_company_mapping" in sql:
            row = dict(self.snapshot_row)
            row.update({
                "exposure_score": 1.0,
                "evidence_json": "{}",
                "mapping_updated_at": self.mapping_updated_at,
            })
            return _Rows([row])
        raise AssertionError(sql)


def test_best_theme_rejects_mapping_updated_after_cutoff(monkeypatch):
    snapshot = _row()
    late = _BestThemeConnection(snapshot, "2026-08-14T15:01:00+08:00")
    monkeypatch.setattr(theme_engine, "_conn", lambda: late)
    assert theme_engine.best_theme_for_stock(
        "000001.SZ", as_of_time="2026-08-14T15:00:00+08:00",
    ) is None

    early = _BestThemeConnection(snapshot, "2026-08-14T14:59:00+08:00")
    monkeypatch.setattr(theme_engine, "_conn", lambda: early)
    result = theme_engine.best_theme_for_stock(
        "000001.SZ", as_of_time="2026-08-14T15:00:00+08:00",
    )
    assert result is not None
