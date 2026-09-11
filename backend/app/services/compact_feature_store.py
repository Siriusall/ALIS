"""Point-in-time safe local Parquet feature bundles for research workloads."""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.runtime_paths import backend_cache_root

logger = logging.getLogger("quant.feature_store")
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
FEATURE_BUNDLE_CONTRACT = "alis_compact_feature_bundle_v2"
_MANIFEST_SUFFIX = ".manifest.json"


def feature_cache_root() -> Path:
    configured = os.getenv("ALIS_FEATURE_CACHE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return backend_cache_root() / "feature_store"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ymd(value: str) -> str:
    cleaned = str(value or "").replace("-", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise ValueError("date must be YYYYMMDD")
    datetime.strptime(cleaned, "%Y%m%d")
    return cleaned


def _available_at(record: Dict[str, Any]) -> datetime:
    raw = record.get("available_time") or record.get("observed_at")
    if not raw:
        raise ValueError("feature cache record is missing available_time/observed_at")
    parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("feature cache availability timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("as_of_time must include timezone")
    return parsed.astimezone(timezone.utc)


def is_local_feature_cache_enabled() -> bool:
    return str(os.getenv("ALIS_LOCAL_FEATURE_CACHE_ENABLED", "1")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _manifest_path(data_path: Path) -> Path:
    return data_path.with_name(data_path.name + _MANIFEST_SUFFIX)


def _write_parquet_slice(
    target: Path,
    records: List[Dict[str, Any]],
    *,
    category: str,
    trade_date: str,
    metadata: Optional[Dict[str, Any]],
) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    expected_date = _ymd(trade_date)
    available_times = [_available_at(item) for item in records]
    for item in records:
        item_date = _ymd(str(item.get("trade_date") or expected_date))
        if item_date != expected_date:
            raise ValueError(f"feature cache row date {item_date} does not match {expected_date}")

    _ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_manifest = _manifest_path(tmp_path)
    try:
        pq.write_table(pa.Table.from_pylist(records), tmp_path, compression="zstd", use_dictionary=True)
        frozen_metadata = metadata or {}
        manifest = {
            "contract": FEATURE_BUNDLE_CONTRACT,
            "category": category,
            "trade_date": expected_date,
            "row_count": len(records),
            "created_at": datetime.now(_BUSINESS_TZ).isoformat(),
            "min_available_time": min(available_times).isoformat() if available_times else None,
            "max_available_time": max(available_times).isoformat() if available_times else None,
            "metadata": frozen_metadata,
            "metadata_sha256": _sha256_bytes(
                json.dumps(frozen_metadata, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ),
            "file_sha256": _sha256_file(tmp_path),
        }
        tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(target)
        tmp_manifest.replace(_manifest_path(target))
        return target
    finally:
        tmp_path.unlink(missing_ok=True)
        tmp_manifest.unlink(missing_ok=True)


def _load_parquet_slice(
    target: Path,
    *,
    category: str,
    trade_date: str,
    as_of_time: Optional[str],
) -> Optional[List[Dict[str, Any]]]:
    if not target.is_file() or not _manifest_path(target).is_file():
        return None
    try:
        import pyarrow.parquet as pq

        manifest = json.loads(_manifest_path(target).read_text(encoding="utf-8"))
        manifest_metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
        metadata_sha = _sha256_bytes(
            json.dumps(manifest_metadata, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        )
        if (
            manifest.get("contract") != FEATURE_BUNDLE_CONTRACT
            or manifest.get("category") != category
            or manifest.get("trade_date") != _ymd(trade_date)
            or manifest.get("file_sha256") != _sha256_file(target)
            or manifest.get("metadata_sha256") != metadata_sha
        ):
            return None
        records = pq.read_table(target).to_pylist()
        if int(manifest.get("row_count", -1)) != len(records):
            return None
        cutoff = _cutoff(as_of_time) if as_of_time else None
        available_times = [_available_at(item) for item in records]
        if records and (
            manifest.get("min_available_time") != min(available_times).isoformat()
            or manifest.get("max_available_time") != max(available_times).isoformat()
        ):
            return None
        result: List[Dict[str, Any]] = []
        for item, available_at in zip(records, available_times):
            if cutoff is None or available_at <= cutoff:
                result.append(item)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rejected invalid local feature slice %s: %s", target, exc)
        return None


def save_snapshot_bundle_to_local_cache(
    trade_date_ymd: str,
    records: List[Dict[str, Any]],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    ymd = _ymd(trade_date_ymd)
    target = feature_cache_root() / "daily_snapshots" / f"snapshot_{ymd}.parquet"
    return _write_parquet_slice(target, records, category="daily_snapshot", trade_date=ymd, metadata=metadata)


def load_snapshot_bundle_from_local_cache(
    trade_date_ymd: str,
    *,
    as_of_time: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    if not is_local_feature_cache_enabled():
        return None
    ymd = _ymd(trade_date_ymd)
    target = feature_cache_root() / "daily_snapshots" / f"snapshot_{ymd}.parquet"
    return _load_parquet_slice(target, category="daily_snapshot", trade_date=ymd, as_of_time=as_of_time)


def _month_ranges(start_ymd: str, end_ymd: str) -> List[tuple[str, str]]:
    cursor = datetime.strptime(start_ymd, "%Y%m%d").date().replace(day=1)
    end = datetime.strptime(end_ymd, "%Y%m%d").date()
    ranges: List[tuple[str, str]] = []
    while cursor <= end:
        month_end = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
        ranges.append((max(cursor.strftime("%Y%m%d"), start_ymd), min(month_end.strftime("%Y%m%d"), end_ymd)))
        cursor = (month_end + timedelta(days=1)).replace(day=1)
    return ranges


def export_market_features_bundle(
    start_date: str,
    end_date: str,
    *,
    dest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Export monthly Parquet partitions without materialising the full range in memory."""
    from app.data_platform.db import _read_conn

    start_ymd, end_ymd = _ymd(start_date), _ymd(end_date)
    if start_ymd > end_ymd:
        raise ValueError("start_date must not be after end_date")
    target_dir = Path(dest_path or feature_cache_root() / f"bundle_{start_ymd}_{end_ymd}")
    if target_dir.exists():
        raise FileExistsError(f"feature bundle target already exists: {target_dir}")
    stage = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}.", dir=_ensure_dir(target_dir.parent)))
    partitions: List[Dict[str, Any]] = []
    total_rows = 0
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        for month_start, month_end in _month_ranges(start_ymd, end_ymd):
            with _read_conn() as conn:
                rows = conn.execute(
                    """SELECT ts_code,trade_date,name,close,change_pct,volume,turnover,
                              total_mv,circ_mv,pe_ttm,pb,turnover_rate,observed_at,
                              open AS daily_open,high AS daily_high,low AS daily_low,
                              pre_close AS daily_pre_close,adj_factor
                         FROM dc_stock_daily_snapshot
                        WHERE trade_date>=? AND trade_date<=?
                        ORDER BY trade_date,ts_code""",
                    (month_start, month_end),
                ).fetchall()
            records = [dict(row) for row in rows]
            if not records:
                continue
            for record in records:
                _available_at(record)
            partition = stage / f"month={month_start[:6]}" / "features.parquet"
            _ensure_dir(partition.parent)
            pq.write_table(pa.Table.from_pylist(records), partition, compression="zstd", use_dictionary=True)
            partitions.append({
                "path": str(partition.relative_to(stage)),
                "row_count": len(records),
                "sha256": _sha256_file(partition),
            })
            total_rows += len(records)
        manifest = {
            "contract": FEATURE_BUNDLE_CONTRACT,
            "category": "market_features",
            "start_date": start_ymd,
            "end_date": end_ymd,
            "row_count": total_rows,
            "created_at": datetime.now(_BUSINESS_TZ).isoformat(),
            "partitions": partitions,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        stage.replace(target_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    total_bytes = sum((target_dir / item["path"]).stat().st_size for item in partitions)
    return {
        "ok": True,
        "file_path": str(target_dir),
        "bytes": total_bytes,
        "row_count": total_rows,
        "partitions": len(partitions),
    }


def save_text_event_features_to_local_cache(
    trade_date_ymd: str,
    records: List[Dict[str, Any]],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    ymd = _ymd(trade_date_ymd)
    target = feature_cache_root() / "text_events" / f"events_{ymd}.parquet"
    return _write_parquet_slice(target, records, category="text_events", trade_date=ymd, metadata=metadata)


def load_text_event_features_from_local_cache(
    trade_date_ymd: str,
    *,
    as_of_time: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    if not is_local_feature_cache_enabled():
        return None
    ymd = _ymd(trade_date_ymd)
    target = feature_cache_root() / "text_events" / f"events_{ymd}.parquet"
    return _load_parquet_slice(target, category="text_events", trade_date=ymd, as_of_time=as_of_time)
