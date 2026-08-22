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
import time
from typing import Any

import pandas as pd

from app.data_platform.db import _conn, _read_conn
from app.data_platform.services.data_center import _now, _upsert_industry_metric_definitions, _upsert_industry_metric_values
from app.services.industry_event_engine import _derived_industry_proxies, _is_statement_timeout


FINANCIAL_SOURCE = "tushare.financial_pit"
FUTURES_PROXY_SOURCE = "exchange.futures_proxy"
PPI_PROXY_SOURCE = "tushare.cn_ppi_proxy"


@dataclass(frozen=True)
class FuturesProxySpec:
    key: str
    root_contract: str
    name: str
    industries: tuple[str, ...]
    cost_industries: tuple[str, ...] = ()
    lag_days: int = 5
    market_role: str = "industry_transmission"


FUTURES_PROXY_SPECS = (
    FuturesProxySpec("copper", "CU.SHF", "沪铜", ("铜",), ("元器件", "家用电器", "电气设备"), 10),
    FuturesProxySpec("aluminium", "AL.SHF", "沪铝", ("铝",), ("家用电器", "汽车配件"), 10),
    FuturesProxySpec("zinc", "ZN.SHF", "沪锌", ("铅锌",), ("钢加工", "家用电器"), 10),
    FuturesProxySpec("lead", "PB.SHF", "沪铅", ("铅锌",), ("电气设备", "汽车配件"), 10),
    FuturesProxySpec("nickel", "NI.SHF", "沪镍", ("小金属",), ("特种钢", "电气设备"), 10),
    FuturesProxySpec("tin", "SN.SHF", "沪锡", ("小金属",), ("元器件", "半导体"), 10),
    FuturesProxySpec("alumina", "AO.SHF", "氧化铝", ("化工原料",), ("铝",), 5),
    FuturesProxySpec("industrial_silicon", "SI.GFE", "工业硅", ("小金属",), ("电气设备", "半导体"), 10),
    FuturesProxySpec("polysilicon", "PS.GFE", "多晶硅", ("化工原料",), ("电气设备",), 10),
    FuturesProxySpec("lithium_carbonate", "LC.GFE", "碳酸锂", ("小金属",), ("电气设备",), 10),
    FuturesProxySpec("gold", "AU.SHF", "沪金", ("黄金",), (), 1, "risk_regime"),
    FuturesProxySpec("silver", "AG.SHF", "沪银", ("小金属",), ("元器件",), 5, "risk_and_industry"),
    FuturesProxySpec("iron_ore", "I.DCE", "铁矿石", (), ("普钢", "特种钢"), 10),
    FuturesProxySpec("coking_coal", "JM.DCE", "焦煤", ("煤炭开采",), ("焦炭加工", "普钢"), 5),
    FuturesProxySpec("coke", "J.DCE", "焦炭", ("焦炭加工",), ("普钢", "特种钢"), 5),
    FuturesProxySpec("rebar", "RB.SHF", "螺纹钢", ("普钢",), ("建筑工程", "工程机械"), 10),
    FuturesProxySpec("hot_rolled_coil", "HC.SHF", "热轧卷板", ("普钢", "钢加工"), ("汽车配件", "家用电器"), 10),
    FuturesProxySpec("ferrosilicon", "SF.ZCE", "硅铁", ("小金属",), ("普钢", "特种钢"), 10),
    FuturesProxySpec("silicomanganese", "SM.ZCE", "锰硅", ("小金属",), ("普钢", "特种钢"), 10),
    FuturesProxySpec("crude_oil", "SC.INE", "原油", ("石油开采",), ("石油加工", "化工原料", "空运", "水运"), 5),
    FuturesProxySpec("fuel_oil", "FU.SHF", "燃料油", ("石油加工",), ("水运", "火力发电"), 5),
    FuturesProxySpec("low_sulfur_fuel", "LU.INE", "低硫燃料油", ("石油加工",), ("水运",), 5),
    FuturesProxySpec("bitumen", "BU.SHF", "沥青", ("石油加工",), ("公路", "路桥", "建筑工程"), 10),
    FuturesProxySpec("lpg", "PG.DCE", "液化石油气", ("石油加工",), ("供气供热", "化工原料"), 5),
    FuturesProxySpec("pta", "TA.ZCE", "PTA", ("化工原料",), ("化纤", "纺织"), 10),
    FuturesProxySpec("ethylene_glycol", "EG.DCE", "乙二醇", ("化工原料",), ("化纤",), 10),
    FuturesProxySpec("methanol", "MA.ZCE", "甲醇", ("化工原料",), ("塑料", "日用化工"), 10),
    FuturesProxySpec("pvc", "V.DCE", "PVC", ("化工原料",), ("塑料", "其他建材"), 10),
    FuturesProxySpec("polyethylene", "L.DCE", "聚乙烯", ("化工原料",), ("塑料", "广告包装"), 10),
    FuturesProxySpec("polypropylene", "PP.DCE", "聚丙烯", ("化工原料",), ("塑料", "家用电器"), 10),
    FuturesProxySpec("styrene", "EB.DCE", "苯乙烯", ("化工原料",), ("塑料", "家居用品"), 10),
    FuturesProxySpec("soda_ash", "SA.ZCE", "纯碱", ("化工原料",), ("玻璃",), 10),
    FuturesProxySpec("urea", "UR.ZCE", "尿素", ("化工原料",), ("农药化肥", "种植业"), 10),
    FuturesProxySpec("soybean", "A.DCE", "豆一", ("种植业",), ("食品", "饲料"), 15),
    FuturesProxySpec("soymeal", "M.DCE", "豆粕", ("饲料",), ("农业综合", "渔业"), 10),
    FuturesProxySpec("soybean_oil", "Y.DCE", "豆油", (), ("食品",), 5),
    FuturesProxySpec("palm_oil", "P.DCE", "棕榈油", (), ("食品", "日用化工"), 5),
    FuturesProxySpec("corn", "C.DCE", "玉米", ("种植业",), ("饲料", "食品"), 15),
    FuturesProxySpec("sugar", "SR.ZCE", "白糖", ("种植业",), ("食品", "软饮料"), 10),
    FuturesProxySpec("cotton", "CF.ZCE", "棉花", ("种植业",), ("纺织", "服饰"), 15),
    FuturesProxySpec("natural_rubber", "RU.SHF", "天然橡胶", ("橡胶",), ("汽车配件",), 10),
    FuturesProxySpec("pulp", "SP.SHF", "纸浆", ("林业",), ("造纸", "广告包装"), 10),
    FuturesProxySpec("rapeseed_oil", "OI.ZCE", "菜籽油", (), ("食品",), 5),
    FuturesProxySpec("rapeseed_meal", "RM.ZCE", "菜籽粕", ("饲料",), ("农业综合", "渔业"), 10),
    FuturesProxySpec("glass", "FG.ZCE", "玻璃", ("玻璃",), ("家居用品", "汽车配件"), 10),
    FuturesProxySpec("live_hog", "LH.DCE", "生猪", ("农业综合",), ("食品",), 10),
    FuturesProxySpec("egg", "JD.DCE", "鸡蛋", ("农业综合",), ("食品",), 5),
    FuturesProxySpec("apple", "AP.ZCE", "苹果", ("种植业",), ("食品",), 10),
    FuturesProxySpec("red_date", "CJ.ZCE", "红枣", ("种植业",), ("食品",), 10),
    FuturesProxySpec("peanut", "PK.ZCE", "花生", ("种植业",), ("食品",), 10),
    FuturesProxySpec("stainless_steel", "SS.SHF", "不锈钢", ("特种钢", "钢加工"), ("家用电器", "机械基件"), 10),
    FuturesProxySpec("cotton_yarn", "CY.ZCE", "棉纱", ("纺织",), ("服饰",), 10),
    FuturesProxySpec("no20_rubber", "NR.INE", "20号胶", ("橡胶",), ("汽车配件",), 10),
    FuturesProxySpec("japonica_rice", "RR.DCE", "粳米", ("种植业",), ("食品",), 10),
    FuturesProxySpec("paraxylene", "PX.ZCE", "对二甲苯", ("化工原料",), ("化纤",), 10),
    FuturesProxySpec("caustic_soda", "SH.ZCE", "烧碱", ("化工原料",), ("造纸", "铝"), 10),
    FuturesProxySpec("rapeseed", "RS.ZCE", "油菜籽", ("种植业",), ("食品", "饲料"), 15),
    FuturesProxySpec("butadiene_rubber", "BR.SHF", "丁二烯橡胶", ("化工原料",), ("橡胶", "汽车配件"), 10),
    FuturesProxySpec("container_freight", "EC.SHF", "集运指数欧线", ("水运",), ("商贸代理",), 5, "risk_and_industry"),
    FuturesProxySpec("bottle_chip", "PR.ZCE", "瓶片", ("化纤",), ("软饮料", "塑料"), 10),
    FuturesProxySpec("benzene", "BZ.ZCE", "纯苯", ("化工原料",), ("染料涂料", "塑料"), 10),
    FuturesProxySpec("cast_aluminium_alloy", "AD.SHF", "铸造铝合金", ("铝",), ("汽车配件", "机械基件"), 10),
)

FUTURES_PROXY_EXCLUSIONS = {
    "ZC.ZCE": "target_date_not_published_or_inactive",
    "PM.ZCE": "target_date_not_published_or_inactive",
    "WH.ZCE": "target_date_not_published_or_inactive",
    "RI.ZCE": "target_date_not_published_or_inactive",
    "LR.ZCE": "target_date_not_published_or_inactive",
    "JR.ZCE": "target_date_not_published_or_inactive",
}

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
    requested = max(1, int(max_periods))
    with _read_conn() as conn:
        # Discover the time span with a pair of index-friendly aggregates.
        span = conn.execute(
            """SELECT MIN(available_time) AS earliest,
                              MAX(available_time) AS latest
                         FROM dc_tushare_raw_records
                        WHERE api_name IN ('income','balancesheet')
                          AND COALESCE(available_time,'')<>''"""
        ).fetchone()
        span_item = dict(span or {})
        earliest_text = str(span_item.get("earliest") or "")[:10]
        latest_text = str(span_item.get("latest") or "")[:10]
        try:
            earliest = date.fromisoformat(earliest_text)
            latest = date.fromisoformat(latest_text)
        except ValueError:
            return []

        def quarter_start(value: date) -> date:
            return date(value.year, ((value.month - 1) // 3) * 3 + 1, 1)

        def previous_quarter(value: date) -> date:
            return date(value.year - 1, 10, 1) if value.month == 1 else date(
                value.year, value.month - 3, 1
            )

        def next_quarter(value: date) -> date:
            return date(value.year + 1, 1, 1) if value.month == 10 else date(
                value.year, value.month + 3, 1
            )

        cursor = quarter_start(latest)
        oldest_quarter = quarter_start(earliest)
        values: list[str] = []
        # At most 256 short, range-indexed probes for sparse histories.
        for _ in range(256):
            if cursor < oldest_quarter:
                break
            next_start = next_quarter(cursor)
            row = conn.execute(
                """SELECT MAX(available_time) AS cutoff
                           FROM dc_tushare_raw_records
                          WHERE api_name IN ('income','balancesheet')
                            AND available_time>=? AND available_time<?
                            AND COALESCE(available_time,'')<>''""",
                (cursor.isoformat(), next_start.isoformat()),
            ).fetchone()
            cutoff = str(dict(row or {}).get("cutoff") or "")[:10]
            if cutoff:
                values.append(cutoff)
                if len(values) >= requested:
                    break
            cursor = previous_quarter(cursor)

    return sorted(set(values))[-requested:]


def materialize_financial_industry_series(*, max_periods: int = 12) -> dict[str, Any]:
    """Materialise point-in-time industry operating and valuation proxies.

    Values are derived only from statements and daily snapshots whose recorded
    available time precedes the quarter cutoff.  They are formal derived
    metrics, not a substitute for vendor-supplied product or inventory feeds.
    """
    definitions: dict[str, dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    cutoffs = _cutoff_dates(max_periods)
    query_attempts = 0
    for cutoff in cutoffs:
        for attempt in range(2):
            query_attempts += 1
            try:
                with _read_conn() as conn:
                    # Formal industry proxies use the shared explicit-clock
                    # contract.  A date-only/naive timestamp could otherwise
                    # be interpreted in the host timezone and silently
                    # backdate evidence.
                    derived = _derived_industry_proxies(conn, as_of_time=f"{cutoff}T23:59:59+08:00")
                break
            except Exception as exc:
                if attempt or not _is_statement_timeout(exc):
                    raise
                # PostgreSQL leaves the timed-out transaction aborted.  Exit
                # the context, wait briefly, then retry with a new connection.
                time.sleep(0.25)
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
                    "available_time": f"{cutoff}T23:59:59+08:00",
                })
    return {
        "ok": True,
        "cutoffs": len(cutoffs),
        "definitions": _upsert_industry_metric_definitions(list(definitions.values())),
        "values": _upsert_industry_metric_values(values),
        "query_attempts": query_attempts,
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
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_futures_proxy_registry (
            proxy_key TEXT PRIMARY KEY,root_contract TEXT NOT NULL,name TEXT NOT NULL,
            revenue_industries_json TEXT NOT NULL,cost_industries_json TEXT NOT NULL,
            lag_trading_days INTEGER NOT NULL,market_role TEXT NOT NULL,
            contract_version TEXT NOT NULL,updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_futures_source_reconciliation (
            root_contract TEXT NOT NULL,trade_date TEXT NOT NULL,primary_provider TEXT NOT NULL,
            comparison_provider TEXT NOT NULL,primary_price REAL NOT NULL,comparison_price REAL NOT NULL,
            relative_diff REAL NOT NULL,status TEXT NOT NULL,evidence_json TEXT NOT NULL,checked_at TEXT NOT NULL,
            PRIMARY KEY(root_contract,trade_date,primary_provider,comparison_provider)
        )""")


def _sync_futures_proxy_registry(specs: tuple[FuturesProxySpec, ...]) -> None:
    now = _now()
    with _conn() as conn:
        for spec in specs:
            conn.execute(
                """INSERT INTO dc_futures_proxy_registry(
                       proxy_key,root_contract,name,revenue_industries_json,cost_industries_json,
                       lag_trading_days,market_role,contract_version,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(proxy_key) DO UPDATE SET
                       root_contract=excluded.root_contract,name=excluded.name,
                       revenue_industries_json=excluded.revenue_industries_json,
                       cost_industries_json=excluded.cost_industries_json,
                       lag_trading_days=excluded.lag_trading_days,market_role=excluded.market_role,
                       contract_version=excluded.contract_version,updated_at=excluded.updated_at""",
                (
                    spec.key, spec.root_contract, spec.name,
                    json.dumps(spec.industries, ensure_ascii=False),
                    json.dumps(spec.cost_industries, ensure_ascii=False),
                    spec.lag_days, spec.market_role, "alis_futures_industry_transmission_v3", now,
                ),
            )


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


def _persist_direct_futures_cache(root_contract: str, frame: pd.DataFrame) -> None:
    now = _now()
    with _conn() as conn:
        for row in _frame_rows(frame):
            trade_date = str(row.get("trade_date") or "")
            contract = str(row.get("mapped_contract") or "")
            provider = str(row.get("provider") or "sina_futures_continuous")
            if not trade_date or not contract:
                continue
            conn.execute(
                """INSERT INTO dc_futures_mapping_cache(root_contract,trade_date,mapped_contract,provider,available_time,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(root_contract,trade_date) DO UPDATE SET
                   mapped_contract=excluded.mapped_contract,provider=excluded.provider,
                   available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (root_contract, trade_date, contract, provider, now, now),
            )
            conn.execute(
                """INSERT INTO dc_futures_price_cache(mapped_contract,trade_date,price,provider,available_time,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(mapped_contract,trade_date) DO UPDATE SET
                   price=excluded.price,provider=excluded.provider,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (contract, trade_date, float(row["price"]), provider, now, now),
            )


def audit_futures_cross_source(
    trade_date: str, specs: tuple[FuturesProxySpec, ...], *, tolerance: float = 0.005,
) -> dict[str, Any]:
    """Compare cached mapped-contract settlements with independent continuous settlements."""
    from app.data_platform.services.independent_research_sources import fetch_sina_continuous_futures

    roots = {spec.root_contract for spec in specs}
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT m.root_contract,p.price,p.provider
                 FROM dc_futures_mapping_cache m
                 JOIN dc_futures_price_cache p
                   ON p.mapped_contract=m.mapped_contract AND p.trade_date=m.trade_date
                WHERE m.trade_date=? AND p.provider<>'sina_futures_continuous'""",
            (trade_date,),
        ).fetchall() if str(dict(row).get("root_contract") or "") in roots]
        existing = {
            str(dict(row).get("root_contract") or "") for row in conn.execute(
                """SELECT root_contract,status FROM dc_futures_source_reconciliation
                    WHERE trade_date=? AND comparison_provider='sina_futures_continuous'""",
                (trade_date,),
            ).fetchall() if str(dict(row).get("status") or "matched") == "matched"
        }
    pending = [row for row in rows if str(row.get("root_contract") or "") not in existing]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(len(pending), 6)), thread_name_prefix="futures-reconcile") as executor:
        futures = {
            executor.submit(fetch_sina_continuous_futures, str(row["root_contract"]), trade_date, trade_date): row
            for row in pending
        }
        for future, source in futures.items():
            try:
                frame = future.result()
            except Exception as exc:  # noqa: BLE001
                results.append({"root_contract": source["root_contract"], "status": "comparison_failed", "error": str(exc)[:200]})
                continue
            if frame.empty:
                results.append({"root_contract": source["root_contract"], "status": "comparison_missing"})
                continue
            comparison = float(frame.iloc[0]["price"])
            primary = float(source["price"])
            relative_diff = abs(comparison - primary) / max(abs(primary), 1e-9)
            status = "matched" if relative_diff <= tolerance else "conflict"
            item = {
                "root_contract": source["root_contract"], "primary_provider": source["provider"],
                "comparison_provider": "sina_futures_continuous", "primary_price": primary,
                "comparison_price": comparison, "relative_diff": relative_diff, "status": status,
            }
            results.append(item)
            with _conn() as conn:
                conn.execute(
                    """INSERT INTO dc_futures_source_reconciliation(
                           root_contract,trade_date,primary_provider,comparison_provider,primary_price,
                           comparison_price,relative_diff,status,evidence_json,checked_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(root_contract,trade_date,primary_provider,comparison_provider)
                       DO UPDATE SET primary_price=excluded.primary_price,comparison_price=excluded.comparison_price,
                       relative_diff=excluded.relative_diff,status=excluded.status,evidence_json=excluded.evidence_json,
                       checked_at=excluded.checked_at""",
                    (
                        source["root_contract"], trade_date, source["provider"], "sina_futures_continuous",
                        primary, comparison, relative_diff, status,
                        json.dumps({"tolerance": tolerance, "contract_basis": "mapped_settlement_vs_continuous_settlement"}), _now(),
                    ),
                )
    with _conn() as conn:
        aggregate = [dict(row) for row in conn.execute(
            "SELECT status,COUNT(*) count FROM dc_futures_source_reconciliation WHERE trade_date=? GROUP BY status",
            (trade_date,),
        ).fetchall()]
    counts = {str(row["status"]): int(row["count"]) for row in aggregate}
    return {
        "status": "conflict" if counts.get("conflict") else "passed" if counts.get("matched") else "observing",
        "matched": counts.get("matched", 0), "conflicts": counts.get("conflict", 0),
        "new_checks": len(results), "tolerance": tolerance,
    }
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
    _sync_futures_proxy_registry(specs)
    definitions: dict[str, dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ok": True, "status": "completed", "sources": [], "definitions": 0, "values": 0,
        "registered_products": len(specs), "contract": "alis_futures_industry_transmission_v3",
        "explicit_exclusions": FUTURES_PROXY_EXCLUSIONS,
    }
    workers = max(1, min(len(specs), 4))
    direct_rows: dict[str, pd.DataFrame] = {}
    direct_network_keys: set[str] = set()
    from app.data_platform.services.independent_research_sources import fetch_sina_continuous_futures

    if start_date == end_date:
        for spec in specs:
            cached_mapping = _cached_mappings(spec.root_contract, start_date, end_date)
            mapping = cached_mapping.get(start_date) or {}
            contract = str(mapping.get("mapped_contract") or "")
            cached_price = (_cached_futures_prices(contract, start_date, end_date).get(start_date) or {}) if contract else {}
            if contract and cached_price:
                direct_rows[spec.key] = pd.DataFrame([{
                    "trade_date": start_date, "price": cached_price["price"],
                    "mapped_contract": contract, "provider": cached_price.get("provider") or mapping.get("provider"),
                }])
    network_specs = tuple(spec for spec in specs if spec.key not in direct_rows)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sina-futures") as executor:
        direct_futures = {
            spec.key: executor.submit(
                fetch_sina_continuous_futures, spec.root_contract, start_date, end_date,
                timeout_seconds=8.0,
            ) for spec in network_specs
        }
        for key, future in direct_futures.items():
            try:
                direct_rows[key] = future.result()
                if not direct_rows[key].empty:
                    direct_network_keys.add(key)
                    spec = next(item for item in network_specs if item.key == key)
                    _persist_direct_futures_cache(spec.root_contract, direct_rows[key])
            except Exception:  # noqa: BLE001 - TuShare path remains available
                direct_rows[key] = pd.DataFrame()
    missing_specs = tuple(spec for spec in specs if direct_rows.get(spec.key, pd.DataFrame()).empty)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="futures-mapping") as executor:
        mapping_futures = {
            spec.key: executor.submit(_discover_futures_mapping, spec.root_contract, start_date, end_date)
            for spec in missing_specs
        }
        mappings = {key: future.result() for key, future in mapping_futures.items()}
    now_available = _now()
    for spec in specs:
        frame = direct_rows.get(spec.key, pd.DataFrame())
        if frame.empty:
            continue
        mapping_rows: dict[str, dict[str, Any]] = {}
        for row in _frame_rows(frame):
            trade_date = str(row.get("trade_date") or "")
            contract = str(row.get("mapped_contract") or "")
            mapping_rows[trade_date] = {
                "mapped_contract": contract, "provider": str(row.get("provider") or "sina_futures_continuous"),
                "available_time": now_available,
            }
        mappings[spec.key] = (mapping_rows, {
            "status": "available" if spec.key in direct_network_keys else "cache_hit",
            "winner": str(frame.iloc[0].get("provider") or ""),
            "network_requests": 1 if spec.key in direct_network_keys else 0,
            "contract": "independent_continuous_futures_range_v2",
        })
    contracts = sorted({
        str(row.get("mapped_contract") or "")
        for mapping_rows, _discovery in mappings.values() for row in mapping_rows.values()
        if row.get("mapped_contract")
    })
    with ThreadPoolExecutor(max_workers=max(1, min(len(contracts), 4)), thread_name_prefix="futures-prices") as executor:
        price_futures = {
            contract: executor.submit(_discover_futures_prices, contract, start_date, end_date)
            for contract in contracts if not contract.startswith("SINA:")
        }
        prices_by_contract = {contract: future.result() for contract, future in price_futures.items()}
    for frame in direct_rows.values():
        for row in _frame_rows(frame):
            contract = str(row.get("mapped_contract") or "")
            trade_date = str(row.get("trade_date") or "")
            if not contract or not trade_date:
                continue
            price_rows, _evidence = prices_by_contract.setdefault(
                contract, ({}, {"status": "available", "winner": "sina_futures_continuous", "network_requests": 1}),
            )
            price_rows[trade_date] = {
                "price": float(row["price"]), "provider": "sina_futures_continuous",
                "available_time": now_available,
                "previous_settle": row.get("previous_settle"),
                "daily_return": row.get("daily_return"),
                "continuity_status": row.get("continuity_status"),
            }
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
        exposures = [(industry, 1, "revenue") for industry in spec.industries]
        exposures.extend(
            (industry, -1, "cost") for industry in spec.cost_industries if industry not in spec.industries
        )
        for industry, direction, exposure_role in exposures:
            code = _metric_code(f"futures_proxy_{exposure_role}", spec.key, industry)
            definitions[code] = {
                "metric_code": code,
                "industry_code": f"master:{industry}",
                "industry_name": industry,
                "metric_name": f"产品价格：{spec.name}（{'收入端' if direction > 0 else '成本端'}传导）",
                "unit": "direction_adjusted_price",
                "frequency": "daily",
                "source_code": FUTURES_PROXY_SOURCE,
                "description": (
                    f"交易所连续期货价格代理；transmission_direction={direction};"
                    f"exposure_role={exposure_role};lag_trading_days={spec.lag_days};market_role={spec.market_role}。"
                    "正值方向表示对行业景气的理论边际影响，不等同于行业现货价格或企业实际实现价。"
                ),
            }
            for trade_date, contract in mapped_by_date.items():
                price_row = closes.get((trade_date, contract))
                if price_row is None:
                    continue
                close = float(price_row["price"])
                values.append({
                    "metric_code": code,
                    "data_date": trade_date,
                    "value": close * direction,
                    "raw_value": json.dumps({
                        "raw_price": close, "root_contract": spec.root_contract, "mapped_contract": contract,
                        "field": "settle_or_close", "mapping_provider": (mapping_rows.get(trade_date) or {}).get("provider"),
                        "price_provider": price_row.get("provider"), "transmission_direction": direction,
                        "exposure_role": exposure_role, "lag_trading_days": spec.lag_days,
                        "market_role": spec.market_role,
                        "previous_settle": price_row.get("previous_settle"),
                        "daily_return": price_row.get("daily_return"),
                        "continuity_status": price_row.get("continuity_status"),
                    }, ensure_ascii=False),
                    "source_code": FUTURES_PROXY_SOURCE,
                    "available_time": str(price_row.get("available_time") or now),
                })
                written += 1
        result["sources"].append({
            "key": spec.key, "root_contract": spec.root_contract, "contracts": len(spec_contracts),
            "exposures": len(exposures), "revenue_industries": list(spec.industries),
            "cost_industries": list(spec.cost_industries), "lag_trading_days": spec.lag_days,
            "market_role": spec.market_role, "values": written,
            "mapping_discovery": mapping_discovery, "price_discoveries": price_discoveries,
        })
    result["definitions"] = _upsert_industry_metric_definitions(list(definitions.values()))
    result["values"] = _upsert_industry_metric_values(values)
    missing_sources = [str(source.get("key") or "") for source in result["sources"] if not source.get("values")]
    result["missing_sources"] = missing_sources
    result["ready_products"] = len(specs) - len(missing_sources)
    result["coverage_ratio"] = round(result["ready_products"] / len(specs), 6) if specs else 1.0
    if missing_sources:
        result["status"] = "degraded"
    if start_date == end_date:
        result["cross_source_audit"] = audit_futures_cross_source(start_date, specs)
        if result["cross_source_audit"].get("status") == "conflict":
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
