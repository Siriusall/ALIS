"""
自动投研管道：候选池填充 → 投资逻辑生成 → 持仓状态同步
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.research_engine import build_stock_research, materialize_pool_research_snapshots
from app.services.thesis_store import generate_watch_pool_theses, generate_thesis_for_stock
from app.services.decision_store import get_position_state
from app.services.stock_pool_sync import reconcile_missing_position_states_from_confirmed_operations
from app.db.store import get_positions
from app.core.a_share_fees import is_exchange_etf


def run_daily_maintenance(
    *, max_candidates: int = 50, trade_date: str = "",
) -> dict[str, Any]:
    """Run full daily pipeline: research → pool → theses → position states."""
    now = datetime.now().isoformat(timespec="seconds")
    errors = []
    skipped_etf_research = 0
    market_research: dict[str, Any] = {}
    if trade_date:
        # Materialize the page-wide read models before per-company work. A
        # large company pool must not postpone themes and screening for hours.
        from app.data_platform.services.daily_update import _run_research_maintenance

        try:
            market_research = _run_research_maintenance(trade_date)
        except Exception as exc:  # noqa: BLE001
            market_research = {
                "ok": False, "status": "deferred", "error": str(exc)[:300],
            }
        if market_research.get("ok") is False:
            errors.append(f"market_research: {market_research.get('error') or market_research.get('status') or 'not_ok'}")
        for step in ("industry_states", "themes", "screen", "stage", "promote"):
            outcome = market_research.get(step)
            if isinstance(outcome, dict) and outcome.get("ok") is False:
                errors.append(f"{step}: {outcome.get('error') or outcome.get('reason') or outcome.get('status') or 'not_ok'}")

    # Step 1: Research on current positions
    positions = get_positions()
    research_count = 0
    for p in positions:
        code = str(p.get("symbol", "")).strip().upper()
        if is_exchange_etf(code):
            skipped_etf_research += 1
            continue
        candidates = [code] if code.endswith((".SH", ".SZ", ".BJ")) else [
            f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ"
        ]
        attempt_errors = []
        for candidate in candidates:
            try:
                r = build_stock_research(candidate, persist=True)
                if r.get("ok"):
                    research_count += 1
                    attempt_errors = []
                    break
                attempt_errors.append(f"research_{candidate}: {r.get('status') or r.get('error') or 'not_ok'}")
            except Exception as e:
                attempt_errors.append(f"research_{candidate}: {str(e)[:60]}")
        errors.extend(attempt_errors)

    # Step 2: Generate watch pool theses (this also fills the pool)
    try:
        pool_result = generate_watch_pool_theses(limit=max_candidates)
        pool_added = len(pool_result.get("generated_or_reviewed") or [])
        if pool_result.get("ok") is False:
            errors.append(f"pool_theses: {str(pool_result.get('error') or pool_result.get('status') or 'not_ok')[:100]}")
    except Exception as e:
        errors.append(f"pool_theses: {str(e)[:100]}")
        pool_added = 0

    # Step 3: Generate theses for current positions
    thesis_count = 0
    blocked_position_theses = 0
    for p in positions:
        code = str(p.get("symbol", "")).strip().upper()
        if is_exchange_etf(code):
            continue
        if not code.endswith((".SH", ".SZ", ".BJ")):
            code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ"
        try:
            r = generate_thesis_for_stock(code, force=True)
            if r.get("ok") or r.get("thesis_id"):
                thesis_count += 1
            elif str(r.get("reason") or "") == "risk_hard_veto":
                # A risk veto is the intended result for a held legacy/ST
                # security.  Preserve the veto in the thesis service, but do
                # not turn daily maintenance into an operational failure.
                blocked_position_theses += 1
            else:
                errors.append(f"position_thesis_{code}: {str(r.get('error') or r.get('status') or 'not_ok')[:80]}")
        except Exception as e:
            errors.append(f"position_thesis_{code}: {str(e)[:80]}")

    # Step 4: Audit position states without rewriting them.
    #
    # Position state is a factual lifecycle record.  Only the confirmed
    # execution synchronizer may advance ``watch -> ... -> holding``.  Daily
    # research must neither invent that evidence nor use the retired ``hold``
    # pseudo-state merely because a security is currently in the account.
    state_count = 0
    missing_position_states = 0
    state_reconciliation: dict[str, Any] = {}
    try:
        # Recover only absent lifecycle evidence from confirmed immutable
        # operations.  The reconciler is idempotent and cannot mutate the
        # account, positions, executions, or frozen plans.
        state_reconciliation = reconcile_missing_position_states_from_confirmed_operations()
    except Exception as e:
        errors.append(f"position_state_reconciliation: {str(e)[:80]}")
    for p in positions:
        sym = str(p.get("symbol", "")).strip().upper()
        if not sym.endswith((".SH", ".SZ", ".BJ")):
            sym = f"{sym}.SH" if sym.startswith(("5", "6", "9")) else f"{sym}.BJ" if sym.startswith(("4", "8")) else f"{sym}.SZ"
        try:
            if get_position_state(sym):
                state_count += 1
            else:
                missing_position_states += 1
        except Exception as e:
            errors.append(f"position_state_audit_{sym}: {str(e)[:80]}")

    return {
        "ok": not errors,
        "status": "completed" if not errors else "partial",
        "results": {
            "research": research_count,
            "pool_theses": pool_added,
            "position_theses": thesis_count,
            "blocked_position_theses": blocked_position_theses,
            "position_states": state_count,
            "missing_position_states": missing_position_states,
            "position_state_reconciliation": state_reconciliation,
            "skipped_etf_research": skipped_etf_research,
            "market_research": market_research,
        },
        "errors": errors[:10],
        "error_count": len(errors),
        "timestamp": now,
        "model_version": "daily_maintenance_v1",
    }
