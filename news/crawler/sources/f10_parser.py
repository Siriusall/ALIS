from __future__ import annotations

import re
from typing import Any

from metrics import cagr, infer_cyclicality, ratio, safe_float, volatility_coefficient


MISSING = {"", "--", "-", "—", "－", "N/A", "不适用"}


def _parse_cn_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace("%", "")
    if text in MISSING:
        return None

    multiplier = 1.0
    if text.endswith("万亿"):
        multiplier = 1e12
        text = text[:-2]
    elif text.endswith("亿"):
        multiplier = 1e8
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 1e4
        text = text[:-1]

    value = safe_float(text)
    return value * multiplier if value is not None else None


def _split_row(line: str) -> list[str]:
    return [part.strip() for part in re.split(r"\t+|\s{2,}", line.strip()) if part.strip()]


def find_metric_values(text: str, metric_name: str) -> list[str]:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(metric_name):
            continue
        parts = _split_row(line)
        if parts and parts[0] == metric_name:
            return parts[1:]
        return _split_row(line[len(metric_name):])
    return []


def first_metric_number(text: str, metric_name: str) -> float | None:
    for item in find_metric_values(text, metric_name):
        value = _parse_cn_number(item)
        if value is not None:
            return value
    return None


def metric_series(text: str, metric_name: str, count: int = 5) -> list[float | None]:
    return [_parse_cn_number(item) for item in find_metric_values(text, metric_name)[:count]]


def _sum_metric_names(text: str, names: list[str]) -> float | None:
    values = [first_metric_number(text, name) for name in names]
    nums = [value for value in values if value is not None]
    return sum(nums) if nums else None


def parse_f10_finance(text: str) -> dict[str, Any]:
    revenue = first_metric_number(text, "营业总收入(元)")
    net_profit = first_metric_number(text, "归属净利润(元)")
    deduct_profit = first_metric_number(text, "扣非净利润(元)")
    ocf = first_metric_number(text, "经营活动产生的现金流量净额")
    capex = first_metric_number(text, "购建固定资产、无形资产和其他长期资产支付的现金")
    cash = first_metric_number(text, "货币资金")
    total_debt = first_metric_number(text, "负债合计")
    short_debt = first_metric_number(text, "短期借款")
    long_debt = first_metric_number(text, "长期借款")
    lease_debt = first_metric_number(text, "租赁负债")
    bond_debt = first_metric_number(text, "应付债券")
    interest_debt = _sum_metric_names(text, ["短期借款", "长期借款", "租赁负债", "应付债券"])
    if interest_debt == 0:
        interest_debt = None
    treasury_stock = first_metric_number(text, "减:库存股")

    revenue_series = [v for v in metric_series(text, "营业总收入(元)", 8) if v is not None]
    profit_series = [v for v in metric_series(text, "扣非净利润(元)", 8) if v is not None]
    profit_yoy_vals = [v for v in metric_series(text, "扣非净利润同比增长(%)", 8) if v is not None]

    return {
        "revenue_yoy": first_metric_number(text, "营业总收入同比增长(%)"),
        "deduct_profit_yoy": first_metric_number(text, "扣非净利润同比增长(%)"),
        "cagr_3y": cagr(revenue_series, 3),
        "gross_margin": first_metric_number(text, "毛利率(%)"),
        "net_margin": first_metric_number(text, "净利率(%)"),
        "roe": first_metric_number(text, "净资产收益率(加权)(%)"),
        "roic": first_metric_number(text, "总资产收益率(加权)(%)"),
        "ocf_to_profit": ratio(ocf, net_profit) or first_metric_number(text, "经营净现金流/营业收入"),
        "free_cashflow": (ocf - capex) if ocf is not None and capex is not None else None,
        "debt_ratio": first_metric_number(text, "资产负债率(%)"),
        "interest_debt": interest_debt,
        "cash_coverage": ratio(cash, interest_debt or total_debt),
        "payout_ratio": ratio(first_metric_number(text, "分配股利、利润或偿付利息支付的现金"), net_profit),
        "buyback_amount": treasury_stock,
        "profit_volatility_5y": volatility_coefficient(profit_series[:5]),
        "cyclicality": infer_cyclicality(volatility_coefficient(profit_series[:5])),
        "revenue_series": revenue_series,
        "profit_yoy_series": profit_yoy_vals,
        "_debug": {
            "revenue": revenue,
            "deduct_profit": deduct_profit,
            "short_debt": short_debt,
            "long_debt": long_debt,
            "lease_debt": lease_debt,
            "bond_debt": bond_debt,
        },
    }


def _first_percent_after(text: str, anchor: str) -> float | None:
    index = text.find(anchor)
    if index < 0:
        return None
    snippet = text[index:index + 500]
    match = re.search(r"(-?\d+(?:\.\d+)?)%", snippet)
    return safe_float(match.group(1)) if match else None


def parse_f10_holder(text: str) -> dict[str, Any]:
    fund_holding = None
    for line in text.splitlines():
        if line.strip().startswith("基金\t"):
            parts = _split_row(line)
            if len(parts) >= 6:
                fund_holding = _parse_cn_number(parts[-2])
            break

    etf_holding = 0.0
    saw_etf = False
    for line in text.splitlines():
        if "ETF" not in line:
            continue
        parts = _split_row(line)
        if len(parts) >= 8:
            value = _parse_cn_number(parts[-2])
            if value is not None:
                etf_holding += value
                saw_etf = True

    northbound = None
    for line in text.splitlines():
        if "香港中央结算有限公司" not in line:
            continue
        parts = _split_row(line)
        for part in reversed(parts):
            if part.endswith("%"):
                northbound = _parse_cn_number(part)
                break
        if northbound is not None:
            break

    return {
        "fund_holding": fund_holding,
        "northbound": northbound or _first_percent_after(text, "沪深港通持股"),
        "etf_holding": etf_holding if saw_etf else None,
        "research_count": int(first_metric_number(text, "机构调研次数") or 0) or None,
    }


def parse_quote_header(text: str) -> dict[str, Any]:
    pe = _number_after_any(text, ["市盈率TTM", "市盈率", "市盈", "PE(动)", "PE", "甯傜泩鐜", "甯傜泩"])
    if pe is None:
        pe = _first_regex_number(text, [
            r"PE\s*TTM[^\d-]{0,40}(-?\d+(?:\.\d+)?)",
            r"TTM[^\d-]{0,40}(-?\d+(?:\.\d+)?)",
            r"市盈率[^\d-]{0,40}(-?\d+(?:\.\d+)?)",
        ])

    return {
        "pe": _bounded(pe, -500, 5000),
        "pb": _bounded(_number_after_metric_label(text, ["市净率", "市净", "PB", "甯傚噣鐜囷細", "甯傚噣鐜"]), 0, 200),
        "ps": _bounded(_number_after_metric_label(text, ["市销率", "市销", "PS", "甯傞攢鐜囷細", "甯傞攢鐜"]), 0, 200),
        "dividend_yield": _bounded(_number_after_metric_label(text, ["股息率", "股息率TTM", "股息", "DY", "鑲℃伅鐜囷細", "鑲℃伅鐜"]), 0, 100),
        "valuation_percentile": None,
    }


def _number_after_any(text: str, labels: list[str], window: int = 100) -> float | None:
    for label in labels:
        start = text.find(label)
        if start < 0:
            continue
        snippet = text[start + len(label): start + len(label) + window]
        match = re.search(r"-?\d+(?:\.\d+)?", snippet)
        if match:
            return safe_float(match.group(0))
    return None


def _number_after_metric_label(text: str, labels: list[str], window: int = 40) -> float | None:
    for label in labels:
        search_from = 0
        while True:
            start = text.find(label, search_from)
            if start < 0:
                break
            snippet = text[start + len(label): start + len(label) + window]
            match = re.search(r"[:：\s\t锛歕囷細-]{0,20}(-?\d+\.\d+|-?\d+)", snippet)
            if match:
                return safe_float(match.group(1))
            search_from = start + len(label)
    return None


def _first_regex_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.S)
        if match:
            return safe_float(match.group(1))
    return None


def _bounded(value: float | None, low: float, high: float) -> float | None:
    if value is None:
        return None
    if value < low or value > high:
        return None
    return value
