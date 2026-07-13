from __future__ import annotations

import json
import hashlib
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

from app.data_platform.db import _conn, _is_mysql
from app.data_platform.tushare_client import get_tushare_client, reset_tushare_client
from app.data_platform.services.runtime_logs import write_runtime_log
from app.data_platform.redis_store import set_add as redis_set_add, set_remove as redis_set_remove

SOURCE_TUSHARE = "tushare"
SOURCE_CNINFO = "cninfo"
SOURCE_MANUAL = "manual"

_tushare_activity_local = threading.local()


def set_tushare_activity_hook(hook: Callable[[dict[str, Any]], None] | None) -> None:
    """Attach a short-lived observer used by background repair status UI."""
    _tushare_activity_local.hook = hook


def _emit_tushare_activity(event: str, api_name: str, params: dict[str, Any], **extra: Any) -> None:
    if event in {"request", "retry", "failed", "saved"}:
        level = "ERROR" if event == "failed" else "WARNING" if event == "retry" else "INFO"
        write_runtime_log(level, "tushare", f"{event} {api_name}", {"params": params, **extra})
    hook = getattr(_tushare_activity_local, "hook", None)
    if hook is None:
        return
    hook({"event": event, "api": api_name, "params": params, "at": _now(), **extra})


def _parallel_tushare_map(items: list[Any], fn: Callable[[Any], Any], *, prefix: str) -> tuple[list[Any], int]:
    """Run bounded upstream calls concurrently while preserving order and UI activity."""
    workers = max(1, min(int(os.getenv("TUSHARE_CONCURRENCY", "3") or 3), 6, len(items) or 1))
    parent_hook = getattr(_tushare_activity_local, "hook", None)

    def wrapped(item: Any) -> Any:
        set_tushare_activity_hook(parent_hook)
        try:
            return fn(item)
        finally:
            set_tushare_activity_hook(None)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix) as pool:
        return list(pool.map(wrapped, items)), workers


@dataclass(frozen=True)
class TushareCatalogItem:
    api_name: str
    category: str
    display_name: str
    use_case: str
    recommended_frequency: str
    fields: str
    params: dict[str, Any]


TUSHARE_CATALOG: list[TushareCatalogItem] = [
    TushareCatalogItem("stock_basic", "基础", "股票基础信息", "股票主数据、行业初始分类、上市状态", "weekly", "ts_code,symbol,name,area,industry,market,list_date", {"exchange": "", "list_status": "L"}),
    TushareCatalogItem("trade_cal", "基础", "交易日历", "调度、回测、可用时间判断", "monthly", "exchange,cal_date,is_open,pretrade_date", {"exchange": "SSE"}),
    TushareCatalogItem("namechange", "基础", "曾用名", "ST/名称变更历史、治理风险辅助", "monthly", "ts_code,name,start_date,end_date,change_reason", {"ts_code": "000001.SZ"}),
    TushareCatalogItem("daily", "行情", "A股日线", "价格、成交量、技术面和回测基础", "daily", "ts_code,trade_date,open,high,low,close,vol,amount", {"trade_date": ""}),
    TushareCatalogItem("weekly", "行情", "周线", "长线趋势辅助", "weekly", "ts_code,trade_date,open,high,low,close,vol,amount", {"ts_code": ""}),
    TushareCatalogItem("monthly", "行情", "月线", "长线趋势辅助", "monthly", "ts_code,trade_date,open,high,low,close,vol,amount", {"ts_code": ""}),
    TushareCatalogItem("adj_factor", "行情", "复权因子", "历史价格复权和收益计算", "daily", "ts_code,trade_date,adj_factor", {"ts_code": ""}),
    TushareCatalogItem("daily_basic", "估值", "每日指标", "估值、市值、换手、量化快照", "daily", "ts_code,trade_date,close,turnover_rate,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,total_mv,circ_mv", {"trade_date": ""}),
    TushareCatalogItem("suspend_d", "交易状态", "停复牌", "交易约束、风险过滤", "daily", "ts_code,trade_date,suspend_timing,suspend_type", {"trade_date": ""}),
    TushareCatalogItem("stk_limit", "交易状态", "涨跌停价", "交易约束、涨跌停识别", "daily", "ts_code,trade_date,up_limit,down_limit", {"trade_date": ""}),
    TushareCatalogItem("moneyflow", "资金", "个股资金流", "资金确认项、短中期风险辅助", "daily", "ts_code,trade_date,buy_sm_vol,sell_sm_vol,buy_lg_vol,sell_lg_vol,net_mf_vol,net_mf_amount", {"ts_code": ""}),
    TushareCatalogItem("income", "财务", "利润表", "营收、利润、成长能力", "quarterly", "ts_code,ann_date,f_ann_date,end_date,report_type,total_revenue,n_income", {"ts_code": "", "period": ""}),
    TushareCatalogItem("balancesheet", "财务", "资产负债表", "资产质量、负债、安全性", "quarterly", "ts_code,ann_date,f_ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int", {"ts_code": "", "period": ""}),
    TushareCatalogItem("cashflow", "财务", "现金流量表", "经营现金流、自由现金流质量", "quarterly", "ts_code,ann_date,f_ann_date,end_date,n_cashflow_act,n_cash_flows_inv_act,n_cash_flows_fnc_act", {"ts_code": "", "period": ""}),
    TushareCatalogItem("fina_indicator", "财务", "财务指标", "ROE、毛利率、净利率、偿债指标", "quarterly", "ts_code,ann_date,end_date,roe,roa,grossprofit_margin,netprofit_margin,debt_to_assets,ocf_to_or", {"ts_code": "", "period": ""}),
    TushareCatalogItem("fina_audit", "财务", "审计意见", "非标审计预警", "yearly", "ts_code,ann_date,end_date,audit_result,audit_fees,audit_agency", {"ts_code": "", "period": ""}),
    TushareCatalogItem("fina_mainbz", "财务", "主营构成", "业务结构和行业暴露", "yearly", "ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost,curr_type", {"ts_code": "", "period": "", "type": "P"}),
    TushareCatalogItem("forecast", "业绩", "业绩预告", "预期变化和业绩风险", "daily_incremental", "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,summary", {"ann_date": ""}),
    TushareCatalogItem("express", "业绩", "业绩快报", "财报前业绩验证", "daily_incremental", "ts_code,ann_date,end_date,revenue,total_profit,n_income,total_assets", {"ts_code": "", "period": ""}),
    TushareCatalogItem("dividend", "分红", "分红送股", "股东回报、股息率验证", "quarterly", "ts_code,end_date,ann_date,div_proc,stk_div,cash_div_tax,record_date,ex_date,pay_date", {"ts_code": ""}),
    TushareCatalogItem("top10_holders", "股东", "十大股东", "机构/大股东结构", "quarterly", "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio", {"ts_code": "", "period": ""}),
    TushareCatalogItem("top10_floatholders", "股东", "十大流通股东", "流通股东结构", "quarterly", "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio", {"ts_code": "", "period": ""}),
    TushareCatalogItem("stk_holdernumber", "股东", "股东人数", "筹码集中度变化", "quarterly", "ts_code,ann_date,end_date,holder_num", {"ts_code": "", "enddate": ""}),
    TushareCatalogItem("pledge_stat", "质押", "股权质押统计", "治理风险和质押风险", "weekly", "ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,total_share,pledge_ratio", {"ts_code": ""}),
    TushareCatalogItem("pledge_detail", "质押", "股权质押明细", "质押风险明细兜底", "weekly", "ts_code,ann_date,holder_name,pledge_amount,start_date,end_date,is_release", {"ts_code": ""}),
    TushareCatalogItem("disclosure_date", "公告", "财报披露计划", "财报跟踪、下次验证时间", "weekly", "ts_code,ann_date,end_date,pre_date,actual_date,modify_date", {"ts_code": "", "end_date": ""}),
    TushareCatalogItem("index_basic", "指数", "指数基础信息", "行业/宽基指数主数据", "monthly", "ts_code,name,market,publisher,category,base_date,list_date", {"market": "SSE"}),
    TushareCatalogItem("index_daily", "指数", "指数日线", "市场/行业趋势与基准", "daily", "ts_code,trade_date,open,high,low,close,vol,amount", {"ts_code": ""}),
    TushareCatalogItem("index_weight", "指数", "指数权重", "沪深300/中证指数成分与权重", "monthly", "index_code,con_code,trade_date,weight", {"index_code": ""}),
    TushareCatalogItem("cn_gdp", "宏观", "国内生产总值", "经济周期与增长判断", "quarterly", "quarter,gdp,gdp_yoy,pi,pi_yoy,si,si_yoy,ti,ti_yoy", {}),
    TushareCatalogItem("cn_cpi", "宏观", "居民消费价格指数", "通胀与政策环境判断", "monthly", "month,nt_val,nt_yoy,nt_mom,nt_accu,town_val,town_yoy,cnt_val,cnt_yoy", {}),
    TushareCatalogItem("cn_pmi", "宏观", "采购经理指数", "制造业与非制造业景气判断", "monthly", "MONTH,PMI010000,PMI020100,PMI030000", {}),
    TushareCatalogItem("shibor", "宏观", "上海银行间同业拆放利率", "流动性和利率环境", "daily", "date,on,1w,2w,1m,3m,6m,9m,1y", {}),
    TushareCatalogItem("fx_daily", "宏观", "美元离岸人民币", "汇率与外围风险", "daily", "ts_code,trade_date,bid_open,bid_close,bid_high,bid_low,ask_open,ask_close,ask_high,ask_low", {"ts_code": "USDCNH.FXCM"}),
]


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _invalidate_asset_views() -> None:
    # Local import avoids the assets -> data_center planning dependency cycle.
    try:
        from app.data_platform.services.assets import invalidate_data_assets_cache
        invalidate_data_assets_cache()
    except Exception:
        pass


def classify_tushare_error(exc: Exception) -> str:
    """Classify upstream failures so retry and operator actions are predictable."""
    message = str(exc).lower()
    if any(x in message for x in ("token", "unauthorized", "401", "登录", "认证")):
        return "authentication"
    if any(x in message for x in ("权限", "permission", "forbidden", "403", "积分")):
        return "permission"
    if any(x in message for x in ("频率", "限流", "rate limit", "too many", "429")):
        return "rate_limit"
    if any(x in message for x in ("timeout", "timed out", "connection", "proxy", "502", "503", "504", "temporarily")):
        return "transient"
    if any(x in message for x in ("invalid", "参数", "parameter", "字段")):
        return "invalid_request"
    return "unknown"


def _request_key(api_name: str, params: dict[str, Any], limit_rows: int | None) -> str:
    raw = json.dumps({"api": api_name, "params": params, "limit_rows": limit_rows}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_failed_tushare_request(api_name: str, params: dict[str, Any], limit_rows: int | None, category: str, error: str) -> None:
    key, now = _request_key(api_name, params, limit_rows), _now()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO dc_tushare_failed_requests
                (request_key,api_name,params_json,limit_rows,error_category,last_error,status,attempts,first_failed_at,last_failed_at)
            VALUES (?,?,?,?,?,?,'pending',1,?,?)
            ON CONFLICT(request_key) DO UPDATE SET
                error_category=excluded.error_category,last_error=excluded.last_error,status='pending',
                attempts=dc_tushare_failed_requests.attempts+1,last_failed_at=excluded.last_failed_at
            """,
            (key, api_name, json.dumps(params, ensure_ascii=False, sort_keys=True), limit_rows, category, error[:1000], now, now),
        )
    redis_set_add("alis:data-platform:tushare:failed", key)


def _resolve_failed_tushare_request(api_name: str, params: dict[str, Any], limit_rows: int | None) -> None:
    key = _request_key(api_name, params, limit_rows)
    with _conn() as c:
        c.execute(
            "UPDATE dc_tushare_failed_requests SET status='resolved',resolved_at=? WHERE request_key=? AND status<>'resolved'",
            (_now(), key),
        )
    redis_set_remove("alis:data-platform:tushare:failed", key)


def _record_request_coverage(api_name: str, params: dict[str, Any], limit_rows: int | None, rows_read: int) -> None:
    key = _request_key(api_name, params, limit_rows)
    with _conn() as c:
        c.execute(
            """INSERT INTO dc_tushare_request_coverage
               (request_key,api_name,ts_code,trade_date,period,params_json,rows_read,checked_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(request_key) DO UPDATE SET rows_read=excluded.rows_read,checked_at=excluded.checked_at""",
            (key, api_name, str(params.get("ts_code") or ""), str(params.get("trade_date") or ""),
             str(params.get("period") or params.get("enddate") or ""), json.dumps(params, ensure_ascii=False, sort_keys=True), rows_read, _now()),
        )


def _recent_calendar_window(days_back: int = 30, days_forward: int = 60) -> tuple[str, str]:
    start = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
    end = (date.today() + timedelta(days=days_forward)).strftime("%Y%m%d")
    return start, end


def ensure_data_center_seeded() -> dict[str, Any]:
    now = _now()
    with _conn() as c:
        for values in (
            (SOURCE_TUSHARE, "TuShare Pro", "api", 10, "active", "A股行情、财务、股东、分红、指数等结构化数据主通道", now, now),
            (SOURCE_CNINFO, "巨潮资讯", "crawler", 20, "active", "A股公告元数据、公告正文和附件主通道", now, now),
            (SOURCE_MANUAL, "手工/公开行业指标", "manual", 30, "active", "行业景气指标定义和值的人工维护或脚本导入通道", now, now),
        ):
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO data_sources (code, name, source_type, priority, status, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        source_type = VALUES(source_type),
                        priority = VALUES(priority),
                        status = VALUES(status),
                        description = VALUES(description),
                        updated_at = VALUES(updated_at)
                    """,
                    values,
                )
            else:
                c.execute(
                    """
                    INSERT INTO data_sources (code, name, source_type, priority, status, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                        name = excluded.name,
                        source_type = excluded.source_type,
                        priority = excluded.priority,
                        status = excluded.status,
                        description = excluded.description,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
        for item in TUSHARE_CATALOG:
            params_json = json.dumps(item.params, ensure_ascii=False, sort_keys=True)
            values = (
                item.api_name,
                item.category,
                item.display_name,
                item.use_case,
                item.recommended_frequency,
                1,
                item.fields,
                params_json,
                now,
            )
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO tushare_api_catalog
                        (api_name, category, display_name, use_case, recommended_frequency, enabled, fields, params_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        category = VALUES(category),
                        display_name = VALUES(display_name),
                        use_case = VALUES(use_case),
                        recommended_frequency = VALUES(recommended_frequency),
                        enabled = VALUES(enabled),
                        fields = VALUES(fields),
                        params_json = VALUES(params_json),
                        updated_at = VALUES(updated_at)
                    """,
                    values,
                )
            else:
                c.execute(
                    """
                    INSERT INTO tushare_api_catalog
                        (api_name, category, display_name, use_case, recommended_frequency, enabled, fields, params_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(api_name) DO UPDATE SET
                        category = excluded.category,
                        display_name = excluded.display_name,
                        use_case = excluded.use_case,
                        recommended_frequency = excluded.recommended_frequency,
                        enabled = excluded.enabled,
                        fields = excluded.fields,
                        params_json = excluded.params_json,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
    return {"ok": True, "source": SOURCE_TUSHARE, "catalog_count": len(TUSHARE_CATALOG)}


def start_ingestion_run(job_name: str, meta: dict[str, Any] | None = None) -> int:
    now = _now()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO data_ingestion_runs (job_name, source_code, status, started_at, rows_read, rows_written, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_name, SOURCE_TUSHARE, "running", now, 0, 0, json.dumps(meta or {}, ensure_ascii=False)),
        )
        return c.last_insert_id()


def finish_ingestion_run(
    run_id: int,
    *,
    status: str,
    rows_read: int,
    rows_written: int,
    error: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    with _conn() as c:
        c.execute(
            """
            UPDATE data_ingestion_runs
            SET status = ?, finished_at = ?, rows_read = ?, rows_written = ?, error = ?, meta_json = ?
            WHERE id = ?
            """,
            (status, _now(), rows_read, rows_written, error[:1000], json.dumps(meta or {}, ensure_ascii=False), run_id),
        )


def _record_probe(api_name: str, status: str, rows: int, error: str = "") -> None:
    with _conn() as c:
        c.execute(
            """
            UPDATE tushare_api_catalog
            SET last_probe_status = ?, last_probe_rows = ?, last_probe_error = ?, last_probe_at = ?, updated_at = ?
            WHERE api_name = ?
            """,
            (status, rows, error[:1000], _now(), _now(), api_name),
        )


def sync_tushare_catalog_from_probe(probe_results: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_data_center_seeded()
    updated = 0
    for item in probe_results:
        api = str(item.get("api") or "").strip()
        if not api:
            continue
        ok = bool(item.get("ok"))
        rows = int(item.get("rows") or 0)
        status = "ok" if ok and rows > 0 else ("empty" if ok else "failed")
        _record_probe(api, status, rows, str(item.get("error") or ""))
        updated += 1
    return {"ok": True, "updated": updated}


def sync_stock_basic() -> dict[str, Any]:
    ensure_data_center_seeded()
    run_id = start_ingestion_run("tushare.stock_basic", {"api": "stock_basic"})
    try:
        client = get_tushare_client()
        df = client.stock_basic(list_status="L", fields="ts_code,symbol,name,area,industry,market,list_date")
        written = _upsert_stock_master(df)
        finish_ingestion_run(run_id, status="completed", rows_read=len(df), rows_written=written)
        _record_probe("stock_basic", "ok" if written else "empty", len(df))
        return {"ok": True, "run_id": run_id, "rows_read": len(df), "rows_written": written}
    except Exception as exc:  # noqa: BLE001
        finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc))
        _record_probe("stock_basic", "failed", 0, str(exc))
        raise


def _upsert_stock_master(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0
    now = _now()
    written = 0
    with _conn() as c:
        for _, row in df.iterrows():
            values = (
                str(row.get("ts_code") or "").strip(),
                str(row.get("symbol") or "").strip(),
                str(row.get("name") or "").strip(),
                str(row.get("area") or "").strip(),
                str(row.get("industry") or "").strip(),
                str(row.get("market") or "").strip(),
                str(row.get("list_date") or "").strip(),
                SOURCE_TUSHARE,
                now,
                now,
            )
            if not values[0]:
                continue
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO dc_stock_master
                        (ts_code, symbol, name, area, industry, market, list_date, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        symbol = VALUES(symbol),
                        name = VALUES(name),
                        area = VALUES(area),
                        industry = VALUES(industry),
                        market = VALUES(market),
                        list_date = VALUES(list_date),
                        source_code = VALUES(source_code),
                        available_time = VALUES(available_time),
                        updated_at = VALUES(updated_at)
                    """,
                    values,
                )
            else:
                c.execute(
                    """
                    INSERT INTO dc_stock_master
                        (ts_code, symbol, name, area, industry, market, list_date, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ts_code) DO UPDATE SET
                        symbol = excluded.symbol,
                        name = excluded.name,
                        area = excluded.area,
                        industry = excluded.industry,
                        market = excluded.market,
                        list_date = excluded.list_date,
                        source_code = excluded.source_code,
                        available_time = excluded.available_time,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
            written += 1
    return written


def sync_trade_calendar(start_date: str | None = None, end_date: str | None = None, exchange: str = "SSE") -> dict[str, Any]:
    ensure_data_center_seeded()
    if not start_date or not end_date:
        start_date, end_date = _recent_calendar_window()
    exchange = exchange or "SSE"
    run_id = start_ingestion_run("tushare.trade_cal", {"api": "trade_cal", "exchange": exchange, "start_date": start_date, "end_date": end_date})
    try:
        client = get_tushare_client()
        df = client.pro.trade_cal(
            exchange=exchange,
            start_date=start_date,
            end_date=end_date,
            fields="exchange,cal_date,is_open,pretrade_date",
        )
        df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        written = _upsert_trade_calendar(df)
        finish_ingestion_run(run_id, status="completed", rows_read=len(df), rows_written=written)
        _record_probe("trade_cal", "ok" if written else "empty", len(df))
        return {
            "ok": True,
            "run_id": run_id,
            "exchange": exchange,
            "start_date": start_date,
            "end_date": end_date,
            "rows_read": len(df),
            "rows_written": written,
        }
    except Exception as exc:  # noqa: BLE001
        finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc))
        _record_probe("trade_cal", "failed", 0, str(exc))
        raise


def _upsert_trade_calendar(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0
    now = _now()
    written = 0
    with _conn() as c:
        for _, row in df.iterrows():
            values = (
                str(row.get("exchange") or "").strip() or "SSE",
                str(row.get("cal_date") or "").strip(),
                int(row.get("is_open") or 0),
                str(row.get("pretrade_date") or "").strip(),
                SOURCE_TUSHARE,
                now,
                now,
            )
            if not values[1]:
                continue
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO dc_trade_calendar
                        (exchange, cal_date, is_open, pretrade_date, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        is_open = VALUES(is_open),
                        pretrade_date = VALUES(pretrade_date),
                        source_code = VALUES(source_code),
                        available_time = VALUES(available_time),
                        updated_at = VALUES(updated_at)
                    """,
                    values,
                )
            else:
                c.execute(
                    """
                    INSERT INTO dc_trade_calendar
                        (exchange, cal_date, is_open, pretrade_date, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(exchange, cal_date) DO UPDATE SET
                        is_open = excluded.is_open,
                        pretrade_date = excluded.pretrade_date,
                        source_code = excluded.source_code,
                        available_time = excluded.available_time,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
            written += 1
    return written


def get_data_center_status(limit_runs: int = 10) -> dict[str, Any]:
    ensure_data_center_seeded()
    from app.data_platform.services.web_fallback import ensure_web_fallback_source
    ensure_web_fallback_source()
    with _conn() as c:
        source_rows = c.execute("SELECT * FROM data_sources ORDER BY priority ASC, code ASC").fetchall()
        catalog_rows = c.execute(
            """
            SELECT api_name, category, display_name, recommended_frequency, enabled,
                   last_probe_status, last_probe_rows, last_probe_error, last_probe_at
            FROM tushare_api_catalog
            ORDER BY category ASC, api_name ASC
            """
        ).fetchall()
        stock_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_stock_master").fetchone()
        cal_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_trade_calendar").fetchone()
        raw_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_tushare_raw_records").fetchone()
        snapshot_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_stock_daily_snapshot").fetchone()
        quality_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_data_quality_checks").fetchone()
        announcement_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_announcement_raw").fetchone()
        event_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_announcement_event").fetchone()
        industry_metric_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_industry_metric_definition").fetchone()
        industry_value_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_industry_metric_value").fetchone()
        web_factor_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_web_factor_snapshot").fetchone()
        runs = c.execute(
            """
            SELECT id, job_name, source_code, status, started_at, finished_at,
                   rows_read, rows_written, error, meta_json
            FROM data_ingestion_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit_runs),),
        ).fetchall()
    return {
        "ok": True,
        "sources": [_row_to_dict(row) for row in source_rows],
        "catalog": [_row_to_dict(row) for row in catalog_rows],
        "coverage": {
            "stock_master_rows": _row_count(stock_count),
            "trade_calendar_rows": _row_count(cal_count),
            "raw_record_rows": _row_count(raw_count),
            "stock_daily_snapshot_rows": _row_count(snapshot_count),
            "data_quality_check_rows": _row_count(quality_count),
            "announcement_raw_rows": _row_count(announcement_count),
            "announcement_event_rows": _row_count(event_count),
            "industry_metric_definitions": _row_count(industry_metric_count),
            "industry_metric_values": _row_count(industry_value_count),
            "web_factor_rows": _row_count(web_factor_count),
        },
        "recent_runs": [_row_to_dict(row) for row in runs],
    }


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _row_count(row: Any) -> int:
    data = _row_to_dict(row)
    return int(data.get("cnt") or 0)


def _catalog_item(api_name: str) -> TushareCatalogItem:
    for item in TUSHARE_CATALOG:
        if item.api_name == api_name:
            return item
    raise ValueError(f"Unsupported TuShare API: {api_name}")


def _call_tushare_api(api_name: str, params: dict[str, Any] | None = None, fields: str | None = None) -> pd.DataFrame:
    item = _catalog_item(api_name)
    client = get_tushare_client()
    kwargs = {k: v for k, v in {**item.params, **(params or {})}.items() if v not in (None, "")}
    use_fields = fields or item.fields
    fn = getattr(client.pro, api_name, None)
    if callable(fn):
        value = fn(**kwargs, fields=use_fields)
    else:
        value = client.pro.query(api_name, **kwargs, fields=use_fields)
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def sync_tushare_api_raw(api_name: str, params: dict[str, Any] | None = None, *, limit_rows: int | None = None) -> dict[str, Any]:
    """Sync any catalogued TuShare API into dc_tushare_raw_records."""

    ensure_data_center_seeded()
    item = _catalog_item(api_name)
    run_id = start_ingestion_run(f"tushare.{api_name}", {"api": api_name, "params": params or {}, "limit_rows": limit_rows})
    request_params = params or {}
    try:
        retry_delays = (1.0, 3.0, 8.0)
        df: pd.DataFrame | None = None
        last_exc: Exception | None = None
        for attempt in range(len(retry_delays) + 1):
            try:
                _emit_tushare_activity("request", api_name, request_params, attempt=attempt + 1)
                df = _call_tushare_api(api_name, params=request_params, fields=item.fields)
                _emit_tushare_activity("response", api_name, request_params, attempt=attempt + 1, rows=len(df))
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                category = classify_tushare_error(exc)
                _emit_tushare_activity(
                    "retry" if category in {"authentication", "transient", "rate_limit"} else "failed",
                    api_name,
                    request_params,
                    attempt=attempt + 1,
                    category=category,
                    error=str(exc)[:300],
                )
                if category == "authentication" and attempt == 0:
                    reset_tushare_client()
                    continue
                if category not in {"transient", "rate_limit"} or attempt >= len(retry_delays):
                    raise
                delay = retry_delays[attempt] * (3 if category == "rate_limit" else 1)
                time.sleep(delay)
        if last_exc is not None:
            raise last_exc
        if df is None:
            df = pd.DataFrame()
        if limit_rows is not None and limit_rows > 0 and isinstance(df, pd.DataFrame):
            df = df.head(int(limit_rows))
        written = _upsert_raw_records(api_name, df)
        finish_ingestion_run(run_id, status="completed", rows_read=len(df), rows_written=written, meta={"api": api_name, "params": params or {}})
        _record_probe(api_name, "ok" if written else "empty", len(df))
        _record_request_coverage(api_name, request_params, limit_rows, len(df))
        _resolve_failed_tushare_request(api_name, request_params, limit_rows)
        if written:
            _invalidate_asset_views()
        _emit_tushare_activity("saved", api_name, request_params, rows=len(df), written=written)
        return {"ok": True, "api": api_name, "run_id": run_id, "rows_read": len(df), "rows_written": written}
    except Exception as exc:  # noqa: BLE001
        category = classify_tushare_error(exc)
        finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc), meta={"api": api_name, "params": params or {}})
        _record_probe(api_name, "failed", 0, str(exc))
        _record_failed_tushare_request(api_name, request_params, limit_rows, category, str(exc))
        raise


def list_failed_tushare_requests(*, status: str = "pending", limit: int = 100) -> dict[str, Any]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id,api_name,params_json,limit_rows,error_category,last_error,status,attempts,
                      first_failed_at,last_failed_at,resumed_at,resolved_at
               FROM dc_tushare_failed_requests WHERE status=? ORDER BY last_failed_at ASC LIMIT ?""",
            (status, max(1, min(limit, 500))),
        ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["params"] = json.loads(item.pop("params_json") or "{}")
    return {"ok": True, "status": status, "total": len(items), "items": items}


def resume_failed_tushare_requests(*, limit: int = 20) -> dict[str, Any]:
    pending = list_failed_tushare_requests(status="pending", limit=limit)["items"]
    results = []
    for item in pending:
        with _conn() as c:
            c.execute("UPDATE dc_tushare_failed_requests SET status='resuming',resumed_at=? WHERE id=?", (_now(), item["id"]))
        try:
            result = sync_tushare_api_raw(item["api_name"], params=item["params"], limit_rows=item["limit_rows"])
            results.append({"id": item["id"], "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            results.append({"id": item["id"], "ok": False, "category": classify_tushare_error(exc), "error": str(exc)[:500]})
    return {"ok": all(x["ok"] for x in results), "attempted": len(results), "succeeded": sum(1 for x in results if x["ok"]), "results": results}


def _upsert_raw_records(api_name: str, df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0
    now = _now()
    written = 0
    with _conn() as c:
        for idx, row in df.iterrows():
            payload = _clean_payload(row.to_dict())
            ts_code = str(payload.get("ts_code") or payload.get("con_code") or "").strip()
            trade_date = str(payload.get("trade_date") or payload.get("date") or payload.get("month") or payload.get("MONTH") or payload.get("quarter") or "").strip()
            period = str(payload.get("period") or payload.get("end_date") or "").strip()
            ann_date = str(payload.get("ann_date") or "").strip()
            record_key = _record_key(api_name, payload, idx)
            values = (
                api_name,
                record_key,
                ts_code,
                trade_date,
                period,
                ann_date,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                SOURCE_TUSHARE,
                now,
                now,
            )
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO dc_tushare_raw_records
                        (api_name, record_key, ts_code, trade_date, period, ann_date, payload_json, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        ts_code = VALUES(ts_code),
                        trade_date = VALUES(trade_date),
                        period = VALUES(period),
                        ann_date = VALUES(ann_date),
                        payload_json = VALUES(payload_json),
                        source_code = VALUES(source_code),
                        available_time = VALUES(available_time),
                        updated_at = VALUES(updated_at)
                    """,
                    values,
                )
            else:
                c.execute(
                    """
                    INSERT INTO dc_tushare_raw_records
                        (api_name, record_key, ts_code, trade_date, period, ann_date, payload_json, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(api_name, record_key) DO UPDATE SET
                        ts_code = excluded.ts_code,
                        trade_date = excluded.trade_date,
                        period = excluded.period,
                        ann_date = excluded.ann_date,
                        payload_json = excluded.payload_json,
                        source_code = excluded.source_code,
                        available_time = excluded.available_time,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
            written += 1
    return written


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if pd.isna(value):
            clean[str(key)] = None
        elif hasattr(value, "item"):
            try:
                clean[str(key)] = value.item()
            except Exception:
                clean[str(key)] = str(value)
        else:
            clean[str(key)] = value
    return clean


def _record_key(api_name: str, payload: dict[str, Any], idx: Any) -> str:
    preferred = [
        "ts_code",
        "con_code",
        "index_code",
        "trade_date",
        "ann_date",
        "end_date",
        "period",
        "holder_name",
        "bz_item",
        "report_type",
        "comp_type",
        "update_flag",
        "div_proc",
        "pledge_amount",
        "cal_date",
        "exchange",
        "name",
    ]
    parts = [str(payload.get(k) or "").strip() for k in preferred if str(payload.get(k) or "").strip()]
    if not parts:
        parts = [str(idx)]
    return "|".join(parts)[:240]


def sync_market_bundle(trade_date: str, *, include_suspend: bool = False, apis: list[str] | None = None) -> dict[str, Any]:
    """Sync daily market/valuation/trading-status bundle for one trade date."""

    requested_apis = list(apis or ["daily", "daily_basic", "stk_limit"])
    if include_suspend and "suspend_d" not in requested_apis:
        requested_apis.append("suspend_d")
    allowed = {"daily", "daily_basic", "stk_limit", "suspend_d"}
    requested_apis = [api for api in requested_apis if api in allowed]
    results = []
    for api in requested_apis:
        results.append(_safe_sync_raw(api, {"trade_date": trade_date}))
    snapshot = build_stock_daily_snapshot(trade_date)
    return {"ok": all(bool(x.get("ok")) for x in results), "trade_date": trade_date, "apis": requested_apis, "results": results, "snapshot": snapshot}


def sync_financial_bundle(ts_code: str, period: str, *, include_yearly: bool = True) -> dict[str, Any]:
    """Sync financial statement/indicator bundle for one stock and report period."""

    apis = ["income", "balancesheet", "cashflow", "fina_indicator"]
    if include_yearly:
        apis.extend(["fina_audit", "fina_mainbz"])
    with _conn() as c:
        existing = {
            str(dict(row).get("api_name") or "")
            for row in c.execute(
                "SELECT DISTINCT api_name FROM dc_tushare_raw_records WHERE ts_code=? AND period=?",
                (ts_code, period),
            ).fetchall()
        }
    apis = [api for api in apis if api not in existing]
    results = []
    for api in apis:
        params = {"ts_code": ts_code, "period": period}
        if api == "fina_mainbz":
            params["type"] = "P"
        results.append(_safe_sync_raw(api, params))
    return {"ok": all(bool(x.get("ok")) for x in results), "ts_code": ts_code, "period": period, "skipped_existing": sorted(existing), "results": results}


def sync_financial_period_bundle(period: str, *, include_yearly: bool = False) -> dict[str, Any]:
    """Sync financial bundle for all stocks in one report period.

    This is the preferred full-load path. It avoids 5000+ per-stock calls and
    keeps TuShare usage closer to one call per API per report period.
    """

    apis = ["income", "balancesheet", "cashflow", "fina_indicator"]
    if include_yearly:
        apis.extend(["fina_audit", "fina_mainbz"])
    results = []
    for api in apis:
        params: dict[str, Any] = {"period": period}
        if api == "fina_mainbz":
            params["type"] = "P"
        results.append(_safe_sync_raw(api, params))
    return {"ok": True, "period": period, "results": results}


def sync_financial_batch(
    period: str,
    *,
    codes: list[str] | None = None,
    limit: int = 20,
    include_yearly: bool = False,
    allow_partial: bool = False,
    completion_api: str = "core",
) -> dict[str, Any]:
    """Sync financial data by stock in resumable batches.

    The current TuShare proxy requires ts_code for statement APIs, so this is
    the safe path for watchlists and staged full loads.
    """

    ensure_data_center_seeded()
    target_codes = [c.strip() for c in (codes or []) if c.strip()]
    if not target_codes:
        # A batch must advance across repeated runs.  Selecting the first N
        # symbols every time only rewrites the same stocks forever.
        target_codes = _load_financial_missing_codes(period, limit=limit, completion_api=completion_api)
    if limit and limit > 0:
        target_codes = target_codes[:limit]
    started = _now()
    results = []
    ok_count = 0
    failed_count = 0
    errors: list[str] = []
    def fetch_one(ts_code: str) -> dict[str, Any]:
        try:
            return sync_financial_bundle(ts_code, period, include_yearly=include_yearly)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:500]
            return {"ok": False, "ts_code": ts_code, "period": period, "error": msg, "results": []}

    results, workers = _parallel_tushare_map(target_codes, fetch_one, prefix="tushare-financial")
    for ts_code, result in zip(target_codes, results):
        result_ok = bool(result.get("ok", True)) and all(bool(item.get("ok")) for item in result.get("results", []))
        if result_ok:
            ok_count += 1
        else:
            failed_count += 1
            if result.get("error"):
                errors.append(f"{ts_code}: {result['error']}")
            for item in result.get("results") or []:
                if not item.get("ok"):
                    errors.append(f"{ts_code}/{item.get('api')}: {item.get('error') or 'failed'}")
    total = len(target_codes)
    soft_ok = allow_partial and ok_count > 0 and failed_count <= max(1, total // 5)
    return {
        "ok": failed_count == 0 or soft_ok,
        "period": period,
        "started_at": started,
        "finished_at": _now(),
        "total": total,
        "success": ok_count,
        "failed": failed_count,
        "concurrency": workers,
        "error": "; ".join(errors[:5])[:1000] if errors else "",
        "results": results,
    }


def _load_stock_codes(limit: int = 20) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ts_code FROM dc_stock_master ORDER BY ts_code ASC LIMIT ?",
            (max(1, int(limit or 20)),),
        ).fetchall()
    return [str(_row_to_dict(row).get("ts_code") or "") for row in rows]


def _load_financial_missing_codes(period: str, limit: int = 20, completion_api: str = "core") -> list[str]:
    """Return the next symbols whose completion API is absent for period."""
    allowed_apis = {"core", "income", "fina_audit"}
    if completion_api not in allowed_apis:
        raise ValueError(f"unsupported financial completion API: {completion_api}")
    required = ["income", "balancesheet", "cashflow", "fina_indicator"] if completion_api == "core" else [completion_api]
    placeholders = ",".join("?" for _ in required)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT s.ts_code
            FROM dc_stock_master s
            LEFT JOIN dc_tushare_raw_records r
              ON r.ts_code=s.ts_code AND r.period=? AND r.api_name IN ({placeholders})
            GROUP BY s.ts_code
            HAVING COUNT(DISTINCT r.api_name) < ?
            ORDER BY s.ts_code ASC
            LIMIT ?
            """,
            (period, *required, len(required), max(1, int(limit or 20))),
        ).fetchall()
    return [str(_row_to_dict(row).get("ts_code") or "") for row in rows]


def sync_tushare_stock_batch(
    api_name: str,
    *,
    period: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Progress a per-stock API without repeating already checked requests."""
    _catalog_item(api_name)
    with _conn() as c:
        rows = c.execute(
            """SELECT s.ts_code FROM dc_stock_master s
               WHERE NOT EXISTS (
                 SELECT 1 FROM dc_tushare_request_coverage q
                 WHERE q.api_name=? AND q.ts_code=s.ts_code AND q.period=?
               )
               ORDER BY s.ts_code LIMIT ?""",
            (api_name, period, max(1, min(int(limit), 500))),
        ).fetchall()
    codes = [str(dict(row).get("ts_code") or "") for row in rows]
    def fetch_one(ts_code: str) -> dict[str, Any]:
        params: dict[str, Any] = {"ts_code": ts_code}
        if period:
            params["enddate" if api_name == "stk_holdernumber" else "period"] = period
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return _safe_sync_raw(api_name, params)
    results, workers = _parallel_tushare_map(codes, fetch_one, prefix=f"tushare-{api_name}")
    return {
        "ok": all(bool(x.get("ok")) for x in results), "api": api_name, "period": period,
        "total": len(results), "success": sum(1 for x in results if x.get("ok")),
        "failed": sum(1 for x in results if not x.get("ok")), "concurrency": workers, "results": results,
    }


def sync_tushare_date_batch(api_name: str, *, trade_dates: list[str], limit: int = 10) -> dict[str, Any]:
    """Progress a date-based API, treating successful empty responses as checked."""
    _catalog_item(api_name)
    wanted = list(dict.fromkeys(str(x) for x in trade_dates if str(x)))
    with _conn() as c:
        checked = {str(dict(row).get("trade_date") or "") for row in c.execute(
            f"SELECT trade_date FROM dc_tushare_request_coverage WHERE api_name=? AND trade_date IN ({','.join('?' for _ in wanted)})",
            (api_name, *wanted),
        ).fetchall()} if wanted else set()
    pending = [d for d in wanted if d not in checked][:max(1, min(int(limit), 60))]
    results, workers = _parallel_tushare_map(
        pending, lambda d: _safe_sync_raw(api_name, {"trade_date": d}), prefix=f"tushare-{api_name}"
    )
    return {
        "ok": all(bool(x.get("ok")) for x in results), "api": api_name,
        "total": len(results), "success": sum(1 for x in results if x.get("ok")),
        "failed": sum(1 for x in results if not x.get("ok")), "concurrency": workers, "results": results,
    }


def sync_governance_bundle(ts_code: str, period: str | None = None) -> dict[str, Any]:
    """Sync shareholder/dividend/pledge/disclosure bundle for one stock."""

    results = []
    params_with_period = {"ts_code": ts_code}
    if period:
        params_with_period["period"] = period
    for api in ("top10_holders", "top10_floatholders"):
        results.append(_safe_sync_raw(api, params_with_period))
    holder_params = {"ts_code": ts_code}
    if period:
        holder_params["enddate"] = period
    results.append(_safe_sync_raw("stk_holdernumber", holder_params))
    for api in ("dividend", "pledge_stat", "pledge_detail", "disclosure_date"):
        params = {"ts_code": ts_code}
        if api == "disclosure_date" and period:
            params["end_date"] = period
        results.append(_safe_sync_raw(api, params))
    return {"ok": True, "ts_code": ts_code, "period": period, "results": results}


def sync_expectation_bundle(ts_code: str, period: str | None = None) -> dict[str, Any]:
    """Sync forecast/express data for one stock."""

    results = [_safe_sync_raw("forecast", {"ts_code": ts_code})]
    if period:
        results.append(_safe_sync_raw("express", {"ts_code": ts_code, "period": period}))
    return {"ok": True, "ts_code": ts_code, "period": period, "results": results}


def sync_index_bundle(index_code: str = "000300.SH", start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    """Sync index metadata, daily bars, and weights."""

    if not start_date or not end_date:
        start_date, end_date = _recent_calendar_window(days_back=30, days_forward=0)
    results = [
        _safe_sync_raw("index_basic", {"market": "SSE"}),
        _safe_sync_raw("index_daily", {"ts_code": index_code, "start_date": start_date, "end_date": end_date}),
        _safe_sync_raw("index_weight", {"index_code": index_code, "start_date": start_date, "end_date": end_date}),
    ]
    return {"ok": all(bool(x.get("ok")) for x in results), "index_code": index_code, "start_date": start_date, "end_date": end_date, "results": results}


def _safe_sync_raw(api_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return sync_tushare_api_raw(api_name, params)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "api": api_name,
            "params": params or {},
            "rows_read": 0,
            "rows_written": 0,
            "error": str(exc)[:500],
        }


def build_stock_daily_snapshot(trade_date: str) -> dict[str, Any]:
    """Build one-day stock snapshot from raw daily_basic, daily, stk_limit and suspend_d rows."""

    run_id = start_ingestion_run("data_center.stock_daily_snapshot", {"trade_date": trade_date})
    try:
        with _conn() as c:
            stocks = {
                row["ts_code"] if not isinstance(row, dict) else row["ts_code"]: _row_to_dict(row)
                for row in c.execute("SELECT ts_code, symbol, name, industry FROM dc_stock_master").fetchall()
            }
            daily_basic = _raw_payload_map(c, "daily_basic", trade_date, "ts_code")
            daily = _raw_payload_map(c, "daily", trade_date, "ts_code")
            limits = _raw_payload_map(c, "stk_limit", trade_date, "ts_code")
            suspended = set(_raw_payload_map(c, "suspend_d", trade_date, "ts_code").keys())
        now = _now()
        written = 0
        stock_codes = set(stocks.keys())
        all_codes = sorted((set(daily_basic) | set(daily) | set(limits) | suspended) & stock_codes)
        with _conn() as c:
            c.execute("DELETE FROM dc_stock_daily_snapshot WHERE trade_date = ?", (trade_date,))
            for ts_code in all_codes:
                info = stocks.get(ts_code, {})
                basic = daily_basic.get(ts_code, {})
                price = daily.get(ts_code, {})
                limit = limits.get(ts_code, {})
                flags = []
                close = _float_or_none(basic.get("close", price.get("close")))
                if close is None:
                    flags.append("missing_close")
                if ts_code not in daily_basic:
                    flags.append("missing_daily_basic")
                if ts_code not in daily:
                    flags.append("missing_daily")
                if ts_code in suspended:
                    flags.append("suspended")
                score = max(0, 100 - len([f for f in flags if f != "suspended"]) * 25)
                values = (
                    ts_code,
                    trade_date,
                    str(info.get("symbol") or ts_code[:6]),
                    str(info.get("name") or ""),
                    str(info.get("industry") or ""),
                    close,
                    _float_or_none(basic.get("total_mv")),
                    _float_or_none(basic.get("circ_mv")),
                    _float_or_none(basic.get("pe_ttm")),
                    _float_or_none(basic.get("pb")),
                    _float_or_none(basic.get("turnover_rate")),
                    _float_or_none(limit.get("up_limit")),
                    _float_or_none(limit.get("down_limit")),
                    1 if ts_code in suspended else 0,
                    float(score),
                    json.dumps(flags, ensure_ascii=False),
                    SOURCE_TUSHARE,
                    now,
                    now,
                )
                _upsert_snapshot_row(c, values)
                written += 1
        finish_ingestion_run(run_id, status="completed", rows_read=len(all_codes), rows_written=written, meta={"trade_date": trade_date})
        quality = run_data_quality_checks(trade_date=trade_date)
        return {"ok": True, "run_id": run_id, "trade_date": trade_date, "rows_written": written, "quality": quality}
    except Exception as exc:  # noqa: BLE001
        finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc), meta={"trade_date": trade_date})
        raise


def _raw_payload_map(c: Any, api_name: str, trade_date: str, key: str) -> dict[str, dict[str, Any]]:
    rows = c.execute(
        "SELECT payload_json FROM dc_tushare_raw_records WHERE api_name = ? AND trade_date = ?",
        (api_name, trade_date),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(_row_to_dict(row).get("payload_json") or "{}")
        k = str(payload.get(key) or "").strip()
        if k:
            out[k] = payload
    return out


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _upsert_snapshot_row(c: Any, values: tuple[Any, ...]) -> None:
    if c._is_mysql:
        c.execute(
            """
            INSERT INTO dc_stock_daily_snapshot
                (ts_code, trade_date, symbol, name, industry, close, total_mv, circ_mv, pe_ttm, pb,
                 turnover_rate, up_limit, down_limit, is_suspended, data_quality_score, quality_flags_json,
                 source_code, available_time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                symbol = VALUES(symbol), name = VALUES(name), industry = VALUES(industry),
                close = VALUES(close), total_mv = VALUES(total_mv), circ_mv = VALUES(circ_mv),
                pe_ttm = VALUES(pe_ttm), pb = VALUES(pb), turnover_rate = VALUES(turnover_rate),
                up_limit = VALUES(up_limit), down_limit = VALUES(down_limit), is_suspended = VALUES(is_suspended),
                data_quality_score = VALUES(data_quality_score), quality_flags_json = VALUES(quality_flags_json),
                source_code = VALUES(source_code), available_time = VALUES(available_time), updated_at = VALUES(updated_at)
            """,
            values,
        )
    else:
        c.execute(
            """
            INSERT INTO dc_stock_daily_snapshot
                (ts_code, trade_date, symbol, name, industry, close, total_mv, circ_mv, pe_ttm, pb,
                 turnover_rate, up_limit, down_limit, is_suspended, data_quality_score, quality_flags_json,
                 source_code, available_time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts_code, trade_date) DO UPDATE SET
                symbol = excluded.symbol,
                name = excluded.name,
                industry = excluded.industry,
                close = excluded.close,
                total_mv = excluded.total_mv,
                circ_mv = excluded.circ_mv,
                pe_ttm = excluded.pe_ttm,
                pb = excluded.pb,
                turnover_rate = excluded.turnover_rate,
                up_limit = excluded.up_limit,
                down_limit = excluded.down_limit,
                is_suspended = excluded.is_suspended,
                data_quality_score = excluded.data_quality_score,
                quality_flags_json = excluded.quality_flags_json,
                source_code = excluded.source_code,
                available_time = excluded.available_time,
                updated_at = excluded.updated_at
            """,
            values,
        )


def run_data_quality_checks(trade_date: str | None = None) -> dict[str, Any]:
    """Run basic data-center quality checks and persist results."""

    checks: list[dict[str, Any]] = []
    with _conn() as c:
        stock_count = _row_count(c.execute("SELECT COUNT(*) AS cnt FROM dc_stock_master").fetchone())
        checks.append(_quality_item("stock_master_coverage", "stock_master", "ok" if stock_count >= 5000 else "warning", "high", f"stock master rows={stock_count}", stock_count))
        if trade_date:
            snap_count = _row_count(c.execute("SELECT COUNT(*) AS cnt FROM dc_stock_daily_snapshot WHERE trade_date = ?", (trade_date,)).fetchone())
            bad_count = _row_count(c.execute("SELECT COUNT(*) AS cnt FROM dc_stock_daily_snapshot WHERE trade_date = ? AND data_quality_score < 75", (trade_date,)).fetchone())
            checks.append(_quality_item("snapshot_coverage", f"snapshot:{trade_date}", "ok" if snap_count >= 5000 else "warning", "high", f"snapshot rows={snap_count}", snap_count))
            checks.append(_quality_item("snapshot_low_quality", f"snapshot:{trade_date}", "ok" if bad_count == 0 else "warning", "medium", f"low quality rows={bad_count}", bad_count))
        now = _now()
        for item in checks:
            c.execute(
                """
                INSERT INTO dc_data_quality_checks
                    (check_name, scope, status, severity, message, affected_count, checked_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["check_name"],
                    item["scope"],
                    item["status"],
                    item["severity"],
                    item["message"],
                    item["affected_count"],
                    now,
                    json.dumps(item.get("meta") or {}, ensure_ascii=False),
                ),
            )
    return {"ok": True, "checks": checks}


def _quality_item(check_name: str, scope: str, status: str, severity: str, message: str, affected_count: int) -> dict[str, Any]:
    return {
        "check_name": check_name,
        "scope": scope,
        "status": status,
        "severity": severity,
        "message": message,
        "affected_count": affected_count,
    }


def sync_cninfo_announcements(
    *,
    start_date: str,
    end_date: str,
    ts_code: str | None = None,
    page_size: int = 30,
    max_pages: int = 1,
) -> dict[str, Any]:
    """Sync announcement metadata from CNINFO and create coarse rule-based events."""

    ensure_data_center_seeded()
    page_size = max(1, min(int(page_size), 50))
    max_pages = max(1, min(int(max_pages), 20))
    run_id = start_ingestion_run(
        "cninfo.announcements",
        {"start_date": start_date, "end_date": end_date, "ts_code": ts_code or "", "page_size": page_size, "max_pages": max_pages},
    )
    try:
        rows: list[dict[str, Any]] = []
        for page_num in range(1, max_pages + 1):
            page_rows = _fetch_cninfo_announcement_page(
                start_date=start_date,
                end_date=end_date,
                ts_code=ts_code,
                page_num=page_num,
                page_size=page_size,
            )
            if not page_rows:
                break
            rows.extend(page_rows)
            if len(page_rows) < page_size:
                break
        written, events = _upsert_announcements(rows)
        finish_ingestion_run(
            run_id,
            status="completed",
            rows_read=len(rows),
            rows_written=written,
            meta={"events_created": events, "start_date": start_date, "end_date": end_date, "ts_code": ts_code or ""},
        )
        return {"ok": True, "run_id": run_id, "rows_read": len(rows), "rows_written": written, "events_created": events}
    except Exception as exc:  # noqa: BLE001
        finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc))
        raise


def _fetch_cninfo_announcement_page(
    *,
    start_date: str,
    end_date: str,
    ts_code: str | None,
    page_num: int,
    page_size: int,
) -> list[dict[str, Any]]:
    url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    se_date = f"{_fmt_cninfo_date(start_date)}~{_fmt_cninfo_date(end_date)}"
    payload = {
        "pageNum": page_num,
        "pageSize": page_size,
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": _cninfo_stock_param(ts_code),
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": se_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        "Accept": "application/json, text/plain, */*",
    }
    resp = requests.post(url, data=payload, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    announcements = data.get("announcements") or []
    out: list[dict[str, Any]] = []
    for item in announcements:
        sec_code = str(item.get("secCode") or "").strip()
        sec_name = str(item.get("secName") or "").strip()
        announcement_id = str(item.get("announcementId") or item.get("announcementTime") or item.get("adjunctUrl") or "").strip()
        title = _strip_html(str(item.get("announcementTitle") or ""))
        adjunct_url = str(item.get("adjunctUrl") or "").strip()
        url_full = f"http://static.cninfo.com.cn/{adjunct_url}" if adjunct_url and not adjunct_url.startswith("http") else adjunct_url
        publish_time = _cninfo_time_to_iso(item.get("announcementTime"))
        out.append(
            {
                "announcement_id": announcement_id or _hash_text(f"{sec_code}|{title}|{publish_time}|{url_full}"),
                "ts_code": _guess_ts_code(sec_code),
                "sec_name": sec_name,
                "title": title,
                "publish_time": publish_time,
                "url": url_full,
                "raw_text": "",
                "content_hash": _hash_text(f"{sec_code}|{title}|{publish_time}|{url_full}"),
            }
        )
    return out


def _fmt_cninfo_date(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _cninfo_stock_param(ts_code: str | None) -> str:
    if not ts_code:
        return ""
    symbol = ts_code.split(".")[0]
    suffix = ts_code.split(".")[1].lower() if "." in ts_code else ("sh" if symbol.startswith(("5", "6", "9")) else "sz")
    return f"{symbol},{suffix}{symbol}"


def _guess_ts_code(symbol: str) -> str:
    s = str(symbol or "").strip()
    if not s:
        return ""
    suffix = "SH" if s.startswith(("5", "6", "9")) else "SZ"
    return f"{s}.{suffix}"


def _cninfo_time_to_iso(value: Any) -> str:
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:
        return str(value or "")


def _strip_html(text: str) -> str:
    return text.replace("<em>", "").replace("</em>", "").replace("&nbsp;", " ").strip()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _upsert_announcements(rows: list[dict[str, Any]]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    now = _now()
    written = 0
    events = 0
    with _conn() as c:
        for row in rows:
            values = (
                SOURCE_CNINFO,
                str(row.get("announcement_id") or ""),
                str(row.get("ts_code") or ""),
                str(row.get("title") or ""),
                str(row.get("publish_time") or ""),
                str(row.get("url") or ""),
                str(row.get("raw_text") or ""),
                "",
                str(row.get("content_hash") or ""),
                now,
                now,
                now,
            )
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO dc_announcement_raw
                        (source_code, announcement_id, ts_code, title, publish_time, url, raw_text, file_path,
                         content_hash, available_time, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        ts_code = VALUES(ts_code), title = VALUES(title), publish_time = VALUES(publish_time),
                        url = VALUES(url), raw_text = VALUES(raw_text), file_path = VALUES(file_path),
                        content_hash = VALUES(content_hash), available_time = VALUES(available_time),
                        updated_at = VALUES(updated_at)
                    """,
                    values,
                )
                ann_row = c.execute(
                    "SELECT id FROM dc_announcement_raw WHERE source_code = ? AND announcement_id = ?",
                    (SOURCE_CNINFO, values[1]),
                ).fetchone()
            else:
                c.execute(
                    """
                    INSERT INTO dc_announcement_raw
                        (source_code, announcement_id, ts_code, title, publish_time, url, raw_text, file_path,
                         content_hash, available_time, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_code, announcement_id) DO UPDATE SET
                        ts_code = excluded.ts_code,
                        title = excluded.title,
                        publish_time = excluded.publish_time,
                        url = excluded.url,
                        raw_text = excluded.raw_text,
                        file_path = excluded.file_path,
                        content_hash = excluded.content_hash,
                        available_time = excluded.available_time,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
                ann_row = c.execute(
                    "SELECT id FROM dc_announcement_raw WHERE source_code = ? AND announcement_id = ?",
                    (SOURCE_CNINFO, values[1]),
                ).fetchone()
            written += 1
            ann_id = int(_row_to_dict(ann_row).get("id") or 0)
            event = classify_announcement_event(str(row.get("title") or ""), str(row.get("publish_time") or ""))
            if ann_id and event:
                c.execute("DELETE FROM dc_announcement_event WHERE announcement_id = ?", (ann_id,))
                c.execute(
                    """
                    INSERT INTO dc_announcement_event
                        (announcement_id, event_type, impact_direction, severity, confidence, event_date,
                         entities_json, metrics_json, summary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ann_id,
                        event["event_type"],
                        event["impact_direction"],
                        event["severity"],
                        event["confidence"],
                        event["event_date"],
                        json.dumps({"ts_code": row.get("ts_code"), "title": row.get("title")}, ensure_ascii=False),
                        "{}",
                        event["summary"],
                        now,
                    ),
                )
                events += 1
    return written, events


def classify_announcement_event(title: str, publish_time: str = "") -> dict[str, Any] | None:
    rules = [
        ("立案|调查|处罚|监管函|问询函|警示函", "regulatory_risk", "negative", "high"),
        ("减持|减持计划", "shareholder_reduction", "negative", "high"),
        ("质押|解除质押", "share_pledge", "negative", "medium"),
        ("业绩预告|业绩快报|年度报告|季度报告|半年度报告", "earnings", "neutral", "medium"),
        ("回购|增持", "buyback_or_increase", "positive", "medium"),
        ("分红|权益分派|利润分配", "dividend", "positive", "low"),
        ("重大合同|中标|订单", "major_contract", "positive", "medium"),
        ("并购|重组|收购|资产购买|定增|非公开发行", "capital_operation", "neutral", "medium"),
    ]
    import re

    for pattern, event_type, direction, severity in rules:
        if re.search(pattern, title):
            return {
                "event_type": event_type,
                "impact_direction": direction,
                "severity": severity,
                "confidence": 0.65,
                "event_date": publish_time[:10],
                "summary": title[:300],
            }
    return None


def import_industry_metrics(path: str) -> dict[str, Any]:
    """Import industry metric definitions and values from JSON or CSV."""

    ensure_data_center_seeded()
    source_path = Path(path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(str(source_path))
    run_id = start_ingestion_run("manual.industry_metrics", {"path": str(source_path)})
    try:
        definitions, values = _load_industry_metric_file(source_path)
        def_count = _upsert_industry_metric_definitions(definitions)
        val_count = _upsert_industry_metric_values(values)
        finish_ingestion_run(run_id, status="completed", rows_read=len(definitions) + len(values), rows_written=def_count + val_count, meta={"path": str(source_path)})
        return {"ok": True, "run_id": run_id, "definitions": def_count, "values": val_count}
    except Exception as exc:  # noqa: BLE001
        finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc), meta={"path": str(source_path)})
        raise


def _load_industry_metric_file(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return list(payload.get("definitions") or []), list(payload.get("values") or [])
        if isinstance(payload, list):
            return payload, []
    df = pd.read_csv(path)
    rows = [_clean_payload(row) for row in df.to_dict(orient="records")]
    definitions = [r for r in rows if r.get("metric_name") or r.get("industry_name")]
    values = [r for r in rows if r.get("data_date") and ("value" in r or "raw_value" in r)]
    return definitions, values


def _upsert_industry_metric_definitions(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = _now()
    written = 0
    with _conn() as c:
        for row in rows:
            metric_code = str(row.get("metric_code") or "").strip()
            if not metric_code:
                continue
            values = (
                metric_code,
                str(row.get("industry_code") or "").strip(),
                str(row.get("industry_name") or "").strip(),
                str(row.get("metric_name") or "").strip(),
                str(row.get("unit") or "").strip(),
                str(row.get("frequency") or "").strip(),
                str(row.get("source_code") or SOURCE_MANUAL).strip(),
                str(row.get("description") or "").strip(),
                now,
                now,
            )
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO dc_industry_metric_definition
                        (metric_code, industry_code, industry_name, metric_name, unit, frequency, source_code, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        industry_code = VALUES(industry_code), industry_name = VALUES(industry_name),
                        metric_name = VALUES(metric_name), unit = VALUES(unit), frequency = VALUES(frequency),
                        source_code = VALUES(source_code), description = VALUES(description), updated_at = VALUES(updated_at)
                    """,
                    values,
                )
            else:
                c.execute(
                    """
                    INSERT INTO dc_industry_metric_definition
                        (metric_code, industry_code, industry_name, metric_name, unit, frequency, source_code, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(metric_code) DO UPDATE SET
                        industry_code = excluded.industry_code,
                        industry_name = excluded.industry_name,
                        metric_name = excluded.metric_name,
                        unit = excluded.unit,
                        frequency = excluded.frequency,
                        source_code = excluded.source_code,
                        description = excluded.description,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
            written += 1
    return written


def estimate_data_center_runtime(years: int = 10, *, trading_days_per_year: int = 250) -> dict[str, Any]:
    """Return a practical runtime estimate for initial full load and daily delta."""

    years = max(1, min(int(years), 30))
    trading_days = years * int(trading_days_per_year)
    report_periods = years * 4
    yearly_periods = years

    # Empirical baseline from local probes: full-market daily/daily_basic/stk_limit
    # is usually 1-6s each; use conservative 3s plus 1s spacing per call.
    per_call_sec = 4.0
    slow_call_sec = 10.0
    daily_market_calls = trading_days * 3
    stock_count = 5534
    financial_calls_watchlist_300 = 300 * report_periods * 4
    financial_calls_full_market = stock_count * report_periods * 4
    index_calls = trading_days + years * 12
    governance_calls = report_periods * 3 + yearly_periods * 3

    initial_calls_core = daily_market_calls + financial_calls_watchlist_300 + index_calls + governance_calls
    initial_sec_core = daily_market_calls * per_call_sec + financial_calls_watchlist_300 * slow_call_sec + index_calls * per_call_sec + governance_calls * slow_call_sec
    full_financial_sec = financial_calls_full_market * slow_call_sec

    daily_calls = 8
    daily_sec = 45
    disclosure_sec = 20

    return {
        "ok": True,
        "assumptions": {
            "years": years,
            "trading_days": trading_days,
            "report_periods": report_periods,
            "rate_limit_policy": "single-process sequential by default; increase only after observing stability",
        },
        "initial_full_load": {
            "recommended_strategy": "按交易日批量拉行情；财务按股票池分批；公告按日期分页增量；行业指标手工/脚本导入",
            "estimated_api_calls_core_300": initial_calls_core,
            "estimated_time_hours_core_300_conservative": round(initial_sec_core / 3600, 2),
            "estimated_financial_calls_full_market": financial_calls_full_market,
            "estimated_financial_time_hours_full_market_conservative": round(full_financial_sec / 3600, 2),
            "practical_window": "行情/估值全市场约数小时；财务建议先 100-300 只观察池，首次约 2-3 天分批更稳",
            "avoid": "当前代理财务三表强制 ts_code，别一次性跑全市场全历史财务；要分股票池、分报告期、可恢复执行",
        },
        "daily_incremental": {
            "tasks": [
                "daily",
                "daily_basic",
                "stk_limit",
                "suspend_d optional",
                "forecast/express by announcement date or recent window",
                "cninfo announcements by date",
                "build snapshot",
                "quality checks",
            ],
            "estimated_api_calls": daily_calls,
            "estimated_time_minutes": round((daily_sec + disclosure_sec) / 60, 1),
            "practical_window": "约 1-3 分钟；公告分页多时 3-8 分钟",
        },
        "next_optimization": [
            "财务使用 sync-financial-batch 分股票池、分报告期、可恢复同步",
            "行情按 trade_date 批量同步并缓存 Raw",
            "失败接口只重试失败 API，不重跑整包",
            "公告按日期分页增量，保存 content_hash 去重",
            "后续可加低并发队列，但默认不并发打 TuShare",
        ],
    }


def _upsert_industry_metric_values(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = _now()
    written = 0
    with _conn() as c:
        for row in rows:
            metric_code = str(row.get("metric_code") or "").strip()
            data_date = str(row.get("data_date") or "").strip()
            if not metric_code or not data_date:
                continue
            raw_value = str(row.get("raw_value") if row.get("raw_value") is not None else row.get("value") or "")
            values = (
                metric_code,
                data_date,
                _float_or_none(row.get("value")),
                raw_value,
                str(row.get("source_code") or SOURCE_MANUAL).strip(),
                now,
                now,
            )
            if c._is_mysql:
                c.execute(
                    """
                    INSERT INTO dc_industry_metric_value
                        (metric_code, data_date, value, raw_value, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        value = VALUES(value), raw_value = VALUES(raw_value), source_code = VALUES(source_code),
                        available_time = VALUES(available_time), updated_at = VALUES(updated_at)
                    """,
                    values,
                )
            else:
                c.execute(
                    """
                    INSERT INTO dc_industry_metric_value
                        (metric_code, data_date, value, raw_value, source_code, available_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(metric_code, data_date) DO UPDATE SET
                        value = excluded.value,
                        raw_value = excluded.raw_value,
                        source_code = excluded.source_code,
                        available_time = excluded.available_time,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
            written += 1
    return written
