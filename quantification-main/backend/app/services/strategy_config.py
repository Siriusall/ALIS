"""
策略配置解析：将 DB 中的 strategy content（JSON）解析为统一的内部结构。
兼容旧版简单权重配置，支持新版 universe / filters / score / rebalance。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ParsedStrategyConfig:
    """解析后的策略配置（面向日线多因子）。"""
    universe: Dict[str, Any]  # e.g. {"index": "000300", "exclude_st": true}
    filters: List[Dict[str, Any]]  # [{"factor": "turnover_avg_20", "op": ">", "value": 1e7}]
    score: Dict[str, float]  # factor_name -> weight，已归一化
    rebalance: Dict[str, Any]  # {"freq": "daily"|"weekly"|"monthly", "top_n": 20}
    # 原始 content 中若存在 lookback_days，用于日线因子拉取窗口
    lookback_days: int = 60
    # 持仓天数，到期自动卖出；None 表示不按持仓日自动卖
    hold_days: Optional[int] = None


# 预设策略：名称 -> 完整 content 字典（新格式）
PRESET_STRATEGIES: Dict[str, Dict[str, Any]] = {
    "default_momentum": {
        "universe": {},
        "filters": [],
        "score": {"factors": {"momentum_20": 0.5, "volume_ratio_5": 0.3, "change_pct": 0.2}},
        "rebalance": {"freq": "daily", "top_n": 20},
        "lookback_days": 60,
    },
    "low_volatility": {
        "universe": {},
        "filters": [],
        "score": {"factors": {"volatility_20": -0.5, "volume": 0.3, "turnover": 0.2}},
        "rebalance": {"freq": "weekly", "top_n": 15},
        "lookback_days": 60,
    },
}

# 回测引擎当前支持的因子（需与 backtest_engine._factor_value 保持一致）
BACKTEST_SUPPORTED_FACTORS: set[str] = {
    "volume",
    "turnover",
    "change_pct",
    "momentum_5",
    "momentum_20",
    "volatility_20",
    "volume_ratio_5",
    "float_mv",
    "total_mv",
    "pe_ttm",
    "pb",
    "is_st",
    "is_limit_up",
    "is_limit_down",
    "is_suspended",
}


def _is_legacy_weights_only(content: Any) -> bool:
    """判断是否为旧版仅权重的 content（只有 volume/turnover/change_pct 数字键）。"""
    if not isinstance(content, dict):
        return False
    allowed = {"volume", "turnover", "change_pct"}
    keys = set(content.keys())
    if not keys or keys - allowed:
        return False
    return all(isinstance(content.get(k), (int, float)) for k in keys)


def _normalize_score_factors(score: Dict[str, Any]) -> Dict[str, float]:
    """从 score 配置中提取 factors 并归一化权重。"""
    factors = score.get("factors")
    if not isinstance(factors, dict):
        return {}
    out = {k: float(v) for k, v in factors.items() if isinstance(v, (int, float))}
    total = sum(out.values())
    if total <= 0:
        return out
    return {k: v / total for k, v in out.items()}


def parse_strategy_content(
    content: Any,
    strategy_type: str,
) -> ParsedStrategyConfig:
    """
    将策略 content 解析为 ParsedStrategyConfig。
    - content: 来自 DB 的 strategies.content（可能为 dict 或 JSON 字符串已解析为 dict）。
    - strategy_type: "formula" | "code" | "preset"。
    - 旧版仅权重 content 会升级为默认 universe/filters/rebalance + score.factors。
    - preset 类型且 content 为字符串时，按预设名查找完整配置再解析。
    """
    # preset：content 可能为预设名称
    if strategy_type == "preset" and isinstance(content, str):
        preset = PRESET_STRATEGIES.get(content.strip())
        if preset is not None:
            content = preset
        else:
            content = {}

    if not isinstance(content, dict):
        content = {}

    # 旧版仅权重
    if _is_legacy_weights_only(content):
        factors = {
            "volume": float(content.get("volume", 0.4)),
            "turnover": float(content.get("turnover", 0.4)),
            "change_pct": float(content.get("change_pct", 0.2)),
        }
        total = sum(factors.values()) or 1.0
        score = {k: v / total for k, v in factors.items()}
        return ParsedStrategyConfig(
            universe={},
            filters=[],
            score=score,
            rebalance={"freq": "daily", "top_n": 20},
            lookback_days=60,
            hold_days=None,
        )

    # 新格式
    universe = content.get("universe")
    if not isinstance(universe, dict):
        universe = {}

    filters = content.get("filters")
    if not isinstance(filters, list):
        filters = []

    score_cfg = content.get("score")
    if not isinstance(score_cfg, dict):
        score_cfg = {}
    score = _normalize_score_factors(score_cfg)
    if not score:
        score = {"volume": 0.4, "turnover": 0.4, "change_pct": 0.2}

    rebalance_cfg = content.get("rebalance")
    if not isinstance(rebalance_cfg, dict):
        rebalance_cfg = {}
    rebalance = {
        "freq": rebalance_cfg.get("freq", "daily"),
        "top_n": int(rebalance_cfg.get("top_n", 20)),
    }

    lookback_days = 60
    if isinstance(content.get("lookback_days"), (int, float)):
        lookback_days = max(5, min(250, int(content["lookback_days"])))

    hold_days: Optional[int] = None
    for key in ("hold_days", "holding_days"):
        raw = rebalance_cfg.get(key)
        if isinstance(raw, (int, float)) and int(raw) > 0:
            hold_days = int(raw)
            break
        if isinstance(raw, str) and raw.strip().isdigit() and int(raw.strip()) > 0:
            hold_days = int(raw.strip())
            break
    if hold_days is None and isinstance(content.get("hold_days"), (int, float)) and int(content["hold_days"]) > 0:
        hold_days = int(content["hold_days"])
    if hold_days is None and isinstance(content.get("holding_days"), (int, float)) and int(content["holding_days"]) > 0:
        hold_days = int(content["holding_days"])

    return ParsedStrategyConfig(
        universe=universe,
        filters=filters,
        score=score,
        rebalance=rebalance,
        lookback_days=lookback_days,
        hold_days=hold_days,
    )


def get_strategy_factor_names(content: Any, strategy_type: str) -> set[str]:
    """提取策略中出现的所有因子名（filters + score.factors）。"""
    cfg = parse_strategy_content(content, strategy_type)
    out: set[str] = set(cfg.score.keys())
    for f in cfg.filters:
        fn = str(f.get("factor", "")).strip()
        if fn:
            out.add(fn)
    return out


def get_unsupported_backtest_factors(content: Any, strategy_type: str) -> list[str]:
    """返回策略中不被回测引擎支持的因子名（排序后）。"""
    used = get_strategy_factor_names(content, strategy_type)
    return sorted([x for x in used if x not in BACKTEST_SUPPORTED_FACTORS])
