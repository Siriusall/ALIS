"""Free, auditable research data materialisation.

The module deliberately distinguishes exchange-traded futures proxies from
direct industry spot feeds.  Historical futures fetched today become usable
today, rather than being backdated into historical OOS windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from typing import Any

import pandas as pd

from app.data_platform.db import _conn
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


def _ensure_futures_proxy_cache() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_futures_mapping_cache (
            root_contract TEXT NOT NULL,trade_date TEXT NOT NULL,mapped_contract TEXT NOT NULL,
            provider TEXT NOT NULL,available_time TEXT NOT NULL,updated_at TEXT NOT NULL,
            PRIMARY KEY(root_contract,trade_date)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_futures_price_cache (
            mapped_contract TEXT NOT NULL,trade_date TEXT NOT NULL,price REAL NOT NULL,
            provider TEXT NOT NULL,available_time TEXT NOT NULL,updated_at TEXT NOT NULL,
            PRIMARY KEY(mapped_contract,trade_date)
        )""")


def _cached_mappings(root_contract: str, start_date: str, end_date: str) -> dict[str, dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT trade_date,mapped_contract,provider,available_time
                 FROM dc_futures_mapping_cache
                WHERE root_contract=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date""",
            (root_contract, start_date, end_date),
        ).fetchall()
    return {str(dict(row)["trade_date"]): dict(row) for row in rows}


def _discover_futures_mapping(root_contract: str, start_date: str, end_date: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    from app.data_platform.services.research_source_discovery import race_tushare_sources

    cached = _cached_mappings(root_contract, start_date, end_date)
    if cached and start_date == end_date and start_date in cached:
        return cached, {"status": "cache_hit", "winner": cached[start_date]["provider"], "network_requests": 0}
    discovery = race_tushare_sources(
        dataset="futures_mapping", target_key=f"{root_contract}:{start_date}:{end_date}",
        api_name="fut_mapping", params={"ts_code": root_contract},
        validator=lambda frame: bool(
            isinstance(frame, pd.DataFrame) and not frame.empty and "trade_date" in frame.columns
            and any(start_date <= str(value) <= end_date for value in frame["trade_date"])
        ),
        timeout_seconds=12, cooldown_seconds=600,
    )
    frame = discovery.get("frame")
    if discovery.get("ok") and isinstance(frame, pd.DataFrame):
        now = _now()
        with _conn() as conn:
            for row in _frame_rows(frame):
                trade_date = str(row.get("trade_date") or "")
                mapped = str(row.get("mapping_ts_code") or "")
                if not mapped or len(trade_date) != 8:
                    continue
                conn.execute(
                    """INSERT INTO dc_futures_mapping_cache(root_contract,trade_date,mapped_contract,provider,available_time,updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(root_contract,trade_date) DO UPDATE SET
                       mapped_contract=excluded.mapped_contract,provider=excluded.provider,
                       available_time=excluded.available_time,updated_at=excluded.updated_at""",
                    (root_contract, trade_date, mapped, str(discovery.get("winner") or ""), now, now),
                )
        cached = _cached_mappings(root_contract, start_date, end_date)
    if not cached and start_date == end_date:
        with _conn() as conn:
            previous = conn.execute(
                """SELECT trade_date,mapped_contract,provider,available_time
                     FROM dc_futures_mapping_cache
                    WHERE root_contract=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1""",
                (root_contract, start_date),
            ).fetchone()
        if previous:
            item = dict(previous)
            try:
                age_days = (date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
                            - date(int(str(item["trade_date"])[:4]), int(str(item["trade_date"])[4:6]), int(str(item["trade_date"])[6:8]))).days
            except (TypeError, ValueError):
                age_days = 999
            if 0 < age_days <= 10:
                cached[start_date] = {**item, "trade_date": start_date, "mapping_cache_age_days": age_days}
                return cached, {
                    "status": "recent_mapping_cache_fallback", "winner": item.get("provider"),
                    "mapping_source_date": item.get("trade_date"), "mapping_cache_age_days": age_days,
                    "network_discovery": {key: value for key, value in discovery.items() if key != "frame"},
                }
    return cached, {key: value for key, value in discovery.items() if key != "frame"}


def _cached_futures_prices(contract: str, start_date: str, end_date: str) -> dict[str, dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT trade_date,price,provider,available_time FROM dc_futures_price_cache
                WHERE mapped_contract=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date""",
            (contract, start_date, end_date),
        ).fetchall()
    return {str(dict(row)["trade_date"]): dict(row) for row in rows}


def _discover_futures_prices(contract: str, start_date: str, end_date: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    from app.data_platform.services.research_source_discovery import race_tushare_sources

    cached = _cached_futures_prices(contract, start_date, end_date)
    if cached and start_date == end_date and start_date in cached:
        return cached, {"status": "cache_hit", "winner": cached[start_date]["provider"], "network_requests": 0}
    discovery = race_tushare_sources(
        dataset="futures_daily", target_key=f"{contract}:{start_date}:{end_date}",
        api_name="fut_daily", params={"ts_code": contract, "start_date": start_date, "end_date": end_date, "fields": "ts_code,trade_date,close,settle"},
        validator=lambda frame: bool(isinstance(frame, pd.DataFrame) and not frame.empty and "trade_date" in frame.columns),
        timeout_seconds=12, cooldown_seconds=600,
    )
    frame = discovery.get("frame")
    if discovery.get("ok") and isinstance(frame, pd.DataFrame):
        now = _now()
        with _conn() as conn:
            for row in _frame_rows(frame):
                trade_date = str(row.get("trade_date") or "")
                raw = row.get("settle") if row.get("settle") is not None else row.get("close")
                try:
                    price = float(raw)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    """INSERT INTO dc_futures_price_cache(mapped_contract,trade_date,price,provider,available_time,updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(mapped_contract,trade_date) DO UPDATE SET
                       price=excluded.price,provider=excluded.provider,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                    (contract, trade_date, price, str(discovery.get("winner") or ""), now, now),
                )
        cached = _cached_futures_prices(contract, start_date, end_date)
    return cached, {key: value for key, value in discovery.items() if key != "frame"}


def materialize_futures_price_proxies(*, start_date: str, end_date: str, specs: tuple[FuturesProxySpec, ...] = FUTURES_PROXY_SPECS) -> dict[str, Any]:
    """Build exchange-traded commodity price proxies for named industries.

    The mapping endpoint chooses the exchange's mapped contract for each date.
    Historical values are stamped with the materialisation time, so an initial
    backfill can never leak into an earlier point-in-time replay.
    """
    now = _now()
    _ensure_futures_proxy_cache()
    definitions: dict[str, dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    result: dict[str, Any] = {"ok": True, "status": "completed", "sources": [], "definitions": 0, "values": 0, "contract": "exchange_futures_proxy_v2"}
    workers = max(1, min(len(specs), 4))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="futures-mapping") as executor:
        mapping_futures = {
            spec.key: executor.submit(_discover_futures_mapping, spec.root_contract, start_date, end_date)
            for spec in specs
        }
        mappings = {key: future.result() for key, future in mapping_futures.items()}
    contracts = sorted({
        str(row.get("mapped_contract") or "")
        for mapping_rows, _discovery in mappings.values() for row in mapping_rows.values()
        if row.get("mapped_contract")
    })
    with ThreadPoolExecutor(max_workers=max(1, min(len(contracts), 4)), thread_name_prefix="futures-prices") as executor:
        price_futures = {
            contract: executor.submit(_discover_futures_prices, contract, start_date, end_date)
            for contract in contracts
        }
        prices_by_contract = {contract: future.result() for contract, future in price_futures.items()}
    for spec in specs:
        mapping_rows, mapping_discovery = mappings[spec.key]
        mapped_by_date = {
            trade_date: str(row.get("mapped_contract") or "")
            for trade_date, row in mapping_rows.items() if row.get("mapped_contract")
        }
        spec_contracts = sorted(set(mapped_by_date.values()))
        closes: dict[tuple[str, str], dict[str, Any]] = {}
        price_discoveries: list[dict[str, Any]] = []
        for contract in spec_contracts:
            prices, price_discovery = prices_by_contract.get(contract, ({}, {"status": "not_available"}))
            price_discoveries.append(price_discovery)
            for trade_date, row in prices.items():
                closes[(trade_date, contract)] = row
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
                price_row = closes.get((trade_date, contract))
                if price_row is None:
                    continue
                close = float(price_row["price"])
                values.append({
                    "metric_code": code,
                    "data_date": trade_date,
                    "value": close,
                    "raw_value": json.dumps({"root_contract": spec.root_contract, "mapped_contract": contract, "field": "settle_or_close", "mapping_provider": (mapping_rows.get(trade_date) or {}).get("provider"), "price_provider": price_row.get("provider")}, ensure_ascii=False),
                    "source_code": FUTURES_PROXY_SOURCE,
                    "available_time": str(price_row.get("available_time") or now),
                })
                written += 1
        result["sources"].append({"key": spec.key, "root_contract": spec.root_contract, "contracts": len(spec_contracts), "values": written, "mapping_discovery": mapping_discovery, "price_discoveries": price_discoveries})
    result["definitions"] = _upsert_industry_metric_definitions(list(definitions.values()))
    result["values"] = _upsert_industry_metric_values(values)
    missing_sources = [str(source.get("key") or "") for source in result["sources"] if not source.get("values")]
    result["missing_sources"] = missing_sources
    if missing_sources:
        result["status"] = "degraded"
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
