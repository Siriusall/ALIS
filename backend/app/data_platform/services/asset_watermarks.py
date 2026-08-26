"""Persistent asset watermarks and private-deployment migration inventory."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn


CORE_ASSETS = {
    "stock_master", "trade_calendar", "daily", "valuation", "limit_price", "snapshot",
    "adj_factor", "suspension", "income", "balancesheet", "cashflow",
    "financial_indicator", "financial_research_fields", "announcements",
}
OPTIONAL_ASSETS = {"web_fallback", "shareholder_return", "namechange", "pledge_detail"}
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
# Watermark audits older than this, or lagging the live market day, must refresh.
_WATERMARK_STALE_HOURS = 12
# These assets share the market trade-date frontier and can be patched cheaply.
_MARKET_FRONTIER_ASSETS = frozenset({
    "daily",
    "adj_factor",
    "valuation",
    "limit_price",
    "snapshot",
    "suspension",
    "moneyflow",
    "industry_index",
    "vip_fund_daily",
    "vip_margin",
    "vip_margin_detail",
    "vip_moneyflow_hsgt",
    "vip_top_inst",
    "vip_top_list",
    "etf_price_nav_vip",
})
_ASSET_CATALOG_STATEMENT_TIMEOUT_MS = "300000"


def _tier(asset_key: str) -> str:
    if asset_key in CORE_ASSETS:
        return "core"
    if asset_key in OPTIONAL_ASSETS:
        return "optional"
    return "enhanced"


def _normalize_trade_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _live_market_max_trade_date() -> str:
    """Return the freshest durable market date without rebuilding the catalog."""
    try:
        with _conn() as conn:
            probes = (
                "SELECT MAX(trade_date) AS max_date FROM dc_available_trade_dates",
                "SELECT MAX(trade_date) AS max_date FROM dc_stock_daily_snapshot",
                """
                SELECT MAX(trade_date) AS max_date
                FROM dc_tushare_request_coverage
                WHERE api_name='daily'
                """,
            )
            for sql in probes:
                try:
                    row = conn.execute(sql).fetchone()
                except Exception:  # noqa: BLE001 - optional probe tables may be absent
                    continue
                value = _normalize_trade_date(dict(row or {}).get("max_date"))
                if value:
                    return value
    except Exception:  # noqa: BLE001 - freshness must degrade when DB is unreachable
        return ""
    return ""


def _audit_trade_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Prefer explicit YYYYMMDD / ISO date prefixes over full timestamps.
    normalized = _normalize_trade_date(text[:10])
    if normalized:
        return normalized
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(_BUSINESS_TZ).strftime("%Y%m%d")
    except ValueError:
        return ""


def persist_asset_watermarks(payload: dict[str, Any]) -> None:
    """Persist the latest audit; rows survive API loss and process restarts."""
    now = datetime.now().isoformat(timespec="seconds")
    rows: list[tuple[Any, ...]] = []
    for item in payload.get("items") or []:
        key = str(item.get("key") or "")
        if not key:
            continue
        expected = int(item.get("coverage_expected") or 0)
        available = int(item.get("coverage_basis") or 0)
        status = str(item.get("status") or "unknown")
        metadata = {
            "coverage": item.get("coverage"),
            "window_start": item.get("window_start"),
            "window_end": item.get("window_end"),
            "measurement": item.get("measurement"),
            "data_max_date": item.get("max_date"),
            "min_date": item.get("min_date"),
            "asset_snapshot": item,
        }
        rows.append((key, "overall", _tier(key), expected, available, max(0, expected - available),
                     "complete" if status in {"complete", "available"} else "repairable",
                     "", 10 if key in CORE_ASSETS else 50 if _tier(key) == "enhanced" else 90,
                     str(item.get("source") or ""), json.dumps(metadata, ensure_ascii=False, default=str), now, now))
        for detail in item.get("period_details") or []:
            period = str(detail.get("period") or "")
            checks = int(detail.get("checks") or 0)
            complete = int(detail.get("complete_checks") or 0)
            rows.append((key, period, _tier(key), checks, complete, max(0, checks - complete),
                         "complete" if complete >= checks and checks else "repairable", "", 10,
                         str(item.get("source") or ""), json.dumps(detail, ensure_ascii=False), now, now))
    with _conn() as conn:
        for row in rows:
            conn.execute("""INSERT INTO dc_asset_watermark
                (asset_key,slice_key,contract_tier,expected_count,available_count,missing_count,status,
                 terminal_reason,repair_priority,source_code,metadata_json,checked_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset_key,slice_key) DO UPDATE SET contract_tier=excluded.contract_tier,
                 expected_count=excluded.expected_count,available_count=excluded.available_count,
                 missing_count=excluded.missing_count,
                 status=CASE WHEN dc_asset_watermark.status IN ('upstream_unavailable','not_applicable','permission_denied')
                    AND excluded.available_count<=dc_asset_watermark.available_count THEN dc_asset_watermark.status ELSE excluded.status END,
                 terminal_reason=CASE WHEN dc_asset_watermark.status IN ('upstream_unavailable','not_applicable','permission_denied')
                    AND excluded.available_count<=dc_asset_watermark.available_count THEN dc_asset_watermark.terminal_reason ELSE excluded.terminal_reason END,
                 repair_priority=excluded.repair_priority,source_code=excluded.source_code,
                 metadata_json=excluded.metadata_json,checked_at=excluded.checked_at,updated_at=excluded.updated_at""", row)


def mark_asset_terminal(asset_key: str, slice_key: str, status: str, reason: str) -> None:
    if status not in {"upstream_unavailable", "not_applicable", "permission_denied"}:
        raise ValueError("invalid terminal asset status")
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute("""UPDATE dc_asset_watermark SET status=?,terminal_reason=?,checked_at=?,updated_at=?
            WHERE asset_key=? AND slice_key=?""", (status, str(reason)[:1000], now, now, asset_key, slice_key))


def apply_terminal_states(payload: dict[str, Any]) -> dict[str, Any]:
    with _conn() as conn:
        rows = conn.execute("""SELECT asset_key,slice_key,status,terminal_reason,available_count
            FROM dc_asset_watermark WHERE status IN ('upstream_unavailable','not_applicable','permission_denied')""").fetchall()
    terminal = {(str(dict(r)["asset_key"]), str(dict(r)["slice_key"])): dict(r) for r in rows}
    for item in payload.get("items") or []:
        key = str(item.get("key") or "")
        for detail in item.get("period_details") or []:
            state = terminal.get((key, str(detail.get("period") or "")))
            if state and int(detail.get("complete_checks") or 0) <= int(state.get("available_count") or 0):
                detail["status"] = state["status"]
                detail["terminal_reason"] = state.get("terminal_reason") or ""
        overall = terminal.get((key, "overall"))
        if overall and int(item.get("coverage_basis") or 0) <= int(overall.get("available_count") or 0):
            item["repair_status"] = overall["status"]
            item["terminal_reason"] = overall.get("terminal_reason") or ""
    return payload


def _enrich_watermark_item(
    item: dict[str, Any],
    *,
    live_max_trade_date: str = "",
) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    snapshot = metadata.get("asset_snapshot") if isinstance(metadata.get("asset_snapshot"), dict) else {}
    data_max_date = (
        _normalize_trade_date(metadata.get("data_max_date"))
        or _normalize_trade_date(snapshot.get("max_date"))
        or _normalize_trade_date(item.get("latest_trade_date"))
    )
    window_end = (
        _normalize_trade_date(metadata.get("window_end"))
        or _normalize_trade_date(snapshot.get("window_end"))
        or data_max_date
    )
    live_override = False
    asset_key = str(item.get("asset_key") or "")
    if (
        live_max_trade_date
        and str(item.get("slice_key") or "") == "overall"
        and asset_key in _MARKET_FRONTIER_ASSETS
        and (not data_max_date or data_max_date < live_max_trade_date)
    ):
        data_max_date = live_max_trade_date
        window_end = live_max_trade_date
        live_override = True
    item["name"] = snapshot.get("name") or item.get("asset_key")
    item["data_max_date"] = data_max_date or None
    item["window_end"] = window_end or None
    item["latest_trade_date"] = data_max_date or window_end or None
    item["audit_updated_at"] = item.get("updated_at")
    item["audit_checked_at"] = item.get("checked_at")
    # Keep `watermark` as the data frontier, not the audit write time.
    item["watermark"] = data_max_date or window_end or None
    item["live_frontier_override"] = live_override
    return item


def patch_live_market_frontier(*, live_max_trade_date: str | None = None) -> dict[str, Any]:
    """Persist cheap live market dates without waiting for the full catalog rebuild."""
    live_max = _normalize_trade_date(live_max_trade_date) or _live_market_max_trade_date()
    if not live_max:
        return {"ok": False, "patched": 0, "reason": "live_max_unavailable"}
    now = datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")
    patched = 0
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT asset_key, metadata_json
            FROM dc_asset_watermark
            WHERE slice_key='overall'
            """
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            asset_key = str(row.get("asset_key") or "")
            if asset_key not in _MARKET_FRONTIER_ASSETS:
                continue
            metadata = json.loads(row.get("metadata_json") or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
            snapshot = metadata.get("asset_snapshot") if isinstance(metadata.get("asset_snapshot"), dict) else {}
            current = (
                _normalize_trade_date(metadata.get("data_max_date"))
                or _normalize_trade_date(snapshot.get("max_date"))
                or _normalize_trade_date(metadata.get("window_end"))
                or _normalize_trade_date(snapshot.get("window_end"))
            )
            if current and current >= live_max:
                continue
            metadata["data_max_date"] = live_max
            metadata["window_end"] = live_max
            if snapshot:
                snapshot = dict(snapshot)
                snapshot["max_date"] = live_max
                snapshot["window_end"] = live_max
                metadata["asset_snapshot"] = snapshot
            metadata["live_frontier_patched_at"] = now
            conn.execute(
                """
                UPDATE dc_asset_watermark
                SET metadata_json=?, updated_at=?
                WHERE asset_key=? AND slice_key='overall'
                """,
                (json.dumps(metadata, ensure_ascii=False, default=str), now, asset_key),
            )
            patched += 1
    return {
        "ok": True,
        "patched": patched,
        "live_max_trade_date": live_max,
        "patched_at": now,
    }


def _watermark_freshness(items: list[dict[str, Any]]) -> dict[str, Any]:
    overall = [item for item in items if str(item.get("slice_key") or "") == "overall"]
    audit_checked_at = max((str(item.get("checked_at") or "") for item in overall), default="")
    audit_trade_date = _audit_trade_date(audit_checked_at)
    live_max_trade_date = _live_market_max_trade_date()
    age_hours = None
    if audit_checked_at:
        try:
            checked = datetime.fromisoformat(audit_checked_at.replace("Z", "+00:00"))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=_BUSINESS_TZ)
            age_hours = round(
                max(0.0, (datetime.now(_BUSINESS_TZ) - checked.astimezone(_BUSINESS_TZ)).total_seconds() / 3600.0),
                2,
            )
        except ValueError:
            age_hours = None
    lagged_market = bool(
        live_max_trade_date
        and (not audit_trade_date or audit_trade_date < live_max_trade_date)
    )
    aged_out = age_hours is not None and age_hours >= _WATERMARK_STALE_HOURS
    stale = lagged_market or aged_out or not overall
    return {
        "live_max_trade_date": live_max_trade_date or None,
        "audit_checked_at": audit_checked_at or None,
        "audit_trade_date": audit_trade_date or None,
        "age_hours": age_hours,
        "stale": stale,
        "reason": (
            "missing_watermark_rows"
            if not overall
            else "audit_behind_live_market"
            if lagged_market
            else "audit_aged_out"
            if aged_out
            else "fresh"
        ),
    }


def request_data_assets_refresh(*, invalidate: bool = True) -> dict[str, Any]:
    """Ask the expensive catalog rebuild to run without blocking the caller."""
    from app.data_platform.services.assets import invalidate_data_assets_cache, request_assets_refresh

    if invalidate:
        invalidate_data_assets_cache()
    started = request_assets_refresh()
    if started:
        try:
            from app.services.console_snapshots import invalidate as invalidate_console_snapshots

            invalidate_console_snapshots(["data", "overview"], reason="asset_watermarks_refresh")
        except Exception:  # noqa: BLE001 - console invalidation must not block data refresh
            pass
    return {"started": started, "running": bool(started)}


def asset_watermark_status(*, trigger_refresh_if_stale: bool = True) -> dict[str, Any]:
    with _conn() as conn:
        rows = conn.execute("""SELECT asset_key,slice_key,contract_tier,expected_count,available_count,
            missing_count,status,terminal_reason,repair_priority,source_code,metadata_json,checked_at,updated_at
            FROM dc_asset_watermark ORDER BY repair_priority,asset_key,slice_key""").fetchall()
    live_max_trade_date = _live_market_max_trade_date()
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        items.append(_enrich_watermark_item(item, live_max_trade_date=live_max_trade_date))
    freshness = _watermark_freshness(items)
    # Recompute with the true live probe so lag detection is independent of
    # per-row overrides that already surface the frontier on the page.
    freshness["live_max_trade_date"] = live_max_trade_date or freshness.get("live_max_trade_date")
    if live_max_trade_date and freshness.get("audit_trade_date") and freshness["audit_trade_date"] < live_max_trade_date:
        freshness["stale"] = True
        freshness["reason"] = "audit_behind_live_market"
    refresh = {"started": False, "running": False, "triggered": False, "frontier_patch": None}
    if trigger_refresh_if_stale and freshness["stale"]:
        refresh["frontier_patch"] = patch_live_market_frontier(live_max_trade_date=live_max_trade_date)
        refresh = {
            **refresh,
            **request_data_assets_refresh(invalidate=True),
            "triggered": True,
        }
    return {
        "ok": True,
        "items": items,
        "freshness": freshness,
        "refresh": refresh,
        "summary": {
            "watermarks": len(items),
            "repairable": sum(x["status"] == "repairable" for x in items),
            "terminal": sum(x["status"] in {"upstream_unavailable", "not_applicable", "permission_denied"} for x in items),
            "missing_count": sum(int(x["missing_count"] or 0) for x in items if x["slice_key"] == "overall"),
            "stale": freshness["stale"],
            "live_max_trade_date": freshness["live_max_trade_date"],
            "audit_checked_at": freshness["audit_checked_at"],
        },
    }


def load_asset_catalog_snapshot() -> dict[str, Any]:
    """Rebuild the last persisted catalog without scanning raw data tables."""
    status = asset_watermark_status(trigger_refresh_if_stale=False)
    overall = [item for item in status["items"] if item["slice_key"] == "overall"]
    items = []
    for row in overall:
        snapshot = dict((row.get("metadata") or {}).get("asset_snapshot") or {})
        snapshot.setdefault("key", row["asset_key"])
        snapshot.setdefault("source", row["source_code"])
        snapshot.setdefault("actual", row["available_count"])
        snapshot.setdefault("expected", row["expected_count"])
        snapshot.setdefault("max_date", row.get("data_max_date"))
        snapshot.setdefault("window_end", row.get("window_end"))
        snapshot["status"] = (
            snapshot.get("status")
            or ("complete" if row["status"] == "complete" else row["status"])
        )
        snapshot["watermark_checked_at"] = row["checked_at"]
        items.append(snapshot)
    checked = max((str(item.get("checked_at") or "") for item in overall), default="")
    freshness = status.get("freshness") or {}
    return {
        "ok": True,
        "items": items,
        "summary": status["summary"],
        "freshness": freshness,
        "snapshot": {
            "source": "dc_asset_watermark",
            "checked_at": checked,
            "stale": bool(freshness.get("stale", True)),
            "live_max_trade_date": freshness.get("live_max_trade_date"),
        },
    }


MIGRATION_TABLES = (
    "dc_tushare_raw_records", "dc_tushare_request_coverage", "dc_stock_master", "dc_trade_calendar",
    "dc_stock_daily_snapshot", "dc_available_trade_dates", "dc_announcement_raw", "dc_announcement_event",
    "dc_industry_metric_definition", "dc_industry_metric_value", "dc_etf_pcf_header", "dc_etf_pcf_component",
    "dc_web_factor_snapshot", "dc_market_context_snapshot", "dc_asset_watermark", "dc_cold_archive_manifest",
)


def private_migration_manifest() -> dict[str, Any]:
    with _conn() as conn:
        tables = []
        for table in MIGRATION_TABLES:
            if conn._is_postgres:
                row = conn.execute("SELECT COALESCE(reltuples,0)::BIGINT n FROM pg_class WHERE relname=?", (table,)).fetchone()
            else:
                row = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()
            tables.append({"table": table, "estimated_rows": int(dict(row).get("n") or 0), "ownership": "private",
                           "migration": "pg_dump/pg_restore", "required": True})
    return {"ok": True, "engine": "postgresql", "portable": True, "source_independent_reads": True,
            "recommended": "同版本PostgreSQL使用pg_dump custom格式迁移；大表可并行pg_restore",
            "tables": tables, "table_count": len(tables),
            "cold_archive": {"format": "parquet", "compression": "zstd", "copy_with_database": True}}
