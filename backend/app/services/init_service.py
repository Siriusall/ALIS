"""PostgreSQL-backed market data initialization."""

from __future__ import annotations

import time
from typing import Any

from app.core.trade_calendar import get_last_trade_date_str
from app.services.cache_consolidation import (
    has_consolidated_for_date,
    is_strict_first_load_enabled,
    read_init_status,
    write_init_status,
)
from app.services.startup_checks import run_startup_connectivity_checks


def get_data_init_status() -> dict[str, Any]:
    return read_init_status()


def run_data_initialization(
    *,
    all_in_one_only: bool = True,
    strict: bool | None = None,
    allow_remote: bool = True,
) -> dict[str, Any]:
    """Synchronize the current market snapshot into PostgreSQL.

    The keyword arguments are retained for API compatibility.  Durable market
    data is always written through the data-platform pipeline; no local cache
    bundle is created or read by this entry point.
    """
    del all_in_one_only, allow_remote
    started = time.perf_counter()
    stage_costs: dict[str, float] = {}
    errors: list[str] = []
    today_ymd = get_last_trade_date_str(for_data_availability=True)
    strict_mode = is_strict_first_load_enabled() if strict is None else bool(strict)
    first_load_for_today = not has_consolidated_for_date(today_ymd)

    def mark(name: str, started_at: float) -> None:
        stage_costs[name] = round(time.perf_counter() - started_at, 3)

    write_init_status({
        "status": "running", "trade_date": today_ymd,
        "strict_first_load": strict_mode, "first_load_for_today": first_load_for_today,
        "stage_costs": {}, "message": "PostgreSQL data initialization is running",
    })
    try:
        from app.data_platform.db import _conn
        from app.data_platform.services.data_center import (
            build_stock_daily_snapshot,
            run_data_quality_checks,
            sync_market_bundle,
            sync_stock_basic,
        )

        t0 = time.perf_counter()
        connectivity = run_startup_connectivity_checks()
        mark("startup_checks", t0)
        if not connectivity.get("tushare_ok"):
            errors.extend(str(item) for item in connectivity.get("errors", []))

        t0 = time.perf_counter()
        master = sync_stock_basic()
        mark("postgres_stock_master", t0)
        t0 = time.perf_counter()
        market_bundle = sync_market_bundle(today_ymd)
        mark("postgres_market_bundle", t0)
        ingestion: dict[str, Any] = {
            "market_bundle": market_bundle,
            "providers": dict(market_bundle.get("winners") or {}),
        }

        t0 = time.perf_counter()
        snapshot_result = build_stock_daily_snapshot(today_ymd)
        mark("postgres_daily_snapshot", t0)
        quality_result = run_data_quality_checks(today_ymd)
        with _conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM dc_stock_daily_snapshot WHERE trade_date=?", (today_ymd,)
            ).fetchone()
        snapshot_count = int(dict(row).get("cnt") or 0)
        master_count = int(master.get("rows_written") or 0)
        quality = {
            "ok": master_count >= 5000 and snapshot_count >= 5000,
            "checks": {"snapshot_count": snapshot_count, "basic_info_count": master_count, "data_quality": quality_result},
            "errors": [] if master_count >= 5000 and snapshot_count >= 5000 else [
                f"PostgreSQL data coverage insufficient: master={master_count}, snapshot={snapshot_count}"
            ],
            "trade_date": today_ymd,
        }
        market_bundle_ok = bool(market_bundle.get("ok"))
        if not market_bundle_ok:
            category = str(
                market_bundle.get("failure_category")
                or market_bundle.get("status")
                or "market_bundle_not_ready"
            )
            quality["ok"] = False
            quality["errors"].append(f"Market bundle incomplete: {category}")
            errors.append(f"market_bundle:{category}")
        # A non-strict invocation only controls whether this is the first-load
        # policy; it must never turn a missing market slice into a successful
        # initialization.  Downstream callers rely on this result as a hard
        # data-availability signal.
        status = "completed" if quality["ok"] and market_bundle_ok else "failed"
        payload = {
            "status": status, "trade_date": today_ymd, "strict_first_load": strict_mode,
            "first_load_for_today": first_load_for_today, "stage_costs": stage_costs,
            "total_cost_sec": round(time.perf_counter() - started, 3), "spot_count": snapshot_count,
            "snapshot_count": snapshot_count, "basic_info_count": master_count, "quality": quality,
            "ingestion": ingestion, "snapshot": snapshot_result, "errors": errors,
            "market_bundle_failure_category": market_bundle.get("failure_category") or "",
            "message": "PostgreSQL data initialization completed" if status == "completed" else "PostgreSQL data initialization quality check failed",
        }
        write_init_status(payload)
        return {"ok": status == "completed", **payload}
    except Exception as exc:
        payload = {
            "status": "failed", "trade_date": today_ymd, "strict_first_load": strict_mode,
            "first_load_for_today": first_load_for_today, "stage_costs": stage_costs,
            "errors": [*errors, str(exc)[:500]], "message": "PostgreSQL data initialization failed",
        }
        write_init_status(payload)
        raise
