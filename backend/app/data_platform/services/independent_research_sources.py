"""Independent public-web fallbacks for macro and commodity research data."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

import pandas as pd
import requests


EASTMONEY_PPI_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA_FUTURES_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_data=/"
    "InnerFuturesNewService.getDailyKLine"
)

SINA_CONTINUOUS_SYMBOLS = {
    "CU.SHF": "CU0", "AL.SHF": "AL0", "SC.INE": "SC0", "JM.DCE": "JM0",
    "PP.DCE": "PP0", "LC.GFE": "LC0", "RB.SHF": "RB0",
}


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "ALIS/1.0 research fallback"})
    return session


def fetch_eastmoney_ppi_headline(target_month: str, *, timeout_seconds: float = 8.0) -> pd.DataFrame:
    """Fetch headline PPI only; this source does not provide TuShare component fields."""
    with _session() as session:
        response = session.get(
            EASTMONEY_PPI_URL,
            params={
                "reportName": "RPT_ECONOMY_PPI", "columns": "ALL",
                "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                "pageNumber": 1, "pageSize": 24,
            },
            headers={"Referer": "https://data.eastmoney.com/"}, timeout=timeout_seconds,
        )
        response.raise_for_status()
        rows = ((response.json().get("result") or {}).get("data") or [])
    output: list[dict[str, Any]] = []
    for row in rows:
        month = str(row.get("REPORT_DATE") or "")[:7].replace("-", "")
        if month != target_month:
            continue
        output.append({
            "month": month, "ppi_yoy": row.get("BASE_SAME"),
            "ppi_index": row.get("BASE"), "ppi_accumulate_index": row.get("BASE_ACCUMULATE"),
            "source_code": "eastmoney_ppi_headline",
            "source_url": EASTMONEY_PPI_URL,
            "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "field_scope": "headline_only_no_component_claim",
        })
    return pd.DataFrame(output)


def fetch_sina_continuous_futures(
    root_contract: str, start_date: str, end_date: str, *, timeout_seconds: float = 8.0,
) -> pd.DataFrame:
    symbol = SINA_CONTINUOUS_SYMBOLS.get(root_contract)
    if not symbol:
        return pd.DataFrame()
    with _session() as session:
        response = session.get(
            SINA_FUTURES_URL, params={"symbol": symbol},
            headers={"Referer": "https://finance.sina.com.cn/futures/"}, timeout=timeout_seconds,
        )
        response.raise_for_status()
    match = re.search(r"\((\[.*\])\)\s*;?\s*$", response.text, flags=re.DOTALL)
    rows = json.loads(match.group(1)) if match else []
    output = []
    for row in rows:
        trade_date = str(row.get("d") or "").replace("-", "")
        if not (start_date <= trade_date <= end_date):
            continue
        raw_price = row.get("s") or row.get("c")
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        output.append({
            "trade_date": trade_date, "price": price, "close": float(row.get("c") or price),
            "mapped_contract": f"SINA:{symbol}", "provider": "sina_futures_continuous",
            "source_url": f"{SINA_FUTURES_URL}?symbol={symbol}",
        })
    return pd.DataFrame(output)
