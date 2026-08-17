"""Audited Eastmoney fallback for a missing HS300 daily benchmark bar.

The fallback is intentionally limited to one exact completed session of
``000300.SH``.  It never overwrites an existing row and requires the provider's
previous close to agree with the latest preceding TuShare close.  Economic
availability (the completed 15:00 bar) and the later ALIS observation time are
both retained so replay and operations audits do not conflate them.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import requests

from app.data_platform.db import _conn
from app.data_platform.services.observation_clock import upsert_raw_record


BENCHMARK_CODE = "000300.SH"
SOURCE_EASTMONEY_INDEX = "eastmoney_index_daily_fallback"
SOURCE_SINA_INDEX = "sina_index_daily_fallback"
HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SINA_HISTORY_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
HTTP_TIMEOUT = 20
PRICE_TOLERANCE = 0.011


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _http_get(url: str, **kwargs: Any) -> requests.Response:
    # Market-data hosts are directly reachable on the production machine. A
    # stale desktop HTTP(S)_PROXY must not strand the deterministic fallback.
    session = requests.Session()
    session.trust_env = False
    try:
        return session.get(url, **kwargs)
    finally:
        session.close()


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _ensure_source(source_code: str) -> None:
    now = _now()
    is_eastmoney = source_code == SOURCE_EASTMONEY_INDEX
    with _conn() as conn:
        conn.execute(
            """INSERT INTO data_sources
               (code,name,source_type,priority,status,description,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
               name=excluded.name,source_type=excluded.source_type,priority=excluded.priority,
               status=excluded.status,description=excluded.description,updated_at=excluded.updated_at""",
            (
                source_code,
                "东方财富沪深300日线备用源" if is_eastmoney else "新浪沪深300日线备用源",
                "historical_quote_api",
                85,
                "fallback",
                "仅补 TuShare 尚缺的沪深300完整日线；校验日期、OHLC及前收连续性后写入，主源优先。",
                now,
                now,
            ),
        )


def fetch_eastmoney_hs300_bar(trade_date: str) -> dict[str, Any] | None:
    target = str(trade_date or "").replace("-", "")[:8]
    response = _http_get(
        HISTORY_URL,
        params={
            "secid": "1.000300",
            "klt": "101",
            "fqt": "0",
            "beg": target,
            "end": target,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        headers={"User-Agent": "ALIS/1.0 market-data fallback", "Referer": "https://quote.eastmoney.com/"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    klines = data.get("klines") if isinstance(data, dict) else None
    if body.get("rc") != 0 or not isinstance(klines, list) or len(klines) != 1:
        return None
    fields = str(klines[0] or "").split(",")
    if len(fields) < 11 or fields[0].replace("-", "") != target:
        return None
    try:
        open_price, close, high, low = map(float, fields[1:5])
        volume, amount = float(fields[5]), float(fields[6])
        pct_chg, change = float(fields[8]), float(fields[9])
        pre_close = float(data.get("preKPrice") or 0)
    except (TypeError, ValueError):
        return None
    if min(open_price, close, high, low, pre_close) <= 0 or volume < 0 or amount < 0:
        return None
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        return None
    if abs((close - pre_close) - change) > PRICE_TOLERANCE:
        return None
    expected_pct = (close / pre_close - 1.0) * 100.0
    if abs(expected_pct - pct_chg) > 0.011:
        return None
    return {
        "ts_code": BENCHMARK_CODE,
        "trade_date": target,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": pre_close,
        "change": change,
        "pct_chg": pct_chg,
        "vol": volume,
        # Eastmoney reports yuan; TuShare index_daily uses thousand yuan.
        "amount": amount / 1000.0,
        "name": str(data.get("name") or "沪深300"),
        "source_url": HISTORY_URL,
        "source_code": SOURCE_EASTMONEY_INDEX,
    }


def fetch_sina_hs300_bar(trade_date: str) -> dict[str, Any] | None:
    """Read the same completed bar from an independent historical endpoint."""
    target = str(trade_date or "").replace("-", "")[:8]
    response = _http_get(
        SINA_HISTORY_URL,
        params={"symbol": "sh000300", "scale": "240", "ma": "no", "datalen": "40"},
        headers={"User-Agent": "ALIS/1.0 market-data fallback", "Referer": "https://finance.sina.com.cn/"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    match = re.search(r"(\[.*\])", response.text or "", flags=re.S)
    if not match:
        return None
    try:
        rows = json.loads(match.group(1))
    except (TypeError, json.JSONDecodeError):
        return None
    item = next(
        (row for row in rows if str(row.get("day") or "").replace("-", "") == target),
        None,
    )
    if not isinstance(item, dict):
        return None
    try:
        open_price = float(item.get("open") or 0)
        high = float(item.get("high") or 0)
        low = float(item.get("low") or 0)
        close = float(item.get("close") or 0)
        volume = float(item.get("volume") or 0) / 100.0
    except (TypeError, ValueError):
        return None
    if min(open_price, high, low, close, volume) <= 0:
        return None
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        return None
    return {
        "ts_code": BENCHMARK_CODE,
        "trade_date": target,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "vol": volume,
        "amount": None,
        "name": "沪深300",
        "source_url": SINA_HISTORY_URL,
        "source_code": SOURCE_SINA_INDEX,
    }


def repair_hs300_index_daily_from_eastmoney(
    trade_date: str,
    *,
    provider: str = "",
) -> dict[str, Any]:
    """Fill one verified exact-session gap without replacing primary data."""
    target = str(trade_date or "").replace("-", "")[:8]
    if len(target) != 8 or not target.isdigit():
        return {"ok": False, "status": "invalid_scope", "rows_written": 0}
    with _conn() as conn:
        existing = conn.execute(
            """SELECT source_code FROM dc_tushare_raw_records
               WHERE api_name='index_daily' AND ts_code=? AND trade_date=? LIMIT 1""",
            (BENCHMARK_CODE, target),
        ).fetchone()
        prior = conn.execute(
            """SELECT trade_date,payload_json FROM dc_tushare_raw_records
               WHERE api_name='index_daily' AND ts_code=? AND trade_date<?
               ORDER BY trade_date DESC LIMIT 1""",
            (BENCHMARK_CODE, target),
        ).fetchone()
    if existing:
        return {"ok": True, "status": "nothing_missing", "rows_written": 0,
                "source_code": dict(existing).get("source_code")}
    if prior is None:
        return {"ok": False, "status": "previous_tushare_close_missing", "rows_written": 0}
    prior_item = dict(prior)
    try:
        prior_close = float(_payload(prior_item.get("payload_json")).get("close") or 0)
    except (TypeError, ValueError):
        prior_close = 0
    provider_errors: list[str] = []
    bar: dict[str, Any] | None = None
    providers = {
        "eastmoney": fetch_eastmoney_hs300_bar,
        "sina": fetch_sina_hs300_bar,
    }
    provider_alias = {
        SOURCE_EASTMONEY_INDEX: "eastmoney",
        SOURCE_SINA_INDEX: "sina",
        "eastmoney": "eastmoney",
        "sina": "sina",
    }
    if provider:
        selected = provider_alias.get(provider)
        if not selected:
            return {"ok": False, "status": "unknown_provider", "rows_written": 0}
        providers = {selected: providers[selected]}
    executor = ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="hs300-provider")
    futures = {executor.submit(fetcher, target): name for name, fetcher in providers.items()}
    try:
        for future in as_completed(futures):
            name = futures[future]
            try:
                candidate = future.result()
            except requests.RequestException as exc:
                provider_errors.append(f"{name}:{type(exc).__name__}")
                continue
            if candidate is not None:
                bar = candidate
                for pending in futures:
                    if pending is not future:
                        pending.cancel()
                break
    finally:
        executor.shutdown(wait=bar is None)
    if bar is None:
        return {"ok": False, "status": "fallback_not_published", "rows_written": 0,
                "provider_errors": provider_errors}
    provider_pre_close = float(bar.get("pre_close") or prior_close)
    if prior_close <= 0 or abs(provider_pre_close - prior_close) > PRICE_TOLERANCE:
        return {
            "ok": False,
            "status": "previous_close_cross_check_failed",
            "rows_written": 0,
            "prior_trade_date": str(prior_item.get("trade_date") or ""),
        }

    source_code = str(bar.get("source_code") or "")
    _ensure_source(source_code)
    observed_at = _now()
    economic_available_time = f"{target[:4]}-{target[4:6]}-{target[6:]}T15:00:00+08:00"
    payload = dict(bar)
    payload.update({
        "verification_status": "previous_tushare_close_and_provider_fields_cross_checked",
        "previous_tushare_trade_date": str(prior_item.get("trade_date") or ""),
        "previous_tushare_close": prior_close,
        "economic_available_time": economic_available_time,
        "observed_at": observed_at,
        "field_contract_version": "alis_hs300_index_daily_fallback_v1",
        "pending_primary_reconciliation": True,
    })
    with _conn() as conn:
        written = upsert_raw_record(
            conn, api_name="index_daily", record_key=f"{BENCHMARK_CODE}|{target}",
            ts_code=BENCHMARK_CODE, trade_date=target, payload=payload, source_code=source_code,
            observed_at=observed_at, economic_time=economic_available_time,
            provenance=f"{source_code}:validated_exact_session",
        )
    return {
        "ok": written == 1,
        "status": "completed" if written == 1 else "partial_or_raced",
        "rows_written": written,
        "trade_date": target,
        "source_code": source_code,
        "available_time": observed_at,
        "observed_at": observed_at,
    }
