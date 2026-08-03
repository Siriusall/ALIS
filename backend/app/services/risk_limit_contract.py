"""Versioned position-limit contract shared by research and execution paths."""

from __future__ import annotations

import hashlib
import json
from typing import Any


RISK_LIMIT_CONTRACT_SCHEMA_VERSION = "alis_risk_limit_contract_v1"
DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT = 0.15
DEFAULT_INDUSTRY_LIMIT = 0.25
DEFAULT_CORRELATED_INDUSTRY_LIMIT = 0.35


def _number(value: Any, default: float = 1.0) -> float:
    try:
        result = float(value)
        if result != result:
            return default
        return max(0.0, min(1.0, result))
    except (TypeError, ValueError):
        return default


def build_risk_limit_contract(
    symbols: list[str] | set[str] | tuple[str, ...],
    *,
    defensive_symbols: set[str] | list[str] | tuple[str, ...] | None = None,
    symbol_overrides: dict[str, float] | None = None,
    dynamic_caps: dict[str, float] | None = None,
    global_single_asset_limit: float = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    industry_limit: float = DEFAULT_INDUSTRY_LIMIT,
    correlated_industry_limit: float = DEFAULT_CORRELATED_INDUSTRY_LIMIT,
) -> dict[str, Any]:
    """Build one tightening-only limit source for a new decision.

    Asset-class defaults intentionally equal the current 15% hard line. Any
    strategy or market-state cap may only tighten the result, never loosen it.
    """
    defensive = {str(value or "") for value in (defensive_symbols or []) if str(value or "")}
    overrides = {str(key): _number(value, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT) for key, value in (symbol_overrides or {}).items()}
    dynamic = {str(key): _number(value, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT) for key, value in (dynamic_caps or {}).items()}
    global_limit = _number(global_single_asset_limit, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)
    asset_limits = {
        "stock": global_limit,
        "etf": global_limit,
        "defensive_etf": global_limit,
    }
    items: dict[str, dict[str, Any]] = {}
    for raw_symbol in sorted({str(value or "") for value in symbols if str(value or "")}):
        asset_class = "defensive_etf" if raw_symbol in defensive else "etf" if raw_symbol.startswith(("1", "5")) else "stock"
        asset_limit = _number(asset_limits.get(asset_class), global_limit)
        symbol_limit = _number(overrides.get(raw_symbol), global_limit)
        dynamic_limit = _number(dynamic.get(raw_symbol), global_limit)
        final_limit = min(global_limit, asset_limit, symbol_limit, dynamic_limit)
        items[raw_symbol] = {
            "global_limit": round(global_limit, 8),
            "asset_class": asset_class,
            "asset_class_limit": round(asset_limit, 8),
            "symbol_override_limit": round(symbol_limit, 8),
            "dynamic_limit": round(dynamic_limit, 8),
            "final_limit": round(final_limit, 8),
        }
    contract: dict[str, Any] = {
        "schema_version": RISK_LIMIT_CONTRACT_SCHEMA_VERSION,
        "policy": "final_limit=min(global_limit,asset_class_limit,symbol_override_limit,dynamic_limit)",
        "global_single_asset_limit": round(global_limit, 8),
        "asset_class_limits": {key: round(value, 8) for key, value in asset_limits.items()},
        "industry_limit": round(_number(industry_limit), 8),
        "correlated_industry_limit": round(_number(correlated_industry_limit), 8),
        "tightening_only": True,
        "items": items,
    }
    contract["contract_hash"] = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return contract


def risk_limit_for(contract: dict[str, Any] | None, symbol: str, default: float = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT) -> float:
    item = ((contract or {}).get("items") or {}).get(str(symbol or ""))
    if isinstance(item, dict):
        return _number(item.get("final_limit"), default)
    return _number(default, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)
