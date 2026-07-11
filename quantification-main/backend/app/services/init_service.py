from __future__ import annotations

import logging
import os
import time
from typing import Any

from app.core.trade_calendar import get_last_trade_date_str
from app.integrations.market_data_client import fetch_a_share_spot
from app.services.cache_consolidation import (
    build_consolidated_cache_bundle,
    cleanup_cache_temp_files,
    cleanup_source_cache_dirs,
    has_consolidated_for_date,
    is_strict_first_load_enabled,
    read_init_status,
    should_rebuild_consolidated_for_date,
    validate_first_load_quality,
    write_init_status,
)
from app.services.daily_data import prefetch_daily_bars_for_init, roll_forward_daily_cache
from app.services.startup_checks import run_startup_connectivity_checks
from app.services.tushare_snapshot import get_daily_snapshot
from app.services.tushare_universe import get_basic_info_map

logger = logging.getLogger("quant.init")


def get_data_init_status() -> dict[str, Any]:
    return read_init_status()


def run_data_initialization(
    *,
    all_in_one_only: bool = True,
    strict: bool | None = None,
    allow_remote: bool = True,
) -> dict[str, Any]:
    """Run the heavy market-data initialization explicitly.

    FastAPI startup intentionally does not call this function. Keeping the data
    load behind /api/init lets the service boot even when TuShare, caches, or
    local historical data are temporarily unavailable.
    """

    started = time.perf_counter()
    stage_costs: dict[str, float] = {}
    errors: list[str] = []

    def mark(name: str, t0: float) -> None:
        stage_costs[name] = round(time.perf_counter() - t0, 3)

    today_ymd = get_last_trade_date_str(for_data_availability=True)
    strict_mode = is_strict_first_load_enabled() if strict is None else bool(strict)
    first_load_for_today = not has_consolidated_for_date(today_ymd)

    write_init_status(
        {
            "status": "running",
            "trade_date": today_ymd,
            "strict_first_load": strict_mode,
            "first_load_for_today": first_load_for_today,
            "stage_costs": {},
            "message": "data initialization is running",
        }
    )

    try:
        t0 = time.perf_counter()
        connectivity = run_startup_connectivity_checks()
        mark("startup_checks", t0)
        if not connectivity.get("tushare_ok"):
            errors.extend([str(x) for x in connectivity.get("errors", [])])

        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or ""
        if proxy:
            logger.info("HTTP proxy configured: %s", proxy)

        t0 = time.perf_counter()
        roll_forward_daily_cache()
        mark("roll_forward_daily_cache", t0)

        t0 = time.perf_counter()
        quotes = fetch_a_share_spot(limit=None, allow_remote=allow_remote)
        mark("fetch_a_share_spot", t0)

        t0 = time.perf_counter()
        snapshot: dict[str, Any] = {}
        try:
            snapshot = get_daily_snapshot(force_refresh=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"snapshot_failed: {str(exc)[:200]}")
            logger.warning("daily snapshot preload failed: %s", str(exc)[:120])
        mark("get_daily_snapshot", t0)

        t0 = time.perf_counter()
        basic_map: dict[str, Any] = {}
        try:
            basic_map = get_basic_info_map(refresh=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"basic_info_failed: {str(exc)[:200]}")
            logger.warning("basic info preload failed: %s", str(exc)[:120])
        mark("get_basic_info_map", t0)

        t0 = time.perf_counter()
        try:
            prefetch_daily_bars_for_init(quotes)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"daily_prefetch_failed: {str(exc)[:200]}")
            logger.warning("daily bar prefetch failed: %s", str(exc)[:120])
        mark("prefetch_daily_bars_for_init", t0)

        merged_payload: dict[str, Any] | None = None
        t0 = time.perf_counter()
        should_rebuild = should_rebuild_consolidated_for_date(today_ymd)
        if should_rebuild or not has_consolidated_for_date(today_ymd):
            merged_payload = build_consolidated_cache_bundle(trade_date=today_ymd)
        else:
            merged_payload = {"counts": {}, "skipped": True, "reason": "already_current"}
        mark("build_consolidated_cache_bundle", t0)

        t0 = time.perf_counter()
        cleanup = cleanup_cache_temp_files()
        mark("cleanup_cache_temp_files", t0)

        source_cleanup = {"enabled": False, "removed_dirs": 0, "removed_files": 0, "failed": 0}
        if all_in_one_only:
            t0 = time.perf_counter()
            source_cleanup = cleanup_source_cache_dirs()
            source_cleanup["enabled"] = True
            mark("cleanup_source_cache_dirs", t0)

        quality = validate_first_load_quality(
            spot_count=len(quotes),
            snapshot_count=len(snapshot) if isinstance(snapshot, dict) else 0,
            basic_info_count=len(basic_map) if isinstance(basic_map, dict) else 0,
            merged_payload=merged_payload or {},
            trade_date=today_ymd,
        )

        if strict_mode and not bool(quality.get("ok")):
            payload = {
                "status": "failed",
                "trade_date": today_ymd,
                "strict_first_load": strict_mode,
                "first_load_for_today": first_load_for_today,
                "stage_costs": stage_costs,
                "quality": quality,
                "errors": errors,
                "message": "data initialization quality check failed",
            }
            write_init_status(payload)
            return {"ok": False, **payload}

        total_cost = round(time.perf_counter() - started, 3)
        payload = {
            "status": "completed",
            "trade_date": today_ymd,
            "strict_first_load": strict_mode,
            "first_load_for_today": first_load_for_today,
            "stage_costs": stage_costs,
            "total_cost_sec": total_cost,
            "spot_count": len(quotes),
            "snapshot_count": len(snapshot) if isinstance(snapshot, dict) else 0,
            "basic_info_count": len(basic_map) if isinstance(basic_map, dict) else 0,
            "merged_counts": (merged_payload or {}).get("counts", {}),
            "quality": quality,
            "cleanup": {"removed": cleanup.get("removed", 0), "failed": cleanup.get("failed", 0)},
            "source_cleanup": source_cleanup,
            "errors": errors,
            "message": "data initialization completed",
        }
        write_init_status(payload)
        return {"ok": True, **payload}
    except Exception as exc:  # noqa: BLE001
        payload = {
            "status": "failed",
            "trade_date": today_ymd,
            "strict_first_load": strict_mode,
            "first_load_for_today": first_load_for_today,
            "stage_costs": stage_costs,
            "errors": [*errors, str(exc)[:500]],
            "message": "data initialization failed",
        }
        write_init_status(payload)
        raise
