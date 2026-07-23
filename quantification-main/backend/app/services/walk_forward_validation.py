from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.data_platform.db import _conn
from app.services.backtest_data_source import BacktestDataSource
from app.services.market_context import _transparent_regime, score_market_sentiment
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_replay import run_strategy_replay


@dataclass(frozen=True)
class WalkForwardConfig:
    train_sessions: int = 756
    validation_sessions: int = 126
    test_sessions: int = 126
    purge_sessions: int = 20
    embargo_sessions: int = 5
    step_sessions: int = 126
    minimum_windows: int = 5


def build_purged_walk_forward_windows(dates: list[str], config: WalkForwardConfig) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    offset = 0
    while True:
        train_start = offset
        train_end = train_start + config.train_sessions
        purge_end = train_end + config.purge_sessions
        validation_end = purge_end + config.validation_sessions
        embargo_end = validation_end + config.embargo_sessions
        test_end = embargo_end + config.test_sessions
        if test_end > len(dates):
            break
        windows.append({
            "index": len(windows) + 1,
            "train": {"start": dates[train_start], "end": dates[train_end - 1], "sessions": config.train_sessions},
            "purge": {"start": dates[train_end], "end": dates[purge_end - 1], "sessions": config.purge_sessions},
            "validation": {"start": dates[purge_end], "end": dates[validation_end - 1], "sessions": config.validation_sessions},
            "embargo": {"start": dates[validation_end], "end": dates[embargo_end - 1], "sessions": config.embargo_sessions},
            "test": {"start": dates[embargo_end], "end": dates[test_end - 1], "sessions": config.test_sessions},
        })
        offset += config.step_sessions
    return windows


def _market_states(dates: list[str]) -> dict[str, str]:
    if not dates:
        return {}
    compact = [value.replace("-", "") for value in dates]
    rows: list[dict[str, Any]] = []
    with _conn() as conn:
        for offset in range(0, len(compact), 300):
            batch = compact[offset:offset + 300]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(dict(row) for row in conn.execute(
                f"""SELECT trade_date,COUNT(*) AS total,
                           AVG(CASE WHEN change_pct>0 THEN 1.0 ELSE 0.0 END) AS breadth,
                           AVG(change_pct)/100.0 AS average_return,SUM(COALESCE(turnover,0)) AS amount,
                           AVG(CASE WHEN change_pct>=9.7 THEN 1.0 ELSE 0.0 END) AS limit_up_ratio,
                           AVG(CASE WHEN change_pct<=-9.7 THEN 1.0 ELSE 0.0 END) AS limit_down_ratio
                    FROM dc_stock_daily_snapshot
                    WHERE trade_date IN ({placeholders}) AND available_time<=trade_date||'T15:00:00'
                    GROUP BY trade_date ORDER BY trade_date""", tuple(batch),
            ).fetchall())
    rows.sort(key=lambda item: str(item.get("trade_date") or ""))
    amounts: list[float] = []
    previous = ""
    states: dict[str, str] = {}
    for row in rows:
        amount = float(row.get("amount") or 0)
        baseline = sum(amounts[-20:]) / len(amounts[-20:]) if amounts else amount
        observations = {
            "breadth": float(row.get("breadth") or 0.5),
            "average_return": float(row.get("average_return") or 0),
            "volume_ratio": amount / baseline if baseline > 0 else 1.0,
            "limit_up_ratio": float(row.get("limit_up_ratio") or 0),
            "limit_down_ratio": float(row.get("limit_down_ratio") or 0),
            "northbound_flow_z": 0.0, "leader_persistence": 0.0,
            "price_fundamental_divergence": 0.0, "crowding": 0.0,
        }
        score = float(score_market_sentiment(**observations)["score"])
        state = _transparent_regime(score, observations["average_return"], previous)
        trade_date = str(row.get("trade_date") or "")
        states[f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"] = state
        previous = state
        amounts.append(amount)
    return states


def _state_attribution(replay: dict[str, Any], states: dict[str, str]) -> dict[str, Any]:
    equity = replay.get("equity_series") or []
    grouped: dict[str, list[float]] = {state: [] for state in ("S0", "S1", "S2", "S3")}
    for previous, current in zip(equity, equity[1:]):
        prior = float(previous.get("total_value") or 0)
        state = states.get(str(current.get("date") or ""), "unknown")
        if prior > 0:
            grouped.setdefault(state, []).append(float(current.get("total_value") or 0) / prior - 1.0)
    result = {}
    for state, values in grouped.items():
        compounded = 1.0
        for value in values:
            compounded *= 1.0 + value
        result[state] = {
            "sessions": len(values),
            "cost_after_return": round(compounded - 1.0, 8) if values else None,
            "average_daily_return": round(sum(values) / len(values), 8) if values else None,
            "positive_session_ratio": round(sum(value > 0 for value in values) / len(values), 6) if values else None,
        }
    return result


def run_purged_walk_forward_validation(
    *, start_date: str | None = None, end_date: str | None = None,
    config: WalkForwardConfig | None = None, max_windows: int | None = None,
    strategy_id: int | None = None,
    execute: bool = True, data_source: BacktestDataSource | None = None,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    state_loader: Callable[[list[str]], dict[str, str]] = _market_states,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    checkpoint_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cfg = config or WalkForwardConfig()
    champion = resolve_production_strategy_champion()
    ds = data_source or BacktestDataSource()
    dates = ds.get_available_trade_dates(start_date or "1900-01-01", end_date or "2999-12-31")
    windows = build_purged_walk_forward_windows(dates, cfg)
    selected = windows[:max(0, int(max_windows))] if max_windows is not None else windows
    base = {
        "schema_version": "alis_purged_walk_forward_oos_v1",
        "production_strategy_version_id": champion["champion_id"],
        "production_strategy_version": champion["champion_version"],
        "evaluated_strategy_id": int(strategy_id or champion.get("strategy_id") or 0),
        "evaluation_role": "challenger" if strategy_id and int(strategy_id) != int(champion.get("strategy_id") or 0) else "champion",
        "strategy_controller_version": champion["strategy_controller_version"],
        "protocol": {**cfg.__dict__, "split_policy": "rolling_origin_with_fixed_train_span", "estimator_policy": "frozen_champion_no_refit_inside_evaluation", "cost_policy": "A-share fees and slippage included by strategy replay"},
        "available_sessions": len(dates), "available_window_count": len(windows),
        "required_window_count": cfg.minimum_windows,
    }
    if not execute:
        return {**base, "status": "preview", "capacity_status": "ready" if len(windows) >= cfg.minimum_windows else "insufficient_data", "windows": selected}
    selected_indexes = {int(item["index"]) for item in selected}
    results = [
        dict(item) for item in (checkpoint_results or [])
        if int(item.get("index") or 0) in selected_indexes
    ]
    completed_indexes = {int(item.get("index") or 0) for item in results}
    for window in selected:
        if int(window["index"]) in completed_indexes:
            continue
        test = window["test"]
        replay = replay_runner(strategy_id=strategy_id, start_date=test["start"], end_date=test["end"], data_source=BacktestDataSource())
        replay_dates = [str(item.get("date") or "") for item in replay.get("equity_series") or []]
        states = state_loader(replay_dates)
        window_result = {
            **window,
            "metrics": replay.get("metrics") or {},
            "summary": replay.get("summary") or {},
            "data_complete": int((replay.get("summary") or {}).get("trading_days") or 0) >= int(test["sessions"]) and int((replay.get("summary") or {}).get("decision_count") or 0) > 0,
            "point_in_time_passed": bool(((replay.get("data_quality") or {}).get("point_in_time") or {}).get("passed")),
            "production_strategy_version": replay.get("production_strategy_version"),
            "market_state_attribution": _state_attribution(replay, states),
        }
        results.append(window_result)
        results.sort(key=lambda item: int(item.get("index") or 0))
        if progress_callback:
            progress_callback({
                "phase": "formal_oos",
                "completed_windows": len(results),
                "total_windows": len(selected),
                "window_index": int(window["index"]),
                "progress": round(len(results) / max(1, len(selected)), 6),
                "message": f"正式 OOS 窗口 {len(results)}/{len(selected)} 已完成",
                "checkpoint_results": results,
            })
    valid = [item for item in results if item["data_complete"] and item["point_in_time_passed"] and item["production_strategy_version"] == champion["champion_version"]]
    wins = [item for item in valid if float((item["metrics"] or {}).get("excess_return") or 0) > 0]
    covered_states = sorted({state for item in valid for state, value in item["market_state_attribution"].items() if int(value.get("sessions") or 0) > 0 and state != "unknown"})
    aggregate_return = 1.0
    aggregate_benchmark = 1.0
    worst_drawdown = 0.0
    total_fees = 0.0
    for item in valid:
        aggregate_return *= 1.0 + float(item["metrics"].get("total_return") or 0)
        aggregate_benchmark *= 1.0 + float(item["metrics"].get("benchmark_return") or 0)
        worst_drawdown = min(worst_drawdown, float(item["metrics"].get("max_drawdown") or 0))
        total_fees += float(item["summary"].get("total_fees") or 0)
    gates = {
        "minimum_required_windows": len(valid) >= cfg.minimum_windows,
        "majority_windows_outperform": bool(valid) and len(wins) / len(valid) >= 0.60,
        "aggregate_cost_after_excess_positive": aggregate_return > aggregate_benchmark,
        "all_four_market_states_covered": set(covered_states) == {"S0", "S1", "S2", "S3"},
        "point_in_time_all_passed": all(item["point_in_time_passed"] for item in results),
        "same_champion_all_windows": all(item["production_strategy_version"] == champion["champion_version"] for item in results),
        "complete_test_data_all_windows": all(item["data_complete"] for item in results),
    }
    insufficient = len(windows) < cfg.minimum_windows or len(results) < cfg.minimum_windows or len(valid) < len(results)
    status = "insufficient_data" if insufficient else "passed" if all(gates.values()) else "failed"
    return {
        **base, "status": status, "windows": results, "executed_window_count": len(results),
        "summary": {
            "valid_window_count": len(valid), "outperform_window_count": len(wins),
            "outperform_ratio": round(len(wins) / len(valid), 6) if valid else 0.0,
            "aggregate_cost_after_return": round(aggregate_return - 1.0, 8),
            "aggregate_benchmark_return": round(aggregate_benchmark - 1.0, 8),
            "aggregate_excess_return": round(aggregate_return - aggregate_benchmark, 8),
            "worst_window_drawdown": round(worst_drawdown, 8), "total_fees": round(total_fees, 2),
            "covered_market_states": covered_states,
        },
        "promotion_gates": gates,
        "blocking_reasons": [name for name, passed in gates.items() if not passed] + (["available_history_cannot_form_five_windows"] if len(windows) < cfg.minimum_windows else []),
    }
