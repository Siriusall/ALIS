from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.data_platform.db import _conn
from app.data_platform.services.observation_clock import load_snapshot_market_aggregates
from app.db.store import get_model_version
from app.services.backtest_data_source import BacktestDataSource
from app.services.market_context import _transparent_regime, score_market_sentiment
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_replay import run_strategy_replay
from app.services.strategy_statistics import compare_strategy_windows


PAIRED_DAILY_RETURN_CONTRACT = "alis_oos_paired_daily_returns_v1"
MARKET_STATE_ATTRIBUTION_CONTRACT = "alis_oos_market_state_attribution_v2_immutable_snapshot"
MINIMUM_ANNUALIZED_RELATIVE_EXCESS_RETURN = 0.03
MINIMUM_MARKET_STATE_ROWS = 500
MINIMUM_MARKET_STATE_FIELD_RATIO = 0.80


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
    compact = sorted({
        str(value or "").replace("-", "")[:8]
        for value in dates
        if len(str(value or "").replace("-", "")[:8]) == 8
        and str(value or "").replace("-", "")[:8].isdigit()
    })
    if not compact:
        return {}
    try:
        with _conn() as conn:
            by_date = load_snapshot_market_aggregates(conn, trade_dates=compact)
    except Exception:  # noqa: BLE001 - OOS must fail closed through coverage below
        return {}
    if by_date is None:
        return {}
    amounts: list[float] = []
    previous = ""
    states: dict[str, str] = {}
    for trade_date in compact:
        row = dict(by_date.get(trade_date) or {})
        total = int(row.get("rows_total") or 0)
        market_rows = int(row.get("market_rows") or 0)
        if (
            total < MINIMUM_MARKET_STATE_ROWS
            or market_rows / total < MINIMUM_MARKET_STATE_FIELD_RATIO
        ):
            continue
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
        states[f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"] = state
        previous = state
        amounts.append(amount)
    return states


def _market_state_coverage(replay_dates: list[str], states: dict[str, str]) -> dict[str, Any]:
    expected = sorted({
        f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        for compact in (
            str(item or "").replace("-", "")[:8] for item in replay_dates
        )
        if len(compact) == 8 and compact.isdigit()
    })
    missing = [date for date in expected if str(states.get(date) or "") not in {"S0", "S1", "S2", "S3"}]
    return {
        "contract": MARKET_STATE_ATTRIBUTION_CONTRACT,
        "expected_sessions": len(expected),
        "available_sessions": len(expected) - len(missing),
        "missing_dates": missing,
        "passed": bool(expected) and not missing,
        "reason": None if expected and not missing else "immutable_snapshot_market_state_missing_or_incomplete",
    }


def _state_attribution(replay: dict[str, Any], states: dict[str, str]) -> dict[str, Any]:
    equity = replay.get("equity_series") or []
    benchmark_by_date = {
        str(item.get("date") or ""): float(item.get("total_value") or 0.0)
        for item in replay.get("benchmark_series") or []
    }
    grouped: dict[str, list[float]] = {state: [] for state in ("S0", "S1", "S2", "S3")}
    exposures: dict[str, list[float]] = {state: [] for state in ("S0", "S1", "S2", "S3")}
    cash_drags: dict[str, list[float]] = {state: [] for state in ("S0", "S1", "S2", "S3")}
    for previous, current in zip(equity, equity[1:]):
        prior = float(previous.get("total_value") or 0)
        state = states.get(str(current.get("date") or ""), "unknown")
        if prior > 0:
            grouped.setdefault(state, []).append(float(current.get("total_value") or 0) / prior - 1.0)
            cash_weight = max(0.0, min(1.0, float(previous.get("cash") or 0.0) / prior))
            exposures.setdefault(state, []).append(1.0 - cash_weight)
            previous_benchmark = benchmark_by_date.get(str(previous.get("date") or ""), 0.0)
            current_benchmark = benchmark_by_date.get(str(current.get("date") or ""), 0.0)
            if previous_benchmark > 0 and current_benchmark > 0:
                cash_drags.setdefault(state, []).append(max(0.0, cash_weight * (current_benchmark / previous_benchmark - 1.0)))
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
            "average_invested_weight": round(sum(exposures.get(state, [])) / len(exposures[state]), 8) if exposures.get(state) else None,
            "estimated_cash_drag_return": round(sum(cash_drags.get(state, [])), 8) if cash_drags.get(state) else None,
        }
    return result


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _paired_daily_returns(replay: dict[str, Any]) -> dict[str, Any]:
    """Freeze aligned, non-overlapping strategy and benchmark daily returns."""
    equity_by_date = {
        str(item.get("date") or ""): _number(item.get("total_value"))
        for item in replay.get("equity_series") or []
        if str(item.get("date") or "")
    }
    benchmark_by_date = {
        str(item.get("date") or ""): _number(item.get("total_value"))
        for item in replay.get("benchmark_series") or []
        if str(item.get("date") or "")
    }
    dates = sorted(set(equity_by_date) & set(benchmark_by_date))
    observations: list[dict[str, Any]] = []
    for previous_date, current_date in zip(dates, dates[1:]):
        previous_equity = equity_by_date[previous_date]
        current_equity = equity_by_date[current_date]
        previous_benchmark = benchmark_by_date[previous_date]
        current_benchmark = benchmark_by_date[current_date]
        if min(previous_equity, current_equity, previous_benchmark, current_benchmark) <= 0:
            continue
        observations.append({
            "date": current_date,
            "strategy_return": round(current_equity / previous_equity - 1.0, 10),
            "benchmark_return": round(current_benchmark / previous_benchmark - 1.0, 10),
        })
    expected_count = max(0, len(dates) - 1)
    return {
        "schema_version": PAIRED_DAILY_RETURN_CONTRACT,
        "status": "complete" if observations and len(observations) == expected_count else "insufficient_data",
        "aligned_equity_points": len(dates),
        "observation_count": len(observations),
        "observations": observations,
    }


def _failure_attribution(replay: dict[str, Any], states: dict[str, str]) -> dict[str, Any]:
    """Diagnose a failed OOS replay from replay evidence, without inventing factor attribution."""
    equity = replay.get("equity_series") or []
    benchmark_by_date = {
        str(item.get("date") or ""): _number(item.get("total_value"))
        for item in replay.get("benchmark_series") or []
    }
    cash_weights: list[float] = []
    positive_cash_drags: list[float] = []
    negative_cash_support: list[float] = []
    state_cash_drags: dict[str, float] = {}
    aligned_sessions = 0
    for previous, current in zip(equity, equity[1:]):
        prior_equity = _number(previous.get("total_value"))
        if prior_equity <= 0:
            continue
        cash_weight = max(0.0, min(1.0, _number(previous.get("cash")) / prior_equity))
        cash_weights.append(cash_weight)
        previous_benchmark = benchmark_by_date.get(str(previous.get("date") or ""), 0.0)
        current_benchmark = benchmark_by_date.get(str(current.get("date") or ""), 0.0)
        if previous_benchmark <= 0 or current_benchmark <= 0:
            continue
        aligned_sessions += 1
        contribution = cash_weight * (current_benchmark / previous_benchmark - 1.0)
        state = states.get(str(current.get("date") or ""), "unknown")
        if contribution >= 0:
            positive_cash_drags.append(contribution)
            state_cash_drags[state] = state_cash_drags.get(state, 0.0) + contribution
        else:
            negative_cash_support.append(-contribution)

    blocked = replay.get("blocked_events") or []
    blocked_by_code: dict[str, int] = {}
    for event in blocked:
        code = str((event or {}).get("code") or "unknown")
        blocked_by_code[code] = blocked_by_code.get(code, 0) + 1
    summary = replay.get("summary") or {}
    metrics = replay.get("metrics") or {}
    initial_equity = _number(replay.get("initial_cash")) or _number((equity or [{}])[0].get("total_value"))
    total_fees = _number(summary.get("total_fees"))
    fee_burden = total_fees / initial_equity if initial_equity > 0 else None
    excess_return = _number(metrics.get("excess_return"))
    failure_gap = max(0.0, -excess_return)
    cash_drag = sum(positive_cash_drags)
    blocked_count = sum(blocked_by_code.values())
    decision_count = int(_number(summary.get("decision_count")))
    blocked_per_decision = blocked_count / decision_count if decision_count > 0 else None
    limitations: list[str] = []
    if len(equity) < 2:
        limitations.append("缺少至少两个净值观察点，无法分析仓位与收益路径。")
    if aligned_sessions == 0:
        limitations.append("缺少按交易日对齐的基准净值，无法量化闲置现金的基准拖累。")
    if "blocked_events" not in replay:
        limitations.append("回放未提供被拦截订单明细；执行约束只能标记为未观测。")
    limitations.append("未接入历史行业基准、行业成分与风格因子时点数据；选股与配置仅报告为扣除已知项后的残差，不等同于 Brinson 或因子归因。")

    known_drag = cash_drag + (fee_burden or 0.0)
    residual_gap = max(0.0, failure_gap - known_drag)
    primary_cause = "unknown"
    confidence = "low"
    if failure_gap <= 0:
        limitations.append("本窗口成本后超额收益非负，因此不输出失败主因。")
    elif aligned_sessions and cash_drag >= max(0.01, failure_gap * 0.40) and (sum(cash_weights) / len(cash_weights) if cash_weights else 0.0) >= 0.15:
        primary_cause = "market_exposure"
        confidence = "high" if cash_drag >= failure_gap * 0.60 else "medium"
    elif fee_burden is not None and fee_burden >= max(0.005, failure_gap * 0.40):
        primary_cause = "transaction_costs"
        confidence = "high" if fee_burden >= failure_gap * 0.60 else "medium"
    elif blocked_per_decision is not None and blocked_count >= 3 and blocked_per_decision >= 1.0:
        primary_cause = "execution_constraints"
        confidence = "medium"
    elif failure_gap > 0 and aligned_sessions:
        primary_cause = "selection_or_allocation_residual"
        confidence = "medium" if aligned_sessions and len(equity) >= 10 else "low"

    return {
        "schema_version": "alis_oos_failure_attribution_v1",
        "status": "complete" if aligned_sessions and len(equity) >= 2 else "partial",
        "primary_cause": primary_cause,
        "confidence": confidence,
        "evidence": {
            "cost_after_excess_return": round(excess_return, 8),
            "failure_gap_return": round(failure_gap, 8),
            "average_cash_weight": round(sum(cash_weights) / len(cash_weights), 8) if cash_weights else None,
            "average_invested_weight": round(1.0 - sum(cash_weights) / len(cash_weights), 8) if cash_weights else None,
            "aligned_benchmark_sessions": aligned_sessions,
            "estimated_cash_drag_return": round(cash_drag, 8),
            "estimated_cash_support_return": round(sum(negative_cash_support), 8),
            "estimated_cash_drag_by_market_state": {key: round(value, 8) for key, value in sorted(state_cash_drags.items())},
            "total_fees": round(total_fees, 2),
            "fee_burden_return": round(fee_burden, 8) if fee_burden is not None else None,
            "trade_count": int(_number(summary.get("trade_count"))),
            "turnover_rate": round(_number(metrics.get("turnover_rate")), 8),
            "blocked_count": blocked_count,
            "blocked_per_decision": round(blocked_per_decision, 8) if blocked_per_decision is not None else None,
            "blocked_by_code": dict(sorted(blocked_by_code.items())),
            "residual_failure_gap_return": round(residual_gap, 8),
        },
        "limitations": limitations,
    }


def _aggregate_failure_attribution(windows: list[dict[str, Any]]) -> dict[str, Any]:
    attributed_windows = [item for item in windows if isinstance(item.get("failure_attribution"), dict)]
    attributions = [item["failure_attribution"] for item in attributed_windows]
    causes: dict[str, int] = {}
    for attribution in attributions:
        cause = str(attribution.get("primary_cause") or "unknown")
        causes[cause] = causes.get(cause, 0) + 1
    primary_cause = max(causes, key=lambda cause: (causes[cause], cause != "unknown")) if causes else "unknown"
    return {
        "schema_version": "alis_oos_failure_attribution_v1",
        "window_count": len(attributions),
        "primary_cause": primary_cause,
        "cause_counts": dict(sorted(causes.items())),
        "confidence": "high" if causes.get(primary_cause, 0) >= 3 else "medium" if causes.get(primary_cause, 0) >= 2 else "low",
        "windows": [
            {"index": item.get("index"), "primary_cause": attribution.get("primary_cause"), "confidence": attribution.get("confidence")}
            for item, attribution in zip(attributed_windows, attributions)
        ],
    }


def run_purged_walk_forward_validation(
    *, start_date: str | None = None, end_date: str | None = None,
    config: WalkForwardConfig | None = None, max_windows: int | None = None,
    strategy_id: int | None = None,
    strategy_controller_version_id: int | None = None,
    factor_bindings: dict[str, Any] | None = None,
    model_bindings: dict[str, Any] | None = None,
    market_state_binding: dict[str, Any] | None = None,
    research_package_hash: str | None = None,
    execute: bool = True, data_source: BacktestDataSource | None = None,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    state_loader: Callable[[list[str]], dict[str, str]] = _market_states,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    checkpoint_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cfg = config or WalkForwardConfig()
    champion = resolve_production_strategy_champion()
    controller_version_id = int(
        strategy_controller_version_id
        or champion.get("strategy_controller_version_id")
        or 0
    )
    controller_version_name = str(
        champion.get("strategy_controller_version") or ""
    )
    if strategy_controller_version_id is not None:
        controller_version = get_model_version(controller_version_id)
        if (
            not controller_version
            or str(controller_version.get("model_key") or "")
            != "strategy_controller"
        ):
            raise ValueError(
                "strategy_controller_version_id 不存在或不是策略控制器候选"
            )
        controller_version_name = str(controller_version.get("version") or "")
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
        "strategy_controller_version_id": controller_version_id,
        "strategy_controller_version": controller_version_name,
        "factor_bindings": (
            factor_bindings if factor_bindings is not None
            else champion.get("factor_bindings") or {}
        ),
        "model_bindings": (
            model_bindings if model_bindings is not None
            else champion.get("model_bindings") or {}
        ),
        "market_state_binding": (
            market_state_binding if market_state_binding is not None
            else champion.get("market_state_binding") or {}
        ),
        "research_package_hash": research_package_hash,
        "protocol": {
            **cfg.__dict__,
            "split_policy": "rolling_origin_with_fixed_train_span",
            "estimator_policy": "frozen_champion_no_refit_inside_evaluation",
            "cost_policy": "A-share fees and slippage included by strategy replay",
            "market_state_attribution_contract": MARKET_STATE_ATTRIBUTION_CONTRACT,
            "minimum_annualized_relative_excess_return": MINIMUM_ANNUALIZED_RELATIVE_EXCESS_RETURN,
        },
        "available_sessions": len(dates), "available_window_count": len(windows),
        "required_window_count": cfg.minimum_windows,
    }
    if not execute:
        return {**base, "status": "preview", "capacity_status": "ready" if len(windows) >= cfg.minimum_windows else "insufficient_data", "windows": selected}
    selected_indexes = {int(item["index"]) for item in selected}
    results = [
        dict(item) for item in (checkpoint_results or [])
        if (
            int(item.get("index") or 0) in selected_indexes
            and item.get("market_state_data_contract") == MARKET_STATE_ATTRIBUTION_CONTRACT
        )
    ]
    completed_indexes = {int(item.get("index") or 0) for item in results}
    for window in selected:
        if int(window["index"]) in completed_indexes:
            continue
        test = window["test"]
        def replay_progress(event: dict[str, Any]) -> None:
            if not progress_callback:
                return
            replay_progress_value = float(event.get("progress") or 0.0)
            progress_callback({
                "phase": "formal_oos_replay",
                "completed_windows": len(results),
                "total_windows": len(selected),
                "window_index": int(window["index"]),
                "progress": round((len(results) + replay_progress_value) / max(1, len(selected)), 6),
                "message": str(event.get("message") or f"正式 OOS 窗口 {window['index']} 重放中"),
                "checkpoint_results": results,
            })

        replay = replay_runner(
            strategy_id=strategy_id,
            strategy_controller_version_id=strategy_controller_version_id,
            factor_bindings=factor_bindings,
            model_bindings=model_bindings,
            market_state_binding=market_state_binding,
            research_package_hash=research_package_hash,
            start_date=test["start"], end_date=test["end"], data_source=BacktestDataSource(),
            progress_callback=replay_progress,
        )
        replay_dates = [str(item.get("date") or "") for item in replay.get("equity_series") or []]
        states = state_loader(replay_dates)
        market_state_coverage = _market_state_coverage(replay_dates, states)
        benchmark_quality = (
            (replay.get("data_quality") or {}).get("benchmark")
            if isinstance(replay.get("data_quality"), dict)
            else {}
        )
        benchmark_data_complete = bool(
            isinstance(benchmark_quality, dict)
            and benchmark_quality.get("passed") is True
        )
        window_result = {
            **window,
            "metrics": replay.get("metrics") or {},
            "summary": replay.get("summary") or {},
            "data_complete": (
                int((replay.get("summary") or {}).get("trading_days") or 0)
                >= int(test["sessions"])
                and int((replay.get("summary") or {}).get("decision_count") or 0) > 0
                and benchmark_data_complete
            ),
            "benchmark_data_complete": benchmark_data_complete,
            "benchmark_data_quality": benchmark_quality,
            "point_in_time_passed": bool(((replay.get("data_quality") or {}).get("point_in_time") or {}).get("passed")),
            "production_strategy_version": replay.get("production_strategy_version"),
            "market_state_data_contract": MARKET_STATE_ATTRIBUTION_CONTRACT,
            "market_state_data_complete": bool(market_state_coverage["passed"]),
            "market_state_data_quality": market_state_coverage,
            "market_state_attribution": _state_attribution(replay, states),
            "failure_attribution": _failure_attribution(replay, states),
            "paired_daily_returns": _paired_daily_returns(replay),
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
    valid = [
        item for item in results
        if item["data_complete"]
        and item["point_in_time_passed"]
        and item.get("market_state_data_complete") is True
        and item["production_strategy_version"] == champion["champion_version"]
    ]
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
    effective_test_sessions = sum(int((item.get("test") or {}).get("sessions") or 0) for item in valid)
    relative_wealth = aggregate_return / aggregate_benchmark if aggregate_benchmark > 0 else 0.0
    annualized_relative_excess = (
        relative_wealth ** (252.0 / effective_test_sessions) - 1.0
        if relative_wealth > 0 and effective_test_sessions > 0 else -1.0
    )
    gates = {
        "minimum_required_windows": len(valid) >= cfg.minimum_windows,
        "majority_windows_outperform": bool(valid) and len(wins) / len(valid) >= 0.60,
        "aggregate_cost_after_excess_positive": aggregate_return > aggregate_benchmark,
        "annualized_relative_excess_at_least_3pct": annualized_relative_excess >= MINIMUM_ANNUALIZED_RELATIVE_EXCESS_RETURN,
        "all_four_market_states_covered": set(covered_states) == {"S0", "S1", "S2", "S3"},
        "market_state_data_all_windows": all(
            item.get("market_state_data_complete") is True for item in results
        ),
        "point_in_time_all_passed": all(item["point_in_time_passed"] for item in results),
        "same_champion_all_windows": all(item["production_strategy_version"] == champion["champion_version"] for item in results),
        "complete_test_data_all_windows": all(item["data_complete"] for item in results),
        "benchmark_data_all_windows": all(
            item.get("benchmark_data_complete") is True for item in results
        ),
    }
    statistics = compare_strategy_windows(valid)
    failure_attribution = _aggregate_failure_attribution(valid)
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
            "annualized_relative_excess_return": round(annualized_relative_excess, 8),
            "minimum_annualized_relative_excess_return": MINIMUM_ANNUALIZED_RELATIVE_EXCESS_RETURN,
            "effective_test_sessions": effective_test_sessions,
            "worst_window_drawdown": round(worst_drawdown, 8), "total_fees": round(total_fees, 2),
            "covered_market_states": covered_states,
        },
        "promotion_gates": gates,
        "statistics": statistics,
        "failure_attribution": failure_attribution,
        "blocking_reasons": [name for name, passed in gates.items() if not passed] + (["available_history_cannot_form_five_windows"] if len(windows) < cfg.minimum_windows else []),
    }
