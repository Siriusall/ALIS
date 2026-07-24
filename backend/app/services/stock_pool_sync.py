"""Controlled candidate-to-stock-pool staging."""

from __future__ import annotations

from typing import Any

from app.services.decision_store import get_position_state, set_pool_member_status, transition_position, upsert_pool_member


def _ts_code(symbol: str) -> str:
    code = str(symbol or "").strip().split(".", 1)[0].zfill(6)
    suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
    return f"{code}{suffix}"


def stage_candidates_in_watch_pool(candidates: list[Any]) -> dict[str, Any]:
    """Write a reproducible watch-pool admission record for scored candidates.

    This intentionally stages only the watch pool. Core/holding transitions
    remain evidence- and thesis-gated human decisions.
    """
    staged: list[dict[str, Any]] = []
    for candidate in candidates:
        symbol = str(getattr(candidate, "symbol", "") or (candidate.get("symbol") if isinstance(candidate, dict) else ""))
        if not symbol:
            continue
        get = (lambda key, default=None: getattr(candidate, key, default)) if not isinstance(candidate, dict) else candidate.get
        confidence = float(get("confidence", 0) or 0)
        reason = f"candidate_screen_v1: confidence={confidence:.4f}; price={float(get('price', 0) or 0):.4f}; change_pct={float(get('change_pct', 0) or 0):.4f}"
        item = upsert_pool_member(ts_code=_ts_code(symbol), pool_type="watch", reason=reason)
        staged.append(item)
    return {"ok": True, "pool_type": "watch", "staged": len(staged), "items": staged}


def sync_pool_from_confirmed_execution(
    *, buy_orders: list[dict[str, Any]], sell_orders: list[dict[str, Any]], execution_id: int | None, executed_at: str
) -> dict[str, Any]:
    """Reflect confirmed simulated execution in the three-tier stock pool.

    A confirmed buy enters the holding pool with execution evidence. A confirmed
    sell removes only the holding membership; it deliberately does not invent a
    new watch/core recommendation.
    """
    entered: list[dict[str, Any]] = []
    exited: list[dict[str, Any]] = []
    state_entered: list[dict[str, Any]] = []
    state_exited: list[dict[str, Any]] = []
    evidence_prefix = f"confirmed_execution_v1: execution_id={execution_id or 'n/a'}; executed_at={executed_at}"
    for order in buy_orders:
        symbol = str(order.get("symbol") or "")
        if not symbol:
            continue
        item = upsert_pool_member(
            ts_code=_ts_code(symbol), pool_type="holding",
            reason=f"{evidence_prefix}; side=buy; price={order.get('exec_price')}; quantity={order.get('quantity')}; score={order.get('score')}",
        )
        entered.append(item)
        code = _ts_code(symbol)
        evidence = [{"source": "confirmed_execution_v1", "execution_id": execution_id, "executed_at": executed_at, "side": "buy", "price": order.get("exec_price"), "quantity": order.get("quantity")}]
        state = get_position_state(code)
        current = str((state or {}).get("state") or "")
        # A confirmed buy is a factual position event.  When research has not
        # recorded an earlier state, materialize the mandatory path rather
        # than inserting an invalid holding state directly.
        path = []
        if not current:
            path = ["watch", "waiting_valuation", "trial", "holding"]
        elif current == "watch":
            path = ["waiting_valuation", "trial", "holding"]
        elif current == "waiting_valuation":
            path = ["trial", "holding"]
        elif current == "trial":
            path = ["holding"]
        elif current in {"warning", "reduce"}:
            path = ["holding"]
        for next_state in path:
            transition_position(
                ts_code=code, to_state=next_state, reason=f"{evidence_prefix}; confirmed_buy",
                thesis_id=None, evidence=evidence, rule_version="confirmed_execution_state_v1",
                automatic_decision={"source": "confirmed_execution", "execution_id": execution_id, "side": "BUY", "path": path},
            )
        if path:
            state_entered.append({"ts_code": code, "from_state": current or "uninitialized", "to_state": "holding", "path": path})
    for order in sell_orders:
        if str(order.get("status") or "") not in {"filled", "executed", "simulated"}:
            continue
        symbol = str(order.get("symbol") or "")
        if not symbol:
            continue
        item = set_pool_member_status(
            ts_code=_ts_code(symbol), pool_type="holding", status="removed",
            reason=f"{evidence_prefix}; side=sell; reason={order.get('message') or ''}",
        )
        if item:
            exited.append(item)
        code = _ts_code(symbol)
        evidence = [{"source": "confirmed_execution_v1", "execution_id": execution_id, "executed_at": executed_at, "side": "sell", "reason": order.get("message") or ""}]
        state = get_position_state(code)
        current = str((state or {}).get("state") or "")
        exit_path = []
        if current == "conviction":
            exit_path = ["reduce", "exit"]
        elif current in {"holding", "warning", "reduce"}:
            exit_path = ["exit"]
        for next_state in exit_path:
            transition_position(
                ts_code=code, to_state=next_state, reason=f"{evidence_prefix}; confirmed_sell",
                thesis_id=None, evidence=evidence, rule_version="confirmed_execution_state_v1",
                automatic_decision={"source": "confirmed_execution", "execution_id": execution_id, "side": "SELL", "path": exit_path},
            )
        if exit_path:
            state_exited.append({"ts_code": code, "from_state": current, "to_state": "exit", "path": exit_path})
    return {"ok": True, "entered_holding": entered, "removed_holding": exited, "position_states_entered": state_entered, "position_states_exited": state_exited}
