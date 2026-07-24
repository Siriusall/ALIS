from contextlib import contextmanager
from pathlib import Path

from app.data_platform.services import cold_archive


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _ArchiveDb:
    def __init__(self):
        self.inserted = []

    def execute(self, sql, params=()):
        if "SELECT COUNT" in sql:
            return _Cursor([{"n": 2}])
        if "FROM dc_tushare_raw_records" in sql:
            return _Cursor([
                {"id": 1, "api_name": "index_daily", "record_key": "a", "ts_code": "000300.SH", "trade_date": "20240102", "period": "", "ann_date": "", "payload_json": "{}", "source_code": "tushare", "available_time": "2024-01-02T16:00:00", "updated_at": "2024-01-02T16:00:00"},
                {"id": 2, "api_name": "index_daily", "record_key": "b", "ts_code": "000300.SH", "trade_date": "20240103", "period": "", "ann_date": "", "payload_json": "{}", "source_code": "tushare", "available_time": "2024-01-03T16:00:00", "updated_at": "2024-01-03T16:00:00"},
            ])
        if "INSERT INTO dc_cold_archive_manifest" in sql:
            self.inserted.append(params)
            return _Cursor([])
        raise AssertionError(sql)


def test_raw_archive_writes_zstd_parquet_and_manifest(monkeypatch, tmp_path: Path):
    db = _ArchiveDb()

    @contextmanager
    def connection():
        yield db

    monkeypatch.setattr(cold_archive, "_conn", connection)
    monkeypatch.setattr(cold_archive, "_archive_root", lambda: tmp_path)

    result = cold_archive.archive_raw_records(
        api_names=["index_daily"], start_date="20240102", end_date="20240103", max_rows=10,
    )

    path = Path(result["file_path"])
    assert path.exists()
    assert result["row_count"] == 2
    assert result["delete_online_rows"] is False
    assert len(db.inserted) == 1
    assert db.inserted[0][5] == 2
