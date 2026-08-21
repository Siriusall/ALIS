"""Audited historical Eastmoney fallback for retained ETF OHLC gaps."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import requests

from app.data_platform.db import _conn
from app.data_platform.services.observation_clock import upsert_raw_record

HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SOURCE_CODE = "eastmoney_etf_history_fallback"
PRICE_TOLERANCE = 0.012


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _secid(ts_code: str) -> str:
    code, _, exchange = str(ts_code).upper().partition(".")
    return f"{1 if exchange == 'SH' else 0}.{code}"


def fetch_eastmoney_etf_history(ts_code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(HISTORY_URL, params={
            "secid": _secid(ts_code), "klt": "101", "fqt": "0",
            "beg": start_date, "end": end_date,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }, headers={"User-Agent": "ALIS/1.0 market-data fallback"}, timeout=20)
        response.raise_for_status()
        body = response.json()
    finally:
        session.close()
    return parse_eastmoney_etf_history(ts_code, body)


def parse_eastmoney_etf_history(ts_code: str, body: Any) -> list[dict[str, Any]]:
    """Validate a retained raw HTTP response with the same live-source contract."""
    data = body.get("data") if isinstance(body, dict) else None
    rows: list[dict[str, Any]] = []
    for value in (data or {}).get("klines") or []:
        fields = str(value).split(",")
        if len(fields) < 11:
            continue
        try:
            trade_date = fields[0].replace("-", "")
            open_price, close, high, low = map(float, fields[1:5])
            volume, amount = float(fields[5]), float(fields[6])
            pct_chg, change = float(fields[8]), float(fields[9])
            pre_close = close - change
        except (TypeError, ValueError):
            continue
        if min(open_price, close, high, low, pre_close) <= 0 or high < max(open_price, close) or low > min(open_price, close):
            continue
        if abs((close / pre_close - 1) * 100 - pct_chg) > PRICE_TOLERANCE:
            continue
        rows.append({"ts_code": ts_code, "trade_date": trade_date, "open": open_price,
                     "high": high, "low": low, "close": close, "pre_close": pre_close,
                     "change": change, "pct_chg": pct_chg, "vol": volume,
                     "amount": amount / 1000.0, "name": str((data or {}).get("name") or ""),
                     "source_code": SOURCE_CODE, "source_url": HISTORY_URL})
    return rows


def repair_fund_daily_history_from_eastmoney(
    start_date: str, end_date: str, ts_codes: Iterable[str], *, required_dates: Iterable[str] = (),
) -> dict[str, Any]:
    required = {str(value).replace("-", "")[:8] for value in required_dates}
    now = _now()
    with _conn() as c:
        c.execute("""INSERT INTO data_sources(code,name,source_type,priority,status,description,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET updated_at=excluded.updated_at""",
                  (SOURCE_CODE, "东方财富 ETF 历史日线备用源", "historical_quote_api", 80, "fallback",
                   "仅补结构化主源缺失的精确历史 ETF OHLC；不覆盖已有记录。", now, now))
    written = 0
    found: set[tuple[str, str]] = set()
    for code in sorted(set(ts_codes)):
        for payload in fetch_eastmoney_etf_history(code, start_date, end_date):
            date = str(payload["trade_date"])
            if required and date not in required:
                continue
            with _conn() as c:
                exists = c.execute("SELECT 1 FROM dc_tushare_raw_records WHERE api_name=? AND ts_code=? AND trade_date=?",
                                   ("fund_daily", code, date)).fetchone()
                if exists:
                    found.add((code, date))
                    continue
                written += upsert_raw_record(
                    c, api_name="fund_daily", record_key=f"{code}|{date}", ts_code=code,
                    trade_date=date, payload=payload, source_code=SOURCE_CODE, observed_at=now,
                    economic_time=f"{date[:4]}-{date[4:6]}-{date[6:]}T15:00:00+08:00",
                    provenance="eastmoney_etf_history_fallback:exact_unadjusted_daily",
                )
                found.add((code, date))
    expected = {(code, date) for code in set(ts_codes) for date in required}
    missing = sorted(f"{code}:{date}" for code, date in expected - found)
    return {"ok": not missing, "rows_written": written, "missing": missing, "source_code": SOURCE_CODE}
