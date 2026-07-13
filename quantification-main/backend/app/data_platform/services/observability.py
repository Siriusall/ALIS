from __future__ import annotations

import json
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.runtime_logs import list_runtime_logs
from app.data_platform.redis_store import health as redis_health, stream_health


def _dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def get_observability(limit_logs: int = 50) -> dict[str, Any]:
    with _conn() as c:
        latest_row = c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone()
        latest_date = str(_dict(latest_row).get("value") or "")
        snapshot = c.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END) missing_close,
            SUM(CASE WHEN total_mv IS NULL THEN 1 ELSE 0 END) missing_total_mv,
            SUM(CASE WHEN circ_mv IS NULL THEN 1 ELSE 0 END) missing_circ_mv,
            SUM(CASE WHEN pe_ttm IS NULL THEN 1 ELSE 0 END) missing_pe_ttm,
            SUM(CASE WHEN pb IS NULL THEN 1 ELSE 0 END) missing_pb,
            SUM(CASE WHEN turnover_rate IS NULL THEN 1 ELSE 0 END) missing_turnover_rate,
            SUM(CASE WHEN data_quality_score >= 75 THEN 1 ELSE 0 END) quality_pass,
            AVG(data_quality_score) avg_quality
            FROM dc_stock_daily_snapshot WHERE trade_date=?""", (latest_date,),
        ).fetchone()
        quality_checks = c.execute(
            """SELECT check_name,scope,status,severity,message,affected_count,checked_at
            FROM dc_data_quality_checks ORDER BY id DESC LIMIT 30"""
        ).fetchall()
        catalog = c.execute(
            """SELECT api_name,category,display_name,recommended_frequency,last_probe_status,
            last_probe_rows,last_probe_error,last_probe_at FROM tushare_api_catalog
            ORDER BY category,api_name"""
        ).fetchall()
        logs = c.execute(
            """SELECT id,job_name,source_code,status,started_at,finished_at,rows_read,rows_written,error,meta_json
            FROM data_ingestion_runs ORDER BY id DESC LIMIT ?""", (limit_logs,),
        ).fetchall()
        source_stats = c.execute(
            """SELECT source_code,COUNT(*) total_runs,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) successful_runs,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed_runs,
            MAX(started_at) last_run_at
            FROM data_ingestion_runs GROUP BY source_code ORDER BY source_code"""
        ).fetchall()

    stats = _dict(snapshot)
    total = int(stats.get("total") or 0)
    fields = [
        ("close", "收盘价", "核心行情"), ("total_mv", "总市值", "估值"),
        ("circ_mv", "流通市值", "估值"), ("pe_ttm", "市盈率 TTM", "估值"),
        ("pb", "市净率", "估值"), ("turnover_rate", "换手率", "交易活跃度"),
    ]
    missing = []
    for key, label, category in fields:
        count = int(stats.get(f"missing_{key}") or 0)
        missing.append({
            "field": key, "label": label, "category": category, "total": total,
            "missing": count, "missing_rate": round(count / total, 6) if total else 0,
            "coverage_rate": round((total - count) / total, 6) if total else 0,
        })

    api_items = []
    for row in catalog:
        item = _dict(row)
        probe = str(item.get("last_probe_status") or "unknown")
        item["health"] = "healthy" if probe == "ok" else ("warning" if probe in {"empty", "unknown"} else "failed")
        api_items.append(item)

    log_items = []
    for row in logs:
        item = _dict(row)
        try:
            item["meta"] = json.loads(item.pop("meta_json") or "{}")
        except Exception:
            item["meta"] = {}
        log_items.append(item)

    quality_pass = int(stats.get("quality_pass") or 0)
    return {
        "ok": True,
        "latest_trade_date": latest_date,
        "quality": {
            "sample_rows": total,
            "quality_pass_rows": quality_pass,
            "quality_pass_rate": round(quality_pass / total, 6) if total else 0,
            "average_quality_score": round(float(stats.get("avg_quality") or 0) / 100, 6),
            "definition": "质量通过率基于完整性和规则校验，不等同于与外部真实值逐条核验的准确率。",
            "checks": [_dict(row) for row in quality_checks],
        },
        "missingness": missing,
        "interfaces": api_items,
        "interface_summary": {
            "total": len(api_items),
            "healthy": sum(1 for item in api_items if item["health"] == "healthy"),
            "warning": sum(1 for item in api_items if item["health"] == "warning"),
            "failed": sum(1 for item in api_items if item["health"] == "failed"),
        },
        "source_stats": [_dict(row) for row in source_stats],
        "logs": log_items,
        "runtime_logs": list_runtime_logs(limit_logs),
        "redis": redis_health(),
        "redis_stream": stream_health(),
    }
