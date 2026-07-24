"""Free, auditable research data materialisation.

The module deliberately distinguishes exchange-traded futures proxies from
direct industry spot feeds.  Historical futures fetched today become usable
today, rather than being backdated into historical OOS windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from typing import Any

import pandas as pd

from app.data_platform.db import _conn
from app.data_platform.tushare_client import get_tushare_client
from app.data_platform.services.data_center import _now, _upsert_industry_metric_definitions, _upsert_industry_metric_values
from app.services.industry_event_engine import _derived_industry_proxies


FINANCIAL_SOURCE = "tushare.financial_pit"
FUTURES_PROXY_SOURCE = "tushare.futures_proxy"
PPI_PROXY_SOURCE = "tushare.cn_ppi_proxy"


@dataclass(frozen=True)
class FuturesProxySpec:
    key: str
    root_contract: str
    name: str
    industries: tuple[str, ...]


FUTURES_PROXY_SPECS = (
    FuturesProxySpec("copper", "CU.SHF", "沪铜连续期货价格代理", ("铜", "元器件", "家用电器")),
    FuturesProxySpec("aluminium", "AL.SHF", "沪铝连续期货价格代理", ("铝", "家用电器")),
    FuturesProxySpec("crude_oil", "SC.INE", "原油连续期货价格代理", ("石油开采",)),
    FuturesProxySpec("coking_coal", "JM.DCE", "焦煤连续期货价格代理", ("煤炭开采",)),
    FuturesProxySpec("polypropylene", "PP.DCE", "聚丙烯连续期货价格代理", ("化工原料",)),
    FuturesProxySpec("lithium_carbonate", "LC.GFE", "碳酸锂连续期货价格代理", ("电气设备",)),
    FuturesProxySpec("rebar", "RB.SHF", "螺纹钢连续期货价格代理", ("工程机械", "汽车整车")),
)

PPI_PROXY_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("mining", "ppi_mp_qm_yoy", ("煤炭开采", "石油开采")),
    ("raw_material", "ppi_mp_rm_yoy", ("化工原料", "普钢", "有色")),
    ("processing", "ppi_mp_p_yoy", ("工程机械", "汽车整车", "电气设备", "元器件")),
    ("durable_goods", "ppi_cg_dcg_yoy", ("家用电器", "汽车整车")),
)


def _metric_code(prefix: str, component: str, industry: str) -> str:
    digest = hashlib.sha256(industry.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{component}:{digest}"


def _cutoff_dates(max_periods: int) -> list[str]:
    """Return one latest source-availability date for every observed quarter."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT MAX(SUBSTR(available_time,1,10)) AS cutoff
                 FROM dc_tushare_raw_records
                WHERE api_name IN ('income','balancesheet')
                  AND COALESCE(available_time,'')<>''
                GROUP BY SUBSTR(available_time,1,4),
                         ((CAST(SUBSTR(available_time,6,2) AS INTEGER)-1)/3)"""
        ).fetchall()
    values = sorted({str(dict(row).get("cutoff") or "") for row in rows if dict(row).get("cutoff")})
    return values[-max(1, int(max_periods)):]


def materialize_financial_industry_series(*, max_periods: int = 12) -> dict[str, Any]:
    """Materialise point-in-time industry operating and valuation proxies.

    Values are derived only from statements and daily snapshots whose recorded
    available time precedes the quarter cutoff.  They are formal derived
    metrics, not a substitute for vendor-supplied product or inventory feeds.
    """
    definitions: dict[str, dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    cutoffs = _cutoff_dates(max_periods)
    for cutoff in cutoffs:
        with _conn() as conn:
            derived = _derived_industry_proxies(conn, as_of_time=f"{cutoff}T23:59:59")
        for industry, item in derived.items():
            for component in (
                "demand_orders_sales",
                "inventory_cycle",
                "profit_diffusion",
                "industry_valuation",
            ):
                value = (item.get("observations") or {}).get(component)
                if value is None:
                    continue
                code = _metric_code("financial_pit", component, industry)
                definitions[code] = {
                    "metric_code": code,
                    "industry_code": f"master:{industry}",
                    "industry_name": industry,
                    "metric_name": {
                        "demand_orders_sales": "行业需求/销量（点时营收扩散代理）",
                        "inventory_cycle": "行业库存周期（点时库存营收比代理）",
                        "profit_diffusion": "行业盈利扩散（点时财务聚合）",
                        "industry_valuation": "行业估值（点时财务聚合）",
                    }[component],
                    "unit": "score",
                    "frequency": "quarterly",
                    "source_code": FINANCIAL_SOURCE,
                    "description": {
                        "demand_orders_sales": "由点时营收同比扩散聚合；是需求/销量代理，不等同于实际订单或行业销量。",
                        "inventory_cycle": "由点时库存/营收同比改善聚合；是库存周期代理，不等同于专业库存口径。",
                        "profit_diffusion": "由点时财报聚合；不使用未来披露。",
                        "industry_valuation": "由点时财报与估值快照聚合；不使用未来披露。",
                    }[component],
                }
                values.append({
                    "metric_code": code,
                    "data_date": cutoff,
                    "value": float(value),
                    "raw_value": json.dumps(item.get("evidence") or {}, ensure_ascii=False),
                    "source_code": FINANCIAL_SOURCE,
                    "available_time": f"{cutoff}T23:59:59",
                })
    return {
        "ok": True,
        "cutoffs": len(cutoffs),
        "definitions": _upsert_industry_metric_definitions(list(definitions.values())),
        "values": _upsert_industry_metric_values(values),
        "contract": "financial_industry_pit_v1",
    }


def _frame_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    return [{str(key): value for key, value in row.items()} for row in frame.to_dict(orient="records")]


def materialize_futures_price_proxies(*, start_date: str, end_date: str, specs: tuple[FuturesProxySpec, ...] = FUTURES_PROXY_SPECS) -> dict[str, Any]:
    """Build exchange-traded commodity price proxies for named industries.

    The mapping endpoint chooses the exchange's mapped contract for each date.
    Historical values are stamped with the materialisation time, so an initial
    backfill can never leak into an earlier point-in-time replay.
    """
    now = _now()
    client = get_tushare_client().pro
    definitions: dict[str, dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    result: dict[str, Any] = {"ok": True, "sources": [], "definitions": 0, "values": 0, "contract": "exchange_futures_proxy_v1"}
    for spec in specs:
        mapping = _frame_rows(client.fut_mapping(ts_code=spec.root_contract))
        mapped_by_date = {
            str(row.get("trade_date") or ""): str(row.get("mapping_ts_code") or "")
            for row in mapping
            if start_date <= str(row.get("trade_date") or "") <= end_date and row.get("mapping_ts_code")
        }
        contracts = sorted(set(mapped_by_date.values()))
        closes: dict[tuple[str, str], float] = {}
        for contract in contracts:
            frame = client.fut_daily(ts_code=contract, start_date=start_date, end_date=end_date, fields="ts_code,trade_date,close,settle")
            for row in _frame_rows(frame):
                raw = row.get("settle") if row.get("settle") is not None else row.get("close")
                try:
                    closes[(str(row.get("trade_date") or ""), contract)] = float(raw)
                except (TypeError, ValueError):
                    continue
        written = 0
        for industry in spec.industries:
            code = _metric_code("futures_proxy", spec.key, industry)
            definitions[code] = {
                "metric_code": code,
                "industry_code": f"master:{industry}",
                "industry_name": industry,
                "metric_name": f"产品价格：{spec.name}",
                "unit": "price",
                "frequency": "daily",
                "source_code": FUTURES_PROXY_SOURCE,
                "description": "交易所连续期货价格代理，不等同于行业现货价格或企业实际实现价。",
            }
            for trade_date, contract in mapped_by_date.items():
                close = closes.get((trade_date, contract))
                if close is None:
                    continue
                values.append({
                    "metric_code": code,
                    "data_date": trade_date,
                    "value": close,
                    "raw_value": json.dumps({"root_contract": spec.root_contract, "mapped_contract": contract, "field": "settle_or_close"}, ensure_ascii=False),
                    "source_code": FUTURES_PROXY_SOURCE,
                    "available_time": now,
                })
                written += 1
        result["sources"].append({"key": spec.key, "root_contract": spec.root_contract, "contracts": len(contracts), "values": written})
    result["definitions"] = _upsert_industry_metric_definitions(list(definitions.values()))
    result["values"] = _upsert_industry_metric_values(values)
    return result


def materialize_cn_ppi_price_proxies(*, specs: tuple[tuple[str, str, tuple[str, ...]], ...] = PPI_PROXY_SPECS) -> dict[str, Any]:
    """Materialise retained TuShare PPI as industry product-price proxies.

    The PPI API is a public macro source, not an industry spot-price vendor.
    Historical records keep their ingestion `available_time`, so a fresh
    backfill remains unavailable to a prior point-in-time replay.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payload_json,available_time FROM dc_tushare_raw_records WHERE api_name=? ORDER BY id",
            ("cn_ppi",),
        ).fetchall()
    observations: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = dict(row)
        try:
            payload = json.loads(str(raw.get("payload_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        month = str(payload.get("month") or "")
        if len(month) == 6 and month.isdigit():
            observations[month] = {"payload": payload, "available_time": str(raw.get("available_time") or "")}
    definitions: dict[str, dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    for key, field, industries in specs:
        for industry in industries:
            code = _metric_code("cn_ppi_proxy", key, industry)
            definitions[code] = {
                "metric_code": code, "industry_code": f"master:{industry}", "industry_name": industry,
                "metric_name": f"产品价格：工业 PPI {key} 同比代理", "unit": "yoy_percent", "frequency": "monthly",
                "source_code": PPI_PROXY_SOURCE,
                "description": "TuShare 中国工业生产者价格分项同比代理，不等同于行业现货价、企业实际售价或价差。",
            }
            for month, item in observations.items():
                try:
                    value = float(item["payload"].get(field))
                except (TypeError, ValueError):
                    continue
                values.append({
                    "metric_code": code, "data_date": f"{month[:4]}-{month[4:]}-01", "value": value,
                    "raw_value": json.dumps({"field": field, "value": item["payload"].get(field)}, ensure_ascii=False),
                    "source_code": PPI_PROXY_SOURCE, "available_time": item["available_time"],
                })
    return {"ok": True, "definitions": _upsert_industry_metric_definitions(list(definitions.values())),
            "values": _upsert_industry_metric_values(values), "months": len(observations),
            "contract": "tushare_cn_ppi_industry_price_proxy_v1",
            "limitations": ["工业 PPI 仅为行业产品价格宏观代理；不替代现货、企业实现价格或专业价差。"]}
