from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
import hashlib
import json
import math
import multiprocessing
import os
from statistics import mean
from typing import Any, Callable

from app.db.store import _conn
from app.services.backtest_data_source import BacktestDataSource
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_replay import run_strategy_replay


RESEARCH_SCHEMA_VERSION = "alis_continuous_factor_research_v2"
MAX_EXPERIMENTS_PER_SNAPSHOT = 40
MAX_GENERATIONS_PER_SNAPSHOT = 20
DEFAULT_CANDIDATES_PER_TASK = 1
RESEARCH_FOLD_SESSIONS = 15
RESEARCH_FOLD_COUNT = 2
QUICK_SCREEN_TARGET = 12
CONFIRMATION_ELITE_COUNT = 3
CONFIRMATION_FOLD_SESSIONS = 30
CONFIRMATION_FOLD_COUNT = 3
MAX_REPLAY_SECONDS = 900


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def ensure_continuous_research_tables() -> None:
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_research_snapshots (
               snapshot_id TEXT PRIMARY KEY,
               data_as_of TEXT NOT NULL,
               factor_universe_hash TEXT NOT NULL,
               factor_count INTEGER NOT NULL,
               research_windows_json TEXT NOT NULL,
               contract_json TEXT NOT NULL,
               created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_combination_experiments (
               experiment_id TEXT PRIMARY KEY,
               snapshot_id TEXT NOT NULL,
               generation INTEGER NOT NULL,
               parent_experiment_id TEXT,
               mutation_type TEXT NOT NULL,
               research_stage TEXT NOT NULL DEFAULT 'quick_screen',
               strategy_hash TEXT NOT NULL,
               factor_weights_json TEXT NOT NULL,
               windows_json TEXT NOT NULL,
               metrics_json TEXT NOT NULL,
               fitness REAL NOT NULL,
               status TEXT NOT NULL,
               error TEXT,
               created_at TEXT NOT NULL,
               finished_at TEXT
            )"""
        )
        if conn._is_postgres:
            column_exists = conn.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_name='factor_combination_experiments'
                     AND column_name='research_stage'"""
            ).fetchone()
        else:
            column_exists = next(
                (
                    row for row in conn.execute(
                        "PRAGMA table_info(factor_combination_experiments)"
                    ).fetchall()
                    if str(row["name"]) == "research_stage"
                ),
                None,
            )
        if not column_exists:
            conn.execute(
                "ALTER TABLE factor_combination_experiments ADD COLUMN research_stage TEXT NOT NULL DEFAULT 'quick_screen'"
            )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_factor_combination_experiments_snapshot
               ON factor_combination_experiments(snapshot_id,fitness,generation)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_research_population (
               snapshot_id TEXT NOT NULL,
               experiment_id TEXT NOT NULL,
               rank_no INTEGER NOT NULL,
               role TEXT NOT NULL,
               fitness REAL NOT NULL,
               evidence_json TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY(snapshot_id,experiment_id)
            )"""
        )


def _factor_universe() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT id,factor_key,version,status,name_cn,category,formula_json,
                          direction,window_sessions,leakage_status
                   FROM factor_definitions
                   WHERE status IN ('active','candidate')
                     AND leakage_status='passed'
                     AND category<>'交易约束'
                   ORDER BY category,factor_key,version"""
            ).fetchall()
        ]
    return [{
        **row,
        "id": int(row.get("id") or 0),
        "direction": int(row.get("direction") or 1),
        "window_sessions": int(row.get("window_sessions") or 1),
        "formula": _loads(row.pop("formula_json", "{}"), {}),
    } for row in rows]


def freeze_research_snapshot(data_source: BacktestDataSource | None = None) -> dict[str, Any]:
    """Freeze the historical search surface; closing data is not required."""
    ensure_continuous_research_tables()
    ds = data_source or BacktestDataSource()
    dates = ds.get_available_trade_dates("2000-01-01", datetime.now().date().isoformat())
    minimum = RESEARCH_FOLD_SESSIONS * RESEARCH_FOLD_COUNT + 1
    if len(dates) < minimum:
        raise RuntimeError("连续因子研究可用交易日不足")
    factors = _factor_universe()
    if len(factors) < 3:
        raise RuntimeError("连续因子研究至少需要三个可用因子")
    windows: list[dict[str, Any]] = []
    for offset in range(RESEARCH_FOLD_COUNT, 0, -1):
        end_index = len(dates) - 1 - (offset - 1) * RESEARCH_FOLD_SESSIONS
        start_index = end_index - RESEARCH_FOLD_SESSIONS + 1
        windows.append({
            "index": len(windows) + 1,
            "start": str(dates[start_index]),
            "end": str(dates[end_index]),
            "sessions": RESEARCH_FOLD_SESSIONS,
        })
    factor_identity = [{
        "id": item["id"], "factor_key": item["factor_key"], "version": item["version"],
        "status": item["status"], "formula": item["formula"],
    } for item in factors]
    factor_hash = _hash(factor_identity)
    contract = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "data_as_of": str(dates[-1]),
        "factor_universe_hash": factor_hash,
        "research_windows": windows,
        "cost_policy": "strategy_replay_a_share_cost_v1",
        "benchmark": "000300.SH",
        "search_use_only": True,
        "formal_oos_evidence": False,
        "trial_budget": MAX_EXPERIMENTS_PER_SNAPSHOT,
        "quick_screen": {
            "target": QUICK_SCREEN_TARGET,
            "fold_count": RESEARCH_FOLD_COUNT,
            "sessions_per_fold": RESEARCH_FOLD_SESSIONS,
        },
        "confirmation": {
            "elite_count": CONFIRMATION_ELITE_COUNT,
            "fold_count": CONFIRMATION_FOLD_COUNT,
            "sessions_per_fold": CONFIRMATION_FOLD_SESSIONS,
        },
        "factor_universe": factors,
    }
    snapshot_id = f"factor-research-{str(dates[-1]).replace('-', '')}-{_hash(contract)[:12]}"
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT snapshot_id FROM factor_research_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO factor_research_snapshots(
                   snapshot_id,data_as_of,factor_universe_hash,factor_count,
                   research_windows_json,contract_json,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot_id, str(dates[-1]), factor_hash, len(factors),
                    _json(windows), _json(contract), now,
                ),
            )
    return {
        "snapshot_id": snapshot_id,
        "data_as_of": str(dates[-1]),
        "factor_universe_hash": factor_hash,
        "factor_count": len(factors),
        "research_windows": windows,
        "contract": contract,
        "factors": factors,
    }


def _active_research_snapshot(
    data_source: BacktestDataSource | None = None,
) -> dict[str, Any]:
    """Reuse the current frozen epoch until its search budget is consumed."""
    ensure_continuous_research_tables()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM factor_research_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row:
        item = dict(row)
        contract = _loads(item.get("contract_json"), {})
        factors = contract.get("factor_universe") if isinstance(contract, dict) else None
        experiments = _experiment_rows(str(item["snapshot_id"]))
        max_generation = max(
            (int(experiment.get("generation") or 0) for experiment in experiments),
            default=-1,
        )
        if (
            isinstance(factors, list)
            and len(factors) >= 3
            and contract.get("schema_version") == RESEARCH_SCHEMA_VERSION
            and all(
                int(window.get("sessions") or 0) == RESEARCH_FOLD_SESSIONS
                for window in _loads(item.get("research_windows_json"), [])
            )
            and not _research_epoch_complete(experiments)
            and len(experiments) < MAX_EXPERIMENTS_PER_SNAPSHOT
            and max_generation < MAX_GENERATIONS_PER_SNAPSHOT
        ):
            return {
                "snapshot_id": str(item["snapshot_id"]),
                "data_as_of": str(item["data_as_of"]),
                "factor_universe_hash": str(item["factor_universe_hash"]),
                "factor_count": int(item["factor_count"]),
                "research_windows": _loads(item.get("research_windows_json"), []),
                "contract": contract,
                "factors": factors,
            }
    # Discovery expands the next epoch only. The currently running epoch never
    # changes its factor universe, preserving reproducible comparisons.
    from app.services.factor_discovery import run_open_factor_discovery_cycle

    run_open_factor_discovery_cycle(search_limit=1)
    return freeze_research_snapshot(data_source=data_source)


def _experiment_rows(snapshot_id: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT * FROM factor_combination_experiments
                   WHERE snapshot_id=? ORDER BY fitness DESC,created_at ASC""",
                (snapshot_id,),
            ).fetchall()
        ]
    for row in rows:
        row["factor_weights"] = _loads(row.pop("factor_weights_json", "{}"), {})
        row["windows"] = _loads(row.pop("windows_json", "[]"), [])
        row["metrics"] = _loads(row.pop("metrics_json", "{}"), {})
        row["generation"] = int(row.get("generation") or 0)
        row["fitness"] = float(row.get("fitness") or 0.0)
        row["research_stage"] = str(row.get("research_stage") or "quick_screen")
    return rows


def _research_epoch_complete(experiments: list[dict[str, Any]]) -> bool:
    quick = [
        item for item in experiments
        if item.get("research_stage") == "quick_screen"
        and item.get("status") == "completed"
    ]
    confirmation = [
        item for item in experiments
        if item.get("research_stage") == "confirmation"
        and item.get("status") in {"completed", "blocked_point_in_time", "failed"}
    ]
    return len(quick) >= QUICK_SCREEN_TARGET and len(confirmation) >= CONFIRMATION_ELITE_COUNT


def _normalized_weights(raw: dict[str, float]) -> dict[str, float]:
    cleaned = {str(key): float(value) for key, value in raw.items() if abs(float(value)) > 1e-12}
    total = sum(abs(value) for value in cleaned.values())
    return {
        key: round(value / total, 10)
        for key, value in sorted(cleaned.items())
    } if total > 0 else {}


def _seed_weights(factors: list[dict[str, Any]], index: int) -> dict[str, float]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for factor in factors:
        categories.setdefault(str(factor["category"]), []).append(factor)
    selected: list[dict[str, Any]] = []
    for category_index, category in enumerate(sorted(categories)):
        options = categories[category]
        selected.append(options[(index + category_index) % len(options)])
        if len(selected) >= 5:
            break
    if len(selected) < 3:
        selected = factors[index % len(factors):] + factors[:index % len(factors)]
        selected = selected[:min(5, len(selected))]
    return _normalized_weights({
        str(item["factor_key"]): float(item.get("direction") or 1)
        for item in selected
    })


def _mutate_weights(
    parent: dict[str, float],
    factors: list[dict[str, Any]],
    generation: int,
) -> tuple[dict[str, float], str]:
    universe = {str(item["factor_key"]): item for item in factors}
    mutation = generation % 4
    weights = dict(parent)
    if mutation == 0 and len(weights) > 3:
        weakest = min(weights, key=lambda key: (abs(weights[key]), key))
        weights.pop(weakest)
        mutation_type = "drop_weakest"
    elif mutation == 1:
        available = [key for key in sorted(universe) if key not in weights]
        if available:
            key = available[generation % len(available)]
            weights[key] = 0.25 * int(universe[key].get("direction") or 1)
        mutation_type = "add_novel_factor"
    elif mutation == 2 and weights:
        key = sorted(weights)[generation % len(weights)]
        weights[key] *= 1.5
        mutation_type = "tilt_existing_factor"
    else:
        available = [key for key in sorted(universe) if key not in weights]
        if available and weights:
            removed = sorted(weights)[generation % len(weights)]
            weights.pop(removed)
            replacement = available[generation % len(available)]
            weights[replacement] = 0.25 * int(universe[replacement].get("direction") or 1)
        mutation_type = "replace_factor"
    return _normalized_weights(weights), mutation_type


def propose_next_combinations(snapshot: dict[str, Any], *, count: int) -> list[dict[str, Any]]:
    existing = _experiment_rows(str(snapshot["snapshot_id"]))
    generation = max((int(item["generation"]) for item in existing), default=-1) + 1
    factors = list(snapshot["factors"])
    proposals: list[dict[str, Any]] = []
    seen_hashes = {str(item.get("strategy_hash") or "") for item in existing}
    parents = [item for item in existing if str(item.get("status") or "") == "completed"][:5]
    attempts = 0
    while len(proposals) < max(1, int(count)) and attempts < max(20, int(count) * 20):
        attempts += 1
        parent = parents[attempts % len(parents)] if parents else None
        if parent:
            weights, mutation_type = _mutate_weights(parent["factor_weights"], factors, generation + attempts)
            parent_id = str(parent["experiment_id"])
        else:
            weights = _seed_weights(factors, generation * max(1, int(count)) + attempts)
            mutation_type = "diverse_seed"
            parent_id = None
        strategy_hash = _hash({
            "snapshot_id": snapshot["snapshot_id"],
            "research_stage": "quick_screen",
            "weights": weights,
            "rebalance": {"freq": "weekly", "top_n": 30},
        })
        if len(weights) < 3 or strategy_hash in seen_hashes:
            continue
        seen_hashes.add(strategy_hash)
        proposals.append({
            "generation": generation,
            "parent_experiment_id": parent_id,
            "mutation_type": mutation_type,
            "research_stage": "quick_screen",
            "strategy_hash": strategy_hash,
            "factor_weights": weights,
        })
    return proposals


def _confirmation_windows(data_as_of: str) -> list[dict[str, Any]]:
    dates = BacktestDataSource().get_available_trade_dates("2000-01-01", data_as_of)
    required = CONFIRMATION_FOLD_SESSIONS * CONFIRMATION_FOLD_COUNT
    if len(dates) < required:
        raise RuntimeError("连续因子复筛可用交易日不足")
    windows: list[dict[str, Any]] = []
    for offset in range(CONFIRMATION_FOLD_COUNT, 0, -1):
        end_index = len(dates) - 1 - (offset - 1) * CONFIRMATION_FOLD_SESSIONS
        start_index = end_index - CONFIRMATION_FOLD_SESSIONS + 1
        windows.append({
            "index": len(windows) + 1,
            "start": str(dates[start_index]),
            "end": str(dates[end_index]),
            "sessions": CONFIRMATION_FOLD_SESSIONS,
        })
    return windows


def propose_confirmation_candidate(
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    rows = _experiment_rows(str(snapshot["snapshot_id"]))
    quick = [
        item for item in rows
        if item["research_stage"] == "quick_screen"
        and item["status"] == "completed"
    ]
    confirmed_parent_ids = {
        str(item.get("parent_experiment_id") or "")
        for item in rows if item["research_stage"] == "confirmation"
    }
    parent = next(
        (
            item for item in quick[:CONFIRMATION_ELITE_COUNT]
            if str(item["experiment_id"]) not in confirmed_parent_ids
        ),
        None,
    )
    if not parent:
        return None
    windows = _confirmation_windows(str(snapshot["data_as_of"]))
    strategy_hash = _hash({
        "snapshot_id": snapshot["snapshot_id"],
        "research_stage": "confirmation",
        "parent_experiment_id": parent["experiment_id"],
        "weights": parent["factor_weights"],
        "windows": windows,
    })
    return {
        "generation": int(parent["generation"]),
        "parent_experiment_id": str(parent["experiment_id"]),
        "mutation_type": "confirmation_replay",
        "research_stage": "confirmation",
        "strategy_hash": strategy_hash,
        "factor_weights": parent["factor_weights"],
        "research_windows": windows,
    }


def _fitness(folds: list[dict[str, Any]], *, trial_count: int) -> tuple[float, dict[str, Any]]:
    excess = [float(item.get("excess_return") or 0.0) for item in folds]
    returns = [float(item.get("total_return") or 0.0) for item in folds]
    drawdowns = [float(item.get("max_drawdown") or 0.0) for item in folds]
    fees = [float(item.get("total_fees") or 0.0) for item in folds]
    positive_ratio = sum(value > 0 for value in excess) / max(1, len(excess))
    mean_excess = mean(excess) if excess else -1.0
    variance = mean((value - mean_excess) ** 2 for value in excess) if excess else 0.0
    instability = math.sqrt(variance)
    multiple_testing_penalty = 0.0025 * math.sqrt(2.0 * math.log(max(1, trial_count)))
    fee_penalty = sum(fees) / 200_000.0 / max(1, len(fees))
    components = {
        "mean_excess": mean_excess,
        "absolute_return": 0.25 * (mean(returns) if returns else -1.0),
        "positive_window_bonus": 0.05 * positive_ratio,
        "drawdown": 0.20 * (min(drawdowns) if drawdowns else -1.0),
        "instability_penalty": -0.50 * instability,
        "multiple_testing_penalty": -multiple_testing_penalty,
        "fee_penalty": -fee_penalty,
    }
    score = sum(components.values())
    return score, {
        "mean_excess_return": round(mean_excess, 8),
        "mean_total_return": round(mean(returns) if returns else 0.0, 8),
        "worst_drawdown": round(min(drawdowns) if drawdowns else 0.0, 8),
        "positive_excess_window_ratio": round(positive_ratio, 8),
        "excess_instability": round(instability, 8),
        "multiple_testing_penalty": round(multiple_testing_penalty, 8),
        "trial_count": int(trial_count),
        "fitness": round(score, 8),
        "fitness_components": {
            key: round(value, 8) for key, value in components.items()
        },
    }


def _strategy_content(weights: dict[str, float]) -> dict[str, Any]:
    champion = resolve_production_strategy_champion()
    strategy = champion.get("strategy") if isinstance(champion.get("strategy"), dict) else {}
    content = deepcopy(strategy.get("content") if isinstance(strategy.get("content"), dict) else {})
    content["schema_version"] = "alis_continuous_factor_research_strategy_v1"
    content["score"] = {"normalization": "rank_percentile", "factors": weights}
    content["rebalance"] = {**(content.get("rebalance") or {}), "freq": "weekly", "top_n": 30}
    content["lookback_days"] = max(120, int(content.get("lookback_days") or 0))
    content["research_only"] = True
    return content


def _research_process_initializer() -> None:
    """Lower the OS scheduling priority of CPU-heavy exploratory replays."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def _isolated_replay_job(payload: dict[str, Any]) -> dict[str, Any]:
    replay = run_strategy_replay(
        strategy_id=None,
        start_date=str(payload["start"]),
        end_date=str(payload["end"]),
        strategy_content=payload["strategy_content"],
        strategy_name=str(payload["strategy_name"]),
    )
    return {
        "metrics": replay.get("metrics") or {},
        "summary": replay.get("summary") or {},
        "data_quality": {
            "point_in_time": ((replay.get("data_quality") or {}).get("point_in_time") or {})
        },
    }


def _run_isolated_replay(
    payload: dict[str, Any],
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    completed_folds: int,
    total_folds: int,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=context,
        initializer=_research_process_initializer,
    ) as executor:
        future = executor.submit(_isolated_replay_job, payload)
        waited = 0
        while True:
            try:
                return future.result(timeout=5)
            except FutureTimeout:
                waited += 5
                if progress_callback:
                    progress_callback({
                        "completed_folds": completed_folds,
                        "total_folds": total_folds,
                        "heartbeat_only": True,
                        "elapsed_seconds": waited,
                        "execution_isolation": "low_priority_process",
                    })
                if waited >= MAX_REPLAY_SECONDS:
                    future.cancel()
                    raise TimeoutError(
                        f"连续因子研究单折超过 {MAX_REPLAY_SECONDS} 秒资源预算"
                    )


def run_combination_experiment(
    snapshot: dict[str, Any],
    proposal: dict[str, Any],
    *,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    ensure_continuous_research_tables()
    experiment_id = f"factor-experiment-{proposal['strategy_hash'][:20]}"
    research_stage = str(proposal.get("research_stage") or "quick_screen")
    research_windows = proposal.get("research_windows") or snapshot["research_windows"]
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT * FROM factor_combination_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if existing:
            row = dict(existing)
            row["metrics"] = _loads(row.pop("metrics_json", "{}"), {})
            row["factor_weights"] = _loads(row.pop("factor_weights_json", "{}"), {})
            return row
        conn.execute(
            """INSERT INTO factor_combination_experiments(
               experiment_id,snapshot_id,generation,parent_experiment_id,mutation_type,
               research_stage,strategy_hash,factor_weights_json,windows_json,metrics_json,fitness,
               status,error,created_at,finished_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,0,'running',NULL,?,NULL)""",
            (
                experiment_id, snapshot["snapshot_id"], int(proposal["generation"]),
                proposal.get("parent_experiment_id"), proposal["mutation_type"],
                research_stage,
                proposal["strategy_hash"], _json(proposal["factor_weights"]),
                _json(research_windows), "{}", now,
            ),
        )
    folds: list[dict[str, Any]] = []
    try:
        content = _strategy_content(proposal["factor_weights"])
        for window_index, window in enumerate(research_windows, start=1):
            replay_payload = {
                "start": str(window["start"]),
                "end": str(window["end"]),
                "strategy_content": content,
                "strategy_name": f"连续因子研究 {experiment_id[-8:]}",
            }
            replay = (
                _run_isolated_replay(
                    replay_payload,
                    progress_callback=progress_callback,
                    completed_folds=window_index - 1,
                    total_folds=len(research_windows),
                )
                if replay_runner is run_strategy_replay
                else replay_runner(
                    strategy_id=None,
                    start_date=replay_payload["start"],
                    end_date=replay_payload["end"],
                    strategy_content=content,
                    strategy_name=replay_payload["strategy_name"],
                )
            )
            metrics = replay.get("metrics") if isinstance(replay.get("metrics"), dict) else {}
            summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
            folds.append({
                "window": window,
                "total_return": float(metrics.get("total_return") or 0.0),
                "benchmark_return": float(metrics.get("benchmark_return") or 0.0),
                "excess_return": float(metrics.get("excess_return") or 0.0),
                "max_drawdown": float(metrics.get("max_drawdown") or 0.0),
                "total_fees": float(summary.get("total_fees") or 0.0),
                "point_in_time_passed": bool(((replay.get("data_quality") or {}).get("point_in_time") or {}).get("passed")),
            })
            if progress_callback:
                progress_callback({
                    "experiment_id": experiment_id,
                    "completed_folds": window_index,
                    "total_folds": len(research_windows),
                })
        trial_count = len(_experiment_rows(str(snapshot["snapshot_id"]))) + 1
        fitness, aggregate = _fitness(folds, trial_count=trial_count)
        parent_id = proposal.get("parent_experiment_id")
        if parent_id:
            with _conn() as conn:
                parent = conn.execute(
                    """SELECT fitness,research_stage FROM factor_combination_experiments
                       WHERE experiment_id=? AND status='completed'""",
                    (str(parent_id),),
                ).fetchone()
            if parent:
                parent_fitness = float(parent["fitness"] or 0.0)
                aggregate["parent_fitness"] = round(parent_fitness, 8)
                aggregate["fitness_delta_vs_parent"] = round(fitness - parent_fitness, 8)
                comparable = str(parent["research_stage"] or "quick_screen") == research_stage
                aggregate["parent_comparison_is_like_for_like"] = comparable
                aggregate["mutation_verdict"] = (
                    "improved" if comparable and fitness > parent_fitness
                    else "degraded" if comparable
                    else "confirmation_revalidated"
                )
        pit_passed = all(item["point_in_time_passed"] for item in folds)
        status = "completed" if pit_passed else "blocked_point_in_time"
        metrics_payload = {
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "folds": folds,
            "aggregate": aggregate,
            "formal_oos_evidence": False,
            "research_stage": research_stage,
            "execution_isolation": (
                "low_priority_process"
                if replay_runner is run_strategy_replay else "in_process_test_runner"
            ),
        }
        with _conn() as conn:
            conn.execute(
                """UPDATE factor_combination_experiments
                   SET metrics_json=?,fitness=?,status=?,finished_at=?,error=NULL
                   WHERE experiment_id=?""",
                (_json(metrics_payload), fitness, status, _now(), experiment_id),
            )
        return {
            "experiment_id": experiment_id,
            "snapshot_id": snapshot["snapshot_id"],
            "generation": proposal["generation"],
            "parent_experiment_id": proposal.get("parent_experiment_id"),
            "mutation_type": proposal["mutation_type"],
            "research_stage": research_stage,
            "factor_weights": proposal["factor_weights"],
            "fitness": fitness,
            "status": status,
            "metrics": metrics_payload,
        }
    except Exception as exc:
        with _conn() as conn:
            conn.execute(
                """UPDATE factor_combination_experiments
                   SET status='failed',error=?,finished_at=? WHERE experiment_id=?""",
                (str(exc)[:2000], _now(), experiment_id),
            )
        raise


def _weight_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    keys = set(left) | set(right)
    numerator = sum(float(left.get(key, 0.0)) * float(right.get(key, 0.0)) for key in keys)
    left_norm = math.sqrt(sum(float(left.get(key, 0.0)) ** 2 for key in keys))
    right_norm = math.sqrt(sum(float(right.get(key, 0.0)) ** 2 for key in keys))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _diverse_population(
    rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[tuple[dict[str, Any], str]]:
    if not rows:
        return []
    selected: list[tuple[dict[str, Any], str]] = []
    used: set[str] = set()

    def add(row: dict[str, Any] | None, role: str) -> None:
        if row and str(row["experiment_id"]) not in used and len(selected) < limit:
            selected.append((row, role))
            used.add(str(row["experiment_id"]))

    confirmed = [row for row in rows if row["research_stage"] == "confirmation"]
    add(confirmed[0] if confirmed else None, "confirmed_elite")
    add(rows[0], "fitness_elite")
    add(
        max(
            rows,
            key=lambda row: float(
                ((row.get("metrics") or {}).get("aggregate") or {}).get("worst_drawdown")
                or -1.0
            ),
        ),
        "drawdown_elite",
    )
    add(
        min(
            rows,
            key=lambda row: float(
                ((row.get("metrics") or {}).get("aggregate") or {}).get("excess_instability")
                or 1.0
            ),
        ),
        "stability_elite",
    )
    for row in rows:
        if str(row["experiment_id"]) in used:
            continue
        maximum_similarity = max(
            (
                abs(_weight_similarity(row["factor_weights"], item["factor_weights"]))
                for item, _role in selected
            ),
            default=0.0,
        )
        if maximum_similarity < 0.90:
            add(row, "diversity_survivor")
    for row in rows:
        add(row, "fitness_survivor")
    return selected


def refresh_research_population(snapshot_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    eligible = [
        item for item in _experiment_rows(snapshot_id)
        if str(item.get("status") or "") == "completed"
    ]
    rows = _diverse_population(eligible, limit=max(1, int(limit)))
    now = _now()
    with _conn() as conn:
        conn.execute("DELETE FROM factor_research_population WHERE snapshot_id=?", (snapshot_id,))
        for index, (row, role) in enumerate(rows, start=1):
            evidence = {
                "strategy_hash": row["strategy_hash"],
                "generation": row["generation"],
                "research_stage": row["research_stage"],
                "metrics": row["metrics"],
            }
            conn.execute(
                """INSERT INTO factor_research_population(
                   snapshot_id,experiment_id,rank_no,role,fitness,evidence_json,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot_id, row["experiment_id"], index, role,
                    row["fitness"], _json(evidence), now,
                ),
            )
    return [{
        "experiment_id": row["experiment_id"],
        "rank": index,
        "role": role,
        "fitness": row["fitness"],
        "factor_weights": row["factor_weights"],
        "generation": row["generation"],
        "research_stage": row["research_stage"],
        "mutation_type": row["mutation_type"],
        "parent_experiment_id": row.get("parent_experiment_id"),
        "metrics": (row.get("metrics") or {}).get("aggregate") or {},
    } for index, (row, role) in enumerate(rows, start=1)]


def run_continuous_factor_research_cycle(
    *,
    candidates_per_task: int = DEFAULT_CANDIDATES_PER_TASK,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    snapshot = _active_research_snapshot()
    existing = _experiment_rows(str(snapshot["snapshot_id"]))
    completed_count = sum(str(item.get("status") or "") == "completed" for item in existing)
    quick_completed = [
        item for item in existing
        if item["research_stage"] == "quick_screen" and item["status"] == "completed"
    ]
    confirmation_attempts = [
        item for item in existing if item["research_stage"] == "confirmation"
    ]
    max_generation = max(
        (
            int(item.get("generation") or 0) for item in existing
            if item["research_stage"] == "quick_screen"
        ),
        default=-1,
    )
    if (
        _research_epoch_complete(existing)
        or len(existing) >= MAX_EXPERIMENTS_PER_SNAPSHOT
        or (
            max_generation >= MAX_GENERATIONS_PER_SNAPSHOT
            and len(quick_completed) < QUICK_SCREEN_TARGET
        )
    ):
        population = refresh_research_population(str(snapshot["snapshot_id"]))
        distillation = None
        if _research_epoch_complete(existing):
            from app.services.factor_strategy_evolution import (
                distill_confirmed_research_experiments,
            )

            distillation = distill_confirmed_research_experiments(
                snapshot_id=str(snapshot["snapshot_id"]),
                limit=CONFIRMATION_ELITE_COUNT,
            )
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "budget_exhausted",
            "snapshot": {key: value for key, value in snapshot.items() if key != "factors"},
            "experiment_count": len(existing),
            "completed_count": completed_count,
            "quick_screen_completed": len(quick_completed),
            "confirmation_completed": len(confirmation_attempts),
            "population": population,
            "distillation": distillation,
            "continue_research": bool(_research_epoch_complete(existing)),
            "rollover_epoch": bool(_research_epoch_complete(existing)),
        }
    if len(quick_completed) < QUICK_SCREEN_TARGET:
        proposals = propose_next_combinations(
            snapshot,
            count=max(1, min(int(candidates_per_task or 1), 3)),
        )
        current_stage = "quick_screen"
    else:
        confirmation = propose_confirmation_candidate(snapshot)
        proposals = [confirmation] if confirmation else []
        current_stage = "confirmation"
    results = [
        run_combination_experiment(
            snapshot,
            proposal,
            replay_runner=replay_runner,
            progress_callback=progress_callback,
        )
        for proposal in proposals
    ]
    population = refresh_research_population(str(snapshot["snapshot_id"]))
    total_count = len(_experiment_rows(str(snapshot["snapshot_id"])))
    latest_rows = _experiment_rows(str(snapshot["snapshot_id"]))
    epoch_complete = _research_epoch_complete(latest_rows)
    distillation = None
    if epoch_complete:
        from app.services.factor_strategy_evolution import (
            distill_confirmed_research_experiments,
        )

        distillation = distill_confirmed_research_experiments(
            snapshot_id=str(snapshot["snapshot_id"]),
            limit=CONFIRMATION_ELITE_COUNT,
        )
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "status": "completed",
        "snapshot": {key: value for key, value in snapshot.items() if key != "factors"},
        "experiments": results,
        "experiment_count": total_count,
        "current_stage": current_stage,
        "quick_screen_completed": sum(
            item["research_stage"] == "quick_screen" and item["status"] == "completed"
            for item in latest_rows
        ),
        "confirmation_completed": sum(
            item["research_stage"] == "confirmation" and item["status"] == "completed"
            for item in latest_rows
        ),
        "population": population,
        "distillation": distillation,
        "continue_research": (
            epoch_complete
            or (
                bool(proposals)
                and total_count < MAX_EXPERIMENTS_PER_SNAPSHOT
            )
        ),
        "rollover_epoch": epoch_complete,
        "next_generation": max((int(item.get("generation") or 0) for item in results), default=max_generation) + 1,
    }


def continuous_factor_research_status() -> dict[str, Any]:
    ensure_continuous_research_tables()
    with _conn() as conn:
        snapshot_row = conn.execute(
            "SELECT * FROM factor_research_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not snapshot_row:
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "not_started",
            "experiments": [],
            "population": [],
        }
    snapshot = dict(snapshot_row)
    snapshot["research_windows"] = _loads(snapshot.pop("research_windows_json", "[]"), [])
    snapshot["contract"] = _loads(snapshot.pop("contract_json", "{}"), {})
    experiments = _experiment_rows(str(snapshot["snapshot_id"]))
    population = refresh_research_population(str(snapshot["snapshot_id"]))
    quick_completed = sum(
        item["research_stage"] == "quick_screen" and item["status"] == "completed"
        for item in experiments
    )
    confirmation_completed = sum(
        item["research_stage"] == "confirmation" and item["status"] == "completed"
        for item in experiments
    )
    confirmation_running = any(
        item["research_stage"] == "confirmation" and item["status"] == "running"
        for item in experiments
    )
    completed_rows = [item for item in experiments if item["status"] == "completed"]
    confirmed_rows = [
        item for item in completed_rows if item["research_stage"] == "confirmation"
    ]
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "status": "budget_exhausted" if _research_epoch_complete(experiments) else "running",
        "current_stage": (
            "confirmation"
            if confirmation_running or quick_completed >= QUICK_SCREEN_TARGET
            else "quick_screen"
        ),
        "snapshot": snapshot,
        "experiment_count": len(experiments),
        "best_experiment": (
            confirmed_rows[0] if confirmed_rows
            else completed_rows[0] if completed_rows else None
        ),
        "quick_screen_completed": quick_completed,
        "quick_screen_target": QUICK_SCREEN_TARGET,
        "confirmation_completed": confirmation_completed,
        "confirmation_target": CONFIRMATION_ELITE_COUNT,
        "population": population,
    }
