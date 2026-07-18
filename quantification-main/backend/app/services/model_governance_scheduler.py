"""Durable-ish, auditable model governance tick.

The scheduler only invokes the same gated challenger evaluation exposed by the
API.  It never trains or promotes an unvalidated version and records each tick
in the runtime log for the governance page.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from app.db.store import get_active_model_version, list_model_versions, set_active_model_version
from app.services.model_registry import compare_champion_challenger
from app.services.market_context import materialize_market_training_history
from app.data_platform.services.runtime_logs import write_runtime_log

logger = logging.getLogger("quant.model_governance")
_lock = threading.Lock()
_started = False


def auto_tune_models_once() -> dict[str, Any]:
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
    champion = get_active_model_version("main")
    all_candidates = [x for x in list_model_versions("main", limit=50) if not x.get("is_active") and str(x.get("status")) in {"candidate", "watch"}]
    candidates = [
        x for x in all_candidates
        if bool((x.get("validation") or {}).get("production_eligible"))
        and str((x.get("validation") or {}).get("status")) == "passed"
        and float((x.get("metrics") or {}).get("lift", 0) or 0) > 0
        and float((x.get("metrics") or {}).get("walk_forward_lift", 0) or 0) > 0
    ]
    if not champion:
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
    result["hsmm_shadow"] = hsmm_shadow
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
