from __future__ import annotations

import json
import hashlib
import os
import time
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from app.data_platform.db import _conn, _is_mysql
from app.data_platform.tushare_client import get_tushare_client, reset_tushare_client
from app.data_platform.services.runtime_logs import write_runtime_log
from app.data_platform.redis_store import set_add as redis_set_add, set_remove as redis_set_remove

SOURCE_TUSHARE = "tushare"
SOURCE_CNINFO = "cninfo"
SOURCE_MANUAL = "manual"
SOURCE_EXCHANGE_PCF = "exchange_etf_pcf"
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

_tushare_activity_local = threading.local()
_tushare_cooldown_lock = threading.Lock()
_data_center_seed_lock = threading.Lock()
_data_center_seeded = False
_tushare_api_cooldown_until: dict[str, float] = {}
_tushare_global_cooldown_until = 0.0
_tushare_reduced_concurrency_until = 0.0
_tushare_reduced_concurrency = 0
TUSHARE_RATE_LIMIT_COOLDOWN_SECONDS = int(os.getenv("TUSHARE_RATE_LIMIT_COOLDOWN_SECONDS", "180") or 180)
STK_LIMIT_PAGE_SIZE = max(500, min(int(os.getenv("TUSHARE_STK_LIMIT_PAGE_SIZE", "5800") or 5800), 5800))
STK_LIMIT_MAX_PAGES = max(2, min(int(os.getenv("TUSHARE_STK_LIMIT_MAX_PAGES", "4") or 4), 10))
STK_LIMIT_GAP_BATCH_SIZE = max(3, min(int(os.getenv("TUSHARE_STK_LIMIT_GAP_BATCH_SIZE", "25") or 25), 100))
DATE_RAW_COVERAGE_CHUNK_SIZE = max(
    5,
    min(int(os.getenv("DATE_RAW_COVERAGE_CHUNK_SIZE", "20") or 20), 60),
)
STK_LIMIT_MIN_STOCK_COVERAGE = max(
    0.5,
    min(float(os.getenv("TUSHARE_STK_LIMIT_MIN_STOCK_COVERAGE", "0.8") or 0.8), 1.0),
)


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
    workers = _effective_tushare_workers(len(items))
    parent_hook = getattr(_tushare_activity_local, "hook", None)

    def wrapped(item: Any) -> Any:
        set_tushare_activity_hook(parent_hook)
        try:
            return fn(item)
        finally:
            set_tushare_activity_hook(None)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix) as pool:
        return list(pool.map(wrapped, items)), workers


def _base_tushare_workers(item_count: int) -> int:
    configured = int(os.getenv("TUSHARE_CONCURRENCY", "16") or 16)
    return max(1, min(configured, 20, item_count or 1))


def _effective_tushare_workers(item_count: int) -> int:
    base = _base_tushare_workers(item_count)
    now = time.monotonic()
    with _tushare_cooldown_lock:
        if now < _tushare_reduced_concurrency_until and _tushare_reduced_concurrency > 0:
            return max(1, min(base, _tushare_reduced_concurrency, item_count or 1))
    return base


def _cooldown_remaining(api_name: str) -> int:
    now = time.monotonic()
    with _tushare_cooldown_lock:
        until = max(_tushare_global_cooldown_until, _tushare_api_cooldown_until.get(api_name, 0.0))
    return max(0, int(until - now))


def _rate_limit_cooldown_active(value: Any, *, now: datetime | None = None) -> bool:
    """Handle both legacy naive and timezone-aware failure timestamps."""
    try:
        failed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if now is not None:
            current = now
        else:
            current = datetime.now(failed.tzinfo) if failed.tzinfo else datetime.now()
        if failed.tzinfo and current.tzinfo is None:
            current = current.replace(tzinfo=failed.tzinfo)
        elif failed.tzinfo and current.tzinfo:
            current = current.astimezone(failed.tzinfo)
        elif failed.tzinfo is None and current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        return (current - failed).total_seconds() < AUTONOMOUS_RATE_LIMIT_COOLDOWN_SECONDS
    except (TypeError, ValueError):
        return False


def _activate_rate_limit_cooldown(api_name: str, *, seconds: int | None = None) -> None:
    global _tushare_global_cooldown_until, _tushare_reduced_concurrency_until, _tushare_reduced_concurrency
    duration = int(seconds or TUSHARE_RATE_LIMIT_COOLDOWN_SECONDS)
    until = time.monotonic() + duration
    base_workers = _base_tushare_workers(999999)
    reduced_workers = max(1, min(base_workers, max(4, base_workers // 2)))
    with _tushare_cooldown_lock:
        _tushare_api_cooldown_until[api_name] = max(_tushare_api_cooldown_until.get(api_name, 0.0), until)
        _tushare_global_cooldown_until = max(_tushare_global_cooldown_until, time.monotonic() + min(duration, 60))
        _tushare_reduced_concurrency_until = max(_tushare_reduced_concurrency_until, until)
        _tushare_reduced_concurrency = reduced_workers
    write_runtime_log(
        "WARNING",
        "tushare",
        f"cooldown {api_name}",
        {
            "api": api_name,
            "seconds": duration,
            "reason": "rate_limit",
            "base_concurrency": base_workers,
            "reduced_concurrency": reduced_workers,
        },
    )


class TushareCoolingDown(RuntimeError):
    def __init__(self, api_name: str, seconds: int):
        self.api_name = api_name
        self.seconds = seconds
        super().__init__(f"Tushare {api_name} cooling down for {seconds}s after rate_limit")


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
    TushareCatalogItem("daily", "行情", "A股日线", "价格、成交量、技术面和回测基础", "daily", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount", {"trade_date": ""}),
    TushareCatalogItem("fund_basic", "基金", "场内基金基础信息", "防御ETF主数据与交易状态", "monthly", "ts_code,name,management,custodian,fund_type,found_date,due_date,list_date,issue_date,delist_date,market", {"market": "E"}),
    TushareCatalogItem("fund_daily", "基金", "场内基金日线", "货币、债券、黄金、低波和红利ETF模拟交易", "daily", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount", {"ts_code": ""}),
    TushareCatalogItem("fund_nav", "基金", "公募基金净值", "ETF折溢价、规模与复权净值", "daily", "ts_code,ann_date,nav_date,unit_nav,accum_nav,accum_div,net_asset,total_netasset,adj_nav", {"ts_code": ""}),
    TushareCatalogItem("fund_div", "基金", "公募基金分红", "ETF现金分配与账户总收益", "weekly", "ts_code,ann_date,imp_anndate,base_date,div_proc,record_date,ex_date,pay_date,earpay_date,net_ex_date,div_cash", {"ts_code": ""}),
    TushareCatalogItem("weekly", "行情", "周线", "长线趋势辅助", "weekly", "ts_code,trade_date,open,high,low,close,vol,amount", {"trade_date": ""}),
    TushareCatalogItem("monthly", "行情", "月线", "长线趋势辅助", "monthly", "ts_code,trade_date,open,high,low,close,vol,amount", {"trade_date": ""}),
    TushareCatalogItem("adj_factor", "行情", "复权因子", "历史价格复权和收益计算", "daily", "ts_code,trade_date,adj_factor", {"trade_date": ""}),
    TushareCatalogItem("daily_basic", "估值", "每日指标", "估值、市值、换手、量化快照", "daily", "ts_code,trade_date,close,turnover_rate,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,total_mv,circ_mv", {"trade_date": ""}),
    TushareCatalogItem("suspend_d", "交易状态", "停复牌", "交易约束、风险过滤", "daily", "ts_code,trade_date,suspend_timing,suspend_type", {"trade_date": ""}),
    TushareCatalogItem("stk_limit", "交易状态", "涨跌停价", "交易约束、涨跌停识别", "daily", "ts_code,trade_date,up_limit,down_limit", {"trade_date": ""}),
    TushareCatalogItem("stock_st", "交易状态", "ST股票列表", "风险警示状态与涨跌停规则校验", "daily", "ts_code,name,trade_date,type,type_name", {"trade_date": ""}),
    TushareCatalogItem("moneyflow", "资金", "个股资金流", "资金确认项、短中期风险辅助", "daily", "ts_code,trade_date,buy_sm_vol,sell_sm_vol,buy_lg_vol,sell_lg_vol,net_mf_vol,net_mf_amount", {"trade_date": ""}),
    TushareCatalogItem("moneyflow_hsgt", "资金", "沪深港通资金流向", "北向资金净流入和南向资金观测", "daily", "trade_date,ggt_ss,ggt_sz,hgt,sgt,north_money,south_money", {"start_date": "", "end_date": ""}),
    TushareCatalogItem("hk_hold", "资金", "沪深股通持股明细", "外资持仓变化、行业和龙头资金确认", "daily", "code,trade_date,ts_code,name,vol,ratio,exchange", {"trade_date": "", "exchange": "SH"}),
    TushareCatalogItem("margin", "两融", "融资融券交易汇总", "市场杠杆风险偏好和拥挤度", "daily", "trade_date,exchange_id,rzye,rzmre,rzche,rqye,rqmcl,rqyl", {"trade_date": ""}),
    TushareCatalogItem("margin_detail", "两融", "融资融券交易明细", "个股资金持续、杠杆拥挤和退潮信号", "daily", "trade_date,ts_code,rzye,rqye,rzmre,rqmcl,rzche,rqchl", {"trade_date": ""}),
    TushareCatalogItem("margin_secs", "两融", "融资融券标的", "历史融资融券资格和可交易约束", "daily", "trade_date,ts_code,name,exchange", {"trade_date": "", "exchange": "SSE"}),
    TushareCatalogItem("top_list", "席位", "龙虎榜每日明细", "异动强度、资金结构和事件响应", "daily", "trade_date,ts_code,name,close,pct_change,turnover_rate,amount,l_sell,l_buy,l_amount,net_amount,net_rate,amount_rate,float_values,reason", {"trade_date": ""}),
    TushareCatalogItem("top_inst", "席位", "龙虎榜机构交易明细", "机构与营业部结构、龙头质量", "daily", "trade_date,ts_code,exalter,buy,sell,net_buy", {"trade_date": ""}),
    TushareCatalogItem("major_news", "资讯", "长篇财经新闻", "主线叙事聚类、催化新鲜度与事件识别", "hourly", "title,content,pub_time,src", {"start_date": "", "end_date": ""}),
    TushareCatalogItem("report_rc", "预期", "券商盈利预测", "点时独立机构一致预期、盈利预期差和目标价修正", "daily_incremental", "ts_code,name,report_date,report_title,report_type,classify,org_name,author_name,quarter,op_rt,op_pr,tp,np,eps,pe,rd,roe,ev_ebitda,rating,max_price,min_price", {"start_date": "", "end_date": ""}),
    TushareCatalogItem("income", "财务", "利润表", "营收、利润、成长能力", "quarterly", "ts_code,ann_date,f_ann_date,end_date,report_type,revenue,total_revenue,operate_profit,total_profit,n_income,n_income_attr_p,basic_eps,diluted_eps", {"ts_code": "", "period": ""}),
    TushareCatalogItem("balancesheet", "财务", "资产负债表", "资产质量、负债、安全性", "quarterly", "ts_code,ann_date,f_ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int,money_cap,accounts_receiv,inventories,goodwill,st_borr,lt_borr,bond_payable,non_cur_liab_due_1y", {"ts_code": "", "period": ""}),
    TushareCatalogItem("cashflow", "财务", "现金流量表", "经营现金流、自由现金流质量", "quarterly", "ts_code,ann_date,f_ann_date,end_date,n_cashflow_act,n_cash_flows_inv_act,n_cash_flows_fnc_act,c_pay_acq_const_fiolta", {"ts_code": "", "period": ""}),
    TushareCatalogItem("fina_indicator", "财务", "财务指标", "ROE、毛利率、净利率、偿债指标", "quarterly", "ts_code,ann_date,end_date,roe,roe_waa,roa,roic,grossprofit_margin,netprofit_margin,debt_to_assets,current_ratio,quick_ratio,assets_turn,inv_turn,ar_turn,ocf_to_or,ocf_to_profit,q_sales_yoy,q_profit_yoy", {"ts_code": "", "period": ""}),
    TushareCatalogItem("fina_audit", "财务", "审计意见", "非标审计预警", "yearly", "ts_code,ann_date,end_date,audit_result,audit_fees,audit_agency", {"ts_code": "", "period": ""}),
    TushareCatalogItem("fina_mainbz", "财务", "主营构成", "业务结构和行业暴露", "yearly", "ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost,curr_type", {"ts_code": "", "period": "", "type": "P"}),
    TushareCatalogItem("forecast", "业绩", "业绩预告", "预期变化和业绩风险", "daily_incremental", "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,summary", {"ann_date": ""}),
    TushareCatalogItem("express", "业绩", "业绩快报", "财报前业绩验证", "daily_incremental", "ts_code,ann_date,end_date,revenue,total_profit,n_income,total_assets", {"ts_code": "", "period": ""}),
    TushareCatalogItem("dividend", "分红", "分红送股", "股东回报、股息率验证", "quarterly", "ts_code,end_date,ann_date,div_proc,stk_div,cash_div_tax,record_date,ex_date,pay_date", {"ts_code": ""}),
    TushareCatalogItem("repurchase", "资本动作", "股票回购", "回购进度、规模和金额/市值重要性", "daily_incremental", "ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,low_limit", {"start_date": "", "end_date": ""}),
    TushareCatalogItem("stk_holdertrade", "资本动作", "股东增减持", "增减持方向、股数、比例和交易价格", "daily_incremental", "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_share,after_ratio,avg_price,total_share", {"start_date": "", "end_date": ""}),
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
    TushareCatalogItem("cn_ppi", "宏观", "工业生产者出厂价格指数", "行业产品价格与成本的宏观代理；字段按上游实际响应留痕", "monthly", "", {}),
    TushareCatalogItem("cn_m", "宏观", "货币供应量", "工业景气与流动性辅助；字段按上游实际响应留痕", "monthly", "", {}),
    TushareCatalogItem("cn_pmi", "宏观", "采购经理指数", "制造业与非制造业景气判断", "monthly", "MONTH,PMI010000,PMI020100,PMI030000", {}),
    TushareCatalogItem("shibor", "宏观", "上海银行间同业拆放利率", "流动性和利率环境", "daily", "date,on,1w,2w,1m,3m,6m,9m,1y", {}),
    TushareCatalogItem("fx_daily", "宏观", "美元离岸人民币", "汇率与外围风险", "daily", "ts_code,trade_date,bid_open,bid_close,bid_high,bid_low,ask_open,ask_close,ask_high,ask_low", {"ts_code": "USDCNH.FXCM"}),
]


def _now() -> str:
    # The rest of the data platform stores naive timestamps in the host
    # timezone (Asia/Shanghai). UTC here made live activity appear eight hours
    # behind and corrupted elapsed/ETA presentation in the frontend.
    return datetime.now().isoformat(timespec="seconds")


class EmptyTushareResult(RuntimeError):
    """Tushare returned no rows for a request that was expected to be checked.

    An empty response is not a successful ingestion: it must remain visible to
    the quality gate and be eligible for retry/fallback instead of being marked
    as covered silently.
    """


class TushareUpstreamNotReady(EmptyTushareResult):
    def __init__(self, api_name: str, trade_date: str):
        self.api_name = api_name
        self.trade_date = trade_date
        super().__init__(f"Tushare {api_name} {trade_date} 今日数据尚未入库，等待上游更新后重试")


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
    if isinstance(exc, TushareUpstreamNotReady) or "今日数据尚未入库" in message:
        return "upstream_not_ready"
    if isinstance(exc, EmptyTushareResult) or "empty result" in message or "返回为空" in message:
        return "empty_pending"
    if any(x in message for x in ("token", "unauthorized", "401", "登录", "认证")):
        return "authentication"
    if any(x in message for x in ("权限", "permission", "forbidden", "403", "积分")):
        return "permission"
    if any(x in message for x in ("频率", "频繁", "限流", "rate limit", "too many", "429")):
        return "rate_limit"
    if any(x in message for x in ("timeout", "timed out", "connection", "proxy", "502", "503", "504", "temporarily")):
        return "transient"
    if any(x in message for x in ("invalid", "参数", "parameter", "字段")):
        return "invalid_request"
    return "unknown"


def _request_key(api_name: str, params: dict[str, Any], limit_rows: int | None) -> str:
    raw = json.dumps({"api": api_name, "params": params, "limit_rows": limit_rows}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


COMPLETED_REQUEST_STATUSES = ("success", "not_applicable", "valid_empty", "deferred_empty")
DEFER_EMPTY_AFTER_ATTEMPTS = 5


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


def resolve_covered_failed_tushare_requests(*, limit: int = 500) -> dict[str, Any]:
    """Close stale failures only when their exact request already has terminal evidence."""
    cap = max(1, min(int(limit), 2_000))
    with _conn() as c:
        rows = c.execute(
            """SELECT f.id,f.request_key
                 FROM dc_tushare_failed_requests f
                 JOIN dc_tushare_request_coverage q ON q.request_key=f.request_key
                WHERE f.status='pending' AND q.result_status IN ('success','not_applicable','valid_empty','deferred_empty')
                ORDER BY f.id LIMIT ?""",
            (cap,),
        ).fetchall()
        ids = [int(dict(row)["id"]) for row in rows]
        if ids:
            resolved_sql = """UPDATE dc_tushare_failed_requests SET status='resolved',resolved_at=?
                              WHERE id=? AND status='pending'"""
            resolved_rows = [(_now(), request_id) for request_id in ids]
            cursor = c.raw.cursor() if bool(getattr(c, "_is_postgres", False)) else c.raw
            cursor.executemany(
                resolved_sql.replace("?", "%s") if bool(getattr(c, "_is_postgres", False)) else resolved_sql,
                resolved_rows,
            )
    for row in rows:
        redis_set_remove("alis:data-platform:tushare:failed", str(dict(row)["request_key"]))
    return {
        "ok": True,
        "status": "completed",
        "examined": len(ids),
        "resolved": len(ids),
        "policy": "exact_request_key_with_terminal_coverage_only",
    }


def _record_request_coverage(
    api_name: str, params: dict[str, Any], limit_rows: int | None, rows_read: int,
    *, result_status: str = "success", result_reason: str = "",
) -> None:
    key = _request_key(api_name, params, limit_rows)
    with _conn() as c:
        c.execute(
            """INSERT INTO dc_tushare_request_coverage
               (request_key,api_name,ts_code,trade_date,period,params_json,rows_read,result_status,result_reason,checked_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_key) DO UPDATE SET rows_read=excluded.rows_read,result_status=excluded.result_status,
                   result_reason=excluded.result_reason,checked_at=excluded.checked_at""",
            (key, api_name, str(params.get("ts_code") or ""), str(params.get("trade_date") or ""),
             str(params.get("period") or params.get("enddate") or ""), json.dumps(params, ensure_ascii=False, sort_keys=True),
             rows_read, result_status, result_reason[:500], _now()),
        )


def _failed_request_attempts(api_name: str, params: dict[str, Any], limit_rows: int | None = None) -> int:
    key = _request_key(api_name, params, limit_rows)
    with _conn() as c:
        row = c.execute(
            "SELECT attempts FROM dc_tushare_failed_requests WHERE request_key=?",
            (key,),
        ).fetchone()
    return int((dict(row) if row else {}).get("attempts") or 0)


def _defer_empty_request_if_exhausted(api_name: str, params: dict[str, Any], limit_rows: int | None = None) -> bool:
    attempts = _failed_request_attempts(api_name, params, limit_rows)
    if attempts < DEFER_EMPTY_AFTER_ATTEMPTS:
        return False
    _record_request_coverage(
        api_name,
        params,
        limit_rows,
        0,
        result_status="deferred_empty",
        result_reason=f"empty_result_after_{attempts}_attempts_deferred_to_fallback",
    )
    _resolve_failed_tushare_request(api_name, params, limit_rows)
    return True


def _mature_fund_nav_empty(params: dict[str, Any], *, today_value: date | None = None) -> bool:
    end_date = str(params.get("end_date") or params.get("nav_date") or "")
    if len(end_date) != 8 or not end_date.isdigit():
        return False
    observed = (today_value or _business_today()).strftime("%Y%m%d")
    cutoff = _etf_nav_publish_cutoff(observed)
    return bool(cutoff and end_date <= cutoff)


def _mature_optional_daily_empty(api_name: str, params: dict[str, Any], *, today_value: date | None = None) -> bool:
    if api_name != "moneyflow_hsgt":
        return False
    target_date = str(params.get("trade_date") or params.get("end_date") or "")
    if len(target_date) != 8 or not target_date.isdigit():
        return False
    observed = (today_value or _business_today()).strftime("%Y%m%d")
    cutoff = _etf_nav_publish_cutoff(observed)
    return bool(cutoff and target_date <= cutoff)


def _recent_calendar_window(days_back: int = 30, days_forward: int = 60) -> tuple[str, str]:
    start = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
    end = (date.today() + timedelta(days=days_forward)).strftime("%Y%m%d")
    return start, end


def ensure_data_center_seeded() -> dict[str, Any]:
    global _data_center_seeded
    if _data_center_seeded:
        return {"ok": True, "source": SOURCE_TUSHARE, "catalog_count": len(TUSHARE_CATALOG)}
    with _data_center_seed_lock:
        if _data_center_seeded:
            return {"ok": True, "source": SOURCE_TUSHARE, "catalog_count": len(TUSHARE_CATALOG)}
        result = _seed_data_center_catalog()
        _data_center_seeded = True
        return result


def _seed_data_center_catalog() -> dict[str, Any]:
    now = _now()
    with _conn() as c:
        for values in (
            (SOURCE_TUSHARE, "TuShare Pro", "api", 10, "active", "A股行情、财务、股东、分红、指数等结构化数据主通道", now, now),
            (SOURCE_CNINFO, "巨潮资讯", "crawler", 20, "active", "A股公告元数据、公告正文和附件主通道", now, now),
            (SOURCE_EXCHANGE_PCF, "沪深交易所 ETF PCF", "exchange_api", 15, "active", "上交所与深交所每日申购赎回篮子官方数据", now, now),
            ("official_policy", "主管部门公开政策与供给文件", "official_web", 25, "active", "发改委、工信部、能源局、海关和统计局公开目录；保留原文链接与可用时间", now, now),
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
        # This table has tens of millions of rows. PostgreSQL's exact COUNT(*)
        # scans the whole table and used to make the status poll hit the 30s
        # statement timeout. The catalog estimate is sufficient for a status
        # card; SQLite tests retain the exact count.
        raw_count = (
            c.execute("SELECT COALESCE(reltuples, 0)::BIGINT AS cnt FROM pg_class WHERE relname='dc_tushare_raw_records'").fetchone()
            if c._is_postgres
            else c.execute("SELECT COUNT(*) AS cnt FROM dc_tushare_raw_records").fetchone()
        )
        snapshot_count = (
            c.execute("SELECT COALESCE(reltuples, 0)::BIGINT AS cnt FROM pg_class WHERE relname='dc_stock_daily_snapshot'").fetchone()
            if c._is_postgres
            else c.execute("SELECT COUNT(*) AS cnt FROM dc_stock_daily_snapshot").fetchone()
        )
        quality_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_data_quality_checks").fetchone()
        announcement_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_announcement_raw").fetchone()
        event_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_announcement_event").fetchone()
        industry_metric_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_industry_metric_definition").fetchone()
        industry_value_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_industry_metric_value").fetchone()
        web_factor_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_web_factor_snapshot").fetchone()
        etf_pcf_header_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_etf_pcf_header").fetchone()
        etf_pcf_component_count = c.execute("SELECT COUNT(*) AS cnt FROM dc_etf_pcf_component").fetchone()
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
        "catalog": [_row_to_dict(row) for row in catalog_rows] + [
            {"api_name": "sse_etf_pcf", "category": "交易所ETF", "display_name": "上交所ETF每日申赎篮子",
             "recommended_frequency": "daily_premarket", "enabled": 1, "source_code": SOURCE_EXCHANGE_PCF,
             "last_probe_status": "official_public", "last_probe_rows": 0, "last_probe_error": None, "last_probe_at": None},
            {"api_name": "szse_etf_pcf", "category": "交易所ETF", "display_name": "深交所ETF每日申赎篮子",
             "recommended_frequency": "daily_premarket", "enabled": 1, "source_code": SOURCE_EXCHANGE_PCF,
             "last_probe_status": "official_public", "last_probe_rows": 0, "last_probe_error": None, "last_probe_at": None},
        ],
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
            "etf_pcf_header_rows": _row_count(etf_pcf_header_count),
            "etf_pcf_component_rows": _row_count(etf_pcf_component_count),
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


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    return _row_to_dict(row).get(key, default)


def _period_not_applicable(ts_code: str, period: str) -> bool:
    """A pre-listing report period is a valid no-data case, not an API failure."""
    if not ts_code or not period or not period.isdigit() or len(period) != 8:
        return False
    try:
        with _conn() as c:
            row = c.execute("SELECT list_date FROM dc_stock_master WHERE ts_code=?", (ts_code,)).fetchone()
        listed = str((dict(row) if row else {}).get("list_date") or "")
        return bool(listed and listed.isdigit() and listed > period)
    except Exception:
        return False


VALID_EMPTY_TUSHARE_APIS = {
    # These APIs describe optional events/disclosures. No returned row is a
    # valid negative observation after a successful request.
    "express", "pledge_detail", "pledge_stat", "namechange", "suspend_d",
    # A company or fund may never have distributed cash or shares. An empty
    # response is therefore positive evidence of "no corporate action", not
    # an ingestion fault.
    "dividend", "fund_div",
    # Dragon-Tiger and long-form news are event datasets. A successful query
    # with no rows is evidence that no event occurred in the requested window.
    "top_list", "top_inst", "major_news", "report_rc", "repurchase", "stk_holdertrade",
    # Index constituents/weights change on adjustment events. An empty monthly
    # window records a valid observation of no adjustment, not a daily outage.
    "index_weight",
}

PERIOD_FALLBACK_APIS = {
    "income", "balancesheet", "cashflow", "fina_indicator", "fina_mainbz", "fina_audit",
}

FINANCIAL_RESEARCH_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "income": ("total_revenue", "n_income", "n_income_attr_p"),
    "balancesheet": ("total_assets", "total_liab", "money_cap"),
    "cashflow": ("n_cashflow_act",),
    "fina_indicator": ("roe", "grossprofit_margin", "netprofit_margin", "debt_to_assets"),
}
FINANCIAL_SECTOR_RESEARCH_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "income": ("total_revenue", "n_income"),
    "balancesheet": ("total_assets", "total_liab", "total_hldr_eqy_exc_min_int"),
    "fina_indicator": ("roe",),
}


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
    cooldown = _cooldown_remaining(api_name)
    if cooldown > 0:
        _emit_tushare_activity("cooldown", api_name, params or {}, seconds=cooldown)
        raise TushareCoolingDown(api_name, cooldown)
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
                if category == "rate_limit":
                    _activate_rate_limit_cooldown(api_name)
                    raise
                # Empty responses are audited across independent attempts in
                # the persisted failure queue. Retrying the same empty slice
                # four times with 1/3/8-second sleeps here multiplied that
                # outer policy into 20 calls per slice. Only transport-level
                # transient failures use immediate backoff.
                if category != "transient" or attempt >= len(retry_delays):
                    raise
                delay = retry_delays[attempt]
                time.sleep(delay)
        if last_exc is not None:
            raise last_exc
        if df is None:
            df = pd.DataFrame()
        # Some Tushare financial endpoints occasionally return empty for an
        # exact period query although the stock-wide query contains that same
        # report. Verify with a broader request before declaring a real gap.
        if isinstance(df, pd.DataFrame) and df.empty and api_name in PERIOD_FALLBACK_APIS:
            ts_code = str(request_params.get("ts_code") or "")
            period = str(request_params.get("period") or "")
            if ts_code and period:
                broad_params = {k: v for k, v in request_params.items() if k not in {"period", "start_date", "end_date"}}
                _emit_tushare_activity("request", api_name, broad_params, attempt="period_fallback")
                broad = _call_tushare_api(api_name, params=broad_params, fields=item.fields)
                if isinstance(broad, pd.DataFrame) and not broad.empty:
                    period_col = "end_date" if "end_date" in broad.columns else ("period" if "period" in broad.columns else "")
                    if period_col:
                        df = broad[broad[period_col].astype(str) == period].copy()
                _emit_tushare_activity("response", api_name, broad_params, attempt="period_fallback", rows=len(df))
        if limit_rows is not None and limit_rows > 0 and isinstance(df, pd.DataFrame):
            df = df.head(int(limit_rows))
        if not isinstance(df, pd.DataFrame) or df.empty:
            trade_date = str(request_params.get("trade_date") or "")
            if trade_date and api_name in {"daily", "daily_basic", "stk_limit", "moneyflow", "moneyflow_hsgt", "adj_factor"}:
                try:
                    from app.data_platform.services.market_calendar import target_trade_date
                    with _conn() as c:
                        is_target = trade_date == target_trade_date(c)
                except Exception:
                    is_target = False
                if is_target:
                    raise TushareUpstreamNotReady(api_name, trade_date)
            ts_code = str(request_params.get("ts_code") or "")
            period = str(request_params.get("period") or "")
            if _period_not_applicable(ts_code, period):
                finish_ingestion_run(run_id, status="not_applicable", rows_read=0, rows_written=0, meta={"api": api_name, "params": request_params, "reason": "listed_after_period"})
                _record_request_coverage(api_name, request_params, limit_rows, 0, result_status="not_applicable", result_reason="listed_after_period")
                _emit_tushare_activity("skipped", api_name, request_params, rows=0, written=0, category="not_applicable", error="股票上市日晚于报告期")
                return {"ok": True, "api": api_name, "run_id": run_id, "rows_read": 0, "rows_written": 0, "not_applicable": True}
            if (
                api_name in VALID_EMPTY_TUSHARE_APIS
                or (api_name == "fund_nav" and _mature_fund_nav_empty(request_params))
                or _mature_optional_daily_empty(api_name, request_params)
            ):
                finish_ingestion_run(run_id, status="valid_empty", rows_read=0, rows_written=0, meta={"api": api_name, "params": request_params, "reason": "no_optional_event"})
                _record_request_coverage(api_name, request_params, limit_rows, 0, result_status="valid_empty", result_reason="no_optional_event")
                _resolve_failed_tushare_request(api_name, request_params, limit_rows)
                _emit_tushare_activity("skipped", api_name, request_params, rows=0, written=0, category="valid_empty", error="该范围内无可选事件")
                return {"ok": True, "api": api_name, "run_id": run_id, "rows_read": 0, "rows_written": 0, "valid_empty": True}
            raise EmptyTushareResult(
                f"Tushare {api_name} 返回为空，待下一轮复核（empty_pending）"
            )
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


def refresh_cn_ppi_from_first_source(*, today: date | None = None) -> dict[str, Any]:
    """Discover independent headline PPI plus the richer TuShare component slice."""
    observed_date = today or datetime.now().astimezone().date()
    first_this_month = observed_date.replace(day=1)
    previous_month_end = first_this_month - timedelta(days=1)
    target_month = previous_month_end.strftime("%Y%m")
    with _conn() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) rows FROM dc_tushare_raw_records WHERE api_name='cn_ppi' AND trade_date=?",
            (target_month,),
        ).fetchone()
    if int(dict(existing).get("rows") or 0) > 0:
        return {
            "ok": True, "schema_version": "alis_cn_ppi_publication_discovery_v1",
            "status": "already_current", "target_month": target_month,
            "source": "postgresql_local", "network_requests": 0,
        }
    release_probe_day = max(1, min(int(os.getenv("CN_PPI_PROBE_START_DAY", "7")), 20))
    if observed_date.day < release_probe_day:
        return {
            "ok": True, "schema_version": "alis_cn_ppi_publication_discovery_v1",
            "status": "not_due", "target_month": target_month,
            "next_probe_day": release_probe_day, "network_requests": 0,
        }
    from app.data_platform.services.independent_research_sources import fetch_eastmoney_ppi_headline

    headline: dict[str, Any]
    with _conn() as conn:
        headline_existing = conn.execute(
            "SELECT COUNT(*) rows FROM dc_tushare_raw_records WHERE api_name='cn_ppi_headline' AND trade_date=?",
            (target_month,),
        ).fetchone()
    if int(dict(headline_existing).get("rows") or 0) > 0:
        headline = {
            "status": "cache_hit", "provider": "eastmoney_ppi_headline",
            "rows": int(dict(headline_existing).get("rows") or 0), "network_requests": 0,
            "field_scope": "headline_only_no_component_claim",
        }
    else:
        try:
            headline_frame = fetch_eastmoney_ppi_headline(
                target_month, timeout_seconds=float(os.getenv("RESEARCH_PROBE_TIMEOUT_SECONDS", "12")),
            )
            headline_written = _upsert_raw_records("cn_ppi_headline", headline_frame) if not headline_frame.empty else 0
            headline = {
                "status": "available" if not headline_frame.empty else "not_published",
                "provider": "eastmoney_ppi_headline", "rows": len(headline_frame),
                "rows_written": headline_written, "field_scope": "headline_only_no_component_claim",
            }
        except Exception as exc:  # noqa: BLE001 - component discovery must still continue
            headline = {
                "status": "probe_failed", "provider": "eastmoney_ppi_headline",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "field_scope": "headline_only_no_component_claim",
            }
    from app.data_platform.services.research_source_discovery import race_tushare_sources

    def target_available(frame: pd.DataFrame) -> bool:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return False
        column = "month" if "month" in frame.columns else "MONTH" if "MONTH" in frame.columns else ""
        return bool(column and target_month in set(frame[column].astype(str)))

    discovery = race_tushare_sources(
        dataset="cn_ppi", target_key=target_month, api_name="cn_ppi", params={},
        validator=target_available, timeout_seconds=float(os.getenv("RESEARCH_PROBE_TIMEOUT_SECONDS", "12")),
        cooldown_seconds=int(os.getenv("RESEARCH_PROBE_COOLDOWN_SECONDS", "600")),
    )
    if not discovery.get("ok"):
        completed = discovery.get("completed") if isinstance(discovery.get("completed"), list) else []
        network_failed = bool(completed) and all(str(item.get("status") or "") == "probe_failed" for item in completed)
        return {
            "ok": True, "schema_version": "alis_cn_ppi_publication_discovery_v1",
            "status": "degraded" if network_failed else "observing",
            "reason": "all_sources_failed" if network_failed else str(discovery.get("status") or "not_published"),
            "target_month": target_month, "discovery": {key: value for key, value in discovery.items() if key != "frame"},
            "headline": headline,
            "production_change": False,
        }
    frame = discovery.get("frame")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {"ok": True, "status": "observing", "reason": "winner_returned_empty", "target_month": target_month}
    run_id = start_ingestion_run(
        "tushare.cn_ppi.first_source", {
            "api": "cn_ppi", "target_month": target_month, "provider": discovery.get("winner"),
            "endpoint": discovery.get("endpoint"),
        },
    )
    try:
        written = _upsert_raw_records("cn_ppi", frame)
        finish_ingestion_run(
            run_id, status="completed", rows_read=len(frame), rows_written=written,
            meta={"target_month": target_month, "provider": discovery.get("winner"), "probe_contract": discovery.get("probe_contract")},
        )
        _record_probe("cn_ppi", "ok", len(frame))
        _record_request_coverage("cn_ppi", {}, None, len(frame))
        _resolve_failed_tushare_request("cn_ppi", {}, None)
        if written:
            _invalidate_asset_views()
        return {
            "ok": True, "schema_version": "alis_cn_ppi_publication_discovery_v1",
            "status": "completed", "target_month": target_month,
            "winner": discovery.get("winner"), "rows_read": len(frame), "rows_written": written,
            "headline": headline,
            "discovery": {key: value for key, value in discovery.items() if key != "frame"},
        }
    except Exception as exc:  # noqa: BLE001
        finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc))
        raise


def list_failed_tushare_requests(*, status: str = "pending", limit: int = 100) -> dict[str, Any]:
    with _conn() as c:
        count_row = c.execute(
            "SELECT COUNT(*) AS total FROM dc_tushare_failed_requests WHERE status=?", (status,),
        ).fetchone()
        rows = c.execute(
            """SELECT id,api_name,params_json,limit_rows,error_category,last_error,status,attempts,
                      first_failed_at,last_failed_at,resumed_at,resolved_at
               FROM dc_tushare_failed_requests WHERE status=? ORDER BY last_failed_at ASC LIMIT ?""",
            (status, max(1, min(limit, 500))),
        ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["params"] = json.loads(item.pop("params_json") or "{}")
    return {"ok": True, "status": status, "total": int(dict(count_row).get("total") or 0), "items": items}


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


SAFE_AUTONOMOUS_RETRY_CATEGORIES = {"transient", "empty_pending", "rate_limit"}
AUTONOMOUS_RATE_LIMIT_COOLDOWN_SECONDS = 180
SUPERSEDED_API_REPLACEMENTS = {
    "etf_sh_cons": "exchange_official_pcf",
    "etf_sz_cons": "exchange_official_pcf",
}


def resolve_legacy_daily_index_weight_requests(*, limit: int = 100) -> dict[str, Any]:
    """Retire obsolete one-day index-weight probes replaced by monthly windows."""
    cap = max(1, min(int(limit), 500))
    with _conn() as c:
        rows = c.execute(
            """SELECT id,params_json,limit_rows FROM dc_tushare_failed_requests
                WHERE api_name='index_weight' AND status='pending' AND error_category='empty_pending'
                ORDER BY id LIMIT ?""",
            (cap,),
        ).fetchall()
    records: list[tuple[int, dict[str, Any], int | None]] = []
    skipped = 0
    for row in rows:
        item = dict(row)
        try:
            params = json.loads(item.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            skipped += 1
            continue
        start_date = str(params.get("start_date") or "")
        end_date = str(params.get("end_date") or "")
        if start_date and start_date == end_date:
            records.append((int(item["id"]), params, item.get("limit_rows")))
        else:
            skipped += 1
    resolved = _bulk_mark_requests_valid_empty(
        "index_weight", records,
        reason="superseded_single_day_probe_by_monthly_index_weight_window",
    )
    return {
        "ok": True, "status": "completed", "examined": len(rows),
        "resolved": resolved, "skipped": skipped,
        "policy": "single_day_index_weight_empty_replaced_by_monthly_window_only",
    }


def resume_safe_failed_tushare_requests_by_id(
    request_ids: list[int], *, limit: int = 5, maximum_attempts: int = DEFER_EMPTY_AFTER_ATTEMPTS,
) -> dict[str, Any]:
    """Retry only explicitly selected, low-risk pending requests.

    The caller may nominate ids, but this function independently enforces the
    category, attempt and batch boundaries. Authentication, permission,
    invalid-request and unknown failures never cross this route. Rate limits
    must independently pass the cooldown check below.
    """
    parsed_ids = set()
    for value in request_ids:
        try:
            request_id = int(value)
        except (TypeError, ValueError):
            continue
        if request_id > 0:
            parsed_ids.add(request_id)
    ids = sorted(parsed_ids)[:max(1, min(int(limit), 5))]
    if not ids:
        return {"ok": True, "attempted": 0, "succeeded": 0, "results": [], "skipped": []}
    placeholders = ",".join("?" for _ in ids)
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT id,api_name,params_json,limit_rows,error_category,last_error,status,attempts,last_failed_at
                FROM dc_tushare_failed_requests WHERE id IN ({placeholders}) ORDER BY id""",
            tuple(ids),
        ).fetchall()]
    by_id = {int(item["id"]): item for item in rows}
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for request_id in ids:
        item = by_id.get(request_id)
        if not item:
            skipped.append({"id": request_id, "reason": "request_not_found"})
            continue
        category = str(item.get("error_category") or "")
        attempts = int(item.get("attempts") or 0)
        if str(item.get("status") or "") != "pending":
            skipped.append({"id": request_id, "reason": "request_not_pending"})
            continue
        if category not in SAFE_AUTONOMOUS_RETRY_CATEGORIES:
            skipped.append({"id": request_id, "reason": "category_not_safe_for_autonomous_retry", "category": category})
            continue
        if attempts >= max(1, int(maximum_attempts)):
            skipped.append({"id": request_id, "reason": "maximum_attempts_reached", "attempts": attempts})
            continue
        if category == "rate_limit":
            if _rate_limit_cooldown_active(item.get("last_failed_at")):
                skipped.append({"id": request_id, "reason": "rate_limit_cooldown_active"})
                continue
        try:
            params = json.loads(item.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            skipped.append({"id": request_id, "reason": "invalid_stored_params"})
            continue
        with _conn() as c:
            c.execute("UPDATE dc_tushare_failed_requests SET status='resuming',resumed_at=? WHERE id=? AND status='pending'", (_now(), request_id))
        try:
            result = sync_tushare_api_raw(str(item["api_name"]), params=params, limit_rows=item.get("limit_rows"))
            results.append({"id": request_id, "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            failure_category = classify_tushare_error(exc)
            if failure_category == "empty_pending" and _defer_empty_request_if_exhausted(
                str(item["api_name"]), params, item.get("limit_rows"),
            ):
                results.append({"id": request_id, "ok": True, "status": "deferred_empty", "reason": "empty_result_attempt_limit_reached"})
            else:
                results.append({"id": request_id, "ok": False, "category": failure_category, "error": str(exc)[:500]})
    return {
        "ok": all(item.get("ok") for item in results),
        "attempted": len(results),
        "succeeded": sum(1 for item in results if item.get("ok")),
        "results": results,
        "skipped": skipped,
        "policy": "explicit_ids_safe_categories_max_5_and_bounded_attempts",
    }


def resolve_superseded_failed_requests_by_id(request_ids: list[int], *, limit: int = 5) -> dict[str, Any]:
    """Resolve only failures whose API has an explicitly registered replacement."""
    parsed_ids: set[int] = set()
    for value in request_ids:
        try:
            request_id = int(value)
        except (TypeError, ValueError):
            continue
        if request_id > 0:
            parsed_ids.add(request_id)
    ids = sorted(parsed_ids)[:max(1, min(int(limit), 5))]
    if not ids:
        return {"ok": True, "resolved": 0, "results": [], "skipped": []}
    placeholders = ",".join("?" for _ in ids)
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT id,api_name,params_json,limit_rows,status FROM dc_tushare_failed_requests
                WHERE id IN ({placeholders}) ORDER BY id""",
            tuple(ids),
        ).fetchall()]
    by_id = {int(item["id"]): item for item in rows}
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for request_id in ids:
        item = by_id.get(request_id)
        if not item:
            skipped.append({"id": request_id, "reason": "request_not_found"})
            continue
        api_name = str(item.get("api_name") or "")
        replacement = SUPERSEDED_API_REPLACEMENTS.get(api_name)
        if str(item.get("status") or "") != "pending":
            skipped.append({"id": request_id, "reason": "request_not_pending"})
            continue
        if not replacement:
            skipped.append({"id": request_id, "reason": "api_not_registered_as_superseded", "api_name": api_name})
            continue
        try:
            params = json.loads(item.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            skipped.append({"id": request_id, "reason": "invalid_stored_params"})
            continue
        _record_request_coverage(
            api_name, params, item.get("limit_rows"), 0,
            result_status="not_applicable", result_reason=f"superseded_by:{replacement}",
        )
        _resolve_failed_tushare_request(api_name, params, item.get("limit_rows"))
        results.append({"id": request_id, "api_name": api_name, "replacement": replacement, "status": "resolved"})
    return {
        "ok": True,
        "resolved": len(results),
        "results": results,
        "skipped": skipped,
        "policy": "registered_superseded_apis_only_max_5",
    }


def _upsert_raw_records(api_name: str, df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0
    now = _now()
    returned_fields = sorted(str(column) for column in df.columns)
    written = 0
    field_audit: dict[tuple[str, str, str], dict[str, float]] = {}
    with _conn() as c:
        if api_name in {"cn_ppi", "cn_ppi_headline", "cn_cpi"}:
            _normalize_monthly_raw_record_keys(c, api_name)
        for idx, row in df.iterrows():
            payload = _clean_payload(row.to_dict())
            if api_name in {"daily", "daily_basic", "stk_limit"}:
                payload["_field_sources"] = {
                    field: SOURCE_TUSHARE for field, value in payload.items()
                    if not field.startswith("_") and value is not None
                }
                payload["_field_source_contract"] = "alis_market_field_lineage_v1"
            if api_name in FINANCIAL_RESEARCH_REQUIRED_FIELDS:
                payload["_returned_fields"] = returned_fields
                payload["_field_contract_version"] = "financial_research_fields_v1"
            ts_code = str(payload.get("ts_code") or payload.get("con_code") or "").strip()
            trade_date = str(payload.get("trade_date") or payload.get("report_date") or payload.get("nav_date") or payload.get("date") or payload.get("month") or payload.get("MONTH") or payload.get("quarter") or "").strip()
            period = str(payload.get("period") or payload.get("end_date") or "").strip()
            ann_date = str(payload.get("ann_date") or "").strip()
            available_time = _point_in_time_available_time(api_name, payload, fallback=now)
            record_key = _record_key(api_name, payload, idx)
            if api_name == "index_daily" and ts_code == "000300.SH":
                previous = c.execute(
                    """SELECT source_code,payload_json FROM dc_tushare_raw_records
                       WHERE api_name=? AND record_key=? LIMIT 1""",
                    (api_name, record_key),
                ).fetchone()
                previous_item = dict(previous) if previous else {}
                previous_source = str(previous_item.get("source_code") or "")
                if previous_source in {"eastmoney_index_daily_fallback", "sina_index_daily_fallback"}:
                    try:
                        previous_payload = json.loads(previous_item.get("payload_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        previous_payload = {}
                    mismatches = []
                    for field in ("open", "high", "low", "close"):
                        try:
                            if abs(float(payload[field]) - float(previous_payload[field])) > 0.021:
                                mismatches.append(field)
                        except (KeyError, TypeError, ValueError):
                            mismatches.append(field)
                    if mismatches:
                        c.execute(
                            """INSERT INTO dc_data_quality_checks
                               (check_name,scope,status,severity,message,affected_count,checked_at,meta_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                "hs300_fallback_primary_reconciliation",
                                f"index_daily:{ts_code}:{trade_date}",
                                "failed",
                                "critical",
                                "沪深300备用日线与后到 TuShare 主源字段不一致，保留备用记录等待复核",
                                1,
                                now,
                                _strict_json({"fallback_source": previous_source, "mismatched_fields": mismatches}),
                            ),
                        )
                        continue
                    payload.update({
                        "pending_primary_reconciliation": False,
                        "fallback_reconciliation_status": "matched_primary",
                        "reconciled_fallback_source": previous_source,
                        "reconciled_at": now,
                    })
            elif api_name in {"daily", "daily_basic", "stk_limit"}:
                previous = c.execute(
                    """SELECT source_code,payload_json FROM dc_tushare_raw_records
                       WHERE api_name=? AND record_key=? LIMIT 1""",
                    (api_name, record_key),
                ).fetchone()
                previous_item = dict(previous) if previous else {}
                previous_source = str(previous_item.get("source_code") or "")
                if previous_source == "tencent_market_snapshot":
                    try:
                        previous_payload = json.loads(previous_item.get("payload_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        previous_payload = {}
                    tolerances = {
                        "daily": {"open": 0.021, "high": 0.021, "low": 0.021, "close": 0.021, "pre_close": 0.021},
                        "daily_basic": {
                            "turnover_rate": 0.051, "volume_ratio": 0.051, "pe_ttm": 0.101,
                            "pb": 0.051, "total_mv": 0.0, "circ_mv": 0.0,
                        },
                        "stk_limit": {"up_limit": 0.011, "down_limit": 0.011},
                    }[api_name]
                    mismatches = []
                    for field, tolerance in tolerances.items():
                        left, right = payload.get(field), previous_payload.get(field)
                        if left is None or right is None:
                            continue
                        try:
                            absolute = abs(float(left) - float(right))
                            relative = absolute / max(abs(float(left)), abs(float(right)), 1e-12)
                            mismatch = relative > .005 if field in {"total_mv", "circ_mv"} else absolute > tolerance
                            key = (trade_date, api_name, field)
                            audit = field_audit.setdefault(key, {
                                "count": 0, "mismatches": 0, "abs_sum": 0, "abs_max": 0,
                                "relative_sum": 0, "relative_max": 0,
                            })
                            audit["count"] += 1
                            audit["mismatches"] += int(mismatch)
                            audit["abs_sum"] += absolute
                            audit["abs_max"] = max(audit["abs_max"], absolute)
                            audit["relative_sum"] += relative
                            audit["relative_max"] = max(audit["relative_max"], relative)
                            if mismatch:
                                mismatches.append(field)
                        except (TypeError, ValueError):
                            mismatches.append(field)
                    status = "conflict_primary_selected" if mismatches else "matched_primary"
                    previous_sources = previous_payload.get("_field_sources")
                    field_sources = dict(previous_sources) if isinstance(previous_sources, dict) else {}
                    for field in returned_fields:
                        if not field.startswith("_") and payload.get(field) is not None:
                            field_sources[field] = SOURCE_TUSHARE
                    payload.update({
                        "pending_primary_reconciliation": False,
                        "fallback_reconciliation_status": status,
                        "reconciled_fallback_source": previous_source,
                        "reconciled_at": now,
                        "_field_sources": field_sources,
                        "_field_source_contract": "alis_market_field_lineage_v1",
                        "_field_reconciliation_contract": "non_null_authoritative_field_merge_v1",
                    })
                    if mismatches:
                        c.execute(
                            """INSERT INTO dc_data_quality_checks
                               (check_name,scope,status,severity,message,affected_count,checked_at,meta_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                "market_provider_reconciliation",
                                f"{api_name}:{ts_code}:{trade_date}",
                                "warning",
                                "medium",
                                "腾讯先到切片与后到 TuShare 主源存在字段差异，已保留冲突证据并采用主源修订",
                                1,
                                now,
                                _strict_json({"fallback_source": previous_source, "mismatched_fields": mismatches}),
                            ),
                        )
            if api_name == "stk_holdernumber":
                end_date = str(payload.get("end_date") or "")
                holder_num = payload.get("holder_num")
                invalid_date = bool(end_date and (not end_date.isdigit() or len(end_date) != 8 or end_date > date.today().strftime("%Y%m%d")))
                if invalid_date or holder_num is None:
                    reason = "invalid_future_end_date" if invalid_date else "missing_holder_num"
                    c.execute(
                        """INSERT INTO dc_data_quarantine(api_name,record_key,ts_code,period,payload_json,reason,quarantined_at)
                           VALUES(?,?,?,?,?,?,?) ON CONFLICT(api_name,record_key) DO UPDATE SET
                           payload_json=excluded.payload_json,reason=excluded.reason,quarantined_at=excluded.quarantined_at""",
                        (api_name, record_key, ts_code, end_date, _strict_json(payload), reason, now),
                    )
                    continue
            values = (
                api_name,
                record_key,
                ts_code,
                trade_date,
                period,
                ann_date,
                _strict_json(payload),
                SOURCE_TUSHARE,
                available_time,
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
                        payload_json = jsonb_set(
                            jsonb_strip_nulls(dc_tushare_raw_records.payload_json::jsonb)
                            || jsonb_strip_nulls(excluded.payload_json::jsonb),
                            '{_returned_fields}',
                            COALESCE((
                                SELECT jsonb_agg(field ORDER BY field)
                                FROM (
                                    SELECT DISTINCT field
                                    FROM jsonb_array_elements_text(
                                        COALESCE(dc_tushare_raw_records.payload_json::jsonb->'_returned_fields','[]'::jsonb)
                                        || COALESCE(excluded.payload_json::jsonb->'_returned_fields','[]'::jsonb)
                                    ) AS field
                                ) merged_fields
                            ), '[]'::jsonb)
                        )::text,
                        source_code = excluded.source_code,
                        available_time = excluded.available_time,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
            written += 1
        for (audit_date, dataset, field), audit in field_audit.items():
            count = int(audit["count"])
            mismatch_count = int(audit["mismatches"])
            status = "conflict" if mismatch_count else "matched"
            c.execute(
                """INSERT INTO dc_market_provider_field_audit
                   (dataset,trade_date,primary_provider,fallback_provider,field_name,compared_count,
                    mismatch_count,mean_abs_diff,max_abs_diff,mean_relative_diff,max_relative_diff,
                    status,checked_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(dataset,trade_date,primary_provider,fallback_provider,field_name) DO UPDATE SET
                    compared_count=excluded.compared_count,mismatch_count=excluded.mismatch_count,
                    mean_abs_diff=excluded.mean_abs_diff,max_abs_diff=excluded.max_abs_diff,
                    mean_relative_diff=excluded.mean_relative_diff,max_relative_diff=excluded.max_relative_diff,
                    status=excluded.status,checked_at=excluded.checked_at,evidence_json=excluded.evidence_json""",
                (
                    dataset, audit_date, SOURCE_TUSHARE, "tencent_market_snapshot", field, count,
                    mismatch_count, audit["abs_sum"] / count, audit["abs_max"],
                    audit["relative_sum"] / count, audit["relative_max"], status, now,
                    _strict_json({
                        "contract": "alis_market_provider_field_audit_v1",
                        "absolute_tolerance": tolerances.get(field),
                        "relative_tolerance": .005 if field in {"total_mv", "circ_mv"} else None,
                    }),
                ),
            )
        if api_name in {"daily", "daily_basic", "stk_limit", "adj_factor"}:
            trade_dates = sorted({
                str(value or "").replace("-", "")[:8]
                for value in df.get("trade_date", pd.Series(dtype=str)).tolist()
                if len(str(value or "").replace("-", "")[:8]) == 8
            })
            _refresh_oos_hard_api_stats(c, api_name, trade_dates, now)
    return written


_DISCLOSURE_APIS = {
    "income", "balancesheet", "cashflow", "fina_indicator", "fina_audit", "forecast", "express",
    "dividend", "fund_div", "top10_holders", "top10_floatholders", "stk_holdernumber", "pledge_detail", "disclosure_date",
    "repurchase", "stk_holdertrade",
    # Analyst research is point-in-time at the publication/trade date.  It is
    # not a financial statement, but treating it as ingestion-time data would
    # make every historical expectation feature silently unavailable.
    "report_rc",
}


def _iso_available_on_date(value: Any, *, hour: int = 23, minute: int = 59, second: int = 59) -> str | None:
    text = str(value or "").strip().replace("-", "")[:8]
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return parsed.replace(hour=hour, minute=minute, second=second).isoformat(timespec="seconds")


def _point_in_time_available_time(api_name: str, payload: dict[str, Any], *, fallback: str) -> str:
    """Separate economic availability from ingestion time for replay safety."""
    if api_name == "major_news":
        published = str(payload.get("pub_time") or "").strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(published).isoformat(timespec="seconds")
        except ValueError:
            return fallback
    if api_name in _DISCLOSURE_APIS:
        disclosed = (
            payload.get("actual_date")
            or payload.get("f_ann_date")
            or payload.get("ann_date")
            or payload.get("report_date")
            or payload.get("trade_date")
        )
        return _iso_available_on_date(disclosed) or fallback
    trade_date = payload.get("trade_date") or payload.get("date")
    return _iso_available_on_date(trade_date, hour=15, minute=0, second=0) or fallback


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


def _strict_json(payload: Any) -> str:
    """Serialize standards-compliant JSON; raw storage must be JSONB-castable."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _record_key(api_name: str, payload: dict[str, Any], idx: Any) -> str:
    if api_name == "major_news":
        raw = "|".join(str(payload.get(key) or "").strip() for key in ("src", "pub_time", "title", "content"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if api_name == "report_rc":
        raw = "|".join(
            str(payload.get(key) or "").strip()
            for key in (
                "ts_code",
                "report_date",
                "org_name",
                "author_name",
                "report_title",
                "report_type",
                "quarter",
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    preferred = [
        "ts_code",
        "con_code",
        "index_code",
        "trade_date",
        "report_date",
        "ann_date",
        "end_date",
        "period",
        "holder_name",
        "bz_item",
        "report_type",
        "report_title",
        "org_name",
        "author_name",
        "quarter",
        "comp_type",
        "update_flag",
        "div_proc",
        "pledge_amount",
        "proc",
        "in_de",
        "change_vol",
        "cal_date",
        "exchange",
        "exchange_id",
        "month",
        "MONTH",
        "date",
        "name",
        "code",
        "nav_date",
        "reason",
        "exalter",
        "pub_time",
        "src",
    ]
    parts = [str(payload.get(k) or "").strip() for k in preferred if str(payload.get(k) or "").strip()]
    if not parts:
        parts = [str(idx)]
    return "|".join(parts)[:240]


def _normalize_monthly_raw_record_keys(c: Any, api_name: str) -> dict[str, int]:
    """Replace unstable positional keys for bounded monthly macro series."""
    rows = c.execute(
        """SELECT id,record_key,payload_json,updated_at
             FROM dc_tushare_raw_records WHERE api_name=?
             ORDER BY updated_at DESC,id DESC""",
        (api_name,),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        desired = _record_key(api_name, payload if isinstance(payload, dict) else {}, row.get("id"))
        grouped.setdefault(desired, []).append(row)
    changed = deleted = 0
    for desired, candidates in grouped.items():
        winner = candidates[0]
        loser_ids = [int(item["id"]) for item in candidates[1:]]
        if loser_ids:
            placeholders = ",".join("?" for _ in loser_ids)
            c.execute(f"DELETE FROM dc_tushare_raw_records WHERE id IN ({placeholders})", tuple(loser_ids))
            deleted += len(loser_ids)
        if str(winner.get("record_key") or "") != desired:
            c.execute("UPDATE dc_tushare_raw_records SET record_key=? WHERE id=?", (desired, int(winner["id"])))
            changed += 1
    return {"changed": changed, "deleted_duplicates": deleted}


def normalize_report_rc_record_keys(*, limit: int = 200_000) -> dict[str, int]:
    """Migrate legacy analyst-report keys before historical backfill.

    Older imports used process-dependent keys and could not distinguish two
    brokers, report dates, or forecast quarters. The migration is idempotent;
    collisions are merged field-wise and never create a second logical report.
    """
    cap = max(1, min(int(limit), 500_000))
    scanned = changed = merged = 0
    with _conn() as c:
        rows = c.execute(
            """SELECT id,record_key,payload_json,available_time,updated_at
               FROM dc_tushare_raw_records
               WHERE api_name='report_rc' ORDER BY id LIMIT ?""",
            (cap,),
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            scanned += 1
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict) or not payload:
                continue
            new_key = _record_key("report_rc", payload, row.get("id"))
            if new_key == str(row.get("record_key") or ""):
                continue
            existing = c.execute(
                """SELECT id,payload_json,available_time,updated_at
                   FROM dc_tushare_raw_records
                   WHERE api_name='report_rc' AND record_key=? AND id<>?""",
                (new_key, row.get("id")),
            ).fetchone()
            if existing is not None:
                target = dict(existing)
                try:
                    target_payload = json.loads(target.get("payload_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    target_payload = {}
                if not isinstance(target_payload, dict):
                    target_payload = {}
                merged_payload = {**target_payload, **payload}
                c.execute(
                    """UPDATE dc_tushare_raw_records
                       SET payload_json=?,available_time=?,updated_at=?
                       WHERE id=?""",
                    (
                        _strict_json(merged_payload),
                        min(str(target.get("available_time") or ""), str(row.get("available_time") or "")) or str(target.get("available_time") or row.get("available_time") or ""),
                        max(str(target.get("updated_at") or ""), str(row.get("updated_at") or "")),
                        target.get("id"),
                    ),
                )
                c.execute("DELETE FROM dc_tushare_raw_records WHERE id=?", (row.get("id"),))
                merged += 1
            else:
                c.execute(
                    "UPDATE dc_tushare_raw_records SET record_key=? WHERE id=?",
                    (new_key, row.get("id")),
                )
                changed += 1
    return {"scanned": scanned, "changed": changed, "merged": merged}


def _stk_limit_stock_coverage(trade_date: str) -> dict[str, Any]:
    """Measure limit-price coverage against that day's retained A-share bars."""

    with _conn() as c:
        expected_row = c.execute(
            """SELECT COUNT(DISTINCT daily.ts_code) AS count
               FROM dc_tushare_raw_records daily
               JOIN dc_stock_master stock ON stock.ts_code=daily.ts_code
               WHERE daily.api_name='daily' AND daily.trade_date=?""",
            (trade_date,),
        ).fetchone()
        covered_row = c.execute(
            """SELECT COUNT(DISTINCT limits.ts_code) AS count
               FROM dc_tushare_raw_records limits
               JOIN dc_tushare_raw_records daily
                 ON daily.api_name='daily' AND daily.trade_date=limits.trade_date
                AND daily.ts_code=limits.ts_code
               JOIN dc_stock_master stock ON stock.ts_code=limits.ts_code
               WHERE limits.api_name='stk_limit' AND limits.trade_date=?""",
            (trade_date,),
        ).fetchone()
        retained_row = c.execute(
            """SELECT COUNT(DISTINCT ts_code) AS count
               FROM dc_tushare_raw_records
               WHERE api_name='stk_limit' AND trade_date=?""",
            (trade_date,),
        ).fetchone()
    expected = int(dict(expected_row).get("count") or 0)
    covered = int(dict(covered_row).get("count") or 0)
    retained = int(dict(retained_row).get("count") or 0)
    coverage = covered / expected if expected else 0.0
    return {
        "trade_date": trade_date,
        "expected_stock_rows": expected,
        "covered_stock_rows": covered,
        "retained_rows": retained,
        "coverage": round(coverage, 6),
        "minimum_coverage": STK_LIMIT_MIN_STOCK_COVERAGE,
        "ready": expected > 0 and coverage >= STK_LIMIT_MIN_STOCK_COVERAGE,
    }


def _missing_stk_limit_stock_codes(trade_date: str, *, limit: int) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            """SELECT DISTINCT daily.ts_code
               FROM dc_tushare_raw_records daily
               JOIN dc_stock_master stock ON stock.ts_code=daily.ts_code
               LEFT JOIN dc_tushare_raw_records limits
                 ON limits.api_name='stk_limit' AND limits.trade_date=daily.trade_date
                AND limits.ts_code=daily.ts_code
               WHERE daily.api_name='daily' AND daily.trade_date=? AND limits.id IS NULL
               ORDER BY daily.ts_code LIMIT ?""",
            (trade_date, max(1, int(limit))),
        ).fetchall()
    return [str(dict(row).get("ts_code") or "") for row in rows if str(dict(row).get("ts_code") or "")]


def sync_stk_limit_gap_batch(trade_date: str, *, limit: int = STK_LIMIT_GAP_BATCH_SIZE) -> dict[str, Any]:
    """Bounded exact-code fallback after the full-market slice is truncated.

    This path only touches codes proven missing from the retained full-market
    result.  Three leading probes prevent a not-yet-published upstream slice
    from turning into hundreds of empty per-stock requests.  Successful rows
    disappear from the next missing-code query, providing durable progress
    without a separate cursor table.
    """

    codes = _missing_stk_limit_stock_codes(trade_date, limit=max(3, int(limit)))
    if not codes:
        coverage = _stk_limit_stock_coverage(trade_date)
        return {"ok": coverage["ready"], "status": "no_gaps", "codes": [], "results": [], "coverage": coverage}
    results: list[dict[str, Any]] = []
    successes = 0
    probe_count = min(3, len(codes))
    for index, ts_code in enumerate(codes):
        result = _safe_sync_raw("stk_limit", {"trade_date": trade_date, "ts_code": ts_code})
        rows_read = int(result.get("rows_read") or 0)
        results.append({"ts_code": ts_code, "ok": bool(result.get("ok")) and rows_read > 0, "rows_read": rows_read, "result": result})
        if bool(result.get("ok")) and rows_read > 0:
            successes += 1
        if index + 1 == probe_count and successes == 0:
            from app.data_platform.services.tencent_limit_fallback import repair_stk_limit_from_tencent

            try:
                fallback = repair_stk_limit_from_tencent(trade_date)
            except Exception as exc:  # noqa: BLE001
                fallback = {
                    "ok": False,
                    "status": "fallback_request_failed",
                    "error": str(exc)[:500],
                }
            fallback_coverage = _stk_limit_stock_coverage(trade_date)
            if fallback.get("ok") and fallback_coverage.get("ready"):
                return {
                    "ok": True,
                    "status": "completed_by_verified_fallback",
                    "category": "complete",
                    "codes": codes[:probe_count],
                    "results": results,
                    "fallback": fallback,
                    "coverage": fallback_coverage,
                    "cooldown": False,
                    "cooldown_seconds": 0,
                }
            coverage = _stk_limit_stock_coverage(trade_date)
            return {
                "ok": False,
                "status": "upstream_not_ready",
                "category": "upstream_not_ready",
                "codes": codes[:probe_count],
                "results": results,
                "fallback": fallback,
                "coverage": coverage,
                "cooldown": True,
                "cooldown_seconds": 600,
                "error": "缺失股票的精确 stk_limit 探针仍为空，等待上游完成当日切片",
            }
    coverage = _stk_limit_stock_coverage(trade_date)
    return {
        "ok": coverage["ready"],
        "status": "completed" if coverage["ready"] else "repair_in_progress",
        "category": "complete" if coverage["ready"] else "repair_in_progress",
        "codes": codes,
        "results": results,
        "succeeded": successes,
        "coverage": coverage,
        "cooldown": not coverage["ready"],
        "cooldown_seconds": 600 if not coverage["ready"] else 0,
    }


def sync_complete_stk_limit_slice(trade_date: str) -> dict[str, Any]:
    """Fetch a full trade-date ``stk_limit`` slice with bounded pagination.

    The endpoint can return an exact maximum-size page containing funds before all
    A-share codes have been covered.  Offset pagination preserves the required
    full-market batch contract and avoids an unbounded per-stock fallback.
    """

    pages: list[dict[str, Any]] = []
    rule_fallback: dict[str, Any] | None = None
    previous_retained = _stk_limit_stock_coverage(trade_date)["retained_rows"]
    saw_full_page = False
    for page_index in range(STK_LIMIT_MAX_PAGES):
        params = {
            "trade_date": trade_date,
            "limit": STK_LIMIT_PAGE_SIZE,
            "offset": page_index * STK_LIMIT_PAGE_SIZE,
        }
        result = _safe_sync_raw("stk_limit", params)
        coverage = _stk_limit_stock_coverage(trade_date)
        retained = int(coverage["retained_rows"])
        rows_read = int(result.get("rows_read") or 0)
        page = {
            "page": page_index + 1,
            "offset": params["offset"],
            "rows_read": rows_read,
            "new_retained_rows": max(0, retained - int(previous_retained)),
            "result": result,
            "coverage": coverage,
        }
        pages.append(page)
        if not result.get("ok"):
            if str(result.get("category") or "") == "upstream_not_ready" and page_index == 0:
                from app.data_platform.services.derived_limit_price import (
                    repair_stk_limit_from_exchange_rules,
                )

                rule_fallback = repair_stk_limit_from_exchange_rules(trade_date)
                fallback_coverage = _stk_limit_stock_coverage(trade_date)
                if rule_fallback.get("ok") and fallback_coverage.get("ready"):
                    return {
                        "ok": True,
                        "api": "stk_limit",
                        "trade_date": trade_date,
                        "category": "complete",
                        "pages": pages,
                        "rule_fallback": rule_fallback,
                        "coverage": fallback_coverage,
                        "rows_read": int(rule_fallback.get("requested") or 0),
                        "rows_written": int(rule_fallback.get("rows_written") or 0),
                    }
            return {
                "ok": False,
                "api": "stk_limit",
                "trade_date": trade_date,
                "category": str(result.get("category") or "upstream_failure"),
                "pages": pages,
                "coverage": coverage,
                "cooldown": bool(result.get("cooldown")),
                "cooldown_seconds": int(result.get("cooldown_seconds") or 0),
                "error": str(result.get("error") or "stk_limit page failed")[:500],
                "rule_fallback": rule_fallback,
            }
        if coverage["ready"]:
            return {
                "ok": True,
                "api": "stk_limit",
                "trade_date": trade_date,
                "category": "complete",
                "pages": pages,
                "coverage": coverage,
                "rows_read": sum(int(item["rows_read"]) for item in pages),
                "rows_written": sum(int((item["result"] or {}).get("rows_written") or 0) for item in pages),
            }
        if rows_read >= STK_LIMIT_PAGE_SIZE:
            saw_full_page = True
        if page_index > 0 and rows_read >= STK_LIMIT_PAGE_SIZE and retained <= int(previous_retained):
            gap_repair = sync_stk_limit_gap_batch(trade_date)
            if gap_repair.get("coverage", {}).get("ready"):
                return {
                    "ok": True,
                    "api": "stk_limit",
                    "trade_date": trade_date,
                    "category": "complete",
                    "pages": pages,
                    "gap_repair": gap_repair,
                    "coverage": gap_repair["coverage"],
                }
            return {
                "ok": False,
                "api": "stk_limit",
                "trade_date": trade_date,
                # A repeated full page with no newly retained stock rows is a
                # pagination/truncation signal, not proof that the upstream
                # has not published the whole date.
                "category": "truncated" if retained <= int(previous_retained)
                else str(gap_repair.get("category") or "truncated"),
                "diagnostic_category": "pagination_truncated" if retained <= int(previous_retained)
                else str(gap_repair.get("category") or "truncated"),
                "pages": pages,
                "gap_repair": gap_repair,
                "coverage": gap_repair.get("coverage") or coverage,
                "cooldown": bool(gap_repair.get("cooldown", True)),
                "cooldown_seconds": int(gap_repair.get("cooldown_seconds") or 600),
                "error": str(gap_repair.get("error") or "stk_limit 分页被截断，缺失股票进入有界精确补齐"),
            }
        previous_retained = retained
        if rows_read < STK_LIMIT_PAGE_SIZE:
            return {
                "ok": False,
                "api": "stk_limit",
                "trade_date": trade_date,
                "category": "incomplete_slice",
                "pages": pages,
                "coverage": coverage,
                "cooldown": True,
                "cooldown_seconds": 600,
                "error": "stk_limit 已到分页末尾，但 A 股覆盖率仍未达到生产门",
            }
    coverage = _stk_limit_stock_coverage(trade_date)
    return {
        "ok": False,
        "api": "stk_limit",
        "trade_date": trade_date,
        "category": "truncated" if saw_full_page else "incomplete_slice",
        "pages": pages,
        "coverage": coverage,
        "error": f"stk_limit 在有界 {STK_LIMIT_MAX_PAGES} 页内未达到生产覆盖率",
    }


def _sync_tushare_market_bundle(trade_date: str, *, include_suspend: bool = False, apis: list[str] | None = None) -> dict[str, Any]:
    """Run the TuShare provider without controlling provider selection."""

    # Suspension/resumption and money flow are core daily inputs, not optional
    # enrichments. Include both in every normal market update so an empty
    # result is also persisted as evidence that the date was checked.
    requested_apis = list(apis or ["daily", "daily_basic", "stk_limit", "adj_factor", "suspend_d", "moneyflow", "moneyflow_hsgt"])
    if include_suspend and "suspend_d" not in requested_apis:
        requested_apis.append("suspend_d")
    allowed = {"daily", "daily_basic", "stk_limit", "adj_factor", "suspend_d", "moneyflow", "moneyflow_hsgt"}
    requested_apis = [api for api in requested_apis if api in allowed]
    results = []
    for api in requested_apis:
        if api == "stk_limit":
            result = sync_complete_stk_limit_slice(trade_date)
        else:
            result = _safe_sync_raw(api, {"trade_date": trade_date})
        results.append(result)
        if api == "stk_limit" and not result.get("ok"):
            # The limit-price hard gate already blocks the snapshot.  Do not
            # repeatedly pull adj-factor, suspension and money-flow datasets
            # while this same core slice is waiting or under repair.
            break
    snapshot = build_stock_daily_snapshot(trade_date)
    failure_categories = [str(item.get("category") or "") for item in results if not item.get("ok")]
    return {
        "ok": all(bool(x.get("ok")) for x in results),
        "provider": SOURCE_TUSHARE,
        "trade_date": trade_date,
        "apis": requested_apis,
        "results": results,
        "snapshot": snapshot,
        "failure_category": (
            "pagination_truncated"
            if "pagination_truncated" in failure_categories
            else ("truncated" if "truncated" in failure_categories else (failure_categories[0] if failure_categories else ""))
        ),
    }


def _decision_core_ready(trade_date: str) -> tuple[bool, dict[str, Any]]:
    from app.data_platform.services.decision_data_contract import decision_data_contract

    snapshot = build_stock_daily_snapshot(trade_date)
    with _conn() as conn:
        contract = decision_data_contract(trade_date, conn=conn)
    return bool(contract.get("ok")), {"snapshot": snapshot, "decision_contract": contract}


def sync_market_bundle(trade_date: str, *, include_suspend: bool = False, apis: list[str] | None = None) -> dict[str, Any]:
    """Probe first, start immediately, then hedge a slow bulk with another published source."""
    if apis is not None:
        return _sync_tushare_market_bundle(trade_date, include_suspend=include_suspend, apis=apis)

    from app.data_platform.services.market_data_discovery import (
        available_bulk_candidates,
        discover_market_core,
        hedge_decision,
        mark_provider_bulk_result,
    )
    from app.data_platform.services.market_bulk_execution import (
        begin_execution,
        cancel_requested as bulk_cancel_requested,
        execution_status as bulk_execution_status,
        finish_execution,
        request_cancel as request_bulk_cancel,
    )
    from app.data_platform.services.tencent_market_snapshot import sync_tencent_market_bundle

    def _tracked_tushare_job(dataset: str, job: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = time.monotonic()
        begin_execution(trade_date, SOURCE_TUSHARE, dataset, 1, evidence={"single_call_provider": True})
        if bulk_cancel_requested(trade_date, SOURCE_TUSHARE, dataset):
            return {"ok": False, "status": "cancelled_before_request"}
        try:
            result = dict(job())
        except Exception as exc:  # noqa: BLE001
            finish_execution(
                trade_date, SOURCE_TUSHARE, dataset, status="failed",
                elapsed_ms=int((time.monotonic() - started) * 1000), evidence={"error": str(exc)[:300]},
            )
            raise
        finish_execution(
            trade_date, SOURCE_TUSHARE, dataset,
            status="validated" if result.get("ok") else "failed",
            elapsed_ms=int((time.monotonic() - started) * 1000), evidence={
                "status": result.get("status"),
                "rows_received": int(result.get("received") or result.get("rows_read") or 0),
            },
        )
        return result

    default_apis = ["daily", "daily_basic", "stk_limit", "adj_factor", "suspend_d", "moneyflow", "moneyflow_hsgt"]
    existing_ready, existing_evidence = _decision_core_ready(trade_date)
    if existing_ready:
        return {
            "ok": True,
            "trade_date": trade_date,
            "apis": default_apis,
            "status": "canonical_slice_already_ready",
            "snapshot": existing_evidence["snapshot"],
            "decision_contract": existing_evidence["decision_contract"],
            "policy": "skip_probe_and_bulk_for_complete_canonical_slice",
        }
    existing_contract = dict(existing_evidence.get("decision_contract") or {})
    needed_apis = sorted(set(existing_contract.get("missing_apis") or ()) | set(existing_contract.get("incomplete_apis") or ()))
    discovery = discover_market_core(trade_date, needed_apis or None)
    if not discovery.get("ok"):
        core_ready, evidence = _decision_core_ready(trade_date)
        return {
            "ok": core_ready,
            "trade_date": trade_date,
            "apis": default_apis,
            "provider_discovery_contract": "alis_market_publication_probe_v1",
            "discovery": discovery,
            "snapshot": evidence["snapshot"],
            "decision_contract": evidence["decision_contract"],
            "failure_category": "upstream_not_published" if not core_ready else "",
            "policy": "probe_all_due_sources_then_bulk_fetch_first_publisher_only",
        }

    winners = dict(discovery.get("winners") or {})
    tencent_apis = sorted(api for api, provider in winners.items() if provider == "tencent_market_snapshot")
    jobs: list[tuple[str, list[str], Callable[[], dict[str, Any]]]] = []
    if tencent_apis:
        jobs.append((
            "tencent_market_snapshot",
            tencent_apis,
            lambda: sync_tencent_market_bundle(trade_date, tencent_apis),
        ))
    for api, provider in winners.items():
        if provider != SOURCE_TUSHARE:
            continue
        if api == "stk_limit":
            jobs.append((f"tushare:{api}", [api], lambda api_name=api: _tracked_tushare_job(
                api_name, lambda: sync_complete_stk_limit_slice(trade_date)
            )))
        else:
            jobs.append((
                f"tushare:{api}",
                [api],
                lambda api_name=api: _tracked_tushare_job(
                    api_name, lambda: _safe_sync_raw(api_name, {"trade_date": trade_date})
                ),
            ))

    provider_results: list[dict[str, Any]] = []
    hedge_started: list[dict[str, Any]] = []
    completed_evidence: dict[str, Any] | None = None
    if jobs:
        # Do not use a context manager here: returning as soon as the canonical contract is
        # complete must not wait for the losing hedge.  Its late result is reconciliation-only.
        executor = ThreadPoolExecutor(max_workers=max(4, len(jobs) + 3), thread_name_prefix="market-bulk")
        pending: dict[Any, tuple[str, list[str], float]] = {
            executor.submit(job): (name, datasets, time.monotonic()) for name, datasets, job in jobs
        }
        launched = {(dataset, "tencent_market_snapshot" if name == "tencent_market_snapshot" else SOURCE_TUSHARE)
                    for name, datasets, _job in jobs for dataset in datasets}
        bulk_started_at = time.monotonic()
        hard_deadline = bulk_started_at + max(30.0, float(os.getenv("MARKET_BULK_HARD_TIMEOUT_SECONDS", "120")))

        def _launch_hedges(*, force: bool = False) -> None:
            for dataset in winners:
                for candidate in available_bulk_candidates(dataset, trade_date):
                    provider = str(candidate.get("provider") or "")
                    if not provider or (dataset, provider) in launched:
                        continue
                    primary_provider = str(winners.get(dataset) or "")
                    primary_scope = "a_share_close" if primary_provider == "tencent_market_snapshot" else dataset
                    progress = bulk_execution_status(trade_date, primary_provider, primary_scope)
                    decision = hedge_decision(
                        dataset, primary_provider, provider,
                        elapsed_ms=int((time.monotonic() - bulk_started_at) * 1000),
                        primary_progress=progress, force=force,
                    )
                    if not decision.get("start"):
                        continue
                    if provider == "tencent_market_snapshot":
                        job = lambda dataset_name=dataset: sync_tencent_market_bundle(trade_date, [dataset_name])
                    elif provider == SOURCE_TUSHARE and dataset == "stk_limit":
                        job = lambda dataset_name=dataset: _tracked_tushare_job(
                            dataset_name, lambda: sync_complete_stk_limit_slice(trade_date)
                        )
                    elif provider == SOURCE_TUSHARE:
                        job = lambda dataset_name=dataset: _tracked_tushare_job(
                            dataset_name, lambda: _safe_sync_raw(dataset_name, {"trade_date": trade_date})
                        )
                    else:
                        continue
                    pending[executor.submit(job)] = (f"hedge:{provider}:{dataset}", [dataset], time.monotonic())
                    launched.add((dataset, provider))
                    hedge_started.append({
                        "dataset": dataset, "provider": provider,
                        "reason": decision.get("reason"),
                        "eta_decision": decision,
                        "performance": candidate.get("performance") or {},
                    })
                    break

        while pending:
            if time.monotonic() >= hard_deadline:
                for future in pending:
                    pending_name, pending_datasets, _pending_started = pending[future]
                    pending_provider = "tencent_market_snapshot" if "tencent_market_snapshot" in pending_name else SOURCE_TUSHARE
                    pending_scope = "a_share_close" if pending_provider == "tencent_market_snapshot" else pending_datasets[0]
                    request_bulk_cancel(
                        trade_date, pending_provider, pending_scope, reason="bulk_hard_timeout",
                    )
                    future.cancel()
                provider_results.append({
                    "ok": False, "status": "bulk_hard_timeout",
                    "bulk_latency_ms": int((time.monotonic() - bulk_started_at) * 1000),
                })
                break
            timeout = min(1.0, max(0.0, hard_deadline - time.monotonic()))
            done, _ = wait(set(pending), timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                _launch_hedges()
                continue
            for future in done:
                name, datasets, started = pending.pop(future)
                latency_ms = max(0, int((time.monotonic() - started) * 1000))
                try:
                    result = dict(future.result())
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "status": "bulk_fetch_failed", "error": str(exc)[:300]}
                result.setdefault("provider_job", name)
                result["bulk_latency_ms"] = latency_ms
                provider_results.append(result)
                provider = "tencent_market_snapshot" if "tencent_market_snapshot" in name else SOURCE_TUSHARE
                received = int(result.get("received") or result.get("rows_read") or 0)
                expected = int(result.get("expected") or received or 0)
                coverage = received / expected if expected else (1.0 if result.get("ok") else 0.0)
                for dataset in datasets:
                    mark_provider_bulk_result(
                        dataset, trade_date, provider, ok=bool(result.get("ok")), latency_ms=latency_ms,
                        coverage=coverage,
                        evidence={"status": result.get("status"), "rows": received, "hedged": name.startswith("hedge:")},
                    )
            ready_now, _ready_evidence = _decision_core_ready(trade_date)
            if ready_now:
                completed_evidence = _ready_evidence
                for future in pending:
                    pending_name, pending_datasets, _pending_started = pending[future]
                    pending_provider = "tencent_market_snapshot" if "tencent_market_snapshot" in pending_name else SOURCE_TUSHARE
                    pending_scope = "a_share_close" if pending_provider == "tencent_market_snapshot" else pending_datasets[0]
                    request_bulk_cancel(
                        trade_date, pending_provider, pending_scope,
                        reason="another_provider_completed_canonical_contract",
                    )
                    future.cancel()
                break
            if not pending:
                _launch_hedges(force=True)
        executor.shutdown(wait=False, cancel_futures=True)

    if completed_evidence is not None:
        core_ready, evidence = True, completed_evidence
    else:
        core_ready, evidence = _decision_core_ready(trade_date)
    if core_ready:
        def _enrich() -> None:
            for api_name in ("adj_factor", "suspend_d", "moneyflow", "moneyflow_hsgt"):
                _safe_sync_raw(api_name, {"trade_date": trade_date})

        threading.Thread(target=_enrich, daemon=True, name=f"market-enrichment-{trade_date}").start()
    return {
        "ok": core_ready,
        "trade_date": trade_date,
        "apis": default_apis,
        "provider_discovery_contract": "alis_market_publication_probe_v1",
        "bulk_contract": "alis_market_progress_aware_hedged_checkpoint_v3",
        "discovery": discovery,
        "winners": winners,
        "provider_results": provider_results,
        "hedge_started": hedge_started,
        "snapshot": evidence["snapshot"],
        "decision_contract": evidence["decision_contract"],
        "failure_category": "" if core_ready else "bulk_slice_contract_failed",
        "field_source_policy": {
            "daily": "first_full_contract_source; tushare_late_reconciliation",
            "stk_limit": "first_full_contract_source; exact_limit_cross_check",
            "daily_basic": "fast_source_provisional; tushare_field_definitions_authoritative_on_reconciliation",
            "adj_factor": "tushare_only_until_pit_equivalent_source_exists",
        },
        "policy": "bounded_batches_with_checkpoint_resume; hedge_only_on_eta_advantage_or_stall; cancel_unstarted_loser_batches",
    }


def sync_capital_structure_bundle(trade_date: str) -> dict[str, Any]:
    """Sync the high-permission capital, leverage and leader-seat evidence for one date."""
    requests: list[tuple[str, dict[str, Any]]] = [
        ("moneyflow_hsgt", {"trade_date": trade_date}),
        ("margin", {"trade_date": trade_date}),
        ("margin_detail", {"trade_date": trade_date}),
        ("top_list", {"trade_date": trade_date}),
        ("top_inst", {"trade_date": trade_date}),
        ("hk_hold", {"trade_date": trade_date, "exchange": "SH"}),
        ("hk_hold", {"trade_date": trade_date, "exchange": "SZ"}),
        ("margin_secs", {"trade_date": trade_date, "exchange": "SSE"}),
        ("margin_secs", {"trade_date": trade_date, "exchange": "SZSE"}),
        ("margin_secs", {"trade_date": trade_date, "exchange": "BSE"}),
    ]
    results = [
        {"api_name": api_name, "request": params, **_safe_sync_raw(api_name, params)}
        for api_name, params in requests
    ]
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "trade_date": trade_date,
        "contract": "capital_structure_vip_account_v1",
        "results": results,
        "succeeded": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
    }


def _request_is_complete(api_name: str, params: dict[str, Any]) -> bool:
    """Return whether this exact parameter slice already reached a terminal state."""
    request_key = _request_key(api_name, params, None)
    with _conn() as c:
        row = c.execute(
            """SELECT 1 FROM dc_tushare_request_coverage
               WHERE request_key=? AND result_status IN
               ('success','not_applicable','valid_empty','deferred_empty')
               LIMIT 1""",
            (request_key,),
        ).fetchone()
    return row is not None


def _etf_nav_publish_cutoff(end_date: str, *, lag_open_sessions: int = 2) -> str:
    """Latest trade date whose ETF NAV is expected to be published."""
    if not (len(str(end_date)) == 8 and str(end_date).isdigit()):
        return ""
    with _conn() as c:
        rows = c.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE exchange='SSE' AND is_open=1 AND cal_date<=?
               ORDER BY cal_date DESC LIMIT ?""",
            (str(end_date), max(1, int(lag_open_sessions) + 1)),
        ).fetchall()
    dates = [str(dict(row).get("cal_date") or "") for row in rows]
    if not dates:
        return ""
    index = min(max(0, int(lag_open_sessions)), len(dates) - 1)
    return dates[index]


def _bulk_mark_requests_valid_empty(
    api_name: str,
    records: list[tuple[int, dict[str, Any], int | None]],
    *,
    reason: str,
) -> int:
    if not records:
        return 0
    now = _now()
    coverage_rows = []
    resolved_rows = []
    request_keys = []
    for request_id, params, limit_rows in records:
        key = _request_key(api_name, params, limit_rows)
        request_keys.append(key)
        coverage_rows.append((
            key, api_name, str(params.get("ts_code") or ""), str(params.get("trade_date") or ""),
            str(params.get("period") or params.get("enddate") or ""),
            json.dumps(params, ensure_ascii=False, sort_keys=True), 0, "valid_empty", reason[:500], now,
        ))
        resolved_rows.append((now, int(request_id)))
    with _conn() as c:
        coverage_sql = """INSERT INTO dc_tushare_request_coverage
               (request_key,api_name,ts_code,trade_date,period,params_json,rows_read,result_status,result_reason,checked_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_key) DO UPDATE SET rows_read=excluded.rows_read,result_status=excluded.result_status,
                   result_reason=excluded.result_reason,checked_at=excluded.checked_at"""
        resolved_sql = """UPDATE dc_tushare_failed_requests
                               SET status='resolved',resolved_at=?
                             WHERE id=? AND status='pending'"""
        cursor = c.raw.cursor() if bool(getattr(c, "_is_postgres", False)) else c.raw
        cursor.executemany(coverage_sql.replace("?", "%s") if bool(getattr(c, "_is_postgres", False)) else coverage_sql, coverage_rows)
        cursor.executemany(resolved_sql.replace("?", "%s") if bool(getattr(c, "_is_postgres", False)) else resolved_sql, resolved_rows)
        updated_count = getattr(cursor, "rowcount", -1)
    for key in request_keys:
        redis_set_remove("alis:data-platform:tushare:failed", key)
    # A concurrent retry/worker may have changed a row after the eligibility
    # scan.  Report only rows the terminal update actually touched.
    return len(records) if not isinstance(updated_count, int) or updated_count < 0 else updated_count


def _business_today(*, now: datetime | None = None) -> date:
    """Return today's date under ALIS's UTC+8 business-time contract."""
    current = now or datetime.now(_BUSINESS_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_BUSINESS_TZ)
    return current.astimezone(_BUSINESS_TZ).date()


def _local_business_date_string(*, now: datetime | None = None) -> str:
    """Return today's date in ALIS's fixed Asia/Shanghai business timezone."""
    return _business_today(now=now).strftime("%Y%m%d")


def resolve_mature_fund_nav_empty_requests(*, limit: int = 500) -> dict[str, Any]:
    """Close historical ETF NAV empties after the publication lag has elapsed."""
    cutoff = _etf_nav_publish_cutoff(_local_business_date_string())
    cap = max(1, min(int(limit), 2_000))
    with _conn() as c:
        rows = c.execute(
            """SELECT id,params_json,limit_rows,attempts
                 FROM dc_tushare_failed_requests
                WHERE api_name='fund_nav' AND status='pending' AND error_category='empty_pending'
                ORDER BY last_failed_at,id LIMIT ?""",
            (cap,),
        ).fetchall()
    eligible: list[tuple[int, dict[str, Any], int | None]] = []
    skipped = 0
    for raw in rows:
        item = dict(raw)
        try:
            params = json.loads(item.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            skipped += 1
            continue
        end_date = str(params.get("end_date") or params.get("nav_date") or "")
        if not cutoff or len(end_date) != 8 or not end_date.isdigit() or end_date > cutoff:
            skipped += 1
            continue
        eligible.append((int(item["id"]), params, item.get("limit_rows")))
    resolved = _bulk_mark_requests_valid_empty(
        "fund_nav", eligible, reason=f"mature_empty_after_nav_publish_cutoff_{cutoff}",
    )
    return {
        "ok": True, "status": "completed", "cutoff": cutoff,
        "examined": len(rows), "resolved": resolved, "skipped": skipped,
        "policy": "fund_nav_empty_only_after_two_open_session_publication_lag",
    }


def resolve_mature_optional_daily_empty_requests(*, limit: int = 100) -> dict[str, Any]:
    """Close historical optional daily enrichments without assigning a numeric value."""
    cutoff = _etf_nav_publish_cutoff(_local_business_date_string())
    cap = max(1, min(int(limit), 500))
    with _conn() as c:
        rows = c.execute(
            """SELECT id,api_name,params_json,limit_rows
                 FROM dc_tushare_failed_requests
                WHERE api_name='moneyflow_hsgt' AND status='pending'
                  AND error_category IN ('empty_pending','upstream_not_ready')
                ORDER BY last_failed_at,id LIMIT ?""",
            (cap,),
        ).fetchall()
    eligible: list[tuple[int, dict[str, Any], int | None]] = []
    skipped = 0
    for raw in rows:
        item = dict(raw)
        try:
            params = json.loads(item.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            skipped += 1
            continue
        target_date = str(params.get("trade_date") or params.get("end_date") or "")
        if not cutoff or len(target_date) != 8 or not target_date.isdigit() or target_date > cutoff:
            skipped += 1
            continue
        eligible.append((int(item["id"]), params, item.get("limit_rows")))
    resolved = _bulk_mark_requests_valid_empty(
        "moneyflow_hsgt", eligible,
        reason=f"optional_daily_empty_after_publication_cutoff_{cutoff}",
    )
    return {
        "ok": True, "status": "completed", "cutoff": cutoff,
        "examined": len(rows), "resolved": resolved, "skipped": skipped,
        "policy": "optional_moneyflow_hsgt_empty_only_after_two_open_session_lag",
    }


def backfill_capital_structure_history(
    *, start_date: str, end_date: str, cursor: str = "", limit_dates: int = 10
) -> dict[str, Any]:
    """Backfill a resumable slice of capital-structure history in ascending order.

    ``hk_hold`` stopped publishing daily holdings after the disclosure change
    effective 2024-08-19, so post-cutoff dates are intentionally not requested.
    Exact request keys (including exchange) are used for resume safety.
    """
    if not (len(start_date) == len(end_date) == 8 and start_date.isdigit() and end_date.isdigit()):
        raise ValueError("start_date/end_date must be YYYYMMDD")
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    limit_dates = max(1, min(int(limit_dates), 60))
    with _conn() as c:
        rows = c.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN ? AND ?
                 AND cal_date>?
               ORDER BY cal_date ASC LIMIT ?""",
            (start_date, end_date, cursor, limit_dates),
        ).fetchall()
    trade_dates = [str(dict(row).get("cal_date") or "") for row in rows]
    pending_requests: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    for trade_date in trade_dates:
        requests: list[tuple[str, dict[str, Any]]] = [
            ("moneyflow_hsgt", {"trade_date": trade_date}),
            ("margin", {"trade_date": trade_date}),
            ("margin_detail", {"trade_date": trade_date}),
            ("top_list", {"trade_date": trade_date}),
            ("top_inst", {"trade_date": trade_date}),
            ("margin_secs", {"trade_date": trade_date, "exchange": "SSE"}),
            ("margin_secs", {"trade_date": trade_date, "exchange": "SZSE"}),
        ]
        # Beijing Stock Exchange margin trading launched on 2023-02-13;
        # requesting BSE eligibility before that date only manufactures gaps.
        if trade_date >= "20230213":
            requests.append(("margin_secs", {"trade_date": trade_date, "exchange": "BSE"}))
        if trade_date < "20240819":
            requests.extend([
                ("hk_hold", {"trade_date": trade_date, "exchange": "SH"}),
                ("hk_hold", {"trade_date": trade_date, "exchange": "SZ"}),
            ])
        for api_name, params in requests:
            if _request_is_complete(api_name, params):
                skipped += 1
                continue
            pending_requests.append((api_name, params))
    def fetch_one(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        api_name, params = item
        return {"api_name": api_name, "request": params, **_safe_sync_raw(api_name, params)}

    # This bundle contains several high-volume APIs. Keep the aggregate request
    # rate below the VIP account's observed limit even if the global worker
    # setting is tuned higher for lighter endpoints.
    results = []
    workers = 0
    for offset in range(0, len(pending_requests), 4):
        chunk_results, chunk_workers = _parallel_tushare_map(
            pending_requests[offset : offset + 4],
            fetch_one,
            prefix="tushare-capital-history",
        )
        results.extend(chunk_results)
        workers = max(workers, chunk_workers)
    next_cursor = trade_dates[-1] if trade_dates else cursor
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "start_date": start_date,
        "end_date": end_date,
        "cursor": next_cursor,
        "dates_processed": len(trade_dates),
        "has_more": bool(trade_dates) and next_cursor < end_date,
        "requests_executed": len(results),
        "requests_skipped": skipped,
        "concurrency": workers,
        "rows_written": sum(int(item.get("rows_written") or 0) for item in results),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
    }


def backfill_etf_price_history(
    *, start_date: str, end_date: str, cursor: str = "", limit_funds: int = 20
) -> dict[str, Any]:
    """Backfill listed ETF price and NAV histories with an exact fund cursor."""
    if not (len(start_date) == len(end_date) == 8 and start_date.isdigit() and end_date.isdigit()):
        raise ValueError("start_date/end_date must be YYYYMMDD")
    limit_funds = max(1, min(int(limit_funds), 100))
    with _conn() as c:
        rows = c.execute(
            """SELECT ts_code,payload_json FROM dc_tushare_raw_records
               WHERE api_name='fund_basic' AND ts_code>? ORDER BY ts_code ASC""",
            (cursor,),
        ).fetchall()
    funds: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        raw_payload = item.get("payload_json") or {}
        if isinstance(raw_payload, dict):
            payload = raw_payload
        else:
            try:
                payload = json.loads(str(raw_payload))
            except json.JSONDecodeError:
                continue
        name = str(payload.get("name") or "")
        list_date = str(payload.get("list_date") or "")
        delist_date = str(payload.get("delist_date") or "")
        if "ETF" not in name.upper() or not list_date or list_date > end_date:
            continue
        if delist_date and delist_date < start_date:
            continue
        funds.append({
            "ts_code": str(item.get("ts_code") or payload.get("ts_code") or ""),
            "name": name,
            "list_date": list_date,
        })
        if len(funds) >= limit_funds:
            break
    pending_requests: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    for fund in funds:
        effective_start = max(start_date, fund["list_date"])
        for api_name in ("fund_daily", "fund_nav"):
            params = {"ts_code": fund["ts_code"], "start_date": effective_start, "end_date": end_date}
            if _request_is_complete(api_name, params):
                skipped += 1
            else:
                pending_requests.append((api_name, params))

    def fetch_one(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        api_name, params = item
        return {"api_name": api_name, "request": params, **_safe_sync_raw(api_name, params)}

    results: list[dict[str, Any]] = []
    workers = 0
    for offset in range(0, len(pending_requests), 4):
        chunk_results, chunk_workers = _parallel_tushare_map(
            pending_requests[offset : offset + 4], fetch_one, prefix="tushare-etf-history"
        )
        results.extend(chunk_results)
        workers = max(workers, chunk_workers)
    next_cursor = funds[-1]["ts_code"] if funds else cursor
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "start_date": start_date,
        "end_date": end_date,
        "cursor": next_cursor,
        "funds_processed": len(funds),
        "has_more": len(funds) == limit_funds,
        "requests_executed": len(results),
        "requests_skipped": skipped,
        "concurrency": workers,
        "rows_written": sum(int(item.get("rows_written") or 0) for item in results),
        "failed": sum(1 for item in results if not item.get("ok")),
        "funds": funds,
        "results": results,
    }


def backfill_etf_daily_incremental(
    *, start_date: str, end_date: str, cursor: str = "", limit_dates: int = 10, nav_limit_funds: int = 100
) -> dict[str, Any]:
    """Incrementally sync ETF prices by trade date instead of by fund code."""
    if not (len(start_date) == len(end_date) == 8 and start_date.isdigit() and end_date.isdigit()):
        raise ValueError("start_date/end_date must be YYYYMMDD")
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    limit_dates = max(1, min(int(limit_dates), 60))
    nav_limit_funds = max(0, min(int(nav_limit_funds), 300))
    with _conn() as c:
        date_rows = c.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN ? AND ?
                 AND cal_date>?
               ORDER BY cal_date ASC LIMIT ?""",
            (start_date, end_date, cursor, limit_dates),
        ).fetchall()
        fund_rows = c.execute(
            """SELECT ts_code,payload_json FROM dc_tushare_raw_records
               WHERE api_name='fund_basic' ORDER BY ts_code ASC"""
        ).fetchall()
    trade_dates = [str(dict(row).get("cal_date") or "") for row in date_rows]
    nav_end_date = _etf_nav_publish_cutoff(end_date)
    if not trade_dates:
        return {
            "ok": True,
            "start_date": start_date,
            "end_date": end_date,
            "cursor": cursor,
            "dates_processed": 0,
            "has_more": False,
            "requests_executed": 0,
            "requests_skipped": 0,
            "rows_written": 0,
            "failed": 0,
            "nav_publish_cutoff": nav_end_date,
            "results": [],
        }

    funds: list[dict[str, str]] = []
    if nav_limit_funds > 0:
        for row in fund_rows:
            item = dict(row)
            raw_payload = item.get("payload_json") or {}
            if isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                try:
                    payload = json.loads(str(raw_payload))
                except json.JSONDecodeError:
                    continue
            name = str(payload.get("name") or "")
            list_date = str(payload.get("list_date") or "")
            delist_date = str(payload.get("delist_date") or "")
            if "ETF" not in name.upper() or not list_date or list_date > (nav_end_date or trade_dates[-1]):
                continue
            if delist_date and delist_date < trade_dates[0]:
                continue
            ts_code = str(item.get("ts_code") or payload.get("ts_code") or "")
            nav_params = {"ts_code": ts_code, "start_date": max(start_date, list_date), "end_date": nav_end_date}
            if not nav_end_date or nav_params["start_date"] > nav_end_date or _request_is_complete("fund_nav", nav_params):
                continue
            funds.append({"ts_code": ts_code, "list_date": list_date})
            if len(funds) >= nav_limit_funds:
                break

    pending_requests: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    for trade_date in trade_dates:
        daily_params = {"trade_date": trade_date}
        if _request_is_complete("fund_daily", daily_params):
            skipped += 1
        else:
            pending_requests.append(("fund_daily", daily_params))
    for fund in funds:
        nav_params = {"ts_code": fund["ts_code"], "start_date": max(start_date, fund["list_date"]), "end_date": nav_end_date}
        if _request_is_complete("fund_nav", nav_params):
            skipped += 1
        else:
            pending_requests.append(("fund_nav", nav_params))

    def fetch_one(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        api_name, params = item
        return {"api_name": api_name, "request": params, **_safe_sync_raw(api_name, params)}

    results: list[dict[str, Any]] = []
    workers = 0
    for offset in range(0, len(pending_requests), 4):
        chunk_results, chunk_workers = _parallel_tushare_map(
            pending_requests[offset : offset + 4], fetch_one, prefix="tushare-etf-daily"
        )
        results.extend(chunk_results)
        workers = max(workers, chunk_workers)
    next_cursor = trade_dates[-1]
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "start_date": start_date,
        "end_date": end_date,
        "cursor": next_cursor,
        "dates_processed": len(trade_dates),
        "funds_processed_for_nav": len(funds),
        "nav_publish_cutoff": nav_end_date,
        "nav_deferred_unpublished_dates": [date for date in trade_dates if nav_end_date and date > nav_end_date],
        "has_more": next_cursor < end_date,
        "requests_executed": len(results),
        "requests_skipped": skipped,
        "concurrency": workers,
        "rows_written": sum(int(item.get("rows_written") or 0) for item in results),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
    }


MAJOR_NEWS_SOURCES = (
    "新华网", "凤凰财经", "同花顺", "新浪财经", "华尔街见闻",
    "中证网", "财新网", "第一财经", "财联社",
)


def backfill_major_news_history(
    *, start_date: str, end_date: str, cursor: str = "", limit_days: int = 7
) -> dict[str, Any]:
    """Backfill news in adaptive source/time windows without crossing the 800-row cap."""
    try:
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
        after = datetime.strptime(cursor, "%Y%m%d").date() if cursor else start - timedelta(days=1)
    except ValueError as exc:
        raise ValueError("start_date/end_date/cursor must be YYYYMMDD") from exc
    if start > end:
        raise ValueError("start_date must not be after end_date")
    limit_days = max(1, min(int(limit_days), 31))
    first = max(start, after + timedelta(days=1))
    days = [first + timedelta(days=i) for i in range(limit_days) if first + timedelta(days=i) <= end]
    pending_sources: list[str] = []
    skipped = 0
    for source in MAJOR_NEWS_SOURCES:
        daily_params = [
            {
                "src": source,
                "start_date": f"{day.isoformat()} 00:00:00",
                "end_date": f"{day.isoformat()} 23:59:59",
            }
            for day in days
        ]
        if daily_params and all(_request_is_complete("major_news", params) for params in daily_params):
            skipped += len(daily_params)
        else:
            pending_sources.append(source)

    def fetch_source(source: str) -> dict[str, Any]:
        if not days:
            return {"ok": True, "source": source, "results": [], "requests_executed": 0}
        results: list[dict[str, Any]] = []

        def fetch_interval(start_at: datetime, end_at: datetime, depth: int = 0) -> bool:
            params = {
                "src": source,
                "start_date": start_at.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            result = {
                "api_name": "major_news",
                "request": params,
                **_safe_sync_raw("major_news", params),
            }
            results.append(result)
            rows_read = int(result.get("rows_read") or 0)
            duration = (end_at - start_at).total_seconds()
            if result.get("ok") and rows_read >= 800 and duration > 60 and depth < 16:
                midpoint = start_at + (end_at - start_at) / 2
                return (
                    fetch_interval(start_at, midpoint, depth + 1)
                    and fetch_interval(midpoint + timedelta(seconds=1), end_at, depth + 1)
                )
            return bool(result.get("ok")) and rows_read < 800

        complete = fetch_interval(
            datetime.combine(days[0], datetime.min.time()),
            datetime.combine(days[-1], datetime.max.time()).replace(microsecond=0),
        )
        if complete:
            for day in days:
                day_text = day.isoformat()
                _record_request_coverage(
                    "major_news",
                    {
                        "src": source,
                        "start_date": f"{day_text} 00:00:00",
                        "end_date": f"{day_text} 23:59:59",
                    },
                    None,
                    sum(int(item.get("rows_read") or 0) for item in results),
                    result_status="success",
                    result_reason="adaptive_source_window_complete_v1",
                )
        return {
            "ok": complete,
            "source": source,
            "results": results,
            "requests_executed": len(results),
            "rows_written": sum(int(item.get("rows_written") or 0) for item in results),
        }

    source_results, workers = _parallel_tushare_map(
        pending_sources,
        fetch_source,
        prefix="tushare-major-news-adaptive",
    ) if pending_sources else ([], 0)
    results = [
        item
        for source_result in source_results
        for item in source_result.get("results") or []
    ]
    next_cursor = days[-1].strftime("%Y%m%d") if days else cursor
    return {
        "ok": all(bool(item.get("ok")) for item in source_results),
        "start_date": start_date,
        "end_date": end_date,
        "cursor": next_cursor,
        "days_processed": len(days),
        "has_more": bool(days) and days[-1] < end,
        "sources": list(MAJOR_NEWS_SOURCES),
        "requests_executed": sum(int(item.get("requests_executed") or 0) for item in source_results),
        "requests_skipped": skipped,
        "concurrency": workers,
        "rows_written": sum(int(item.get("rows_written") or 0) for item in source_results),
        "failed": sum(1 for item in source_results if not item.get("ok")),
        "source_results": source_results,
        "results": results,
    }


EVENT_RESEARCH_APIS = ("report_rc", "repurchase", "stk_holdertrade")


def backfill_event_research_history(
    *,
    start_date: str,
    end_date: str,
    cursor: str = "",
    limit_months: int = 1,
) -> dict[str, Any]:
    """Backfill point-in-time consensus and quantitative event disclosures.

    Calendar-month slices keep requests resumable and independently audited.
    The upstream may return many forecast-quarter rows for one report, so the
    deterministic report key includes broker, report identity, and quarter.
    """
    try:
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
        after = datetime.strptime(cursor, "%Y%m%d").date() if cursor else start - timedelta(days=1)
    except ValueError as exc:
        raise ValueError("start_date/end_date/cursor must be YYYYMMDD") from exc
    if start > end:
        raise ValueError("start_date must not be after end_date")
    current = max(start, after + timedelta(days=1))
    month_limit = max(1, min(int(limit_months), 6))
    windows: list[tuple[date, date]] = []
    for _ in range(month_limit):
        if current > end:
            break
        next_month = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
        window_end = min(end, next_month - timedelta(days=1))
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    results: list[dict[str, Any]] = []
    skipped = 0
    for window_start, window_end in windows:
        params = {
            "start_date": window_start.strftime("%Y%m%d"),
            "end_date": window_end.strftime("%Y%m%d"),
        }
        for api_name in EVENT_RESEARCH_APIS:
            if _request_is_complete(api_name, params):
                skipped += 1
                continue
            results.append({
                "api_name": api_name,
                "request": params,
                **_safe_sync_raw(api_name, params),
            })
    next_cursor = windows[-1][1].strftime("%Y%m%d") if windows else cursor
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "start_date": start_date,
        "end_date": end_date,
        "cursor": next_cursor,
        "months_processed": len(windows),
        "has_more": bool(windows) and windows[-1][1] < end,
        "apis": list(EVENT_RESEARCH_APIS),
        "requests_executed": len(results),
        "requests_skipped": skipped,
        "rows_written": sum(int(item.get("rows_written") or 0) for item in results),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
    }


def get_vip_backfill_status(
    *, capital_start: str = "20200101", end_date: str, news_start: str = "20200101"
) -> dict[str, Any]:
    """Return request-slice coverage for the three VIP backfill contracts."""
    terminal = {"success", "not_applicable", "valid_empty", "deferred_empty"}
    with _conn() as c:
        date_rows = c.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN ? AND ?
               ORDER BY cal_date""",
            (capital_start, end_date),
        ).fetchall()
        coverage_rows = c.execute(
            """SELECT api_name,params_json,result_status,rows_read
               FROM dc_tushare_request_coverage
               WHERE api_name IN
               ('moneyflow_hsgt','margin','margin_detail','top_list','top_inst','hk_hold','margin_secs',
                'fund_daily','fund_nav','major_news')"""
        ).fetchall()
        fund_rows = c.execute(
            """SELECT ts_code,payload_json FROM dc_tushare_raw_records
               WHERE api_name='fund_basic' ORDER BY ts_code"""
        ).fetchall()
    dates = [str(dict(row).get("cal_date") or "") for row in date_rows]
    nav_end_date = _etf_nav_publish_cutoff(end_date)
    expected_capital: set[str] = set()
    expected_by_api: dict[str, set[str]] = {}
    def expect(api_name: str, params: dict[str, Any]) -> str:
        key = _request_key(api_name, params, None)
        expected_by_api.setdefault(api_name, set()).add(key)
        return key
    capital_keys_by_date: list[tuple[str, set[str]]] = []
    for trade_date in dates:
        requests: list[tuple[str, dict[str, Any]]] = [
            (api, {"trade_date": trade_date})
            for api in ("moneyflow_hsgt", "margin", "margin_detail", "top_list", "top_inst")
        ] + [
            ("margin_secs", {"trade_date": trade_date, "exchange": "SSE"}),
            ("margin_secs", {"trade_date": trade_date, "exchange": "SZSE"}),
        ]
        if trade_date >= "20230213":
            requests.append(("margin_secs", {"trade_date": trade_date, "exchange": "BSE"}))
        if trade_date < "20240819":
            requests.extend([
                ("hk_hold", {"trade_date": trade_date, "exchange": "SH"}),
                ("hk_hold", {"trade_date": trade_date, "exchange": "SZ"}),
            ])
        day_keys = {expect(api, params) for api, params in requests}
        expected_capital.update(day_keys)
        capital_keys_by_date.append((trade_date, day_keys))

    completed_keys: set[str] = set()
    for raw_row in coverage_rows:
        row = dict(raw_row)
        if str(row.get("result_status") or "") not in terminal:
            continue
        try:
            params = json.loads(str(row.get("params_json") or "{}"))
        except json.JSONDecodeError:
            continue
        api_name = str(row.get("api_name") or "")
        key = _request_key(api_name, params, None)
        completed_keys.add(key)

    etf_funds: list[dict[str, str]] = []
    for raw_row in fund_rows:
        row = dict(raw_row)
        raw_payload = row.get("payload_json") or {}
        try:
            payload = raw_payload if isinstance(raw_payload, dict) else json.loads(str(raw_payload))
        except json.JSONDecodeError:
            continue
        name = str(payload.get("name") or "")
        list_date = str(payload.get("list_date") or "")
        delist_date = str(payload.get("delist_date") or "")
        if "ETF" in name.upper() and list_date and list_date <= end_date and (not delist_date or delist_date >= capital_start):
            etf_funds.append({"ts_code": str(row.get("ts_code") or payload.get("ts_code") or ""), "list_date": list_date})

    expected_etf: set[str] = set()
    etf_keys_by_code: list[tuple[str, set[str]]] = []
    etf_daily_keys_by_date: list[tuple[str, set[str]]] = []
    for trade_date in dates:
        daily_keys = {expect("fund_daily", {"trade_date": trade_date})}
        expected_etf.update(daily_keys)
        etf_daily_keys_by_date.append((trade_date, daily_keys))
    for fund in etf_funds:
        fund_keys = set()
        if nav_end_date and fund["list_date"] <= nav_end_date:
            fund_keys.add(expect("fund_nav", {
                "ts_code": fund["ts_code"],
                "start_date": max(capital_start, fund["list_date"]),
                "end_date": nav_end_date,
            }))
        etf_keys_by_code.append((fund["ts_code"], fund_keys))
        expected_etf.update(fund_keys)

    # Count completed solely from coverage table.  Raw-records existence is not
    # a substitute for verified request-slice completion.

    news_days = (datetime.strptime(end_date, "%Y%m%d").date() - datetime.strptime(news_start, "%Y%m%d").date()).days + 1

    expected_news: set[str] = set()
    news_keys_by_day: list[tuple[str, set[str]]] = []
    news_start_day = datetime.strptime(news_start, "%Y%m%d").date()
    for offset in range(max(0, news_days)):
        day = news_start_day + timedelta(days=offset)
        day_text = day.isoformat()
        day_keys = {
            expect("major_news", {
                "src": source,
                "start_date": f"{day_text} 00:00:00",
                "end_date": f"{day_text} 23:59:59",
            })
            for source in MAJOR_NEWS_SOURCES
        }
        expected_news.update(day_keys)
        news_keys_by_day.append((day.strftime("%Y%m%d"), day_keys))

    def resume_cursor(groups: list[tuple[str, set[str]]]) -> str:
        previous = ""
        for value, keys in groups:
            if not keys.issubset(completed_keys):
                return previous
            previous = value
        return previous

    def contract(done: int, expected: int, cursor: str) -> dict[str, Any]:
        return {
            "completed_slices": done,
            "expected_slices": expected,
            "missing_slices": max(0, expected - done),
            "coverage": round(done / expected, 6) if expected else 1.0,
            "complete": done >= expected,
            "resume_cursor": cursor,
        }

    per_api = {
        api_name: contract(len(keys & completed_keys), len(keys), "")
        for api_name, keys in expected_by_api.items()
    }
    per_api["fund_basic"] = contract(1 if etf_funds else 0, 1, "")
    return {
        "ok": True,
        "capital": contract(len(expected_capital & completed_keys), len(expected_capital), resume_cursor(capital_keys_by_date)),
        "etf_price_nav": {
            **contract(len(expected_etf & completed_keys), len(expected_etf), resume_cursor(etf_keys_by_code)),
            "resume_cursor": resume_cursor(etf_daily_keys_by_date),
            "resume_cursor_mode": "trade_date",
            "fund_code_resume_cursor": resume_cursor(etf_keys_by_code),
            "nav_publish_cutoff": nav_end_date,
            "deferred_unpublished_nav_dates": [date for date in dates if nav_end_date and date > nav_end_date],
        },
        "major_news": contract(len(expected_news & completed_keys), len(expected_news), resume_cursor(news_keys_by_day)),
        "datasets": per_api,
        "capabilities": {},
        "scope": {
            "capital_start": capital_start,
            "news_start": news_start,
            "end_date": end_date,
            "trade_dates": len(dates),
            "etfs": len(etf_funds),
            "news_sources": len(MAJOR_NEWS_SOURCES),
        },
    }


def recover_orphaned_ingestion_runs() -> dict[str, Any]:
    """Close ingestion runs orphaned by a process restart.

    Workers run in-process in V1.  A persisted ``running`` record from before
    this API process started cannot still be active, and leaving it running
    permanently hides the retry/continuation path from both operators and the
    data-center UI.  Backfill has its own richer recovery routine, so it is
    intentionally excluded here.
    """
    now = _now()
    message = "服务重启前任务未完成；已标记为中断，可从失败队列或缺口继续"
    with _conn() as c:
        cursor = c.execute(
            """UPDATE data_ingestion_runs
               SET status='interrupted',finished_at=?,error=CASE WHEN error='' THEN ? ELSE error END
               WHERE status='running' AND job_name<>'data_platform.backfill'""",
            (now, message),
        )
        recovered = int(cursor.rowcount or 0)
    return {"ok": True, "recovered": recovered, "at": now}


def sync_financial_bundle(ts_code: str, period: str, *, include_yearly: bool = True, apis_override: list[str] | None = None) -> dict[str, Any]:
    """Sync financial statement/indicator bundle for one stock and report period."""

    apis = list(apis_override or ["income", "balancesheet", "cashflow", "fina_indicator"])
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
    """Sync one report period through the officially supported dimensions.

    Core statements use the whole-market VIP endpoints.  Audit opinions and
    main-business composition require stock codes, so an annual request only
    advances one resumable stock batch instead of sending invalid period-only
    calls to those ordinary endpoints.
    """

    core = sync_financial_vip_period(period)
    yearly = None
    if include_yearly:
        yearly = sync_financial_batch(
            period,
            limit=20,
            include_yearly=True,
            completion_api="fina_audit",
        )
    return {
        "ok": bool(core.get("ok")) and (yearly is None or bool(yearly.get("ok"))),
        "period": period,
        "mode": "vip_period_with_resumable_yearly_extras" if include_yearly else "vip_period",
        "core": core,
        "yearly_extras": yearly,
        "results": core.get("results", []),
    }


def financial_research_completeness(
    ts_code: str, *, periods: list[str] | None = None, period_limit: int = 8,
    as_of_time: str | None = None,
) -> dict[str, Any]:
    """Measure field-level research readiness; row/request coverage is not sufficient."""
    code = str(ts_code or "").strip().upper()
    with _conn() as c:
        master = c.execute("SELECT industry FROM dc_stock_master WHERE ts_code=?", (code,)).fetchone()
        industry = str(dict(master).get("industry") or "") if master else ""
        required_contract = (
            FINANCIAL_SECTOR_RESEARCH_REQUIRED_FIELDS
            if any(label in industry for label in ("银行", "保险", "证券", "多元金融"))
            else FINANCIAL_RESEARCH_REQUIRED_FIELDS
        )
        if periods:
            target_periods = [str(value) for value in periods if str(value)]
        else:
            cutoff_sql = " AND available_time<=?" if as_of_time else ""
            params: tuple[Any, ...] = (code, str(as_of_time), max(1, min(int(period_limit), 12))) if as_of_time else (code, max(1, min(int(period_limit), 12)))
            rows = c.execute(
                """SELECT DISTINCT period FROM dc_tushare_raw_records
                   WHERE api_name='income' AND ts_code=? AND period<>''
                   """ + cutoff_sql + " ORDER BY period DESC LIMIT ?",
                params,
            ).fetchall()
            target_periods = [str(dict(row).get("period") or "") for row in rows]
        details: list[dict[str, Any]] = []
        for period in target_periods:
            for api_name, required in required_contract.items():
                cutoff_sql = " AND available_time<=?" if as_of_time else ""
                params = (api_name, code, period, str(as_of_time)) if as_of_time else (api_name, code, period)
                rows = c.execute(
                    """SELECT payload_json FROM dc_tushare_raw_records
                       WHERE api_name=? AND ts_code=? AND period=?
                       """ + cutoff_sql + " ORDER BY available_time DESC,length(payload_json) DESC,id DESC",
                    params,
                ).fetchall()
                merged: dict[str, Any] = {}
                returned_fields: set[str] = set()
                for row in reversed(rows):
                    try:
                        payload = json.loads(dict(row).get("payload_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    returned_fields.update(str(field) for field in (payload.get("_returned_fields") or []))
                    merged.update({key: value for key, value in payload.items() if value is not None})
                missing = [field for field in required if merged.get(field) is None and field not in returned_fields]
                any_of_fields: list[str] = []
                if required_contract is FINANCIAL_RESEARCH_REQUIRED_FIELDS and api_name == "balancesheet":
                    any_of_fields = ["accounts_receiv", "inventories", "goodwill"]
                    if not any(merged.get(field) is not None or field in returned_fields for field in any_of_fields):
                        missing.append("accounts_receiv|inventories|goodwill")
                elif required_contract is FINANCIAL_RESEARCH_REQUIRED_FIELDS and api_name == "cashflow":
                    any_of_fields = ["free_cashflow", "c_pay_acq_const_fiolta"]
                    if not any(merged.get(field) is not None or field in returned_fields for field in any_of_fields):
                        missing.append("free_cashflow|c_pay_acq_const_fiolta")
                details.append({
                    "period": period, "api": api_name, "required_fields": list(required),
                    "any_of_fields": any_of_fields,
                    "present_fields": [field for field in required if merged.get(field) is not None or field in returned_fields]
                                      + ([next((field for field in any_of_fields if merged.get(field) is not None or field in returned_fields), "")] if any_of_fields and any(merged.get(field) is not None or field in returned_fields for field in any_of_fields) else []),
                    "missing_fields": missing, "complete": not missing,
                })
    checks = len(details)
    complete = sum(1 for item in details if item["complete"])
    required_field_checks = sum(len(item["required_fields"]) + (1 if item.get("any_of_fields") else 0) for item in details)
    present_field_checks = sum(
        sum(1 for field in item["required_fields"] if field in item["present_fields"])
        + (1 if item.get("any_of_fields") and any(field in item["present_fields"] for field in item["any_of_fields"]) else 0)
        for item in details
    )
    return {
        "ok": True, "ts_code": code, "industry": industry, "as_of_time": as_of_time, "periods": target_periods,
        "checks": checks, "complete_checks": complete,
        "coverage": round(present_field_checks / required_field_checks, 4) if required_field_checks else 0.0,
        "field_checks": required_field_checks, "present_field_checks": present_field_checks,
        "complete_check_coverage": round(complete / checks, 4) if checks else 0.0,
        "status": "complete" if checks and complete == checks else "partial" if complete else "missing",
        "details": details,
    }


def financial_research_periods_completeness(periods: list[str]) -> dict[str, dict[str, Any]]:
    """Audit multiple report periods in one raw-data pass."""
    targets = list(dict.fromkeys(str(period or "").strip() for period in periods if str(period or "").strip()))
    if not targets:
        return {}
    apis = tuple(FINANCIAL_RESEARCH_REQUIRED_FIELDS)
    with _conn() as c:
        masters = [dict(row) for row in c.execute(
            "SELECT ts_code,industry,list_date FROM dc_stock_master WHERE ts_code<>''"
        ).fetchall()]
        api_placeholders = ",".join("?" for _ in apis)
        period_placeholders = ",".join("?" for _ in targets)
        rows = c.execute(
            f"""SELECT period,api_name,ts_code,payload_json FROM dc_tushare_raw_records
                WHERE period IN ({period_placeholders}) AND api_name IN ({api_placeholders}) AND ts_code<>''""",
            (*targets, *apis),
        ).fetchall()
        attempted_by_period: dict[str, set[str]] = {target: set() for target in targets}
        for row in c.execute(
            f"""SELECT DISTINCT period,api_name FROM dc_tushare_request_coverage
                WHERE period IN ({period_placeholders}) AND params_json LIKE '%%research_fields_v2%%'
                  AND result_status='success'""",
            tuple(targets),
        ).fetchall():
            item = dict(row)
            attempted_by_period.setdefault(str(item.get("period") or ""), set()).add(str(item.get("api_name") or ""))
    present: dict[tuple[str, str, str], set[str]] = {}
    for raw in rows:
        item = dict(raw)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        fields = {str(key) for key, value in payload.items() if value is not None}
        fields.update(str(value) for value in payload.get("_returned_fields") or [])
        present.setdefault((str(item.get("period") or ""), str(item.get("ts_code") or ""), str(item.get("api_name") or "")), set()).update(fields)
    results: dict[str, dict[str, Any]] = {}
    for target in targets:
        missing_by_api = {api: 0 for api in apis}
        incomplete_symbols: set[str] = set()
        complete_checks = checks = applicable = 0
        for master in masters:
            listed = str(master.get("list_date") or "")
            if listed and listed.isdigit() and listed > target:
                continue
            applicable += 1
            industry = str(master.get("industry") or "")
            contract = FINANCIAL_SECTOR_RESEARCH_REQUIRED_FIELDS if any(label in industry for label in ("银行", "保险", "证券", "多元金融")) else FINANCIAL_RESEARCH_REQUIRED_FIELDS
            code = str(master.get("ts_code") or "")
            for api_name, required in contract.items():
                checks += 1
                available = present.get((target, code, api_name), set())
                complete = all(field in available for field in required)
                if contract is FINANCIAL_RESEARCH_REQUIRED_FIELDS and api_name == "balancesheet":
                    complete = complete and bool(available.intersection({"accounts_receiv", "inventories", "goodwill"}))
                if contract is FINANCIAL_RESEARCH_REQUIRED_FIELDS and api_name == "cashflow":
                    complete = complete and bool(available.intersection({"free_cashflow", "c_pay_acq_const_fiolta"}))
                if complete:
                    complete_checks += 1
                else:
                    missing_by_api[api_name] += 1
                    incomplete_symbols.add(code)
        attempted_apis = attempted_by_period.get(target, set())
        results[target] = {"ok": True, "period": target, "applicable_stocks": applicable, "checks": checks,
                           "complete_checks": complete_checks, "coverage": round(complete_checks / checks, 6) if checks else 0.0,
                           "status": "complete" if checks and complete_checks == checks else "partial" if complete_checks else "missing",
                           "missing_by_api": missing_by_api, "batch_attempted": all(api in attempted_apis for api in apis),
                           "attempted_apis": sorted(attempted_apis), "incomplete_symbols": sorted(incomplete_symbols)}
    return results


def financial_research_period_completeness(period: str) -> dict[str, Any]:
    """Audit field contracts for every stock applicable to one report period."""
    target = str(period or "").strip()
    return financial_research_periods_completeness([target]).get(target, {})


def enrich_financial_research_fields(
    ts_code: str, *, periods: list[str] | None = None, period_limit: int = 8, request_limit: int = 32,
) -> dict[str, Any]:
    """Fetch only incomplete company-period statement APIs through ordinary endpoints."""
    before = financial_research_completeness(ts_code, periods=periods, period_limit=period_limit)
    gaps = [item for item in before["details"] if not item["complete"]][:max(1, min(int(request_limit), 64))]
    results = [
        _safe_sync_raw(item["api"], {"ts_code": before["ts_code"], "period": item["period"]})
        for item in gaps
    ]
    after = financial_research_completeness(ts_code, periods=before["periods"], period_limit=period_limit)
    return {
        "ok": all(bool(item.get("ok")) for item in results) and after["status"] == "complete",
        "ts_code": before["ts_code"], "before": before, "after": after,
        "requested": len(results), "results": results,
    }


def enrich_financial_research_batch(period: str, *, limit: int = 5) -> dict[str, Any]:
    """Advance a resumable field-level enrichment batch for one report period."""
    target_period = str(period or "").strip()
    if len(target_period) != 8 or not target_period.isdigit():
        raise ValueError("period must be YYYYMMDD")
    period_state = financial_research_period_completeness(target_period)
    all_targets = list(period_state.get("incomplete_symbols") or [])
    # Preserve the full-market completion goal while repairing the highest
    # value private data first: holdings, then candidate/core pools, then the
    # remaining market. API availability may disappear before the long tail.
    try:
        from app.db.store import get_positions
        from app.services.decision_store import list_pool_members
        held_bases = {str(item.get("symbol") or "").split(".")[0] for item in get_positions()}
        pool_codes = {
            str(item.get("ts_code") or "")
            for item in list_pool_members("candidate") + list_pool_members("core")
        }
    except Exception:
        held_bases, pool_codes = set(), set()
    all_targets.sort(key=lambda code: (0 if code.split(".")[0] in held_bases else 1 if code in pool_codes else 2, code))
    targets = all_targets[:max(1, min(int(limit), 100))]
    checked = int(period_state.get("applicable_stocks") or 0)

    def enrich(code: str) -> dict[str, Any]:
        try:
            return enrich_financial_research_fields(code, periods=[target_period], request_limit=4)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "ts_code": code, "error": str(exc)[:500]}

    results, workers = _parallel_tushare_map(targets, enrich, prefix="research-field-enrich") if targets else ([], 0)
    completed = sum(1 for item in results if item.get("after", {}).get("status") == "complete")
    return {
        "ok": all(bool(item.get("ok")) for item in results), "period": target_period,
        "checked_symbols": checked, "target_symbols": targets, "batch_size": len(targets),
        "completed": completed, "failed": len(results) - completed, "concurrency": workers,
        "results": results,
    }


def sync_financial_vip_period(period: str, *, contract_mode: str = "core") -> dict[str, Any]:
    """Load a whole report period through TuShare's 5000-point VIP APIs.

    The normal statement endpoints are explicitly single-stock only.  VIP
    endpoints return the whole period in one request; unsupported VIP APIs
    are reported as unavailable so the caller can fall back to stock batches.
    """
    client = get_tushare_client()
    results: list[dict[str, Any]] = []
    vip_fields = {
        "income": "ts_code,ann_date,f_ann_date,end_date,report_type,revenue,total_revenue,operate_profit,total_profit,n_income,n_income_attr_p,basic_eps,diluted_eps",
        "balancesheet": "ts_code,ann_date,f_ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int,money_cap,accounts_receiv,inventories,goodwill,st_borr,lt_borr,bond_payable,non_cur_liab_due_1y",
        "cashflow": "ts_code,ann_date,f_ann_date,end_date,n_cashflow_act,n_cash_flows_inv_act,n_cash_flows_fnc_act,c_pay_acq_const_fiolta",
        "fina_indicator": "ts_code,ann_date,end_date,roe,roe_waa,roa,roic,grossprofit_margin,netprofit_margin,debt_to_assets,current_ratio,quick_ratio,assets_turn,inv_turn,ar_turn,ocf_to_or,ocf_to_profit,q_sales_yoy,q_profit_yoy",
    }
    for api in ("income", "balancesheet", "cashflow", "fina_indicator"):
        vip_api = f"{api}_vip"
        run_id = start_ingestion_run(f"data_platform.{vip_api}", {"period": period, "mode": "vip_batch"})
        try:
            frame = getattr(client.pro, vip_api)(period=period, fields=vip_fields[api])
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise EmptyTushareResult(f"{vip_api} period={period} returned empty")
            frame = frame.copy()
            # VIP results use end_date; our raw store normalizes it to period.
            written = _upsert_raw_records(api, frame)
            finish_ingestion_run(run_id, status="completed", rows_read=len(frame), rows_written=written,
                                 meta={"api": vip_api, "period": period, "mode": "vip_batch"})
            _record_probe(vip_api, "ok", len(frame))
            if contract_mode == "research_fields_v2":
                _record_request_coverage(api, {"period": period, "mode": contract_mode}, None, len(frame))
            _invalidate_asset_views()
            results.append({"api": api, "vip_api": vip_api, "ok": True, "rows": len(frame), "written": written})
        except Exception as exc:  # noqa: BLE001
            finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc),
                                 meta={"api": vip_api, "period": period, "mode": "vip_batch"})
            _record_probe(vip_api, "failed", 0, str(exc))
            results.append({"api": api, "vip_api": vip_api, "ok": False, "rows": 0, "error": str(exc)[:300]})
    succeeded = sum(1 for item in results if item.get("ok"))
    return {"ok": succeeded == len(results), "period": period, "mode": "vip_batch", "contract_mode": contract_mode, "results": results,
            "succeeded": succeeded, "total": len(results)}


def sync_financial_batch(
    period: str,
    *,
    codes: list[str] | None = None,
    limit: int = 20,
    include_yearly: bool = False,
    allow_partial: bool = False,
    completion_api: str = "core",
) -> dict[str, Any]:
    """Sync financial data with VIP-first core loads and resumable fallbacks."""

    ensure_data_center_seeded()
    # Prefer one whole-period request where the account/proxy exposes the VIP
    # endpoint.  This turns thousands of stock calls into at most four calls;
    # the caller retains the ordinary path for VIP APIs that are unavailable.
    api_override = ["fina_audit"] if completion_api == "fina_audit" else None
    if not codes and completion_api == "core":
        vip = sync_financial_vip_period(period)
        vip_ok = {str(x.get("api")) for x in vip.get("results", []) if x.get("ok")}
        vip_failed = [str(x.get("api")) for x in vip.get("results", []) if not x.get("ok")]
        if vip_ok and not vip_failed:
            return {"ok": True, "period": period, "mode": "vip_batch", "total": vip["total"],
                    "success": vip["succeeded"], "failed": vip["total"] - vip["succeeded"],
                    "concurrency": 1, "cooldown": False, "cooldown_seconds": 0,
                    "error": "; ".join(str(x.get("error")) for x in vip["results"] if x.get("error")),
                    "results": vip["results"]}
        if vip_ok:
            with _conn() as c:
                codes = [str(dict(r).get("ts_code") or "") for r in c.execute(
                    "SELECT ts_code FROM dc_stock_master WHERE ts_code<>'' ORDER BY ts_code"
                ).fetchall()]
            api_override = vip_failed
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
            return sync_financial_bundle(
                ts_code,
                period,
                include_yearly=include_yearly and completion_api != "fina_audit",
                apis_override=api_override,
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:500]
            return {"ok": False, "ts_code": ts_code, "period": period, "error": msg, "results": []}

    results, workers = _parallel_tushare_map(target_codes, fetch_one, prefix="tushare-financial")
    cooldown = any(bool(item.get("cooldown")) for result in results for item in (result.get("results") or [result]))
    cooldown_seconds = max(
        [int(item.get("cooldown_seconds") or 0) for result in results for item in (result.get("results") or [result]) if item.get("cooldown")]
        or [0]
    )
    for ts_code, result in zip(target_codes, results):
        result_ok = (
            not cooldown
            and bool(result.get("ok", True))
            and all(bool(item.get("ok")) for item in result.get("results", []))
        )
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
        "ok": (failed_count == 0 or soft_ok) and not cooldown,
        "period": period,
        "started_at": started,
        "finished_at": _now(),
        "total": total,
        "success": ok_count,
        "failed": failed_count,
        "concurrency": workers,
        "cooldown": cooldown,
        "cooldown_seconds": cooldown_seconds,
        "error": "; ".join(errors[:5])[:1000] if errors else "",
        "failed_items": _failed_result_items(results, limit=8),
        "results": results,
    }


def _failed_result_items(results: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for result in results:
        if result.get("ok") and all(bool(x.get("ok", True)) for x in result.get("results", [])):
            continue
        if result.get("error"):
            items.append({
                "ts_code": result.get("ts_code", ""),
                "api": result.get("api", ""),
                "error": str(result.get("error") or "")[:300],
            })
        for sub in result.get("results") or []:
            if isinstance(sub, dict) and not sub.get("ok"):
                items.append({
                    "ts_code": result.get("ts_code") or sub.get("ts_code", ""),
                    "api": sub.get("api", ""),
                    "error": str(sub.get("error") or "failed")[:300],
                })
        if len(items) >= limit:
            return items[:limit]
    return items[:limit]


def _load_stock_codes(limit: int = 20) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ts_code FROM dc_stock_master ORDER BY ts_code ASC LIMIT ?",
            (max(1, int(limit or 20)),),
        ).fetchall()
    return [str(_row_to_dict(row).get("ts_code") or "") for row in rows]


def _load_financial_missing_codes(period: str, limit: int = 20, completion_api: str = "core") -> list[str]:
    """Return the next symbols whose completion API is absent for period.

    Completed request coverage counts as done even when no raw row is written
    (pre-listing, valid empty, or deferred empty).  Empty-pending rows are kept
    eligible, but sorted after never-checked symbols so a few permanent gaps do
    not monopolize every batch.
    """
    allowed_apis = {"core", "income", "fina_audit"}
    if completion_api not in allowed_apis:
        raise ValueError(f"unsupported financial completion API: {completion_api}")
    required = ["income", "balancesheet", "cashflow", "fina_indicator"] if completion_api == "core" else [completion_api]
    placeholders = ",".join("?" for _ in required)
    completed_placeholders = ",".join("?" for _ in COMPLETED_REQUEST_STATUSES)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT s.ts_code
            FROM dc_stock_master s
            LEFT JOIN (
                SELECT ts_code, period, api_name
                FROM dc_tushare_raw_records
                WHERE period=? AND api_name IN ({placeholders})
                UNION
                SELECT ts_code, period, api_name
                FROM dc_tushare_request_coverage
                WHERE period=? AND api_name IN ({placeholders})
                  AND result_status IN ({completed_placeholders})
            ) done ON done.ts_code=s.ts_code
            LEFT JOIN dc_tushare_request_coverage q_any
              ON q_any.ts_code=s.ts_code AND q_any.period=? AND q_any.api_name IN ({placeholders})
            GROUP BY s.ts_code
            HAVING COUNT(DISTINCT done.api_name) < ?
            ORDER BY CASE WHEN COUNT(q_any.api_name) = 0 THEN 0 ELSE 1 END ASC,
                     MAX(CASE WHEN q_any.result_status='empty_pending' THEN 1 ELSE 0 END) ASC,
                     s.ts_code ASC
            LIMIT ?
            """,
            (
                period,
                *required,
                period,
                *required,
                *COMPLETED_REQUEST_STATUSES,
                period,
                *required,
                len(required),
                max(1, int(limit or 20)),
            ),
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
    item = _catalog_item(api_name)
    # Full-market report-period VIP endpoints verified with the active account.
    # Prefer them over 5,000+ ordinary per-stock calls when a period is supplied.
    period_vip = {
        "express": ("express_vip", {}),
        "forecast": ("forecast_vip", {}),
        "fina_mainbz": ("fina_mainbz_vip", {"type": "P"}),
    }
    period_batch = {
        "top10_holders": ("top10_holders", {}),
        "top10_floatholders": ("top10_floatholders", {}),
        "stk_holdernumber": ("stk_holdernumber", {}),
    }
    batch_api = period_vip.get(api_name) or period_batch.get(api_name)
    if batch_api and period:
        vip_api, extra_params = batch_api
        if api_name == "stk_holdernumber":
            try:
                year = int(period[:4])
                extra_params = {"start_date": f"{year - 2}{period[4:]}", "end_date": period}
            except (TypeError, ValueError):
                extra_params = {"end_date": period}
        run_id = start_ingestion_run(f"data_platform.{vip_api}", {"period": period, "mode": "vip_batch"})
        try:
            request_params = {"period": period, **extra_params}
            if api_name == "stk_holdernumber":
                request_params.pop("period", None)
            frame = getattr(get_tushare_client().pro, vip_api)(fields=item.fields, **request_params)
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise EmptyTushareResult(f"{vip_api} period={period} returned empty")
            written = _upsert_raw_records(api_name, frame)
            finish_ingestion_run(run_id, status="completed", rows_read=len(frame), rows_written=written,
                                 meta={"api": vip_api, "period": period, "mode": "vip_batch"})
            _record_request_coverage(api_name, {"period": period, "mode": "vip_batch"}, None, len(frame))
            _record_probe(vip_api, "ok", len(frame))
            _invalidate_asset_views()
            return {"ok": True, "api": api_name, "vip_api": vip_api, "period": period,
                    "mode": "vip_batch", "rows_read": len(frame), "rows_written": written,
                    "total": len(frame), "success": len(frame), "failed": 0, "concurrency": 1}
        except Exception as exc:  # noqa: BLE001
            finish_ingestion_run(run_id, status="failed", rows_read=0, rows_written=0, error=str(exc),
                                 meta={"api": vip_api, "period": period, "mode": "vip_batch"})
            _record_probe(vip_api, "failed", 0, str(exc))
            # Fall through to the ordinary resumable per-stock path.
    with _conn() as c:
        rows = c.execute(
            """SELECT s.ts_code FROM dc_stock_master s
               WHERE NOT EXISTS (
                 SELECT 1 FROM dc_tushare_request_coverage q
                 WHERE q.api_name=? AND q.ts_code=s.ts_code AND q.period=?
                   AND q.result_status IN ('success','not_applicable','valid_empty','deferred_empty')
               )
               ORDER BY s.ts_code LIMIT ?""",
            (api_name, period, max(1, min(int(limit), 500))),
        ).fetchall()
    codes = [str(dict(row).get("ts_code") or "") for row in rows]
    def fetch_one(ts_code: str) -> dict[str, Any]:
        cooldown = _cooldown_remaining(api_name)
        if cooldown > 0:
            return {
                "ok": False,
                "api": api_name,
                "params": {"ts_code": ts_code},
                "rows_read": 0,
                "rows_written": 0,
                "cooldown": True,
                "cooldown_seconds": cooldown,
                "soft_failed": True,
                "error": f"Tushare {api_name} cooling down for {cooldown}s",
            }
        params: dict[str, Any] = {"ts_code": ts_code}
        if period:
            params["enddate" if api_name == "stk_holdernumber" else "period"] = period
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return _safe_sync_raw(api_name, params)
    results, workers = _parallel_tushare_map(codes, fetch_one, prefix=f"tushare-{api_name}")
    cooldown = any(bool(x.get("cooldown")) for x in results)
    cooldown_seconds = max([int(x.get("cooldown_seconds") or 0) for x in results if x.get("cooldown")] or [0])
    return {
        "ok": all(bool(x.get("ok")) for x in results) and not cooldown, "api": api_name, "period": period,
        "total": len(results), "success": sum(1 for x in results if x.get("ok") and not x.get("cooldown")),
        "failed": sum(1 for x in results if not x.get("ok")), "concurrency": workers,
        "cooldown": cooldown,
        "cooldown_seconds": cooldown_seconds,
        "error": _batch_error_summary(results),
        "failed_items": _failed_result_items(results, limit=8),
        "results": results,
    }


def _pending_raw_coverage_dates(
    api_name: str,
    wanted: list[str],
    *,
    pending_limit: int,
    minimum_rows: int,
    require_point_in_time: bool,
    chunk_size: int = DATE_RAW_COVERAGE_CHUNK_SIZE,
    verified_rows: dict[str, int] | None = None,
) -> list[str]:
    """Return the next incomplete dates using bounded aggregate queries."""
    pending: list[str] = []
    size = max(1, min(int(chunk_size), 60))
    cap = max(1, min(int(pending_limit), 60))
    for offset in range(0, len(wanted), size):
        batch = wanted[offset:offset + size]
        if not batch:
            continue
        having = "HAVING COUNT(*)>=?"
        if require_point_in_time:
            having += (
                " AND MAX(available_time)<=(SUBSTRING(trade_date,1,4)||'-'||"
                "SUBSTRING(trade_date,5,2)||'-'||SUBSTRING(trade_date,7,2)||'T15:00:00')"
            )
        placeholders = ",".join("?" for _ in batch)
        # Use a fresh transaction for every bounded slice. A PostgreSQL
        # statement timeout aborts its transaction, so reusing one connection
        # would make later slices fail even when they are small enough.
        with _conn() as c:
            rows = c.execute(
                f"""SELECT trade_date,COUNT(*) row_count FROM dc_tushare_raw_records
                    WHERE api_name=? AND trade_date IN ({placeholders})
                    GROUP BY trade_date {having}""",
                (api_name, *batch, max(1, int(minimum_rows))),
            ).fetchall()
        checked: set[str] = set()
        for row in rows:
            item = dict(row)
            trade_date = str(item.get("trade_date") or "")
            if not trade_date:
                continue
            checked.add(trade_date)
            if verified_rows is not None:
                verified_rows[trade_date] = int(item.get("row_count") or 0)
        pending.extend(trade_date for trade_date in batch if trade_date not in checked)
        if len(pending) >= cap:
            break
    return pending[:cap]


def _reconcile_verified_date_coverage(
    api_name: str,
    verified_rows: dict[str, int],
    *,
    chunk_size: int = 200,
) -> int:
    """Backfill the request ledger from raw slices that pass the full contract."""
    dates = sorted(trade_date for trade_date in verified_rows if trade_date)
    if not dates:
        return 0
    terminal = {"success", "not_applicable", "valid_empty", "deferred_empty"}
    recorded: set[str] = set()
    size = max(1, min(int(chunk_size), 500))
    for offset in range(0, len(dates), size):
        batch = dates[offset:offset + size]
        placeholders = ",".join("?" for _ in batch)
        with _conn() as c:
            rows = c.execute(
                f"""SELECT trade_date,result_status FROM dc_tushare_request_coverage
                    WHERE api_name=? AND trade_date IN ({placeholders})""",
                (api_name, *batch),
            ).fetchall()
        recorded.update(
            str(dict(row).get("trade_date") or "")
            for row in rows
            if str(dict(row).get("result_status") or "") in terminal
        )
    missing = [trade_date for trade_date in dates if trade_date not in recorded]
    now = _now()
    if missing:
        with _conn() as c:
            for trade_date in missing:
                params = {"trade_date": trade_date}
                c.execute(
                    """INSERT INTO dc_tushare_request_coverage
                       (request_key,api_name,ts_code,trade_date,period,params_json,rows_read,result_status,result_reason,checked_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(request_key) DO UPDATE SET rows_read=excluded.rows_read,
                           result_status=excluded.result_status,result_reason=excluded.result_reason,
                           checked_at=excluded.checked_at""",
                    (
                        _request_key(api_name, params, None),
                        api_name,
                        "",
                        trade_date,
                        "",
                        json.dumps(params, ensure_ascii=False, sort_keys=True),
                        max(0, int(verified_rows.get(trade_date) or 0)),
                        "success",
                        "verified_existing_raw_contract",
                        now,
                    ),
                )
    # Pagination and fallback requests use different request keys. Once the
    # date-level raw contract is verified, close only failures for that same
    # API/date; this cannot hide a partial or unverified slice.
    with _conn() as c:
        rows = c.execute(
            """SELECT id,params_json FROM dc_tushare_failed_requests
                WHERE api_name=? AND status='pending'""",
            (api_name,),
        ).fetchall()
    resolved_ids = []
    for row in rows:
        item = dict(row)
        try:
            params = json.loads(item.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        trade_date = str(params.get("trade_date") or "").replace("-", "")[:8]
        if trade_date in verified_rows:
            resolved_ids.append(int(item["id"]))
    if resolved_ids:
        with _conn() as c:
            cursor = c.raw.cursor() if bool(getattr(c, "_is_postgres", False)) else c.raw
            sql = "UPDATE dc_tushare_failed_requests SET status='resolved',resolved_at=? WHERE id=? AND status='pending'"
            cursor.executemany(
                sql.replace("?", "%s") if bool(getattr(c, "_is_postgres", False)) else sql,
                [(_now(), request_id) for request_id in resolved_ids],
            )
    return len(missing)


def sync_tushare_date_batch(
    api_name: str, *, trade_dates: list[str], limit: int = 10, require_rows: bool = False,
    minimum_rows: int = 1, require_point_in_time: bool = False,
) -> dict[str, Any]:
    """Progress a date-based API without letting retryable empty rows block forever."""
    _catalog_item(api_name)
    wanted = list(dict.fromkeys(str(x) for x in trade_dates if str(x)))
    cap = max(1, min(int(limit), 60))
    try:
        if require_rows:
            verified_rows: dict[str, int] = {}
            pending = _pending_raw_coverage_dates(
                api_name,
                wanted,
                pending_limit=cap,
                minimum_rows=minimum_rows,
                require_point_in_time=require_point_in_time,
                verified_rows=verified_rows,
            )
            _reconcile_verified_date_coverage(api_name, verified_rows)
        else:
            with _conn() as c:
                checked = {str(dict(row).get("trade_date") or "") for row in c.execute(
                    f"""SELECT trade_date FROM dc_tushare_request_coverage
                        WHERE api_name=? AND trade_date IN ({','.join('?' for _ in wanted)})
                          AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')""",
                    (api_name, *wanted),
                ).fetchall()} if wanted else set()
            pending = [d for d in wanted if d not in checked][:cap]
    except Exception as exc:  # noqa: BLE001 - the resumable worker must retain its checkpoint
        error = str(exc)[:1000]
        timed_out = "statement timeout" in error.lower()
        return {
            "ok": False,
            "api": api_name,
            "require_rows": require_rows,
            "minimum_rows": max(1, int(minimum_rows)),
            "require_point_in_time": require_point_in_time,
            "total": 0,
            "success": 0,
            "failed": 1,
            "concurrency": 0,
            "cooldown": False,
            "cooldown_seconds": 0,
            "deferred": True,
            "error_category": (
                "coverage_query_timeout" if timed_out else "coverage_query_failed"
            ),
            "error": f"{api_name} coverage query failed: {error}",
            "failed_items": [],
            "results": [],
        }
    results, workers = _parallel_tushare_map(
        pending, lambda d: _safe_sync_raw(api_name, {"trade_date": d}), prefix=f"tushare-{api_name}"
    )
    cooldown = any(bool(x.get("cooldown")) for x in results)
    cooldown_seconds = max([int(x.get("cooldown_seconds") or 0) for x in results if x.get("cooldown")] or [0])
    return {
        "ok": all(bool(x.get("ok")) for x in results) and not cooldown, "api": api_name,
        "require_rows": require_rows, "minimum_rows": max(1, int(minimum_rows)),
        "require_point_in_time": require_point_in_time,
        "total": len(results), "success": sum(1 for x in results if x.get("ok") and not x.get("cooldown")),
        "failed": sum(1 for x in results if not x.get("ok")), "concurrency": workers,
        "cooldown": cooldown,
        "cooldown_seconds": cooldown_seconds,
        "error": _batch_error_summary(results),
        "failed_items": _failed_result_items(results, limit=8),
        "results": results,
    }


def _batch_error_summary(results: list[dict[str, Any]]) -> str:
    errors: list[str] = []
    for item in _failed_result_items(results, limit=5):
        code = str(item.get("ts_code") or "").strip()
        api = str(item.get("api") or "").strip()
        error = str(item.get("error") or "failed").strip()
        label = "/".join(x for x in (code, api) if x)
        errors.append(f"{label}: {error}" if label else error)
    return "; ".join(errors)[:1000]


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


def sync_dividend_data(ts_code: str) -> dict[str, Any]:
    """Refresh the dividend ledger without pulling the full governance bundle."""
    code = str(ts_code or "").strip().upper()
    if not code:
        return {"ok": False, "api": "dividend", "ts_code": code, "error": "ts_code is required"}
    result = _safe_sync_raw("dividend", {"ts_code": code})
    return {
        "ok": bool(result.get("ok")),
        "api": "dividend",
        "ts_code": code,
        "rows_read": int(result.get("rows_read") or result.get("rows") or 0),
        "rows_written": int(result.get("rows_written") or 0),
        "soft_failed": bool(result.get("soft_failed")),
        "error": str(result.get("error") or ""),
        "result": result,
    }


def sync_fund_dividend_data(ts_code: str) -> dict[str, Any]:
    """Refresh official public-fund cash distributions for one listed fund."""
    code = str(ts_code or "").strip().upper()
    if not code:
        return {"ok": False, "api": "fund_div", "ts_code": code, "error": "ts_code is required"}
    result = _safe_sync_raw("fund_div", {"ts_code": code})
    return {
        "ok": bool(result.get("ok")),
        "api": "fund_div",
        "ts_code": code,
        "rows_read": int(result.get("rows_read") or result.get("rows") or 0),
        "rows_written": int(result.get("rows_written") or 0),
        "soft_failed": bool(result.get("soft_failed")),
        "error": str(result.get("error") or ""),
        "result": result,
    }


def sync_expectation_bundle(ts_code: str, period: str | None = None) -> dict[str, Any]:
    """Sync forecast/express data for one stock."""

    results = [_safe_sync_raw("forecast", {"ts_code": ts_code})]
    if period:
        results.append(_safe_sync_raw("express", {"ts_code": ts_code, "period": period}))
    return {"ok": True, "ts_code": ts_code, "period": period, "results": results}


def sync_index_bundle(index_code: str = "000300.SH", start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    """Probe equivalent index sources, then fetch from the first publisher."""

    if not start_date or not end_date:
        start_date, end_date = _recent_calendar_window(days_back=30, days_forward=0)
    if index_code == "000300.SH" and start_date == end_date:
        with _conn() as conn:
            existing = conn.execute(
                """SELECT source_code FROM dc_tushare_raw_records
                   WHERE api_name='index_daily' AND ts_code=? AND trade_date=? LIMIT 1""",
                (index_code, start_date),
            ).fetchone()
        if existing:
            existing_source = str(dict(existing).get("source_code") or "")
            if existing_source != SOURCE_TUSHARE:
                threading.Thread(
                    target=lambda: _safe_sync_raw(
                        "index_daily",
                        {"ts_code": index_code, "start_date": start_date, "end_date": end_date},
                    ),
                    daemon=True,
                    name=f"index-primary-reconcile-{start_date}",
                ).start()
            return {
                "ok": True,
                "benchmark_ready": True,
                "status": "canonical_bar_already_ready",
                "winner": existing_source,
                "index_code": index_code,
                "start_date": start_date,
                "end_date": end_date,
                "reconciliation_pending": existing_source != SOURCE_TUSHARE,
                "policy": "consume_ready_bar_immediately_reconcile_primary_in_background",
            }
        from app.data_platform.services.eastmoney_index_daily_fallback import (
            repair_hs300_index_daily_from_eastmoney,
        )
        from app.data_platform.services.market_data_discovery import (
            discover_first_published,
            mark_provider_bulk_result,
        )

        discovery = discover_first_published("index_daily", start_date)
        winner = str(discovery.get("winner") or "")
        if winner == SOURCE_TUSHARE:
            bulk = _safe_sync_raw(
                "index_daily",
                {"ts_code": index_code, "start_date": start_date, "end_date": end_date},
            )
        elif winner:
            bulk = repair_hs300_index_daily_from_eastmoney(start_date, provider=winner)
        else:
            bulk = {"ok": False, "status": "no_provider_published"}
        with _conn() as conn:
            ready = bool(conn.execute(
                """SELECT 1 FROM dc_tushare_raw_records
                   WHERE api_name='index_daily' AND ts_code=? AND trade_date=? LIMIT 1""",
                (index_code, start_date),
            ).fetchone())
        if winner:
            mark_provider_bulk_result(
                "index_daily",
                start_date,
                winner,
                ok=ready,
                evidence={"status": bulk.get("status"), "rows": bulk.get("rows_written") or bulk.get("rows_read")},
            )

        def _metadata() -> None:
            _safe_sync_raw("index_basic", {"market": "SSE"})
            _sync_monthly_index_weight_if_due(index_code, start_date)

        if ready:
            threading.Thread(target=_metadata, daemon=True, name=f"index-metadata-{start_date}").start()
        return {
            "ok": ready,
            "benchmark_ready": ready,
            "provider_discovery_contract": "alis_market_publication_probe_v1",
            "bulk_contract": "alis_index_first_publisher_bulk_v1",
            "winner": winner,
            "index_code": index_code,
            "start_date": start_date,
            "end_date": end_date,
            "discovery": discovery,
            "bulk_result": bulk,
            "metadata_pending": ["index_basic", "index_weight"] if ready else [],
            "policy": "probe_all_due_sources_then_bulk_fetch_first_publisher_only",
        }
    results = [
        _safe_sync_raw("index_basic", {"market": "SSE"}),
        _safe_sync_raw("index_daily", {"ts_code": index_code, "start_date": start_date, "end_date": end_date}),
        _sync_monthly_index_weight_if_due(index_code, end_date),
    ]
    with _conn() as c:
        benchmark_ready = bool(c.execute(
            """SELECT 1 FROM dc_tushare_raw_records
               WHERE api_name='index_daily' AND ts_code=? AND trade_date=? LIMIT 1""",
            (index_code, end_date),
        ).fetchone())
    return {
        "ok": all(bool(x.get("ok")) for x in results),
        "index_code": index_code,
        "start_date": start_date,
        "end_date": end_date,
        "results": results,
        "benchmark_ready": benchmark_ready,
    }


def _sync_monthly_index_weight_if_due(index_code: str, trade_date: str) -> dict[str, Any]:
    """Probe an index-weight adjustment at most once per calendar month."""
    value = str(trade_date or "").replace("-", "")[:8]
    if len(value) != 8 or not value.isdigit():
        return {"ok": False, "api": "index_weight", "status": "invalid_trade_date"}
    month_start = f"{value[:6]}01"
    params = {"index_code": index_code, "start_date": month_start, "end_date": value}
    terminal = ("success", "not_applicable", "valid_empty", "deferred_empty")
    with _conn() as c:
        existing = c.execute(
            """SELECT result_status FROM dc_tushare_request_coverage
                WHERE api_name='index_weight' AND params_json LIKE ?
                ORDER BY checked_at DESC LIMIT 1""",
            (f'%"index_code": "{index_code}", "start_date": "{month_start}"%',),
        ).fetchone()
    if existing and str(dict(existing).get("result_status") or "") in terminal:
        return {
            "ok": True, "api": "index_weight", "status": "monthly_window_already_checked",
            "index_code": index_code, "start_date": month_start, "end_date": value,
        }
    return _safe_sync_raw("index_weight", params)


def sync_training_market_date_vip(trade_date: str) -> dict[str, Any]:
    """Fetch one full-market training day through TuShare's VIP batch path.

    TuShare exposes the stock daily, daily-basic and limit datasets as
    trade-date batch calls (the entitlement is attached to the token rather
    than a separate ``*_vip`` method name). Keeping this wrapper explicit
    prevents training from falling back to per-stock requests or local-only
    history while still reusing the audited raw-record writer.
    """
    value = str(trade_date or "").replace("-", "")[:8]
    if len(value) != 8 or not value.isdigit():
        raise ValueError("trade_date must be YYYYMMDD")
    max_attempts = max(1, min(int(os.getenv("TUSHARE_TRAINING_MAX_RETRIES", "6")), 10))
    retry_delays = (5, 15, 30, 60, 120, 180, 240, 300, 300)
    results: list[dict[str, Any]] = []
    for api_name in ("daily", "daily_basic", "stk_limit"):
        last_error: Exception | None = None
        outcome: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                candidate = sync_tushare_api_raw(api_name, {"trade_date": value})
                if bool(candidate.get("ok")) and int(candidate.get("rows_written") or candidate.get("rows_read") or 0) > 0:
                    outcome = candidate
                    break
                last_error = RuntimeError(str(candidate.get("error") or f"{api_name} returned no rows"))
            except Exception as exc:  # noqa: BLE001 - historical download is resumable
                last_error = exc
            if attempt < max_attempts:
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                write_runtime_log(
                    "WARNING", "model-training",
                    f"{value} {api_name} 返回异常，{delay}秒后重试",
                    {"trade_date": value, "api": api_name, "attempt": attempt, "max_attempts": max_attempts, "error": str(last_error)[:300]},
                )
                reset_tushare_client()
                time.sleep(delay)
        if outcome is None:
            raise RuntimeError(f"训练历史 {value} {api_name} 连续{max_attempts}次下载失败：{str(last_error)[:300]}")
        results.append(outcome)
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "trade_date": value,
        "mode": "tushare_vip_trade_date_batch",
        "results": results,
        "rows_written": sum(int(item.get("rows_written") or 0) for item in results),
    }


def training_market_date_ready(trade_date: str) -> bool:
    """Return whether a retained training day already has all required data."""
    value = str(trade_date or "").replace("-", "")[:8]
    if len(value) != 8 or not value.isdigit():
        return False
    from app.data_platform.services.decision_data_contract import decision_data_contract

    return bool(decision_data_contract(value).get("ok"))


def _safe_sync_raw(api_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return sync_tushare_api_raw(api_name, params)
    except TushareCoolingDown as exc:
        return {
            "ok": False,
            "api": api_name,
            "params": params or {},
            "rows_read": 0,
            "rows_written": 0,
            "cooldown": True,
            "cooldown_seconds": exc.seconds,
            "soft_failed": True,
            "error": str(exc)[:500],
        }
    except Exception as exc:  # noqa: BLE001
        category = classify_tushare_error(exc)
        if category == "upstream_not_ready":
            return {
                "ok": False,
                "api": api_name,
                "params": params or {},
                "rows_read": 0,
                "rows_written": 0,
                "cooldown": True,
                "cooldown_seconds": 600,
                "soft_failed": True,
                "error": str(exc)[:500],
                "category": category,
            }
        if category == "empty_pending" and _defer_empty_request_if_exhausted(api_name, params or {}):
            return {
                "ok": True,
                "api": api_name,
                "params": params or {},
                "rows_read": 0,
                "rows_written": 0,
                "deferred_empty": True,
                "category": "deferred_empty",
            }
        if category == "rate_limit":
            return {
                "ok": False,
                "api": api_name,
                "params": params or {},
                "rows_read": 0,
                "rows_written": 0,
                "cooldown": True,
                "cooldown_seconds": TUSHARE_RATE_LIMIT_COOLDOWN_SECONDS,
                "soft_failed": True,
                "error": str(exc)[:500],
                "category": category,
            }
        return {
            "ok": False,
            "api": api_name,
            "params": params or {},
            "rows_read": 0,
            "rows_written": 0,
            "error": str(exc)[:500],
            "category": category,
        }


def purge_temporary_training_market_data(*, fetched_dates: list[str], online_window_start: str) -> dict[str, Any]:
    """Delete only old, training-created market slices after feature extraction.

    The caller supplies all dates fetched for one training slice.  Dates in or
    after the normal online window are deliberately excluded, so a failed or
    repeated training run cannot erase current business data.
    """
    from app.services.streaming_training import temporary_dates_for_cleanup

    dates = temporary_dates_for_cleanup(fetched_dates=fetched_dates, online_window_start=online_window_start)
    if not dates:
        return {"ok": True, "deleted_dates": [], "raw_records_deleted": 0, "snapshots_deleted": 0}
    placeholders = ",".join("?" for _ in dates)
    with _conn() as c:
        snapshot = c.execute(f"DELETE FROM dc_stock_daily_snapshot WHERE trade_date IN ({placeholders})", tuple(dates))
        raw = c.execute(
            f"DELETE FROM dc_tushare_raw_records WHERE trade_date IN ({placeholders}) AND api_name IN ('daily','daily_basic','stk_limit','suspend_d')",
            tuple(dates),
        )
        c.execute(f"DELETE FROM dc_available_trade_dates WHERE trade_date IN ({placeholders})", tuple(dates))
    return {
        "ok": True, "deleted_dates": dates, "raw_records_deleted": int(raw.rowcount or 0),
        "snapshots_deleted": int(snapshot.rowcount or 0),
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
            adj_factors = _raw_payload_map(c, "adj_factor", trade_date, "ts_code")
            limits = _raw_payload_map(c, "stk_limit", trade_date, "ts_code")
            suspended = set(_raw_payload_map(c, "suspend_d", trade_date, "ts_code").keys())
        now = _now()
        written = 0
        stock_codes = set(stocks.keys())
        all_codes = sorted((set(daily_basic) | set(daily) | set(adj_factors) | set(limits) | suspended) & stock_codes)
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
                snapshot_available_time = _iso_available_on_date(trade_date, hour=15, minute=0, second=0) or now
                values = (
                    ts_code,
                    trade_date,
                    str(info.get("symbol") or ts_code[:6]),
                    str(info.get("name") or ""),
                    str(info.get("industry") or ""),
                    close,
                    _float_or_none(price.get("open")),
                    _float_or_none(price.get("high")),
                    _float_or_none(price.get("low")),
                    _float_or_none(price.get("pre_close")),
                    _float_or_none(adj_factors.get(ts_code, {}).get("adj_factor")),
                    _float_or_none(price.get("pct_chg")),
                    _float_or_none(price.get("vol")),
                    _float_or_none(price.get("amount")),
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
                    str(limit.get("source_code") or SOURCE_TUSHARE),
                    snapshot_available_time,
                    now,
                )
                _upsert_snapshot_row(c, values)
                written += 1
            if written > 0:
                available_time = _iso_available_on_date(trade_date, hour=15, minute=0, second=0) or now
                c.execute(
                    """INSERT INTO dc_available_trade_dates(trade_date,source_code,available_time,updated_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(trade_date) DO UPDATE SET
                         source_code=excluded.source_code,
                         available_time=excluded.available_time,
                         updated_at=excluded.updated_at""",
                    (trade_date, SOURCE_TUSHARE, available_time, now),
                )
            _refresh_snapshot_stats(c, trade_date, now)
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


def _refresh_snapshot_stats(c: Any, trade_date: str, updated_at: str) -> None:
    """Refresh one date only; never aggregate the full snapshot table on a read path."""
    # The summary table is a read optimization. Some production databases
    # create it with an administrative role and grant the app role SELECT
    # only; that must not make the authoritative snapshot ingestion fail.
    target_table = "dc_stock_daily_snapshot_stats"
    if bool(getattr(c, "_is_postgres", False)):
        privilege = c.execute(
            """SELECT has_table_privilege(current_user, ?, 'INSERT')
                      AND has_table_privilege(current_user, ?, 'UPDATE') AS can_write""",
            ("public.dc_stock_daily_snapshot_stats", "public.dc_stock_daily_snapshot_stats"),
        ).fetchone()
        if not bool(_row_value(privilege, "can_write", False)):
            # Some upgraded databases retain the legacy summary under an
            # administrative owner. Keep new daily watermarks in the
            # migration-owned live table instead of silently dropping them.
            target_table = "dc_stock_daily_snapshot_stats_live"
    row = c.execute(
        """SELECT COUNT(*) AS rows_total,
                  SUM(CASE WHEN close IS NOT NULL AND volume IS NOT NULL AND turnover IS NOT NULL THEN 1 ELSE 0 END) AS market_rows,
                  COALESCE(MIN(available_time), '') AS min_available_time,
                  COALESCE(MAX(available_time), '') AS max_available_time
             FROM dc_stock_daily_snapshot
            WHERE trade_date=?""",
        (trade_date,),
    ).fetchone()
    values = (
        trade_date,
        int(_row_value(row, "rows_total", 0) or 0),
        int(_row_value(row, "market_rows", 0) or 0),
        str(_row_value(row, "min_available_time", "") or ""),
        str(_row_value(row, "max_available_time", "") or ""),
        updated_at,
    )
    c.execute(
        f"""INSERT INTO {target_table}
                  (trade_date,rows_total,market_rows,min_available_time,max_available_time,updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(trade_date) DO UPDATE SET
                  rows_total=excluded.rows_total,
                  market_rows=excluded.market_rows,
                  min_available_time=excluded.min_available_time,
                  max_available_time=excluded.max_available_time,
                  updated_at=excluded.updated_at""",
        values,
    )


def _refresh_oos_hard_api_stats(c: Any, api_name: str, trade_dates: list[str], updated_at: str) -> None:
    """Persist bounded point-in-time raw coverage for the permanent OOS gate."""
    for trade_date in trade_dates:
        row = c.execute(
            """SELECT COUNT(DISTINCT ts_code) FILTER (WHERE available_time<=
                        (SUBSTRING(trade_date,1,4)||'-'||SUBSTRING(trade_date,5,2)||'-'||SUBSTRING(trade_date,7,2)||'T15:00:00')) AS point_in_time_rows,
                      COUNT(DISTINCT ts_code) AS raw_rows,
                      COALESCE(MIN(available_time), '') AS first_available_time
                 FROM dc_tushare_raw_records
                WHERE api_name=? AND trade_date=? AND ts_code<>''""",
            (api_name, trade_date),
        ).fetchone()
        c.execute(
            """INSERT INTO dc_oos_hard_api_stats_live
                   (api_name,trade_date,point_in_time_rows,raw_rows,first_available_time,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(api_name,trade_date) DO UPDATE SET
                   point_in_time_rows=excluded.point_in_time_rows,
                   raw_rows=excluded.raw_rows,
                   first_available_time=excluded.first_available_time,
                   updated_at=excluded.updated_at""",
            (
                api_name, trade_date, int(_row_value(row, "point_in_time_rows", 0) or 0),
                int(_row_value(row, "raw_rows", 0) or 0),
                str(_row_value(row, "first_available_time", "") or ""), updated_at,
            ),
        )


def _upsert_snapshot_row(c: Any, values: tuple[Any, ...]) -> None:
    if c._is_mysql:
        c.execute(
            """
            INSERT INTO dc_stock_daily_snapshot
                (ts_code, trade_date, symbol, name, industry, close, open, high, low, pre_close, adj_factor, change_pct, volume, turnover, total_mv, circ_mv, pe_ttm, pb,
                 turnover_rate, up_limit, down_limit, is_suspended, data_quality_score, quality_flags_json,
                 source_code, available_time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                symbol = VALUES(symbol), name = VALUES(name), industry = VALUES(industry),
                close = VALUES(close), open = VALUES(open), high = VALUES(high), low = VALUES(low), pre_close = VALUES(pre_close), adj_factor = VALUES(adj_factor),
                change_pct = VALUES(change_pct), volume = VALUES(volume), turnover = VALUES(turnover), total_mv = VALUES(total_mv), circ_mv = VALUES(circ_mv),
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
                (ts_code, trade_date, symbol, name, industry, close, open, high, low, pre_close, adj_factor, change_pct, volume, turnover, total_mv, circ_mv, pe_ttm, pb,
                 turnover_rate, up_limit, down_limit, is_suspended, data_quality_score, quality_flags_json,
                 source_code, available_time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts_code, trade_date) DO UPDATE SET
                symbol = excluded.symbol,
                name = excluded.name,
                industry = excluded.industry,
                close = excluded.close,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                pre_close = excluded.pre_close,
                adj_factor = excluded.adj_factor,
                change_pct = excluded.change_pct,
                volume = excluded.volume,
                turnover = excluded.turnover,
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
            pcf_headers = _row_count(c.execute(
                "SELECT COUNT(*) AS cnt FROM dc_etf_pcf_header WHERE trade_date=?", (trade_date,)
            ).fetchone())
            pcf_components = _row_count(c.execute(
                "SELECT COUNT(*) AS cnt FROM dc_etf_pcf_component WHERE trade_date=?", (trade_date,)
            ).fetchone())
            pcf_mismatch = _row_count(c.execute(
                """SELECT COUNT(*) AS cnt FROM dc_etf_pcf_header h
                   WHERE h.trade_date=? AND h.component_count<>(SELECT COUNT(*) FROM dc_etf_pcf_component c
                   WHERE c.exchange=h.exchange AND c.etf_code=h.etf_code AND c.trade_date=h.trade_date)""",
                (trade_date,),
            ).fetchone())
            checks.append(_quality_item(
                "etf_pcf_coverage", f"exchange_etf_pcf:{trade_date}",
                "ok" if pcf_headers > 0 and pcf_components > 0 else "warning", "medium",
                f"ETF PCF headers={pcf_headers}, components={pcf_components}", 0 if pcf_headers else 1,
            ))
            checks.append(_quality_item(
                "etf_pcf_component_count", f"exchange_etf_pcf:{trade_date}",
                "ok" if pcf_mismatch == 0 else "warning", "high",
                f"ETF PCF component count mismatches={pcf_mismatch}", pcf_mismatch,
            ))
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
    category: str = "",
) -> dict[str, Any]:
    """Sync announcement metadata from CNINFO and create coarse rule-based events."""

    ensure_data_center_seeded()
    # CNINFO's history endpoint silently repeats page one when pageSize > 30.
    # Keep the provider's effective page size so pageNum advances correctly.
    page_size = max(1, min(int(page_size), 30))
    max_pages = max(1, min(int(max_pages), 20))
    run_id = start_ingestion_run(
        "cninfo.announcements",
        {"start_date": start_date, "end_date": end_date, "ts_code": ts_code or "", "page_size": page_size, "max_pages": max_pages, "category": category},
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
                category=category,
            )
            if not page_rows:
                break
            rows.extend(page_rows)
        written, events = _upsert_announcements(rows)
        finish_ingestion_run(
            run_id,
            status="completed",
            rows_read=len(rows),
            rows_written=written,
            meta={"events_created": events, "start_date": start_date, "end_date": end_date, "ts_code": ts_code or "", "category": category},
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
    category: str = "",
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
        "category": str(category or ""),
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
    data: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            parsed = resp.json()
            if not isinstance(parsed, dict):
                raise ValueError("CNINFO response is not a JSON object")
            data = parsed
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt >= 2:
                break
            time.sleep(0.5 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"CNINFO announcement request failed after 3 attempts: {last_error}") from last_error
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
                "provider_type_codes": str(item.get("announcementType") or ""),
                "provider_type_name": str(item.get("announcementTypeName") or ""),
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
            if ann_id:
                # A changed title may no longer match the current contract.
                # Delete the previous derived label first so stale rule events
                # cannot survive a source refresh.
                c.execute("DELETE FROM dc_announcement_event WHERE announcement_id = ?", (ann_id,))
            if ann_id and event:
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
    """Classify announcement titles into the public P1 event contract.

    Keep the output vocabulary aligned with ``V1_EVENT_TYPES`` in the
    research event engine.  In particular, do not merge repurchases with
    shareholder increases or M&A with private placements: their research
    horizon and affected dimensions are different.
    """
    rules = [
        ("立案调查|被立案|立案", "investigation", "negative", "critical"),
        ("处罚|监管函|问询函|警示函", "regulatory_risk", "negative", "high"),
        ("减持|减持计划", "shareholder_reduction", "negative", "high"),
        ("解除质押", "pledge", "positive", "low"),
        ("质押", "pledge", "negative", "medium"),
        ("业绩预告|业绩预增|业绩预盈|业绩预亏|业绩预减", "earnings_forecast", "neutral", "medium"),
        ("业绩快报", "earnings_flash", "neutral", "medium"),
        (
            "年度报告|季度报告|半年度报告|一季报|三季报|Annual Report|Half[- ]Year Report|First Quarterly Report|Third Quarterly Report",
            "earnings",
            "neutral",
            "medium",
        ),
        ("回购", "repurchase", "positive", "medium"),
        ("增持", "shareholder_increase", "positive", "medium"),
        ("分红|权益分派|利润分配|股息", "dividend", "positive", "low"),
        ("重大合同|中标|订单", "major_contract", "positive", "medium"),
        ("定增|非公开发行", "private_placement", "neutral", "medium"),
        ("并购|重组|收购|资产购买", "ma_restructuring", "neutral", "medium"),
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


def reclassify_announcement_events(*, limit: int = 50_000) -> dict[str, Any]:
    """Rebuild rule-based event labels from all retained announcement titles.

    The migration updates changed labels, creates labels for previously missed
    announcements and removes stale labels that no longer match the current
    contract.  It does not modify source announcements or invent source facts.
    """
    ensure_data_center_seeded()
    cap = max(1, min(int(limit), 200_000))
    with _conn() as c:
        rows = c.execute(
            """SELECT a.id AS announcement_row_id,a.ts_code,a.title,a.publish_time,
                      e.id AS event_id,e.event_type,e.impact_direction,e.severity,e.confidence,e.event_date,e.summary
                 FROM dc_announcement_raw a
                 LEFT JOIN dc_announcement_event e
                   ON e.id=(SELECT MIN(e2.id) FROM dc_announcement_event e2 WHERE e2.announcement_id=a.id)
                 ORDER BY a.id ASC LIMIT ?""",
            (cap,),
        ).fetchall()
        created = 0
        updated = 0
        removed = 0
        unchanged = 0
        now = _now()
        for raw in rows:
            row = _row_to_dict(raw)
            event = classify_announcement_event(str(row.get("title") or ""), str(row.get("publish_time") or ""))
            if not event:
                if row.get("event_id") is not None:
                    c.execute("DELETE FROM dc_announcement_event WHERE announcement_id=?", (row["announcement_row_id"],))
                    removed += 1
                else:
                    unchanged += 1
                continue
            expected = (event["event_type"], event["impact_direction"], event["severity"], event["event_date"], event["summary"])
            actual = (row.get("event_type"), row.get("impact_direction"), row.get("severity"), row.get("event_date"), row.get("summary"))
            if expected == actual:
                unchanged += 1
                continue
            c.execute("DELETE FROM dc_announcement_event WHERE announcement_id=?", (row["announcement_row_id"],))
            c.execute(
                """INSERT INTO dc_announcement_event
                   (announcement_id,event_type,impact_direction,severity,confidence,event_date,
                    entities_json,metrics_json,summary,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["announcement_row_id"],
                    event["event_type"],
                    event["impact_direction"],
                    event["severity"],
                    event["confidence"],
                    event["event_date"],
                    json.dumps({"ts_code": row.get("ts_code") or "", "title": row.get("title") or ""}, ensure_ascii=False),
                    "{}",
                    event["summary"],
                    now,
                ),
            )
            if row.get("event_id") is None:
                created += 1
            else:
                updated += 1
    return {
        "ok": True,
        "examined": len(rows),
        "created": created,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "contract": "announcement_event_v2_full_reclassification",
    }


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


_PMI_MANUFACTURING_INDUSTRIES = {
    "专用机械", "元器件", "其他建材", "农用机械", "农药化肥", "化学制药", "化工原料", "化工机械", "化纤",
    "半导体", "塑料", "家居用品", "家用电器", "工程机械", "日用化工", "普钢", "服饰", "机床制造",
    "机械基件", "染料涂料", "橡胶", "汽车整车", "汽车配件", "玻璃", "生物制药", "电器仪表", "电气设备",
    "石油加工", "矿物制品", "纺织", "纺织机械", "船舶", "运输设备", "通信设备", "造纸", "钢加工",
    "陶瓷", "食品", "IT设备", "中成药", "医疗保健", "医药商业", "白酒", "啤酒", "软饮料", "红黄酒",
}

_PMI_SERVICE_INDUSTRIES = {
    "互联网", "仓储物流", "公共交通", "公路", "其他商业", "保险", "全国地产", "商贸代理", "商品城", "多元金融",
    "广告包装", "建筑工程", "影视音像", "房产服务", "批发业", "旅游景点", "旅游服务", "机场", "水运", "港口",
    "空运", "航空", "证券", "超市连锁", "软件服务", "酒店餐饮", "铁路", "电信运营", "百货", "电器连锁",
}


def _macro_metric_code(group: str, industry: str) -> str:
    # Stable ASCII keys keep import/export and object storage paths portable.
    digest = hashlib.sha256(industry.encode("utf-8")).hexdigest()[:12]
    return f"cn_pmi_{group}_demand_{digest}"


def sync_pmi_industry_proxies() -> dict[str, Any]:
    """Materialize retained TuShare PMI as a transparent industry demand proxy.

    PMI is a macro observation, not a substitute for product price, capacity
    utilisation or company orders. It only contributes to the demand/orders
    component and preserves raw ingestion time for point-in-time reads.
    """
    ensure_data_center_seeded()
    with _conn() as c:
        raw_rows = c.execute(
            "SELECT payload_json,available_time FROM dc_tushare_raw_records WHERE api_name='cn_pmi' ORDER BY id ASC"
        ).fetchall()
        industries = {
            str(_row_to_dict(row).get("industry") or "").strip()
            for row in c.execute("SELECT DISTINCT industry FROM dc_stock_master WHERE industry IS NOT NULL").fetchall()
        }
    months: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        row = _row_to_dict(raw)
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        month = str(payload.get("MONTH") or payload.get("month") or "").strip()
        if len(month) != 6 or not month.isdigit():
            continue
        months[month] = {"payload": payload, "available_time": str(row.get("available_time") or "")}

    definitions: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    group_specs = (
        ("manufacturing", _PMI_MANUFACTURING_INDUSTRIES & industries, "PMI010000", "制造业PMI（行业需求宏观代理）"),
        ("service", _PMI_SERVICE_INDUSTRIES & industries, "PMI020100", "非制造业PMI（行业需求宏观代理）"),
    )
    for group, mapped_industries, field, metric_name in group_specs:
        for industry in sorted(mapped_industries):
            metric_code = _macro_metric_code(group, industry)
            definitions.append({
                "metric_code": metric_code, "industry_code": f"master:{industry}", "industry_name": industry,
                "metric_name": metric_name, "unit": "index", "frequency": "monthly", "source_code": SOURCE_TUSHARE,
                "description": "TuShare cn_pmi 宏观需求代理；仅补充需求/订单维度，不替代产品价格、库存、产能或行业专属数据。",
            })
            for month, item in months.items():
                payload = item["payload"]
                value = payload.get(field)
                fallback = False
                if value is None and group == "service":
                    value = payload.get("PMI030000")
                    fallback = value is not None
                numeric = _float_or_none(value)
                if numeric is None or numeric <= 0:
                    continue
                values.append({
                    "metric_code": metric_code, "data_date": f"{month[:4]}-{month[4:]}-01", "value": numeric,
                    "raw_value": json.dumps({"field": "PMI030000" if fallback else field, "value": value}, ensure_ascii=False),
                    "source_code": SOURCE_TUSHARE, "available_time": item["available_time"],
                })
    return {
        "ok": True, "definitions": _upsert_industry_metric_definitions(definitions),
        "values": _upsert_industry_metric_values(values), "months": len(months),
        "mapped_industries": len(definitions), "contract": "tushare_cn_pmi_industry_demand_proxy_v1",
        "limitations": ["PMI仅是宏观需求代理；不得据此推断行业产品价格、库存、产能利用率或政策供给。"],
    }


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
            available_time = str(row.get("available_time") or now)
            values = (
                metric_code,
                data_date,
                _float_or_none(row.get("value")),
                raw_value,
                str(row.get("source_code") or SOURCE_MANUAL).strip(),
                available_time,
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
