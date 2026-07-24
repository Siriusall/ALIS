"""Auditable continuous training and gated model governance.

The scheduler retrains only when fresh market data and the configured cadence
both permit it.  Every challenger uses the same validation gates as the API;
an unvalidated version is never promoted.  Training and governance decisions
are persisted in the runtime log for the governance page.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from app.db.store import (
    create_model_train_run,
    get_active_model_version,
    get_latest_model_train_run,
    list_model_versions,
    set_active_model_version,
    update_model_version_validation,
    update_model_version_status,
    update_model_train_run,
)
from app.services.model_registry import compare_champion_challenger
from app.services.market_context import materialize_market_training_history
from app.data_platform.services.runtime_logs import write_runtime_log

logger = logging.getLogger("quant.model_governance")
_lock = threading.Lock()
_started = False
_training_worker_running = False
BOOTSTRAP_MINIMUM_HISTORY_SESSIONS = 252


def bootstrap_candidate_eligible(version: dict[str, Any]) -> bool:
    """Allow one auditable initial champion before full production evidence exists."""
    validation = version.get("validation") or {}
    eligibility = validation.get("production_eligibility") or {}
    gates = eligibility.get("gates") or {}
    metrics = version.get("metrics") or {}
    sessions = int(eligibility.get("history_sessions", 0) or 0)
    return (
        str(validation.get("status") or "") == "passed"
        and bool(gates.get("artifact_exists"))
        and sessions >= BOOTSTRAP_MINIMUM_HISTORY_SESSIONS
        and float(metrics.get("lift", 0) or 0) > 0
        and float(metrics.get("walk_forward_lift", 0) or 0) > 0
        and int(metrics.get("walk_forward_folds", 0) or 0) >= 3
    )


def mark_bootstrap_promotion(version: dict[str, Any]) -> None:
    validation = dict(version.get("validation") or {})
    validation.update({
        "bootstrap_production_eligible": True,
        "promotion_policy": "bootstrap_offline_gates",
        "bootstrap_minimum_history_sessions": BOOTSTRAP_MINIMUM_HISTORY_SESSIONS,
    })
    update_model_version_validation(int(version["id"]), validation=validation)


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


def _latest_market_trade_date() -> str:
    from app.data_platform.db import _conn as data_conn

    with data_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM dc_available_trade_dates").fetchone()
    return str(dict(row).get("trade_date") or "") if row else ""


def _trained_through(version: dict[str, Any] | None) -> str:
    config = (version or {}).get("config") or {}
    training_input = config.get("training_input") or {}
    cross_section = training_input.get("cross_sectional_history") or {}
    explicit = str(cross_section.get("latest_trade_date") or "").replace("-", "")[:8]
    if len(explicit) == 8 and explicit.isdigit():
        return explicit
    value_range = list(training_input.get("range") or [])
    fallback = str(value_range[-1] if value_range else "").replace("-", "")[:8]
    return fallback if len(fallback) == 8 and fallback.isdigit() else ""


def automatic_retraining_decision(
    *,
    latest_run: dict[str, Any] | None,
    latest_version: dict[str, Any] | None,
    current_trade_date: str,
    now: datetime | None = None,
    retrain_interval_hours: int = 24 * 7,
    failed_retry_hours: int = 24,
    stale_running_hours: int = 12,
) -> dict[str, Any]:
    """Decide whether a new challenger is useful, without retraining identical data."""
    current = now or datetime.now()
    status = str((latest_run or {}).get("status") or "")
    updated = _parse_utc((latest_run or {}).get("updated_at"))
    age = current - updated if updated else None
    if status in {"running", "queued"}:
        if age is None or age < timedelta(hours=max(1, stale_running_hours)):
            return {"due": False, "reason": "training_in_progress", "run_id": (latest_run or {}).get("id")}
        return {"due": True, "reason": "stale_training_run", "stale_run_id": (latest_run or {}).get("id")}
    if status == "failed":
        if age is not None and age < timedelta(hours=max(1, failed_retry_hours)):
            return {"due": False, "reason": "failed_retry_cooldown"}
        return {"due": True, "reason": "retry_failed_training"}
    if latest_version is None:
        return {"due": True, "reason": "no_model_version"}
    trained_through = _trained_through(latest_version)
    market_date = str(current_trade_date or "").replace("-", "")[:8]
    if trained_through and (not market_date or market_date <= trained_through):
        return {
            "due": False,
            "reason": "no_new_market_data",
            "trained_through": trained_through,
            "current_trade_date": market_date,
        }
    reference = _parse_utc((latest_run or {}).get("finished_at")) or _parse_utc(latest_version.get("created_at"))
    if reference is not None and current - reference < timedelta(hours=max(1, retrain_interval_hours)):
        return {
            "due": False,
            "reason": "retrain_interval_not_reached",
            "trained_through": trained_through,
            "current_trade_date": market_date,
        }
    return {
        "due": True,
        "reason": "new_market_data_and_interval_reached",
        "trained_through": trained_through,
        "current_trade_date": market_date,
    }


def request_model_training(
    *, start_date: str | None = None, end_date: str | None = None,
    exchange_group: str = "HS", force: bool = False,
) -> dict[str, Any]:
    """Single-flight entry point shared by the scheduler and manual API."""
    global _training_worker_running
    with _lock:
        latest_run = get_latest_model_train_run()
        latest_versions = list_model_versions("main", limit=1)
        try:
            interval = max(24, int(os.getenv("ALIS_MODEL_RETRAIN_INTERVAL_HOURS", "168")))
            retry = max(1, int(os.getenv("ALIS_MODEL_FAILED_RETRY_HOURS", "24")))
            stale = max(2, int(os.getenv("ALIS_MODEL_TRAIN_STALE_HOURS", "12")))
        except ValueError:
            interval, retry, stale = 168, 24, 12
        decision = automatic_retraining_decision(
            latest_run=latest_run,
            latest_version=latest_versions[0] if latest_versions else None,
            current_trade_date=_latest_market_trade_date(),
            retrain_interval_hours=interval,
            failed_retry_hours=retry,
            stale_running_hours=stale,
        )
        if _training_worker_running:
            return {"ok": True, "started": False, "reason": "training_worker_running", "run_id": (latest_run or {}).get("id")}
        if not force and not bool(decision.get("due")):
            return {"ok": True, "started": False, **decision}
        if str((latest_run or {}).get("status") or "") in {"running", "queued"} and decision.get("reason") != "stale_training_run":
            return {"ok": True, "started": False, **decision}
        if decision.get("reason") == "stale_training_run" and latest_run:
            update_model_train_run(
                int(latest_run["id"]), status="failed", phase="failed",
                message="训练进程超时，已由自动治理标记失败", error="stale_training_run",
                finished_at=datetime.now().isoformat(),
            )
        run_id = create_model_train_run("自动持续训练任务已创建" if not force else "训练任务已创建")
        _training_worker_running = True

    def worker() -> None:
        global _training_worker_running
        try:
            from app.services.model_training import run_model_training
            run_model_training(run_id=run_id, start_date=start_date, end_date=end_date, exchange_group=exchange_group)
            # Evaluate and promote immediately after a successful challenger,
            # rather than waiting for the next daily scheduler tick.
            auto_tune_models_once(allow_retrain=False)
        except Exception:  # noqa: BLE001
            logger.exception("Automatic model training failed run_id=%s", run_id)
        finally:
            with _lock:
                _training_worker_running = False

    threading.Thread(target=worker, daemon=True, name=f"model-train-{run_id}").start()
    result = {"ok": True, "started": True, "run_id": run_id, "status": "running", **decision}
    try:
        write_runtime_log("INFO", "model-governance", "automatic model training started", result)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist automatic training trigger")
    return result


def auto_tune_models_once(*, allow_retrain: bool = True) -> dict[str, Any]:
    try:
        training_history = materialize_market_training_history()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to refresh market model training history")
        training_history = {"ok": False, "error": str(exc)[:300]}
    month_prefix = f"hsmm-shadow-{time.strftime('%Y%m')}"
    existing_hsmm = list_model_versions("market_regime", limit=20)
    if any(str(item.get("version") or "").startswith(month_prefix) for item in existing_hsmm):
        hsmm_shadow = {"status": "already_trained_this_month", "version": existing_hsmm[0].get("version") if existing_hsmm else None}
    else:
        try:
            from app.services.hsmm_shadow import train_hsmm_shadow
            hsmm_shadow = train_hsmm_shadow()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to train monthly HSMM shadow")
            hsmm_shadow = {"ok": False, "error": str(exc)[:300]}
    from app.services.review_learning import materialize_decision_outcomes, model_feedback_gate
    review_learning = materialize_decision_outcomes()
    champion = get_active_model_version("main")
    degraded_champion = None
    if champion:
        champion_feedback = model_feedback_gate(str(champion.get("version") or ""), strict_version=True)
        if champion_feedback.get("should_degrade"):
            update_model_version_status(int(champion["id"]), status="feedback_degraded", is_active=False)
            degraded_champion = {"id": champion.get("id"), "version": champion.get("version"), "review_feedback": champion_feedback}
            champion = None
    all_candidates = [x for x in list_model_versions("main", limit=50) if not x.get("is_active") and str(x.get("status")) in {"candidate", "watch"}]
    technically_eligible = [
        x for x in all_candidates
        if bool((x.get("validation") or {}).get("production_eligible"))
        and str((x.get("validation") or {}).get("status")) == "passed"
        and float((x.get("metrics") or {}).get("lift", 0) or 0) > 0
        and float((x.get("metrics") or {}).get("walk_forward_lift", 0) or 0) > 0
    ]
    # The first production model cannot have production feedback yet. Requiring
    # human reviews and mature 20-day outcomes here creates a bootstrap
    # deadlock. The initial champion therefore uses the full offline technical
    # gate; feedback remains mandatory for every later challenger promotion.
    candidates = [x for x in all_candidates if bootstrap_candidate_eligible(x)] if not champion else [
        x for x in technically_eligible
        if bool(model_feedback_gate(str(x.get("version") or "")).get("passed"))
    ]
    if not champion and candidates:
        winner = max(
            candidates,
            key=lambda item: float((item.get("metrics") or {}).get("lift", 0) or 0)
            + float((item.get("metrics") or {}).get("walk_forward_lift", 0) or 0),
        )
        mark_bootstrap_promotion(winner)
        set_active_model_version(int(winner["id"]))
        result = {
            "decision": "promoted_initial", "evaluated": len(candidates),
            "excluded": len(all_candidates) - len(candidates), "winner_id": winner.get("id"),
            "promotion_policy": "bootstrap_offline_gates",
        }
    elif not champion:
        result = {"decision": "no_champion", "evaluated": 0, "excluded": len(all_candidates)}
    else:
        evaluations = [compare_champion_challenger(champion, candidate) for candidate in candidates]
        winner = next((item for item in evaluations if item.get("decision") == "promote_challenger"), None)
        if winner:
            set_active_model_version(int(winner["challenger_id"]))
            result = {"decision": "promoted", "evaluated": len(evaluations), "excluded": len(all_candidates) - len(candidates), "winner": winner, "champion_id": champion.get("id")}
        else:
            result = {"decision": "keep_champion", "evaluated": len(evaluations), "excluded": len(all_candidates) - len(candidates), "evaluations": evaluations, "champion_id": champion.get("id")}
    result["market_training_history"] = training_history
    result["review_learning"] = review_learning
    result["degraded_champion"] = degraded_champion
    result["hsmm_shadow"] = hsmm_shadow
    result["retraining"] = request_model_training() if allow_retrain else {"started": False, "reason": "post_training_evaluation"}
    try:
        write_runtime_log("INFO", "model-governance", "model auto-tune tick completed", result)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist model governance tick")
    return result


def start_model_governance_scheduler() -> bool:
    global _started
    with _lock:
        if _started:
            return False
        _started = True
    enabled = str(os.getenv("ALIS_MODEL_GOVERNANCE_SCHEDULER_ENABLED", "1")).lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return False
    try:
        interval = max(300, int(os.getenv("ALIS_MODEL_GOVERNANCE_INTERVAL_SECONDS", "86400")))
    except ValueError:
        interval = 86400

    def loop() -> None:
        while True:
            try:
                auto_tune_models_once()
            except Exception:  # noqa: BLE001
                logger.exception("Model governance tick failed")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="model-governance-scheduler").start()
    return True
