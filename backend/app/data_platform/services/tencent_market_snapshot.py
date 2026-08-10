"""Provider-neutral close slice built from Tencent's exact-date quotes.

This adapter is a peer of TuShare, not a fallback that waits for TuShare.  It
fetches the registered A-share universe in bounded parallel batches, validates
the whole slice, and atomically publishes ``daily``, ``daily_basic`` and
``stk_limit`` rows.  Existing rows always win the insert race; a later TuShare
arrival is reconciled by the canonical raw writer.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import requests

from app.data_platform.db import _conn


SOURCE_CODE = "tencent_market_snapshot"
QUOTE_URL = "https://qt.gtimg.cn/q="
BATCH_SIZE = 70
MAX_WORKERS = 8
HTTP_TIMEOUT = 20
MINIMUM_COVERAGE = 0.80
PRICE_TOLERANCE = 0.011


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _number(fields: list[str], index: int, *, scale: float = 1.0) -> float | None:
    try:
        text = str(fields[index] or "").strip()
        if not text or text == "-":
            return None
        return float(text) * scale
    except (IndexError, TypeError, ValueError):
        return None


def _quote_symbol(ts_code: str) -> str:
    code, _, exchange = str(ts_code).partition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper(), "")
    return prefix + code


def _parse_quotes(text: str, trade_date: str) -> dict[str, dict[str, Any]]:
    target = str(trade_date or "").replace("-", "")[:8]
    parsed: dict[str, dict[str, Any]] = {}
    for body in re.findall(r'="([^"]*)"', text or ""):
        fields = body.split("~")
        if len(fields) <= 49:
            continue
        code = str(fields[2] or "").strip()
        stamp = str(fields[30] or "").strip()
        exchange = (
            "SH" if fields[0] == "1"
            else "SZ" if fields[0] in {"0", "51"}
            else "BJ" if fields[0] == "62"
            else ""
        )
        if len(code) != 6 or not exchange or not stamp.startswith(target):
            continue
        close = _number(fields, 3)
        pre_close = _number(fields, 4)
        open_price = _number(fields, 5)
        change = _number(fields, 31)
        pct_chg = _number(fields, 32)
        high = _number(fields, 33)
        low = _number(fields, 34)
        volume = _number(fields, 36)
        # Tencent reports STAR Market volume in shares while TuShare's daily
        # contract is in 100-share lots. Other boards already use lots.
        if volume is not None and code.startswith(("688", "689")):
            volume /= 100.0
        amount_parts = str(fields[35] or "").split("/")
        try:
            amount = float(amount_parts[2]) / 1000.0 if len(amount_parts) >= 3 else None
        except (TypeError, ValueError):
            amount = None
        up_limit = _number(fields, 47)
        down_limit = _number(fields, 48)
        required = (close, pre_close, open_price, change, pct_chg, high, low, volume, amount, up_limit, down_limit)
        if any(value is None for value in required):
            continue
        assert close is not None and pre_close is not None and open_price is not None
        assert high is not None and low is not None and volume is not None and amount is not None
        assert change is not None and pct_chg is not None and up_limit is not None and down_limit is not None
        if min(close, pre_close, open_price, high, low, volume, amount, up_limit, down_limit) <= 0:
            continue
        if high < max(open_price, close) or low > min(open_price, close) or high < low or up_limit <= down_limit:
            continue
        if abs((close - pre_close) - change) > PRICE_TOLERANCE:
            continue
        expected_pct = (close / pre_close - 1.0) * 100.0
        if abs(expected_pct - pct_chg) > 0.021:
            continue
        ts_code = f"{code}.{exchange}"
        parsed[ts_code] = {
            "ts_code": ts_code,
            "trade_date": target,
            "name": str(fields[1] or "").strip(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "pre_close": pre_close,
            "change": change,
            "pct_chg": pct_chg,
            "vol": volume,
            "amount": amount,
            "turnover_rate": _number(fields, 38),
            "pe_ttm": _number(fields, 39),
            "circ_mv": _number(fields, 44, scale=10_000.0),
            "total_mv": _number(fields, 45, scale=10_000.0),
            "pb": _number(fields, 46),
            "volume_ratio": _number(fields, 49),
            "up_limit": up_limit,
            "down_limit": down_limit,
            "quote_time": stamp,
            "source_url": QUOTE_URL,
            "source_code": SOURCE_CODE,
        }
    return parsed


def _fetch_batch(codes: list[str], trade_date: str) -> dict[str, dict[str, Any]]:
    session = requests.Session()
    try:
        response = session.get(
            QUOTE_URL + ",".join(_quote_symbol(code) for code in codes),
            headers={"User-Agent": "ALIS/1.0 market-data provider", "Referer": "https://gu.qq.com/"},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return _parse_quotes(response.content.decode("gbk", "ignore"), trade_date)
    finally:
        session.close()


def fetch_tencent_market_snapshot(ts_codes: list[str], trade_date: str) -> dict[str, dict[str, Any]]:
    batches = [ts_codes[start:start + BATCH_SIZE] for start in range(0, len(ts_codes), BATCH_SIZE)]
    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="tencent-market") as executor:
        futures = [executor.submit(_fetch_batch, batch, trade_date) for batch in batches]
        for future in as_completed(futures):
            result.update(future.result())
    return result


def _ensure_source(conn: Any, now: str) -> None:
    conn.execute(
        """INSERT INTO data_sources
           (code,name,source_type,priority,status,description,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
           name=excluded.name,source_type=excluded.source_type,priority=excluded.priority,
           status=excluded.status,description=excluded.description,updated_at=excluded.updated_at""",
        (
            SOURCE_CODE,
            "腾讯全市场收盘行情",
            "quote_api",
            20,
            "active",
            "与TuShare并发竞速；仅在完整日期、全切片覆盖和前收抽样连续性通过后原子发布。",
            now,
            now,
        ),
    )


def _previous_close_sample(conn: Any, target: str, quotes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    prior_row = conn.execute(
        """SELECT MAX(trade_date) AS trade_date FROM dc_tushare_raw_records
           WHERE api_name='daily' AND trade_date<?""",
        (target,),
    ).fetchone()
    prior_date = str((dict(prior_row) if prior_row else {}).get("trade_date") or "")
    if not prior_date:
        return {"ok": False, "reason": "previous_market_date_missing", "compared": 0}
    codes = sorted(quotes)
    stride = max(1, len(codes) // 120)
    sampled = codes[::stride][:120]
    placeholders = ",".join("?" for _ in sampled)
    rows = conn.execute(
        f"""SELECT ts_code,payload_json FROM dc_tushare_raw_records
            WHERE api_name='daily' AND trade_date=? AND ts_code IN ({placeholders})""",
        (prior_date, *sampled),
    ).fetchall()
    previous: dict[str, float] = {}
    for row in rows:
        item = dict(row)
        try:
            previous[str(item["ts_code"])] = float(json.loads(item.get("payload_json") or "{}").get("close") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    compared = 0
    mismatches: list[str] = []
    for code, close in previous.items():
        if close <= 0:
            continue
        compared += 1
        if abs(float(quotes[code]["pre_close"]) - close) > PRICE_TOLERANCE:
            mismatches.append(code)
    allowed = max(3, int(compared * 0.05))
    return {
        "ok": compared >= 50 and len(mismatches) <= allowed,
        "prior_trade_date": prior_date,
        "compared": compared,
        "mismatch_count": len(mismatches),
        "allowed_mismatches": allowed,
    }


def sync_tencent_market_bundle(trade_date: str, apis: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    target = str(trade_date or "").replace("-", "")[:8]
    selected_apis = tuple(api for api in (apis or ("daily", "daily_basic", "stk_limit"))
                          if api in {"daily", "daily_basic", "stk_limit"})
    if len(target) != 8 or not target.isdigit():
        return {"ok": False, "provider": SOURCE_CODE, "status": "invalid_trade_date"}
    if not selected_apis:
        return {"ok": False, "provider": SOURCE_CODE, "status": "empty_api_scope"}
    with _conn() as conn:
        codes = [
            str(dict(row).get("ts_code") or "")
            for row in conn.execute(
                """SELECT ts_code FROM dc_stock_master
                   WHERE ts_code<>'' AND substr(ts_code,8,2) IN ('SH','SZ','BJ') ORDER BY ts_code"""
            ).fetchall()
        ]
    if not codes:
        return {"ok": False, "provider": SOURCE_CODE, "status": "stock_master_empty"}
    quotes = fetch_tencent_market_snapshot(codes, target)
    minimum = max(500, int(len(codes) * MINIMUM_COVERAGE))
    if len(quotes) < minimum:
        return {
            "ok": False,
            "provider": SOURCE_CODE,
            "status": "incomplete_slice",
            "expected": len(codes),
            "minimum": minimum,
            "received": len(quotes),
        }
    required_fields = {
        "daily": ("open", "high", "low", "close", "pre_close", "vol", "amount"),
        "daily_basic": ("turnover_rate", "volume_ratio", "total_mv", "circ_mv"),
        "stk_limit": ("up_limit", "down_limit"),
    }
    eligible_by_api = {
        api: {
            code for code, quote in quotes.items()
            if all(quote.get(field) is not None for field in required_fields[api])
        }
        for api in selected_apis
    }
    incomplete = {api: len(codes_for_api) for api, codes_for_api in eligible_by_api.items()
                  if len(codes_for_api) < minimum}
    if incomplete:
        return {
            "ok": False,
            "provider": SOURCE_CODE,
            "status": "incomplete_api_fields",
            "expected": len(codes),
            "minimum": minimum,
            "received": len(quotes),
            "eligible_rows": {api: len(value) for api, value in eligible_by_api.items()},
            "incomplete": incomplete,
        }
    observed_at = _now()
    available_time = f"{target[:4]}-{target[4:6]}-{target[6:]}T15:00:00"
    with _conn() as conn:
        continuity = _previous_close_sample(conn, target, quotes)
        if not continuity.get("ok"):
            return {"ok": False, "provider": SOURCE_CODE, "status": "continuity_validation_failed",
                    "received": len(quotes), "continuity": continuity}
        _ensure_source(conn, observed_at)
        inserted = {api: 0 for api in selected_apis}
        for code, quote in quotes.items():
            common = {
                "source_code": SOURCE_CODE,
                "source_url": QUOTE_URL,
                "quote_time": quote["quote_time"],
                "observed_at": observed_at,
                "economic_available_time": available_time,
                "verification_status": "full_slice_and_previous_close_sample_validated",
                "continuity_sample_size": int(continuity["compared"]),
                "pending_primary_reconciliation": True,
                "field_contract_version": "alis_tencent_market_snapshot_v1",
            }
            payloads = {
                "daily": {key: quote.get(key) for key in (
                    "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                    "change", "pct_chg", "vol", "amount"
                )},
                "daily_basic": {key: quote.get(key) for key in (
                    "ts_code", "trade_date", "turnover_rate", "volume_ratio", "pe_ttm", "pb",
                    "total_mv", "circ_mv"
                )},
                "stk_limit": {key: quote.get(key) for key in (
                    "ts_code", "trade_date", "up_limit", "down_limit"
                )},
            }
            for api_name in selected_apis:
                if code not in eligible_by_api[api_name]:
                    continue
                payload = payloads[api_name]
                payload.update(common)
                cursor = conn.execute(
                    """INSERT INTO dc_tushare_raw_records
                       (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
                        source_code,available_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(api_name,record_key) DO NOTHING""",
                    (
                        api_name,
                        f"{code}|{target}",
                        code,
                        target,
                        "",
                        "",
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                        SOURCE_CODE,
                        available_time,
                        observed_at,
                    ),
                )
                inserted[api_name] += max(0, int(cursor.rowcount or 0))
    return {
        "ok": True,
        "provider": SOURCE_CODE,
        "status": "published",
        "trade_date": target,
        "expected": len(codes),
        "received": len(quotes),
        "apis": list(selected_apis),
        "inserted": inserted,
        "eligible_rows": {api: len(value) for api, value in eligible_by_api.items()},
        "continuity": continuity,
        "available_time": available_time,
        "observed_at": observed_at,
    }
