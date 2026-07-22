"""Data asset catalog with explicit audit windows and completeness scoring."""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.redis_store import delete as redis_delete, get_json, set_json
from app.data_platform.services.market_calendar import target_trade_day

# Fixed audit policy relative to "today".
DAILY_LOOKBACK_DAYS = 730  # ~2 years
FINANCIAL_QUARTERS = 8
FINANCIAL_ANNUAL_YEARS = 3
# Approximate open trading days in a 2-year window when calendar is incomplete.
FALLBACK_OPEN_DAYS = 500

# Avoid rescanning multi-million-row raw tables on every UI poll.
_ASSETS_CACHE_TTL_SEC = 45.0
_assets_cache_lock = threading.Lock()
_assets_cache: dict[str, Any] = {"at": 0.0, "value": None}

# Sparse / non-daily APIs that need a whole-table sample (usually tiny vs daily).
_SPARSE_RAW_APIS = (
    "namechange",
    "dividend",
    "forecast",
    "express",
    "index_basic",
    "index_weight",
    "index_daily",
    "cn_gdp",
    "cn_cpi",
    "cn_pmi",
    "shibor",
    "fx_daily",
)


def _d(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _i(row: Any, key: str = "n", default: int = 0) -> int:
    return int(_d(row).get(key) or default)


def _s(row: Any, key: str, default: str = "") -> str:
    value = _d(row).get(key)
    return str(value) if value not in (None, "") else default


def _coverage(actual: int, expected: int | None) -> float | None:
    if expected is None:
        return None
    if expected <= 0:
        return 1.0 if actual > 0 else 0.0
    return round(min(1.0, actual / expected), 6)


def _status(actual: int, expected: int | None, coverage: float | None, *, event_like: bool = False) -> str:
    if event_like or expected is None:
        return "available" if actual > 0 else "missing"
    if actual <= 0:
        return "missing"
    # "complete" is an inventory fact, not an operational threshold.  A
    # consumer may safely run at 99% coverage, but the remaining 1% must stay
    # visible as a data gap and in the backfill plan.
    if expected is not None and actual >= expected:
        return "complete"
    return "partial"


def _report_periods(today: date | None = None) -> list[str]:
    """Latest N report periods whose normal disclosure deadline has passed."""
    today = today or date.today()
    candidates: list[tuple[date, str]] = []
    for year in range(today.year, today.year - 5, -1):
        candidates.extend([
            (date(year, 4, 30), f"{year}0331"),
            (date(year, 8, 31), f"{year}0630"),
            (date(year, 10, 31), f"{year}0930"),
            (date(year + 1, 4, 30), f"{year}1231"),
        ])
    available = [item for deadline, item in candidates if deadline <= today]
    return sorted(available, reverse=True)[:FINANCIAL_QUARTERS]


def _annual_periods(today: date | None = None) -> list[str]:
    today = today or date.today()
    # Prefer completed annual reports; if before April use previous year.
    latest_year = today.year - 1 if today.month < 4 else today.year - 1
    return [f"{year}1231" for year in range(latest_year, latest_year - FINANCIAL_ANNUAL_YEARS, -1)]


def get_data_assets(*, force: bool = False) -> dict[str, Any]:
    if not force:
        redis_cached = get_json("alis:data-platform:assets:v3")
        if isinstance(redis_cached, dict):
            return redis_cached
    now = time.monotonic()
    with _assets_cache_lock:
        cached = _assets_cache.get("value")
        if (
            not force
            and cached is not None
            and now - float(_assets_cache.get("at") or 0) < _ASSETS_CACHE_TTL_SEC
        ):
            return cached

    payload = _compute_data_assets()
    with _assets_cache_lock:
        _assets_cache["at"] = time.monotonic()
        _assets_cache["value"] = payload
    set_json("alis:data-platform:assets:v3", payload, ttl=int(_ASSETS_CACHE_TTL_SEC))
    return payload


def invalidate_data_assets_cache() -> None:
    redis_delete("alis:data-platform:assets:v2")
    redis_delete("alis:data-platform:assets:v3")
    with _assets_cache_lock:
        _assets_cache["at"] = 0.0
        _assets_cache["value"] = None


def _compute_data_assets() -> dict[str, Any]:
    today = date.today()
    with _conn() as c:
        end = target_trade_day(c)
    start = end - timedelta(days=DAILY_LOOKBACK_DAYS)
    start_s, end_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    quarters = _report_periods(today)
    annuals = _annual_periods(today)
    financial_periods = sorted(set(quarters + annuals))
    financial_start = min(financial_periods)
    financial_end = max(financial_periods)
    from app.data_platform.services.data_center import get_vip_backfill_status
    from app.data_platform.services.exchange_etf_pcf import tracked_etf_codes
    vip = get_vip_backfill_status(end_date=end_s)

    with _conn() as c:
        stocks = _i(c.execute("SELECT COUNT(*) n FROM dc_stock_master").fetchone())
        industry_named = _i(
            c.execute("SELECT COUNT(*) n FROM dc_stock_master WHERE industry<>''").fetchone()
        )

        cal_total = _i(c.execute("SELECT COUNT(*) n FROM dc_trade_calendar WHERE is_open=1").fetchone())
        cal_window = _i(
            c.execute(
                "SELECT COUNT(*) n FROM dc_trade_calendar WHERE is_open=1 AND cal_date BETWEEN ? AND ?",
                (start_s, end_s),
            ).fetchone()
        )
        calendar_end = min(end + timedelta(days=180), date(end.year, 12, 31))
        calendar_end_s = calendar_end.strftime("%Y%m%d")
        cal_date_rows = _i(
            c.execute(
                "SELECT COUNT(*) n FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date BETWEEN ? AND ?",
                (start_s, calendar_end_s),
            ).fetchone()
        )
        calendar_expected_days = (calendar_end - start).days + 1
        cal_min = _s(c.execute("SELECT MIN(cal_date) v FROM dc_trade_calendar WHERE is_open=1").fetchone(), "v")
        cal_max = _s(c.execute("SELECT MAX(cal_date) v FROM dc_trade_calendar WHERE is_open=1").fetchone(), "v")

        # Prefer calendar-derived open days; fall back when calendar itself is incomplete.
        open_days_expected = cal_window if cal_window >= 200 else FALLBACK_OPEN_DAYS

        snap = _d(
            c.execute(
                """
                SELECT COUNT(*) records,
                       COUNT(DISTINCT trade_date) dates,
                       MIN(trade_date) min_date,
                       MAX(trade_date) max_date
                FROM dc_stock_daily_snapshot
                WHERE trade_date BETWEEN ? AND ?
                """,
                (start_s, end_s),
            ).fetchone()
        )

        # Daily coverage via calendar EXISTS — avoids multi-million-row GROUP BY scans.
        daily_apis = (
            "daily",
            "daily_basic",
            "stk_limit",
            "adj_factor",
            "moneyflow",
            "moneyflow_hsgt",
            "index_daily",
            "suspend_d",
        )
        raw_daily: dict[str, dict[str, Any]] = {}
        for api in daily_apis:
            cov = _d(
                c.execute(
                    """
                    SELECT COUNT(*) n,
                           MIN(cal.cal_date) min_date,
                           MAX(cal.cal_date) max_date
                    FROM dc_trade_calendar cal
                    WHERE cal.is_open = 1
                      AND cal.cal_date BETWEEN ? AND ?
                      AND EXISTS (
                        SELECT 1 FROM dc_tushare_raw_records r
                        WHERE r.api_name = ? AND r.trade_date = cal.cal_date
                      )
                    """,
                    (start_s, end_s, api),
                ).fetchone()
            )
            checked = _d(
                c.execute(
                    """
                    SELECT COUNT(DISTINCT trade_date) n,
                           MIN(trade_date) min_date,
                           MAX(trade_date) max_date
                    FROM dc_tushare_request_coverage
                    WHERE api_name = ?
                      AND trade_date BETWEEN ? AND ?
                      AND result_status IN ('success','valid_empty','not_applicable','deferred_empty')
                    """,
                    (api, start_s, end_s),
                ).fetchone()
            )
            dates = int(cov.get("n") or 0)
            checked_dates = int(checked.get("n") or 0)
            raw_daily[api] = {
                "api_name": api,
                "records": dates * stocks if dates and stocks else 0,
                "symbols": stocks if dates else 0,
                "dates": dates,
                "min_date": cov.get("min_date"),
                "max_date": cov.get("max_date"),
                "checked_dates": checked_dates,
                "checked_min_date": checked.get("min_date"),
                "checked_max_date": checked.get("max_date"),
            }
        # Prefer exact snapshot row count for the fused daily view.
        if "daily" in raw_daily:
            raw_daily["daily"]["records"] = int(snap.get("records") or raw_daily["daily"]["records"])
            if int(snap.get("dates") or 0) > int(raw_daily["daily"]["dates"] or 0):
                raw_daily["daily"]["dates"] = int(snap["dates"])
                raw_daily["daily"]["min_date"] = snap.get("min_date") or raw_daily["daily"].get("min_date")
                raw_daily["daily"]["max_date"] = snap.get("max_date") or raw_daily["daily"].get("max_date")
                raw_daily["daily"]["symbols"] = stocks if raw_daily["daily"]["dates"] else 0

        period_apis = (
            "income",
            "balancesheet",
            "cashflow",
            "fina_indicator",
            "fina_audit",
            "fina_mainbz",
            "top10_holders",
            "top10_floatholders",
            "stk_holdernumber",
        )
        raw_period: dict[str, dict[str, Any]] = {}
        for api in period_apis:
            row = c.execute(
                """
                SELECT COUNT(DISTINCT period) periods,
                       MIN(period) min_date,
                       MAX(period) max_date
                FROM dc_tushare_raw_records
                WHERE api_name = ?
                  AND period <> ''
                  AND period BETWEEN ? AND ?
                """,
                (api, financial_start, financial_end),
            ).fetchone()
            raw_period[api] = dict(row) if row else {}

        # Only scan sparse APIs — never GROUP BY the whole raw table.
        raw_all: dict[str, dict[str, Any]] = {}
        for api in _SPARSE_RAW_APIS:
            row = c.execute(
                """
                SELECT COUNT(*) records,
                       COUNT(DISTINCT ts_code) symbols,
                       COUNT(DISTINCT CASE WHEN trade_date<>'' THEN trade_date END) dates,
                       COUNT(DISTINCT CASE WHEN period<>'' THEN period END) periods,
                       MIN(NULLIF(COALESCE(NULLIF(trade_date,''),NULLIF(period,''),NULLIF(ann_date,'')),'')) min_date,
                       MAX(NULLIF(COALESCE(NULLIF(trade_date,''),NULLIF(period,''),NULLIF(ann_date,'')),'')) max_date
                FROM dc_tushare_raw_records
                WHERE api_name = ?
                """,
                (api,),
            ).fetchone()
            raw_all[api] = dict(row) if row else {}
        pledge_records = _d(
            c.execute(
                """
                SELECT COUNT(*) records,
                       MIN(period) min_date,
                       MAX(period) max_date
                FROM dc_tushare_raw_records
                WHERE api_name = 'pledge_stat'
                """
            ).fetchone()
        )
        pledge_symbols = _i(
            c.execute(
                """
                SELECT COUNT(DISTINCT ts_code) n
                FROM dc_tushare_request_coverage
                WHERE api_name = 'pledge_stat'
                  AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')
                """
            ).fetchone()
        )
        raw_all["pledge_stat"] = {
            **pledge_records,
            "symbols": pledge_symbols,
            "dates": 0,
            "periods": 0,
        }

        # Financial symbol coverage counts real rows plus terminal request
        # coverage, so pre-listing and deferred-empty cases do not remain as
        # permanent false gaps.
        financial_symbol_counts: dict[str, int] = {}
        for api in period_apis:
            row = c.execute(
                """
                SELECT COUNT(DISTINCT ts_code) n
                FROM (
                    SELECT ts_code
                    FROM dc_tushare_raw_records
                    WHERE api_name=? AND period<>'' AND period BETWEEN ? AND ? AND ts_code<>''
                    UNION
                    SELECT ts_code
                    FROM dc_tushare_request_coverage
                    WHERE api_name=? AND period<>'' AND period BETWEEN ? AND ? AND ts_code<>''
                      AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')
                ) done
                """,
                (api, financial_start, financial_end, api, financial_start, financial_end),
            ).fetchone()
            financial_symbol_counts[api] = int((dict(row) if row else {}).get("n") or 0)

        # Period coverage for financial statements.
        financial_period_counts = {
            api: int((raw_period.get(api) or {}).get("periods") or 0)
            for api in ("income", "balancesheet", "cashflow", "fina_indicator", "fina_audit", "fina_mainbz")
        }

        # Event-like tables in the 2-year window.
        announcements = _d(
            c.execute(
                """
                SELECT COUNT(*) records,
                       COUNT(DISTINCT ts_code) symbols,
                       MIN(publish_time) min_date,
                       MAX(publish_time) max_date
                FROM dc_announcement_raw
                WHERE REPLACE(SUBSTR(publish_time, 1, 10), '-', '') BETWEEN ? AND ?
                """,
                (start_s, end_s),
            ).fetchone()
        )
        events = _d(
            c.execute(
                """
                SELECT COUNT(*) records,
                       MIN(event_date) min_date,
                       MAX(event_date) max_date
                FROM dc_announcement_event
                WHERE REPLACE(SUBSTR(event_date, 1, 10), '-', '') BETWEEN ? AND ?
                   OR (event_date <> '' AND LENGTH(REPLACE(event_date, '-', '')) = 8
                       AND REPLACE(event_date, '-', '') BETWEEN ? AND ?)
                """,
                (start_s, end_s, start_s, end_s),
            ).fetchone()
        )
        industry_metrics = _d(
            c.execute(
                """
                SELECT COUNT(*) records,
                       COUNT(DISTINCT metric_code) series_n,
                       MIN(data_date) min_date,
                       MAX(data_date) max_date
                FROM dc_industry_metric_value
                WHERE data_date BETWEEN ? AND ?
                """,
                (financial_start, end_s),
            ).fetchone()
        )
        web = _d(
            c.execute(
                """
                SELECT COUNT(*) records,
                       COUNT(DISTINCT symbol) symbols,
                       MIN(crawled_at) min_date,
                       MAX(crawled_at) max_date
                FROM dc_web_factor_snapshot
                """
            ).fetchone()
        )
        stock_master = _d(
            c.execute(
                """
                SELECT COUNT(*) records,
                       MIN(list_date) min_date,
                       MAX(list_date) max_date
                FROM dc_stock_master
                """
            ).fetchone()
        )
        etf_pcf = _d(
            c.execute(
                """SELECT COUNT(*) records, COUNT(DISTINCT etf_code) symbols,
                          COUNT(DISTINCT trade_date) dates, MIN(trade_date) min_date, MAX(trade_date) max_date
                   FROM dc_etf_pcf_header WHERE trade_date>=?""",
                (start_s,),
            ).fetchone()
        )
        pcf_latest_date = str(etf_pcf.get("max_date") or end_s)
        etf_pcf_latest = _i(c.execute(
            "SELECT COUNT(DISTINCT etf_code) n FROM dc_etf_pcf_header WHERE trade_date=?", (pcf_latest_date,)
        ).fetchone())

    def daily_raw(api: str) -> dict[str, Any]:
        return raw_daily.get(api, {})

    def period_raw(api: str) -> dict[str, Any]:
        return raw_period.get(api, {})

    def all_raw(api: str) -> dict[str, Any]:
        return raw_all.get(api, {})

    expected_daily_records = stocks * open_days_expected if stocks else open_days_expected
    # Financial: prefer stock coverage over row count (row count varies by statement type).
    expected_financial_stocks = stocks
    expected_financial_periods = len(financial_periods)
    expected_quarter_periods = len(quarters)
    expected_annual_periods = len(annuals)

    # Build asset definitions: each has an explicit audit window.
    # measurement:
    #   coverage_dates  -> actual dates / expected open days
    #   coverage_stocks -> actual symbols / expected stocks
    #   coverage_records -> actual records / expected records
    #   event_count     -> no fixed expected volume
    #   snapshot        -> latest master snapshot only
    specs: list[dict[str, Any]] = [
        {
            "key": "exchange_etf_pcf",
            "name": "ETF每日申购赎回篮子（PCF）",
            "domain": "基金与跨资产",
            "consumer": "长线资产暴露、ETF轮动、申赎结构与偏离核验",
            "weight": 25,
            "target_scope": "ALIS长线ETF清单；默认5个防御资产加沪深宽基校验样本，可配置扩展",
            "frequency": "daily_premarket",
            "source": "上海证券交易所 / 深圳证券交易所官方PCF",
            "window_start": start_s,
            "window_end": pcf_latest_date,
            "window_depth": "深市按公开历史回补；沪市从接入日起归档",
            "measurement": "coverage_records",
            "actual": etf_pcf_latest,
            "expected": len(tracked_etf_codes()),
            "symbols": int(etf_pcf.get("symbols") or 0),
            "dates": int(etf_pcf.get("dates") or 0),
            "min_date": etf_pcf.get("min_date"),
            "max_date": etf_pcf.get("max_date"),
            "records_actual": int(etf_pcf.get("records") or 0),
            "rationale": "PCF是每日真实申赎篮子；fund_portfolio与指数权重不得冒充。上交所历史不可得日期保持缺失。",
        },
        {
            "key": "capital_structure_vip",
            "name": "资金与席位接口汇总",
            "domain": "资金与席位",
            "consumer": "交易决策、量化分析、回测、模型训练",
            "weight": 50,
            "target_scope": "2020年至今精确接口切片",
            "frequency": "daily",
            "source": "Tushare moneyflow_hsgt/margin/margin_detail/margin_secs/top_list/top_inst/hk_hold",
            "window_start": vip["scope"]["capital_start"],
            "window_end": vip["scope"]["end_date"],
            "window_depth": "至少6年",
            "measurement": "coverage_records",
            "actual": vip["capital"]["completed_slices"],
            "expected": vip["capital"]["expected_slices"],
            "symbols": 0,
            "dates": vip["scope"]["trade_dates"],
            "min_date": vip["scope"]["capital_start"],
            "max_date": vip["scope"]["end_date"],
            "rationale": "按完整请求参数终态水位验收，不以有若干原始行冒充完整",
        },
        {
            "key": "etf_price_nav_vip",
            "name": "ETF价格与净值接口汇总",
            "domain": "基金与跨资产",
            "consumer": "回测、跨资产轮动、模型训练",
            "weight": 50,
            "target_scope": "全部已上市ETF六年日线与NAV",
            "frequency": "daily",
            "source": "Tushare fund_basic/fund_daily/fund_nav",
            "window_start": vip["scope"]["capital_start"],
            "window_end": vip["scope"]["end_date"],
            "window_depth": "至少6年",
            "measurement": "coverage_records",
            "actual": vip["etf_price_nav"]["completed_slices"],
            "expected": vip["etf_price_nav"]["expected_slices"],
            "symbols": vip["scope"]["etfs"],
            "dates": 0,
            "min_date": None,
            "max_date": None,
            "rationale": "每只ETF的fund_daily与fund_nav均达到终态才算完成",
        },
        {
            "key": "major_news_vip",
            "name": "九来源长篇新闻历史",
            "domain": "事件与叙事",
            "consumer": "量化分析、持仓风控、叙事模型训练",
            "weight": 50,
            "target_scope": "三年、九来源、逐自然日",
            "frequency": "daily",
            "source": "Tushare major_news",
            "window_start": vip["scope"]["news_start"],
            "window_end": vip["scope"]["end_date"],
            "window_depth": "至少3年",
            "measurement": "coverage_records",
            "actual": vip["major_news"]["completed_slices"],
            "expected": vip["major_news"]["expected_slices"],
            "symbols": vip["scope"]["news_sources"],
            "dates": 0,
            "min_date": None,
            "max_date": None,
            "rationale": "按来源×自然日终态切片验收，空日也必须有valid_empty证据",
        },
        {
            "key": "stock_master",
            "name": "股票基础与上市状态",
            "domain": "基础资料",
            "consumer": "全部模块",
            "weight": 100,
            "target_scope": "沪深A股当前上市股票主数据",
            "frequency": "weekly",
            "source": "Tushare stock_basic",
            "window_start": end_s,
            "window_end": end_s,
            "window_depth": "单点快照",
            "measurement": "coverage_stocks",
            "actual": int(stock_master.get("records") or 0),
            "expected": max(stocks, 5000),
            "symbols": stocks,
            "dates": 0,
            "min_date": stock_master.get("min_date"),
            "max_date": stock_master.get("max_date"),
            "rationale": "只需要最新快照，不存历史主数据",
        },
        {
            "key": "namechange",
            "name": "曾用名 / ST 变更",
            "domain": "基础资料",
            "consumer": "治理风险、回测股票状态",
            "weight": 100,
            "target_scope": "近两年名称与 ST 变更",
            "frequency": "monthly",
            "source": "Tushare namechange",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": int(all_raw("namechange").get("records") or 0),
            "expected": None,
            "symbols": int(all_raw("namechange").get("symbols") or 0),
            "dates": int(all_raw("namechange").get("dates") or 0),
            "min_date": all_raw("namechange").get("min_date"),
            "max_date": all_raw("namechange").get("max_date"),
            "rationale": "事件型；多数股票不会改名或进入 ST，不能按全股票覆盖率判缺失",
        },
        {
            "key": "industry_classification",
            "name": "行业分类",
            "domain": "基础资料",
            "consumer": "行业/板块景气度 25%",
            "weight": 25,
            "target_scope": "每只股票当前行业标签",
            "frequency": "monthly",
            "source": "Tushare stock_basic.industry",
            "window_start": end_s,
            "window_end": end_s,
            "window_depth": "单点快照",
            "measurement": "coverage_stocks",
            "actual": industry_named,
            "expected": stocks,
            "symbols": industry_named,
            "dates": 0,
            "min_date": None,
            "max_date": None,
            "rationale": "当前行业字段覆盖率",
        },
        {
            "key": "trade_calendar",
            "name": "交易日历",
            "domain": "市场数据",
            "consumer": "趋势、回测、任务调度",
            "weight": 15,
            "target_scope": "滚动两年沪深开市日 + 半年未来",
            "frequency": "monthly",
            "source": "Tushare trade_cal",
            "window_start": start_s,
            "window_end": min(end + timedelta(days=180), date(end.year, 12, 31)).strftime("%Y%m%d"),
            "window_depth": "2.5年",
            "measurement": "coverage_dates",
            "actual": cal_date_rows,
            "expected": calendar_expected_days,
            "symbols": 0,
            "dates": cal_date_rows,
            "min_date": cal_min,
            "max_date": cal_max,
            "rationale": "按两年历史至未来半年范围内逐个自然日检查，周末和休市日也必须有记录",
        },
        {
            "key": "daily",
            "name": "原始日线行情",
            "domain": "市场数据",
            "consumer": "股价趋势/技术状态 15%",
            "weight": 15,
            "target_scope": "沪深A股 × 两年交易日",
            "frequency": "daily",
            "source": "Tushare daily",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "~500交易日",
            "measurement": "coverage_dates",
            "actual": int(daily_raw("daily").get("dates") or 0),
            "expected": open_days_expected,
            "symbols": int(daily_raw("daily").get("symbols") or 0),
            "dates": int(daily_raw("daily").get("dates") or 0),
            "min_date": daily_raw("daily").get("min_date"),
            "max_date": daily_raw("daily").get("max_date"),
            "rationale": "按交易日覆盖度计；全市场日线按日批量入库",
            "records_actual": int(daily_raw("daily").get("records") or 0),
            "records_expected": expected_daily_records,
        },
        {
            "key": "adj_factor",
            "name": "复权因子",
            "domain": "市场数据",
            "consumer": "趋势、历史回测",
            "weight": 15,
            "target_scope": "沪深A股 × 两年交易日",
            "frequency": "daily",
            "source": "Tushare adj_factor",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "~500交易日",
            "measurement": "coverage_dates",
            "actual": int(daily_raw("adj_factor").get("dates") or 0),
            "expected": open_days_expected,
            "symbols": int(daily_raw("adj_factor").get("symbols") or 0),
            "dates": int(daily_raw("adj_factor").get("dates") or 0),
            "min_date": daily_raw("adj_factor").get("min_date"),
            "max_date": daily_raw("adj_factor").get("max_date"),
            "rationale": "与日线同窗口；按交易日覆盖度计",
            "records_actual": int(daily_raw("adj_factor").get("records") or 0),
            "records_expected": expected_daily_records,
        },
        {
            "key": "limit_price",
            "name": "涨跌停价格",
            "domain": "市场数据",
            "consumer": "交易决策、风控、回测",
            "weight": 15,
            "target_scope": "两年每日涨跌停价格",
            "frequency": "daily",
            "source": "Tushare stk_limit",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "~500交易日",
            "measurement": "coverage_dates",
            "actual": int(daily_raw("stk_limit").get("dates") or 0),
            "expected": open_days_expected,
            "symbols": int(daily_raw("stk_limit").get("symbols") or 0),
            "dates": int(daily_raw("stk_limit").get("dates") or 0),
            "min_date": daily_raw("stk_limit").get("min_date"),
            "max_date": daily_raw("stk_limit").get("max_date"),
            "rationale": "涨跌停按交易日全市场返回，按日期覆盖度计",
            "records_actual": int(daily_raw("stk_limit").get("records") or 0),
            "records_expected": expected_daily_records,
        },
        {
            "key": "suspension",
            "name": "停复牌记录",
            "domain": "市场数据",
            "consumer": "交易决策、风控、回测",
            "weight": 15,
            "target_scope": "两年停复牌事件及每日检查记录",
            "frequency": "daily",
            "source": "Tushare suspend_d",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "~500交易日",
            "measurement": "coverage_dates",
            "actual": max(
                int(daily_raw("suspend_d").get("checked_dates") or 0),
                int(daily_raw("suspend_d").get("dates") or 0),
            ),
            "expected": open_days_expected,
            "symbols": int(daily_raw("suspend_d").get("symbols") or 0),
            "dates": max(
                int(daily_raw("suspend_d").get("checked_dates") or 0),
                int(daily_raw("suspend_d").get("dates") or 0),
            ),
            "min_date": daily_raw("suspend_d").get("checked_min_date") or daily_raw("suspend_d").get("min_date"),
            "max_date": daily_raw("suspend_d").get("checked_max_date") or daily_raw("suspend_d").get("max_date"),
            "rationale": "稀疏事件接口；完整度按每日请求检查覆盖率计，空结果代表当日无停复牌事件且属于有效检查",
            "records_actual": int(daily_raw("suspend_d").get("records") or 0),
            "records_expected": None,
        },
        {
            "key": "valuation",
            "name": "日度估值",
            "domain": "市场数据",
            "consumer": "财务/估值 40%",
            "weight": 40,
            "target_scope": "PE/PB/PS/市值/换手率，两年日频",
            "frequency": "daily",
            "source": "Tushare daily_basic",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "~500交易日",
            "measurement": "coverage_dates",
            "actual": int(daily_raw("daily_basic").get("dates") or 0),
            "expected": open_days_expected,
            "symbols": int(daily_raw("daily_basic").get("symbols") or 0),
            "dates": int(daily_raw("daily_basic").get("dates") or 0),
            "min_date": daily_raw("daily_basic").get("min_date"),
            "max_date": daily_raw("daily_basic").get("max_date"),
            "rationale": "估值分析核心；按交易日覆盖度计",
            "records_actual": int(daily_raw("daily_basic").get("records") or 0),
            "records_expected": expected_daily_records,
        },
        {
            "key": "moneyflow",
            "name": "个股资金流",
            "domain": "资金",
            "consumer": "资金确认、短中期辅助",
            "weight": 15,
            "target_scope": "沪深A股两年日频资金流",
            "frequency": "daily",
            "source": "Tushare moneyflow",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "coverage_dates",
            "actual": max(
                int(daily_raw("moneyflow").get("checked_dates") or 0),
                int(daily_raw("moneyflow").get("dates") or 0),
            ),
            "expected": open_days_expected,
            "symbols": int(daily_raw("moneyflow").get("symbols") or 0),
            "dates": max(
                int(daily_raw("moneyflow").get("checked_dates") or 0),
                int(daily_raw("moneyflow").get("dates") or 0),
            ),
            "min_date": daily_raw("moneyflow").get("checked_min_date") or daily_raw("moneyflow").get("min_date"),
            "max_date": daily_raw("moneyflow").get("checked_max_date") or daily_raw("moneyflow").get("max_date"),
            "rationale": "按交易日终态覆盖度计；成功写入为完整数据，多次空结果会转入 deferred_empty 并进入备用源/异常报告",
        },
        {
            "key": "income",
            "name": "利润表",
            "domain": "财务数据",
            "consumer": "财务/估值 40%",
            "weight": 40,
            "target_scope": f"最近{FINANCIAL_QUARTERS}季度 + {FINANCIAL_ANNUAL_YEARS}年度",
            "frequency": "quarterly",
            "source": "Tushare income",
            "window_start": financial_start,
            "window_end": financial_end,
            "window_depth": f"{FINANCIAL_QUARTERS}季度+{FINANCIAL_ANNUAL_YEARS}年度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("income", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("income", 0),
            "dates": financial_period_counts.get("income", 0),
            "min_date": period_raw("income").get("min_date"),
            "max_date": period_raw("income").get("max_date"),
            "rationale": "按股票覆盖度计；报告期窗口 " + ",".join(quarters[:4]) + "...",
            "periods_actual": financial_period_counts.get("income", 0),
            "periods_expected": expected_financial_periods,
        },
        {
            "key": "balancesheet",
            "name": "资产负债表",
            "domain": "财务数据",
            "consumer": "财务/估值 40%",
            "weight": 40,
            "target_scope": f"最近{FINANCIAL_QUARTERS}季度 + {FINANCIAL_ANNUAL_YEARS}年度",
            "frequency": "quarterly",
            "source": "Tushare balancesheet",
            "window_start": financial_start,
            "window_end": financial_end,
            "window_depth": f"{FINANCIAL_QUARTERS}季度+{FINANCIAL_ANNUAL_YEARS}年度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("balancesheet", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("balancesheet", 0),
            "dates": financial_period_counts.get("balancesheet", 0),
            "min_date": period_raw("balancesheet").get("min_date"),
            "max_date": period_raw("balancesheet").get("max_date"),
            "rationale": "与利润表同窗口",
            "periods_actual": financial_period_counts.get("balancesheet", 0),
            "periods_expected": expected_financial_periods,
        },
        {
            "key": "cashflow",
            "name": "现金流量表",
            "domain": "财务数据",
            "consumer": "财务/估值 40%",
            "weight": 40,
            "target_scope": f"最近{FINANCIAL_QUARTERS}季度 + {FINANCIAL_ANNUAL_YEARS}年度",
            "frequency": "quarterly",
            "source": "Tushare cashflow",
            "window_start": financial_start,
            "window_end": financial_end,
            "window_depth": f"{FINANCIAL_QUARTERS}季度+{FINANCIAL_ANNUAL_YEARS}年度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("cashflow", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("cashflow", 0),
            "dates": financial_period_counts.get("cashflow", 0),
            "min_date": period_raw("cashflow").get("min_date"),
            "max_date": period_raw("cashflow").get("max_date"),
            "rationale": "与利润表同窗口",
            "periods_actual": financial_period_counts.get("cashflow", 0),
            "periods_expected": expected_financial_periods,
        },
        {
            "key": "financial_indicator",
            "name": "财务指标",
            "domain": "财务数据",
            "consumer": "财务/估值 40%",
            "weight": 40,
            "target_scope": f"ROE/ROIC/利润率/偿债，最近{FINANCIAL_QUARTERS}季度",
            "frequency": "quarterly",
            "source": "Tushare fina_indicator",
            "window_start": financial_start,
            "window_end": financial_end,
            "window_depth": f"{FINANCIAL_QUARTERS}季度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("fina_indicator", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("fina_indicator", 0),
            "dates": financial_period_counts.get("fina_indicator", 0),
            "min_date": period_raw("fina_indicator").get("min_date"),
            "max_date": period_raw("fina_indicator").get("max_date"),
            "rationale": "按股票覆盖度计",
            "periods_actual": financial_period_counts.get("fina_indicator", 0),
            "periods_expected": expected_quarter_periods,
        },
        {
            "key": "fina_audit",
            "name": "审计意见",
            "domain": "财务数据",
            "consumer": "财务/估值 40%",
            "weight": 40,
            "target_scope": f"最近{FINANCIAL_ANNUAL_YEARS}个完整年度",
            "frequency": "yearly",
            "source": "Tushare fina_audit",
            "window_start": min(annuals) if annuals else financial_start,
            "window_end": max(annuals) if annuals else financial_end,
            "window_depth": f"{FINANCIAL_ANNUAL_YEARS}年度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("fina_audit", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("fina_audit", 0),
            "dates": financial_period_counts.get("fina_audit", 0),
            "min_date": period_raw("fina_audit").get("min_date"),
            "max_date": period_raw("fina_audit").get("max_date"),
            "rationale": "非标审计预警；年度窗口",
            "periods_actual": financial_period_counts.get("fina_audit", 0),
            "periods_expected": expected_annual_periods,
        },
        {
            "key": "fina_mainbz",
            "name": "主营构成",
            "domain": "财务数据",
            "consumer": "财务/估值 40%",
            "weight": 40,
            "target_scope": f"最近{FINANCIAL_ANNUAL_YEARS}个完整年度",
            "frequency": "yearly",
            "source": "Tushare fina_mainbz",
            "window_start": min(annuals) if annuals else financial_start,
            "window_end": max(annuals) if annuals else financial_end,
            "window_depth": f"{FINANCIAL_ANNUAL_YEARS}年度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("fina_mainbz", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("fina_mainbz", 0),
            "dates": financial_period_counts.get("fina_mainbz", 0),
            "min_date": period_raw("fina_mainbz").get("min_date"),
            "max_date": period_raw("fina_mainbz").get("max_date"),
            "rationale": "业务结构分析；年度窗口",
            "periods_actual": financial_period_counts.get("fina_mainbz", 0),
            "periods_expected": expected_annual_periods,
        },
        {
            "key": "shareholder_return",
            "name": "分红、回购与融资",
            "domain": "财务数据",
            "consumer": "股东回报 40%",
            "weight": 40,
            "target_scope": "近两年公司行为事件",
            "frequency": "event",
            "source": "Tushare dividend + 公告",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": int(all_raw("dividend").get("records") or 0),
            "expected": None,
            "symbols": int(all_raw("dividend").get("symbols") or 0),
            "dates": int(all_raw("dividend").get("periods") or 0),
            "min_date": all_raw("dividend").get("min_date"),
            "max_date": all_raw("dividend").get("max_date"),
            "rationale": "事件型，无固定应有量；有记录即 available",
        },
        {
            "key": "top10_holders",
            "name": "十大股东",
            "domain": "股东",
            "consumer": "机构/大股东结构",
            "weight": 25,
            "target_scope": f"最近{FINANCIAL_QUARTERS}季度",
            "frequency": "quarterly",
            "source": "Tushare top10_holders",
            "window_start": financial_start,
            "window_end": financial_end,
            "window_depth": f"{FINANCIAL_QUARTERS}季度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("top10_holders", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("top10_holders", 0),
            "dates": int(period_raw("top10_holders").get("periods") or 0),
            "min_date": period_raw("top10_holders").get("min_date"),
            "max_date": period_raw("top10_holders").get("max_date"),
            "rationale": "按股票覆盖度计",
        },
        {
            "key": "top10_floatholders",
            "name": "十大流通股东",
            "domain": "股东",
            "consumer": "流通股东结构",
            "weight": 25,
            "target_scope": f"最近{FINANCIAL_QUARTERS}季度",
            "frequency": "quarterly",
            "source": "Tushare top10_floatholders",
            "window_start": financial_start,
            "window_end": financial_end,
            "window_depth": f"{FINANCIAL_QUARTERS}季度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("top10_floatholders", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("top10_floatholders", 0),
            "dates": int(period_raw("top10_floatholders").get("periods") or 0),
            "min_date": period_raw("top10_floatholders").get("min_date"),
            "max_date": period_raw("top10_floatholders").get("max_date"),
            "rationale": "按股票覆盖度计",
        },
        {
            "key": "stk_holdernumber",
            "name": "股东人数",
            "domain": "股东",
            "consumer": "筹码集中度",
            "weight": 25,
            "target_scope": f"最近{FINANCIAL_QUARTERS}季度",
            "frequency": "quarterly",
            "source": "Tushare stk_holdernumber",
            "window_start": financial_start,
            "window_end": financial_end,
            "window_depth": f"{FINANCIAL_QUARTERS}季度",
            "measurement": "coverage_stocks",
            "actual": financial_symbol_counts.get("stk_holdernumber", 0),
            "expected": expected_financial_stocks,
            "symbols": financial_symbol_counts.get("stk_holdernumber", 0),
            "dates": int(period_raw("stk_holdernumber").get("periods") or 0),
            "min_date": period_raw("stk_holdernumber").get("min_date"),
            "max_date": period_raw("stk_holdernumber").get("max_date"),
            "rationale": "按股票覆盖度计",
        },
        {
            "key": "pledge_stat",
            "name": "股权质押统计",
            "domain": "股东",
            "consumer": "治理与质押风险",
            "weight": 25,
            "target_scope": "近两年质押统计",
            "frequency": "weekly",
            "source": "Tushare pledge_stat",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": int(all_raw("pledge_stat").get("records") or 0),
            "expected": None,
            "symbols": int(all_raw("pledge_stat").get("symbols") or 0),
            "dates": int(all_raw("pledge_stat").get("periods") or 0),
            "min_date": all_raw("pledge_stat").get("min_date"),
            "max_date": all_raw("pledge_stat").get("max_date"),
            "rationale": "事件型；无质押记录不代表数据缺失，不能按全股票覆盖率判缺口",
        },
        {
            "key": "forecast",
            "name": "业绩预告",
            "domain": "业绩",
            "consumer": "公告/新闻/研报事件 15%",
            "weight": 15,
            "target_scope": "近两年业绩预告事件",
            "frequency": "daily_incremental",
            "source": "Tushare forecast",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": int(all_raw("forecast").get("records") or 0),
            "expected": None,
            "symbols": int(all_raw("forecast").get("symbols") or 0),
            "dates": int(all_raw("forecast").get("periods") or 0),
            "min_date": all_raw("forecast").get("min_date"),
            "max_date": all_raw("forecast").get("max_date"),
            "rationale": "事件型；有记录即 available",
        },
        {
            "key": "express",
            "name": "业绩快报",
            "domain": "业绩",
            "consumer": "公告/新闻/研报事件 15%",
            "weight": 15,
            "target_scope": "近两年业绩快报",
            "frequency": "daily_incremental",
            "source": "Tushare express",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": int(all_raw("express").get("records") or 0),
            "expected": None,
            "symbols": int(all_raw("express").get("symbols") or 0),
            "dates": int(all_raw("express").get("periods") or 0),
            "min_date": all_raw("express").get("min_date"),
            "max_date": all_raw("express").get("max_date"),
            "rationale": "事件型；有记录即 available",
        },
        {
            "key": "announcements",
            "name": "公告原文与结构化事件",
            "domain": "事件数据",
            "consumer": "公告/新闻/研报事件 15%",
            "weight": 15,
            "target_scope": "近两年正式披露与高价值事件",
            "frequency": "daily",
            "source": "巨潮资讯",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": int(announcements.get("records") or 0),
            "expected": None,
            "symbols": int(announcements.get("symbols") or 0),
            "dates": 0,
            "min_date": announcements.get("min_date"),
            "max_date": announcements.get("max_date"),
            "rationale": "事件型；窗口内有公告即 available",
            "events_actual": int(events.get("records") or 0),
        },
        {
            "key": "industry_index",
            "name": "行业指数与板块状态",
            "domain": "行业数据",
            "consumer": "行业/板块景气度 25%",
            "weight": 25,
            "target_scope": "主要行业指数两年日频",
            "frequency": "daily",
            "source": "Tushare index_daily",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "~500交易日",
            "measurement": "coverage_dates",
            "actual": int(daily_raw("index_daily").get("dates") or 0),
            "expected": open_days_expected,
            "symbols": int(daily_raw("index_daily").get("symbols") or all_raw("index_daily").get("symbols") or 0),
            "dates": int(daily_raw("index_daily").get("dates") or 0),
            "min_date": daily_raw("index_daily").get("min_date") or all_raw("index_daily").get("min_date"),
            "max_date": daily_raw("index_daily").get("max_date") or all_raw("index_daily").get("max_date"),
            "rationale": "按交易日覆盖度计",
        },
        {
            "key": "index_basic",
            "name": "指数基础信息",
            "domain": "指数",
            "consumer": "大盘/外围/宏观 5%",
            "weight": 5,
            "target_scope": "指数元数据最新快照",
            "frequency": "monthly",
            "source": "Tushare index_basic",
            "window_start": end_s,
            "window_end": end_s,
            "window_depth": "单点快照",
            "measurement": "event_count",
            "actual": int(all_raw("index_basic").get("records") or 0),
            "expected": None,
            "symbols": int(all_raw("index_basic").get("symbols") or 0),
            "dates": 0,
            "min_date": all_raw("index_basic").get("min_date"),
            "max_date": all_raw("index_basic").get("max_date"),
            "rationale": "快照型；有记录即 available",
        },
        {
            "key": "index_weight",
            "name": "指数权重",
            "domain": "指数",
            "consumer": "大盘/外围/宏观 5%",
            "weight": 5,
            "target_scope": "沪深300/中证成分权重，两年",
            "frequency": "monthly",
            "source": "Tushare index_weight",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": int(all_raw("index_weight").get("records") or 0),
            "expected": None,
            "symbols": int(all_raw("index_weight").get("symbols") or 0),
            "dates": int(all_raw("index_weight").get("dates") or 0),
            "min_date": all_raw("index_weight").get("min_date"),
            "max_date": all_raw("index_weight").get("max_date"),
            "rationale": "事件/月更型；有记录即 available",
        },
        {
            "key": "industry_metrics",
            "name": "行业经营景气指标",
            "domain": "行业数据",
            "consumer": "行业/板块景气度 25%",
            "weight": 25,
            "target_scope": f"公司财务聚合 + 重点行业专属指标，{FINANCIAL_QUARTERS}季度",
            "frequency": "quarterly",
            "source": "内部聚合 + 行业源",
            "window_start": financial_start,
            "window_end": end_s,
            "window_depth": f"{FINANCIAL_QUARTERS}季度",
            "measurement": "event_count",
            "actual": int(industry_metrics.get("records") or 0),
            "expected": None,
            "symbols": int(industry_metrics.get("series_n") or 0),
            "dates": 0,
            "min_date": industry_metrics.get("min_date"),
            "max_date": industry_metrics.get("max_date"),
            "rationale": "手工/导入型；有序列即 available",
        },
        {
            "key": "macro",
            "name": "大盘、外围与宏观序列",
            "domain": "宏观数据",
            "consumer": "大盘/外围/宏观 5%",
            "weight": 5,
            "target_scope": "宽基指数、利率、汇率、核心宏观，两年",
            "frequency": "daily/monthly",
            "source": "Tushare cn_gdp/cn_cpi/cn_pmi/shibor/fx_daily",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "2年",
            "measurement": "event_count",
            "actual": sum(int(all_raw(api).get("records") or 0) for api in ("cn_gdp", "cn_cpi", "cn_pmi", "shibor", "fx_daily")),
            "expected": None,
            "symbols": sum(1 for api in ("cn_gdp", "cn_cpi", "cn_pmi", "shibor", "fx_daily") if int(all_raw(api).get("records") or 0) > 0),
            "dates": int(all_raw("shibor").get("dates") or 0) + int(all_raw("fx_daily").get("dates") or 0),
            "min_date": min((str(all_raw(api).get("min_date")) for api in ("cn_gdp", "cn_cpi", "cn_pmi", "shibor", "fx_daily") if all_raw(api).get("min_date")), default=None),
            "max_date": max((str(all_raw(api).get("max_date")) for api in ("cn_gdp", "cn_cpi", "cn_pmi", "shibor", "fx_daily") if all_raw(api).get("max_date")), default=None),
            "rationale": "宏观为混合频率序列；五类核心序列均通过真实接口验证，有记录即 available",
        },
        {
            "key": "web_factors",
            "name": "网页/F10 兜底因子",
            "domain": "兜底",
            "consumer": "结构化数据缺失时补充",
            "weight": 0,
            "target_scope": "按需采集，不做全量历史",
            "frequency": "on_demand",
            "source": "Playwright + OCR",
            "window_start": "按需",
            "window_end": "按需",
            "window_depth": "按需",
            "measurement": "event_count",
            "actual": int(web.get("records") or 0),
            "expected": None,
            "symbols": int(web.get("symbols") or 0),
            "dates": 0,
            "min_date": web.get("min_date"),
            "max_date": web.get("max_date"),
            "rationale": "兜底通道；有快照即 available，不计入主完整度目标",
        },
        {
            "key": "snapshot",
            "name": "股票每日快照",
            "domain": "市场数据",
            "consumer": "决策、看板、质量检查",
            "weight": 15,
            "target_scope": "两年交易日 × 沪深A股",
            "frequency": "daily",
            "source": "内部构建 dc_stock_daily_snapshot",
            "window_start": start_s,
            "window_end": end_s,
            "window_depth": "~500交易日",
            "measurement": "coverage_dates",
            "actual": int(snap.get("dates") or 0),
            "expected": open_days_expected,
            "symbols": 0,
            "dates": int(snap.get("dates") or 0),
            "min_date": snap.get("min_date"),
            "max_date": snap.get("max_date"),
            "rationale": "由 daily+valuation+limit 合成；按交易日覆盖度计",
            "records_actual": int(snap.get("records") or 0),
            "records_expected": expected_daily_records,
        },
    ]

    # Expose every contracted VIP/high-entitlement dataset independently.
    # Aggregate cards are useful operationally, but previously hid a missing
    # API (notably ETF disclosed baskets) behind otherwise complete siblings.
    dataset_meta = {
        "moneyflow_hsgt": ("沪深港通流向", "资金", "市场状态、外资风险偏好"),
        "hk_hold": ("沪深股通持股", "资金", "个股与行业外资持仓变化"),
        "margin": ("两融汇总", "两融", "市场杠杆风险偏好"),
        "margin_detail": ("两融明细", "两融", "个股资金持续与拥挤度"),
        "margin_secs": ("两融标的", "两融", "历史可融资融券资格"),
        "top_list": ("龙虎榜每日明细", "席位", "异动和游资强度"),
        "top_inst": ("龙虎榜机构明细", "席位", "机构与营业部结构"),
        "fund_basic": ("ETF基础信息", "基金与跨资产", "ETF资产池与上市状态"),
        "fund_daily": ("ETF日线", "基金与跨资产", "ETF趋势与轮动"),
        "fund_nav": ("ETF净值", "基金与跨资产", "折溢价、规模与流动性"),
    }
    for api_name, (name, domain, consumer) in dataset_meta.items():
        contract = vip.get("datasets", {}).get(api_name, {})
        expected = int(contract.get("expected_slices") or 0)
        actual = int(contract.get("completed_slices") or 0)
        specs.append({
            "key": f"vip_{api_name}",
            "name": name,
            "domain": domain,
            "consumer": consumer,
            "weight": 25,
            "target_scope": "2020年至今，按完整请求参数切片验收",
            "frequency": "monthly" if api_name == "fund_basic" else "daily",
            "source": f"Tushare {api_name}",
            "window_start": vip["scope"]["capital_start"],
            "window_end": vip["scope"]["end_date"],
            "window_depth": "至少6年",
            "measurement": "coverage_records",
            "actual": actual,
            "expected": expected,
            "symbols": vip["scope"]["etfs"] if api_name.startswith("fund_") else 0,
            "dates": vip["scope"]["trade_dates"] if api_name != "fund_basic" else 0,
            "min_date": vip["scope"]["capital_start"],
            "max_date": vip["scope"]["end_date"],
            "rationale": "独立接口终态水位；不能由同组其他接口的完成状态代替",
        })

    items: list[dict[str, Any]] = []
    for spec in specs:
        actual = int(spec["actual"] or 0)
        expected = spec["expected"]
        event_like = spec["measurement"] == "event_count"
        cov = _coverage(actual, expected)
        state = _status(actual, expected, cov, event_like=event_like)
        # For stock_master, treat >=5000 listed A-shares as complete even if expected uses max().
        if spec["key"] == "stock_master" and actual >= 5000:
            cov = 1.0
            state = "complete"
        # Calendar is a scheduling dimension, not a sampled metric: one
        # missing natural date is a real hole and must not be rounded to full.
        if spec["key"] == "trade_calendar":
            state = "complete" if expected is not None and actual >= int(expected) else "partial"
        items.append(
            {
                "key": spec["key"],
                "name": spec["name"],
                "domain": spec["domain"],
                "consumer": spec["consumer"],
                "weight": spec["weight"],
                "target_scope": spec["target_scope"],
                "frequency": spec["frequency"],
                "source": spec["source"],
                "window_start": spec["window_start"],
                "window_end": spec["window_end"],
                "window_depth": spec["window_depth"],
                "rationale": spec.get("rationale", ""),
                "actual_records": int(spec.get("records_actual", actual) or actual),
                "expected_records": (
                    None if event_like else int(spec.get("records_expected", expected) or expected or 0)
                ),
                "coverage": cov,
                "coverage_basis": actual,
                "coverage_expected": expected,
                "symbols": int(spec.get("symbols") or 0),
                "dates": int(spec.get("dates") or 0),
                "periods_actual": spec.get("periods_actual"),
                "periods_expected": spec.get("periods_expected"),
                "min_date": spec.get("min_date"),
                "max_date": spec.get("max_date"),
                "status": state,
                "measurement": spec["measurement"],
            }
        )

    scored = [x for x in items if x["weight"] > 0 and x["measurement"] != "event_count"]
    avg_coverage = (
        round(sum(float(x["coverage"] or 0) for x in scored) / len(scored), 6) if scored else 0.0
    )

    return {
        "ok": True,
        "scope": {
            "market": "沪深A股",
            "start_date": start_s,
            "end_date": end_s,
            "stocks": stocks,
            "open_days": open_days_expected,
            "open_days_in_calendar": cal_window,
            "calendar_total_open_days": cal_total,
            "financial": f"最近{FINANCIAL_QUARTERS}季度 + {FINANCIAL_ANNUAL_YEARS}年度",
            "financial_periods": financial_periods,
            "financial_start": financial_start,
            "financial_end": financial_end,
            "policy": {
                "daily_lookback_days": DAILY_LOOKBACK_DAYS,
                "financial_quarters": FINANCIAL_QUARTERS,
                "financial_annual_years": FINANCIAL_ANNUAL_YEARS,
                "fallback_open_days": FALLBACK_OPEN_DAYS,
            },
        },
        "summary": {
            "datasets": len(items),
            "actual_records": sum(int(x["actual_records"] or 0) for x in items),
            "complete": sum(1 for x in items if x["status"] == "complete"),
            "partial": sum(1 for x in items if x["status"] == "partial"),
            "available": sum(1 for x in items if x["status"] == "available"),
            "missing": sum(1 for x in items if x["status"] == "missing"),
            "average_coverage": avg_coverage,
        },
        "items": items,
    }
