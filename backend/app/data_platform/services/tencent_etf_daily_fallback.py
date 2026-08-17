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
from app.data_platform.tushare_client import tushare_compatible_source_codes
from app.services.defensive_assets import (
    DEFENSIVE_ASSETS,
    defensive_asset_execution_missing_fields,
)
from app.data_platform.services.observation_clock import upsert_raw_record


SOURCE_TENCENT_ETF_QUOTE = "tencent_etf_quote_fallback"
SOURCE_SINA_ETF_HISTORY = "sina_etf_history_fallback"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
HTTP_TIMEOUT = 20
MIN_SAME_DAY_REFERENCE = 2
PRICE_TOLERANCE = 0.0011
VOLUME_RELATIVE_TOLERANCE = 0.002
AMOUNT_RELATIVE_TOLERANCE = 0.002


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_source() -> None:
    now = _now()
    with _conn() as c:
        for values in (
            (
                SOURCE_TENCENT_ETF_QUOTE,
                "腾讯防御ETF日行情备用源",
                "quote_api",
                85,
                "fallback",
                "仅补冻结 T+1 执行所需且 TuShare 缺失的注册防御ETF日线；同日重叠样本和前收连续性均通过后写入。",
                now,
                now,
            ),
            (
                SOURCE_SINA_ETF_HISTORY,
                "新浪ETF历史前收证据源",
                "history_api",
                86,
                "fallback",
                "仅为当日备用行情提供前一交易日收盘连续性证据，不直接写入执行行情。",
                now,
                now,
            ),
        ):
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


def fetch_sina_etf_previous_closes(
    ts_codes: Iterable[str], trade_date: str,
) -> dict[str, dict[str, Any]]:
    """Fetch prior-session closes used only for continuity cross-checks."""
    target = str(trade_date or "").replace("-", "")[:8]
    if len(target) != 8 or not target.isdigit():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for value in sorted({str(code or "").strip().upper() for code in ts_codes if str(code or "").strip()}):
        code, _, exchange = value.partition(".")
        if len(code) != 6 or exchange.upper() not in {"SH", "SZ"}:
            continue
        response = requests.get(
            SINA_KLINE_URL,
            params={"symbol": f"{'sh' if exchange.upper() == 'SH' else 'sz'}{code}", "scale": 240, "ma": "no", "datalen": 1024},
            headers={"User-Agent": "ALIS/1.0 market-data fallback", "Referer": "https://finance.sina.com.cn/"},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError:
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if str((row or {}).get("day") or "").replace("-", "")[:8] != target:
                continue
            try:
                close = float((row or {}).get("close") or 0)
            except (TypeError, ValueError):
                close = 0.0
            if close > 0:
                result[value] = {
                    "close": close,
                    "source_code": SOURCE_SINA_ETF_HISTORY,
                    "source_url": SINA_KLINE_URL,
                    "trade_date": target,
                }
            break
    return result


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _has_execution_fields(payload: dict[str, Any]) -> bool:
    """Keep row presence separate from an execution-capable exact bar."""
    return not defensive_asset_execution_missing_fields(payload)


def _exact_tushare_references(c: Any, trade_date: str, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    source_codes = tushare_compatible_source_codes()
    source_placeholders = ",".join("?" for _ in source_codes)
    rows = c.execute(
        f"""SELECT ts_code,payload_json FROM dc_tushare_raw_records
            WHERE api_name='fund_daily' AND trade_date=?
              AND source_code IN ({source_placeholders})
              AND ts_code IN ({placeholders}) ORDER BY ts_code""",
        (trade_date, *source_codes, *codes),
    ).fetchall()
    return {str(dict(row)["ts_code"]): _payload(dict(row).get("payload_json")) for row in rows}


def _missing_codes(c: Any, trade_date: str, codes: list[str]) -> list[str]:
    """Return only absent records; malformed primary rows need primary repair.

    The fallback must never overwrite a primary record. Returning malformed
    symbols separately keeps the failure explicit instead of falsely claiming
    that nothing is missing while the execution gate remains blocked.
    """
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


def _malformed_codes(c: Any, trade_date: str, codes: list[str]) -> list[dict[str, Any]]:
    """Expose retained-but-unusable primary rows without overwriting them."""
    malformed: list[dict[str, Any]] = []
    for code in codes:
        rows = c.execute(
            """SELECT payload_json FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND trade_date=? AND ts_code=?""",
            (trade_date, code),
        ).fetchall()
        payloads = [_payload(dict(row).get("payload_json")) for row in rows]
        if payloads and not any(_has_execution_fields(payload) for payload in payloads):
            missing_fields = sorted({
                field for payload in payloads
                for field in defensive_asset_execution_missing_fields(payload)
            })
            malformed.append({
                "ts_code": code,
                "missing_fields": missing_fields,
                "record_count": len(payloads),
            })
    return malformed


def _previous_canonical_closes(c: Any, trade_date: str, codes: list[str]) -> dict[str, float]:
    """Return the latest validated close regardless of the winning provider.

    A prior session may itself have been repaired by the audited Tencent
    fallback.  Restricting continuity to ``source_code='tushare'`` made that
    valid row invisible and blocked the next session indefinitely.
    """
    result: dict[str, float] = {}
    allowed_verifications = {
        "same_day_and_previous_close_cross_checked",
        "provider_contract_and_previous_close_cross_checked",
    }
    for code in codes:
        rows = c.execute(
            """SELECT payload_json,source_code FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND ts_code=? AND trade_date<?
               ORDER BY trade_date DESC""",
            (code, trade_date),
        ).fetchall()
        for row in rows:
            payload = _payload(dict(row).get("payload_json"))
            source_code = str(dict(row).get("source_code") or "").strip()
            verification = str(payload.get("verification_status") or "").strip()
            if (
                source_code not in tushare_compatible_source_codes()
                and verification not in allowed_verifications
            ):
                continue
            try:
                close = float(payload.get("close") or 0)
            except (TypeError, ValueError):
                close = 0
            if close > 0:
                result[code] = close
                break
    return result


# Backward-compatible private alias for callers and fixtures that still use
# the old helper name; the contract now accepts any validated provider.
_previous_tushare_closes = _previous_canonical_closes


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
        try:
            expected_amount = float(expected["amount"])
            quote_amount = float(quote["amount"])
            if (
                expected_amount <= 0
                or abs(quote_amount - expected_amount) / expected_amount > AMOUNT_RELATIVE_TOLERANCE
            ):
                bad_fields.append("amount")
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            bad_fields.append("amount")
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
        malformed = _malformed_codes(c, target, required)
        # A frozen plan may need only one ETF.  The other registered defensive
        # ETFs may still provide the required same-session provider cross-check,
        # while writes remain restricted to the plan's missing symbols.
        references = _exact_tushare_references(c, target, reference_universe)
        previous_closes = _previous_canonical_closes(c, target, missing)
    previous_close_evidence = {
        code: {"close": close, "source_code": "canonical_raw_record"}
        for code, close in previous_closes.items()
    }
    missing_previous = [code for code in missing if code not in previous_closes]
    if missing_previous:
        try:
            previous_close_evidence.update(fetch_sina_etf_previous_closes(missing_previous, target))
        except Exception:
            # A missing secondary continuity source remains a hard gap.
            pass
        previous_closes.update({
            code: float(item["close"])
            for code, item in previous_close_evidence.items()
            if code in missing_previous and float(item.get("close") or 0) > 0
        })
    if malformed:
        return {
            "ok": False,
            "status": "primary_execution_fields_invalid",
            "rows_written": 0,
            "missing": missing,
            "malformed": malformed,
            "retryable": True,
            "source_action": "retry_tushare_primary_without_overwriting_retained_evidence",
        }
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
                "previous_close_source": str(
                    (previous_close_evidence.get(code) or {}).get("source_code") or "canonical_raw_record"
                ),
                "field_contract_version": "alis_tencent_defensive_etf_daily_fallback_v1",
            })
            written += upsert_raw_record(
                c, api_name="fund_daily", record_key=f"{code}|{target}", ts_code=code,
                trade_date=target, payload=payload, source_code=SOURCE_TENCENT_ETF_QUOTE,
                observed_at=now, economic_time=f"{target[:4]}-{target[4:6]}-{target[6:]}T15:00:00+08:00",
                provenance="tencent_etf_quote_fallback:validated_exact_session",
            )
    return {
        "ok": written == len(missing),
        "status": "completed" if written == len(missing) else "partial_or_raced",
        "source_code": SOURCE_TENCENT_ETF_QUOTE,
        "available_time": available_time,
        "requested": len(missing),
        "received": len(quotes),
        "rows_written": written,
        "reference_compared": compared,
        "missing": [code for code in missing if code not in quotes or not quotes.get(code)],
    }
