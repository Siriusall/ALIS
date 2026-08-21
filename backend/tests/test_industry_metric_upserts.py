from __future__ import annotations

import pytest

from app.data_platform.services import data_center as service


class _RawConnection:
    def __init__(self, *, cursor_mode: bool, fail_on_call: int | None = None):
        self.cursor_mode = cursor_mode
        self.fail_on_call = fail_on_call
        self.calls: list[tuple[str, int]] = []
        self.commits = 0
        self.rollbacks = 0

    def _executemany(self, sql, rows):  # noqa: ANN001
        call_number = len(self.calls) + 1
        if self.fail_on_call == call_number:
            raise RuntimeError("batch write failed")
        self.calls.append((str(sql), len(rows)))

    def executemany(self, sql, rows):  # noqa: ANN001
        assert not self.cursor_mode
        self._executemany(sql, rows)

    def cursor(self):
        assert self.cursor_mode
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Cursor:
    def __init__(self, raw: _RawConnection):
        self.raw = raw

    def executemany(self, sql, rows):  # noqa: ANN001
        self.raw._executemany(sql, rows)

    def close(self):
        return None


class _Connection:
    def __init__(self, raw: _RawConnection, *, dialect: str):
        self.raw = raw
        self._is_postgres = dialect == "postgres"
        self._is_mysql = dialect == "mysql"

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()


class _ConnectionContext:
    def __init__(self, connection: _Connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


def _definition_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "metric_code": f"metric-{index}",
            "industry_code": "industry",
            "industry_name": "行业",
            "metric_name": "指标",
            "unit": "index",
            "frequency": "monthly",
        }
        for index in range(count)
    ]


def _value_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "metric_code": f"metric-{index}",
            "data_date": f"2026-{(index % 12) + 1:02d}-01",
            "value": float(index),
        }
        for index in range(count)
    ]


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite"])
@pytest.mark.parametrize(
    ("upsert", "rows_factory"),
    [
        (service._upsert_industry_metric_definitions, _definition_rows),
        (service._upsert_industry_metric_values, _value_rows),
    ],
)
def test_industry_metric_upserts_use_250_row_batches_and_commit_each(
    monkeypatch, dialect, upsert, rows_factory,
):
    raw = _RawConnection(cursor_mode=dialect != "sqlite")
    connection = _Connection(raw, dialect=dialect)
    monkeypatch.setattr(service, "_conn", lambda: _ConnectionContext(connection))

    assert upsert(rows_factory(251)) == 251
    assert [size for _sql, size in raw.calls] == [250, 1]
    assert raw.commits == 2
    assert raw.rollbacks == 0
    if dialect == "sqlite":
        assert "?" in raw.calls[0][0]
    else:
        assert "%s" in raw.calls[0][0]


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite"])
@pytest.mark.parametrize(
    ("upsert", "rows_factory"),
    [
        (service._upsert_industry_metric_definitions, _definition_rows),
        (service._upsert_industry_metric_values, _value_rows),
    ],
)
def test_industry_metric_upserts_roll_back_only_the_failed_batch(
    monkeypatch, dialect, upsert, rows_factory,
):
    raw = _RawConnection(cursor_mode=dialect != "sqlite", fail_on_call=2)
    connection = _Connection(raw, dialect=dialect)
    monkeypatch.setattr(service, "_conn", lambda: _ConnectionContext(connection))

    with pytest.raises(RuntimeError, match="batch write failed"):
        upsert(rows_factory(251))

    assert [size for _sql, size in raw.calls] == [250]
    assert raw.commits == 1
    assert raw.rollbacks == 1
