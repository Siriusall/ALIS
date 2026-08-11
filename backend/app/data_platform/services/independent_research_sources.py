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
    "ZN.SHF": "ZN0", "PB.SHF": "PB0", "NI.SHF": "NI0", "SN.SHF": "SN0",
    "AO.SHF": "AO0", "SI.GFE": "SI0", "PS.GFE": "PS0", "AU.SHF": "AU0",
    "AG.SHF": "AG0", "I.DCE": "I0", "J.DCE": "J0", "HC.SHF": "HC0",
    "SF.ZCE": "SF0", "SM.ZCE": "SM0", "FU.SHF": "FU0", "LU.INE": "LU0",
    "BU.SHF": "BU0", "PG.DCE": "PG0", "TA.ZCE": "TA0", "EG.DCE": "EG0",
    "MA.ZCE": "MA0", "V.DCE": "V0", "L.DCE": "L0", "EB.DCE": "EB0",
    "SA.ZCE": "SA0", "UR.ZCE": "UR0", "A.DCE": "A0", "M.DCE": "M0",
    "Y.DCE": "Y0", "P.DCE": "P0", "C.DCE": "C0", "SR.ZCE": "SR0",
    "CF.ZCE": "CF0", "RU.SHF": "RU0", "SP.SHF": "SP0", "OI.ZCE": "OI0",
    "RM.ZCE": "RM0",
    "FG.ZCE": "FG0", "LH.DCE": "LH0", "JD.DCE": "JD0", "AP.ZCE": "AP0",
    "CJ.ZCE": "CJ0", "PK.ZCE": "PK0", "SS.SHF": "SS0", "CY.ZCE": "CY0",
    "NR.INE": "NR0", "RR.DCE": "RR0", "PX.ZCE": "PX0", "SH.ZCE": "SH0",
    "RS.ZCE": "RS0", "BR.SHF": "BR0", "EC.SHF": "EC0", "PR.ZCE": "PR0",
    "BZ.ZCE": "BZ0", "AD.SHF": "AD0",
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
    for index, row in enumerate(rows):
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
        previous_price = None
        if index > 0:
            previous = rows[index - 1]
            try:
                previous_price = float(previous.get("s") or previous.get("c"))
            except (TypeError, ValueError):
                previous_price = None
        daily_return = (price / previous_price - 1.0) if previous_price and previous_price > 0 else None
        if daily_return is not None and abs(daily_return) > 0.30:
            continue
        output.append({
            "trade_date": trade_date, "price": price, "close": float(row.get("c") or price),
            "mapped_contract": f"SINA:{symbol}", "provider": "sina_futures_continuous",
            "source_url": f"{SINA_FUTURES_URL}?symbol={symbol}",
            "previous_settle": previous_price, "daily_return": daily_return,
            "continuity_status": "bounded_daily_move" if daily_return is not None else "no_prior_observation",
        })
    return pd.DataFrame(output)
