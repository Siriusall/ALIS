from __future__ import annotations

from datetime import datetime
import json
import hashlib
from typing import Any

from app.db.store import _conn


POOL_TYPES = {"watch", "candidate", "core", "holding"}
POSITION_TRANSITIONS = {
    "excluded": {"watch"}, "watch": {"waiting_valuation", "excluded"},
    "waiting_valuation": {"trial", "watch"}, "trial": {"holding", "warning", "exit"},
    "holding": {"conviction", "warning", "reduce", "exit"},
    "conviction": {"holding", "warning", "reduce"}, "warning": {"holding", "reduce", "exit"},
    "reduce": {"holding", "exit"}, "exit": {"watch"},
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _decode(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    value = dict(row)
    for field in fields:
        try: value[field.removesuffix("_json")] = json.loads(value.pop(field) or "{}")
        except (TypeError, json.JSONDecodeError): value[field.removesuffix("_json")] = {}
    return value


def _decision_evidence_payload(payload: dict[str, Any], *, decision_time: str, operation_id: int | None, include_release: bool = True) -> dict[str, Any]:
    """Canonical, replayable representation of the frozen decision inputs."""
    result = {
        "ts_code": payload["ts_code"], "decision_type": payload["decision_type"],
        "decision_time": decision_time, "price": payload.get("price"),
        "thesis_id": payload.get("thesis_id"), "execution_id": payload.get("execution_id"), "operation_id": operation_id,
        "data_as_of_time": payload["data_as_of_time"], "model_version": payload["model_version"],
        "market_state": payload.get("market_state") or {}, "research_state": payload.get("research_state") or {},
        "valuation": payload.get("valuation") or {}, "portfolio_context": payload.get("portfolio_context") or {},
        "target_weight_contract": payload.get("target_weight_contract") or {},
        "reason": payload["reason"], "invalidation_conditions": payload.get("invalidation_conditions") or [],
    }
    if include_release:
        result["release_id"] = payload.get("release_id")
    return result


def _evidence_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def upsert_pool_member(*, ts_code: str, pool_type: str, reason: str) -> dict[str, Any]:
    if pool_type not in POOL_TYPES: raise ValueError("invalid_pool_type")
    now = _now()
    with _conn() as c:
        c.execute("""INSERT INTO stock_pool_memberships(ts_code,pool_type,reason,status,entered_at,updated_at) VALUES(?,?,?,'active',?,?)
                     ON CONFLICT(ts_code,pool_type) DO UPDATE SET reason=excluded.reason,status='active',updated_at=excluded.updated_at""", (ts_code, pool_type, reason, now, now))
        row = c.execute("SELECT * FROM stock_pool_memberships WHERE ts_code=? AND pool_type=?", (ts_code, pool_type)).fetchone()
    return dict(row)


def list_pool_members(pool_type: str = "", limit: int | None = None) -> list[dict[str, Any]]:
    with _conn() as c:
        params: tuple[Any, ...] = (pool_type,) if pool_type else ()
        where = "pool_type=? AND status='active'" if pool_type else "status='active'"
        sql = f"SELECT * FROM stock_pool_memberships WHERE {where} ORDER BY updated_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params += (max(1, min(int(limit), 500)),)
        rows = c.execute(sql, params).fetchall()
    return [dict(x) for x in rows]


def remove_pool_member(member_id: int) -> bool:
    with _conn() as c:
        row = c.execute("UPDATE stock_pool_memberships SET status='removed',updated_at=? WHERE id=? RETURNING id", (_now(), member_id)).fetchone()
    return bool(row)


def set_pool_member_status(*, ts_code: str, pool_type: str, status: str, reason: str = "") -> dict[str, Any] | None:
    """Keep pool transitions auditable without deleting historical membership rows."""
    if pool_type not in POOL_TYPES:
        raise ValueError("invalid_pool_type")
    if status not in {"active", "removed"}:
        raise ValueError("invalid_pool_status")
    with _conn() as c:
        row = c.execute(
            """UPDATE stock_pool_memberships
               SET status=?, reason=CASE WHEN ?='' THEN reason ELSE ? END, updated_at=?
               WHERE ts_code=? AND pool_type=? RETURNING *""",
            (status, reason, reason, _now(), ts_code, pool_type),
        ).fetchone()
    return dict(row) if row else None


def transition_pool_member(*, ts_code: str, from_pool: str, to_pool: str, reason: str) -> dict[str, Any]:
    """Move a membership between pool tiers without erasing its audit trail.

    This is deliberately an explicit, evidence-bearing transition.  The system
    may recommend a move, but it never upgrades a watch candidate or exits a
    live holding merely because a score changed.
    """
    if from_pool not in POOL_TYPES or to_pool not in POOL_TYPES or from_pool == to_pool:
        raise ValueError("invalid_pool_transition")
    now = _now()
    with _conn() as c:
        source = c.execute(
            "SELECT id FROM stock_pool_memberships WHERE ts_code=? AND pool_type=? AND status='active'",
            (ts_code, from_pool),
        ).fetchone()
        if not source:
            raise ValueError("source_pool_membership_not_active")
        c.execute(
            "UPDATE stock_pool_memberships SET status='removed', reason=?, updated_at=? WHERE id=?",
            (f"transition_to_{to_pool}: {reason}", now, int(dict(source)["id"])),
        )
        c.execute(
            """INSERT INTO stock_pool_memberships(ts_code,pool_type,reason,status,entered_at,updated_at)
               VALUES(?,?,?,'active',?,?)
               ON CONFLICT(ts_code,pool_type) DO UPDATE SET reason=excluded.reason,status='active',updated_at=excluded.updated_at""",
            (ts_code, to_pool, f"transition_from_{from_pool}: {reason}", now, now),
        )
        target = c.execute(
            "SELECT * FROM stock_pool_memberships WHERE ts_code=? AND pool_type=?", (ts_code, to_pool)
        ).fetchone()
    return dict(target)


def create_decision_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    decision_time = payload.get("decision_time") or _now()
    data_as_of = payload["data_as_of_time"]
    if str(data_as_of) > str(decision_time): raise ValueError("future_data_not_allowed")
    now = _now()
    with _conn() as c:
        operation_id = payload.get("operation_id")
        if not operation_id and payload.get("execution_id"):
            symbol = str(payload["ts_code"]).split(".")[0]
            operation = c.execute(
                """SELECT id FROM operations WHERE execution_id=? AND symbol=? AND UPPER(side)=? ORDER BY id DESC LIMIT 1""",
                (payload.get("execution_id"), symbol, str(payload["decision_type"]).upper()),
            ).fetchone()
            operation_id = dict(operation)["id"] if operation else None
        release_id = payload.get("release_id")
        if not release_id and payload.get("execution_id"):
            execution = c.execute("SELECT release_id FROM executions WHERE id=?", (payload.get("execution_id"),)).fetchone()
            release_id = dict(execution).get("release_id") if execution else None
        payload = {**payload, "release_id": release_id}
        evidence_payload = _decision_evidence_payload(payload, decision_time=decision_time, operation_id=operation_id)
        evidence_hash = _evidence_hash(evidence_payload)
        row = c.execute("""INSERT INTO decision_snapshots(ts_code,decision_type,decision_time,price,thesis_id,execution_id,operation_id,data_as_of_time,model_version,release_id,
                 market_state_json,research_state_json,valuation_json,portfolio_context_json,target_weight_contract_json,reason,invalidation_conditions_json,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""", (
            payload["ts_code"], payload["decision_type"], decision_time, payload.get("price"), payload.get("thesis_id"), payload.get("execution_id"), operation_id, data_as_of,
            payload["model_version"], release_id, json.dumps(payload.get("market_state") or {}, ensure_ascii=False), json.dumps(payload.get("research_state") or {}, ensure_ascii=False),
            json.dumps(payload.get("valuation") or {}, ensure_ascii=False), json.dumps(payload.get("portfolio_context") or {}, ensure_ascii=False),
            json.dumps(payload.get("target_weight_contract") or {}, ensure_ascii=False), payload["reason"],
            json.dumps(payload.get("invalidation_conditions") or [], ensure_ascii=False), now)).fetchone()
        c.execute("UPDATE decision_snapshots SET evidence_hash=?, evidence_schema_version=? WHERE id=?", (evidence_hash, "evidence_freeze_v3", dict(row)["id"]))
        row = c.execute("SELECT * FROM decision_snapshots WHERE id=?", (dict(row)["id"],)).fetchone()
    return _decode(row, ("market_state_json", "research_state_json", "valuation_json", "portfolio_context_json", "target_weight_contract_json", "invalidation_conditions_json"))


def verify_decision_snapshot(decision_snapshot_id: int) -> dict[str, Any] | None:
    """Verify a v2 snapshot against exactly the immutable fields it stores."""
    with _conn() as c:
        row = c.execute("SELECT * FROM decision_snapshots WHERE id=?", (int(decision_snapshot_id),)).fetchone()
    if not row:
        return None
    item = _decode(row, ("market_state_json", "research_state_json", "valuation_json", "portfolio_context_json", "target_weight_contract_json", "invalidation_conditions_json"))
    version = str(item.get("evidence_schema_version") or "")
    if version not in {"evidence_freeze_v2", "evidence_freeze_v3"}:
        return {
            "ok": False, "id": int(decision_snapshot_id), "verified": False,
            "status": "legacy_schema_unverifiable", "schema_version": version or "evidence_freeze_v1",
            "reason": "旧快照在执行关联解析前计算哈希，不能按当前落库字段做严格回验",
        }
    payload = _decision_evidence_payload(
        item, decision_time=str(item["decision_time"]), operation_id=item.get("operation_id"),
        include_release=version == "evidence_freeze_v3",
    )
    actual = _evidence_hash(payload)
    expected = str(item.get("evidence_hash") or "")
    return {
        "ok": actual == expected, "id": int(decision_snapshot_id), "verified": actual == expected,
        "status": "verified" if actual == expected else "integrity_mismatch", "schema_version": version,
        "expected_hash": expected, "actual_hash": actual,
    }


def get_decision_snapshot(decision_snapshot_id: int) -> dict[str, Any] | None:
    """Load one complete frozen decision for the selected audit record."""
    with _conn() as c:
        row = c.execute("SELECT * FROM decision_snapshots WHERE id=?", (int(decision_snapshot_id),)).fetchone()
    return _decode(row, ("market_state_json", "research_state_json", "valuation_json", "portfolio_context_json", "target_weight_contract_json", "invalidation_conditions_json")) if row else None


def _structured_diff(left: Any, right: Any, *, path: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Small deterministic diff for frozen JSON evidence, capped for API use."""
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        items: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            items.extend(_structured_diff(left.get(key), right.get(key), path=f"{path}.{key}" if path else str(key), limit=limit - len(items)))
            if len(items) >= limit:
                return items[:limit]
        return items
    if isinstance(left, list) and isinstance(right, list):
        items: list[dict[str, Any]] = []
        for index in range(max(len(left), len(right))):
            a = left[index] if index < len(left) else None
            b = right[index] if index < len(right) else None
            items.extend(_structured_diff(a, b, path=f"{path}[{index}]", limit=limit - len(items)))
            if len(items) >= limit:
                return items[:limit]
        return items
    return [{"path": path or "$", "left": left, "right": right}]


def diff_decision_snapshots(left_id: int, right_id: int) -> dict[str, Any] | None:
    """Compare two frozen decisions without reading mutable live state."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM decision_snapshots WHERE id IN (?,?)", (int(left_id), int(right_id))).fetchall()
    decoded = {
        int(dict(row).get("id")): _decode(row, ("market_state_json", "research_state_json", "valuation_json", "portfolio_context_json", "target_weight_contract_json", "invalidation_conditions_json"))
        for row in rows
    }
    left, right = decoded.get(int(left_id)), decoded.get(int(right_id))
    if not left or not right:
        return None
    fields = (
        "ts_code", "decision_type", "decision_time", "price", "data_as_of_time", "model_version",
        "market_state", "research_state", "valuation", "portfolio_context", "target_weight_contract", "reason", "invalidation_conditions",
    )
    changes: list[dict[str, Any]] = []
    for field in fields:
        changes.extend(_structured_diff(left.get(field), right.get(field), path=field, limit=200 - len(changes)))
        if len(changes) >= 200:
            break
    return {
        "ok": True, "left_id": int(left_id), "right_id": int(right_id),
        "changed": bool(changes), "change_count": len(changes), "changes": changes,
        "truncated": len(changes) >= 200,
    }


def list_decision_snapshots(ts_code: str = "", limit: int = 100) -> list[dict[str, Any]]:
    columns = "id,ts_code,decision_type,decision_time,price,thesis_id,execution_id,operation_id,data_as_of_time,model_version,market_state_json,research_state_json,valuation_json,portfolio_context_json,target_weight_contract_json,reason,invalidation_conditions_json,evidence_hash,evidence_schema_version,created_at"
    with _conn() as c:
        if ts_code: rows = c.execute(f"SELECT {columns} FROM decision_snapshots WHERE ts_code=? ORDER BY decision_time DESC,id DESC LIMIT ?", (ts_code,limit)).fetchall()
        else: rows = c.execute(f"SELECT {columns} FROM decision_snapshots ORDER BY decision_time DESC,id DESC LIMIT ?", (limit,)).fetchall()
    return [_decode(x,("market_state_json","research_state_json","valuation_json","portfolio_context_json","target_weight_contract_json","invalidation_conditions_json")) for x in rows]


def list_decision_snapshot_summaries(ts_code: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """List only fields needed to select an audit record in the console."""
    columns = "id,ts_code,decision_type,decision_time,price,thesis_id,execution_id,operation_id,data_as_of_time,model_version,reason,evidence_hash,evidence_schema_version,created_at"
    bounded = max(1, min(int(limit), 500))
    with _conn() as c:
        if ts_code:
            rows = c.execute(
                f"SELECT {columns} FROM decision_snapshots WHERE ts_code=? ORDER BY decision_time DESC,id DESC LIMIT ?",
                (ts_code, bounded),
            ).fetchall()
        else:
            rows = c.execute(
                f"SELECT {columns} FROM decision_snapshots ORDER BY decision_time DESC,id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
    return [dict(row) for row in rows]


def summarize_decision_audit(limit: int = 100) -> dict[str, Any]:
    """Return audit counters without transferring full frozen decision payloads."""
    bounded = max(1, min(int(limit), 500))
    with _conn() as c:
        decision_rows = c.execute(
            """SELECT id,data_as_of_time,decision_time,model_version,market_state_json,research_state_json
               FROM decision_snapshots ORDER BY decision_time DESC,id DESC LIMIT ?""",
            (bounded,),
        ).fetchall()
        review_rows = c.execute(
            "SELECT decision_snapshot_id FROM review_records ORDER BY review_time DESC,id DESC LIMIT ?",
            (bounded,),
        ).fetchall()
    decisions = [_decode(row, ("market_state_json", "research_state_json")) for row in decision_rows]
    review_values = [dict(row) for row in review_rows]
    reviewed_ids = {int(row["decision_snapshot_id"]) for row in review_values if row.get("decision_snapshot_id")}
    missing = [row for row in decisions if int(row.get("id") or 0) not in reviewed_ids]
    future_violations = [
        row for row in decisions
        if str(row.get("data_as_of_time") or "") > str(row.get("decision_time") or "")
    ]
    missing_evidence = [
        row for row in decisions
        if not row.get("market_state") or not row.get("research_state") or not row.get("model_version")
    ]
    return {
        "ok": not future_violations and not missing_evidence,
        "counts": {"decisions": len(decisions), "reviews": len(reviewed_ids), "awaiting_review": len(missing)},
        "quality": {"future_data_violations": len(future_violations), "missing_evidence": len(missing_evidence)},
        "compact": True,
    }


def create_review(payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    with _conn() as c:
        row = c.execute("""INSERT INTO review_records(decision_snapshot_id,review_time,outcome_json,attribution_json,error_type,lessons,created_at)
              VALUES(?,?,?,?,?,?,?) RETURNING *""", (payload.get("decision_snapshot_id"),payload.get("review_time") or now,json.dumps(payload.get("outcome") or {},ensure_ascii=False),json.dumps(payload.get("attribution") or {},ensure_ascii=False),payload.get("error_type"),payload.get("lessons") or "",now)).fetchone()
    return _decode(row,("outcome_json","attribution_json"))


def list_reviews(limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as c: rows=c.execute("SELECT id,decision_snapshot_id,review_time,outcome_json,attribution_json,error_type,lessons,created_at FROM review_records ORDER BY review_time DESC,id DESC LIMIT ?",(limit,)).fetchall()
    return [_decode(x,("outcome_json","attribution_json")) for x in rows]


def build_review_outcome_suggestion(decision_snapshot_id: int) -> dict[str, Any] | None:
    """Calculate an auditable post-decision price outcome from PostgreSQL snapshots.

    This is deliberately a *subsequent price return*, not claimed realized PnL:
    realized PnL needs matched execution lots and is left for the execution
    ledger. The distinction prevents a later price from rewriting what happened.
    """
    with _conn() as c:
        decision_row = c.execute("SELECT * FROM decision_snapshots WHERE id=?", (decision_snapshot_id,)).fetchone()
    if not decision_row:
        return None
    decision = _decode(decision_row, ("market_state_json", "research_state_json", "valuation_json", "portfolio_context_json", "target_weight_contract_json", "invalidation_conditions_json"))
    execution_outcome = _matched_execution_outcome(decision)
    if execution_outcome:
        return {"ok": True, "decision": decision, "outcome": execution_outcome, "attribution": _execution_attribution(decision, execution_outcome)}
    entry_price = float(decision.get("price") or 0)
    if entry_price <= 0:
        return {"ok": False, "reason": "decision_price_missing", "decision": decision}
    decision_date = str(decision.get("decision_time") or "").replace("-", "")[:8]
    try:
        from app.data_platform.db import _conn as data_conn
        with data_conn() as c:
            row = c.execute(
                """SELECT trade_date, close FROM dc_stock_daily_snapshot
                   WHERE ts_code=? AND trade_date>=? AND close IS NOT NULL
                   ORDER BY trade_date DESC LIMIT 1""",
                (decision["ts_code"], decision_date),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "postgresql_snapshot_unavailable", "error": str(exc)[:240], "decision": decision}
    if not row:
        return {"ok": False, "reason": "no_post_decision_price", "decision": decision}
    latest = dict(row)
    latest_price = float(latest.get("close") or 0)
    raw_return = latest_price / entry_price - 1.0
    direction = str(decision.get("decision_type") or "").lower()
    directional_return = -raw_return if direction == "sell" else raw_return
    return {
        "ok": True,
        "decision": decision,
        "outcome": {
            "metric": "subsequent_price_return",
            "entry_price": entry_price,
            "latest_price": latest_price,
            "latest_trade_date": str(latest.get("trade_date") or ""),
            "raw_price_return": round(raw_return, 6),
            "directional_return": round(directional_return, 6),
            "decision_type": direction,
            "note": "后续价格表现，不等同已实现收益或完整归因。",
        },
        "attribution": {
            "method": "postgresql_post_decision_price_v1",
            "market_attribution": None,
            "selection_attribution": None,
            "execution_attribution": None,
            "limitations": ["尚未匹配实际成交批次", "尚未接入基准指数归因"],
        },
    }


def _matched_execution_outcome(decision: dict[str, Any]) -> dict[str, Any] | None:
    operation_id = decision.get("operation_id")
    if not operation_id:
        return None
    with _conn() as c:
        operation_row = c.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not operation_row:
            return None
        operation = dict(operation_row)
        symbol = str(operation.get("symbol") or "")
        side = str(operation.get("side") or "").upper()
        quantity = int(operation.get("quantity") or 0)
        price = float(operation.get("price") or 0)
        at = str(operation.get("at") or "")
        if quantity <= 0 or price <= 0:
            return None
        fee_keys = ("commission", "transfer_fee", "stamp_tax")

        def _fees(row: dict[str, Any], used: int) -> float:
            row_qty = int(row.get("quantity") or 0)
            if row_qty <= 0 or used <= 0:
                return 0.0
            return sum(float(row.get(k) or 0) for k in fee_keys) * used / row_qty

        ledger = [dict(row) for row in c.execute(
            "SELECT * FROM operations WHERE symbol=? ORDER BY at,id", (symbol,)
        ).fetchall()]

        if side == "BUY":
            lots: list[dict[str, Any]] = []
            matched_operations: list[dict[str, Any]] = []
            proceeds = exit_fees = 0.0
            target_lot: dict[str, Any] | None = None
            for event in ledger:
                event_side = str(event.get("side") or "").upper()
                if event_side == "BUY":
                    lot = {"operation": event, "remaining": int(event.get("quantity") or 0)}
                    lots.append(lot)
                    if int(event.get("id") or 0) == int(operation_id):
                        target_lot = lot
                    continue
                if event_side != "SELL":
                    continue
                sell_remaining = int(event.get("quantity") or 0)
                for lot in lots:
                    if sell_remaining <= 0:
                        break
                    used = min(sell_remaining, int(lot["remaining"]))
                    if used <= 0:
                        continue
                    lot["remaining"] -= used
                    sell_remaining -= used
                    if lot is target_lot:
                        proceeds += used * float(event.get("price") or 0)
                        allocated_fee = _fees(event, used)
                        exit_fees += allocated_fee
                        matched_operations.append({
                            "operation_id": event.get("id"), "quantity": used,
                            "price": event.get("price"), "at": event.get("at"),
                            "fees": round(allocated_fee, 4),
                        })
            if target_lot is None:
                return None
            remaining = int(target_lot["remaining"])
            matched_quantity = quantity - remaining
            entry_fees = _fees(operation, matched_quantity)
            gross_cost = matched_quantity * price
            net_cost = gross_cost + entry_fees
            net_proceeds = proceeds - exit_fees
            realized_pnl = net_proceeds - net_cost
            return {
                "status": "realized" if matched_quantity == quantity else "partially_realized" if matched_quantity else "unrealized",
                "entry_operation_id": operation_id,
                "entry_execution_id": operation.get("execution_id"),
                "entry_price": price,
                "entry_at": at,
                "quantity": quantity,
                "matched_quantity": matched_quantity,
                "remaining_quantity": remaining,
                "matched_exit_operations": matched_operations,
                "entry_fees": round(entry_fees, 4), "exit_fees": round(exit_fees, 4),
                "gross_cost": round(gross_cost, 4), "net_proceeds": round(net_proceeds, 4),
                "realized_pnl": round(realized_pnl, 4),
                "realized_return": round(realized_pnl / net_cost, 6) if net_cost else None,
                "fee_schedule_version": operation.get("fee_schedule_version") or "",
            }
        if side == "SELL":
            lots: list[dict[str, Any]] = []
            matched_entries: list[dict[str, Any]] = []
            entry_cost = entry_fees = 0.0
            matched = 0
            for event in ledger:
                event_side = str(event.get("side") or "").upper()
                if event_side == "BUY":
                    lots.append({"operation": event, "remaining": int(event.get("quantity") or 0)})
                    continue
                if event_side != "SELL":
                    continue
                sell_remaining = int(event.get("quantity") or 0)
                is_target = int(event.get("id") or 0) == int(operation_id)
                for lot in lots:
                    if sell_remaining <= 0:
                        break
                    used = min(sell_remaining, int(lot["remaining"]))
                    if used <= 0:
                        continue
                    lot["remaining"] -= used
                    sell_remaining -= used
                    if is_target:
                        buy = lot["operation"]
                        allocated_fee = _fees(buy, used)
                        entry_cost += used * float(buy.get("price") or 0)
                        entry_fees += allocated_fee
                        matched += used
                        matched_entries.append({
                            "operation_id": buy.get("id"), "quantity": used,
                            "price": buy.get("price"), "at": buy.get("at"),
                            "fees": round(allocated_fee, 4),
                        })
                if is_target:
                    break
            if matched <= 0:
                return {"status": "exit_unmatched", "exit_operation_id": operation_id, "quantity": quantity, "matched_quantity": 0}
            exit_fees = _fees(operation, matched)
            net_cost = entry_cost + entry_fees
            net_proceeds = matched * price - exit_fees
            pnl = net_proceeds - net_cost
            return {
                "status": "realized" if matched == quantity else "partially_realized",
                "entry_operation_id": matched_entries[0]["operation_id"], "exit_operation_id": operation_id,
                "exit_at": at,
                "matched_entry_operations": matched_entries, "exit_price": price,
                "quantity": quantity, "matched_quantity": matched,
                "entry_fees": round(entry_fees, 4), "exit_fees": round(exit_fees, 4),
                "gross_cost": round(entry_cost, 4), "net_proceeds": round(net_proceeds, 4),
                "realized_pnl": round(pnl, 4),
                "realized_return": round(pnl / net_cost, 6) if net_cost else None,
                "fee_schedule_version": operation.get("fee_schedule_version") or "",
            }
    return None


def _market_and_industry_returns(symbol: str, entry_date: str, exit_date: str) -> tuple[float | None, float | None, str]:
    from app.data_platform.db import _conn as data_conn

    code = str(symbol or "").split(".")[0]
    ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
    with data_conn() as c:
        master = c.execute("SELECT industry FROM dc_stock_master WHERE ts_code=?", (ts_code,)).fetchone()
        industry = str(dict(master).get("industry") or "") if master else ""
        index_rows = [dict(row) for row in c.execute(
            """SELECT trade_date,payload_json FROM dc_tushare_raw_records
               WHERE api_name='index_daily' AND ts_code IN ('000300.SH','000300')
                 AND trade_date IN (?,?) ORDER BY trade_date""", (entry_date, exit_date),
        ).fetchall()]
        benchmark_by_date: dict[str, float] = {}
        for row in index_rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if payload.get("close") is not None:
                benchmark_by_date[str(row.get("trade_date") or "")] = float(payload["close"])
        industry_row = c.execute(
            """WITH entry_prices AS (
                   SELECT ts_code,close FROM dc_stock_daily_snapshot
                   WHERE trade_date=? AND industry=? AND close>0
               ), exit_prices AS (
                   SELECT ts_code,close FROM dc_stock_daily_snapshot
                   WHERE trade_date=? AND industry=? AND close>0
               ) SELECT AVG(x.close/e.close-1.0) industry_return
                 FROM entry_prices e JOIN exit_prices x USING(ts_code)""",
            (entry_date, industry, exit_date, industry),
        ).fetchone() if industry else None
    entry_bm, exit_bm = benchmark_by_date.get(entry_date), benchmark_by_date.get(exit_date)
    benchmark_return = exit_bm / entry_bm - 1.0 if entry_bm and exit_bm else None
    industry_return = float(dict(industry_row).get("industry_return")) if industry_row and dict(industry_row).get("industry_return") is not None else None
    return benchmark_return, industry_return, industry


def _execution_attribution(decision: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    if not outcome.get("matched_quantity"):
        return {"method": "brinson_fifo_execution_v1", "status": "unmatched", "limitations": ["没有可匹配成交批次"]}
    side = str(decision.get("decision_type") or "").upper()
    lots: list[dict[str, Any]] = []
    if side == "BUY":
        for exit_item in outcome.get("matched_exit_operations") or []:
            lots.append({
                "entry_operation_id": outcome.get("entry_operation_id"), "exit_operation_id": exit_item.get("operation_id"),
                "entry_price": float(outcome.get("entry_price") or 0), "exit_price": float(exit_item.get("price") or 0),
                "entry_at": str(outcome.get("entry_at") or ""), "exit_at": str(exit_item.get("at") or ""), "quantity": int(exit_item.get("quantity") or 0),
            })
    else:
        for entry_item in outcome.get("matched_entry_operations") or []:
            lots.append({
                "entry_operation_id": entry_item.get("operation_id"), "exit_operation_id": outcome.get("exit_operation_id"),
                "entry_price": float(entry_item.get("price") or 0), "exit_price": float(outcome.get("exit_price") or 0),
                "entry_at": str(entry_item.get("at") or ""), "exit_at": str(outcome.get("exit_at") or ""), "quantity": int(entry_item.get("quantity") or 0),
            })
    market_total = industry_total = selection_total = gross_total = 0.0
    missing_market = missing_industry = 0
    lot_results: list[dict[str, Any]] = []
    symbol = str(decision.get("ts_code") or "")
    for lot in lots:
        quantity = int(lot["quantity"])
        capital = float(lot["entry_price"]) * quantity
        stock_return = float(lot["exit_price"]) / float(lot["entry_price"]) - 1.0 if lot["entry_price"] else 0.0
        entry_date = str(lot["entry_at"])[:10].replace("-", "")
        exit_date = str(lot["exit_at"])[:10].replace("-", "")
        benchmark_return, industry_return, industry = _market_and_industry_returns(symbol, entry_date, exit_date)
        market_component = (benchmark_return or 0.0) * capital
        industry_component = ((industry_return or benchmark_return or 0.0) - (benchmark_return or 0.0)) * capital
        selection_component = (stock_return - (industry_return or benchmark_return or 0.0)) * capital
        if benchmark_return is None:
            missing_market += 1
        if industry_return is None:
            missing_industry += 1
        gross = stock_return * capital
        market_total += market_component; industry_total += industry_component; selection_total += selection_component; gross_total += gross
        lot_results.append({**lot, "industry": industry, "stock_return": round(stock_return, 8), "benchmark_return": benchmark_return, "industry_return": industry_return, "market_attribution": round(market_component, 4), "industry_attribution": round(industry_component, 4), "selection_attribution": round(selection_component, 4)})
    execution_cost = float(outcome.get("entry_fees") or 0) + float(outcome.get("exit_fees") or 0)
    attributed_net = market_total + industry_total + selection_total - execution_cost
    realized = float(outcome.get("realized_pnl") or 0)
    limitations = []
    if missing_market:
        limitations.append(f"{missing_market}个批次缺沪深300同期价格，市场贡献按0暂代")
    if missing_industry:
        limitations.append(f"{missing_industry}个批次缺行业同期横截面，行业贡献回退到市场")
    return {
        "method": "brinson_fifo_execution_v1", "status": "complete" if not limitations else "partial",
        "market_attribution": round(market_total, 4), "industry_allocation_attribution": round(industry_total, 4),
        "selection_attribution": round(selection_total, 4), "execution_attribution": round(-execution_cost, 4),
        "gross_pnl": round(gross_total, 4), "attributed_net_pnl": round(attributed_net, 4),
        "realized_net_pnl": round(realized, 4), "reconciliation_difference": round(attributed_net - realized, 4),
        "lots": lot_results, "limitations": limitations,
    }


def transition_position(
    *, ts_code: str, to_state: str, reason: str, thesis_id: int | None,
    evidence: list[dict[str, Any]], rule_version: str = "position_state_v1",
    model_version: str = "", automatic_decision: dict[str, Any] | None = None,
    intervention_type: str = "none", intervention_actor: str = "",
) -> dict[str, Any]:
    if intervention_type not in {"none", "pause", "override", "reject", "rollback", "constraint_change"}:
        raise ValueError("invalid_intervention_type")
    now=_now()
    with _conn() as c:
        old=c.execute("SELECT * FROM position_states WHERE ts_code=?",(ts_code,)).fetchone()
        from_state=str(dict(old).get("state")) if old else None
        if from_state is not None and to_state not in POSITION_TRANSITIONS.get(from_state,set()): raise ValueError("invalid_transition")
        if from_state is None and to_state not in {"excluded","watch"}: raise ValueError("initial_state_must_be_watch_or_excluded")
        c.execute("""INSERT INTO position_states(ts_code,state,thesis_id,reason,changed_at,updated_at) VALUES(?,?,?,?,?,?)
                     ON CONFLICT(ts_code) DO UPDATE SET state=excluded.state,thesis_id=excluded.thesis_id,reason=excluded.reason,changed_at=excluded.changed_at,updated_at=excluded.updated_at""",(ts_code,to_state,thesis_id,reason,now,now))
        c.execute(
            """INSERT INTO position_state_history
               (ts_code,from_state,to_state,reason,evidence_json,rule_version,model_version,
                automatic_decision_json,intervention_type,intervention_actor,changed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (ts_code,from_state,to_state,reason,json.dumps(evidence,ensure_ascii=False),rule_version,
             model_version,json.dumps(automatic_decision or {},ensure_ascii=False),
             intervention_type,intervention_actor,now),
        )
        row=c.execute("SELECT * FROM position_states WHERE ts_code=?",(ts_code,)).fetchone()
    return dict(row)


def list_position_state_history(ts_code: str = "", limit: int = 500) -> list[dict[str, Any]]:
    with _conn() as c:
        if ts_code:
            rows = c.execute(
                "SELECT * FROM position_state_history WHERE ts_code=? ORDER BY changed_at DESC,id DESC LIMIT ?",
                (ts_code, max(1, min(int(limit), 2000))),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM position_state_history ORDER BY changed_at DESC,id DESC LIMIT ?",
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
    return [_decode(row, ("evidence_json", "automatic_decision_json")) for row in rows]


def list_position_states() -> list[dict[str, Any]]:
    """Return the latest auditable state for each investment position."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM position_states ORDER BY updated_at DESC, ts_code").fetchall()
    return [dict(row) for row in rows]


def get_position_state(ts_code: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM position_states WHERE ts_code=?", (ts_code,)).fetchone()
    return dict(row) if row else None
