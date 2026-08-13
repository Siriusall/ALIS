"""Audited Tencent quote fallback for required defensive ETF daily bars.

The fallback is deliberately narrow: it may only fill an exact-date missing
``fund_daily`` record for the registered defensive ETF universe. Same-day
TuShare overlap is used when available but is no longer a prerequisite; every
missing symbol must independently match the latest preceding canonical close.
Existing rows are never updated.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable

import requests

from app.data_platform.db import _conn
from app.services.defensive_assets import DEFENSIVE_ASSETS


SOURCE_TENCENT_ETF_QUOTE = "tencent_etf_quote_fallback"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
HTTP_TIMEOUT = 20
MIN_SAME_DAY_REFERENCE = 2
PRICE_TOLERANCE = 0.0011
VOLUME_RELATIVE_TOLERANCE = 0.002


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_source() -> None:
    now = _now()
    values = (
        SOURCE_TENCENT_ETF_QUOTE,
        "腾讯防御ETF日行情备用源",
        "quote_api",
        85,
        "fallback",
        "仅补冻结 T+1 执行所需且 TuShare 缺失的注册防御ETF日线；同日重叠样本和前收连续性均通过后写入。",
        now,
        now,
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


def _registered_codes(ts_codes: Iterable[str]) -> list[str]:
    allowed = {asset["ts_code"] for asset in DEFENSIVE_ASSETS}
    requested = {str(value or "").strip().upper() for value in ts_codes if str(value or "").strip()}
    return sorted(allowed & requested)


def _quote_symbol(ts_code: str) -> str:
    code, _, exchange = str(ts_code).partition(".")
    return ("sh" if exchange.upper() == "SH" else "sz") + code


def _number(fields: list[str], index: int) -> float:
    return float(str(fields[index] or "").strip())


def _parse_quotes(text: str, trade_date: str) -> dict[str, dict[str, Any]]:
    """Parse only complete quotes stamped with the requested session date."""
    target = str(trade_date or "").replace("-", "")[:8]
    parsed: dict[str, dict[str, Any]] = {}
    for body in re.findall(r'="([^"]*)"', text or ""):
        fields = body.split("~")
        if len(fields) <= 37:
            continue
        code = str(fields[2] or "").strip()
        stamp = str(fields[30] or "").strip()
        if len(code) != 6 or len(target) != 8 or not stamp.startswith(target):
            continue
        exchange = "SH" if fields[0] == "1" else "SZ" if fields[0] in {"0", "51"} else ""
        if not exchange:
            continue
        try:
            close = _number(fields, 3)
            pre_close = _number(fields, 4)
            open_price = _number(fields, 5)
            change = _number(fields, 31)
            pct_chg = _number(fields, 32)
            high = _number(fields, 33)
            low = _number(fields, 34)
            volume = _number(fields, 36)
            amount_parts = str(fields[35] or "").split("/")
            amount = float(amount_parts[2]) / 1000.0
        except (IndexError, TypeError, ValueError):
            continue
        if min(close, pre_close, open_price, high, low, volume, amount) <= 0:
            continue
        ts_code = f"{code}.{exchange}"
        parsed[ts_code] = {
            "ts_code": ts_code,
            "trade_date": target,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "pre_close": pre_close,
            "change": change,
            "pct_chg": pct_chg,
            "vol": volume,
            "amount": amount,
            "name": str(fields[1] or "").strip(),
            "quote_time": stamp,
            "source_url": TENCENT_QUOTE_URL,
            "source_code": SOURCE_TENCENT_ETF_QUOTE,
            "verification_status": "pending_cross_check",
        }
    return parsed


def fetch_tencent_etf_quotes(ts_codes: list[str], trade_date: str) -> dict[str, dict[str, Any]]:
    if not ts_codes:
        return {}
    response = requests.get(
        TENCENT_QUOTE_URL + ",".join(_quote_symbol(code) for code in ts_codes),
        headers={"User-Agent": "ALIS/1.0 market-data fallback", "Referer": "https://gu.qq.com/"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return _parse_quotes(response.content.decode("gbk", "ignore"), trade_date)


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _exact_tushare_references(c: Any, trade_date: str, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = c.execute(
        f"""SELECT ts_code,payload_json FROM dc_tushare_raw_records
            WHERE api_name='fund_daily' AND trade_date=? AND source_code='tushare'
              AND ts_code IN ({placeholders}) ORDER BY ts_code""",
        (trade_date, *codes),
    ).fetchall()
    return {str(dict(row)["ts_code"]): _payload(dict(row).get("payload_json")) for row in rows}


def _missing_codes(c: Any, trade_date: str, codes: list[str]) -> list[str]:
    missing: list[str] = []
    for code in codes:
        row = c.execute(
            """SELECT 1 FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND trade_date=? AND ts_code=? LIMIT 1""",
            (trade_date, code),
        ).fetchone()
        if row is None:
            missing.append(code)
    return missing


def _previous_tushare_closes(c: Any, trade_date: str, codes: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for code in codes:
        row = c.execute(
            """SELECT payload_json FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND ts_code=? AND trade_date<?
               ORDER BY trade_date DESC LIMIT 1""",
            (code, trade_date),
        ).fetchone()
        if row is None:
            continue
        try:
            close = float(_payload(dict(row).get("payload_json")).get("close") or 0)
        except (TypeError, ValueError):
            close = 0
        if close > 0:
            result[code] = close
    return result


def _same_day_mismatches(
    references: dict[str, dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    compared = 0
    mismatches: list[dict[str, Any]] = []
    for code, expected in references.items():
        quote = quotes.get(code)
        if not quote:
            continue
        compared += 1
        bad_fields: list[str] = []
        for field in ("open", "high", "low", "close", "pre_close"):
            try:
                if abs(float(quote[field]) - float(expected[field])) > PRICE_TOLERANCE:
                    bad_fields.append(field)
            except (KeyError, TypeError, ValueError):
                bad_fields.append(field)
        try:
            expected_vol = float(expected["vol"])
            quote_vol = float(quote["vol"])
            if expected_vol <= 0 or abs(quote_vol - expected_vol) / expected_vol > VOLUME_RELATIVE_TOLERANCE:
                bad_fields.append("vol")
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            bad_fields.append("vol")
        if bad_fields:
            mismatches.append({"ts_code": code, "fields": sorted(set(bad_fields))})
    return compared, mismatches


def repair_fund_daily_from_tencent(trade_date: str, ts_codes: Iterable[str]) -> dict[str, Any]:
    """Fill verified exact-session gaps without replacing any primary row."""
    target = str(trade_date or "").replace("-", "")[:8]
    required = _registered_codes(ts_codes)
    reference_universe = sorted(asset["ts_code"] for asset in DEFENSIVE_ASSETS)
    if len(target) != 8 or not target.isdigit() or not required:
        return {"ok": False, "status": "invalid_scope", "rows_written": 0}
    _ensure_source()
    with _conn() as c:
        missing = _missing_codes(c, target, required)
        # A frozen plan may need only one ETF.  The other registered defensive
        # ETFs may still provide the required same-session provider cross-check,
        # while writes remain restricted to the plan's missing symbols.
        references = _exact_tushare_references(c, target, reference_universe)
        previous_closes = _previous_tushare_closes(c, target, missing)
    if not missing:
        return {"ok": True, "status": "nothing_missing", "rows_written": 0, "missing": []}
    quotes = fetch_tencent_etf_quotes(reference_universe, target)
    compared, mismatches = _same_day_mismatches(references, quotes)
    if references and (compared < MIN_SAME_DAY_REFERENCE or mismatches):
        return {
            "ok": False,
            "status": "same_day_cross_check_failed",
            "reference_compared": compared,
            "mismatches": mismatches,
            "rows_written": 0,
            "missing": missing,
        }
    continuity_failures: list[str] = []
    for code in missing:
        quote = quotes.get(code)
        previous_close = previous_closes.get(code)
        if quote is None or previous_close is None or abs(float(quote["pre_close"]) - previous_close) > PRICE_TOLERANCE:
            continuity_failures.append(code)
    if continuity_failures:
        return {
            "ok": False,
            "status": "previous_close_cross_check_failed",
            "reference_compared": compared,
            "continuity_failures": continuity_failures,
            "rows_written": 0,
            "missing": missing,
        }

    # A quote fetched after the close must not be represented as though it had
    # been available at 15:00.  The execution scheduler will re-check on its
    # next tick and may consume it only after this real observation time.
    now = _now()
    available_time = now
    written = 0
    with _conn() as c:
        for code in missing:
            payload = dict(quotes[code])
            payload.update({
                "verification_status": (
                    "same_day_and_previous_close_cross_checked"
                    if compared >= MIN_SAME_DAY_REFERENCE
                    else "provider_contract_and_previous_close_cross_checked"
                ),
                "same_day_reference_count": compared,
                "previous_tushare_close": previous_closes[code],
                "field_contract_version": "alis_tencent_defensive_etf_daily_fallback_v1",
            })
            cursor = c.execute(
                """INSERT INTO dc_tushare_raw_records
                   (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
                    source_code,available_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(api_name,record_key) DO NOTHING""",
                (
                    "fund_daily",
                    f"{code}|{target}",
                    code,
                    target,
                    "",
                    "",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                    SOURCE_TENCENT_ETF_QUOTE,
                    available_time,
                    now,
                ),
            )
            written += max(0, int(cursor.rowcount or 0))
    return {
        "ok": written == len(missing),
        "status": "completed" if written == len(missing) else "partial_or_raced",
        "source_code": SOURCE_TENCENT_ETF_QUOTE,
        "available_time": available_time,
        "requested": len(missing),
        "received": len(quotes),
        "rows_written": written,
        "reference_compared": compared,
        "missing": [],
    }
