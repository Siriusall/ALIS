from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "trend_momentum_v1": {
        "name": "趋势动量模板",
        "description": "趋势偏强时，优先选择20日动量、量比和成交额更活跃的股票。",
        "score": {"momentum_20": 0.5, "volume_ratio_5": 0.3, "turnover": 0.2},
        "filters": [
            {"factor": "is_suspended", "op": "==", "value": 0},
            {"factor": "is_st", "op": "==", "value": 0},
        ],
        "risk_budget": {"target_position_pct": 0.6, "max_single_position_pct": 0.08},
    },
    "sideways_low_vol_v1": {
        "name": "震荡低波模板",
        "description": "震荡市降低追涨权重，偏向低波动、适中市值和相对便宜的标的。",
        "score": {"volatility_20": -0.45, "float_mv": 0.25, "pb": -0.2, "turnover": 0.1},
        "filters": [
            {"factor": "is_suspended", "op": "==", "value": 0},
            {"factor": "is_st", "op": "==", "value": 0},
        ],
        "risk_budget": {"target_position_pct": 0.45, "max_single_position_pct": 0.06},
    },
    "defensive_low_vol_v1": {
        "name": "防守低波模板",
        "description": "趋势偏弱或风险升高时，降低目标仓位，只保留流动性和低波动筛选。",
        "score": {"volatility_20": -0.6, "turnover": 0.25, "float_mv": 0.15},
        "filters": [
            {"factor": "is_suspended", "op": "==", "value": 0},
            {"factor": "is_st", "op": "==", "value": 0},
        ],
        "risk_budget": {"target_position_pct": 0.3, "max_single_position_pct": 0.05},
    },
}


def recommend_strategy_template(trend_analysis: Dict[str, Any], *, requested_top_n: int) -> Dict[str, Any]:
    trend = str((trend_analysis or {}).get("trend") or "sideways")
    risk_level = str((trend_analysis or {}).get("risk_level") or "normal")
    if trend == "uptrend" and risk_level == "normal":
        template_id = "trend_momentum_v1"
        top_n = int(requested_top_n)
    elif trend in {"downtrend", "high_risk"} or risk_level == "high":
        template_id = "defensive_low_vol_v1"
        top_n = max(3, min(int(requested_top_n), 8))
    else:
        template_id = "sideways_low_vol_v1"
        top_n = max(5, min(int(requested_top_n), 12))

    template = deepcopy(_TEMPLATES[template_id])
    template["id"] = template_id
    template["trend"] = trend
    template["risk_level"] = risk_level
    template["top_n"] = top_n
    template["factors"] = list(template["score"].keys())
    template["reason"] = (
        f"{trend_analysis.get('reason') or '趋势信号不明显。'}"
        f" 采用{template['name']}，目标持仓数 {top_n}。"
    )
    return template
