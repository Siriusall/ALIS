"""
A股市场状态体系 - 12状态完整版

覆盖A股所有典型场景：全面牛熊、结构牛熊、震荡、流动性、政策、主题、
风险收缩、恐慌、修复、阴跌、风格分化、估值回归
"""

from __future__ import annotations

from typing import Any

MARKET_STATES = {
    "S0": {
        "label": "全面牛市",
        "desc": "指数趋势向上、宽度高、放量、情绪亢奋、赚钱效应强、资金持续流入",
        "risk_budget": 1.00,
        "first_pct": 1.00,
        "margin": 1.00,
        "narrative": 1.10,
        "max_equity": 0.95,
    },
    "S1": {
        "label": "结构性牛市",
        "desc": "指数涨但宽度分化、主线清晰、龙头领涨、非普涨",
        "risk_budget": 0.90,
        "first_pct": 0.85,
        "margin": 1.05,
        "narrative": 1.05,
        "max_equity": 0.85,
    },
    "S2": {
        "label": "流动性驱动",
        "desc": "政策宽松、资金充裕、估值扩张、题材活跃、基本面未跟上",
        "risk_budget": 0.85,
        "first_pct": 0.80,
        "margin": 1.10,
        "narrative": 1.15,
        "max_equity": 0.80,
    },
    "S3": {
        "label": "政策驱动",
        "desc": "重大政策出台、板块轮动快、预期先行、游资活跃",
        "risk_budget": 0.80,
        "first_pct": 0.75,
        "margin": 1.15,
        "narrative": 1.20,
        "max_equity": 0.75,
    },
    "S4": {
        "label": "主题炒作",
        "desc": "指数窄幅、妖股频出、题材轮动快、基本面股滞涨、游资主导",
        "risk_budget": 0.65,
        "first_pct": 0.55,
        "margin": 1.20,
        "narrative": 1.30,
        "max_equity": 0.55,
    },
    "S5": {
        "label": "风格分化",
        "desc": "大小盘/成长价值严重分化、一方涨一方跌、赚指数不赚钱",
        "risk_budget": 0.70,
        "first_pct": 0.60,
        "margin": 1.15,
        "narrative": 0.85,
        "max_equity": 0.60,
    },
    "S6": {
        "label": "震荡市",
        "desc": "指数横盘、缩量、无方向、板块轮动快、情绪中性",
        "risk_budget": 0.65,
        "first_pct": 0.60,
        "margin": 1.20,
        "narrative": 0.90,
        "max_equity": 0.60,
    },
    "S7": {
        "label": "估值回归",
        "desc": "高估值板块回调、低估值板块补涨、风格切换、防御品种走强",
        "risk_budget": 0.60,
        "first_pct": 0.50,
        "margin": 1.25,
        "narrative": 0.70,
        "max_equity": 0.50,
    },
    "S8": {
        "label": "风险收缩",
        "desc": "趋势走弱、波动上升、缩量、资金离场、避险升温",
        "risk_budget": 0.50,
        "first_pct": 0.45,
        "margin": 1.35,
        "narrative": 0.60,
        "max_equity": 0.45,
    },
    "S9": {
        "label": "阴跌磨底",
        "desc": "持续缩量阴跌、情绪低迷、无主线、资金持续流出",
        "risk_budget": 0.40,
        "first_pct": 0.35,
        "margin": 1.45,
        "narrative": 0.50,
        "max_equity": 0.35,
    },
    "S10": {
        "label": "恐慌/危机",
        "desc": "急跌放量、流动性枯竭、情绪冰点、踩踏、杠杆平仓",
        "risk_budget": 0.20,
        "first_pct": 0.15,
        "margin": 1.60,
        "narrative": 0.30,
        "max_equity": 0.15,
    },
    "S11": {
        "label": "危机修复",
        "desc": "急跌后反弹、波动仍高、情绪从冰点回升、政策托底",
        "risk_budget": 0.55,
        "first_pct": 0.35,
        "margin": 1.30,
        "narrative": 0.70,
        "max_equity": 0.50,
    },
}


def classify(
    *,
    trend: float,         # 趋势强度 -100~100
    breadth: float,        # 上涨比例 0~1
    vol: float,            # 波动率 0~1
    volume: float,         # 成交额相对50日均值 0~3
    sentiment: float,      # 情绪 0~100
    capital: float,        # 资金 -100~100
    val_pe: float,         # 全市场估值分位 0~1
    policy: float,         # 政策力度 0~100
    style_gap: float,      # 大小盘风格分化度 0~1
    prev: str = "S6",
) -> tuple[str, str, dict]:
    ev = {"trend": trend, "breadth": breadth, "vol": vol, "volume": volume,
          "sentiment": sentiment, "capital": capital, "val_pe": val_pe, "policy": policy, "style_gap": style_gap}

    # 恐慌
    if vol > 0.8 and breadth < 0.15 and sentiment < 15:
        return "S10", "恐慌/危机", ev
    if vol > 0.5 and breadth < 0.2 and sentiment < 20 and volume > 1.3:
        return "S10", "恐慌/危机", ev

    # 危机修复
    if vol > 0.4 and breadth > 0.5 and sentiment > 60 and volume > 1.2:
        return "S11", "危机修复", ev

    # 全面牛市
    if trend > 50 and breadth > 0.65 and volume > 1.2 and sentiment > 70 and capital > 20:
        return "S0", "全面牛市", ev
    if trend > 40 and breadth > 0.60 and volume > 1.1 and sentiment > 65:
        return "S0", "全面牛市", ev

    # 结构性牛市
    if trend > 20 and 0.35 < breadth < 0.60 and volume > 0.9 and sentiment > 55:
        return "S1", "结构性牛市", ev

    # 流动性驱动
    if policy > 70 and volume > 1.0 and sentiment > 45 and val_pe < 0.5:
        return "S2", "流动性驱动", ev

    # 政策驱动
    if policy > 60 and sentiment > 50 and breadth > 0.4:
        return "S3", "政策驱动", ev

    # 风格分化
    if style_gap > 0.7 and -20 < trend < 20:
        return "S5", "风格分化", ev

    # 主题炒作
    if -10 < trend < 15 and 0.25 < breadth < 0.45 and volume < 0.9 and sentiment < 45:
        return "S4", "主题炒作", ev

    # 震荡
    if -20 < trend < 20 and 0.3 < breadth < 0.55 and 0.3 < vol < 0.6:
        return "S6", "震荡市", ev

    # 估值回归
    if trend < 0 and val_pe > 0.7 and volume < 1.0:
        return "S7", "估值回归", ev

    # 风险收缩
    if trend < -20 and vol > 0.4 and volume < 0.9 and sentiment < 40:
        return "S8", "风险收缩", ev

    # 阴跌磨底
    if trend < -30 and vol < 0.4 and volume < 0.7 and sentiment < 30:
        return "S9", "阴跌磨底", ev

    return prev, MARKET_STATES.get(prev, {}).get("label", "震荡市"), ev


def params(state: str) -> dict:
    return MARKET_STATES.get(state, MARKET_STATES["S6"])


def list_states() -> list[dict]:
    return [{"id": k, "label": v["label"], "desc": v["desc"]} for k, v in sorted(MARKET_STATES.items())]
