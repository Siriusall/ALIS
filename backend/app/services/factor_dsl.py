from __future__ import annotations

import hashlib
import json
import math
from statistics import mean
from typing import Any

from app.services.backtest_data_source import HistBar, SnapshotRow


DSL_SCHEMA_VERSION = "alis_factor_dsl_v1"
MAX_EXPRESSION_DEPTH = 8
MAX_WINDOW_SESSIONS = 252

_FIELD_NAMES = {
    "open", "high", "low", "close", "volume", "amount", "returns",
    "turnover_rate", "float_mv", "total_mv", "pe_ttm", "pb",
}
_OPS = {
    "field", "constant", "add", "sub", "mul", "div", "neg", "abs", "sign",
    "log", "pow", "delay", "delta", "returns", "ts_mean", "ts_std",
    "ts_min", "ts_max", "ts_sum", "ts_rank", "ts_corr",
}


class FactorDSLValidationError(ValueError):
    pass


def canonical_expression(expression: dict[str, Any]) -> str:
    validate_expression(expression)
    return json.dumps(expression, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def expression_hash(expression: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_expression(expression).encode("utf-8")).hexdigest()


def expression_complexity(expression: Any) -> int:
    if not isinstance(expression, dict):
        return 0
    return 1 + sum(
        expression_complexity(value)
        for value in expression.get("args", [])
        if isinstance(value, dict)
    )


def expression_inputs(expression: Any) -> list[str]:
    if not isinstance(expression, dict):
        return []
    values: set[str] = set()
    if expression.get("op") == "field":
        values.add(str(expression.get("name") or ""))
    for value in expression.get("args", []):
        values.update(expression_inputs(value))
    return sorted(item for item in values if item)


def expression_max_window(expression: Any) -> int:
    if not isinstance(expression, dict):
        return 1
    own = int(expression.get("window") or expression.get("period") or 1)
    nested = [expression_max_window(value) for value in expression.get("args", []) if isinstance(value, dict)]
    return max([own, *nested, 1])


def validate_expression(expression: Any, *, depth: int = 0) -> None:
    if not isinstance(expression, dict):
        raise FactorDSLValidationError("factor expression must be an object")
    if depth > MAX_EXPRESSION_DEPTH:
        raise FactorDSLValidationError("factor expression exceeds maximum depth")
    op = str(expression.get("op") or "")
    if op not in _OPS:
        raise FactorDSLValidationError(f"unsupported factor operator: {op}")
    if op == "field" and str(expression.get("name") or "") not in _FIELD_NAMES:
        raise FactorDSLValidationError(f"unsupported factor field: {expression.get('name')}")
    window = int(expression.get("window") or expression.get("period") or 1)
    if window < 1 or window > MAX_WINDOW_SESSIONS:
        raise FactorDSLValidationError("factor window is outside the allowed range")
    args = expression.get("args") or []
    if not isinstance(args, list):
        raise FactorDSLValidationError("factor args must be a list")
    expected = {
        "constant": 0, "field": 0, "neg": 1, "abs": 1, "sign": 1, "log": 1,
        "pow": 1, "delay": 1, "delta": 1, "returns": 1, "ts_mean": 1,
        "ts_std": 1, "ts_min": 1, "ts_max": 1, "ts_sum": 1, "ts_rank": 1,
        "add": 2, "sub": 2, "mul": 2, "div": 2, "ts_corr": 2,
    }[op]
    if len(args) != expected:
        raise FactorDSLValidationError(f"operator {op} expects {expected} args")
    for arg in args:
        validate_expression(arg, depth=depth + 1)


def _finite(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _history_rows(row: SnapshotRow, hist: list[HistBar]) -> list[dict[str, float]]:
    rows = [{
        "open": _finite(item.open), "high": _finite(item.high), "low": _finite(item.low),
        "close": _finite(item.close), "volume": _finite(item.volume),
        "amount": _finite(item.turnover),
    } for item in hist]
    current = {
        "open": _finite(row.open), "high": _finite(row.high), "low": _finite(row.low),
        "close": _finite(row.close), "volume": _finite(row.volume),
        "amount": _finite(row.turnover),
    }
    if not rows or rows[-1]["close"] != current["close"] or rows[-1]["volume"] != current["volume"]:
        rows.append(current)
    return rows


def _rank_last(values: list[float]) -> float:
    if not values:
        return 0.0
    last = values[-1]
    below = sum(value < last for value in values)
    equal = sum(value == last for value in values)
    return (below + 0.5 * equal) / len(values)


def _corr(left: list[float], right: list[float]) -> float:
    length = min(len(left), len(right))
    if length < 3:
        return 0.0
    xs, ys = left[-length:], right[-length:]
    mx, my = mean(xs), mean(ys)
    denominator = math.sqrt(
        sum((value - mx) ** 2 for value in xs)
        * sum((value - my) ** 2 for value in ys)
    )
    return (
        sum((xs[index] - mx) * (ys[index] - my) for index in range(length)) / denominator
        if denominator > 0 else 0.0
    )


def evaluate_expression(
    expression: dict[str, Any],
    row: SnapshotRow,
    hist: list[HistBar],
    daily_basic: dict[str, float] | None = None,
) -> float:
    validate_expression(expression)
    market_rows = _history_rows(row, hist)
    basic = daily_basic or {}

    def series(node: dict[str, Any]) -> list[float]:
        op = str(node["op"])
        args = node.get("args") or []
        window = int(node.get("window") or node.get("period") or 1)
        if op == "constant":
            return [_finite(node.get("value"))] * len(market_rows)
        if op == "field":
            name = str(node.get("name") or "")
            if name in {"turnover_rate", "float_mv", "total_mv", "pe_ttm", "pb"}:
                return [_finite(basic.get(name))] * len(market_rows)
            if name == "returns":
                closes = [item["close"] for item in market_rows]
                return [0.0] + [
                    closes[index] / closes[index - 1] - 1.0 if closes[index - 1] > 0 else 0.0
                    for index in range(1, len(closes))
                ]
            return [_finite(item.get(name)) for item in market_rows]
        values = [series(arg) for arg in args]
        length = len(market_rows)

        def aligned(items: list[float]) -> list[float]:
            return ([0.0] * max(0, length - len(items)) + items)[-length:]

        values = [aligned(item) for item in values]
        if op in {"add", "sub", "mul", "div"}:
            left, right = values
            if op == "add":
                return [left[i] + right[i] for i in range(length)]
            if op == "sub":
                return [left[i] - right[i] for i in range(length)]
            if op == "mul":
                return [left[i] * right[i] for i in range(length)]
            return [left[i] / right[i] if abs(right[i]) > 1e-12 else 0.0 for i in range(length)]
        source = values[0] if values else [0.0] * length
        if op == "neg":
            return [-value for value in source]
        if op == "abs":
            return [abs(value) for value in source]
        if op == "sign":
            return [1.0 if value > 0 else (-1.0 if value < 0 else 0.0) for value in source]
        if op == "log":
            return [math.log(abs(value)) if abs(value) > 1e-12 else 0.0 for value in source]
        if op == "pow":
            exponent = _finite(node.get("exponent", 1.0))
            return [math.copysign(abs(value) ** exponent, value) for value in source]
        if op == "delay":
            return [0.0] * min(window, length) + source[:max(0, length - window)]
        if op == "delta":
            return [
                source[i] - source[i - window] if i >= window else 0.0
                for i in range(length)
            ]
        if op == "returns":
            return [
                source[i] / source[i - window] - 1.0
                if i >= window and abs(source[i - window]) > 1e-12 else 0.0
                for i in range(length)
            ]
        if op == "ts_corr":
            left, right = values
            return [_corr(left[max(0, i - window + 1):i + 1], right[max(0, i - window + 1):i + 1]) for i in range(length)]
        result: list[float] = []
        for index in range(length):
            sample = source[max(0, index - window + 1):index + 1]
            if op == "ts_mean":
                result.append(mean(sample) if sample else 0.0)
            elif op == "ts_std":
                avg = mean(sample) if sample else 0.0
                result.append(math.sqrt(mean((value - avg) ** 2 for value in sample)) if sample else 0.0)
            elif op == "ts_min":
                result.append(min(sample) if sample else 0.0)
            elif op == "ts_max":
                result.append(max(sample) if sample else 0.0)
            elif op == "ts_sum":
                result.append(sum(sample))
            elif op == "ts_rank":
                result.append(_rank_last(sample))
        return result

    values = series(expression)
    return _finite(values[-1] if values else 0.0)

