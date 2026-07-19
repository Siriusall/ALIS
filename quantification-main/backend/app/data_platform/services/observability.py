from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.market_calendar import target_trade_date
from app.data_platform.services.runtime_logs import list_runtime_logs
from app.data_platform.redis_store import health as redis_health, stream_health


def _dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _value(row: Any, key: str, default: Any = 0) -> Any:
    data = _dict(row)
    value = data.get(key, default)
    return default if value is None else value


def _latest_usable_snapshot_date(c: Any) -> str:
    stock_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_stock_master").fetchone(), "n"))
    threshold = max(1, int(stock_count * 0.8)) if stock_count else 1
    row = c.execute(
        """
        SELECT trade_date value
        FROM dc_stock_daily_snapshot
        GROUP BY trade_date
        HAVING SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) >= ?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (threshold,),
    ).fetchone()
    usable = str(_value(row, "value", ""))
    if usable:
        return usable
    return str(_value(c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone(), "value", ""))


def _quality_issue(
    code: str,
    title: str,
    severity: str,
    count: int,
    message: str,
    *,
    sample: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    status = "ok" if count <= 0 else ("error" if severity == "error" else "warning" if severity == "warning" else "info")
    return {
        "code": code,
        "title": title,
        "severity": severity,
        "status": status,
        "count": int(count or 0),
        "message": message,
        "sample": sample or [],
    }


def _quality_audit(c: Any) -> dict[str, Any]:
    today_s = date.today().strftime("%Y%m%d")
    pending_total = int(_value(c.execute(
        "SELECT COUNT(*) n FROM dc_tushare_failed_requests WHERE status='pending'"
    ).fetchone(), "n"))
    pending_rows = [_dict(r) for r in c.execute(
        """
        SELECT api_name,error_category,COUNT(*) n,MAX(attempts) max_attempts
        FROM dc_tushare_failed_requests
        WHERE status='pending'
        GROUP BY api_name,error_category
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()]

    quarantine_total = int(_value(c.execute("SELECT COUNT(*) n FROM dc_data_quarantine").fetchone(), "n"))
    quarantine_rows = [_dict(r) for r in c.execute(
        """
        SELECT api_name,reason,COUNT(*) n
        FROM dc_data_quarantine
        GROUP BY api_name,reason
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()]

    deferred_total = int(_value(c.execute(
        "SELECT COUNT(*) n FROM dc_tushare_request_coverage WHERE result_status='deferred_empty'"
    ).fetchone(), "n"))
    deferred_rows = [_dict(r) for r in c.execute(
        """
        SELECT api_name,COUNT(*) n
        FROM dc_tushare_request_coverage
        WHERE result_status='deferred_empty'
        GROUP BY api_name
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()]

    empty_pending_total = int(_value(c.execute(
        "SELECT COUNT(*) n FROM dc_tushare_request_coverage WHERE result_status='empty_pending'"
    ).fetchone(), "n"))
    empty_pending_rows = [_dict(r) for r in c.execute(
        """
        SELECT api_name,COUNT(*) n
        FROM dc_tushare_request_coverage
        WHERE result_status='empty_pending'
        GROUP BY api_name
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()]

    daily_apis = ("daily", "daily_basic", "adj_factor", "moneyflow", "moneyflow_hsgt", "suspend_d")
    invalid_trade_total = int(_value(c.execute(
        """
        SELECT COUNT(*) n
        FROM dc_tushare_raw_records
        WHERE api_name IN (?,?,?,?,?,?)
          AND trade_date <> ''
          AND trade_date > ?
        """,
        (*daily_apis, today_s),
    ).fetchone(), "n"))
    invalid_trade_rows = [_dict(r) for r in c.execute(
        """
        SELECT api_name,trade_date,COUNT(*) n
        FROM dc_tushare_raw_records
        WHERE api_name IN (?,?,?,?,?,?)
          AND trade_date <> ''
          AND trade_date > ?
        GROUP BY api_name,trade_date
        ORDER BY n DESC
        LIMIT 20
        """,
        (*daily_apis, today_s),
    ).fetchall()]

    non_daily_date_rows = [_dict(r) for r in c.execute(
        """
        SELECT api_name,trade_date,COUNT(*) n
        FROM dc_tushare_raw_records
        WHERE api_name IN ('cn_cpi','cn_ppi','cn_gdp')
          AND trade_date <> ''
          AND LENGTH(trade_date) <> 8
        GROUP BY api_name,trade_date
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()]
    non_daily_date_total = len(non_daily_date_rows)

    future_financial_total = int(_value(c.execute(
        """
        SELECT COUNT(*) n
        FROM dc_tushare_raw_records
        WHERE api_name IN ('income','balancesheet','cashflow','fina_indicator','fina_audit','fina_mainbz')
          AND period <> ''
          AND period > ?
        """,
        (today_s,),
    ).fetchone(), "n"))
    future_financial_rows = [_dict(r) for r in c.execute(
        """
        SELECT api_name,period,COUNT(*) n
        FROM dc_tushare_raw_records
        WHERE api_name IN ('income','balancesheet','cashflow','fina_indicator','fina_audit','fina_mainbz')
          AND period <> ''
          AND period > ?
        GROUP BY api_name,period
        ORDER BY n DESC
        LIMIT 20
        """,
        (today_s,),
    ).fetchall()]

    raw_total = int(_value(c.execute(
        "SELECT COALESCE(reltuples,0)::bigint n FROM pg_class WHERE oid='dc_tushare_raw_records'::regclass"
    ).fetchone(), "n"))
    no_available_time_exists = c.execute(
        """
        SELECT 1
        FROM dc_tushare_raw_records
        WHERE available_time='' OR available_time IS NULL
        LIMIT 1
        """
    ).fetchone() is not None
    empty_key_exists = c.execute(
        """
        SELECT 1
        FROM dc_tushare_raw_records
        WHERE record_key='' OR api_name=''
        LIMIT 1
        """
    ).fetchone() is not None

    issues = [
        _quality_issue(
            "pending_failed_requests",
            "待处理接口失败",
            "warning",
            pending_total,
            "仍有请求在失败队列中；不一定阻断运行，但需要继续重试、降级或确认替代源。",
            sample=pending_rows,
        ),
        _quality_issue(
            "quarantined_records",
            "隔离数据",
            "warning",
            quarantine_total,
            "入库时发现字段缺失或未来日期等异常，已隔离，不能当作可用事实。",
            sample=quarantine_rows,
        ),
        _quality_issue(
            "empty_pending_coverage",
            "待复核空结果",
            "warning",
            empty_pending_total,
            "接口返回空但尚未达到终态，不能算作真实无数据。",
            sample=empty_pending_rows,
        ),
        _quality_issue(
            "deferred_empty_coverage",
            "长期空结果已后置",
            "info",
            deferred_total,
            "多次确认仍为空的请求已后置到备用源/异常报告，不再阻断主补全流程。",
            sample=deferred_rows,
        ),
        _quality_issue(
            "invalid_trade_date",
            "日频接口未来日期",
            "error",
            invalid_trade_total,
            "日频行情/估值/资金流/停复牌的 trade_date 不应晚于今天；格式类全表检查留给离线审计，避免拖慢监控页。",
            sample=invalid_trade_rows,
        ),
        _quality_issue(
            "non_daily_date_semantics",
            "非日频日期语义样本",
            "info",
            non_daily_date_total,
            "月度、季度、事件类接口可能使用 YYYYMM/季度等日期语义，不按日频错误处理；这里只展示样本，后续应拆成 period/month/event_date 等字段。",
            sample=non_daily_date_rows,
        ),
        _quality_issue(
            "future_financial_period",
            "财务报告期晚于今天",
            "error",
            future_financial_total,
            "财务类 period 不应晚于今天；若是到期日/事件日，应改用对应语义字段。",
            sample=future_financial_rows,
        ),
        _quality_issue(
            "raw_missing_available_time",
            "原始记录缺 available_time",
            "error",
            1 if no_available_time_exists else 0,
            "时点可用性字段缺失会影响回测和未来函数控制。",
        ),
        _quality_issue(
            "raw_key_integrity",
            "原始记录键异常",
            "error",
            1 if empty_key_exists else 0,
            "api_name/record_key 为空会破坏幂等写入；重复键由数据库唯一索引强制拦截。",
            sample=[{"empty_key_exists": empty_key_exists, "duplicate_guard": "unique(api_name, record_key)"}],
        ),
    ]
    error_count = sum(1 for x in issues if x["status"] == "error")
    warning_count = sum(1 for x in issues if x["status"] == "warning")
    info_count = sum(1 for x in issues if x["status"] == "info")
    return {
        "status": "error" if error_count else "warning" if warning_count else "ok",
        "summary": {
            "errors": error_count,
            "warnings": warning_count,
            "info": info_count,
            "raw_records": raw_total,
        },
        "issues": issues,
    }


def get_observability(limit_logs: int = 50) -> dict[str, Any]:
    with _conn() as c:
        latest_date = target_trade_date(c)
        stock_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_stock_master").fetchone(), "n"))
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
        quality_audit = _quality_audit(c)

    stats = _dict(snapshot)
    total = int(stats.get("total") or 0)
    missing_total = total if total else stock_count
    fields = [
        ("close", "收盘价", "核心行情"), ("total_mv", "总市值", "估值"),
        ("circ_mv", "流通市值", "估值"), ("pe_ttm", "市盈率 TTM", "估值"),
        ("pb", "市净率", "估值"), ("turnover_rate", "换手率", "交易活跃度"),
    ]
    missing = []
    for key, label, category in fields:
        count = int(stats.get(f"missing_{key}") or 0) if total else missing_total
        missing.append({
            "field": key, "label": label, "category": category, "total": missing_total,
            "missing": count, "missing_rate": round(count / missing_total, 6) if missing_total else 0,
            "coverage_rate": round((missing_total - count) / missing_total, 6) if missing_total else 0,
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
        "quality_audit": quality_audit,
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
