from __future__ import annotations

import json
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.db.store import _conn as business_conn, get_account_baseline


_CENT = Decimal("0.01")


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0")


def _ledger_money(value: Any, *, allow_negative: bool = False, positive: bool = False) -> Decimal | None:
    try:
        parsed = Decimal(str(value if value is not None else 0))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if not parsed.is_finite() or (parsed < 0 and not allow_negative) or (positive and parsed <= 0):
        return None
    return parsed


def _quantity(value: Any, *, allow_negative: bool = False) -> int | None:
    try:
        parsed = Decimal(str(value if value is not None else 0))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value() or (parsed < 0 and not allow_negative):
        return None
    return int(parsed)


def _row_id(value: Any, fallback: int) -> int:
    parsed = _quantity(value)
    return parsed if parsed is not None else fallback


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def replay_simulation_ledger(
    operations: list[dict[str, Any]],
    corporate_actions: list[dict[str, Any]],
    *,
    baseline_cash: float,
    adjustments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Independently derive cash and quantities from immutable ledger rows."""
    quantities: dict[str, int] = {}
    invalid_operations: list[dict[str, Any]] = []
    baseline_value = _ledger_money(baseline_cash)
    if baseline_value is None:
        invalid_operations.append({
            "code": "invalid_baseline_cash",
            "field": "baseline_cash",
            "value": baseline_cash,
        })
        baseline_value = Decimal("0")
    cash = _round_money(baseline_value)
    total_fees = Decimal("0")
    buy_turnover = Decimal("0")
    sell_turnover = Decimal("0")

    timeline: list[tuple[str, int, int, dict[str, Any]]] = []
    for index, operation in enumerate(operations):
        timeline.append((
            str(operation.get("at") or ""),
            0,
            _row_id(operation.get("id"), index),
            {"kind": "operation", **operation},
        ))
    for index, action in enumerate(corporate_actions):
        timeline.append((
            f"{str(action.get('event_date') or '')}T15:00:00",
            1,
            _row_id(action.get("id"), index),
            {"kind": "corporate_action", **action},
        ))
    for index, adjustment in enumerate(adjustments or []):
        timeline.append((
            str(adjustment.get("adjusted_at") or ""),
            2,
            _row_id(adjustment.get("id"), index),
            {"kind": "adjustment", **adjustment},
        ))

    for _, _, _, item in sorted(timeline, key=lambda row: (row[0], row[1], row[2])):
        kind = str(item.get("kind") or "")
        symbol = _symbol(item.get("symbol"))
        if kind == "adjustment":
            quantity_delta = _quantity(item.get("quantity_delta"), allow_negative=True)
            if quantity_delta is None:
                invalid_operations.append({
                    "adjustment_id": item.get("id"),
                    "symbol": symbol,
                    "code": "invalid_adjustment_quantity",
                    "value": item.get("quantity_delta"),
                })
                continue
            cash_delta = _ledger_money(item.get("cash_delta"), allow_negative=True)
            if cash_delta is None:
                invalid_operations.append({
                    "adjustment_id": item.get("id"),
                    "symbol": symbol,
                    "code": "invalid_adjustment_cash",
                    "field": "cash_delta",
                    "value": item.get("cash_delta"),
                })
                continue
            cash = _round_money(cash + cash_delta)
            if symbol:
                quantities[symbol] = quantities.get(symbol, 0) + quantity_delta
            continue
        if not symbol:
            continue
        if kind == "corporate_action":
            action_type = str(item.get("action_type") or "")
            if action_type == "cash_dividend":
                cash_amount = _ledger_money(item.get("cash_amount"))
                if cash_amount is None:
                    invalid_operations.append({
                        "corporate_action_id": item.get("id"),
                        "symbol": symbol,
                        "code": "invalid_corporate_action_cash",
                        "field": "cash_amount",
                        "value": item.get("cash_amount"),
                    })
                    continue
                cash = _round_money(cash + cash_amount)
            elif action_type == "stock_dividend":
                share_quantity = _quantity(item.get("share_quantity"))
                if share_quantity is None:
                    invalid_operations.append({
                        "corporate_action_id": item.get("id"),
                        "symbol": symbol,
                        "code": "invalid_corporate_action_quantity",
                        "value": item.get("share_quantity"),
                    })
                    continue
                quantities[symbol] = quantities.get(symbol, 0) + share_quantity
            continue

        side = str(item.get("side") or "").upper()
        quantity = _quantity(item.get("quantity"))
        if quantity is None:
            invalid_operations.append({
                "operation_id": item.get("id"),
                "symbol": symbol,
                "code": "invalid_operation_quantity",
                "value": item.get("quantity"),
            })
            continue
        price = _ledger_money(item.get("price"), positive=True)
        fee_values = {
            name: _ledger_money(item.get(name))
            for name in ("commission", "transfer_fee", "stamp_tax")
        }
        invalid_field = "price" if price is None else next((name for name, value in fee_values.items() if value is None), None)
        if invalid_field:
            invalid_operations.append({
                "operation_id": item.get("id"),
                "symbol": symbol,
                "code": "invalid_operation_money",
                "field": invalid_field,
                "value": item.get(invalid_field),
            })
            continue
        amount = price * Decimal(quantity)
        fees = sum(fee_values.values(), Decimal("0"))
        total_fees += fees
        if side == "BUY":
            quantities[symbol] = quantities.get(symbol, 0) + quantity
            cash = _round_money(cash - amount - fees)
            buy_turnover += amount
        elif side == "SELL":
            available = quantities.get(symbol, 0)
            if quantity > available:
                invalid_operations.append({
                    "operation_id": item.get("id"),
                    "symbol": symbol,
                    "code": "oversell",
                    "available": available,
                    "quantity": quantity,
                })
            quantities[symbol] = available - quantity
            cash = _round_money(cash + amount - fees)
            sell_turnover += amount

    return {
        "schema_version": "alis_simulation_ledger_replay_v1",
        "baseline_cash": float(_round_money(baseline_value)),
        "cash": float(_round_money(cash)),
        "positions": {
            symbol: quantity
            for symbol, quantity in sorted(quantities.items())
            if quantity != 0
        },
        "operation_count": len(operations),
        "corporate_action_count": len(corporate_actions),
        "adjustment_count": len(adjustments or []),
        "total_fees": float(_round_money(total_fees)),
        "buy_turnover": float(_round_money(buy_turnover)),
        "sell_turnover": float(_round_money(sell_turnover)),
        "invalid_operations": invalid_operations,
    }


def _ensure_table() -> None:
    with business_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS simulation_ledger_adjustments (
                   id BIGSERIAL PRIMARY KEY,
                   adjustment_uid VARCHAR(64) NOT NULL UNIQUE,
                   adjusted_at VARCHAR(64) NOT NULL,
                   symbol VARCHAR(32) NOT NULL DEFAULT '',
                   cash_delta DOUBLE PRECISION NOT NULL DEFAULT 0,
                   quantity_delta INTEGER NOT NULL DEFAULT 0,
                   reason TEXT NOT NULL,
                   evidence_json TEXT NOT NULL DEFAULT '{}'
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS simulation_ledger_audits (
                   id BIGSERIAL PRIMARY KEY,
                   audited_at VARCHAR(64) NOT NULL,
                   status VARCHAR(24) NOT NULL,
                   baseline_cash DOUBLE PRECISION NOT NULL,
                   operation_count INTEGER NOT NULL,
                   corporate_action_count INTEGER NOT NULL,
                   expected_cash DOUBLE PRECISION NOT NULL,
                   actual_cash DOUBLE PRECISION NOT NULL,
                   cash_drift DOUBLE PRECISION NOT NULL,
                   expected_positions_json TEXT NOT NULL,
                   actual_positions_json TEXT NOT NULL,
                   position_deltas_json TEXT NOT NULL,
                   issues_json TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_simulation_ledger_audits_time
               ON simulation_ledger_audits(audited_at)"""
        )


def audit_simulation_ledger(*, persist: bool = True) -> dict[str, Any]:
    """Compare replayed ledger state with mutable account/position projections."""
    if persist:
        _ensure_table()
    with business_conn() as conn:
        operations = [
            dict(row) for row in conn.execute(
                """SELECT id,symbol,side,price,quantity,at,
                          COALESCE(commission,0) commission,
                          COALESCE(transfer_fee,0) transfer_fee,
                          COALESCE(stamp_tax,0) stamp_tax
                   FROM operations
                   WHERE UPPER(side) IN ('BUY','SELL')
                   ORDER BY at,id"""
            ).fetchall()
        ]
        actions = [
            dict(row) for row in conn.execute(
                """SELECT id,symbol,action_type,event_date,cash_amount,share_quantity
                   FROM simulation_corporate_actions
                   WHERE status='applied'
                   ORDER BY event_date,id"""
            ).fetchall()
        ]
        adjustments = [
            dict(row) for row in conn.execute(
                """SELECT id,adjustment_uid,adjusted_at,symbol,cash_delta,quantity_delta,reason
                   FROM simulation_ledger_adjustments
                   ORDER BY adjusted_at,id"""
            ).fetchall()
        ]
        actual_positions = {
            _symbol(row["symbol"]): int(row["quantity"] or 0)
            for row in conn.execute(
                "SELECT symbol,quantity FROM positions WHERE quantity<>0 ORDER BY symbol"
            ).fetchall()
        }
        account_row = conn.execute("SELECT balance FROM account WHERE id=1").fetchone()
    actual_cash = round(float((dict(account_row) if account_row else {}).get("balance") or 0), 2)
    replay = replay_simulation_ledger(
        operations,
        actions,
        baseline_cash=float(get_account_baseline()),
        adjustments=adjustments,
    )
    expected_positions = {
        symbol: int(quantity)
        for symbol, quantity in (replay.get("positions") or {}).items()
        if int(quantity) != 0
    }
    deltas = [
        {
            "symbol": symbol,
            "expected_quantity": int(expected_positions.get(symbol, 0)),
            "actual_quantity": int(actual_positions.get(symbol, 0)),
            "quantity_drift": int(actual_positions.get(symbol, 0)) - int(expected_positions.get(symbol, 0)),
        }
        for symbol in sorted(set(expected_positions) | set(actual_positions))
        if int(actual_positions.get(symbol, 0)) != int(expected_positions.get(symbol, 0))
    ]
    cash_drift = round(actual_cash - float(replay["cash"]), 2)
    issues: list[dict[str, Any]] = []
    if abs(cash_drift) > 0.05:
        issues.append({
            "code": "cash_drift",
            "message": f"账户现金与独立账本相差 {cash_drift:+.2f} 元",
            "expected": replay["cash"],
            "actual": actual_cash,
        })
    if deltas:
        issues.append({
            "code": "position_quantity_drift",
            "message": f"{len(deltas)} 个持仓数量与独立账本不一致",
            "items": deltas,
        })
    if replay["invalid_operations"]:
        issues.append({
            "code": "invalid_operation_sequence",
            "message": f"{len(replay['invalid_operations'])} 条账本记录存在数量、超卖或顺序异常",
            "items": replay["invalid_operations"],
        })
    status = "passed" if not issues else "failed"
    audited_at = datetime.now().astimezone().isoformat()
    result = {
        "ok": not issues,
        "schema_version": "alis_simulation_ledger_audit_v1",
        "status": status,
        "audited_at": audited_at,
        "baseline_cash": replay["baseline_cash"],
        "operation_count": replay["operation_count"],
        "corporate_action_count": replay["corporate_action_count"],
        "adjustment_count": replay["adjustment_count"],
        "expected_cash": replay["cash"],
        "actual_cash": actual_cash,
        "cash_drift": cash_drift,
        "expected_positions": expected_positions,
        "actual_positions": actual_positions,
        "position_deltas": deltas,
        "total_fees": replay["total_fees"],
        "invalid_operations": replay["invalid_operations"],
        "issues": issues,
        "auto_repair": False,
        "policy": "独立重算只告警和阻断新的自动成交，不自动篡改模拟账本。",
    }
    if persist:
        with business_conn() as conn:
            conn.execute(
                """INSERT INTO simulation_ledger_audits(
                       audited_at,status,baseline_cash,operation_count,corporate_action_count,
                       expected_cash,actual_cash,cash_drift,expected_positions_json,
                       actual_positions_json,position_deltas_json,issues_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    audited_at, status, replay["baseline_cash"], replay["operation_count"],
                    replay["corporate_action_count"], replay["cash"], actual_cash, cash_drift,
                    json.dumps(expected_positions, ensure_ascii=False, sort_keys=True),
                    json.dumps(actual_positions, ensure_ascii=False, sort_keys=True),
                    json.dumps(deltas, ensure_ascii=False, sort_keys=True),
                    json.dumps(issues, ensure_ascii=False, sort_keys=True),
                ),
            )
    return result


def reconcile_current_projection(*, reason: str, execute: bool = False) -> dict[str, Any]:
    """Register current drift as an explicit migration baseline without changing account state."""
    _ensure_table()
    before = audit_simulation_ledger(persist=False)
    if before.get("ok"):
        return {
            "ok": True,
            "executed": False,
            "status": "already_consistent",
            "audit": before,
        }
    proposal = {
        "cash_delta": round(float(before.get("cash_drift") or 0), 2),
        "positions": [
            {
                "symbol": str(item.get("symbol") or ""),
                "quantity_delta": int(item.get("quantity_drift") or 0),
            }
            for item in before.get("position_deltas") or []
        ],
        "reason": str(reason or "").strip(),
        "source_audit": {
            "audited_at": before.get("audited_at"),
            "operation_count": before.get("operation_count"),
            "corporate_action_count": before.get("corporate_action_count"),
            "expected_cash": before.get("expected_cash"),
            "actual_cash": before.get("actual_cash"),
            "issues": before.get("issues"),
        },
    }
    if not execute:
        return {
            "ok": True,
            "executed": False,
            "status": "preview",
            "proposal": proposal,
            "audit": before,
        }
    if not proposal["reason"]:
        return {"ok": False, "executed": False, "status": "reason_required", "proposal": proposal}
    adjusted_at = datetime.now().astimezone().isoformat()
    evidence_json = json.dumps(proposal["source_audit"], ensure_ascii=False, sort_keys=True)
    rows = [{"symbol": "", "cash_delta": proposal["cash_delta"], "quantity_delta": 0}]
    rows.extend({
        "symbol": item["symbol"],
        "cash_delta": 0.0,
        "quantity_delta": item["quantity_delta"],
    } for item in proposal["positions"])
    inserted = 0
    with business_conn() as conn:
        for item in rows:
            identity = json.dumps({
                "adjusted_at": adjusted_at,
                "symbol": item["symbol"],
                "cash_delta": item["cash_delta"],
                "quantity_delta": item["quantity_delta"],
                "reason": proposal["reason"],
            }, ensure_ascii=False, sort_keys=True)
            uid = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            existing = conn.execute(
                "SELECT id FROM simulation_ledger_adjustments WHERE adjustment_uid=?",
                (uid,),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO simulation_ledger_adjustments(
                       adjustment_uid,adjusted_at,symbol,cash_delta,quantity_delta,reason,evidence_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    uid, adjusted_at, item["symbol"], item["cash_delta"], item["quantity_delta"],
                    proposal["reason"], evidence_json,
                ),
            )
            inserted += 1
    after = audit_simulation_ledger(persist=True)
    return {
        "ok": bool(after.get("ok")),
        "executed": True,
        "status": "reconciled" if after.get("ok") else "still_inconsistent",
        "inserted": inserted,
        "proposal": proposal,
        "audit": after,
    }
