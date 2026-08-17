from __future__ import annotations

"""
基于规范数据平台的基础信息与指数成分服务：
- basic_info: ts_code/symbol/name/industry/list_date/list_status
- index_constituents: 指数成分及权重

读取 PostgreSQL 的可追溯投影；刷新由数据平台执行，不允许业务进程直连供应商。
"""

import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.data_platform.db import _conn

logger = logging.getLogger("quant.tushare_universe")


@dataclass
class BasicInfo:
    ts_code: str
    symbol: str
    name: str
    area: str
    industry: str
    list_date: str
    list_status: str


@dataclass
class IndexConstituent:
    index_code: str
    ts_code: str
    weight: float
    trade_date: str


_basic_info_cache: Dict[str, BasicInfo] = {}
_index_constituents_cache: Dict[str, List[IndexConstituent]] = {}
_basic_cache_warning_logged = False
_index_cache_warning_logged: set[str] = set()


def _read_basic_info() -> Dict[str, BasicInfo]:
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT ts_code,symbol,name,area,industry,list_date
                   FROM dc_stock_master
                   ORDER BY symbol"""
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - callers receive an empty, explicit unavailable result
        logger.warning("规范证券主表读取失败: %s", str(exc)[:160])
        return {}
    out: Dict[str, BasicInfo] = {}
    for raw in rows:
        row = dict(raw)
        symbol = str(row.get("symbol") or row.get("ts_code") or "").split(".", 1)[0].zfill(6)
        if not symbol:
            continue
        out[symbol] = BasicInfo(
            ts_code=str(row.get("ts_code") or ""), symbol=symbol,
            name=str(row.get("name") or ""), area=str(row.get("area") or ""),
            industry=str(row.get("industry") or ""), list_date=str(row.get("list_date") or ""),
            list_status="L",
        )
    return out


def _read_index_constituents(index_code: str, start_date: str, end_date: str) -> List[IndexConstituent]:
    start = str(start_date or "").replace("-", "")[:8]
    end = str(end_date or "").replace("-", "")[:8]
    if len(start) != 8 or len(end) != 8:
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT trade_date,payload_json
                   FROM dc_tushare_raw_records
                   WHERE api_name='index_weight' AND trade_date<=?
                   ORDER BY trade_date DESC""",
                (end,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - caller's universe gate handles absence
        logger.warning("规范指数成分读取失败 index=%s: %s", index_code, str(exc)[:160])
        return []

    # Index weights are published periodically.  Use the newest published
    # constituent date at or before the requested end date, never a later one.
    target_date = ""
    parsed: list[tuple[str, dict]] = []
    for raw in rows:
        row = dict(raw)
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if str(payload.get("index_code") or "") != index_code:
            continue
        trade_date = str(payload.get("trade_date") or row.get("trade_date") or "")
        if len(trade_date) != 8 or not trade_date.isdigit() or trade_date > end:
            continue
        if not target_date:
            target_date = trade_date
        if trade_date == target_date:
            parsed.append((trade_date, payload))
    return [
        IndexConstituent(
            index_code=index_code,
            ts_code=str(payload.get("con_code") or ""),
            weight=float(payload.get("weight") or 0.0),
            trade_date=trade_date,
        )
        for trade_date, payload in parsed
        if str(payload.get("con_code") or "")
    ]


def get_basic_info_map(refresh: bool = False, cache_only: bool = False) -> Dict[str, BasicInfo]:
    """
    获取全市场基础信息映射：symbol(6位) -> BasicInfo。
    默认缓存结果，如需强制刷新传 refresh=True。
    cache_only=True 时不发起同步，但仍可读取规范 PostgreSQL。
    """
    global _basic_info_cache, _basic_cache_warning_logged
    if _basic_info_cache and not refresh:
        return _basic_info_cache
    out = _read_basic_info()
    if not out and not cache_only:
        from app.data_platform.services.data_center import sync_stock_basic

        sync_stock_basic()
        out = _read_basic_info()
    if not out and cache_only and not _basic_cache_warning_logged:
        logger.warning("规范证券主表未初始化；业务读取将保持不可用")
        _basic_cache_warning_logged = True
    _basic_info_cache = out
    return out


def get_index_constituents(
    index_code: str,
    start_date: str,
    end_date: str,
    use_cache: bool = True,
    cache_only: bool = False,
) -> List[IndexConstituent]:
    """
    获取某指数在给定区间内的成分股及权重。
    结果缓存键为 index_code(start_date,end_date) 简单合成。
    cache_only=True 时不发起同步，但仍可读取规范 PostgreSQL。
    """
    key = f"{index_code}:{start_date}:{end_date}"
    if use_cache and key in _index_constituents_cache:
        return _index_constituents_cache[key]
    out = _read_index_constituents(index_code, start_date, end_date)
    if not out and not cache_only:
        from app.data_platform.services.data_center import sync_tushare_api_raw

        sync_tushare_api_raw(
            "index_weight",
            {"index_code": index_code, "start_date": start_date, "end_date": end_date},
        )
        out = _read_index_constituents(index_code, start_date, end_date)
    if not out and cache_only and key not in _index_cache_warning_logged:
        logger.warning("规范指数成分未初始化（%s）；业务读取将保持不可用", key)
        _index_cache_warning_logged.add(key)
    _index_constituents_cache[key] = out
    return out


def build_index_membership_set(
    index_code: str,
    trade_date: str,
    cache_only: bool = False,
) -> Dict[str, bool]:
    """
    构建某指数在指定日期的成分集合：symbol(6位) -> True/False。
    简单策略：使用 index_weight 接口在 trade_date 之前最近的一天的权重记录。
    cache_only 会传给 get_index_constituents；True 时无缓存返回 {}。
    """
    cons = get_index_constituents(
        index_code=index_code,
        start_date=trade_date,
        end_date=trade_date,
        cache_only=cache_only,
    )
    members: Dict[str, bool] = {}
    for c in cons:
        ts_code = c.ts_code
        symbol = ts_code.split(".")[0].zfill(6) if ts_code else ""
        if symbol:
            members[symbol] = True
    return members
