"""Persistence and deterministic audit for isolated candidate simulations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


LEDGER_CONTRACT = "alis_candidate_isolated_simulation_ledger_v1"


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def persist_candidate_replay(model_version_id: int, replay: dict[str, Any]) -> dict[str, Any]:
    """Replace one candidate's derived ledger atomically from replay evidence."""
    from app.db.store import _conn, get_model_version

    version = get_model_version(int(model_version_id))
    if not version or bool(version.get("is_active")):
        raise ValueError("isolated candidate ledger requires an inactive candidate version")
    component = replay.get("component_contract") if isinstance(replay.get("component_contract"), dict) else {}
    data_quality = replay.get("data_quality") if isinstance(replay.get("data_quality"), dict) else {}
    contract = {
        "schema_version": LEDGER_CONTRACT,
        "model_version_id": int(model_version_id),
        "model_key": str(version.get("model_key") or ""),
        "candidate_version": str(version.get("version") or ""),
        "component_contract": component,
        "research_package_hash": replay.get("research_package_hash"),
        "initial_cash": float(replay.get("initial_cash") or 200000.0),
    }
    contract_hash = _stable_hash(contract)
    equity = [item for item in replay.get("equity_series") or [] if isinstance(item, dict)]
    decisions = [item for item in replay.get("decisions") or [] if isinstance(item, dict)]
    trades = [item for item in replay.get("trades") or [] if isinstance(item, dict)]
    blocked = [item for item in replay.get("blocked_events") or [] if isinstance(item, dict)]
    latest = equity[-1] if equity else {}
    pit = data_quality.get("point_in_time") if isinstance(data_quality.get("point_in_time"), dict) else {}
    pit_passed = bool(pit.get("passed"))
    now = _now()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM candidate_simulation_accounts WHERE model_version_id=? AND contract_hash=?",
            (int(model_version_id), contract_hash),
        ).fetchone()
        if row:
            account_id = int(dict(row)["id"])
            conn.execute(
                """UPDATE candidate_simulation_accounts SET status='observing',latest_cash=?,
                          latest_equity=?,latest_trade_date=?,data_contract_json=?,updated_at=? WHERE id=?""",
                (float(latest.get("cash") or 0), float(latest.get("total_value") or 0),
                 str(latest.get("date") or ""), json.dumps(contract, ensure_ascii=False), now, account_id),
            )
        else:
            conn.execute(
                """INSERT INTO candidate_simulation_accounts(
                       model_version_id,contract_hash,status,initial_cash,latest_cash,latest_equity,
                       latest_trade_date,data_contract_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (int(model_version_id), contract_hash, "observing", float(contract["initial_cash"]),
                 float(latest.get("cash") or contract["initial_cash"]),
                 float(latest.get("total_value") or contract["initial_cash"]), str(latest.get("date") or ""),
                 json.dumps(contract, ensure_ascii=False), now, now),
            )
            account_id = conn.last_insert_id()
        for table in (
            "candidate_simulation_daily_equity", "candidate_simulation_decisions",
            "candidate_simulation_operations", "candidate_simulation_blocked_events",
        ):
            conn.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
        benchmark = {str(item.get("date") or ""): item.get("total_value") for item in replay.get("benchmark_series") or [] if isinstance(item, dict)}
        for item in equity:
            conn.execute(
                """INSERT INTO candidate_simulation_daily_equity(
                       account_id,trade_date,cash,stock_value,total_value,benchmark_value,
                       positions_json,point_in_time_passed,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (account_id, str(item.get("date") or ""), float(item.get("cash") or 0),
                 float(item.get("stock_value") or 0), float(item.get("total_value") or 0),
                 benchmark.get(str(item.get("date") or "")), json.dumps(item.get("by_symbol") or {}, ensure_ascii=False),
                 int(pit_passed), now),
            )
        for item in decisions:
            conn.execute(
                """INSERT INTO candidate_simulation_decisions(
                       account_id,signal_date,execute_date,status,decision_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (account_id, str(item.get("signal_date") or ""), str(item.get("execute_date") or ""),
                 str(item.get("status") or ""), json.dumps(item, ensure_ascii=False), now),
            )
        for index, item in enumerate(trades):
            uid = _stable_hash({"candidate": model_version_id, "index": index, "trade": item})[:40]
            conn.execute(
                """INSERT INTO candidate_simulation_operations(
                       account_id,operation_uid,trade_date,symbol,side,price,quantity,fees,operation_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (account_id, uid, str(item.get("date") or item.get("trade_date") or ""),
                 str(item.get("symbol") or ""), str(item.get("side") or ""), float(item.get("price") or 0),
                 int(item.get("quantity") or 0), float(item.get("fees") or 0), json.dumps(item, ensure_ascii=False), now),
            )
        for index, item in enumerate(blocked):
            uid = _stable_hash({"candidate": model_version_id, "index": index, "event": item})[:40]
            conn.execute(
                """INSERT INTO candidate_simulation_blocked_events(
                       account_id,event_uid,trade_date,code,event_json,created_at) VALUES(?,?,?,?,?,?)""",
                (account_id, uid, str(item.get("date") or ""), str(item.get("code") or "unknown"),
                 json.dumps(item, ensure_ascii=False), now),
            )
    return audit_candidate_account(account_id)


def audit_candidate_account(account_id: int) -> dict[str, Any]:
    from app.db.store import _conn

    with _conn() as conn:
        account = conn.execute("SELECT * FROM candidate_simulation_accounts WHERE id=?", (int(account_id),)).fetchone()
        if not account:
            return {"ok": False, "status": "missing_account", "account_id": int(account_id)}
        rows = [dict(item) for item in conn.execute(
            "SELECT * FROM candidate_simulation_daily_equity WHERE account_id=? ORDER BY trade_date", (int(account_id),)
        ).fetchall()]
    mismatches = [
        {"trade_date": row["trade_date"], "delta": round(float(row["total_value"]) - float(row["cash"]) - float(row["stock_value"]), 8)}
        for row in rows
        if abs(float(row["total_value"]) - float(row["cash"]) - float(row["stock_value"])) > 0.01
    ]
    account_row = dict(account)
    latest = rows[-1] if rows else None
    latest_delta = (
        round(float(account_row["latest_equity"]) - float(latest["total_value"]), 8)
        if latest else 0.0
    )
    return {
        "ok": not mismatches and abs(latest_delta) <= 0.01,
        "schema_version": LEDGER_CONTRACT,
        "status": "passed" if not mismatches and abs(latest_delta) <= 0.01 else "failed",
        "account_id": int(account_id),
        "model_version_id": int(account_row["model_version_id"]),
        "equity_sessions": len(rows),
        "latest_trade_date": account_row["latest_trade_date"],
        "latest_equity": float(account_row["latest_equity"]),
        "latest_equity_delta": latest_delta,
        "equity_identity_mismatches": mismatches,
        "production_account_effect": "none",
    }
