from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any

from app.data_platform.db import _conn as data_conn
from app.core.a_share_fees import is_exchange_etf, normalize_exchange_etf_ts_code
from app.db.store import (
    _conn as business_conn,
    atomic_business_transaction,
    get_positions,
    _rounded_account_balance_expression,
)


def _normalize_ts_code(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if "." in value:
        return value
    if is_exchange_etf(value):
        return normalize_exchange_etf_ts_code(value)
    return (
        f"{value}.SH" if value.startswith(("5", "6", "9"))
        else f"{value}.BJ" if value.startswith(("4", "8"))
        else f"{value}.SZ"
    )


def _base_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().split(".")[0]


def _ensure_table() -> None:
    with business_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS simulation_corporate_actions (
                   id BIGSERIAL PRIMARY KEY,
                   symbol VARCHAR(32) NOT NULL,
                   action_type VARCHAR(32) NOT NULL,
                   event_date VARCHAR(8) NOT NULL,
                   record_date VARCHAR(8) NOT NULL DEFAULT '',
                   quantity_entitled INTEGER NOT NULL DEFAULT 0,
                   cash_per_share DOUBLE PRECISION NOT NULL DEFAULT 0,
                   share_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
                   cash_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                   share_quantity INTEGER NOT NULL DEFAULT 0,
                   source_hash VARCHAR(64) NOT NULL,
                   source_available_time VARCHAR(64) NOT NULL DEFAULT '',
                   status VARCHAR(24) NOT NULL DEFAULT 'applied',
                   note TEXT NOT NULL DEFAULT '',
                   applied_at VARCHAR(64) NOT NULL,
                   UNIQUE(symbol,action_type,event_date,source_hash)
               )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_simulation_corporate_action_event
               ON simulation_corporate_actions(symbol,action_type,event_date,record_date)"""
        )


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _equivalent_no_entitlement_revision(
    existing: dict[str, Any],
    event: dict[str, Any],
    *,
    entitled: int,
    cash_amount: float,
    share_quantity: int,
) -> bool:
    """Accept a source-only revision that provably cannot change the account."""
    if str(existing.get("status") or "") != "no_entitlement" or int(entitled) != 0:
        return False
    numeric_pairs = (
        (existing.get("cash_per_share"), event.get("cash_per_share")),
        (existing.get("share_ratio"), event.get("share_ratio")),
        (existing.get("cash_amount"), cash_amount),
    )
    try:
        return (
            int(existing.get("quantity_entitled") or 0) == 0
            and int(existing.get("share_quantity") or 0) == int(share_quantity)
            and all(math.isclose(float(left or 0), float(right or 0), rel_tol=0, abs_tol=1e-12) for left, right in numeric_pairs)
        )
    except (TypeError, ValueError, OverflowError):
        return False


def refresh_position_dividends(*, max_age_days: int = 7, limit: int = 50) -> dict[str, Any]:
    """Refresh stock dividends and listed-fund cash distributions."""
    from app.data_platform.services.data_center import sync_dividend_data, sync_fund_dividend_data

    position_codes = sorted({
        _normalize_ts_code(str(item.get("symbol") or ""))
        for item in get_positions()
    })[:max(1, min(int(limit), 200))]
    cutoff = datetime.now().astimezone() - timedelta(days=max(1, int(max_age_days)))
    refreshed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for code in position_codes:
        api_name = "fund_div" if _base_symbol(code).startswith(("5", "1")) else "dividend"
        with data_conn() as conn:
            row = conn.execute(
                """SELECT MAX(observed_at) updated_at
                   FROM (
                       SELECT updated_at AS observed_at
                       FROM dc_tushare_raw_records
                       WHERE api_name=? AND ts_code=?
                       UNION ALL
                       SELECT checked_at AS observed_at
                       FROM dc_tushare_request_coverage
                       WHERE api_name=? AND ts_code=?
                         AND result_status IN ('success','valid_empty','not_applicable','deferred_empty')
                   ) freshness""",
                (api_name, code, api_name, code),
            ).fetchone()
        latest = _parse_time(dict(row).get("updated_at")) if row else None
        comparable_cutoff = cutoff
        if latest and latest.tzinfo is None:
            comparable_cutoff = cutoff.replace(tzinfo=None)
        if latest and latest >= comparable_cutoff:
            skipped.append({"ts_code": code, "reason": "fresh_within_window", "updated_at": latest.isoformat()})
            continue
        refreshed.append(
            sync_fund_dividend_data(code) if api_name == "fund_div" else sync_dividend_data(code)
        )
    failures = [item for item in refreshed if not item.get("ok") and not item.get("soft_failed")]
    return {
        "ok": not failures,
        "schema_version": "alis_dividend_refresh_v1",
        "eligible_symbols": position_codes,
        "refreshed": refreshed,
        "skipped": skipped,
        "failed_count": len(failures),
        "sources": {"stock": "dividend", "listed_fund": "fund_div"},
    }


def load_dividend_events(
    symbols: list[str],
    *,
    as_of_date: str,
    strict_event_time: bool = False,
) -> list[dict[str, Any]]:
    codes = sorted({_normalize_ts_code(symbol) for symbol in symbols if symbol})
    if not codes:
        return []
    placeholders = ",".join("?" for _ in codes)
    with data_conn() as conn:
        rows = [
            dict(row) for row in conn.execute(
                f"""SELECT api_name,ts_code,payload_json,available_time,updated_at
                    FROM dc_tushare_raw_records
                    WHERE api_name IN ('dividend','fund_div') AND ts_code IN ({placeholders})
                    ORDER BY ts_code,available_time,id""",
                tuple(codes),
            ).fetchall()
        ]
    cutoff_date = str(as_of_date or "").replace("-", "")[:8]
    target_cutoff = datetime.strptime(cutoff_date, "%Y%m%d").replace(hour=15)
    events: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            continue
        if str(payload.get("div_proc") or "") not in {"实施", "实施方案", ""}:
            continue
        record_date = str(payload.get("record_date") or "").replace("-", "")[:8]
        ex_date = str(payload.get("ex_date") or "").replace("-", "")[:8]
        pay_date = str(payload.get("pay_date") or ex_date).replace("-", "")[:8]
        if len(record_date) != 8 or len(ex_date) != 8:
            continue
        available = _parse_time(row.get("available_time"))
        if not available:
            continue
        comparable_target = target_cutoff.replace(tzinfo=available.tzinfo) if available.tzinfo else target_cutoff
        if available > comparable_target:
            continue
        if strict_event_time:
            event_cutoff = datetime.strptime(ex_date, "%Y%m%d").replace(hour=15)
            if available.tzinfo:
                event_cutoff = event_cutoff.replace(tzinfo=available.tzinfo)
            if available > event_cutoff:
                continue
        base = _base_symbol(str(row.get("ts_code") or payload.get("ts_code") or ""))
        raw_identity = {
            "symbol": base,
            "source_api": str(row.get("api_name") or ""),
            "end_date": payload.get("end_date"),
            "record_date": record_date,
            "ex_date": ex_date,
            "pay_date": pay_date,
            "cash_div_tax": payload.get("cash_div_tax") if row.get("api_name") == "dividend" else payload.get("div_cash"),
            "stk_div": payload.get("stk_div"),
        }
        source_hash = hashlib.sha256(
            json.dumps(raw_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        events[source_hash] = {
            **raw_identity,
            "cash_per_share": max(0.0, float((
                payload.get("cash_div_tax")
                if row.get("api_name") == "dividend"
                else payload.get("div_cash")
            ) or 0)),
            "share_ratio": max(0.0, float(payload.get("stk_div") or 0)),
            "source_hash": source_hash,
            "source_available_time": available.isoformat(),
        }
    return sorted(events.values(), key=lambda item: (item["record_date"], item["ex_date"], item["symbol"]))


def quantity_entitled(
    operations: list[dict[str, Any]],
    *,
    symbol: str,
    record_date: str,
    prior_share_actions: list[dict[str, Any]] | None = None,
) -> int:
    target = _base_symbol(symbol)
    quantity = 0
    for operation in operations:
        op_date = str(operation.get("at") or "")[:10].replace("-", "")
        if op_date > record_date or _base_symbol(str(operation.get("symbol") or "")) != target:
            continue
        amount = int(operation.get("quantity") or 0)
        quantity += amount if str(operation.get("side") or "").upper() == "BUY" else -amount
    for action in prior_share_actions or []:
        if (
            _base_symbol(str(action.get("symbol") or "")) == target
            and str(action.get("event_date") or "") <= record_date
            and str(action.get("action_type") or "") == "stock_dividend"
        ):
            quantity += int(action.get("share_quantity") or 0)
    return max(0, quantity)


def build_replay_corporate_actions(
    operations: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Freeze entitlements from the simulated ledger for account-curve replay."""
    actions: list[dict[str, Any]] = []
    prior_share_actions: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: (item["record_date"], item["ex_date"], item["symbol"])):
        entitled = quantity_entitled(
            operations,
            symbol=event["symbol"],
            record_date=event["record_date"],
            prior_share_actions=prior_share_actions,
        )
        if entitled <= 0:
            continue
        if float(event.get("cash_per_share") or 0) > 0:
            actions.append({
                "symbol": event["symbol"],
                "action_type": "cash_dividend",
                "event_date": event["pay_date"],
                "record_date": event["record_date"],
                "quantity_entitled": entitled,
                "cash_amount": round(entitled * float(event["cash_per_share"]), 2),
                "share_quantity": 0,
                "source_hash": event["source_hash"],
            })
        if float(event.get("share_ratio") or 0) > 0:
            share_quantity = max(0, math.floor(entitled * float(event["share_ratio"]) + 1e-9))
            action = {
                "symbol": event["symbol"],
                "action_type": "stock_dividend",
                "event_date": event["ex_date"],
                "record_date": event["record_date"],
                "quantity_entitled": entitled,
                "cash_amount": 0.0,
                "share_quantity": share_quantity,
                "source_hash": event["source_hash"],
            }
            if share_quantity > 0:
                actions.append(action)
                prior_share_actions.append(action)
    return sorted(actions, key=lambda item: (item["event_date"], item["action_type"], item["symbol"]))


def materialize_simulation_corporate_actions(*, as_of_date: str) -> dict[str, Any]:
    """Apply one whole corporate-action cycle as a serialized account transaction."""
    with atomic_business_transaction():
        return _materialize_simulation_corporate_actions(as_of_date=as_of_date)


def _materialize_simulation_corporate_actions(*, as_of_date: str) -> dict[str, Any]:
    """Apply known cash/stock dividends once to the simulation account."""
    _ensure_table()
    target_date = str(as_of_date or "").replace("-", "")[:8]
    with business_conn() as conn:
        operations = [
            dict(row) for row in conn.execute(
                "SELECT symbol,side,quantity,at FROM operations ORDER BY at,id"
            ).fetchall()
        ]
        prior_actions = [
            dict(row) for row in conn.execute(
                """SELECT symbol,action_type,event_date,share_quantity
                   FROM simulation_corporate_actions WHERE status='applied'"""
            ).fetchall()
        ]
    symbols = sorted({_base_symbol(str(item.get("symbol") or "")) for item in operations if item.get("symbol")})
    events = load_dividend_events(symbols, as_of_date=target_date, strict_event_time=False)
    applied: list[dict[str, Any]] = []
    skipped = 0
    no_entitlement = 0
    recorded = 0
    reconciled_source_revisions = 0
    source_conflicts: list[dict[str, Any]] = []
    for event in events:
        entitled = quantity_entitled(
            operations,
            symbol=event["symbol"],
            record_date=event["record_date"],
            prior_share_actions=prior_actions,
        )
        action_specs: list[tuple[str, str, float, int]] = []
        if event["cash_per_share"] > 0 and str(event["pay_date"]) <= target_date:
            action_specs.append((
                "cash_dividend",
                str(event["pay_date"]),
                round(entitled * float(event["cash_per_share"]), 2),
                0,
            ))
        if event["share_ratio"] > 0 and str(event["ex_date"]) <= target_date:
            action_specs.append((
                "stock_dividend",
                str(event["ex_date"]),
                0.0,
                max(0, math.floor(entitled * float(event["share_ratio"]) + 1e-9)),
            ))
        for action_type, event_date, cash_amount, share_quantity in action_specs:
            now = datetime.now().astimezone().isoformat()
            with business_conn() as conn:
                existing = conn.execute(
                    """SELECT id,source_hash,status,quantity_entitled,cash_per_share,
                              share_ratio,cash_amount,share_quantity
                         FROM simulation_corporate_actions
                       WHERE symbol=? AND action_type=? AND event_date=? AND record_date=?
                       LIMIT 1""",
                    (event["symbol"], action_type, event_date, event["record_date"]),
                ).fetchone()
                if existing:
                    existing_row = dict(existing)
                    if str(existing_row.get("source_hash") or "") != str(event.get("source_hash") or ""):
                        if _equivalent_no_entitlement_revision(
                            existing_row, event, entitled=entitled,
                            cash_amount=cash_amount, share_quantity=share_quantity,
                        ):
                            conn.execute(
                                """UPDATE simulation_corporate_actions
                                      SET source_hash=?,source_available_time=?,note=?,applied_at=?
                                    WHERE id=?""",
                                (
                                    event["source_hash"], event["source_available_time"],
                                    "无持仓权益且经济字段未变；已对账更新来源版本，不影响模拟账户",
                                    now, int(existing_row["id"]),
                                ),
                            )
                            reconciled_source_revisions += 1
                        else:
                            source_conflicts.append({
                                "symbol": event["symbol"],
                                "action_type": action_type,
                                "event_date": event_date,
                                "record_date": event["record_date"],
                                "existing_action_id": int(existing_row.get("id") or 0),
                                "existing_status": str(existing_row.get("status") or ""),
                                "code": "corporate_action_source_revision_requires_reconciliation",
                            })
                    skipped += 1
                    continue
                status = "applied" if entitled > 0 and (cash_amount > 0 or share_quantity > 0) else "no_entitlement"
                conn.execute(
                    """INSERT INTO simulation_corporate_actions(
                           symbol,action_type,event_date,record_date,quantity_entitled,
                           cash_per_share,share_ratio,cash_amount,share_quantity,
                           source_hash,source_available_time,status,note,applied_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event["symbol"], action_type, event_date, event["record_date"], entitled,
                        event["cash_per_share"], event["share_ratio"], cash_amount, share_quantity,
                        event["source_hash"], event["source_available_time"], status,
                        "按股权登记日模拟持仓计算；仅作用于模拟账户", now,
                    ),
                )
                if status == "applied" and action_type == "cash_dividend":
                    credited = conn.execute(
                        f"UPDATE account SET balance={_rounded_account_balance_expression(conn, 'balance+?')} WHERE id=1",
                        (cash_amount,),
                    )
                    if int(getattr(credited, "rowcount", 0) or 0) != 1:
                        raise RuntimeError("模拟账户不存在，现金分红整体回滚")
                elif status == "applied" and action_type == "stock_dividend":
                    lock_suffix = " FOR UPDATE" if getattr(conn, "_is_postgres", False) or getattr(conn, "_is_mysql", False) else ""
                    position = conn.execute(
                        "SELECT id,quantity,price,name,bought_at FROM positions WHERE symbol=?" + lock_suffix,
                        (event["symbol"],),
                    ).fetchone()
                    if position:
                        current = dict(position)
                        old_quantity = int(current.get("quantity") or 0)
                        new_quantity = old_quantity + share_quantity
                        adjusted_price = (
                            float(current.get("price") or 0) * old_quantity / new_quantity
                            if new_quantity > 0 else 0.0
                        )
                        conn.execute(
                            "UPDATE positions SET quantity=?,price=?,updated_at=? WHERE id=?",
                            (new_quantity, adjusted_price, now[:19], int(current["id"])),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO positions(symbol,quantity,price,updated_at,name,bought_at)
                               VALUES(?,?,?,?,?,?)""",
                            (event["symbol"], share_quantity, 0.0, now[:19], "", event_date),
                        )
            item = {
                "symbol": event["symbol"],
                "action_type": action_type,
                "event_date": event_date,
                "record_date": event["record_date"],
                "quantity_entitled": entitled,
                "cash_amount": cash_amount,
                "share_quantity": share_quantity,
                "status": status,
            }
            recorded += 1
            if status == "applied":
                applied.append(item)
            else:
                no_entitlement += 1
            if action_type == "stock_dividend" and status == "applied":
                prior_actions.append(item)
    return {
        "ok": not source_conflicts,
        "schema_version": "alis_simulation_corporate_actions_v1",
        "as_of_date": target_date,
        "event_count": len(events),
        "applied": applied,
        "recorded_count": recorded,
        "no_entitlement_count": no_entitlement,
        "already_materialized": skipped,
        "reconciled_source_revisions": reconciled_source_revisions,
        "source_conflicts": source_conflicts,
        "simulation_only": True,
    }


def run_corporate_action_cycle(*, as_of_date: str) -> dict[str, Any]:
    refresh = refresh_position_dividends()
    materialization = materialize_simulation_corporate_actions(as_of_date=as_of_date)
    return {
        "ok": bool(refresh.get("ok")) and bool(materialization.get("ok")),
        "schema_version": "alis_corporate_action_cycle_v1",
        "refresh": refresh,
        "materialization": materialization,
    }
