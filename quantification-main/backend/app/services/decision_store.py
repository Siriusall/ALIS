from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from app.db.store import _conn


POOL_TYPES = {"watch", "core", "holding"}
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


def upsert_pool_member(*, ts_code: str, pool_type: str, reason: str) -> dict[str, Any]:
    if pool_type not in POOL_TYPES: raise ValueError("invalid_pool_type")
    now = _now()
    with _conn() as c:
        c.execute("""INSERT INTO stock_pool_memberships(ts_code,pool_type,reason,status,entered_at,updated_at) VALUES(?,?,?,'active',?,?)
                     ON CONFLICT(ts_code,pool_type) DO UPDATE SET reason=excluded.reason,status='active',updated_at=excluded.updated_at""", (ts_code, pool_type, reason, now, now))
        row = c.execute("SELECT * FROM stock_pool_memberships WHERE ts_code=? AND pool_type=?", (ts_code, pool_type)).fetchone()
    return dict(row)


def list_pool_members(pool_type: str = "") -> list[dict[str, Any]]:
    with _conn() as c:
        if pool_type: rows = c.execute("SELECT * FROM stock_pool_memberships WHERE pool_type=? AND status='active' ORDER BY updated_at DESC", (pool_type,)).fetchall()
        else: rows = c.execute("SELECT * FROM stock_pool_memberships WHERE status='active' ORDER BY updated_at DESC").fetchall()
    return [dict(x) for x in rows]


def remove_pool_member(member_id: int) -> bool:
    with _conn() as c:
        row = c.execute("UPDATE stock_pool_memberships SET status='removed',updated_at=? WHERE id=? RETURNING id", (_now(), member_id)).fetchone()
    return bool(row)


def create_decision_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    decision_time = payload.get("decision_time") or _now()
    data_as_of = payload["data_as_of_time"]
    if str(data_as_of) > str(decision_time): raise ValueError("future_data_not_allowed")
    now = _now()
    with _conn() as c:
        row = c.execute("""INSERT INTO decision_snapshots(ts_code,decision_type,decision_time,price,thesis_id,data_as_of_time,model_version,
                 market_state_json,research_state_json,valuation_json,portfolio_context_json,reason,invalidation_conditions_json,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""", (
            payload["ts_code"], payload["decision_type"], decision_time, payload.get("price"), payload.get("thesis_id"), data_as_of,
            payload["model_version"], json.dumps(payload.get("market_state") or {}, ensure_ascii=False), json.dumps(payload.get("research_state") or {}, ensure_ascii=False),
            json.dumps(payload.get("valuation") or {}, ensure_ascii=False), json.dumps(payload.get("portfolio_context") or {}, ensure_ascii=False), payload["reason"],
            json.dumps(payload.get("invalidation_conditions") or [], ensure_ascii=False), now)).fetchone()
    return _decode(row, ("market_state_json", "research_state_json", "valuation_json", "portfolio_context_json", "invalidation_conditions_json"))


def list_decision_snapshots(ts_code: str = "", limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as c:
        if ts_code: rows = c.execute("SELECT * FROM decision_snapshots WHERE ts_code=? ORDER BY decision_time DESC LIMIT ?", (ts_code,limit)).fetchall()
        else: rows = c.execute("SELECT * FROM decision_snapshots ORDER BY decision_time DESC LIMIT ?", (limit,)).fetchall()
    return [_decode(x,("market_state_json","research_state_json","valuation_json","portfolio_context_json","invalidation_conditions_json")) for x in rows]


def create_review(payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    with _conn() as c:
        row = c.execute("""INSERT INTO review_records(decision_snapshot_id,review_time,outcome_json,attribution_json,error_type,lessons,created_at)
              VALUES(?,?,?,?,?,?,?) RETURNING *""", (payload.get("decision_snapshot_id"),payload.get("review_time") or now,json.dumps(payload.get("outcome") or {},ensure_ascii=False),json.dumps(payload.get("attribution") or {},ensure_ascii=False),payload.get("error_type"),payload.get("lessons") or "",now)).fetchone()
    return _decode(row,("outcome_json","attribution_json"))


def list_reviews(limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as c: rows=c.execute("SELECT * FROM review_records ORDER BY review_time DESC LIMIT ?",(limit,)).fetchall()
    return [_decode(x,("outcome_json","attribution_json")) for x in rows]


def transition_position(*, ts_code: str, to_state: str, reason: str, thesis_id: int | None, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    now=_now()
    with _conn() as c:
        old=c.execute("SELECT * FROM position_states WHERE ts_code=?",(ts_code,)).fetchone()
        from_state=str(dict(old).get("state")) if old else None
        if from_state is not None and to_state not in POSITION_TRANSITIONS.get(from_state,set()): raise ValueError("invalid_transition")
        if from_state is None and to_state not in {"excluded","watch"}: raise ValueError("initial_state_must_be_watch_or_excluded")
        c.execute("""INSERT INTO position_states(ts_code,state,thesis_id,reason,changed_at,updated_at) VALUES(?,?,?,?,?,?)
                     ON CONFLICT(ts_code) DO UPDATE SET state=excluded.state,thesis_id=excluded.thesis_id,reason=excluded.reason,changed_at=excluded.changed_at,updated_at=excluded.updated_at""",(ts_code,to_state,thesis_id,reason,now,now))
        c.execute("INSERT INTO position_state_history(ts_code,from_state,to_state,reason,evidence_json,changed_at) VALUES(?,?,?,?,?,?)",(ts_code,from_state,to_state,reason,json.dumps(evidence,ensure_ascii=False),now))
        row=c.execute("SELECT * FROM position_states WHERE ts_code=?",(ts_code,)).fetchone()
    return dict(row)
