"""Audited Tencent quote fallback for an incomplete latest-day stk_limit slice."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import requests

from app.data_platform.db import _conn

SOURCE_TENCENT_QUOTE = "tencent_quote_fallback"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
HTTP_TIMEOUT = 20
BATCH_SIZE = 80
MAX_MISSING_CODES = 2500


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_source() -> None:
    now = _now()
    values = (
        SOURCE_TENCENT_QUOTE, "腾讯行情涨跌停价备用源", "quote_api", 80, "fallback",
        "仅在 TuShare 最新交易日 stk_limit 批量切片被截断时补缺；同日时间戳且抽样对账通过后写入。",
        now, now,
    )
    with _conn() as c:
        c.execute(
            """INSERT INTO data_sources
               (code,name,source_type,priority,status,description,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
               name=excluded.name,source_type=excluded.source_type,priority=excluded.priority,
               status=excluded.status,description=excluded.description,updated_at=excluded.updated_at""",
            values,
        )


def _quote_symbol(ts_code: str) -> str:
    code, _, exchange = str(ts_code).partition(".")
    return ("sh" if exchange.upper() == "SH" else "sz") + code


def _parse_quotes(text: str, trade_date: str) -> dict[str, dict[str, Any]]:
    parsed: dict[str, dict[str, Any]] = {}
    for body in re.findall(r'="([^"]*)"', text or ""):
        fields = body.split("~")
        if len(fields) <= 48:
            continue
        code = str(fields[2] or "").strip()
        stamp = str(fields[30] or "").strip()
        if len(code) != 6 or not stamp.startswith(trade_date):
            continue
        try:
            up_limit, down_limit = float(fields[47]), float(fields[48])
        except (TypeError, ValueError):
            continue
        if up_limit <= 0 or down_limit <= 0 or up_limit <= down_limit:
            continue
        exchange = "SH" if fields[0] == "1" else "SZ"
        ts_code = f"{code}.{exchange}"
        parsed[ts_code] = {
            "ts_code": ts_code, "trade_date": trade_date,
            "up_limit": up_limit, "down_limit": down_limit,
            "quote_time": stamp, "source_url": TENCENT_QUOTE_URL,
            "source_code": SOURCE_TENCENT_QUOTE,
            "verification_status": "pending_cross_check",
        }
    return parsed


def fetch_tencent_limit_quotes(ts_codes: list[str], trade_date: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    headers = {"User-Agent": "ALIS/1.0 market-data fallback", "Referer": "https://gu.qq.com/"}
    for start in range(0, len(ts_codes), BATCH_SIZE):
        batch = ts_codes[start:start + BATCH_SIZE]
        response = requests.get(
            TENCENT_QUOTE_URL + ",".join(_quote_symbol(code) for code in batch),
            headers=headers, timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        result.update(_parse_quotes(response.content.decode("gbk", "ignore"), trade_date))
    return result


def _missing_codes(c: Any, trade_date: str) -> list[str]:
    rows = c.execute(
        """SELECT DISTINCT daily.ts_code
           FROM dc_tushare_raw_records daily
           JOIN dc_stock_master stock ON stock.ts_code=daily.ts_code
           LEFT JOIN dc_tushare_raw_records limits
             ON limits.api_name='stk_limit' AND limits.trade_date=daily.trade_date
            AND limits.ts_code=daily.ts_code
           WHERE daily.api_name='daily' AND daily.trade_date=? AND limits.id IS NULL
           ORDER BY daily.ts_code LIMIT ?""",
        (trade_date, MAX_MISSING_CODES),
    ).fetchall()
    return [str(dict(row).get("ts_code") or "") for row in rows]


def _reference_values(c: Any, trade_date: str, limit: int = 12) -> dict[str, tuple[float, float]]:
    rows = c.execute(
        """SELECT ts_code,payload_json FROM dc_tushare_raw_records
           WHERE api_name='stk_limit' AND trade_date=? AND source_code='tushare'
           ORDER BY ts_code LIMIT ?""",
        (trade_date, limit),
    ).fetchall()
    values: dict[str, tuple[float, float]] = {}
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
            values[str(item["ts_code"])] = (float(payload["up_limit"]), float(payload["down_limit"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return values


def repair_stk_limit_from_tencent(trade_date: str) -> dict[str, Any]:
    """Fill only proven gaps after exact same-date reference cross-checking."""
    _ensure_source()
    with _conn() as c:
        missing = _missing_codes(c, trade_date)
        references = _reference_values(c, trade_date)
    if not missing:
        return {"ok": True, "status": "nothing_missing", "rows_written": 0}
    if len(references) < 3:
        return {"ok": False, "status": "insufficient_reference_sample", "rows_written": 0}

    reference_quotes = fetch_tencent_limit_quotes(list(references), trade_date)
    compared, mismatches = 0, []
    for code, expected in references.items():
        quote = reference_quotes.get(code)
        if not quote:
            continue
        compared += 1
        if abs(float(quote["up_limit"]) - expected[0]) > 0.001 or abs(float(quote["down_limit"]) - expected[1]) > 0.001:
            mismatches.append(code)
    if compared < 3 or mismatches:
        return {"ok": False, "status": "cross_check_failed", "compared": compared,
                "mismatches": mismatches, "rows_written": 0}

    quotes = fetch_tencent_limit_quotes(missing, trade_date)
    now, written = _now(), 0
    with _conn() as c:
        for code in missing:
            payload = quotes.get(code)
            if not payload:
                continue
            payload["verification_status"] = "cross_checked"
            payload["reference_sample_size"] = compared
            values = (
                "stk_limit", f"stk_limit:{trade_date}:{code}", code, trade_date, "", "",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                SOURCE_TENCENT_QUOTE, now, now,
            )
            c.execute(
                """INSERT INTO dc_tushare_raw_records
                   (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
                    source_code,available_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(api_name,record_key) DO UPDATE SET
                    payload_json=excluded.payload_json,source_code=excluded.source_code,
                    available_time=excluded.available_time,updated_at=excluded.updated_at""",
                values,
            )
            written += 1
    return {"ok": written > 0, "status": "completed" if written else "fallback_empty",
            "source_code": SOURCE_TENCENT_QUOTE, "requested": len(missing),
            "received": len(quotes), "rows_written": written,
            "reference_compared": compared, "reference_mismatches": mismatches}
