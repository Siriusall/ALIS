from __future__ import annotations

"""
基于 TuShare 的基础信息与指数成分服务：
- basic_info: ts_code/symbol/name/industry/list_date/list_status
- index_constituents: 指数成分及权重

同样只在内存中缓存，不直接操作数据库。
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.integrations.tushare_client import get_tushare_client

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
_RETRY_DELAYS_SEC = (0.8, 1.5)


def get_basic_info_map(refresh: bool = False, cache_only: bool = False) -> Dict[str, BasicInfo]:
    """
    获取全市场基础信息映射：symbol(6位) -> BasicInfo。
    默认缓存结果，如需强制刷新传 refresh=True。
    cache_only=True 时仅读缓存，无缓存则返回 {} 且不请求 TuShare（业务侧在非 init 路径传 True）。
    """
    global _basic_info_cache, _basic_cache_warning_logged
    if _basic_info_cache and not refresh:
        return _basic_info_cache
    if cache_only:
        if not _basic_cache_warning_logged:
            logger.warning("TuShare 基础信息缓存未初始化；请通过已登录会话执行 POST /api/init（后续请求不再重复刷屏）")
            _basic_cache_warning_logged = True
        return {}
    client = get_tushare_client()
    df = None
    last_err: Exception | None = None
    for i in range(len(_RETRY_DELAYS_SEC) + 1):
        try:
            df = client.stock_basic(
                list_status="L",
                fields="ts_code,symbol,name,area,industry,list_date,list_status",
            )
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < len(_RETRY_DELAYS_SEC):
                time.sleep(_RETRY_DELAYS_SEC[i])
                continue
    if df is None:
        logger.warning(
            "TuShare 基础信息拉取失败（%s），回退到已有缓存（%d 条）",
            str(last_err)[:120] if last_err else "unknown",
            len(_basic_info_cache),
        )
        return _basic_info_cache
    out: Dict[str, BasicInfo] = {}
    if not df.empty:
        for _, row in df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            symbol = str(row.get("symbol", "")).zfill(6)
            info = BasicInfo(
                ts_code=ts_code,
                symbol=symbol,
                name=str(row.get("name", "")),
                area=str(row.get("area", "")),
                industry=str(row.get("industry", "")),
                list_date=str(row.get("list_date", "")),
                list_status=str(row.get("list_status", "")),
            )
            out[symbol] = info
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
    cache_only=True 时仅读缓存，无缓存则返回 [] 且不请求 TuShare。
    """
    key = f"{index_code}:{start_date}:{end_date}"
    if use_cache and key in _index_constituents_cache:
        return _index_constituents_cache[key]
    if cache_only:
        if key not in _index_cache_warning_logged:
            logger.warning("TuShare 指数成分缓存未初始化（%s）；请通过已登录会话执行 POST /api/init", key)
            _index_cache_warning_logged.add(key)
        return []
    client = get_tushare_client()
    df = None
    last_err: Exception | None = None
    for i in range(len(_RETRY_DELAYS_SEC) + 1):
        try:
            df = client.index_weight(index_code=index_code, start_date=start_date, end_date=end_date)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < len(_RETRY_DELAYS_SEC):
                time.sleep(_RETRY_DELAYS_SEC[i])
                continue
    if df is None:
        logger.warning(
            "TuShare 指数成分拉取失败（%s），回退缓存 key=%s（%d 条）",
            str(last_err)[:120] if last_err else "unknown",
            key,
            len(_index_constituents_cache.get(key, [])),
        )
        return _index_constituents_cache.get(key, [])
    out: List[IndexConstituent] = []
    if not df.empty:
        for _, row in df.iterrows():
            out.append(
                IndexConstituent(
                    index_code=str(row.get("index_code", index_code)),
                    ts_code=str(row.get("con_code", "")),
                    weight=float(row.get("weight", 0) or 0.0),
                    trade_date=str(row.get("trade_date", "")),
                )
            )
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

