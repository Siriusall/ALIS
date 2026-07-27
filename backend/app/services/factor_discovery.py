from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from app.db.store import _conn
from app.services.factor_dsl import (
    DSL_SCHEMA_VERSION,
    expression_complexity,
    expression_hash,
    expression_inputs,
    expression_max_window,
    validate_expression,
)


DISCOVERY_SCHEMA_VERSION = "alis_open_factor_discovery_v1"


def field(name: str) -> dict[str, Any]:
    return {"op": "field", "name": name}


def op(name: str, *args: dict[str, Any], window: int | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"op": name, "args": list(args)}
    if window is not None:
        payload["window"] = window
    payload.update(extra)
    return payload


CLASSIC_ALPHA_SEEDS: tuple[dict[str, Any], ...] = (
    {"key": "alpha_price_volume_corr_10", "name_cn": "价格成交量相关反转10日", "category": "经典量价",
     "expression": op("neg", op("ts_corr", field("close"), field("volume"), window=10)), "direction": 1},
    {"key": "alpha_open_close_reversal_5", "name_cn": "开收盘偏离反转5日", "category": "经典量价",
     "expression": op("neg", op("ts_mean", op("div", op("sub", field("close"), field("open")), field("open")), window=5)), "direction": 1},
    {"key": "alpha_intraday_position_10", "name_cn": "日内收盘位置10日", "category": "经典量价",
     "expression": op("ts_mean", op("div", op("sub", field("close"), field("low")), op("sub", field("high"), field("low"))), window=10), "direction": 1},
    {"key": "alpha_volume_shock_reversal_20", "name_cn": "量能冲击反转20日", "category": "经典量价",
     "expression": op("neg", op("mul", op("delta", field("close"), window=5), op("div", field("volume"), op("ts_mean", field("volume"), window=20)))), "direction": 1},
    {"key": "alpha_high_low_range_10", "name_cn": "振幅收缩10日", "category": "波动",
     "expression": op("neg", op("ts_mean", op("div", op("sub", field("high"), field("low")), field("close")), window=10)), "direction": 1},
    {"key": "alpha_return_volatility_20", "name_cn": "收益波动惩罚20日", "category": "波动",
     "expression": op("neg", op("ts_std", field("returns"), window=20)), "direction": 1},
    {"key": "alpha_short_reversal_5", "name_cn": "短期反转5日", "category": "反转",
     "expression": op("neg", op("returns", field("close"), window=5)), "direction": 1},
    {"key": "alpha_medium_momentum_60", "name_cn": "中期动量60日", "category": "动量",
     "expression": op("returns", field("close"), window=60), "direction": 1},
    {"key": "alpha_momentum_acceleration", "name_cn": "动量加速度", "category": "动量",
     "expression": op("sub", op("returns", field("close"), window=20), op("returns", field("close"), window=60)), "direction": 1},
    {"key": "alpha_turnover_stability_20", "name_cn": "成交额稳定性20日", "category": "流动性",
     "expression": op("neg", op("div", op("ts_std", field("amount"), window=20), op("ts_mean", field("amount"), window=20))), "direction": 1},
    {"key": "alpha_amihud_20", "name_cn": "Amihud流动性20日", "category": "流动性",
     "expression": op("neg", op("ts_mean", op("div", op("abs", field("returns")), field("amount")), window=20)), "direction": 1},
    {"key": "alpha_earnings_yield", "name_cn": "盈利收益率开放配方", "category": "价值",
     "expression": op("div", {"op": "constant", "value": 1.0, "args": []}, field("pe_ttm")), "direction": 1},
    {"key": "alpha_book_to_price", "name_cn": "账面市值比开放配方", "category": "价值",
     "expression": op("div", {"op": "constant", "value": 1.0, "args": []}, field("pb")), "direction": 1},
    {"key": "alpha_value_lowvol_interaction", "name_cn": "价值低波交互", "category": "交互",
     "expression": op("mul", op("div", {"op": "constant", "value": 1.0, "args": []}, field("pe_ttm")), op("neg", op("ts_std", field("returns"), window=20))), "direction": 1},
    {"key": "alpha_size_reversal", "name_cn": "规模反转交互", "category": "交互",
     "expression": op("mul", op("neg", op("log", field("float_mv"))), op("neg", op("returns", field("close"), window=5))), "direction": 1},
)


def _bounded_search_space() -> tuple[dict[str, Any], ...]:
    candidates: list[dict[str, Any]] = []
    for window in (3, 5, 10, 20, 40, 60, 120):
        candidates.extend((
            {
                "key": f"dsl_return_{window}",
                "name_cn": f"{window}日收益",
                "category": "自动搜索-动量",
                "expression": op("returns", field("close"), window=window),
                "direction": 1,
            },
            {
                "key": f"dsl_reversal_{window}",
                "name_cn": f"{window}日反转",
                "category": "自动搜索-反转",
                "expression": op("neg", op("returns", field("close"), window=window)),
                "direction": 1,
            },
            {
                "key": f"dsl_price_volume_corr_{window}",
                "name_cn": f"{window}日价量相关",
                "category": "自动搜索-量价",
                "expression": op("ts_corr", field("close"), field("volume"), window=window),
                "direction": 1,
            },
            {
                "key": f"dsl_range_contraction_{window}",
                "name_cn": f"{window}日振幅收缩",
                "category": "自动搜索-波动",
                "expression": op(
                    "neg",
                    op("ts_mean", op("div", op("sub", field("high"), field("low")), field("close")), window=window),
                ),
                "direction": 1,
            },
        ))
    return tuple(candidates)


BOUNDED_DSL_SEARCH_SPACE = _bounded_search_space()


def register_open_factor_candidates(
    specs: tuple[dict[str, Any], ...] = CLASSIC_ALPHA_SEEDS,
    *,
    source: str = "classic_alpha_seed_pack",
) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat()
    created: list[str] = []
    reused: list[str] = []
    rejected: list[dict[str, str]] = []
    with _conn() as conn:
        for spec in specs:
            try:
                expression = spec["expression"]
                validate_expression(expression)
                digest = expression_hash(expression)
            except Exception as exc:
                rejected.append({"factor_key": str(spec.get("key") or ""), "reason": str(exc)})
                continue
            key = str(spec["key"])
            version = f"dsl-{digest[:12]}"
            formula = {
                "engine": DSL_SCHEMA_VERSION,
                "expression": expression,
                "expression_hash": digest,
                "complexity": expression_complexity(expression),
                "source": source,
                "discovery_schema": DISCOVERY_SCHEMA_VERSION,
            }
            existing = conn.execute(
                "SELECT id FROM factor_definitions WHERE factor_key=? AND version=?",
                (key, version),
            ).fetchone()
            if existing:
                reused.append(key)
                continue
            conn.execute(
                """INSERT INTO factor_definitions(
                   factor_key,version,status,name_cn,category,formula_json,inputs_json,
                   direction,window_sessions,available_time_rule,missing_policy,
                   leakage_status,created_at,updated_at
                   ) VALUES(?,?,'candidate',?,?,?,?,?,?,?,
                   'cross_section_median_then_zero_if_unavailable','passed',?,?)""",
                (
                    key, version, str(spec["name_cn"]), str(spec["category"]),
                    json.dumps(formula, ensure_ascii=False),
                    json.dumps(expression_inputs(expression), ensure_ascii=False),
                    int(spec.get("direction") or 1), expression_max_window(expression),
                    "T_close_available_by_T_15_00", now, now,
                ),
            )
            created.append(key)
    return {
        "ok": True,
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "source": source,
        "created": created,
        "reused": reused,
        "rejected": rejected,
        "candidate_count": len(created) + len(reused),
    }


def discover_bounded_dsl_candidates(*, limit: int = 5) -> dict[str, Any]:
    """Register the next deterministic, previously unseen DSL recipes."""
    bounded_limit = max(1, min(int(limit or 5), 20))
    with _conn() as conn:
        existing = {
            str(row["factor_key"])
            for row in conn.execute(
                "SELECT factor_key FROM factor_definitions WHERE factor_key LIKE ?",
                ("dsl_%",),
            ).fetchall()
        }
    pending = tuple(
        spec for spec in BOUNDED_DSL_SEARCH_SPACE
        if str(spec["key"]) not in existing
    )[:bounded_limit]
    result = register_open_factor_candidates(
        pending,
        source="bounded_symbolic_search_v1",
    ) if pending else {
        "ok": True,
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "source": "bounded_symbolic_search_v1",
        "created": [],
        "reused": [],
        "rejected": [],
        "candidate_count": 0,
    }
    result["search_space_size"] = len(BOUNDED_DSL_SEARCH_SPACE)
    result["remaining"] = max(0, len(BOUNDED_DSL_SEARCH_SPACE) - len(existing) - len(pending))
    result["status"] = "generated" if pending else "search_space_exhausted"
    return result


def run_open_factor_discovery_cycle(*, search_limit: int = 5) -> dict[str, Any]:
    classic = register_open_factor_candidates()
    symbolic = discover_bounded_dsl_candidates(limit=search_limit)
    return {
        "ok": True,
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "status": "completed",
        "classic_pack": classic,
        "symbolic_search": symbolic,
        "created_count": len(classic["created"]) + len(symbolic["created"]),
    }
