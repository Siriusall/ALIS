from __future__ import annotations

from typing import Optional


def normalize_exchange_group(exchange: Optional[str]) -> str:
    x = str(exchange or "").strip().upper()
    if not x:
        return "HS"
    if x in {"ALL", "HS", "CYB", "BJ", "SH", "SZ"}:
        return x
    return "HS"


def _normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if "." in s:
        s = s.split(".", 1)[0]
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def detect_exchange(symbol: str) -> Optional[str]:
    s = _normalize_symbol(symbol)
    if not s:
        return None
    # 北交所新老代码段：92/4/8 开头
    if s.startswith("92") or s.startswith(("4", "8")):
        return "BJ"
    if s.startswith(("5", "6", "9")):
        return "SH"
    if s.startswith(("0", "1", "2", "3")):
        return "SZ"
    return None


def is_cyb_symbol(symbol: str) -> bool:
    s = _normalize_symbol(symbol)
    return s.startswith(("300", "301"))


def matches_exchange_group(symbol: str, exchange_group: Optional[str]) -> bool:
    g = normalize_exchange_group(exchange_group)
    if g == "ALL":
        return True
    ex = detect_exchange(symbol)
    if g in {"SH", "SZ", "BJ"}:
        return ex == g
    if g == "CYB":
        return is_cyb_symbol(symbol)
    if g == "HS":
        # 沪深合并口径：上交所 + 深交所（不含创业板）
        return ex in {"SH", "SZ"} and (not is_cyb_symbol(symbol))
    return True

