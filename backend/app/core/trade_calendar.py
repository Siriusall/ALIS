"""
A 股交易日历：周末 + 节假日，计算「前一交易日」。
用于行情/日线缓存日期、快照日期等，避免周六日/节假日使用无效日期。
交易日 15:00 前视为「当日数据尚未产生」，按上一交易日处理。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Set
from zoneinfo import ZoneInfo

# 当日行情/日线数据视为可用的截止时间（本地时间，24 时制）；此前用上一交易日
_MARKET_CLOSE_HOUR = 15
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _build_holiday_set() -> Set[date]:
    """构建 A 股休市日集合（周末由逻辑排除，此处仅节假日）。每年休市安排以交易所公告为准，可在此更新。"""
    out: Set[date] = set()

    def add_range(y: int, m: int, d_start: int, d_end: int) -> None:
        for d in range(d_start, d_end + 1):
            out.add(date(y, m, d))

    # 2024
    add_range(2024, 1, 1, 1)       # 元旦
    add_range(2024, 2, 10, 17)    # 春节
    add_range(2024, 4, 4, 6)      # 清明
    add_range(2024, 5, 1, 5)      # 劳动节
    add_range(2024, 6, 10, 10)    # 端午
    add_range(2024, 9, 15, 17)   # 中秋
    add_range(2024, 10, 1, 7)    # 国庆
    # 2025
    add_range(2025, 1, 1, 1)      # 元旦
    add_range(2025, 1, 28, 31)   # 春节 1 月段
    add_range(2025, 2, 1, 4)     # 春节 2 月段
    add_range(2025, 4, 4, 6)     # 清明
    add_range(2025, 5, 1, 5)     # 劳动节
    add_range(2025, 5, 31, 6)    # 端午 (5/31-6/2)
    add_range(2025, 10, 1, 8)   # 国庆 + 中秋
    # 2026
    add_range(2026, 1, 1, 3)     # 元旦
    add_range(2026, 2, 15, 23)   # 春节
    add_range(2026, 4, 4, 6)     # 清明
    add_range(2026, 5, 1, 5)     # 劳动节
    add_range(2026, 6, 19, 21)   # 端午
    add_range(2026, 9, 25, 27)   # 中秋
    add_range(2026, 10, 1, 7)    # 国庆

    return out


_A_SHARE_HOLIDAYS: Set[date] = _build_holiday_set()


def is_trade_day(d: date) -> bool:
    """是否为 A 股交易日（非周末且非节假日）。"""
    if d.weekday() >= 5:  # 5=周六, 6=周日
        return False
    return d not in _A_SHARE_HOLIDAYS


def get_last_trade_date(as_of: date | None = None, for_data_availability: bool = False) -> date:
    """
    获取「前一交易日」：若 as_of 为交易日则返回 as_of，否则向前回溯到最近一个交易日。
    as_of 默认为今天；用于行情/日线/快照的基准日期。
    for_data_availability=True 时：若今天为交易日且当前时间早于 15:00，视为当日数据尚未产生，返回上一交易日。
    """
    d = as_of if as_of is not None else datetime.now(_BUSINESS_TZ).date()
    if for_data_availability and is_trade_day(d):
        now = datetime.now(_BUSINESS_TZ)
        if now.hour < _MARKET_CLOSE_HOUR:
            d = d - timedelta(days=1)
    while not is_trade_day(d):
        d = d - timedelta(days=1)
    return d


def get_last_trade_date_str(as_of: date | None = None, for_data_availability: bool = False) -> str:
    """返回 YYYYMMDD 格式的前一交易日。for_data_availability 含义同 get_last_trade_date。"""
    return get_last_trade_date(as_of, for_data_availability).strftime("%Y%m%d")
