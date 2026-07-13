#!/usr/bin/env python3
from __future__ import annotations

"""
TuShare 小样本权限探测。

原则：
- 严格复用项目现有 TuShare 初始化方式：
  app.tushare_client.load_tushare_token_from_env
  app.tushare_client.create_strict_tushare_pro
- 每个接口只取极小样本，避免触发限流或拉取大批量数据。
- 不打印 token。

用法（在 backend 目录下）：
  .venv/bin/python scripts/probe_tushare_permissions.py
  .venv/bin/python scripts/probe_tushare_permissions.py --skip-success-from .cache/tushare_permission_probe.json
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

if __name__ == "__main__":
    backend = Path(__file__).resolve().parent.parent
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

from app.data_platform.tushare_client import create_strict_tushare_pro, load_tushare_token_from_env


@dataclass(frozen=True)
class ProbeCase:
    group: str
    api: str
    params: dict[str, Any]
    fields: str
    note: str


def _last_completed_weekday() -> date:
    """Choose a completed weekday instead of a stale or intraday date."""
    value = date.today() - timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


_PROBE_END = _last_completed_weekday()
DEFAULT_TRADE_DATE = _PROBE_END.strftime("%Y%m%d")
DEFAULT_START_DATE = (_PROBE_END - timedelta(days=10)).strftime("%Y%m%d")
DEFAULT_END_DATE = DEFAULT_TRADE_DATE
DEFAULT_PERIOD = f"{_PROBE_END.year - 1}1231"
DEFAULT_TS_CODE = "000001.SZ"


PROBE_CASES: list[ProbeCase] = [
    ProbeCase("基础", "stock_basic", {"exchange": "", "list_status": "L"}, "ts_code,symbol,name,area,industry,list_date", "股票基础信息"),
    ProbeCase("基础", "trade_cal", {"exchange": "SSE", "start_date": DEFAULT_START_DATE, "end_date": DEFAULT_END_DATE}, "exchange,cal_date,is_open,pretrade_date", "交易日历"),
    ProbeCase("基础", "namechange", {"ts_code": DEFAULT_TS_CODE}, "ts_code,name,start_date,end_date,change_reason", "曾用名/名称变更"),
    ProbeCase("行情", "daily", {"trade_date": DEFAULT_TRADE_DATE}, "ts_code,trade_date,open,high,low,close,vol,amount", "A股日线"),
    ProbeCase("行情", "weekly", {"ts_code": DEFAULT_TS_CODE, "start_date": "20260601", "end_date": DEFAULT_END_DATE}, "ts_code,trade_date,open,high,low,close,vol,amount", "周线"),
    ProbeCase("行情", "monthly", {"ts_code": DEFAULT_TS_CODE, "start_date": "20260101", "end_date": DEFAULT_END_DATE}, "ts_code,trade_date,open,high,low,close,vol,amount", "月线"),
    ProbeCase("行情", "adj_factor", {"ts_code": DEFAULT_TS_CODE, "start_date": DEFAULT_START_DATE, "end_date": DEFAULT_END_DATE}, "ts_code,trade_date,adj_factor", "复权因子"),
    ProbeCase("估值", "daily_basic", {"trade_date": DEFAULT_TRADE_DATE}, "ts_code,trade_date,close,turnover_rate,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,total_mv,circ_mv", "每日指标/估值/市值"),
    ProbeCase("交易状态", "suspend_d", {"trade_date": DEFAULT_TRADE_DATE}, "ts_code,trade_date,suspend_timing,suspend_type", "停复牌"),
    ProbeCase("交易状态", "stk_limit", {"trade_date": DEFAULT_TRADE_DATE}, "ts_code,trade_date,up_limit,down_limit", "涨跌停价"),
    ProbeCase("资金", "moneyflow", {"ts_code": DEFAULT_TS_CODE, "start_date": DEFAULT_START_DATE, "end_date": DEFAULT_END_DATE}, "ts_code,trade_date,buy_sm_vol,sell_sm_vol,buy_lg_vol,sell_lg_vol,net_mf_vol,net_mf_amount", "个股资金流向"),
    ProbeCase("财务", "income", {"ts_code": DEFAULT_TS_CODE, "period": DEFAULT_PERIOD}, "ts_code,ann_date,f_ann_date,end_date,report_type,total_revenue,n_income", "利润表"),
    ProbeCase("财务", "balancesheet", {"ts_code": DEFAULT_TS_CODE, "period": DEFAULT_PERIOD}, "ts_code,ann_date,f_ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int", "资产负债表"),
    ProbeCase("财务", "cashflow", {"ts_code": DEFAULT_TS_CODE, "period": DEFAULT_PERIOD}, "ts_code,ann_date,f_ann_date,end_date,n_cashflow_act,n_cash_flows_inv_act,n_cash_flows_fnc_act", "现金流量表"),
    ProbeCase("财务", "fina_indicator", {"ts_code": DEFAULT_TS_CODE, "period": DEFAULT_PERIOD}, "ts_code,ann_date,end_date,roe,roa,grossprofit_margin,netprofit_margin,debt_to_assets,ocf_to_or", "财务指标"),
    ProbeCase("财务", "fina_audit", {"ts_code": DEFAULT_TS_CODE, "period": "20251231"}, "ts_code,ann_date,end_date,audit_result,audit_fees,audit_agency", "审计意见"),
    ProbeCase("财务", "fina_mainbz", {"ts_code": DEFAULT_TS_CODE, "period": "20251231", "type": "P"}, "ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost,curr_type", "主营构成"),
    ProbeCase("业绩", "forecast", {"ts_code": DEFAULT_TS_CODE, "ann_date": ""}, "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,summary", "业绩预告"),
    ProbeCase("业绩", "express", {"period": DEFAULT_PERIOD}, "ts_code,ann_date,end_date,revenue,total_profit,n_income,total_assets", "业绩快报（全市场已完成报告期）"),
    ProbeCase("分红", "dividend", {"ts_code": DEFAULT_TS_CODE}, "ts_code,end_date,ann_date,div_proc,stk_div,cash_div_tax,record_date,ex_date,pay_date", "分红送股"),
    ProbeCase("股东", "top10_holders", {"ts_code": DEFAULT_TS_CODE, "period": DEFAULT_PERIOD}, "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio", "十大股东"),
    ProbeCase("股东", "top10_floatholders", {"ts_code": DEFAULT_TS_CODE, "period": DEFAULT_PERIOD}, "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio", "十大流通股东"),
    ProbeCase("股东", "stk_holdernumber", {"ts_code": DEFAULT_TS_CODE, "enddate": DEFAULT_PERIOD}, "ts_code,ann_date,end_date,holder_num", "股东人数"),
    ProbeCase("质押", "pledge_stat", {"ts_code": DEFAULT_TS_CODE}, "ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,total_share,pledge_ratio", "股权质押统计"),
    ProbeCase("质押", "pledge_detail", {"ts_code": DEFAULT_TS_CODE}, "ts_code,ann_date,holder_name,pledge_amount,start_date,end_date,is_release", "股权质押明细"),
    ProbeCase("公告", "disclosure_date", {"ts_code": DEFAULT_TS_CODE, "end_date": DEFAULT_PERIOD}, "ts_code,ann_date,end_date,pre_date,actual_date,modify_date", "财报披露计划"),
    ProbeCase("指数", "index_basic", {"market": "SSE"}, "ts_code,name,market,publisher,category,base_date,list_date", "指数基础信息"),
    ProbeCase("指数", "index_daily", {"ts_code": "000001.SH", "start_date": DEFAULT_START_DATE, "end_date": DEFAULT_END_DATE}, "ts_code,trade_date,open,high,low,close,vol,amount", "指数日线"),
    ProbeCase("指数", "index_weight", {"index_code": "000300.SH", "start_date": "20260601", "end_date": DEFAULT_END_DATE}, "index_code,con_code,trade_date,weight", "指数成分权重"),
]


def _call_api(pro: Any, case: ProbeCase) -> pd.DataFrame:
    fn = getattr(pro, case.api, None)
    kwargs = dict(case.params)
    kwargs["fields"] = case.fields
    if callable(fn):
        value = fn(**kwargs)
    else:
        value = pro.query(case.api, **kwargs)
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _sample_records(df: pd.DataFrame, rows: int) -> list[dict[str, Any]]:
    if df.empty or rows <= 0:
        return []
    safe = df.head(rows).copy()
    safe = safe.where(pd.notna(safe), None)
    return safe.to_dict(orient="records")


def _load_skip_apis(path: str, *, retry_empty: bool) -> set[str]:
    if not path:
        return set()
    result_path = Path(path)
    if not result_path.exists():
        print(f"[skip] previous result not found: {result_path}")
        return set()
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[skip] previous result unreadable: {result_path} | {exc}")
        return set()
    if not isinstance(payload, list):
        print(f"[skip] previous result is not a list: {result_path}")
        return set()

    skip: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        api = str(item.get("api") or "").strip()
        ok = bool(item.get("ok"))
        rows = int(item.get("rows") or 0)
        if ok and (rows > 0 or not retry_empty):
            skip.add(api)
    if skip:
        empty_policy = "成功+有数据" if retry_empty else "成功含空结果"
        print(f"[skip] loaded {len(skip)} APIs to skip from {result_path} ({empty_policy})")
    return skip


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe small TuShare permission samples.")
    parser.add_argument("--sleep", type=float, default=1.2, help="Sleep seconds between API calls.")
    parser.add_argument("--sample-rows", type=int, default=1, help="Rows to print for each successful API.")
    parser.add_argument("--json-out", default="", help="Optional JSON result path.")
    parser.add_argument("--apis", default="", help="Comma separated API names to probe.")
    parser.add_argument(
        "--skip-success-from",
        default="",
        help="Previous JSON result path. APIs already successful there will be skipped.",
    )
    parser.add_argument(
        "--retry-empty",
        action="store_true",
        help="When used with --skip-success-from, retry APIs that previously succeeded but returned 0 rows.",
    )
    args = parser.parse_args()

    token = load_tushare_token_from_env()
    pro = create_strict_tushare_pro(token)
    selected = {x.strip() for x in args.apis.split(",") if x.strip()}
    skip_apis = _load_skip_apis(args.skip_success_from, retry_empty=args.retry_empty)
    cases = [case for case in PROBE_CASES if not selected or case.api in selected]
    if skip_apis:
        before = len(cases)
        cases = [case for case in cases if case.api not in skip_apis]
        print(f"[skip] skipped {before - len(cases)} successful APIs; remaining={len(cases)}")

    results: list[dict[str, Any]] = []
    print(f"[probe] cases={len(cases)} sleep={args.sleep}s sample_rows={args.sample_rows}")
    for i, case in enumerate(cases, start=1):
        print(f"[{i:02d}/{len(cases):02d}] {case.group}/{case.api} - {case.note} ... ", end="", flush=True)
        started = time.perf_counter()
        result: dict[str, Any] = {
            "group": case.group,
            "api": case.api,
            "note": case.note,
            "ok": False,
            "rows": 0,
            "columns": [],
            "elapsed_sec": None,
            "error": "",
            "sample": [],
        }
        try:
            df = _call_api(pro, case)
            elapsed = time.perf_counter() - started
            result["elapsed_sec"] = round(elapsed, 3)
            result["ok"] = True
            result["rows"] = int(len(df))
            result["columns"] = [str(c) for c in df.columns.tolist()]
            result["sample"] = _sample_records(df, args.sample_rows)
            status = "OK" if len(df) > 0 else "EMPTY"
            print(f"{status} rows={len(df)} elapsed={elapsed:.2f}s")
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - started
            result["elapsed_sec"] = round(elapsed, 3)
            result["error"] = str(exc)[:500]
            print(f"FAIL elapsed={elapsed:.2f}s error={str(exc)[:160]}")
        results.append(result)
        if i < len(cases) and args.sleep > 0:
            time.sleep(args.sleep)

    ok = sum(1 for item in results if item["ok"] and item["rows"] > 0)
    empty = sum(1 for item in results if item["ok"] and item["rows"] == 0)
    fail = sum(1 for item in results if not item["ok"])
    print(f"[summary] available_with_rows={ok} empty_or_no_sample={empty} failed={fail}")

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[probe] wrote {out_path}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
