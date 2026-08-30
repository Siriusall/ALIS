"""Compact Feature Store & Dataset Cache Service for ALIS.

This service allows the Database Host to export point-in-time verified market
and text-event features into compact, zstd-compressed columnar/binary feature bundles.
Compute Worker and Application nodes read from their local filesystem cache first,
avoiding full-table PostgreSQL scans and cross-node network egress during
strategy evolution, backtest replay, and model training.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.runtime_paths import backend_cache_root

logger = logging.getLogger("quant.feature_store")
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

FEATURE_BUNDLE_CONTRACT = "alis_compact_feature_bundle_v1"


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


def is_local_feature_cache_enabled() -> bool:
    return str(os.getenv("ALIS_LOCAL_FEATURE_CACHE_ENABLED", "1")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def save_snapshot_bundle_to_local_cache(
    trade_date_ymd: str,
    records: List[Dict[str, Any]],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Persist normalized daily snapshot records into a local compressed JSON-GZ slice."""
    root = _ensure_dir(feature_cache_root() / "daily_snapshots")
    ymd = str(trade_date_ymd).replace("-", "")[:8]
    target_file = root / f"snapshot_{ymd}.json.gz"
    tmp_file = root / f"snapshot_{ymd}.json.gz.tmp"

    payload = {
        "contract": FEATURE_BUNDLE_CONTRACT,
        "trade_date": ymd,
        "row_count": len(records),
        "created_at": datetime.now(_BUSINESS_TZ).isoformat(),
        "metadata": metadata or {},
        "records": records,
    }
    encoded = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"), compresslevel=6)
    tmp_file.write_bytes(encoded)
    tmp_file.replace(target_file)
    return target_file


def load_snapshot_bundle_from_local_cache(
    trade_date_ymd: str,
    *,
    as_of_time: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Load normalized daily snapshot records from local cache if valid and compliant."""
    if not is_local_feature_cache_enabled():
        return None
    ymd = str(trade_date_ymd).replace("-", "")[:8]
    target_file = feature_cache_root() / "daily_snapshots" / f"snapshot_{ymd}.json.gz"
    if not target_file.is_file():
        return None

    try:
        raw_bytes = target_file.read_bytes()
        decompressed = gzip.decompress(raw_bytes).decode("utf-8")
        payload = json.loads(decompressed)
        if not isinstance(payload, dict) or payload.get("contract") != FEATURE_BUNDLE_CONTRACT:
            return None
        records = payload.get("records")
        if not isinstance(records, list):
            return None

        # Point-in-time filtering if as_of_time is provided
        if as_of_time:
            filtered = []
            for item in records:
                observed_at = str(item.get("observed_at") or item.get("available_time") or "")
                if not observed_at or observed_at <= as_of_time:
                    filtered.append(item)
            return filtered

        return records
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load snapshot feature bundle for %s: %s", ymd, exc)
        return None


def export_market_features_bundle(
    start_date: str,
    end_date: str,
    *,
    dest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Export date-range market snapshots and factors into a single portable bundle."""
    from app.data_platform.db import _conn

    start_ymd = str(start_date).replace("-", "")[:8]
    end_ymd = str(end_date).replace("-", "")[:8]
    with _conn() as conn:
        rows = conn.execute(
            """SELECT ts_code,trade_date,name,close,change_pct,volume,turnover,
                      total_mv,circ_mv,pe_ttm,pb,turnover_rate,observed_at,
                      open AS daily_open,high AS daily_high,low AS daily_low,
                      pre_close AS daily_pre_close,adj_factor
                 FROM dc_stock_daily_snapshot
                WHERE trade_date>=? AND trade_date<=?
                ORDER BY trade_date,ts_code""",
            (start_ymd, end_ymd),
        ).fetchall()

    records = [dict(row) for row in rows]
    target = dest_path or (
        feature_cache_root() / f"bundle_{start_ymd}_{end_ymd}.json.gz"
    )
    _ensure_dir(target.parent)

    bundle_payload = {
        "contract": FEATURE_BUNDLE_CONTRACT,
        "start_date": start_ymd,
        "end_date": end_ymd,
        "row_count": len(records),
        "created_at": datetime.now(_BUSINESS_TZ).isoformat(),
        "records": records,
    }
    raw = json.dumps(bundle_payload, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6)
    target.write_bytes(compressed)

    return {
        "ok": True,
        "file_path": str(target),
        "bytes": len(compressed),
        "raw_bytes": len(raw),
        "compression_ratio": round(len(raw) / max(1, len(compressed)), 2),
        "row_count": len(records),
    }


def save_text_event_features_to_local_cache(
    trade_date_ymd: str,
    records: List[Dict[str, Any]],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Persist structured announcement/news event features into a local compressed slice."""
    root = _ensure_dir(feature_cache_root() / "text_events")
    ymd = str(trade_date_ymd).replace("-", "")[:8]
    target_file = root / f"events_{ymd}.json.gz"
    tmp_file = root / f"events_{ymd}.json.gz.tmp"

    payload = {
        "contract": FEATURE_BUNDLE_CONTRACT,
        "category": "text_events",
        "trade_date": ymd,
        "row_count": len(records),
        "created_at": datetime.now(_BUSINESS_TZ).isoformat(),
        "metadata": metadata or {},
        "records": records,
    }
    encoded = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"), compresslevel=6)
    tmp_file.write_bytes(encoded)
    tmp_file.replace(target_file)
    return target_file


def load_text_event_features_from_local_cache(
    trade_date_ymd: str,
    *,
    as_of_time: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Load structured text event features from local cache if present."""
    if not is_local_feature_cache_enabled():
        return None
    ymd = str(trade_date_ymd).replace("-", "")[:8]
    target_file = feature_cache_root() / "text_events" / f"events_{ymd}.json.gz"
    if not target_file.is_file():
        return None

    try:
        raw_bytes = target_file.read_bytes()
        decompressed = gzip.decompress(raw_bytes).decode("utf-8")
        payload = json.loads(decompressed)
        if not isinstance(payload, dict) or payload.get("contract") != FEATURE_BUNDLE_CONTRACT:
            return None
        records = payload.get("records")
        if not isinstance(records, list):
            return None

        if as_of_time:
            filtered = []
            for item in records:
                observed_at = str(item.get("observed_at") or item.get("available_time") or "")
                if not observed_at or observed_at <= as_of_time:
                    filtered.append(item)
            return filtered

        return records
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load text event features for %s: %s", ymd, exc)
        return None
