"""Persistent asset watermarks and private-deployment migration inventory."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.data_platform.db import _conn


CORE_ASSETS = {
    "stock_master", "trade_calendar", "daily", "valuation", "limit_price", "snapshot",
    "adj_factor", "suspension", "income", "balancesheet", "cashflow",
    "financial_indicator", "financial_research_fields", "announcements",
}
OPTIONAL_ASSETS = {"web_fallback", "shareholder_return", "namechange", "pledge_detail"}


def _tier(asset_key: str) -> str:
    if asset_key in CORE_ASSETS:
        return "core"
    if asset_key in OPTIONAL_ASSETS:
        return "optional"
    return "enhanced"


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
        rows.append((key, "overall", _tier(key), expected, available, max(0, expected - available),
                     "complete" if status in {"complete", "available"} else "repairable",
                     "", 10 if key in CORE_ASSETS else 50 if _tier(key) == "enhanced" else 90,
                     str(item.get("source") or ""), json.dumps({"coverage": item.get("coverage"), "window_start": item.get("window_start"),
                     "window_end": item.get("window_end"), "measurement": item.get("measurement")}, ensure_ascii=False), now, now))
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


def asset_watermark_status() -> dict[str, Any]:
    with _conn() as conn:
        rows = conn.execute("""SELECT asset_key,slice_key,contract_tier,expected_count,available_count,
            missing_count,status,terminal_reason,repair_priority,source_code,metadata_json,checked_at,updated_at
            FROM dc_asset_watermark ORDER BY repair_priority,asset_key,slice_key""").fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        items.append(item)
    return {"ok": True, "items": items, "summary": {
        "watermarks": len(items), "repairable": sum(x["status"] == "repairable" for x in items),
        "terminal": sum(x["status"] in {"upstream_unavailable", "not_applicable", "permission_denied"} for x in items),
        "missing_count": sum(int(x["missing_count"] or 0) for x in items if x["slice_key"] == "overall"),
    }}


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
