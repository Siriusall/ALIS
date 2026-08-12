"""Controlled candidate-to-stock-pool staging."""

from __future__ import annotations

from typing import Any

from app.core.a_share_fees import normalize_exchange_etf_ts_code
from app.db.store import list_operations
from app.services.decision_store import get_position_state, set_pool_member_status, transition_position, upsert_pool_member


def _ts_code(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if value.endswith((".SH", ".SZ", ".BJ")):
        return value
    etf = normalize_exchange_etf_ts_code(value)
    if etf.endswith((".SH", ".SZ")):
        return etf
    code = value.split(".", 1)[0].zfill(6)
    suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
    return f"{code}{suffix}"


def _apply_confirmed_operation_position_state(
    operation: dict[str, Any], *, remaining_quantity: int | None = None,
) -> list[str]:
    """Replay one confirmed ledger operation into the factual position state machine.

    The operation itself is immutable evidence.  This helper only fills a
    missing lifecycle record; it never changes cash, positions, executions,
    or the operation.  The history is recorded at reconciliation time while
    retaining the original operation timestamp in its evidence.
    """
    symbol = str(operation.get("symbol") or "")
    side = str(operation.get("side") or "").upper()
    if not symbol or side not in {"BUY", "SELL"}:
        return []
    code = _ts_code(symbol)
    state = get_position_state(code)
    current = str((state or {}).get("state") or "")
    if side == "BUY":
        if not current or current in {"exit", "excluded"}:
            path = ["watch", "waiting_valuation", "trial", "holding"]
        elif current == "watch":
            path = ["waiting_valuation", "trial", "holding"]
        elif current == "waiting_valuation":
            path = ["trial", "holding"]
        elif current == "trial" or current in {"warning", "reduce"}:
            path = ["holding"]
        else:
            path = []
    else:
        if current == "conviction":
            path = ["reduce"] if remaining_quantity is not None and remaining_quantity > 0 else ["reduce", "exit"]
        elif current in {"holding", "warning"}:
            path = ["reduce"] if remaining_quantity is not None and remaining_quantity > 0 else ["exit"]
        elif current == "reduce":
            path = [] if remaining_quantity is not None and remaining_quantity > 0 else ["exit"]
        else:
            path = []
    if not path:
        return []
    evidence = [{
        "source": "confirmed_execution_operation_reconciliation_v1",
        "execution_id": operation.get("execution_id"),
        "operation_id": operation.get("id"),
        "operation_at": operation.get("at"),
        "side": side,
        "price": operation.get("price"),
        "quantity": operation.get("quantity"),
    }]
    for next_state in path:
        transition_position(
            ts_code=code,
            to_state=next_state,
            reason=(
                "从已有确认成交补建持仓状态；"
                f"operation_id={operation.get('id')}; execution_id={operation.get('execution_id')}"
            ),
            thesis_id=None,
            evidence=evidence,
            rule_version="confirmed_execution_state_reconciliation_v1",
            automatic_decision={
                "source": "confirmed_execution_operation_reconciliation",
                "operation_id": operation.get("id"),
                "execution_id": operation.get("execution_id"),
                "side": side,
                "path": path,
            },
        )
    return path


def reconcile_missing_position_states_from_confirmed_operations(*, limit: int = 5000) -> dict[str, Any]:
    """Backfill only absent position-state evidence from immutable operations.

    This exists for executions created before the confirmed-execution state
    synchronizer was introduced.  It is idempotent: once a symbol reaches its
    replayed terminal state, later calls produce no new state history.
    """
    operations = list_operations(limit=max(1, min(int(limit), 5000)))
    operations.sort(key=lambda item: (str(item.get("at") or ""), int(item.get("id") or 0)))
    # This is a missing-evidence reconciler, not a state-history rewrite.
    # Capture eligibility before changing anything so a second call is a
    # strict no-op and an existing partial/manual lifecycle is never replaced.
    missing_state_codes = {
        _ts_code(str(operation.get("symbol") or ""))
        for operation in operations
        if str(operation.get("symbol") or "")
        and not get_position_state(_ts_code(str(operation.get("symbol") or "")))
    }
    replayed_operations = 0
    transitions: list[dict[str, Any]] = []
    quantities: dict[str, int] = {}
    for operation in operations:
        code = _ts_code(str(operation.get("symbol") or ""))
        if code not in missing_state_codes:
            continue
        quantity = max(0, int(operation.get("quantity") or 0))
        side = str(operation.get("side") or "").upper()
        before = quantities.get(code, 0)
        remaining_quantity: int | None = None
        if side == "BUY":
            quantities[code] = before + quantity
        elif side == "SELL":
            remaining_quantity = max(0, before - quantity)
            quantities[code] = remaining_quantity
        path = _apply_confirmed_operation_position_state(
            operation, remaining_quantity=remaining_quantity,
        )
        if path:
            replayed_operations += 1
            transitions.append({
                "operation_id": operation.get("id"),
                "ts_code": _ts_code(str(operation.get("symbol") or "")),
                "path": path,
            })
    return {
        "ok": True,
        "schema_version": "confirmed_execution_position_state_reconciliation_v1",
        "operations_examined": len(operations),
        "operations_replayed": replayed_operations,
        "state_transitions_created": sum(len(item["path"]) for item in transitions),
        "items": transitions,
        "policy": "state_evidence_only_no_account_position_execution_or_plan_mutation",
    }


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
        if not current or current in {"exit", "excluded"}:
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
