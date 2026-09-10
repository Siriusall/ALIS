"""Local, explicit Parquet cold archive for large immutable raw records.

Archives are additive: online PostgreSQL remains the source of truth and no
row is deleted by this service.  This makes the first local deployment safe,
while the manifest provides exactly the information needed to copy the same
files to an object store during the later cloud migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.data_platform.db import _conn
from app.core.runtime_paths import backend_cache_root


def _archive_root() -> Path:
    configured = os.getenv("ALIS_COLD_ARCHIVE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return backend_cache_root() / "cold-archive"


def _normalise_date(value: str, name: str) -> str:
    compact = str(value or "").replace("-", "").strip()
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(f"{name} must be YYYYMMDD")
    return compact


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cold_archive_status(*, limit: int = 30) -> dict[str, Any]:
    root = _archive_root()
    usage = shutil.disk_usage(root if root.exists() else root.parent)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT archive_id,dataset,api_names_json,date_start,date_end,row_count,file_path,
                      file_sha256,schema_json,status,created_at
                 FROM dc_cold_archive_manifest ORDER BY created_at DESC LIMIT ?""",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["api_names"] = json.loads(item.pop("api_names_json") or "[]")
        item["schema"] = json.loads(item.pop("schema_json") or "{}")
        file_path = Path(str(item["file_path"]))
        item["file_exists"] = file_path.exists()
        item["file_bytes"] = file_path.stat().st_size if item["file_exists"] else 0
        items.append(item)
    return {
        "ok": True,
        "storage": "local_filesystem",
        "root": str(root),
        "exists": root.exists(),
        "disk": {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "free_pct": round(usage.free / usage.total, 6) if usage.total else 0.0,
        },
        "items": items,
        "contract": "append_only_parquet_zstd_with_manifest",
    }


def archive_raw_records(
    *, api_names: list[str], start_date: str, end_date: str, max_rows: int = 250_000,
) -> dict[str, Any]:
    """Write an explicit date slice of raw records to ZSTD Parquet.

    A bounded row limit is a safety rail for local machines.  Callers must
    narrow the date/API slice rather than accidentally materialise the entire
    multi-million-row raw table a second time.
    """
    apis = sorted({str(value).strip() for value in api_names if str(value).strip()})
    if not apis:
        raise ValueError("api_names must contain at least one API")
    start, end = _normalise_date(start_date, "start_date"), _normalise_date(end_date, "end_date")
    if start > end:
        raise ValueError("start_date must not be after end_date")
    cap = max(1, min(int(max_rows), 1_000_000))
    placeholders = ",".join("?" for _ in apis)
    where = f"api_name IN ({placeholders}) AND trade_date>=? AND trade_date<=?"
    params: tuple[Any, ...] = (*apis, start, end)
    with _conn() as conn:
        count_row = conn.execute(f"SELECT COUNT(*) AS n FROM dc_tushare_raw_records WHERE {where}", params).fetchone()
        row_count = int(dict(count_row).get("n") or 0)
        if row_count == 0:
            raise ValueError("指定范围没有可归档的原始记录")
        if row_count > cap:
            raise ValueError(f"归档范围有 {row_count} 行，超过本机单次安全上限 {cap}；请缩小日期或 API 范围")
        rows = conn.execute(
            f"""SELECT id,api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
                       source_code,available_time,updated_at
                  FROM dc_tushare_raw_records WHERE {where}
                 ORDER BY api_name,trade_date,id""",
            params,
        ).fetchall()

    frame = pd.DataFrame([dict(row) for row in rows])
    archive_id = f"raw-{start}-{end}-{uuid.uuid4().hex[:12]}"
    target_dir = _archive_root() / "raw" / f"year={start[:4]}" / f"month={start[4:6]}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{archive_id}.parquet"
    temporary = target.with_suffix(".parquet.tmp")
    frame.to_parquet(temporary, engine="pyarrow", compression="zstd", index=False)
    temporary.replace(target)
    created_at = datetime.now().astimezone().isoformat()
    schema = {name: str(dtype) for name, dtype in frame.dtypes.items()}
    checksum = _sha256(target)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO dc_cold_archive_manifest
               (archive_id,dataset,api_names_json,date_start,date_end,row_count,file_path,file_sha256,schema_json,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (archive_id, "dc_tushare_raw_records", json.dumps(apis), start, end, len(frame), str(target), checksum,
             json.dumps(schema, ensure_ascii=False), "completed", created_at),
        )
    return {
        "ok": True, "archive_id": archive_id, "dataset": "dc_tushare_raw_records", "api_names": apis,
        "date_start": start, "date_end": end, "row_count": len(frame), "file_path": str(target),
        "file_sha256": checksum, "storage": "local_filesystem", "compression": "zstd",
        "delete_online_rows": False,
    }


def vacuum_table_dead_tuples(
    *,
    table_name: str,
    full: bool = False,
    analyze: bool = True,
    allow_blocking_full: bool = False,
) -> dict[str, Any]:
    """Execute VACUUM / ANALYZE on high-churn tables to reclaim dead tuple storage."""
    allowed_tables = {
        "dc_tushare_raw_records",
        "dc_stock_daily_snapshot",
        "dc_theme_company_mapping",
        "dc_theme_snapshot",
        "dc_tushare_request_coverage",
        "data_ingestion_runs",
        "console_page_snapshots",
    }
    cleaned_table = str(table_name).strip().lower()
    if cleaned_table not in allowed_tables:
        raise ValueError(f"Table {cleaned_table} is not authorized for maintenance vacuum")
    if full and not allow_blocking_full:
        raise ValueError("VACUUM FULL requires allow_blocking_full=True and a maintenance window")

    verb = "VACUUM FULL" if full else "VACUUM"
    if analyze and not full:
        verb = "VACUUM ANALYZE"
    elif analyze and full:
        verb = "VACUUM FULL ANALYZE"

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        from app.data_platform.db import _maintenance_conn

        with _maintenance_conn() as conn:
            conn.execute(f"{verb} public.{cleaned_table}")
        return {
            "ok": True,
            "table": cleaned_table,
            "operation": verb,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "table": cleaned_table,
            "operation": verb,
            "started_at": started_at,
            "error": str(exc)[:300],
            "status": "failed",
        }
