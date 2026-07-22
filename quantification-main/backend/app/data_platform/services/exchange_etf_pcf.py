"""Official SSE/SZSE ETF creation-redemption basket ingestion.

The exchange PCF is a daily operational basket, not a quarterly fund holding.
SSE's public query exposes the latest basket; SZSE also exposes dated text files.
Historical completion therefore records only evidence actually returned by the
exchange and never treats an unavailable old SSE date as a valid empty slice.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from app.data_platform.db import _conn

SOURCE_EXCHANGE_PCF = "exchange_etf_pcf"
SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_REPORT_URL = "https://www.szse.cn/api/report/ShowReport/data"
SZSE_FILE_ROOT = "https://reportdocs.static.szse.cn"
HTTP_TIMEOUT = 20
DEFAULT_TRACKED_ETF_CODES = ("511880", "511010", "518880", "512890", "510880", "510300", "159915")


def tracked_etf_codes() -> tuple[str, ...]:
    configured = [re.sub(r"\D", "", item)[:6] for item in os.getenv("ALIS_ETF_PCF_CODES", "").split(",")]
    cleaned = tuple(dict.fromkeys(item for item in configured if len(item) == 6))
    return cleaned or DEFAULT_TRACKED_ETF_CODES


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "").replace("￥", "").replace("元", "").replace("份", "")
    if not text or text in {"-", "--"}:
        return None
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    try:
        value_f = float(text)
        return value_f / 100 if percent else value_f
    except ValueError:
        return None


def _request_json(url: str, *, params: dict[str, Any], referer: str) -> Any:
    response = requests.get(
        url,
        params=params,
        headers={"Referer": referer, "User-Agent": "ALIS/1.0 ETF-PCF research collector"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def fetch_sse_pcf(etf_code: str, trade_date: str = "") -> dict[str, Any]:
    code = re.sub(r"\D", "", etf_code)[:6]
    common = {"isPagination": "false", "FUNDID2": code}
    meta = _request_json(
        SSE_QUERY_URL,
        params={**common, "sqlId": "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C"},
        referer=f"https://www.sse.com.cn/assortment/fund/list/etfinfo/redemptionlist/index.shtml?FUNDID={code}",
    ).get("result") or []
    rows = _request_json(
        SSE_QUERY_URL,
        params={**common, "sqlId": "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_COMPONENT_C"},
        referer=f"https://www.sse.com.cn/assortment/fund/list/etfinfo/redemptionlist/index.shtml?FUNDID={code}",
    ).get("result") or []
    if not meta:
        return {"ok": False, "status": "not_found", "exchange": "SSE", "etf_code": code, "trade_date": trade_date}
    header = meta[0]
    actual_date = str(header.get("TRADING_DAY") or "")
    if trade_date and actual_date != trade_date:
        return {"ok": False, "status": "historical_unavailable", "exchange": "SSE", "etf_code": code,
                "trade_date": trade_date, "latest_available_date": actual_date}
    components = [
        {
            "component_code": str(row.get("INSTRUMENT_ID") or ""),
            "component_name": str(row.get("INSTRUMENT_NAME") or ""),
            "quantity": _number(row.get("QUANTITY")),
            "substitution_flag": str(row.get("SUBSTITUTION_FLAG") or ""),
            "creation_premium_rate": _number(row.get("CREATION_PREMIUM_RATE")),
            "redemption_discount_rate": _number(row.get("REDEMPTION_DISCOUNT_RATE")),
            "substitution_cash_amount": _number(row.get("SUBSTITUTION_CASH_AMOUNT")),
            "listing_market": str(row.get("UNDERLYION_SECURITY_ID") or ""),
            "raw": row,
        }
        for row in rows if row.get("INSTRUMENT_ID")
    ]
    return {
        "ok": bool(components), "status": "success" if components else "invalid_empty", "exchange": "SSE",
        "etf_code": code, "trade_date": actual_date, "fund_name": str(header.get("FUND_NAME") or ""),
        "fund_manager": str(header.get("FUND_COMP_NAME") or ""), "index_code": "",
        "creation_redemption_unit": _number(header.get("CREATION_REDEMPTION_UNIT")),
        "component_count": int(header.get("RECORD_NUM") or len(components)),
        "publish_iopv": str(header.get("PUBLISH_IOPV") or "") == "是", "components": components,
        "source_url": f"{SSE_QUERY_URL}?FUNDID2={code}", "raw_text": json.dumps(header, ensure_ascii=False),
    }


def _szse_file_path(etf_code: str, trade_date: str) -> str:
    payload = _request_json(
        SZSE_REPORT_URL,
        params={"SHOWTYPE": "JSON", "CATALOGID": "sgshqd", "txtJCorDH": etf_code,
                "txtStart": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
                "txtEnd": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}", "PAGENO": 1},
        referer="https://www.szse.cn/disclosure/fund/currency/index.html",
    )
    data = (payload[0].get("data") if isinstance(payload, list) and payload else []) or []
    if not data:
        return ""
    match = re.search(r"encode-open=['\"]([^'\"]+)", str(data[0].get("jjdm") or ""))
    return match.group(1) if match else ""


def _parse_szse_text(text: str, etf_code: str, trade_date: str) -> dict[str, Any]:
    def field(pattern: str) -> str:
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ""

    components: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(\d{6})\s+(\S.*?)\s{2,}([\d.]+)\s+(\S+)(.*)$", line)
        if not match:
            continue
        tail = match.group(5).strip()
        percentages = re.findall(r"-?[\d.]+%", tail)
        amounts = re.findall(r"-?[\d.]{1,20}", tail)
        components.append({
            "component_code": match.group(1), "component_name": match.group(2).strip(),
            "quantity": _number(match.group(3)), "substitution_flag": match.group(4),
            "creation_premium_rate": _number(percentages[0]) if percentages else None,
            "redemption_discount_rate": _number(percentages[1]) if len(percentages) > 1 else None,
            "substitution_cash_amount": _number(amounts[-2]) if len(amounts) >= 2 else None,
            "listing_market": "", "raw": {"line": line.rstrip()},
        })
    declared = _number(field(r"全部申购赎回组合证券只数：\s*([^\s]+)"))
    return {
        "ok": bool(components), "status": "success" if components else "invalid_empty", "exchange": "SZSE",
        "etf_code": etf_code, "trade_date": trade_date,
        "fund_name": field(r"基金名称：\s*(.+)"), "fund_manager": field(r"基金管理公司名称：\s*(.+)"),
        "index_code": field(r"(?:目标|标的)指数代码：\s*(\S+)"),
        "creation_redemption_unit": _number(field(r"最小申购、赎回单位：\s*([^\s]+)")),
        "component_count": int(declared or len(components)), "publish_iopv": "是否需要公布IOPV：  是" in text,
        "components": components, "raw_text": text,
    }


def fetch_szse_pcf(etf_code: str, trade_date: str) -> dict[str, Any]:
    code = re.sub(r"\D", "", etf_code)[:6]
    path = _szse_file_path(code, trade_date)
    if not path:
        return {"ok": False, "status": "not_found", "exchange": "SZSE", "etf_code": code, "trade_date": trade_date}
    url = f"{SZSE_FILE_ROOT}{quote(path, safe='/')}"
    response = requests.get(url, headers={"Referer": "https://www.szse.cn/"}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    text = response.content.decode("gb18030")
    return {**_parse_szse_text(text, code, trade_date), "source_url": url}


def _save_pcf(result: dict[str, Any]) -> int:
    if not result.get("ok"):
        return 0
    now = _now()
    components = list(result.get("components") or [])
    with _conn() as c:
        c.execute(
            """INSERT INTO dc_etf_pcf_header
               (exchange,etf_code,trade_date,fund_name,fund_manager,index_code,creation_redemption_unit,
                component_count,publish_iopv,source_url,source_code,raw_text,available_time,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(exchange,etf_code,trade_date) DO UPDATE SET
                fund_name=excluded.fund_name,fund_manager=excluded.fund_manager,index_code=excluded.index_code,
                creation_redemption_unit=excluded.creation_redemption_unit,component_count=excluded.component_count,
                publish_iopv=excluded.publish_iopv,source_url=excluded.source_url,raw_text=excluded.raw_text,
                available_time=excluded.available_time,updated_at=excluded.updated_at""",
            (result["exchange"], result["etf_code"], result["trade_date"], result.get("fund_name", ""),
             result.get("fund_manager", ""), result.get("index_code", ""), result.get("creation_redemption_unit"),
             result.get("component_count", len(components)), int(bool(result.get("publish_iopv"))), result["source_url"],
             SOURCE_EXCHANGE_PCF, result.get("raw_text", ""), now, now),
        )
        c.execute("DELETE FROM dc_etf_pcf_component WHERE exchange=? AND etf_code=? AND trade_date=?",
                  (result["exchange"], result["etf_code"], result["trade_date"]))
        for item in components:
            c.execute(
                """INSERT INTO dc_etf_pcf_component
                   (exchange,etf_code,trade_date,component_code,component_name,quantity,substitution_flag,
                    creation_premium_rate,redemption_discount_rate,substitution_cash_amount,listing_market,
                    payload_json,source_code,available_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result["exchange"], result["etf_code"], result["trade_date"], item["component_code"],
                 item.get("component_name", ""), item.get("quantity"), item.get("substitution_flag", ""),
                 item.get("creation_premium_rate"), item.get("redemption_discount_rate"),
                 item.get("substitution_cash_amount"), item.get("listing_market", ""),
                 json.dumps(item.get("raw") or {}, ensure_ascii=False), SOURCE_EXCHANGE_PCF, now, now),
            )
    return len(components)


def _etf_universe(trade_date: str) -> list[tuple[str, str]]:
    with _conn() as c:
        rows = c.execute("""SELECT ts_code,payload_json FROM dc_tushare_raw_records
                            WHERE api_name='fund_basic' ORDER BY ts_code""").fetchall()
    tracked = set(tracked_etf_codes())
    universe: list[tuple[str, str]] = []
    for row in rows:
        item = dict(row)
        payload = item.get("payload_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        code = str(item.get("ts_code") or payload.get("ts_code") or "")
        if code[:6] not in tracked:
            continue
        if "ETF" not in str(payload.get("name") or "").upper() or not code.endswith((".SH", ".SZ")):
            continue
        if str(payload.get("list_date") or "") > trade_date or (payload.get("delist_date") and str(payload["delist_date"]) < trade_date):
            continue
        universe.append((code[:6], "SSE" if code.endswith(".SH") else "SZSE"))
    # Keep a deterministic fallback even before fund_basic has been seeded;
    # these codes are an explicit ALIS contract, not a guessed fund universe.
    found = {code for code, _ in universe}
    for code in tracked_etf_codes():
        if code not in found:
            universe.append((code, "SSE" if code.startswith(("5", "6")) else "SZSE"))
    return sorted(universe)


def sync_exchange_etf_pcf(trade_date: str, *, limit_funds: int = 0) -> dict[str, Any]:
    universe = _etf_universe(trade_date)
    if limit_funds > 0:
        universe = universe[:limit_funds]

    def fetch(item: tuple[str, str]) -> dict[str, Any]:
        code, exchange = item
        try:
            result = fetch_sse_pcf(code, trade_date) if exchange == "SSE" else fetch_szse_pcf(code, trade_date)
            return {**result, "rows_written": _save_pcf(result)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "failed", "exchange": exchange, "etf_code": code,
                    "trade_date": trade_date, "error": str(exc)[:300], "rows_written": 0}

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(universe)))) as pool:
        results = list(pool.map(fetch, universe))
    try:
        from app.data_platform.services.assets import invalidate_data_assets_cache
        from app.data_platform.services.readiness import invalidate_readiness_cache
        invalidate_data_assets_cache()
        invalidate_readiness_cache()
    except Exception:
        pass
    successful = [item for item in results if item.get("ok")]
    return {"ok": bool(successful) and not any(item.get("status") == "failed" for item in results),
            "source": SOURCE_EXCHANGE_PCF, "trade_date": trade_date, "funds_expected": len(universe),
            "funds_succeeded": len(successful), "historical_unavailable": sum(item.get("status") == "historical_unavailable" for item in results),
            "not_found": sum(item.get("status") == "not_found" for item in results),
            "failed": sum(item.get("status") == "failed" for item in results),
            "rows_written": sum(int(item.get("rows_written") or 0) for item in results), "results": results}


def backfill_exchange_etf_pcf(*, start_date: str, end_date: str, cursor: str = "", limit_dates: int = 1,
                              limit_funds: int = 0) -> dict[str, Any]:
    with _conn() as c:
        rows = c.execute("""SELECT cal_date FROM dc_trade_calendar WHERE exchange='SSE' AND is_open=1
                            AND cal_date BETWEEN ? AND ? AND cal_date>? ORDER BY cal_date LIMIT ?""",
                         (start_date, end_date, cursor, max(1, min(limit_dates, 20)))).fetchall()
    dates = [str(dict(row).get("cal_date") or "") for row in rows]
    results = [sync_exchange_etf_pcf(day, limit_funds=limit_funds) for day in dates]
    return {"ok": all(bool(item.get("ok")) for item in results) if results else True,
            "start_date": start_date, "end_date": end_date, "cursor": dates[-1] if dates else cursor,
            "dates_processed": len(dates), "has_more": bool(dates and dates[-1] < end_date),
            "rows_written": sum(int(item.get("rows_written") or 0) for item in results), "results": results}
