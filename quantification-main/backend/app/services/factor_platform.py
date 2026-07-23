from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from statistics import mean
from typing import Any, Iterable

from app.db.store import _conn
from app.services.backtest_data_source import BacktestDataSource, HistBar, SnapshotRow
from app.services.point_in_time import signal_available_time


FACTOR_SCHEMA_VERSION = "alis_factor_registry_v1"
PRODUCTION_FACTOR_SPECS: tuple[dict[str, Any], ...] = (
    {"key": "volume", "name_cn": "成交量", "category": "量价", "formula": "snapshot.volume", "inputs": ["daily.vol"], "direction": 1, "window": 1},
    {"key": "turnover", "name_cn": "成交额", "category": "量价", "formula": "snapshot.amount", "inputs": ["daily.amount"], "direction": 1, "window": 1},
    {"key": "turnover_rate", "name_cn": "换手率", "category": "量价", "formula": "daily_basic.turnover_rate", "inputs": ["daily_basic.turnover_rate"], "direction": 1, "window": 1},
    {"key": "change_pct", "name_cn": "绝对涨跌幅", "category": "量价", "formula": "abs(snapshot.pct_chg)", "inputs": ["daily.pct_chg"], "direction": 1, "window": 1},
    {"key": "momentum_5", "name_cn": "5日动量", "category": "动量", "formula": "close_t/close_t_minus_5-1", "inputs": ["daily.close", "adj_factor"], "direction": 1, "window": 5},
    {"key": "momentum_20", "name_cn": "20日动量", "category": "动量", "formula": "close_t/close_t_minus_20-1", "inputs": ["daily.close", "adj_factor"], "direction": 1, "window": 20},
    {"key": "momentum_60", "name_cn": "60日动量", "category": "动量", "formula": "close_t/close_t_minus_60-1", "inputs": ["daily.close", "adj_factor"], "direction": 1, "window": 60},
    {"key": "momentum_120", "name_cn": "120日动量", "category": "动量", "formula": "close_t/close_t_minus_120-1", "inputs": ["daily.close", "adj_factor"], "direction": 1, "window": 120},
    {"key": "relative_strength_60", "name_cn": "60日相对强度", "category": "动量", "formula": "stock_momentum_60-hs300_momentum_60", "inputs": ["daily.close", "adj_factor", "index_daily.close"], "direction": 1, "window": 60},
    {"key": "volatility_20", "name_cn": "20日波动率", "category": "风险", "formula": "std(daily_return_20)", "inputs": ["daily.close", "adj_factor"], "direction": -1, "window": 20},
    {"key": "downside_volatility_60", "name_cn": "60日下行波动", "category": "风险", "formula": "std(min(daily_return,0),60)", "inputs": ["daily.close", "adj_factor"], "direction": -1, "window": 60},
    {"key": "max_drawdown_60", "name_cn": "60日最大回撤", "category": "风险", "formula": "min(close/running_max(close)-1,60)", "inputs": ["daily.close", "adj_factor"], "direction": 1, "window": 60},
    {"key": "beta_60", "name_cn": "60日Beta", "category": "风险", "formula": "cov(stock_return,index_return)/var(index_return)", "inputs": ["daily.close", "adj_factor", "index_daily.close"], "direction": -1, "window": 60},
    {"key": "float_mv", "name_cn": "流通市值", "category": "估值", "formula": "daily_basic.circ_mv", "inputs": ["daily_basic.circ_mv"], "direction": 1, "window": 1},
    {"key": "total_mv", "name_cn": "总市值", "category": "估值", "formula": "daily_basic.total_mv", "inputs": ["daily_basic.total_mv"], "direction": 1, "window": 1},
    {"key": "pe_ttm", "name_cn": "市盈率TTM", "category": "估值", "formula": "daily_basic.pe_ttm", "inputs": ["daily_basic.pe_ttm"], "direction": -1, "window": 1},
    {"key": "pb", "name_cn": "市净率", "category": "估值", "formula": "daily_basic.pb", "inputs": ["daily_basic.pb"], "direction": -1, "window": 1},
    {"key": "earnings_yield", "name_cn": "盈利收益率", "category": "基本面", "formula": "1/pe_ttm", "inputs": ["daily_basic.pe_ttm"], "direction": 1, "window": 1},
    {"key": "book_to_price", "name_cn": "账面市值比", "category": "基本面", "formula": "1/pb", "inputs": ["daily_basic.pb"], "direction": 1, "window": 1},
    {"key": "volume_ratio_5", "name_cn": "5日量比", "category": "量价", "formula": "volume_t/mean(volume_5)", "inputs": ["daily.vol"], "direction": 1, "window": 5},
    {"key": "is_st", "name_cn": "是否ST", "category": "交易约束", "formula": "name_st_flag", "inputs": ["stock_basic.name"], "direction": -1, "window": 1},
    {"key": "is_suspended", "name_cn": "是否停牌", "category": "交易约束", "formula": "volume<=0", "inputs": ["daily.vol"], "direction": -1, "window": 1},
    {"key": "is_limit_up", "name_cn": "是否涨停", "category": "交易约束", "formula": "pct_chg>=9.7", "inputs": ["daily.pct_chg"], "direction": -1, "window": 1},
    {"key": "is_limit_down", "name_cn": "是否跌停", "category": "交易约束", "formula": "pct_chg<=-9.7", "inputs": ["daily.pct_chg"], "direction": -1, "window": 1},
)
SPEC_BY_KEY = {str(item["key"]): item for item in PRODUCTION_FACTOR_SPECS}
CORE_ACTIVE_KEYS = {
    "volume", "turnover", "turnover_rate", "change_pct", "momentum_5",
    "momentum_20", "volatility_20", "volume_ratio_5", "float_mv", "total_mv",
    "pe_ttm", "pb", "is_st", "is_suspended", "is_limit_up", "is_limit_down",
}


def _safe(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _returns(closes: list[float], window: int) -> list[float]:
    values = closes[-max(2, int(window) + 1):]
    return [
        values[index] / values[index - 1] - 1.0
        for index in range(1, len(values))
        if values[index - 1] > 0
    ]


def _momentum(current: float, closes: list[float], window: int) -> float:
    if len(closes) < window or closes[-window] <= 0:
        return 0.0
    return current / closes[-window] - 1.0


def compute_registered_factor(
    factor: str,
    row: SnapshotRow,
    hist: list[HistBar],
    daily_basic: dict[str, dict[str, float]],
    *,
    benchmark_return: float | None = None,
    benchmark_returns: list[float] | None = None,
) -> float:
    """Single deterministic implementation consumed by selection, training and replay."""
    key = str(factor)
    closes = [_safe(item.close) for item in hist if _safe(item.close) > 0]
    volumes = [_safe(item.volume) for item in hist]
    current = _safe(row.close)
    db = daily_basic.get(row.symbol, {})
    if key == "volume":
        return _safe(row.volume)
    if key == "turnover":
        return _safe(row.turnover)
    if key == "turnover_rate":
        return _safe(db.get("turnover_rate"))
    if key == "change_pct":
        return abs(_safe(row.change_pct))
    if key.startswith("momentum_") and key.split("_")[-1].isdigit():
        return _momentum(current, closes, int(key.split("_")[-1]))
    if key == "relative_strength_60":
        return _momentum(current, closes, 60) - _safe(benchmark_return)
    if key == "volatility_20":
        values = _returns(closes, 20)
        return math.sqrt(sum((value - mean(values)) ** 2 for value in values) / len(values)) if values else 0.0
    if key == "downside_volatility_60":
        values = [min(value, 0.0) for value in _returns(closes, 60)]
        return math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0
    if key == "max_drawdown_60":
        values = closes[-60:]
        peak = 0.0
        drawdown = 0.0
        for value in values:
            peak = max(peak, value)
            if peak > 0:
                drawdown = min(drawdown, value / peak - 1.0)
        return drawdown
    if key == "beta_60":
        stock = _returns(closes, 60)
        benchmark = list(benchmark_returns or [])[-len(stock):]
        if len(stock) < 2 or len(benchmark) != len(stock):
            return 0.0
        stock_mean, benchmark_mean = mean(stock), mean(benchmark)
        variance = sum((value - benchmark_mean) ** 2 for value in benchmark) / len(benchmark)
        covariance = sum(
            (stock[index] - stock_mean) * (benchmark[index] - benchmark_mean)
            for index in range(len(stock))
        ) / len(stock)
        return covariance / variance if variance > 0 else 0.0
    if key == "volume_ratio_5":
        values = volumes[-5:]
        average = sum(values) / len(values) if len(values) >= 5 else 0.0
        return _safe(row.volume) / average if average > 0 else 0.0
    if key == "float_mv":
        return _safe(db.get("float_mv"))
    if key == "total_mv":
        return _safe(db.get("total_mv"))
    if key == "pe_ttm":
        return _safe(db.get("pe_ttm"))
    if key == "pb":
        return _safe(db.get("pb"))
    if key == "earnings_yield":
        pe = _safe(db.get("pe_ttm"))
        return 1.0 / pe if pe > 0 else 0.0
    if key == "book_to_price":
        pb = _safe(db.get("pb"))
        return 1.0 / pb if pb > 0 else 0.0
    if key == "is_st":
        return 1.0 if str(row.name or "").upper().startswith(("ST", "*ST")) else 0.0
    if key == "is_suspended":
        return 1.0 if _safe(row.volume) <= 0 else 0.0
    if key == "is_limit_up":
        return 1.0 if _safe(row.change_pct) >= 9.7 else 0.0
    if key == "is_limit_down":
        return 1.0 if _safe(row.change_pct) <= -9.7 else 0.0
    return 0.0


def ensure_factor_registry() -> list[dict[str, Any]]:
    now = datetime.now().astimezone().isoformat()
    with _conn() as conn:
        for spec in PRODUCTION_FACTOR_SPECS:
            existing = conn.execute(
                "SELECT id FROM factor_definitions WHERE factor_key=? AND version='v1'",
                (str(spec["key"]),),
            ).fetchone()
            initial_status = "active" if str(spec["key"]) in CORE_ACTIVE_KEYS else "candidate"
            payload = (
                initial_status, str(spec["name_cn"]), str(spec["category"]),
                json.dumps({"expression": spec["formula"], "engine": FACTOR_SCHEMA_VERSION}, ensure_ascii=False),
                json.dumps(spec["inputs"], ensure_ascii=False), int(spec["direction"]),
                int(spec["window"]), "T_close_available_by_T_15_00",
                "cross_section_median_then_zero_if_unavailable", "passed", now,
            )
            if existing:
                conn.execute(
                    """UPDATE factor_definitions SET name_cn=?,category=?,
                       formula_json=?,inputs_json=?,direction=?,window_sessions=?,
                       available_time_rule=?,missing_policy=?,leakage_status=?,updated_at=?
                       WHERE id=?""",
                    (*payload[1:], int(existing["id"])),
                )
            else:
                conn.execute(
                    """INSERT INTO factor_definitions(
                       factor_key,version,status,name_cn,category,formula_json,inputs_json,
                       direction,window_sessions,available_time_rule,missing_policy,
                       leakage_status,created_at,updated_at
                       ) VALUES(?,'v1',?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(spec["key"]), *payload[:-1], now, now),
                )
        rows = conn.execute(
            """SELECT * FROM factor_definitions
               WHERE status IN ('active','candidate','degraded')
               ORDER BY category,factor_key"""
        ).fetchall()
    return [_factor_definition_row(dict(row)) for row in rows]


def _factor_definition_row(item: dict[str, Any]) -> dict[str, Any]:
    for source, target, fallback in (
        ("formula_json", "formula", {}),
        ("inputs_json", "inputs", []),
    ):
        try:
            item[target] = json.loads(item.pop(source, "") or json.dumps(fallback))
        except Exception:
            item[target] = fallback
    item["id"] = int(item.get("id") or 0)
    item["direction"] = int(item.get("direction") or 0)
    item["window_sessions"] = int(item.get("window_sessions") or 0)
    return item


def factor_registry_status() -> dict[str, Any]:
    definitions = ensure_factor_registry()
    with _conn() as conn:
        value_row = conn.execute(
            "SELECT MAX(trade_date) AS latest_date,COUNT(*) AS value_count FROM factor_values"
        ).fetchone()
        evaluation_row = conn.execute(
            "SELECT COUNT(*) AS evaluation_count FROM factor_evaluations"
        ).fetchone()
    return {
        "ok": True,
        "schema_version": FACTOR_SCHEMA_VERSION,
        "active_count": sum(str(item.get("status") or "") == "active" for item in definitions),
        "candidate_count": sum(str(item.get("status") or "") == "candidate" for item in definitions),
        "latest_materialized_date": str(dict(value_row).get("latest_date") or "") if value_row else "",
        "value_count": int(dict(value_row).get("value_count") or 0) if value_row else 0,
        "evaluation_count": int(dict(evaluation_row).get("evaluation_count") or 0) if evaluation_row else 0,
        "definitions": definitions,
        "consumer_contract": {
            "selection": FACTOR_SCHEMA_VERSION,
            "model_training": FACTOR_SCHEMA_VERSION,
            "backtest": FACTOR_SCHEMA_VERSION,
            "strategy_replay": FACTOR_SCHEMA_VERSION,
            "daily_decision": FACTOR_SCHEMA_VERSION,
        },
    }


def active_factor_bindings(keys: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    wanted = {str(key) for key in keys} if keys is not None else None
    return {
        str(item["factor_key"]): {
            "definition_id": int(item["id"]),
            "version": str(item["version"]),
            "formula": item["formula"],
            "available_time_rule": item["available_time_rule"],
            "leakage_status": item["leakage_status"],
        }
        for item in ensure_factor_registry()
        if str(item.get("status") or "") == "active"
        and (wanted is None or str(item["factor_key"]) in wanted)
    }


def _winsorized_zscores(values: dict[str, float], direction: int) -> dict[str, float]:
    ordered = sorted(values.values())
    if not ordered:
        return {}
    low = ordered[max(0, int(len(ordered) * 0.01) - 1)]
    high = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    clipped = {key: min(high, max(low, value)) for key, value in values.items()}
    avg = mean(clipped.values())
    variance = sum((value - avg) ** 2 for value in clipped.values()) / len(clipped)
    std = math.sqrt(variance)
    return {
        key: (value - avg) / std * int(direction or 1) if std > 0 else 0.0
        for key, value in clipped.items()
    }


def materialize_factor_snapshot(
    *,
    trade_date: str,
    factor_keys: list[str] | None = None,
    data_source: BacktestDataSource | None = None,
) -> dict[str, Any]:
    definitions = ensure_factor_registry()
    wanted = set(factor_keys or [str(item["factor_key"]) for item in definitions])
    definitions = [item for item in definitions if str(item["factor_key"]) in wanted]
    ds = data_source or BacktestDataSource()
    date_key = str(trade_date).replace("-", "")[:8]
    cutoff = signal_available_time(date_key)
    ds.set_as_of_time(cutoff)
    rows = ds.load_snapshot(date_key)
    daily_basic = ds.get_daily_basic_map(date_key)
    max_window = max((int(item["window_sessions"]) for item in definitions), default=1)
    ds.preload_histories([row.symbol for row in rows], end_date=date_key, lookback=max_window + 1)
    benchmark_returns = ds.get_index_returns("000300.SH", date_key, lookback=60)
    benchmark_return = math.prod(1.0 + value for value in benchmark_returns) - 1.0 if benchmark_returns else None
    raw_by_factor: dict[str, dict[str, float]] = {str(item["factor_key"]): {} for item in definitions}
    input_hashes: dict[tuple[str, str], str] = {}
    for row in rows:
        hist = ds.get_hist_window(row.symbol, date_key, max_window + 1)
        for definition in definitions:
            key = str(definition["factor_key"])
            raw = compute_registered_factor(
                key, row, hist, daily_basic,
                benchmark_return=benchmark_return,
                benchmark_returns=benchmark_returns,
            )
            raw_by_factor[key][row.symbol] = raw
            digest = hashlib.sha256(json.dumps({
                "schema": FACTOR_SCHEMA_VERSION,
                "definition_id": definition["id"],
                "trade_date": date_key,
                "symbol": row.symbol,
                "row": [row.close, row.volume, row.turnover, row.change_pct],
                "history": [[item.date, item.close, item.volume] for item in hist],
                "daily_basic": daily_basic.get(row.symbol, {}),
                "benchmark_return": benchmark_return,
                "benchmark_returns": benchmark_returns,
            }, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            input_hashes[(key, row.symbol)] = digest
    definition_by_key = {str(item["factor_key"]): item for item in definitions}
    now = datetime.now().astimezone().isoformat()
    payload: list[tuple[Any, ...]] = []
    for key, raw_values in raw_by_factor.items():
        definition = definition_by_key[key]
        standardized = _winsorized_zscores(raw_values, int(definition["direction"]))
        for symbol, raw in raw_values.items():
            payload.append((
                int(definition["id"]), date_key, symbol, raw,
                standardized.get(symbol, 0.0), cutoff, input_hashes[(key, symbol)], now,
            ))
    with _conn() as conn:
        if conn._is_postgres and payload:
            cursor = conn._conn.cursor()
            cursor.executemany(
                """INSERT INTO factor_values(
                   factor_definition_id,trade_date,ts_code,raw_value,
                   standardized_value,available_time,input_hash,created_at
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(factor_definition_id,trade_date,ts_code) DO UPDATE SET
                   raw_value=EXCLUDED.raw_value,standardized_value=EXCLUDED.standardized_value,
                   available_time=EXCLUDED.available_time,input_hash=EXCLUDED.input_hash,
                   created_at=EXCLUDED.created_at""",
                payload,
            )
        else:
            for values in payload:
                existing = conn.execute(
                    """SELECT id FROM factor_values
                       WHERE factor_definition_id=? AND trade_date=? AND ts_code=?""",
                    values[:3],
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE factor_values SET raw_value=?,standardized_value=?,
                           available_time=?,input_hash=?,created_at=? WHERE id=?""",
                        (*values[3:], int(existing["id"])),
                    )
                else:
                    conn.execute(
                        """INSERT INTO factor_values(
                           factor_definition_id,trade_date,ts_code,raw_value,
                           standardized_value,available_time,input_hash,created_at
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        values,
                    )
    return {
        "ok": True,
        "schema_version": FACTOR_SCHEMA_VERSION,
        "trade_date": date_key,
        "factor_count": len(definitions),
        "symbol_count": len(rows),
        "value_count": len(payload),
        "point_in_time": {
            "cutoff": cutoff,
            "passed": all(bool(item.get("passed")) for item in ds.point_in_time_sources()),
            "sources": ds.point_in_time_sources(),
        },
    }


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    mx, my = mean(xs), mean(ys)
    covariance = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
    denominator = math.sqrt(
        sum((value - mx) ** 2 for value in xs)
        * sum((value - my) ** 2 for value in ys)
    )
    return covariance / denominator if denominator > 0 else 0.0


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    for rank, index in enumerate(order):
        result[index] = float(rank)
    return result


def evaluate_materialized_factors(
    *,
    trade_date: str,
    forward_date: str,
    transaction_cost: float = 0.0015,
    data_source: BacktestDataSource | None = None,
) -> dict[str, Any]:
    definitions = ensure_factor_registry()
    ds = data_source or BacktestDataSource()
    start_key = str(trade_date).replace("-", "")[:8]
    end_key = str(forward_date).replace("-", "")[:8]
    start_rows = {row.symbol: row for row in ds.load_snapshot(start_key)}
    end_rows = {row.symbol: row for row in ds.load_snapshot(end_key)}
    results: list[dict[str, Any]] = []
    now = datetime.now().astimezone().isoformat()
    with _conn() as conn:
        for definition in definitions:
            values = conn.execute(
                """SELECT ts_code,standardized_value FROM factor_values
                   WHERE factor_definition_id=? AND trade_date=?""",
                (int(definition["id"]), start_key),
            ).fetchall()
            pairs = [
                (
                    _safe(dict(item).get("standardized_value")),
                    end_rows[symbol].close / start_rows[symbol].close - 1.0,
                )
                for item in values
                for symbol in [str(dict(item).get("ts_code") or "")]
                if symbol in start_rows and symbol in end_rows and start_rows[symbol].close > 0
            ]
            xs, ys = [item[0] for item in pairs], [item[1] for item in pairs]
            ic = _pearson(xs, ys)
            rank_ic = _pearson(_ranks(xs), _ranks(ys))
            ordered = sorted(pairs, key=lambda item: item[0])
            bucket = max(1, len(ordered) // 5) if ordered else 1
            top = mean(item[1] for item in ordered[-bucket:]) if ordered else 0.0
            bottom = mean(item[1] for item in ordered[:bucket]) if ordered else 0.0
            spread = top - bottom
            coverage = len(pairs) / max(1, len(start_rows))
            cost_after = spread - 2 * float(transaction_cost)
            status = "healthy" if coverage >= 0.80 and rank_ic > 0 and cost_after > 0 else "degraded"
            conn.execute(
                """INSERT INTO factor_evaluations(
                   factor_definition_id,start_date,end_date,sample_count,
                   information_coefficient,rank_ic,top_bottom_return,turnover,
                   coverage,cost_after_incremental_oos,market_state_json,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(definition["id"]), start_key, end_key, len(pairs), ic, rank_ic,
                    spread, 1.0, coverage, cost_after, "{}", status, now,
                ),
            )
            results.append({
                "factor_key": definition["factor_key"],
                "definition_id": definition["id"],
                "sample_count": len(pairs),
                "ic": round(ic, 8),
                "rank_ic": round(rank_ic, 8),
                "top_bottom_return": round(spread, 8),
                "coverage": round(coverage, 8),
                "cost_after_incremental_oos": round(cost_after, 8),
                "status": status,
            })
    return {
        "ok": True,
        "schema_version": "alis_factor_evaluation_v1",
        "start_date": start_key,
        "end_date": end_key,
        "results": results,
    }


def govern_factor_versions(minimum_evaluations: int = 20) -> dict[str, Any]:
    """Promote or degrade factors only from deterministic, cost-aware evidence."""
    definitions = ensure_factor_registry()
    events: list[dict[str, Any]] = []
    now = datetime.now().astimezone().isoformat()
    with _conn() as conn:
        for definition in definitions:
            rows = [
                dict(row) for row in conn.execute(
                    """SELECT * FROM factor_evaluations
                       WHERE factor_definition_id=? ORDER BY end_date DESC LIMIT 60""",
                    (int(definition["id"]),),
                ).fetchall()
            ]
            if len(rows) < int(minimum_evaluations):
                continue
            recent = rows[:int(minimum_evaluations)]
            avg_rank_ic = mean(_safe(row.get("rank_ic")) for row in recent)
            avg_cost_after = mean(_safe(row.get("cost_after_incremental_oos")) for row in recent)
            avg_coverage = mean(_safe(row.get("coverage")) for row in recent)
            positive_ratio = sum(_safe(row.get("rank_ic")) > 0 for row in recent) / len(recent)
            evidence = {
                "evaluation_count": len(recent),
                "average_rank_ic": round(avg_rank_ic, 8),
                "positive_rank_ic_ratio": round(positive_ratio, 8),
                "average_cost_after_incremental_oos": round(avg_cost_after, 8),
                "average_coverage": round(avg_coverage, 8),
            }
            current = str(definition.get("status") or "")
            event_type = ""
            new_status = current
            if current == "candidate" and avg_rank_ic >= 0.02 and positive_ratio >= 0.60 and avg_cost_after > 0 and avg_coverage >= 0.80:
                event_type, new_status = "promoted", "active"
            elif current == "active" and (avg_rank_ic <= -0.02 or avg_cost_after <= -0.01 or avg_coverage < 0.60):
                event_type, new_status = "degraded", "degraded"
            elif current == "degraded" and avg_rank_ic > 0 and avg_cost_after > 0 and avg_coverage >= 0.80:
                event_type, new_status = "restored", "active"
            if not event_type:
                continue
            conn.execute(
                "UPDATE factor_definitions SET status=?,updated_at=? WHERE id=?",
                (new_status, now, int(definition["id"])),
            )
            conn.execute(
                """INSERT INTO factor_version_events(
                   factor_definition_id,event_type,reason_json,evidence_json,created_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    int(definition["id"]), event_type,
                    json.dumps({"rule": "20_period_cost_aware_factor_gate_v1"}, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False), now,
                ),
            )
            events.append({
                "factor_key": definition["factor_key"],
                "definition_id": definition["id"],
                "event_type": event_type,
                "from_status": current,
                "to_status": new_status,
                "evidence": evidence,
            })
    return {"ok": True, "events": events}


def block_factor_for_leakage(
    factor_definition_id: int,
    *,
    reason: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Permanent hard stop used when a point-in-time violation is confirmed."""
    now = datetime.now().astimezone().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM factor_definitions WHERE id=?",
            (int(factor_definition_id),),
        ).fetchone()
        if not row:
            raise ValueError("factor definition not found")
        conn.execute(
            """UPDATE factor_definitions SET status='blocked_leakage',
               leakage_status='failed',updated_at=? WHERE id=?""",
            (now, int(factor_definition_id)),
        )
        conn.execute(
            """INSERT INTO factor_version_events(
               factor_definition_id,event_type,reason_json,evidence_json,created_at
               ) VALUES(?,'blocked_leakage',?,?,?)""",
            (
                int(factor_definition_id),
                json.dumps(reason, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                now,
            ),
        )
    return {
        "ok": True,
        "factor_definition_id": int(factor_definition_id),
        "status": "blocked_leakage",
    }


def run_factor_daily_cycle() -> dict[str, Any]:
    ds = BacktestDataSource()
    dates = ds.get_available_trade_dates("2000-01-01", datetime.now().date().isoformat())
    if not dates:
        raise RuntimeError("没有可用于因子生产的交易日")
    latest = dates[-1]
    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) AS latest_date FROM factor_values"
        ).fetchone()
    previous_materialized = str(dict(row).get("latest_date") or "") if row else ""
    materialized = materialize_factor_snapshot(trade_date=latest, data_source=ds)
    evaluation: dict[str, Any] | None = None
    if previous_materialized and previous_materialized < latest.replace("-", ""):
        evaluation = evaluate_materialized_factors(
            trade_date=previous_materialized,
            forward_date=latest,
            data_source=ds,
        )
    governance = govern_factor_versions()
    return {
        "schema_version": "alis_factor_daily_cycle_v1",
        "status": "completed",
        "materialization": materialized,
        "evaluation": evaluation,
        "governance": governance,
    }
