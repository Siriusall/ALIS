from __future__ import annotations

from typing import Any

from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE
from app.services.factor_engine import FACTOR_REGISTRY, FACTORS_NEED_HISTORY, SNAPSHOT_ONLY_FACTORS
from app.services.model_training import FEATURES as MODEL_FEATURES
from app.services.strategy_config import BACKTEST_SUPPORTED_FACTORS
from app.services.factor_platform import factor_registry_status

_FACTOR_META: dict[str, dict[str, str]] = {
    "volume": {
        "name_cn": "成交量",
        "category": "量价",
        "description": "当日成交量，反映交易活跃度。",
        "formula_hint": "snapshot.volume",
    },
    "turnover": {
        "name_cn": "成交额",
        "category": "量价",
        "description": "当日成交金额，常用于流动性过滤。",
        "formula_hint": "snapshot.turnover",
    },
    "turnover_rate": {
        "name_cn": "换手率",
        "category": "量价",
        "description": "TuShare daily_basic 换手率，反映筹码交换速度。",
        "formula_hint": "daily_basic.turnover_rate",
    },
    "change_pct": {
        "name_cn": "涨跌幅",
        "category": "量价",
        "description": "当日涨跌幅；回测打分时取绝对值，偏好波动但避免方向偏置。",
        "formula_hint": "abs(snapshot.change_pct)",
    },
    "open": {
        "name_cn": "开盘价",
        "category": "行情",
        "description": "当日开盘价，主要用于执行与缓存截面。",
        "formula_hint": "snapshot.open",
    },
    "high": {
        "name_cn": "最高价",
        "category": "行情",
        "description": "当日最高价。",
        "formula_hint": "snapshot.high",
    },
    "low": {
        "name_cn": "最低价",
        "category": "行情",
        "description": "当日最低价。",
        "formula_hint": "snapshot.low",
    },
    "close": {
        "name_cn": "收盘价",
        "category": "行情",
        "description": "当日收盘价，收益与估值计算的基础价格。",
        "formula_hint": "snapshot.close",
    },
    "price": {
        "name_cn": "成交价参考",
        "category": "行情",
        "description": "all-in-one 合并后的优先成交价字段，通常等同收盘价。",
        "formula_hint": "snapshot.price | close",
    },
    "momentum_5": {
        "name_cn": "5日动量",
        "category": "动量",
        "description": "近 5 个交易日价格动量，衡量短期趋势强度。",
        "formula_hint": "close / close[-5] - 1",
    },
    "momentum_20": {
        "name_cn": "20日动量",
        "category": "动量",
        "description": "近 20 个交易日价格动量，衡量中期趋势。",
        "formula_hint": "close / close[-20] - 1",
    },
    "volatility_20": {
        "name_cn": "20日波动率",
        "category": "风险",
        "description": "近 20 日收益率标准差，衡量价格波动风险。",
        "formula_hint": "std(daily_return[-20:])",
    },
    "volume_ratio_5": {
        "name_cn": "5日量比",
        "category": "量价",
        "description": "当日成交量相对近 5 日均量的倍数。",
        "formula_hint": "volume / avg(volume[-5:])",
    },
    "float_mv": {
        "name_cn": "流通市值",
        "category": "估值",
        "description": "流通市值，用于市值风格与流动性约束。",
        "formula_hint": "daily_basic.float_mv",
    },
    "total_mv": {
        "name_cn": "总市值",
        "category": "估值",
        "description": "总市值，用于大小盘风格划分。",
        "formula_hint": "daily_basic.total_mv",
    },
    "pe_ttm": {
        "name_cn": "市盈率TTM",
        "category": "估值",
        "description": "滚动市盈率，衡量估值高低。",
        "formula_hint": "daily_basic.pe_ttm",
    },
    "pb": {
        "name_cn": "市净率",
        "category": "估值",
        "description": "市净率，衡量资产估值水平。",
        "formula_hint": "daily_basic.pb",
    },
    "is_st": {
        "name_cn": "是否ST",
        "category": "交易约束",
        "description": "ST/ST* 标记，买入侧通常拦截。",
        "formula_hint": "name.startswith('ST')",
    },
    "is_suspended": {
        "name_cn": "是否停牌",
        "category": "交易约束",
        "description": "成交量为 0 或停牌标记，不可成交。",
        "formula_hint": "volume <= 0",
    },
    "is_limit_up": {
        "name_cn": "是否涨停",
        "category": "交易约束",
        "description": "涨停状态，买入侧通常拦截。",
        "formula_hint": "change_pct >= 9.7%",
    },
    "is_limit_down": {
        "name_cn": "是否跌停",
        "category": "交易约束",
        "description": "跌停状态，卖出侧可能拦截。",
        "formula_hint": "change_pct <= -9.7%",
    },
    "in_hs300": {
        "name_cn": "沪深300成分",
        "category": "指数成分",
        "description": "是否属于沪深300成分股，用于指数增强或基准池约束。",
        "formula_hint": "index_membership('000300.SH')",
    },
    "in_csi500": {
        "name_cn": "中证500成分",
        "category": "指数成分",
        "description": "是否属于中证500成分股。",
        "formula_hint": "index_membership('000905.SH')",
    },
}


def _all_factor_keys() -> list[str]:
    keys = set(FACTOR_REGISTRY.keys())
    keys |= set(BACKTEST_SUPPORTED_FACTORS)
    keys |= set(FACTOR_KEYS_FOR_COVERAGE)
    keys |= set(MODEL_FEATURES)
    category_order = ["量价", "动量", "风险", "估值", "行情", "交易约束", "指数成分", "其他"]
    order_index = {c: i for i, c in enumerate(category_order)}

    def sort_key(k: str) -> tuple[int, str]:
        cat = _FACTOR_META.get(k, {}).get("category", "其他")
        return (order_index.get(cat, 99), k)

    return sorted(keys, key=sort_key)


def list_registered_factors() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in _all_factor_keys():
        meta = _FACTOR_META.get(key, {})
        items.append(
            {
                "key": key,
                "name_cn": meta.get("name_cn", key),
                "category": meta.get("category", "其他"),
                "description": meta.get("description", ""),
                "formula_hint": meta.get("formula_hint", ""),
                "in_factor_engine": key in FACTOR_REGISTRY,
                "in_backtest": key in BACKTEST_SUPPORTED_FACTORS,
                "in_model_training": key in MODEL_FEATURES,
                "in_cache_coverage": key in FACTOR_KEYS_FOR_COVERAGE,
                "needs_history": key in FACTORS_NEED_HISTORY,
                "snapshot_only": key in SNAPSHOT_ONLY_FACTORS,
            }
        )
    return items


def factor_library_summary() -> dict[str, Any]:
    factors = list_registered_factors()
    categories: dict[str, int] = {}
    for f in factors:
        cat = str(f.get("category") or "其他")
        categories[cat] = categories.get(cat, 0) + 1
    try:
        versioned = factor_registry_status()
    except Exception:
        versioned = {
            "schema_version": "alis_factor_registry_v1",
            "active_count": 0,
            "latest_materialized_date": "",
            "value_count": 0,
            "evaluation_count": 0,
            "definitions": [],
            "consumer_contract": {},
        }
    version_by_key = {
        str(item.get("factor_key") or ""): item
        for item in versioned.get("definitions") or []
    }
    for factor in factors:
        definition = version_by_key.get(str(factor["key"]))
        factor.update({
            "definition_id": int(definition.get("id") or 0) if definition else None,
            "version": str(definition.get("version") or "") if definition else "",
            "status": str(definition.get("status") or "legacy_unversioned") if definition else "legacy_unversioned",
            "available_time_rule": str(definition.get("available_time_rule") or "") if definition else "",
            "missing_policy": str(definition.get("missing_policy") or "") if definition else "",
            "leakage_status": str(definition.get("leakage_status") or "unknown") if definition else "unknown",
        })
    return {
        "total": len(factors),
        "categories": categories,
        "model_training_features": list(MODEL_FEATURES),
        "factors": factors,
        "versioned_registry": versioned,
    }
