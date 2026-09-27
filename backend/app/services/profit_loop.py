from __future__ import annotations

import json
import hashlib
import math
import os
import threading
from contextlib import contextmanager
from time import perf_counter
from datetime import datetime, time as dtime, timedelta
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn as data_conn, _read_conn as data_read_conn
from app.core.a_share_fees import is_exchange_etf, normalize_exchange_etf_ts_code
from app.db.store import (
    _conn as business_conn,
    _is_postgres as business_is_postgres,
    _read_conn as business_read_conn,
    get_account_baseline,
    get_positions,
)
from app.data_platform.services.market_calendar import target_trade_date
from app.services.backtest_data_source import BacktestDataSource
from app.services.decision_store import list_pool_members
from app.services.industry_allocator import build_industry_allocation
from app.services.market_context import live_market_context
from app.services.operations_overview import build_live_portfolio_risk
from app.services.position_valuation import (
    build_position_market_series_cache,
    missing_exact_position_valuations,
)
from app.services.point_in_time import normalize_explicit_timestamp
from app.services.plan_model_control import model_family_status, reconcile_plan_model_live_health
from app.services.review_learning import materialize_decision_outcomes, model_feedback_gate, review_learning_overview
from app.services.risk_limit_contract import (
    DEFAULT_CORRELATED_INDUSTRY_LIMIT,
    DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    DEFAULT_INDUSTRY_LIMIT,
    build_risk_limit_contract,
)
from app.services.two_group_optimizer import two_group_cvar_optimizer

UNIFIED_DAILY_DECISION_SCHEMA_VERSION = "alis_unified_daily_decision_v3"
PREVIOUS_UNIFIED_DAILY_DECISION_SCHEMA_VERSION = "alis_unified_daily_decision_v2"
LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION = "alis_unified_daily_decision_v1"
UNIFIED_EXECUTION_SAFETY_SCHEMA_VERSION = "alis_t1_execution_safety_v2"
FROZEN_ORDER_DATA_GATE_SCHEMA_VERSION = "alis_frozen_order_execution_data_gate_v1"
_FROZEN_ORDER_PROBE_INTERVAL_SECONDS = max(
    60,
    min(int(os.getenv("ALIS_FROZEN_ORDER_DATA_PROBE_INTERVAL_SECONDS", "300")), 3600),
)

_TABLES_READY = False
_TABLES_INIT_LOCK = threading.Lock()
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except Exception:
        return default


def _proposal_reason(
    *,
    controller_evidence: dict[str, Any],
    new_candidate: bool,
    planned_quantity: int,
    planned_side: str,
    full_exit: bool,
    pnl_pct: float,
    expected_return: float,
    target_weight: float,
    current_weight: float,
    delta: float,
    block_reason: str = "",
) -> str:
    """Explain the frozen order before falling back to target-weight narration."""
    intent = str(controller_evidence.get("intent") or "")
    if intent == "research_only":
        return str(controller_evidence.get("reason") or "")
    if new_candidate and planned_quantity <= 0:
        return block_reason or "核心池候选尚未形成正向收益与风险预算共振"
    if full_exit and pnl_pct <= -0.06 and expected_return <= -0.05:
        return "浮亏超过6%且20日预期低于-5%，触发完整退出"
    if full_exit:
        return "目标权重归零，退出组合"
    if planned_quantity > 0:
        if planned_side == "BUY" and intent == "defensive_switch":
            return "防御资产预期改善覆盖成本，按冻结最小交易单位补足目标权重"
        if planned_side == "BUY":
            return "目标组合提高该资产配置，按冻结最小交易单位执行"
        if planned_side == "SELL":
            return "风险预算或组合相关性要求降低配置，按冻结最小交易单位执行"
    if expected_return < 0 and target_weight >= current_weight:
        return "负预期收益不新增仓位"
    if delta > 0.002:
        return "目标组合提高该资产配置"
    if delta < -0.002:
        return "风险预算或组合相关性要求降低配置"
    return "权重偏差不足以覆盖调仓成本"


def _merge_expected_return_items(
    base_items: list[dict[str, Any]],
    override_items: list[dict[str, Any]],
    *,
    symbol_order: list[str],
) -> list[dict[str, Any]]:
    by_symbol = {
        str(item.get("symbol") or ""): dict(item)
        for item in [*base_items, *override_items]
        if isinstance(item, dict) and str(item.get("symbol") or "")
    }
    return [
        by_symbol[symbol]
        for symbol in dict.fromkeys(str(value or "") for value in symbol_order)
        if symbol in by_symbol
    ]


def _risk_execution_check(
    error_breaches: list[dict[str, Any]],
    all_breaches: list[dict[str, Any]],
    *,
    risk_reducing_only: bool,
) -> dict[str, Any]:
    if error_breaches and risk_reducing_only:
        return {
            "key": "risk_gate", "label": "风险门控", "status": "warn",
            "detail": f"已有 {len(error_breaches)} 项风险超限，但当前方案只减仓/卖出，允许降低风险",
        }
    return {
        "key": "risk_gate", "label": "风险门控",
        "status": "block" if error_breaches else "warn" if all_breaches else "pass",
        "detail": f"{len(error_breaches)} 项阻断，{len(all_breaches) - len(error_breaches)} 项提醒",
    }


def _blocked_portfolio_reoptimization(
    *,
    key: str,
    label: str,
    detail: str,
    decision_data_gate: dict[str, Any],
    target_trade_date: str,
    ready_trade_date: str,
    observation_cutoff: str,
    missing_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Return a non-computable portfolio result without inventing inputs.

    Formal portfolio construction must not fall through to current cost basis,
    a prior close, or a mutable latest snapshot when its authoritative T-day
    input is unavailable.  Keep this shape stable for the autonomy bus and
    plan endpoint, but leave all portfolio calculations empty.
    """
    blocked = {
        "key": key,
        "label": label,
        "status": "block",
        "detail": detail,
    }
    return {
        "ok": False,
        "status": "blocked",
        "blocked_reasons": [{"key": key, "detail": detail}],
        "schema_version": "portfolio_reoptimization_v1",
        "optimizer": {
            "ok": False,
            "status": f"blocked_by_{key}",
            "blocked_reasons": [detail],
        },
        "current_weights": {},
        "rebalance_proposal": [],
        "portfolio_comparison": {},
        "execution_checks": [blocked],
        "execution_authorized": False,
        "risk_reducing_only": False,
        "selection_source": f"{key}_blocked",
        "unified_selection": {
            "ok": False,
            "status": "blocked",
            "error": f"{key}_unavailable",
        },
        "selection_snapshot": {},
        "expected_return_contract": {},
        "pretrade_controller": {},
        "risk_limit_contract": {},
        "industry_allocation": {},
        "portfolio_method_competition": {},
        "execution_orders": [],
        "final_target_weights": {},
        "decision_data_gate": {
            "status": str(decision_data_gate.get("status") or "blocked"),
            "decision_ready": bool(decision_data_gate.get("decision_ready")),
            "target_trade_date": target_trade_date,
            "latest_decision_ready_trade_date": ready_trade_date,
            "observation_cutoff": observation_cutoff or None,
            "issues": decision_data_gate.get("issues") or [],
        },
        "missing_position_market_data": sorted(set(missing_symbols or [])),
        "proposal_unit": "portfolio_fraction",
        "candidate_universe": {
            "holding_count": 0,
            "optimization_count": 0,
            "admission_policy": "关键目标交易日输入缺失；禁止用成本价、前收或旧快照构造组合预览",
        },
    }


def _legacy_pool_preview_inputs(
    *, unified_available: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    if unified_available:
        return [], [], [], "not_consumed_non_authoritative"
    return (
        list_pool_members("core"),
        list_pool_members("candidate"),
        list_pool_members("watch"),
        "compatibility_preview_consumed",
    )


def _ensure_tables() -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _TABLES_INIT_LOCK:
        if _TABLES_READY:
            return
        _ensure_tables_once()
        _TABLES_READY = True


def _ensure_tables_once() -> None:
    with business_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS model_live_health (
            model_key VARCHAR(128) PRIMARY KEY,
            active_version VARCHAR(160) NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL,
            live_ic DOUBLE PRECISION,
            hit_rate DOUBLE PRECISION,
            turnover DOUBLE PRECISION,
            drawdown_contribution DOUBLE PRECISION,
            decay DOUBLE PRECISION,
            weight_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            reason TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            updated_at VARCHAR(64) NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS simulation_trade_learning_labels (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            label_date VARCHAR(16) NOT NULL,
            label VARCHAR(64) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            pnl_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
            expected_vs_actual_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            UNIQUE(symbol,label_date,label)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_rebalance_plans (
            id BIGSERIAL PRIMARY KEY,
            trade_date VARCHAR(16) NOT NULL,
            planned_execute_date VARCHAR(16) NOT NULL DEFAULT '',
            version INTEGER NOT NULL,
            input_hash VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'planned',
            proposal_json TEXT NOT NULL,
            execution_id BIGINT,
            generated_at VARCHAR(64) NOT NULL,
            executed_at VARCHAR(64),
            UNIQUE(trade_date,version),
            UNIQUE(trade_date,input_hash)
        )""")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS planned_execute_date VARCHAR(16) NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS planned_execute_at VARCHAR(40) NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS execution_claim_token VARCHAR(160) NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS execution_claimed_at VARCHAR(64)")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS validation_error_code VARCHAR(128) NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS validation_errors_json TEXT NOT NULL DEFAULT '[]'")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS validated_at VARCHAR(64)")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS release_id BIGINT")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS execution_data_evidence_json TEXT NOT NULL DEFAULT '{}'")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS next_probe_at VARCHAR(64)")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_rebalance_plan_projection (
            plan_id BIGINT PRIMARY KEY,
            execution_authorized BOOLEAN NOT NULL DEFAULT FALSE,
            schema_version VARCHAR(128) NOT NULL DEFAULT '',
            target_symbols_json TEXT NOT NULL DEFAULT '[]',
            target_weights_json TEXT NOT NULL DEFAULT '{}',
            decision_manifest_json TEXT NOT NULL DEFAULT '{}',
            risk_budget_json TEXT NOT NULL DEFAULT '{}',
            selection_snapshot_json TEXT NOT NULL DEFAULT '{}',
            input_hash VARCHAR(128) NOT NULL DEFAULT '',
            proposal_count INTEGER NOT NULL DEFAULT 0,
            actionable_count INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(64) NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_rebalance_plan_orders (
            plan_id BIGINT NOT NULL,
            order_index INTEGER NOT NULL,
            symbol VARCHAR(32) NOT NULL DEFAULT '',
            side VARCHAR(16) NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 0,
            action VARCHAR(64) NOT NULL DEFAULT '',
            weight_change DOUBLE PRECISION NOT NULL DEFAULT 0,
            order_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(plan_id, order_index)
        )""")


def _persist_plan_projection(conn: Any, plan_id: int, contract: dict[str, Any], input_hash: str = "") -> None:
    """Persist the execution-safe projection beside the immutable JSON contract.

    The raw contract remains audit evidence. Runtime scheduling and execution
    probes consume these narrow rows so proposal research payloads never cross
    the production DB connection.
    """
    proposal = contract.get("proposal") if isinstance(contract, dict) else []
    orders = contract.get("execution_orders") if isinstance(contract, dict) else []
    proposal = proposal if isinstance(proposal, list) else []
    orders = orders if isinstance(orders, list) else []
    compact_orders: list[dict[str, Any]] = []
    for item in orders:
        if not isinstance(item, dict):
            continue
        compact_orders.append({
            key: item.get(key)
            for key in ("symbol", "side", "quantity", "action", "weight_change",
                        "planned_execution_price", "reference_close", "planned_quantity",
                        "planned_side", "ts_code")
            if key in item
        })
    actionable = sum(
        1 for item in proposal
        if isinstance(item, dict)
        and str(item.get("action") or "") not in {"", "hold", "observe"}
        and abs(_safe_float(item.get("weight_change"))) > 1e-9
    )
    plan_projection = (
        int(plan_id), bool(contract.get("execution_authorized")),
        str(contract.get("schema_version") or ""),
        json.dumps(contract.get("target_symbols") or [], ensure_ascii=False, separators=(",", ":")),
        json.dumps(contract.get("target_weights") or {}, ensure_ascii=False, separators=(",", ":")),
        json.dumps(contract.get("decision_version_manifest") or {}, ensure_ascii=False, separators=(",", ":")),
        json.dumps({
            **(contract.get("risk_budget") or {}),
            "risk_budget": contract.get("risk_budget") or {},
            "portfolio_method_competition": contract.get("portfolio_method_competition") or {},
        }, ensure_ascii=False, separators=(",", ":")),
        json.dumps(contract.get("selection_snapshot") or {}, ensure_ascii=False, separators=(",", ":")),
        str(input_hash or ""), len(proposal), actionable, _now(),
    )
    conn.execute(
        """INSERT INTO daily_rebalance_plan_projection
           (plan_id,execution_authorized,schema_version,target_symbols_json,target_weights_json,
            decision_manifest_json,risk_budget_json,selection_snapshot_json,input_hash,
            proposal_count,actionable_count,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(plan_id) DO UPDATE SET
             execution_authorized=excluded.execution_authorized,
             schema_version=excluded.schema_version,
             target_symbols_json=excluded.target_symbols_json,
             target_weights_json=excluded.target_weights_json,
             decision_manifest_json=excluded.decision_manifest_json,
             risk_budget_json=excluded.risk_budget_json,
             selection_snapshot_json=excluded.selection_snapshot_json,
             input_hash=excluded.input_hash,
             proposal_count=excluded.proposal_count,
             actionable_count=excluded.actionable_count,
             created_at=excluded.created_at""",
        plan_projection,
    )
    conn.execute("DELETE FROM daily_rebalance_plan_orders WHERE plan_id=?", (int(plan_id),))
    for index, item in enumerate(compact_orders):
        conn.execute(
            """INSERT INTO daily_rebalance_plan_orders
               (plan_id,order_index,symbol,side,quantity,action,weight_change,order_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (int(plan_id), index, str(item.get("symbol") or item.get("ts_code") or ""),
             str(item.get("side") or item.get("planned_side") or ""),
             int(item.get("quantity") or item.get("planned_quantity") or 0),
             str(item.get("action") or ""), _safe_float(item.get("weight_change")),
             json.dumps(item, ensure_ascii=False, separators=(",", ":"))),
        )


def _normalize_execution_ts_code(symbol: str) -> str:
    """Map a frozen order symbol to the raw data platform's qualified code."""
    value = str(symbol or "").strip().upper()
    if value.endswith((".SH", ".SZ", ".BJ")):
        return value
    bare = value.split(".", 1)[0].zfill(6)
    if is_exchange_etf(bare):
        return normalize_exchange_etf_ts_code(bare)
    if bare.startswith(("6", "9")):
        return f"{bare}.SH"
    if bare.startswith(("8", "4")):
        return f"{bare}.BJ"
    return f"{bare}.SZ"


def _positive_execution_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0


def _present_execution_number(value: Any) -> bool:
    """Limit prices may be zero for an unrestricted/newly listed security."""
    if isinstance(value, bool):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed)


def _nonnegative_execution_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed >= 0


def _strict_execution_timestamp(value: Any) -> datetime | None:
    """Parse an explicit observation timestamp without inventing a timezone."""

    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_BUSINESS_TZ)


def _canonical_execution_timestamp(value: Any) -> str:
    parsed = _strict_execution_timestamp(value)
    return parsed.isoformat(timespec="seconds") if parsed is not None else ""


def _observation_evidence_missing(record: dict[str, Any], *, checked_at: str) -> list[str]:
    checked = _strict_execution_timestamp(checked_at)
    if checked is None:
        return ["audit.checked_at:invalid"]
    missing: list[str] = []
    for field in (
        "observed_at", "first_observed_at", "economic_available_time",
        "observation_provenance", "content_hash", "source_code", "created_at",
    ):
        if not str(record.get(field) or "").strip():
            missing.append(field)
    provenance = str(record.get("observation_provenance") or "").strip().lower()
    if provenance.startswith("legacy_"):
        missing.append("observation_provenance:not_legacy")
    observed = _strict_execution_timestamp(record.get("observed_at"))
    first_observed = _strict_execution_timestamp(record.get("first_observed_at"))
    economic = _strict_execution_timestamp(record.get("economic_available_time"))
    created = _strict_execution_timestamp(record.get("created_at"))
    for field, value in (
        ("observed_at", observed),
        ("first_observed_at", first_observed),
        ("economic_available_time", economic),
        ("created_at", created),
    ):
        if value is None:
            missing.append(f"{field}:invalid")
    if observed is not None and observed > checked:
        missing.append("observed_at:after_check")
    if created is not None and created > checked:
        missing.append("created_at:after_check")
    if first_observed is not None and observed is not None and first_observed > observed:
        missing.append("first_observed_at:after_observed")
    if economic is not None and observed is not None and economic > observed:
        missing.append("economic_available_time:after_observed")
    return list(dict.fromkeys(missing))


def _raw_record_for_execution(
    c: Any,
    api_name: str,
    ts_code: str,
    trade_date: str,
    *,
    observation_cutoff: str,
) -> dict[str, Any]:
    # A current raw row is only a mutable projection.  Formal T+1 execution
    # evidence must come from the append-only observation versions, otherwise
    # a later fallback/primary rewrite could make an old order appear valid.
    from app.data_platform.services.observation_clock import load_raw_record_versions

    versioned = load_raw_record_versions(
        c,
        api_name,
        cutoff=observation_cutoff,
        ts_codes=[ts_code],
        trade_date_start=trade_date,
        trade_date_end=trade_date,
    )
    if versioned is None:
        # This helper is only used by the frozen execution audit.  A mutable
        # projection cannot prove what was visible at ``observation_cutoff``;
        # fail closed on every backend, including SQLite test databases.
        return {}
    if not versioned:
        return {}
    item = dict(versioned[-1])
    item.setdefault("available_time", item.get("observed_at", ""))
    try:
        payload = json.loads(item.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {"payload": payload if isinstance(payload, dict) else {}, **item}


def _snapshot_record_for_execution(
    c: Any,
    ts_code: str,
    trade_date: str,
    *,
    observation_cutoff: str,
) -> dict[str, Any]:
    from app.data_platform.services.observation_clock import (
        load_raw_record_versions,
        load_snapshot_versions,
    )

    versioned = load_snapshot_versions(
        c,
        cutoff=observation_cutoff,
        ts_codes=[ts_code],
        trade_date=trade_date,
    )
    if versioned is None:
        # The mutable normalized projection is never an execution evidence
        # source.  Missing observation versions must remain a hard block.
        return {}
    if not versioned:
        return {}
    item = dict(versioned[-1])
    try:
        payload = json.loads(item.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        return {}
    item.update(payload)
    item.setdefault("available_time", item.get("observed_at", ""))
    return item


def audit_frozen_execution_orders(
    contract: dict[str, Any] | None,
    execute_date: str,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Verify every frozen order has an exact, executable market slice.

    This is deliberately independent of the broad market coverage gate.  A
    99% universe can still omit one held position or one ETF in the frozen
    order set.  The audit is read-only and returns field-level evidence for
    both the claim-time and pre-transaction checks.
    """
    target = str(execute_date or "").replace("-", "")[:8]
    checked_at = _canonical_execution_timestamp(
        observed_at or datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds"),
    )
    orders = [
        item for item in (contract or {}).get("execution_orders") or []
        if isinstance(item, dict) and int(item.get("planned_quantity") or item.get("quantity") or 0) > 0
    ]
    if not orders:
        return {
            "schema_version": FROZEN_ORDER_DATA_GATE_SCHEMA_VERSION,
            "execute_date": target,
            "checked_at": checked_at,
            "ok": True,
            "orders": [],
            "missing": [],
            "next_probe_at": None,
        }
    if not checked_at:
        return {
            "schema_version": FROZEN_ORDER_DATA_GATE_SCHEMA_VERSION,
            "execute_date": target,
            "checked_at": str(observed_at or ""),
            "ok": False,
            "orders": [],
            "missing": ["audit_observation_clock"],
            "next_probe_at": None,
        }
    items: list[dict[str, Any]] = []
    with data_read_conn() as c:
        for order in orders:
            symbol = str(order.get("symbol") or order.get("ts_code") or "").strip().upper()
            ts_code = _normalize_execution_ts_code(symbol)
            is_etf = is_exchange_etf(symbol)
            required = (
                ["open", "high", "low", "close", "pre_close", "vol", "amount"]
                if is_etf
                else ["open", "high", "low", "close", "pre_close", "vol", "amount", "up_limit", "down_limit"]
            )
            missing: list[str] = []
            evidence: dict[str, Any] = {
                "symbol": symbol,
                "ts_code": ts_code,
                "side": str(order.get("side") or "").upper(),
                "is_etf": is_etf,
                "required_fields": required,
            }
            if len(target) != 8 or not symbol:
                missing.extend(required)
            elif is_etf:
                raw = _raw_record_for_execution(
                    c, "fund_daily", ts_code, target, observation_cutoff=checked_at,
                )
                payload = raw.get("payload") or {}
                for field in required:
                    if not _positive_execution_number(payload.get(field)):
                        missing.append(f"fund_daily.{field}")
                if str(payload.get("ts_code") or raw.get("ts_code") or "").strip().upper() != ts_code:
                    missing.append("fund_daily.ts_code")
                if str(payload.get("trade_date") or raw.get("trade_date") or "").replace("-", "")[:8] != target:
                    missing.append("fund_daily.trade_date")
                missing.extend(
                    f"fund_daily.{field}" for field in _observation_evidence_missing(raw, checked_at=checked_at)
                )
                evidence.update({
                    "source": raw.get("source_code") or "",
                    "available_time": raw.get("available_time") or "",
                    "record_present": bool(raw),
                })
            else:
                daily = _raw_record_for_execution(
                    c, "daily", ts_code, target, observation_cutoff=checked_at,
                )
                limit = _raw_record_for_execution(
                    c, "stk_limit", ts_code, target, observation_cutoff=checked_at,
                )
                snapshot = _snapshot_record_for_execution(
                    c, ts_code, target, observation_cutoff=checked_at,
                )
                daily_payload = daily.get("payload") or {}
                limit_payload = limit.get("payload") or {}
                for field in ("open", "high", "low", "close", "pre_close"):
                    if not _positive_execution_number(daily_payload.get(field)):
                        missing.append(f"daily.{field}")
                for field in ("vol", "amount"):
                    checker = _nonnegative_execution_number
                    if not checker(daily_payload.get(field)):
                        missing.append(f"daily.{field}")
                for field in ("up_limit", "down_limit"):
                    if not _present_execution_number(limit_payload.get(field)):
                        missing.append(f"stk_limit.{field}")
                for prefix, raw, payload in (
                    ("daily", daily, daily_payload),
                    ("stk_limit", limit, limit_payload),
                ):
                    if str(payload.get("ts_code") or raw.get("ts_code") or "").strip().upper() != ts_code:
                        missing.append(f"{prefix}.ts_code")
                    if str(payload.get("trade_date") or raw.get("trade_date") or "").replace("-", "")[:8] != target:
                        missing.append(f"{prefix}.trade_date")
                snapshot_checks = {
                    "open": _positive_execution_number,
                    "high": _positive_execution_number,
                    "low": _positive_execution_number,
                    "close": _positive_execution_number,
                    "pre_close": _positive_execution_number,
                    "volume": _nonnegative_execution_number,
                    "turnover": _nonnegative_execution_number,
                    "up_limit": _present_execution_number,
                    "down_limit": _present_execution_number,
                }
                for field, checker in snapshot_checks.items():
                    if not checker(snapshot.get(field)):
                        missing.append(f"snapshot.{field}")
                if str(snapshot.get("ts_code") or "").strip().upper() != ts_code:
                    missing.append("snapshot.ts_code")
                if str(snapshot.get("trade_date") or "").replace("-", "")[:8] != target:
                    missing.append("snapshot.trade_date")
                missing.extend(
                    f"daily.{field}" for field in _observation_evidence_missing(daily, checked_at=checked_at)
                )
                missing.extend(
                    f"stk_limit.{field}" for field in _observation_evidence_missing(limit, checked_at=checked_at)
                )
                missing.extend(
                    f"snapshot.{field}" for field in _observation_evidence_missing(snapshot, checked_at=checked_at)
                )
                evidence.update({
                    "daily_source": daily.get("source_code") or "",
                    "limit_source": limit.get("source_code") or "",
                    "daily_available_time": daily.get("available_time") or "",
                    "limit_available_time": limit.get("available_time") or "",
                    "snapshot_available_time": snapshot.get("available_time") or "",
                    "daily_content_hash": daily.get("content_hash") or "",
                    "limit_content_hash": limit.get("content_hash") or "",
                    "snapshot_content_hash": snapshot.get("content_hash") or "",
                    "daily_record_present": bool(daily),
                    "limit_record_present": bool(limit),
                    "snapshot_record_present": bool(snapshot),
                })
            evidence["missing_fields"] = missing
            evidence["ready"] = not missing
            items.append(evidence)
    missing_items = [item for item in items if not item.get("ready")]
    next_probe = (
        datetime.now(_BUSINESS_TZ) + timedelta(seconds=_FROZEN_ORDER_PROBE_INTERVAL_SECONDS)
    ).isoformat(timespec="seconds") if missing_items else None
    return {
        "schema_version": FROZEN_ORDER_DATA_GATE_SCHEMA_VERSION,
        "execute_date": target,
        "checked_at": checked_at,
        "ok": not missing_items,
        "orders": items,
        "missing": [item["symbol"] for item in missing_items],
        "next_probe_at": next_probe,
    }


def _persist_frozen_order_audit(
    *, plan_id: int, audit: dict[str, Any], next_probe_at: str | None = None,
    conn: Any | None = None,
) -> None:
    """Keep the latest field-level gate evidence on the immutable plan row."""
    payload = (
        json.dumps(audit or {}, ensure_ascii=False, sort_keys=True),
        next_probe_at if next_probe_at is not None else audit.get("next_probe_at"),
        int(plan_id),
    )
    if conn is not None:
        conn.execute(
            """UPDATE daily_rebalance_plans
                  SET execution_data_evidence_json=?,next_probe_at=?
                WHERE id=?""",
            payload,
        )
        return
    with business_conn() as c:
        c.execute(
            """UPDATE daily_rebalance_plans
                  SET execution_data_evidence_json=?,next_probe_at=?
                WHERE id=?""",
            payload,
        )


def frozen_order_data_gate_error(audit: dict[str, Any]):
    """Build the retryable error used by both claim-time and T+1 checks."""
    from app.services.quant_run_contracts import ReplayRiskGateError

    missing = ",".join(str(value) for value in audit.get("missing") or [])
    error = ReplayRiskGateError(
        f"冻结计划订单行情未就绪：{missing or 'unknown'}",
        code="paused_frozen_execution_order_data_not_ready",
    )
    # The worker persists this evidence after rolling back the ledger scope.
    error.execution_data_evidence = audit
    return error


def _fallback_execution_clock() -> dtime:
    """Use the publication-discovery window, not a guessed fixed close time."""
    hour = max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_HOUR", "15")), 23))
    minute = max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_MINUTE", "1")), 59))
    return dtime(hour, minute)


def remembered_first_rebalance_clock() -> dtime | None:
    """Legacy diagnostic only; an observed delay must never schedule future runs."""
    try:
        with business_conn() as c:
            row = c.execute(
                """SELECT executed_at FROM daily_rebalance_plans
                   WHERE status='executed' AND COALESCE(executed_at,'')<>''
                   ORDER BY executed_at ASC,id ASC LIMIT 1"""
            ).fetchone()
        value = str(dict(row).get("executed_at") or "") if row else ""
        parsed = datetime.fromisoformat(value)
        return dtime(parsed.hour, parsed.minute)
    except Exception:
        # Startup and schema bootstrap must keep the configured 16:05 fallback
        # even when the plan table is not available yet.
        return None


def effective_rebalance_clock() -> dtime:
    """Start checking with publication probes and execute once all hard gates pass.

    A historical execution time is an outcome of source publication, retries and
    incidents.  Treating the first outcome as a permanent schedule can turn one
    delayed run into an indefinite daily delay.
    """
    return _fallback_execution_clock()


def _planned_execute_at(execute_date: str) -> str:
    clock = effective_rebalance_clock()
    date_value = str(execute_date or "")[:10]
    if len(date_value) == 8 and date_value.isdigit():
        date_value = f"{date_value[:4]}-{date_value[4:6]}-{date_value[6:8]}"
    return f"{date_value}T{clock.hour:02d}:{clock.minute:02d}:00+08:00" if date_value else ""


def _formal_plan_is_due(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Check the frozen execution timestamp before any plan claim is written."""
    execute_date = str(record.get("planned_execute_date") or "").replace("-", "")[:8]
    current = (now.astimezone(_BUSINESS_TZ) if now and now.tzinfo else now) or datetime.now(_BUSINESS_TZ)
    current_date = current.strftime("%Y%m%d")
    value = str(record.get("planned_execute_at") or _planned_execute_at(execute_date)).strip()
    if not value:
        raise ValueError("正式调仓计划缺少冻结执行时刻")
    try:
        due_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("正式调仓计划执行时刻格式无效") from exc
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=_BUSINESS_TZ)
    else:
        due_at = due_at.astimezone(_BUSINESS_TZ)
    # Historical rows are already due by date, but their frozen timestamp is
    # still part of the execution contract and must never bypass validation.
    if execute_date and execute_date < current_date:
        return True
    return current >= due_at


def _latest_report(period_type: str = "daily") -> dict[str, Any]:
    with business_conn() as c:
        row = c.execute(
            "SELECT payload_json,period FROM quant_performance_reports WHERE period_type=? ORDER BY period DESC,id DESC LIMIT 1",
            (period_type,),
        ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(dict(row).get("payload_json") or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _position_returns(
    symbols: list[str],
    limit: int = 120,
    *,
    as_of_time: str | None = None,
    trade_date_end: str | None = None,
) -> dict[str, list[float]]:
    requested = [str(value or "").strip() for value in symbols if str(value or "").strip()]
    if not requested:
        return {}
    history_limit = max(20, min(252, int(limit or 120)))
    code_by_symbol: dict[str, list[str]] = {}
    symbol_by_code: dict[str, str] = {}
    for symbol in requested:
        normalized = symbol.split(".", 1)[0]
        code = (
            symbol if "." in symbol
            else normalize_exchange_etf_ts_code(symbol) if is_exchange_etf(normalized)
            else f"{symbol}.SH" if normalized.startswith(("5", "6", "9")) else f"{symbol}.SZ"
        )
        candidates = list(dict.fromkeys((symbol, code)))
        code_by_symbol[symbol] = candidates
        for candidate in candidates:
            symbol_by_code.setdefault(candidate, symbol)

    result: dict[str, list[float]] = {}
    # Formal plans must use the same frozen observation clock as selection and
    # valuation.  The old ``now`` default remains for non-formal diagnostics,
    # but a caller that supplies a cutoff never silently widens it.
    cutoff = normalize_explicit_timestamp(as_of_time) if as_of_time else datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")
    if as_of_time and not cutoff:
        return {}
    end_date = str(trade_date_end or "").replace("-", "")[:8]
    history_start = ""
    if len(end_date) == 8 and end_date.isdigit():
        # The query only needs the latest ``history_limit`` trading bars.  A
        # three-calendar-day allowance per bar safely covers weekends and
        # exchange holidays while preventing a PIT scan over every historical
        # accepted snapshot version.
        history_start = (
            datetime.strptime(end_date, "%Y%m%d") - timedelta(days=history_limit * 3)
        ).strftime("%Y%m%d")
    placeholders = ",".join("?" for _ in symbol_by_code)
    from app.data_platform.services.observation_clock import (
        load_raw_record_versions,
        load_snapshot_versions,
    )
    with data_conn() as c:
        # Bound the immutable PIT scan to the requested history window.  A
        # trade-date upper bound alone makes PostgreSQL walk every historical
        # revision for the selected symbols before the per-code window can be
        # applied; on the production snapshot this needlessly touches GBs of
        # rows and can hit the 30s statement timeout.
        trade_date_start = ""
        if end_date:
            start_row = c.execute(
                f"""SELECT MIN(trade_date) AS start_date
                      FROM (
                            SELECT trade_date
                              FROM dc_stock_daily_snapshot
                             WHERE ts_code IN ({placeholders})
                               AND trade_date<=?
                             GROUP BY trade_date
                             ORDER BY trade_date DESC
                             LIMIT ?
                           ) bounded_dates""",
                tuple(symbol_by_code) + (end_date, history_limit),
            ).fetchone()
            if start_row:
                trade_date_start = str(dict(start_row).get("start_date") or "")
        try:
            versioned = load_snapshot_versions(
                c,
                cutoff=cutoff,
                ts_codes=list(symbol_by_code),
                trade_date_start=trade_date_start,
                trade_date_end=end_date,
                # A global result cap lets the first dense symbol consume the
                # entire window and leaves the remaining requested series empty.
                # Keep the historical bound independently for every security.
                per_ts_code_limit=history_limit,
                # Position returns only consume change_pct.  Projecting the
                # single field avoids transferring the full immutable payload
                # for every historical bar during formal plan generation.
                payload_fields=("change_pct",),
            )
        except Exception as exc:
            raise RuntimeError(f"position_returns_snapshot_versions: {exc}") from exc
        if versioned is None and as_of_time:
            # A formal cutoff cannot fall back to the mutable snapshot
            # projection.  It has no immutable observation proof and may have
            # been overwritten by a late refresh.
            return {}
        rows = []
        if versioned is not None:
            for item in versioned:
                try:
                    payload = json.loads(item.get("payload_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict) and payload.get("change_pct") is not None:
                    rows.append({
                        "ts_code": item.get("ts_code"),
                        "trade_date": item.get("trade_date"),
                        "change_pct": payload.get("change_pct"),
                    })
        else:
            rows = c.execute(
                f"""WITH ranked AS (
                        SELECT ts_code,trade_date,change_pct,
                               ROW_NUMBER() OVER(PARTITION BY ts_code ORDER BY trade_date DESC) AS series_rank
                          FROM dc_stock_daily_snapshot
                         WHERE ts_code IN ({placeholders}) AND change_pct IS NOT NULL
                           AND (?='' OR trade_date<=?)
                    )
                    SELECT ts_code,trade_date,change_pct
                      FROM ranked
                     WHERE series_rank<=?
                     ORDER BY ts_code,trade_date""",
                tuple(symbol_by_code) + (end_date, end_date, history_limit),
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault(str(item.get("ts_code") or ""), []).append(float(item["change_pct"]) / 100.0)
        for code, returns in grouped.items():
            symbol = symbol_by_code.get(code)
            if symbol and returns:
                result[symbol] = returns

        missing = [symbol for symbol in requested if symbol not in result]
        missing_codes = [code for symbol in missing for code in code_by_symbol[symbol] if code in symbol_by_code]
        if missing_codes:
            raw_placeholders = ",".join("?" for _ in missing_codes)
            # The immutable raw loader's legacy ``limit`` is global. Split
            # this small fallback by security so one ETF cannot starve the
            # other requested series while preserving the PIT contract.
            raw_versions: list[dict[str, Any]] | None = []
            for missing_code in missing_codes:
                try:
                    rows_for_code = load_raw_record_versions(
                        c,
                        "fund_daily",
                        cutoff=cutoff,
                        ts_codes=[missing_code],
                        trade_date_start=trade_date_start,
                        trade_date_end=end_date,
                        limit=history_limit,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"position_returns_fund_daily ts_code={missing_code}: {exc}"
                    ) from exc
                if rows_for_code is None:
                    raw_versions = None
                    break
                raw_versions.extend(rows_for_code)
            if raw_versions is None and as_of_time:
                raw_rows = []
            elif raw_versions is not None:
                raw_rows = raw_versions
            else:
                raw_rows = c.execute(
                    f"""WITH ranked AS (
                            SELECT ts_code,trade_date,payload_json,
                                   ROW_NUMBER() OVER(PARTITION BY ts_code ORDER BY trade_date DESC) AS series_rank
                             FROM dc_tushare_raw_records
                             WHERE api_name='fund_daily' AND ts_code IN ({raw_placeholders})
                               AND (?='' OR trade_date<=?)
                        )
                        SELECT ts_code,trade_date,payload_json
                          FROM ranked
                         WHERE series_rank<=?
                         ORDER BY ts_code,trade_date""",
                    tuple(missing_codes) + (end_date, end_date, history_limit),
                ).fetchall()
            raw_grouped: dict[str, list[float]] = {}
            for row in raw_rows:
                item = dict(row)
                try:
                    payload = json.loads(item.get("payload_json") or "{}")
                    change = payload.get("pct_chg")
                    if change is not None:
                        raw_grouped.setdefault(str(item.get("ts_code") or ""), []).append(float(change) / 100.0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            for code, returns in raw_grouped.items():
                symbol = symbol_by_code.get(code)
                if symbol and returns:
                    result[symbol] = returns
    return result


def _latest_decision_data_gate(trade_date: str = "") -> dict[str, Any]:
    try:
        from app.services.data_health import assess_latest_decision_data_health

        return (
            assess_latest_decision_data_health(trade_date)
            if trade_date
            else assess_latest_decision_data_health()
        )
    except Exception as exc:
        return {
            "decision_ready": False,
            "status": "blocked",
            "issues": [{"message": f"决策数据门检查失败：{str(exc)[:200]}"}],
            "readiness": {},
            "latest_snapshot": {},
        }


def _decision_observation_cutoff(decision_data_gate: dict[str, Any]) -> str:
    """Return the exact observation cutoff captured by the strict data gate."""
    readiness = decision_data_gate.get("readiness") if isinstance(decision_data_gate.get("readiness"), dict) else {}
    contract = readiness.get("decision_contract") if isinstance(readiness.get("decision_contract"), dict) else {}
    observation = contract.get("observation_contract") if isinstance(contract.get("observation_contract"), dict) else {}
    cutoff = str(observation.get("cutoff") or contract.get("observation_cutoff") or "").strip()
    if cutoff:
        return normalize_explicit_timestamp(cutoff)
    direct_contract = decision_data_gate.get("decision_contract") if isinstance(decision_data_gate.get("decision_contract"), dict) else {}
    direct_observation = direct_contract.get("observation_contract") if isinstance(direct_contract.get("observation_contract"), dict) else {}
    return normalize_explicit_timestamp(
        direct_observation.get("cutoff") or direct_contract.get("observation_cutoff") or ""
    )


def _build_formal_unified_selection(
    *,
    trade_date: str,
    holding_symbols: list[str] | None = None,
    observation_cutoff: str | None = None,
) -> dict[str, Any]:
    if not trade_date:
        return {
            "ok": False,
            "status": "blocked",
            "schema_version": "alis_unified_stock_selection_v2",
            "error": "latest_decision_trade_date_missing",
        }
    started_at = perf_counter()
    try:
        from app.services.unified_stock_selection import build_unified_stock_selection

        return build_unified_stock_selection(
            signal_date=trade_date,
            top_n=20,
            exchange_group="HS",
            holding_symbols=holding_symbols or [],
            observation_cutoff=observation_cutoff,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "blocked",
            "schema_version": "alis_unified_stock_selection_v2",
            "error": (
                "portfolio_stage=unified_selection "
                f"elapsed_ms={round((perf_counter() - started_at) * 1000)} error={str(exc)[:400]}"
            ),
        }


def build_return_attribution_loop(*, persist: bool = True) -> dict[str, Any]:
    materialized = materialize_decision_outcomes(limit=2000) if persist else {
        "ok": True, "status": "read_only", "written": 0,
    }
    daily = _latest_report("daily")
    monthly = _latest_report("monthly")
    with business_conn() as c:
        row = c.execute("""SELECT
            COALESCE(SUM(market_contribution),0) market,
            COALESCE(SUM(industry_contribution),0) industry,
            COALESCE(SUM(selection_contribution),0) selection,
            COALESCE(AVG(CASE WHEN correct=1 THEN 1.0 ELSE 0.0 END),0) hit_rate
            FROM decision_outcome_attributions
            WHERE calculated_at >= COALESCE((SELECT MAX(calculated_at) FROM decision_outcome_attributions), '')""").fetchone()
    outcome = dict(row or {})
    capital = max(_safe_float(get_account_baseline(), 0.0), 1.0)
    cost = _safe_float(daily.get("total_cost", daily.get("transaction_cost", 0.0)))
    return_pct = _safe_float(daily.get("return_pct", daily.get("return", 0.0)))
    report_status = str(daily.get("attribution_status") or (daily.get("attribution") or {}).get("status") or "unknown")
    has_report_attribution = report_status == "complete"
    to_pct = lambda amount: _safe_float(amount) / capital * 100.0  # noqa: E731
    attribution = {
        "model_selection": to_pct(daily.get("stock_selection_contribution")) if has_report_attribution else _safe_float(outcome.get("selection")) * 100.0,
        "industry_exposure": to_pct(daily.get("industry_allocation_contribution")) if has_report_attribution else _safe_float(outcome.get("industry")) * 100.0,
        "position_sizing": _safe_float(monthly.get("position_contribution", 0.0)),
        "timing_gate": -abs(_safe_float((daily.get("market_state_gate") or {}).get("blocked_signals", 0.0))) * 0.001,
        "trading_cost": -abs(to_pct(cost)),
        "risk_overlay": to_pct(daily.get("cash_defensive_contribution")) if has_report_attribution else 0.0,
    }
    explained = sum(attribution.values())
    attribution["unexplained"] = return_pct - explained
    return {
        "ok": True,
        "schema_version": "profit_attribution_loop_v1",
        "latest_daily_return_pct": return_pct,
        "latest_monthly_return_pct": _safe_float(monthly.get("return_pct")),
        "unit": "percentage_points",
        "status": report_status,
        "components": {key: round(value, 6) for key, value in attribution.items()},
        "hit_rate": _safe_float(outcome.get("hit_rate")),
        "materialized": materialized,
        "limitations": list(daily.get("attribution_limitations") or []),
        "interpretation": "各项统一为收益率百分点；回撤只作为风险指标，不再伪装成负收益贡献。未解释部分继续进入复盘。",
    }


def build_model_live_monitor(*, persist: bool = True) -> dict[str, Any]:
    _ensure_tables()
    family = model_family_status()
    rows = []
    evaluation_date = datetime.now(_BUSINESS_TZ).date().isoformat()
    quarantine_streak = max(
        2, min(int(os.getenv("ALIS_MODEL_DEGRADED_STREAK_TO_QUARANTINE", "3")), 20)
    )
    for item in family.get("items") or []:
        active = item.get("active_version") or {}
        latest = item.get("latest_version") or {}
        version = str((active or latest).get("version") or "")
        metrics = (active or latest).get("metrics") or {}
        feedback = model_feedback_gate(version) if version else {"weighted_accuracy": None, "should_degrade": False}
        has_live_ic = metrics.get("live_ic") is not None
        has_hit_rate = metrics.get("live_hit_rate") is not None or feedback.get("weighted_accuracy") is not None
        hit_rate = _safe_float(metrics.get("live_hit_rate"), _safe_float(feedback.get("weighted_accuracy"), 0.0))
        live_ic = _safe_float(metrics.get("live_ic"), 0.0)
        turnover = _safe_float(metrics.get("live_turnover"), 0.0)
        drawdown_contribution = -abs(_safe_float(metrics.get("live_drawdown_contribution", 0.0)))
        mature_outcomes = int(feedback.get("mature_20d_outcomes") or 0)
        evidence_mature = mature_outcomes >= 5
        # Human review labels and placeholder zeros are not live performance.
        # Until forward outcomes mature, keep the model neutral and call it
        # observation rather than incorrectly declaring production health.
        has_live_signal = evidence_mature and (has_live_ic or has_hit_rate)
        family_status = str(item.get("status") or "")
        production = bool(active) and family_status in {
            "production", "production_current_standard"
        }
        decay = max(0.0, min(1.0, 0.05 - live_ic)) if has_live_ic and live_ic < 0.05 else max(0.0, 0.45 - hit_rate) if has_hit_rate else 0.0
        should_degrade = production and has_live_signal and (bool(feedback.get("should_degrade")) or (has_live_ic and live_ic < -0.02) or (has_hit_rate and hit_rate < 0.38))
        previous_payload: dict[str, Any] = {}
        if persist:
            with business_conn() as c:
                previous = c.execute(
                    "SELECT active_version,metrics_json FROM model_live_health WHERE model_key=?",
                    (str(item.get("model_key") or ""),),
                ).fetchone()
            if previous and str(previous.get("active_version") or "") == version:
                raw_previous = previous.get("metrics_json")
                try:
                    previous_payload = (
                        raw_previous if isinstance(raw_previous, dict)
                        else json.loads(raw_previous or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    previous_payload = {}
        prior_streak = int(previous_payload.get("degraded_streak") or 0)
        already_evaluated = str(previous_payload.get("last_evaluated_date") or "") == evaluation_date
        degraded_streak = prior_streak if already_evaluated else prior_streak + 1 if should_degrade else 0
        quarantined = should_degrade and degraded_streak >= quarantine_streak
        multiplier = 0.0 if quarantined else 0.55 if should_degrade else 0.8 if production and decay > 0.25 else 1.0
        status = "quarantined" if quarantined else "degraded" if should_degrade else "observing" if production and not has_live_signal else "watch" if multiplier < 1 else family_status or "unknown"
        reason = "连续线上失效，已隔离并进入安全回退" if quarantined else "上线后指标失效，自动降权" if should_degrade else "未来20日样本不足，观察中且不调整权重" if production and not has_live_signal else "指标衰减，降低影响权重" if multiplier < 1 else "非生产模型不参与降权" if not production else "生产健康"
        payload = {
            "model_key": item.get("model_key"),
            "name": item.get("name"),
            "active_version": version,
            "status": status,
            "live_ic": round(live_ic, 6),
            "hit_rate": round(hit_rate, 6),
            "turnover": round(turnover, 6),
            "drawdown_contribution": round(drawdown_contribution, 6),
            "decay": round(decay, 6),
            "weight_multiplier": round(multiplier, 4),
            "reason": reason,
            "degraded_streak": degraded_streak,
            "quarantine_streak_required": quarantine_streak,
            "last_evaluated_date": evaluation_date,
            "evidence_maturity": {
                "status": "mature" if evidence_mature else "insufficient",
                "mature_20d_outcomes": mature_outcomes,
                "minimum_required": 5,
            },
            "feedback": feedback,
        }
        if persist:
            with business_conn() as c:
                c.execute("""INSERT INTO model_live_health
                (model_key,active_version,status,live_ic,hit_rate,turnover,drawdown_contribution,decay,weight_multiplier,reason,metrics_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(model_key) DO UPDATE SET
                active_version=excluded.active_version,status=excluded.status,live_ic=excluded.live_ic,hit_rate=excluded.hit_rate,
                turnover=excluded.turnover,drawdown_contribution=excluded.drawdown_contribution,decay=excluded.decay,
                weight_multiplier=excluded.weight_multiplier,reason=excluded.reason,metrics_json=excluded.metrics_json,updated_at=excluded.updated_at""",
                    (str(item.get("model_key") or ""), version, status, live_ic, hit_rate, turnover, drawdown_contribution,
                 decay, multiplier, reason, json.dumps(payload, ensure_ascii=False), _now()))
        rows.append(payload)
    health_actions = reconcile_plan_model_live_health(rows) if persist else {"items": []}
    return {
        "ok": True,
        "schema_version": "model_live_monitor_v2",
        "items": rows,
        "degraded_count": sum(1 for x in rows if x["status"] == "degraded"),
        "quarantined_count": sum(1 for x in rows if x["status"] == "quarantined"),
        "health_actions": health_actions,
    }


def build_portfolio_reoptimization(*, signal_date: str = "") -> dict[str, Any]:
    def run_stage(stage: str, callback):
        started_at = perf_counter()
        try:
            return callback()
        except Exception as exc:
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            raise RuntimeError(
                f"portfolio_stage={stage} elapsed_ms={elapsed_ms} error={exc}"
            ) from exc

    positions = get_positions()
    names = {str(p.get("symbol") or ""): str(p.get("name") or "") for p in positions}
    holding_symbols = [str(p.get("symbol") or "") for p in positions if str(p.get("symbol") or "")]
    decision_data_gate = _latest_decision_data_gate(signal_date)
    readiness = decision_data_gate.get("readiness") if isinstance(decision_data_gate.get("readiness"), dict) else {}
    ready_trade_date = str(readiness.get("latest_decision_ready_trade_date") or "")
    target_trade_date = str(readiness.get("target_trade_date") or "")
    # A completed prior-session bundle cannot represent the current target
    # session. Keep the gate's target date or leave it empty for an explicit
    # data-not-ready result; never derive it from a mutable snapshot maximum.
    selection_trade_date = target_trade_date
    observation_cutoff = _decision_observation_cutoff(decision_data_gate)
    decision_gate_ready = bool(decision_data_gate.get("decision_ready"))
    decision_clock_ready = bool(observation_cutoff)
    if not decision_gate_ready or not decision_clock_ready:
        detail = (
            "决策数据门虽返回 ready，但缺少可解析的实际观测截止时间；不得用默认 T 日15:00替代"
            if decision_gate_ready
            else "目标交易日决策数据未就绪；禁止用旧快照、前收或成本价构造组合"
        )
        return _blocked_portfolio_reoptimization(
            key="market_data",
            label="最新决策数据门",
            detail=detail,
            decision_data_gate=decision_data_gate,
            target_trade_date=target_trade_date,
            ready_trade_date=ready_trade_date,
            observation_cutoff=observation_cutoff,
        )

    # Current holdings are authoritative T-day inputs, just like the selection
    # universe.  Validate them before running selection, optimisation, or any
    # cost-basis-derived preview calculation.
    try:
        valuation_cache = run_stage(
            "position_market_series",
            lambda: build_position_market_series_cache(
                positions,
                as_of=selection_trade_date,
                lookback=120,
                observation_cutoff=observation_cutoff,
            ),
        )
        missing_position_valuations = run_stage(
            "position_valuation_completeness",
            lambda: missing_exact_position_valuations(
                positions,
                valuation_cache,
                as_of=selection_trade_date,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a read failure is a hard input gap
        return _blocked_portfolio_reoptimization(
            key="position_valuation",
            label="持仓点时估值",
            detail=f"读取持仓目标交易日行情失败；禁止用成本价继续计算：{str(exc)[:300]}",
            decision_data_gate=decision_data_gate,
            target_trade_date=target_trade_date,
            ready_trade_date=ready_trade_date,
            observation_cutoff=observation_cutoff,
        )
    if missing_position_valuations:
        missing_symbols = sorted(missing_position_valuations)
        return _blocked_portfolio_reoptimization(
            key="position_valuation",
            label="持仓点时估值",
            detail=(
                f"持仓缺少 {selection_trade_date} 截止 {observation_cutoff} 可见的精确行情："
                f"{', '.join(missing_symbols)}；禁止用成本价、前收或旧快照继续计算"
            ),
            decision_data_gate=decision_data_gate,
            target_trade_date=target_trade_date,
            ready_trade_date=ready_trade_date,
            observation_cutoff=observation_cutoff,
            missing_symbols=missing_symbols,
        )
    try:
        risk = run_stage(
            "live_portfolio_risk",
            lambda: build_live_portfolio_risk(
                valuation_cache=valuation_cache,
                positions=positions,
                as_of=selection_trade_date,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - keep an explicit non-executable result
        return _blocked_portfolio_reoptimization(
            key="position_valuation",
            label="持仓点时估值",
            detail=f"持仓目标交易日估值校验失败；禁止继续计算：{str(exc)[:300]}",
            decision_data_gate=decision_data_gate,
            target_trade_date=target_trade_date,
            ready_trade_date=ready_trade_date,
            observation_cutoff=observation_cutoff,
        )
    unified_selection = (
        _build_formal_unified_selection(
            trade_date=selection_trade_date,
            holding_symbols=holding_symbols,
            observation_cutoff=observation_cutoff,
        )
        if decision_gate_ready and decision_clock_ready
        else {
            "ok": False,
            "status": "blocked",
            "schema_version": "alis_unified_stock_selection_v2",
            "error": (
                "decision_observation_cutoff_missing_or_invalid"
                if decision_gate_ready and not decision_clock_ready
                else "latest_decision_data_gate_not_ready"
            ),
        }
    )
    unified_available = bool(unified_selection.get("ok")) and str(unified_selection.get("status") or "") in {"ready", "empty_opportunity"}
    core_members, candidate_members, watch_members, legacy_pool_status = _legacy_pool_preview_inputs(
        unified_available=unified_available,
    )
    core_symbols = [str(item.get("ts_code") or "").split(".")[0] for item in core_members if str(item.get("status") or "active") == "active"]
    candidate_symbols = [
        str(item.get("ts_code") or "").split(".")[0]
        for item in candidate_members[:20]
        if str(item.get("status") or "active") == "active"
    ]
    core_member_by_symbol = {
        str(item.get("ts_code") or "").split(".")[0]: item
        for item in core_members if str(item.get("status") or "active") == "active"
    }
    selection_contract = dict(unified_selection.get("selection_contract") or {}) if unified_available else {}
    unified_symbols = [
        str(value or "").split(".")[0]
        for value in (
            selection_contract.get("tradable_symbols")
            or selection_contract.get("admitted_symbols")
            or []
        )
        if str(value or "")
    ]
    legacy_symbols = list(dict.fromkeys(core_symbols + candidate_symbols))
    selection_candidate_symbols = unified_symbols if unified_available else legacy_symbols
    selection_source = "unified_full_market_selection" if unified_available else "legacy_pool_preview_blocked"
    # Existing holdings always remain in the optimisation set so the unified
    # selector can express hold/reduce/exit without losing account continuity.
    symbols = list(dict.fromkeys(holding_symbols + selection_candidate_symbols))
    valuation_as_of = selection_trade_date
    # The selection payload is immutable once hashed. Portfolio and holding
    # scope belong to the containing daily-decision contract, never inside it.
    missing_names = [symbol for symbol in symbols if not names.get(symbol)]
    if missing_names:
        with data_conn() as c:
            placeholders = ",".join("?" for _ in missing_names)
            for row in c.execute(f"SELECT split_part(ts_code,'.',1) symbol,name FROM dc_stock_master WHERE split_part(ts_code,'.',1) IN ({placeholders})", tuple(missing_names)).fetchall():
                item = dict(row)
                names[str(item.get("symbol") or "")] = str(item.get("name") or "")
    returns = run_stage(
        "position_returns",
        lambda: _position_returns(
            symbols,
            as_of_time=observation_cutoff or None,
            trade_date_end=valuation_as_of,
        ),
    )
    current_weights = {
        str(item.get("symbol") or ""): _safe_float(item.get("weight"))
        for item in (risk.get("exposures") or [])
        if str(item.get("symbol") or "")
    }
    if positions and not current_weights:
        return _blocked_portfolio_reoptimization(
            key="position_valuation",
            label="持仓点时估值",
            detail="持仓存在但风险估值没有返回任何目标日权重；禁止以成本价权重替代。",
            decision_data_gate=decision_data_gate,
            target_trade_date=target_trade_date,
            ready_trade_date=ready_trade_date,
            observation_cutoff=observation_cutoff,
            missing_symbols=holding_symbols,
        )
    contracts = risk.get("target_weight_contracts") or []
    buckets = {str(c.get("symbol") or ""): str(c.get("function_bucket") or "narrative") for c in contracts}
    exposures = {str(item.get("symbol") or ""): item for item in (risk.get("exposures") or [])}
    position_map = {str(item.get("symbol") or ""): item for item in positions}
    from app.services.execution_cost_calibration import active_execution_cost_contract
    execution_cost_contract = active_execution_cost_contract()
    one_way_cost = max(
        0.0003,
        _safe_float(execution_cost_contract.get("round_trip_rate")) / 2.0,
    )
    trading_costs = {symbol: one_way_cost for symbol in symbols}
    if unified_available:
        environment_snapshot = (
            selection_contract.get("environment_snapshot")
            if isinstance(selection_contract.get("environment_snapshot"), dict) else {}
        )
        state = str(
            environment_snapshot.get("selection_market_state")
            or environment_snapshot.get("production_market_state")
            or ""
        ).strip()
        context = {
            "market_state": {"state": state},
            "market_profile": environment_snapshot.get("market_profile") or {},
            "source": "frozen_unified_selection",
        }
    else:
        try:
            context = live_market_context(persist=True)
        except Exception:
            context = {}
        state = str((context.get("market_state") or {}).get("state") or "").strip()
    market_context_ready = state in {"S0", "S1", "S2", "S3"}
    if not market_context_ready:
        # Keep a visible unknown input rather than inventing an S1 regime.
        # Downstream preview calculations may still run for diagnosis, but the
        # dedicated execution check below makes the output non-executable.
        state = "unknown"
    market_budget = {"S0": 1.0, "S1": 0.85, "S2": 0.55, "S3": 0.70}.get(state, 0.0)
    profile_budget = max(0.85, min(1.0, _safe_float((context.get("market_profile") or {}).get("risk_budget_multiplier"), 1.0)))
    risk_status = str(risk.get("status") or "unknown")
    error_breaches = [
        item for item in (risk.get("breaches") or [])
        if str(item.get("severity") or "") == "error"
    ]
    reparable_risk_codes = {"single_name_limit", "industry_limit", "correlated_industry_limit"}
    fatal_risk_errors = [
        item for item in error_breaches
        if str(item.get("code") or "") not in reparable_risk_codes
    ]
    portfolio_budget = (
        0.0 if fatal_risk_errors
        else 0.75 if risk_status == "warning"
        else 1.0 if risk_status in {"ready", "blocked"}
        else 0.5
    )
    base_equity_budget = round(market_budget * profile_budget * portfolio_budget, 6)
    from app.services.research_tier_contract import build_tiered_equity_budget
    core_equity_symbols = [
        str(value or "").split(".")[0]
        for value in (selection_contract.get("core_symbols") or [])
        if str(value or "")
    ] if unified_available else list(selection_candidate_symbols)
    restricted_equity_symbols = [
        str(value or "").split(".")[0]
        for value in (selection_contract.get("restricted_symbols") or [])
        if str(value or "")
    ] if unified_available else []
    tiered_equity_budget = build_tiered_equity_budget(
        market_state=state,
        base_equity_budget=base_equity_budget,
        core_symbols=core_equity_symbols,
        restricted_symbols=restricted_equity_symbols,
    )
    equity_budget = float(tiered_equity_budget.get("effective_equity_budget") or 0.0)
    frozen_signal_symbols = (
        ((selection_contract.get("model_signal_snapshot") or {}).get("symbols") or {})
        if unified_available else {}
    )
    unified_selection_by_symbol = {
        str(symbol): item.get("selection")
        for symbol, item in frozen_signal_symbols.items()
        if isinstance(item, dict)
        and isinstance(item.get("selection"), dict)
        and item.get("selection")
    }
    research_evidence = {
        str(key): value
        for key, value in (
            (selection_contract.get("research_evidence") or {})
            if unified_available else (unified_selection.get("research_evidence") or {})
        ).items()
        if isinstance(value, dict)
    }
    from app.services.a_share_expected_return import build_a_share_expected_return_contract
    expected_return_contract = build_a_share_expected_return_contract(
        symbols=symbols,
        selections=unified_selection_by_symbol,
        research_evidence=research_evidence,
        returns_by_symbol=returns,
        market_state=state,
        trading_costs=trading_costs,
    )
    expected = (
        dict(expected_return_contract.get("optimizer_expected_returns") or {})
        if unified_available
        else {symbol: (mean(values[-20:]) * 20 if values else 0.0) for symbol, values in returns.items()}
    )
    if unified_available and restricted_equity_symbols:
        restricted_set = set(restricted_equity_symbols)
        for symbol in restricted_set:
            expected[symbol] = round(_safe_float(expected.get(symbol)) * 0.35, 8)
        for item in expected_return_contract.get("items") or []:
            if not isinstance(item, dict) or str(item.get("symbol") or "") not in restricted_set:
                continue
            raw_expected = _safe_float(item.get("expected_20d_return_before_trade_cost"))
            item["research_tier"] = "restricted"
            item["research_quality_discount"] = 0.35
            item["undiscounted_expected_20d_return"] = round(raw_expected, 8)
            item["expected_20d_return_before_trade_cost"] = round(raw_expected * 0.35, 8)
            item["estimated_entry_net_20d_return"] = round(
                raw_expected * 0.35 - one_way_cost,
                8,
            )
        expected_return_contract["optimizer_expected_returns"] = dict(expected)
    from app.services.defensive_assets import DEFENSIVE_ASSETS, defensive_allocation_for_state
    from app.services.production_strategy import champion_execution_gate
    execution_guard = champion_execution_gate()
    defensive_policy = defensive_allocation_for_state(
        state,
        as_of_date=selection_trade_date,
        equity_target_weight=equity_budget,
    )
    defensive_weights = {
        str(code).split(".")[0]: max(0.0, float(weight or 0.0))
        for code, weight in (defensive_policy.get("target_weights") or {}).items()
        if float(weight or 0.0) > 0
    }
    if str(execution_guard.get("mode") or "") == "research_only_sell_reduce":
        continuity = execution_guard.get("defensive_continuity") if isinstance(execution_guard.get("defensive_continuity"), dict) else {}
        maximum = max(0.0, min(0.30, float(continuity.get("maximum_total_weight") or 0.0))) if continuity.get("enabled") else 0.0
        defensive_total = sum(defensive_weights.values())
        scale = maximum / defensive_total if defensive_total > maximum > 0 else 1.0
        defensive_weights = {
            symbol: round(weight * scale, 8)
            for symbol, weight in defensive_weights.items()
            if maximum > 0
        }
    defensive_meta = {str(item["ts_code"]).split(".")[0]: item for item in DEFENSIVE_ASSETS}
    for symbol, weight in defensive_weights.items():
        if symbol not in symbols:
            symbols.append(symbol)
        names[symbol] = str((defensive_meta.get(symbol) or {}).get("name") or names.get(symbol) or symbol)
        trading_costs[symbol] = one_way_cost
    defensive_returns = run_stage(
        "defensive_returns",
        lambda: _position_returns(
            list(defensive_weights),
            as_of_time=observation_cutoff or None,
            trade_date_end=valuation_as_of,
        ),
    )
    returns.update(defensive_returns)
    expected_items = (
        expected_return_contract.get("items")
        if isinstance(expected_return_contract.get("items"), list) else []
    )
    defensive_expected_items: list[dict[str, Any]] = []
    for symbol in defensive_weights:
        values = returns.get(symbol) or []
        defensive_expected = max(-0.03, min(0.04, mean(values[-20:]) * 20 if values else 0.0))
        expected[symbol] = round(defensive_expected, 8)
        defensive_expected_items.append({
            "symbol": symbol,
            "expected_20d_return_before_trade_cost": round(defensive_expected, 8),
            "estimated_entry_net_20d_return": round(defensive_expected - one_way_cost, 8),
            "components": {"defensive_asset_point_in_time_trend": round(defensive_expected, 8)},
            "penalties": {"one_way_trade_cost_if_entered": round(one_way_cost, 8)},
            "evidence": {"return_history_count": len(values), "asset_class": str((defensive_meta.get(symbol) or {}).get("asset_class") or "")},
            "fallback_policy": "registered_defensive_asset_only",
        })
    expected_return_contract["optimizer_expected_returns"] = expected
    expected_return_contract["items"] = _merge_expected_return_items(
        expected_items,
        defensive_expected_items,
        symbol_order=symbols,
    )
    equity_candidates = [symbol for symbol in symbols if symbol not in {"510880", "511010", "511880", "512890", "518880"}]
    industry_allocation = run_stage(
        "industry_allocation",
        lambda: build_industry_allocation(
            symbols=equity_candidates,
            symbol_scores={symbol: _safe_float(expected.get(symbol)) * 100 for symbol in equity_candidates},
            equity_budget=equity_budget,
            market_state=state,
            as_of_time=str(selection_contract.get("data_as_of_time") or "") if unified_available else None,
        ),
    )
    admitted_industries = set((industry_allocation.get("industry_targets") or {}).keys())
    symbol_industries = industry_allocation.get("symbol_industries") or {}
    candidate_blocks = []
    for symbol in selection_candidate_symbols:
        industry = str(symbol_industries.get(symbol) or "未分类")
        if industry not in admitted_industries:
            candidate_blocks.append({"symbol": symbol, "name": names.get(symbol, ""), "industry": industry, "code": "industry_soft_penalty", "reason": "所属行业未获得优先预算，已施加软惩罚但未禁止入选"})
    industry_penalty = {
        symbol: (0.002 if symbol in equity_candidates and str(symbol_industries.get(symbol) or "未分类") not in admitted_industries else 0.0)
        for symbol in symbols
    }
    from app.services.strategy_controller import build_pretrade_strategy_controller
    if unified_available:
        thesis_status_by_symbol = {
            str(symbol): str((item.get("thesis_snapshot") or {}).get("derived_status") or "")
            for symbol, item in research_evidence.items()
            if isinstance(item.get("thesis_snapshot"), dict)
        }
    else:
        # Legacy compatibility mode must not scan the entire thesis table for
        # every formal plan.  That table contains evidence-heavy rows and the
        # unbounded ``ORDER BY updated_at`` projection can exceed the database
        # statement timeout before the optimizer even starts.  Unified
        # selection already carries point-in-time thesis snapshots; when it is
        # unavailable, leave thesis status explicitly unknown rather than
        # blocking plan generation on a non-authoritative full-table read.
        thesis_status_by_symbol = {}
    position_pnl_by_symbol: dict[str, float] = {}
    for symbol, position in position_map.items():
        quantity = _safe_float(position.get("quantity"))
        if quantity <= 0:
            continue
        cost_basis = _safe_float(position.get("price"))
        market_value = _safe_float((exposures.get(symbol) or {}).get("market_value"))
        if market_value <= 0:
            return _blocked_portfolio_reoptimization(
                key="position_valuation",
                label="持仓点时估值",
                detail=f"持仓 {symbol} 缺少有效目标日市值；禁止以成本价计算盈亏。",
                decision_data_gate=decision_data_gate,
                target_trade_date=target_trade_date,
                ready_trade_date=ready_trade_date,
                observation_cutoff=observation_cutoff,
                missing_symbols=[symbol],
            )
        live_price = market_value / quantity
        position_pnl_by_symbol[symbol] = live_price / cost_basis - 1.0 if cost_basis > 0 else 0.0
    position_weight_caps = {
        str(item.get("symbol") or ""): max(0.0, _safe_float(item.get("single_name_limit"), DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT))
        for item in (risk.get("exposures") or [])
        if str(item.get("symbol") or "")
    }
    restricted_symbol_cap = _safe_float(tiered_equity_budget.get("restricted_symbol_cap"))
    for symbol in restricted_equity_symbols:
        position_weight_caps[symbol] = min(
            position_weight_caps.get(symbol, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT),
            restricted_symbol_cap,
        )
    symbol_limit_overrides = dict(position_weight_caps)
    controller_risk_breaches = [dict(item) for item in error_breaches if item.get("symbol")]
    exposure_rows = [item for item in (risk.get("exposures") or []) if isinstance(item, dict)]
    for industry_item in risk.get("industry_exposures") or []:
        if str(industry_item.get("status") or "") != "breach":
            continue
        industry = str(industry_item.get("industry") or "")
        members = [item for item in exposure_rows if str(item.get("industry") or "") == industry]
        total = sum(_safe_float(item.get("weight")) for item in members)
        limit = max(0.0, _safe_float(industry_item.get("limit"), DEFAULT_INDUSTRY_LIMIT))
        scale = limit / total if total > limit > 0 else 1.0
        for item in members:
            symbol = str(item.get("symbol") or "")
            position_weight_caps[symbol] = min(
                position_weight_caps.get(symbol, 1.0),
                _safe_float(item.get("weight")) * scale,
            )
            controller_risk_breaches.append({
                "code": "industry_limit",
                "symbol": symbol,
                "severity": "error",
                "industry": industry,
            })
    if any(str(item.get("status") or "") == "breach" for item in risk.get("correlated_industry_exposures") or []):
        from app.services.rebalance_allocator import correlated_industry_group
        for group_item in risk.get("correlated_industry_exposures") or []:
            if str(group_item.get("status") or "") != "breach":
                continue
            group = str(group_item.get("group") or "")
            members = [
                item for item in exposure_rows
                if correlated_industry_group(str(item.get("industry") or "")) == group
            ]
            total = sum(_safe_float(item.get("weight")) for item in members)
            limit = max(0.0, _safe_float(group_item.get("limit"), DEFAULT_CORRELATED_INDUSTRY_LIMIT))
            scale = limit / total if total > limit > 0 else 1.0
            for item in members:
                symbol = str(item.get("symbol") or "")
                position_weight_caps[symbol] = min(
                    position_weight_caps.get(symbol, 1.0),
                    _safe_float(item.get("weight")) * scale,
                )
                controller_risk_breaches.append({
                    "code": "correlated_industry_limit",
                    "symbol": symbol,
                    "severity": "error",
                    "group": group,
                })
    reviewed_holding_symbols = [
        str(value or "").split(".")[0]
        for value in (
            (selection_contract.get("holding_admitted_symbols") or [])
            if unified_available else (unified_selection.get("holding_admitted_symbols") or [])
        )
        if str(value or "")
    ]
    risk_limit_symbols = list(dict.fromkeys(
        [*unified_symbols, *holding_symbols, *reviewed_holding_symbols, *defensive_weights.keys()]
    ))
    risk_limit_contract = build_risk_limit_contract(
        risk_limit_symbols,
        defensive_symbols=set(defensive_weights),
        symbol_overrides=symbol_limit_overrides,
        dynamic_caps=position_weight_caps,
    )
    controller_candidates = list(dict.fromkeys(unified_symbols + reviewed_holding_symbols))
    pretrade_controller = run_stage("pretrade_controller", lambda: build_pretrade_strategy_controller(
        candidate_symbols=controller_candidates,
        holding_symbols=holding_symbols,
        selections=unified_selection_by_symbol,
        research_evidence=research_evidence,
        research_tiers=(selection_contract.get("research_tier_contract") or {}).get("items") or {},
        tiered_equity_budget=tiered_equity_budget,
        expected_returns=expected,
        current_weights=current_weights,
        thesis_status_by_symbol=thesis_status_by_symbol,
        position_pnl_by_symbol=position_pnl_by_symbol,
        defensive_weights=defensive_weights,
        position_weight_caps=position_weight_caps,
        hard_risk_breaches=controller_risk_breaches,
        max_single_position=DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
        risk_limit_contract=risk_limit_contract,
        execution_guard=execution_guard,
        context={
            "signal_date": selection_trade_date,
            "data_as_of_time": str(selection_contract.get("data_as_of_time") or ""),
            "data_status": "frozen",
            "risk_budget": {"equity_budget": equity_budget, "max_single_position_pct": DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT},
            "tiered_equity_budget": tiered_equity_budget,
            "market_state": state,
        },
    ))
    controller_bounds = dict(pretrade_controller.get("weight_bounds") or {})
    target_budget = min(1.0, equity_budget + sum(defensive_weights.values()))
    optimizer = run_stage("cvar_optimizer", lambda: two_group_cvar_optimizer(
        returns,
        expected_returns=expected,
        buckets=buckets,
        current_weights=current_weights,
        trading_costs=trading_costs,
        crowding=industry_penalty,
        weight_bounds=controller_bounds,
        target_budget=target_budget,
    ))
    raw_weights = dict(optimizer.get("weights") or {})
    account_equity = _safe_float((risk.get("account") or {}).get("equity"))
    current_quantities = {
        str(item.get("symbol") or ""): max(0, int(item.get("quantity") or 0))
        for item in positions if str(item.get("symbol") or "")
    }
    reference_prices = {
        str(item.get("symbol") or ""): _safe_float(item.get("market_price"))
        for item in (risk.get("exposures") or []) if str(item.get("symbol") or "")
    }
    from app.services.position_valuation import point_in_time_position_market_series
    valuation_source = BacktestDataSource()
    for symbol in symbols:
        if _safe_float(reference_prices.get(symbol)) > 0:
            continue
        valuation = run_stage(
            "reference_price",
            lambda symbol=symbol: point_in_time_position_market_series(
                symbol=symbol,
                as_of=selection_trade_date,
                lookback=20,
                data_source=valuation_source,
                observation_cutoff=observation_cutoff or None,
            ),
        )
        reference_prices[symbol] = _safe_float(valuation.get("price"))
    from app.services.portfolio_method_competition import compete_portfolio_methods
    method_competition = run_stage("portfolio_method_competition", lambda: compete_portfolio_methods(
        returns_by_symbol=returns,
        expected_returns=expected,
        current_weights=current_weights,
        trading_costs=trading_costs,
        current_quantities=current_quantities,
        reference_prices=reference_prices,
        account_equity=account_equity,
        execution_cost_contract=execution_cost_contract,
        risk_limit_contract=risk_limit_contract,
        equity_budget=target_budget,
        cvar_weights=raw_weights,
        weight_bounds=controller_bounds,
        symbol_industries=symbol_industries,
        industry_targets=industry_allocation.get("industry_targets") or {},
        market_state=market_regime_state,
    ))
    scaled_weights = {
        symbol: round(_safe_float(weight), 8)
        for symbol, weight in (method_competition.get("weights") or {}).items()
    }
    optimizer["method_competition"] = method_competition
    optimizer["selected_method"] = method_competition.get("selected_method")
    if method_competition.get("ok"):
        optimizer["ok"] = True
        optimizer["status"] = f"selected_{method_competition.get('selected_method')}"
    block_by_symbol = {str(item.get("symbol") or ""): item for item in candidate_blocks}
    preliminary_improvement = sum(
        (_safe_float(scaled_weights.get(symbol)) - _safe_float(current_weights.get(symbol))) * _safe_float(expected.get(symbol))
        for symbol in set(current_weights) | set(scaled_weights)
    )
    winning_evaluation = method_competition.get("winner") if isinstance(method_competition.get("winner"), dict) else {}
    preliminary_cost = _safe_float(winning_evaluation.get("estimated_one_way_cost"))
    optimizer["cost_gate"] = {
        "triggered": str(method_competition.get("selected_method") or "") == "minimum_turnover_feasible",
        "expected_improvement": round(preliminary_improvement, 8),
        "estimated_one_way_cost": round(preliminary_cost, 8),
        "reason": "主动方案的成本后优势不足，采用满足全部硬义务与预算的最小换手可行方案"
        if str(method_competition.get("selected_method") or "") == "minimum_turnover_feasible" else "",
    }
    optimizer["unscaled_weights"] = raw_weights
    optimizer["weights"] = scaled_weights
    optimizer["equity_budget"] = equity_budget
    actual_equity_budget = round(sum(scaled_weights.values()), 8)
    optimizer["cash_target"] = round(1.0 - actual_equity_budget, 6)
    optimizer["weight_sum"] = actual_equity_budget
    proposal = []
    explanations = optimizer.get("explanations") or {}
    controller_by_symbol = {
        str(item.get("symbol") or ""): item
        for item in (pretrade_controller.get("actions") or [])
        if isinstance(item, dict) and str(item.get("symbol") or "")
    }
    expected_by_symbol = {
        str(item.get("symbol") or ""): item
        for item in (expected_return_contract.get("items") or [])
        if isinstance(item, dict) and str(item.get("symbol") or "")
    }
    execution_order_by_symbol = {
        str(item.get("symbol") or ""): item
        for item in (method_competition.get("execution_orders") or [])
        if isinstance(item, dict) and str(item.get("symbol") or "")
    }
    for symbol in sorted(set(current_weights) | set(scaled_weights) | set(controller_by_symbol)):
        current_weight = _safe_float(current_weights.get(symbol))
        target_weight = _safe_float(scaled_weights.get(symbol))
        delta = target_weight - current_weight
        detail = explanations.get(symbol) or {}
        expected_return = _safe_float(expected.get(symbol))
        position = position_map.get(symbol) or {}
        quantity = _safe_float(position.get("quantity"))
        cost_basis = _safe_float(position.get("price"))
        market_value = _safe_float((exposures.get(symbol) or {}).get("market_value"))
        live_price = market_value / quantity if quantity > 0 and market_value > 0 else cost_basis
        pnl_pct = live_price / cost_basis - 1.0 if cost_basis > 0 else 0.0
        full_exit = current_weight > 0.002 and target_weight <= 0.002
        new_candidate = current_weight <= 0.002 and symbol in set(selection_candidate_symbols)
        unified_evidence = unified_selection_by_symbol.get(symbol) or {}
        controller_evidence = controller_by_symbol.get(symbol) or {}
        execution_order = execution_order_by_symbol.get(symbol) or {}
        planned_quantity = max(0, int(execution_order.get("planned_quantity") or 0))
        planned_side = str(execution_order.get("side") or "").upper()
        display_action = (
            "sell" if planned_side == "SELL" and planned_quantity > 0 and target_weight <= 1e-12
            else "reduce" if planned_side == "SELL" and planned_quantity > 0
            else "buy" if planned_side == "BUY" and planned_quantity > 0
            else "observe" if str(controller_evidence.get("intent") or "") == "research_only"
            else "hold"
        )
        planned_cost = execution_order.get("cost") if isinstance(execution_order.get("cost"), dict) else {}
        proposal.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "action": display_action,
            "current_weight": round(current_weight, 8),
            "target_weight": round(target_weight, 8),
            "weight_change": round(delta, 8),
            "current_amount": round(current_weight * account_equity, 2),
            "target_amount": round(target_weight * account_equity, 2),
            "estimated_trade_amount": round(abs(delta) * account_equity, 2),
            "planned_side": planned_side,
            "planned_quantity": planned_quantity,
            "planned_lots": execution_order.get("planned_lots") if planned_quantity > 0 else 0,
            "reference_close": execution_order.get("reference_close") if planned_quantity > 0 else None,
            "estimated_execution_amount": _safe_float(execution_order.get("estimated_execution_amount")),
            "planned_cost": planned_cost,
            "expected_20d_return": round(expected_return, 8),
            "expected_return_contract": expected_by_symbol.get(symbol) or {},
            "controller_action": str(controller_evidence.get("action") or ""),
            "controller_permission_allowed": bool(controller_evidence.get("permission_allowed", True)),
            "controller_permission": controller_evidence.get("permission") or {},
            "controller_intent": str(controller_evidence.get("intent") or ""),
            "controller_obligation": controller_evidence.get("obligation") or {},
            "residual_breach": execution_order.get("residual_breach")
                or (controller_evidence.get("obligation") or {}).get("residual_breach")
                or {"active": False, "remaining_excess_weight": 0.0, "remaining_excess_amount": 0.0, "reason_code": ""},
            "controller_weight_bounds": {
                "min_weight": _safe_float(controller_evidence.get("min_weight")),
                "max_weight": _safe_float(controller_evidence.get("max_weight")),
            },
            "research_tier": str(controller_evidence.get("research_tier") or ""),
            "research_quality_discount": _safe_float(controller_evidence.get("research_quality_discount")),
            "expected_return_contribution_before": round(current_weight * expected_return, 8),
            "expected_return_contribution_after": round(target_weight * expected_return, 8),
            "estimated_one_way_cost": round(_safe_float(planned_cost.get("total_cost")) / max(account_equity, 1.0), 8),
            "cvar_contribution_after": round(_safe_float(detail.get("cvar_contribution")) * equity_budget, 8),
            "function_bucket": buckets.get(symbol, "narrative"),
            "reason": _proposal_reason(
                controller_evidence=controller_evidence,
                new_candidate=new_candidate,
                planned_quantity=planned_quantity,
                planned_side=planned_side,
                full_exit=full_exit,
                pnl_pct=pnl_pct,
                expected_return=expected_return,
                target_weight=target_weight,
                current_weight=current_weight,
                delta=delta,
                block_reason=str((block_by_symbol.get(symbol) or {}).get("reason") or ""),
            ),
            "candidate_source": (
                "unified_restricted_opportunity_pool"
                if unified_available and symbol in restricted_equity_symbols and symbol not in holding_symbols
                else "unified_core_research_pool"
                if unified_available and symbol in core_equity_symbols and symbol not in holding_symbols
                else "unified_full_market_selection"
            ) if unified_available and symbol in unified_symbols and symbol not in holding_symbols
                else "core_pool_compatibility_preview" if symbol in core_symbols and symbol not in holding_symbols
                else "candidate_pool_compatibility_preview" if symbol in candidate_symbols and symbol not in holding_symbols
                else "holding",
            "candidate_lineage": ({
                "selection_hash": str(selection_contract.get("selection_hash") or ""),
                "rank_score": unified_evidence.get("score"),
                "factor_contributions": unified_evidence.get("factor_contributions") or [],
                "model_signals": unified_evidence.get("model_signals") or {},
                "research_tier": str(controller_evidence.get("research_tier") or ""),
                "path": ["全市场点时截面", "交易硬门", "研究证据分层", "StrategyController层级折扣", "行业预算", "组合优化"],
            } if unified_available and symbol in unified_symbols and symbol not in holding_symbols else {
                "entry_pool": "core",
                "entered_at": str((core_member_by_symbol.get(symbol) or {}).get("entered_at") or ""),
                "source_reason": str((core_member_by_symbol.get(symbol) or {}).get("reason") or ""),
                "path": ["全市场", "基础过滤", "候选池", "财务研究", "行业预算", "核心池", "组合优化"],
            } if symbol in core_symbols and symbol not in holding_symbols else ({
                "entry_pool": "candidate",
                "entered_at": "",
                "source_reason": "active_candidate_pool",
                "path": ["全市场", "基础过滤", "候选池", "组合优化"],
            } if symbol in candidate_symbols and symbol not in holding_symbols else None)),
        })
    current_expected = sum(_safe_float(item["expected_return_contribution_before"]) for item in proposal)
    target_expected = sum(_safe_float(item["expected_return_contribution_after"]) for item in proposal)
    estimated_cost = _safe_float(winning_evaluation.get("estimated_one_way_cost"))
    turnover = sum(abs(_safe_float(item["weight_change"])) for item in proposal)
    expected_improvement = target_expected - current_expected
    risk_reducing_only = bool(proposal) and all(
        _safe_float(item.get("target_weight")) <= _safe_float(item.get("current_weight")) + 1e-9
        for item in proposal
    )
    controller_bound_violations = [
        {
            "symbol": symbol,
            "weight": round(_safe_float(scaled_weights.get(symbol)), 8),
            "min_weight": round(_safe_float(bound.get("min_weight")), 8),
            "max_weight": round(_safe_float(bound.get("max_weight")), 8),
        }
        for symbol, bound in controller_bounds.items()
        if (
            _safe_float(scaled_weights.get(symbol)) < _safe_float(bound.get("min_weight")) - 1e-8
            or _safe_float(scaled_weights.get(symbol)) > _safe_float(bound.get("max_weight")) + 1e-8
        )
    ]
    controller_complete = set(symbols) <= set(controller_bounds)
    unresolved_obligations = [
        str(item.get("symbol") or "")
        for item in (pretrade_controller.get("actions") or [])
        if isinstance(item, dict)
        and bool((item.get("obligation") or {}).get("required"))
        and (
            str((execution_order_by_symbol.get(str(item.get("symbol") or "")) or {}).get("side") or "") != "SELL"
            or int((execution_order_by_symbol.get(str(item.get("symbol") or "")) or {}).get("planned_quantity") or 0) <= 0
            or _safe_float(scaled_weights.get(str(item.get("symbol") or "")))
            > _safe_float((item.get("obligation") or {}).get("target_max_weight")) + 1e-8
        )
    ]
    try:
        from app.services.simulation_ledger_audit import audit_simulation_ledger
        ledger_audit = audit_simulation_ledger(persist=False)
    except Exception as exc:
        ledger_audit = {"ok": False, "status": "error", "issues": [{"message": str(exc)[:300]}]}
    decision_issue_text = "；".join(
        str(item.get("message") or "") for item in decision_data_gate.get("issues") or [] if item.get("message")
    )
    valuation_fallbacks = [
        {
            "symbol": str(item.get("symbol") or ""),
            "source": str(item.get("price_source") or ""),
            "as_of": valuation_as_of,
        }
        for item in (risk.get("exposures") or [])
        if str(item.get("price_source") or "") == "position_cost_fallback"
    ]
    execution_checks = [
        {"key": "market_data", "label": "最新决策数据门", "status": "pass" if decision_gate_ready and decision_clock_ready else "block", "detail": (
            f"{ready_trade_date or target_trade_date} 收盘数据已通过完整性检查；冻结观测截止 {observation_cutoff}"
            if decision_gate_ready and decision_clock_ready
            else (
                "决策数据门虽返回 ready，但缺少可解析的实际观测截止时间；不得用默认 T 日15:00替代"
                if decision_gate_ready
                else decision_issue_text or f"目标交易日 {target_trade_date or '未知'} 未完成，最近可用 {ready_trade_date or '无'}"
            )
        )},
        {"key": "market_context", "label": "市场状态点时证据", "status": "pass" if market_context_ready else "block", "detail": (
            f"{selection_trade_date} 已冻结市场状态 {state}"
            if market_context_ready
            else "市场状态缺失或不可识别；禁止以 S1、上一交易日或默认风险预算代替"
        )},
        {"key": "position_valuation", "label": "持仓点时估值", "status": "block" if valuation_fallbacks else "pass", "detail": (
            "以下持仓没有冻结行情，只能使用成本价；正式计划和成交已禁止："
            + ", ".join(item["symbol"] for item in valuation_fallbacks)
            if valuation_fallbacks
            else f"全部持仓均使用 {valuation_as_of} 截止、{observation_cutoff or '无'} 可见的行情估值"
        ), "fallbacks": valuation_fallbacks},
        {"key": "unified_selection", "label": "统一正式选股链路", "status": "pass" if unified_available else "block", "detail": (
            f"全市场因子模型、交易硬门与研究分层已冻结：核心 {len(core_equity_symbols)} 只，受限 {len(restricted_equity_symbols)} 只"
            if unified_available
            else f"统一选股不可用，旧研究池结果仅保留预览：{unified_selection.get('error') or unified_selection.get('status') or 'unknown'}"
        )},
        {"key": "strategy_controller", "label": "StrategyController 冻结动作与权重边界", "status": "pass" if controller_complete and not controller_bound_violations else "block", "detail": (
            f"已为 {len(controller_bounds)} 个候选、持仓和防御资产冻结动作及权重上下界"
            if controller_complete and not controller_bound_violations
            else f"动作不完整或目标权重越界：missing={sorted(set(symbols) - set(controller_bounds))} violations={controller_bound_violations}"
        )},
        {"key": "simulation_ledger", "label": "模拟账户账本一致", "status": "pass" if ledger_audit.get("ok") else "block", "detail": (
            f"现金与 {len(ledger_audit.get('actual_positions') or {})} 个持仓均可由账本独立重算"
            if ledger_audit.get("ok")
            else "；".join(str(item.get("message") or "") for item in ledger_audit.get("issues") or []) or "账本审计不可用"
        )},
        {"key": "optimizer", "label": "组合优化已完成", "status": "pass" if optimizer.get("ok") else "block", "detail": (
            "主动方案优势不足，采用完成硬风险修复后的最小换手可行方案" if (optimizer.get("cost_gate") or {}).get("triggered")
            else f"已生成目标权重（{optimizer.get('status') or 'optimal'}）" if optimizer.get("ok")
            else "；".join(optimizer.get("blocked_reasons") or ["优化器没有有效解"])
        )},
        {
            "key": "risk_gate",
            "label": "风险门控",
            "status": "block" if fatal_risk_errors or unresolved_obligations else "warn" if error_breaches else "pass",
            "detail": (
                f"不可修复风险 {len(fatal_risk_errors)} 项，未完成强制降权 {unresolved_obligations}"
                if fatal_risk_errors or unresolved_obligations
                else f"{len(error_breaches)} 项集中度超限已冻结为可执行降权单"
                if error_breaches else "无阻断风险"
            ),
        },
        {"key": "cost_benefit", "label": "收益改善覆盖成本", "status": "pass" if expected_improvement > estimated_cost else "warn", "detail": f"预期改善 {expected_improvement:.2%}，单边成本 {estimated_cost:.2%}"},
    ]
    blocked_execution_checks = [
        item for item in execution_checks
        if str(item.get("status") or "") == "block"
    ]
    execution_authorized = not blocked_execution_checks
    return {
        # A preview can still be returned for diagnosis, but it is not a
        # successful portfolio step when any deterministic execution gate is
        # blocked.  This prevents the autonomy bus from collapsing a missing
        # data/ledger/selection contract into a healthy ``ok=True`` result.
        "ok": not blocked_execution_checks,
        "status": "blocked" if blocked_execution_checks else "ready",
        "blocked_reasons": [
            {
                "key": str(item.get("key") or ""),
                "detail": str(item.get("detail") or ""),
            }
            for item in blocked_execution_checks
        ],
        "schema_version": "portfolio_reoptimization_v1",
        "optimizer": optimizer,
        "current_weights": {key: round(value, 6) for key, value in current_weights.items()},
        "rebalance_proposal": proposal,
        "portfolio_comparison": {
            "current": {"expected_20d_return": round(current_expected, 8), "cash_weight": round(max(0.0, 1.0 - sum(current_weights.values())), 8), "max_position_weight": round(max(current_weights.values(), default=0.0), 8)},
            "target": {"expected_20d_return": round(target_expected, 8), "cash_weight": optimizer["cash_target"], "max_position_weight": round(max(scaled_weights.values(), default=0.0), 8)},
            "expected_improvement": round(expected_improvement, 8), "estimated_one_way_cost": round(estimated_cost, 8),
            "net_expected_improvement": round(expected_improvement - estimated_cost, 8), "turnover": round(turnover, 8),
            "method": "A股20日预期收益合同乘以整手投影后的当前/目标权重；佣金、印花税、过户费、滑点和校准缓冲按冻结订单逐笔扣除",
        },
        "execution_checks": execution_checks,
        "execution_authorized": execution_authorized,
        "risk_reducing_only": risk_reducing_only,
        "selection_source": selection_source,
        "unified_selection": unified_selection,
        "selection_snapshot": selection_contract,
        "research_tier_contract": selection_contract.get("research_tier_contract") or {},
        "tiered_equity_budget": tiered_equity_budget,
        "expected_return_contract": expected_return_contract,
        "pretrade_controller": pretrade_controller,
        "risk_limit_contract": risk_limit_contract,
        "industry_allocation": industry_allocation,
        "portfolio_method_competition": method_competition,
        "execution_orders": method_competition.get("execution_orders") or [],
        "final_target_weights": scaled_weights,
        "decision_data_gate": {
            "status": str(decision_data_gate.get("status") or "blocked"),
            "decision_ready": bool(decision_data_gate.get("decision_ready")),
            "target_trade_date": target_trade_date,
            "latest_decision_ready_trade_date": ready_trade_date,
            "observation_cutoff": observation_cutoff or None,
            "issues": decision_data_gate.get("issues") or [],
        },
        "proposal_unit": "portfolio_fraction",
        "candidate_universe": {
            "holding_count": len(holding_symbols),
            "legacy_pool_status": legacy_pool_status,
            "watch_count": len(watch_members),
            "candidate_count": len(candidate_members),
            "core_count": len(core_symbols),
            "unified_ranked_count": len(selection_contract.get("ranked_symbols") or []),
            "unified_research_admitted_count": len(selection_contract.get("research_admitted_symbols") or []),
            "unified_admitted_count": len(unified_symbols),
            "optimization_count": len(returns),
            "new_unified_symbols": [symbol for symbol in unified_symbols if symbol not in holding_symbols],
            "industry_allocation": industry_allocation,
            "blocked_candidates": candidate_blocks,
            "admission_policy": (
                "统一全市场因子模型召回通过研究和动态硬门后，与当前持仓共同优化"
                if unified_available
                else "统一链路失败；当前持仓、核心池和候选池只生成不可执行兼容预览"
            ),
        },
        "execution_cost_contract": execution_cost_contract,
        "risk_budget": {
            "status": risk.get("status"),
            "equity_budget": actual_equity_budget,
            "cash_target": round(1.0 - actual_equity_budget, 6),
            "target_weights": risk.get("target_weights") or {},
            "breaches": risk.get("breaches") or [],
            "tiered_equity_budget": tiered_equity_budget,
            "opportunity_status": tiered_equity_budget.get("opportunity_status"),
        },
        "interpretation": "组合层统一预期收益、波动、相关性、换手成本和风险预算；这里只生成模拟目标权重，不连接券商。",
    }


def build_portfolio_candles(limit: int = 90) -> dict[str, Any]:
    """Reconstruct simulated account OHLC and its point-in-time benchmark."""
    limit = max(20, min(int(limit), 250))
    with business_conn() as c:
        operations = [dict(row) for row in c.execute("""SELECT symbol,side,quantity,price,at,
            COALESCE(commission,0) commission,COALESCE(transfer_fee,0) transfer_fee,
            COALESCE(stamp_tax,0) stamp_tax FROM operations
            WHERE UPPER(side) IN ('BUY','SELL') ORDER BY at,id""").fetchall()]
    if not operations:
        return {"ok": True, "schema_version": "portfolio_candles_v1", "series": [], "position_count": 0,
            "unit": "indexed_account_equity", "base": 100, "data_as_of_date": "",
            "benchmark": {"symbol": "000300.SH", "name": "沪深300", "available": False},
            "latest_comparison": {},
            "corporate_actions": {"count": 0, "cash_dividend_total": 0.0, "stock_dividend_shares": 0},
            "methodology": "无模拟成交账本记录。"}
    symbols = sorted({str(item.get("symbol") or "") for item in operations if item.get("symbol")})
    bars: dict[str, dict[str, dict[str, float]]] = {}
    first_date = str(operations[0].get("at") or "")[:10].replace("-", "")
    with data_conn() as c:
        target_date = target_trade_date(c)
        calendar_rows = c.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE is_open=1 AND cal_date>=? AND cal_date<=?
               ORDER BY cal_date DESC LIMIT ?""",
            (first_date, target_date, limit),
        ).fetchall()
        dates = sorted({
            str(dict(row).get("cal_date") or "").replace("-", "")[:8]
            for row in calendar_rows
            if len(str(dict(row).get("cal_date") or "").replace("-", "")[:8]) == 8
        })
        if not dates:
            return {
                "ok": False,
                "status": "blocked",
                "data_available": False,
                "reason": "portfolio_trade_calendar_missing",
                "series": [],
                "position_count": 0,
                "data_as_of_date": target_date,
                "message": "账户曲线无法取得权威交易日列表，禁止按现有行情日期拼接曲线。",
            }
        for symbol in symbols:
            bare = symbol.split(".", 1)[0]
            code = (
                symbol if "." in symbol
                else normalize_exchange_etf_ts_code(symbol) if is_exchange_etf(bare)
                else f"{symbol}.SH" if bare.startswith(("5", "6", "9")) else f"{symbol}.SZ"
            )
            api_name = "fund_daily" if is_exchange_etf(bare) else "daily"
            raw = [dict(row) for row in c.execute("""SELECT trade_date,payload_json
                FROM dc_tushare_raw_records
                WHERE api_name=? AND ts_code IN (?,?) AND trade_date>=? AND trade_date<=?
                ORDER BY trade_date""", (api_name, symbol, code, dates[0], dates[-1])).fetchall()]
            rows = []
            for item in raw:
                try:
                    payload = json.loads(item.get("payload_json") or "{}")
                    rows.append({"trade_date": item.get("trade_date"), **payload})
                except json.JSONDecodeError:
                    continue
            bars[symbol] = {str(row.get("trade_date") or ""): {field: _safe_float(row.get(field)) for field in ("open", "high", "low", "close")}
                for row in rows if row.get("trade_date") and all(_safe_float(row.get(field)) > 0 for field in ("open", "high", "low", "close"))}
    from app.services.corporate_actions import build_replay_corporate_actions, load_dividend_events
    dividend_events = load_dividend_events(
        symbols,
        as_of_date=dates[-1] if dates else first_date,
        strict_event_time=True,
    )
    corporate_actions = build_replay_corporate_actions(operations, dividend_events)
    cash = _safe_float(get_account_baseline())
    quantities: dict[str, float] = {}
    op_index = 0
    action_index = 0
    raw_series: list[tuple[str, dict[str, float]]] = []
    missing_ohlc_by_date: dict[str, list[str]] = {}
    for date in dates:
        while op_index < len(operations) and str(operations[op_index].get("at") or "")[:10].replace("-", "") <= date:
            operation = operations[op_index]
            symbol = str(operation.get("symbol") or "")
            quantity = _safe_float(operation.get("quantity"))
            amount = quantity * _safe_float(operation.get("price"))
            fees = sum(_safe_float(operation.get(key)) for key in ("commission", "transfer_fee", "stamp_tax"))
            if str(operation.get("side") or "").upper() == "BUY":
                quantities[symbol] = quantities.get(symbol, 0.0) + quantity
                cash -= amount + fees
            else:
                quantities[symbol] = max(0.0, quantities.get(symbol, 0.0) - quantity)
                cash += amount - fees
            op_index += 1
        while action_index < len(corporate_actions) and str(corporate_actions[action_index].get("event_date") or "") <= date:
            action = corporate_actions[action_index]
            symbol = str(action.get("symbol") or "")
            if str(action.get("action_type") or "") == "cash_dividend":
                cash += _safe_float(action.get("cash_amount"))
            elif str(action.get("action_type") or "") == "stock_dividend":
                quantities[symbol] = quantities.get(symbol, 0.0) + int(action.get("share_quantity") or 0)
            action_index += 1
        point = {field: cash for field in ("open", "high", "low", "close")}
        missing_symbols: list[str] = []
        for symbol, quantity in quantities.items():
            if quantity <= 0:
                continue
            bar = bars.get(symbol, {}).get(date)
            if not bar:
                missing_symbols.append(symbol)
                continue
            for field in point:
                point[field] += bar[field] * quantity
        if missing_symbols:
            missing_ohlc_by_date[date] = sorted(set(missing_symbols))
        else:
            raw_series.append((date, point))
    if missing_ohlc_by_date:
        return {
            "ok": False,
            "status": "blocked",
            "data_available": False,
            "reason": "exact_account_ohlc_missing",
            "series": [],
            "position_count": len([quantity for quantity in quantities.values() if quantity > 0]),
            "data_as_of_date": target_date,
            "missing_ohlc_by_date": missing_ohlc_by_date,
            "message": "账户曲线缺少持仓的精确交易日 OHLC；禁止跳过缺口或沿用旧行情。",
        }
    base = raw_series[0][1]["close"] if raw_series else 0.0
    series = [{
        "date": date,
        **{field: round(point[field] / base * 100.0, 4) if base > 0 else 0.0 for field in ("open", "high", "low", "close")},
        "change_pct": round((point["close"] / raw_series[index - 1][1]["close"] - 1) * 100, 4) if index > 0 else 0.0,
    } for index, (date, point) in enumerate(raw_series)]
    benchmark_closes = _load_point_in_time_benchmark_closes([item["date"] for item in series])
    series, latest_comparison = _attach_benchmark_series(series, benchmark_closes)
    return {
        "ok": True,
        "schema_version": "portfolio_candles_v3",
        "series": series,
        "position_count": len([quantity for quantity in quantities.values() if quantity > 0]),
        "unit": "indexed_account_equity",
        "base": 100,
        "data_as_of_date": series[-1]["date"] if series else "",
        "benchmark": {
            "symbol": "000300.SH",
            "name": "沪深300",
            "available": bool(benchmark_closes),
            "point_in_time_cutoff": "每个交易日15:00",
        },
        "latest_comparison": latest_comparison,
        "corporate_actions": {
            "count": len(corporate_actions),
            "cash_dividend_total": round(sum(_safe_float(item.get("cash_amount")) for item in corporate_actions), 2),
            "stock_dividend_shares": sum(int(item.get("share_quantity") or 0) for item in corporate_actions),
            "policy": "现金分红在派息日入账，送股在除权日增加数量；资格按股权登记日模拟持仓冻结。",
        },
        "methodology": "从首笔模拟成交开始逐日重放买卖、费用、分红和现金，聚合当日持仓开高低收；账户与点时沪深300均在共同首日归一化为100。",
    }


def _load_point_in_time_benchmark_closes(dates: list[str]) -> dict[str, float]:
    normalized_dates = sorted({str(value or "").replace("-", "")[:8] for value in dates if value})
    if not normalized_dates:
        return {}
    try:
        with data_conn() as c:
            rows = [
                dict(row) for row in c.execute(
                    """SELECT id,trade_date,payload_json,available_time,observed_at,first_observed_at
                       FROM dc_tushare_raw_records
                       WHERE api_name='index_daily' AND ts_code='000300.SH'
                         AND trade_date>=? AND trade_date<=?
                       ORDER BY trade_date,available_time,id""",
                    (normalized_dates[0], normalized_dates[-1]),
                ).fetchall()
            ]
    except Exception:
        return {}
    wanted = set(normalized_dates)
    result: dict[str, float] = {}
    for row in rows:
        trade_date = str(row.get("trade_date") or "").replace("-", "")[:8]
        # Portfolio display may show late-observed benchmark closes so the
        # account curve continues after the strict PIT research window.
        # Research/OOS consumers keep using _available_by_market_close.
        observation_time = (
            row.get("available_time")
            or row.get("observed_at")
            or row.get("first_observed_at")
        )
        if trade_date not in wanted or not observation_time:
            continue
        try:
            close = float(json.loads(str(row.get("payload_json") or "{}")).get("close") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if close > 0:
            result[trade_date] = close
    return result


def _available_by_market_close(value: Any, trade_date: str) -> bool:
    if not value:
        return False
    try:
        available = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        cutoff = datetime.strptime(str(trade_date), "%Y%m%d").replace(hour=15)
        if available.tzinfo is not None:
            cutoff = cutoff.replace(tzinfo=available.tzinfo)
        return available <= cutoff
    except (TypeError, ValueError):
        return False


def _attach_benchmark_series(
    series: list[dict[str, Any]],
    benchmark_closes: dict[str, float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    common = [
        item for item in series
        if _safe_float(benchmark_closes.get(str(item.get("date") or ""))) > 0
    ]
    if not common:
        return series, {}
    benchmark_base = _safe_float(benchmark_closes[str(common[0]["date"])])
    previous_benchmark_index: float | None = None
    enriched: list[dict[str, Any]] = []
    for item in series:
        output = dict(item)
        raw_close = _safe_float(benchmark_closes.get(str(item.get("date") or "")))
        if raw_close > 0 and benchmark_base > 0:
            benchmark_index = raw_close / benchmark_base * 100.0
            benchmark_change = (
                (benchmark_index / previous_benchmark_index - 1.0) * 100.0
                if previous_benchmark_index and previous_benchmark_index > 0 else 0.0
            )
            portfolio_close = _safe_float(item.get("close"))
            output.update({
                "benchmark_close": round(benchmark_index, 4),
                "benchmark_change_pct": round(benchmark_change, 4),
                "excess_close": round(portfolio_close - benchmark_index, 4),
                "excess_change_pct": round(_safe_float(item.get("change_pct")) - benchmark_change, 4),
            })
            previous_benchmark_index = benchmark_index
        else:
            output.update({
                "benchmark_close": None,
                "benchmark_change_pct": None,
                "excess_close": None,
                "excess_change_pct": None,
            })
        enriched.append(output)
    latest = next(
        (item for item in reversed(enriched) if item.get("benchmark_close") is not None),
        None,
    )
    if not latest:
        return enriched, {}
    portfolio_return = _safe_float(latest.get("close")) - 100.0
    benchmark_return = _safe_float(latest.get("benchmark_close")) - 100.0
    return enriched, {
        "date": latest.get("date"),
        "portfolio_return_pct": round(portfolio_return, 4),
        "benchmark_return_pct": round(benchmark_return, 4),
        "excess_return_pct": round(portfolio_return - benchmark_return, 4),
        "portfolio_daily_return_pct": latest.get("change_pct"),
        "benchmark_daily_return_pct": latest.get("benchmark_change_pct"),
        "daily_excess_return_pct": latest.get("excess_change_pct"),
    }


def build_model_return_contribution() -> dict[str, Any]:
    """Expose honest model evidence while counterfactual portfolio returns accumulate."""
    _ensure_tables()
    with business_conn() as c:
        rows = [dict(row) for row in c.execute("""SELECT d.model_version,a.horizon_sessions,
            COUNT(*) samples,
            AVG(a.directional_return) avg_directional_return,
            AVG(a.benchmark_return) avg_benchmark_return,
            AVG(a.directional_return-COALESCE(a.benchmark_return,0)) avg_excess_return,
            AVG(CASE WHEN a.correct=1 THEN 1.0 ELSE 0.0 END) hit_rate
            FROM decision_outcome_attributions a
            JOIN decision_snapshots d ON d.id=a.decision_snapshot_id
            GROUP BY d.model_version,a.horizon_sessions
            ORDER BY d.model_version,a.horizon_sessions""").fetchall()]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("model_version") or "unknown"), []).append({
            "horizon_sessions": int(row.get("horizon_sessions") or 0),
            "samples": int(row.get("samples") or 0),
            "avg_directional_return_pct": round(_safe_float(row.get("avg_directional_return")) * 100, 6),
            "avg_benchmark_return_pct": round(_safe_float(row.get("avg_benchmark_return")) * 100, 6),
            "avg_excess_return_pct": round(_safe_float(row.get("avg_excess_return")) * 100, 6),
            "hit_rate": round(_safe_float(row.get("hit_rate")), 6),
        })
    family = model_family_status()
    items = []
    for model in family.get("items") or []:
        active = model.get("active_version") or model.get("latest_version") or {}
        version = str(active.get("version") or "")
        evidence = grouped.get(version, [])
        mature = next((x for x in evidence if x["horizon_sessions"] == 20), None)
        items.append({
            "model_key": model.get("model_key"),
            "name": model.get("name"),
            "model_version": version,
            "decision_evidence": evidence,
            "preferred_20d_evidence": mature,
            "portfolio_return_contribution_pct": None,
            "marginal_return_contribution_pct": None,
            "contribution_status": "insufficient_samples",
            "required_method": "leave_one_model_out_shadow_portfolio_v1",
            "minimum_shadow_sessions": 20,
            "reason": "决策结果可用于验证方向与超额收益，但没有逐日影子组合净值，不能可靠归因为账户收益贡献。",
        })
    return {
        "ok": True,
        "schema_version": "model_return_contribution_v1",
        "items": items,
        "methodology": {
            "decision_evidence": "按模型版本和固定持有期聚合已成熟决策结果",
            "portfolio_contribution": "必须用完整组合与剔除单模型的影子组合逐日净值差计算",
            "no_fabrication": True,
        },
    }


def _plan_block_status(portfolio: dict[str, Any]) -> tuple[str, str]:
    blocked = [item for item in portfolio.get("execution_checks") or [] if item.get("status") == "block"]
    if not blocked:
        return "planned", ""
    keys = {str(item.get("key") or "") for item in blocked}
    if "system_release" in keys:
        return "blocked_by_system_release", "当前运行组件与已激活系统发布不一致，禁止生成可执行计划"
    if "market_data" in keys:
        return "blocked_by_decision_data", "最新交易日决策数据门未通过，当前方案仅作为不可执行预览"
    if "market_context" in keys:
        return "blocked_by_market_context", "市场状态缺少目标交易日点时证据，禁止用默认或旧状态生成可执行计划"
    if "position_valuation" in keys:
        return "blocked_by_position_valuation", "持仓缺少冻结点时行情，仅有成本价估值；禁止生成可执行计划"
    if "unified_selection" in keys:
        return "blocked_by_selection", "统一全市场选股链路未完成，旧研究池结果仅作为不可执行预览"
    if "simulation_ledger" in keys:
        return "blocked_by_ledger", "模拟账户账本不一致，禁止生成可执行计划"
    if "risk_gate" in keys:
        return "blocked_by_risk", "方案会增加或维持禁止的风险暴露，风险门拒绝执行"
    if "optimizer" in keys:
        return "blocked_by_optimizer", "组合优化没有形成安全可执行解"
    return "blocked_by_execution_checks", "执行前置检查未全部通过"


def build_daily_decision_contract(
    *,
    portfolio: dict[str, Any],
    trade_date: str,
    planned_execute_date: str,
) -> dict[str, Any]:
    """Build the immutable T-day decision payload without persisting a plan."""
    from app.services.decision_version_manifest import build_decision_version_manifest
    from app.services.system_release_center import release_status

    decision_gate = portfolio.get("decision_data_gate") if isinstance(portfolio.get("decision_data_gate"), dict) else {}
    proposal = portfolio.get("rebalance_proposal") or []
    execution_checks = [
        dict(item) for item in (portfolio.get("execution_checks") or [])
        if isinstance(item, dict)
    ]
    release_gate = release_status()
    execution_checks.append({
        "key": "system_release",
        "status": "pass" if release_gate.get("ok") else "block",
        "detail": "active system release matches observed runtime"
        if release_gate.get("ok") else str(release_gate.get("blocked_reason") or "system release unavailable"),
        "release_id": int((release_gate.get("active_release") or {}).get("id") or 0),
        "drift_components": list(release_gate.get("drift_components") or []),
    })
    execution_authorized = bool(decision_gate.get("decision_ready")) and bool(execution_checks) and not any(
        str(item.get("status") or "") == "block" for item in execution_checks
    )
    decision_version_manifest = build_decision_version_manifest(
        trade_date=trade_date,
        portfolio=portfolio,
    )
    final_target_weights = {
        str(symbol): round(max(0.0, _safe_float(weight)), 8)
        for symbol, weight in (portfolio.get("final_target_weights") or {}).items()
        if str(symbol)
    }
    # Any positive frozen weight remains an execution target.  A display or
    # rebalance materiality threshold must never turn a small holding into an
    # implicit T+1 liquidation.
    target_symbols = [symbol for symbol, weight in final_target_weights.items() if weight > 1e-12]
    execution_target_weights = {
        symbol: weight for symbol, weight in final_target_weights.items() if symbol in target_symbols
    }
    unified_contract = str(portfolio.get("selection_source") or "") == "unified_full_market_selection"
    return {
        "schema_version": UNIFIED_DAILY_DECISION_SCHEMA_VERSION if unified_contract else "alis_legacy_daily_plan_v1",
        "trade_date": trade_date,
        "signal_date": trade_date,
        "data_as_of_time": str((portfolio.get("selection_snapshot") or {}).get("data_as_of_time") or ""),
        "planned_execute_date": planned_execute_date,
        "proposal": proposal,
        "target_symbols": target_symbols,
        "target_weights": execution_target_weights,
        "final_target_weights": final_target_weights,
        "risk_budget": portfolio.get("risk_budget") or {},
        "risk_limit_contract": portfolio.get("risk_limit_contract") or {},
        "execution_authorized": execution_authorized,
        "execution_checks": execution_checks,
        "decision_data_gate": decision_gate,
        "selection_snapshot": portfolio.get("selection_snapshot") or {},
        "research_tier_contract": portfolio.get("research_tier_contract") or {},
        "tiered_equity_budget": portfolio.get("tiered_equity_budget") or {},
        "pretrade_controller": portfolio.get("pretrade_controller") or {},
        "industry_allocation": portfolio.get("industry_allocation") or {},
        "expected_return_contract": portfolio.get("expected_return_contract") or {},
        "portfolio_method_competition": portfolio.get("portfolio_method_competition") or {},
        "execution_orders": portfolio.get("execution_orders") or [],
        "execution_cost_contract": portfolio.get("execution_cost_contract") or {},
        "execution_safety_contract": {
            "schema_version": UNIFIED_EXECUTION_SAFETY_SCHEMA_VERSION,
            "investment_opinion_frozen_on_signal_date": True,
            "t1_rerank_forbidden": True,
            "t1_research_refresh_forbidden": True,
            "t1_industry_reallocation_forbidden": True,
            "t1_controller_regeneration_forbidden": True,
            "t1_portfolio_reoptimization_forbidden": True,
            "t1_quantity_recalculation_forbidden": True,
            "frozen_quantity_must_execute_exactly_or_be_vetoed": True,
            "allowed_vetoes": [
                "missing_open_price", "suspension", "st_status", "limit_up_down", "lot_size",
                "liquidity_capacity", "cash_no_overdraft", "no_oversell",
                "t1_sell_restriction", "ledger_consistency",
                "overnight_champion_restriction_only",
            ],
        },
        "decision_version_manifest": decision_version_manifest,
    }


def _persist_contract_validation_failure(
    *,
    contract: dict[str, Any],
    trade_date: str,
    planned_execute_date: str,
    error: Exception,
) -> dict[str, Any]:
    """Persist a non-executable candidate without pretending it is a plan."""
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    error_code = str(getattr(error, "code", "contract_validation_failed") or "contract_validation_failed")
    error_payload = [{"code": error_code, "message": str(error)[:1000]}]
    _ensure_tables()
    with business_conn() as c:
        existing = c.execute(
            "SELECT * FROM daily_rebalance_plans WHERE trade_date=? AND input_hash=?",
            (trade_date, input_hash),
        ).fetchone()
        if existing:
            return get_latest_daily_rebalance_plan(trade_date)
        latest = c.execute(
            "SELECT COALESCE(MAX(version),0) version FROM daily_rebalance_plans WHERE trade_date=?",
            (trade_date,),
        ).fetchone()
        version = int(dict(latest or {}).get("version") or 0) + 1
        c.execute(
            """INSERT INTO daily_rebalance_plans
               (trade_date,planned_execute_date,planned_execute_at,version,input_hash,status,
                proposal_json,generated_at,validation_error_code,validation_errors_json,validated_at,release_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_date,
                planned_execute_date,
                _planned_execute_at(planned_execute_date),
                version,
                input_hash,
                "blocked_by_contract_validation",
                canonical,
                _now(),
                error_code,
                json.dumps(error_payload, ensure_ascii=False, sort_keys=True),
                _now(),
                int((((contract.get("decision_version_manifest") or {}).get("release") or {}).get("id")) or 0) or None,
            ),
        )
        inserted = c.execute(
            "SELECT id FROM daily_rebalance_plans WHERE trade_date=? AND version=?",
            (trade_date, version),
        ).fetchone()
        if inserted:
            _persist_plan_projection(c, int(dict(inserted).get("id") or 0), contract, input_hash)
    return get_latest_daily_rebalance_plan(trade_date)


_PLAN_MATERIALIZATION_LOCK_NAMESPACE = "alis:daily-plan-materialization:"
_PLAN_DATA_IO_LOCK_KEY = "alis:daily-plan-data-io"


def _plan_materialization_in_progress(*, signal_date: str, reason: str = "") -> dict[str, Any]:
    return {
        "id": None,
        "trade_date": str(signal_date or ""),
        "data_as_of_date": str(signal_date or ""),
        "planned_execute_date": "",
        "planned_execute_at": "",
        "version": None,
        "plan_version": "",
        "status": "materialization_in_progress",
        "persisted": False,
        "is_executed": False,
        "execution_authorized": False,
        "retryable": True,
        "blocked_reason": reason or "同一信号日已有正式计划生成任务运行；本次让路并由调度器自动重试",
        "proposal": [],
        "selection_snapshot": {},
        "decision_contract_schema_version": UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        "generation_policy": "正式计划生成跨进程单飞；禁止并发重算同一信号日",
    }


@contextmanager
def _plan_materialization_lease(signal_date: str):
    """Serialize the expensive formal-plan build across API and workers.

    The plan is built from the data-platform database but persisted in the
    business database.  A PostgreSQL session advisory lock on the business
    database is therefore the smallest cross-process single-flight boundary;
    SQLite/MySQL test doubles simply run without the production lock.
    """
    key = str(signal_date or "").replace("-", "")[:8]
    if not key or not business_is_postgres():
        yield True
        return
    lock_key = f"{_PLAN_MATERIALIZATION_LOCK_NAMESPACE}{key}"
    with business_read_conn() as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(?)) AS acquired",
            (lock_key,),
        ).fetchone()
        acquired = bool(row.get("acquired") if isinstance(row, dict) else row[0])
        if not acquired:
            yield False
            return
        data_conn_scope = data_read_conn()
        data_conn = data_conn_scope.__enter__()
        data_acquired = False
        data_io_acquired = False
        try:
            if bool(getattr(data_conn, "_is_postgres", False)):
                data_row = data_conn.execute(
                    "SELECT pg_try_advisory_lock(hashtext(?)) AS acquired",
                    (lock_key,),
                ).fetchone()
                data_acquired = bool(
                    data_row.get("acquired") if isinstance(data_row, dict) else data_row[0]
                )
            else:
                data_acquired = True
            if not data_acquired:
                yield False
                return
            if bool(getattr(data_conn, "_is_postgres", False)):
                io_row = data_conn.execute(
                    "SELECT pg_try_advisory_lock(hashtext(?)) AS acquired",
                    (_PLAN_DATA_IO_LOCK_KEY,),
                ).fetchone()
                data_io_acquired = bool(
                    io_row.get("acquired") if isinstance(io_row, dict) else io_row[0]
                )
            else:
                data_io_acquired = True
            if not data_io_acquired:
                yield False
                return
            yield True
        finally:
            if data_io_acquired and bool(getattr(data_conn, "_is_postgres", False)):
                try:
                    data_conn.execute(
                        "SELECT pg_advisory_unlock(hashtext(?))",
                        (_PLAN_DATA_IO_LOCK_KEY,),
                    )
                except Exception:  # noqa: BLE001 - closing the session releases it
                    pass
            if data_acquired and bool(getattr(data_conn, "_is_postgres", False)):
                try:
                    data_conn.execute("SELECT pg_advisory_unlock(hashtext(?))", (lock_key,))
                except Exception:  # noqa: BLE001 - closing the session releases it
                    pass
            data_conn_scope.__exit__(None, None, None)
            try:
                conn.execute("SELECT pg_advisory_unlock(hashtext(?))", (lock_key,))
            except Exception:  # noqa: BLE001 - closing the session releases it
                pass


def _materialize_daily_rebalance_plan_with_portfolio(
    *, signal_date: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = str(signal_date or "").replace("-", "")[:8]
    with _plan_materialization_lease(key) as acquired:
        if not acquired:
            return _plan_materialization_in_progress(signal_date=key), {}
        portfolio = build_portfolio_reoptimization(signal_date=key)
        return _materialize_daily_rebalance_plan_locked(portfolio, signal_date=key), portfolio


def _controlled_rebuild_allowed(status: str, *, rebuild_blocked: bool) -> bool:
    """Allow only an explicit rebuild of a contract-invalidated candidate.

    The original row remains immutable evidence.  Execution-recovery and
    other operational blocks must never be bypassed by a materialization retry.
    """
    return bool(rebuild_blocked) and str(status or "") == "blocked_by_contract_validation"


def materialize_daily_rebalance_plan(
    portfolio: dict[str, Any] | None = None,
    *,
    signal_date: str = "",
    rebuild_blocked: bool = False,
) -> dict[str, Any]:
    key = str(signal_date or "").replace("-", "")[:8]
    if not key and isinstance(portfolio, dict):
        gate = portfolio.get("decision_data_gate")
        if isinstance(gate, dict):
            key = str(gate.get("target_trade_date") or "").replace("-", "")[:8]
    with _plan_materialization_lease(key) as acquired:
        if not acquired:
            return _plan_materialization_in_progress(signal_date=key)
        return _materialize_daily_rebalance_plan_locked(
            portfolio,
            signal_date=key,
            rebuild_blocked=rebuild_blocked,
        )


def _materialize_daily_rebalance_plan_locked(
    portfolio: dict[str, Any] | None = None,
    *,
    signal_date: str = "",
    rebuild_blocked: bool = False,
) -> dict[str, Any]:
    """Persist one immutable plan version per materially different daily input."""
    # The wall clock may already be on the next calendar day when the prior
    # close bundle finishes. An explicit completed signal date prevents the
    # planner from selecting tomorrow's incomplete target session.
    if portfolio is None:
        try:
            portfolio = build_portfolio_reoptimization(signal_date=signal_date)
        except Exception as exc:
            raise RuntimeError(f"portfolio_reoptimization_failed: {exc}") from exc
    decision_gate = portfolio.get("decision_data_gate") if isinstance(portfolio.get("decision_data_gate"), dict) else {}
    trade_date = str(decision_gate.get("target_trade_date") or "")
    if not trade_date:
        return {
            "id": None,
            "trade_date": "",
            "data_as_of_date": "",
            "planned_execute_date": "",
            "planned_execute_at": "",
            "version": None,
            "plan_version": "",
            "status": "blocked_by_decision_data",
            "persisted": False,
            "is_executed": False,
            "execution_authorized": False,
            "retryable": True,
            "blocked_reason": "决策数据门没有提供权威目标交易日，禁止从历史快照推断并创建正式计划",
            "proposal": [],
            "selection_snapshot": {},
            "decision_contract_schema_version": UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        }
    if str(portfolio.get("status") or "") == "blocked":
        plan_status, blocked_reason = _plan_block_status(portfolio)
        return {
            "id": None,
            "trade_date": trade_date,
            "data_as_of_date": trade_date,
            "planned_execute_date": "",
            "planned_execute_at": "",
            "version": None,
            "plan_version": "",
            "status": plan_status,
            "persisted": False,
            "is_executed": False,
            "execution_authorized": False,
            "retryable": True,
            "blocked_reason": blocked_reason,
            "proposal": [],
            "selection_snapshot": {},
            "decision_contract_schema_version": UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
            "generation_policy": "关键输入未就绪；不创建不完整冻结计划，等待下一次真实数据探测。",
        }
    if str(portfolio.get("selection_source") or "") != "unified_full_market_selection":
        return {
            "id": None,
            "trade_date": trade_date,
            "data_as_of_date": trade_date,
            "planned_execute_date": "",
            "planned_execute_at": "",
            "version": None,
            "plan_version": "",
            "status": "compatibility_preview_only",
            "persisted": False,
            "is_executed": False,
            "execution_authorized": False,
            "blocked_reason": "统一全市场选股合同未完成；旧研究池结果只保留兼容预览，不能写入正式计划",
            "proposal": portfolio.get("rebalance_proposal") or [],
            "selection_snapshot": portfolio.get("selection_snapshot") or {},
            "decision_contract_schema_version": "alis_legacy_daily_plan_v1",
            "generation_policy": "旧选股池和旧合同只读兼容，不得新建或覆盖正式调仓计划",
        }
    _ensure_tables()
    with business_conn() as c:
        latest_same_day_query = """SELECT status FROM daily_rebalance_plans
               WHERE trade_date=? ORDER BY version DESC LIMIT 1"""
        if getattr(c, "_is_postgres", False) or getattr(c, "_is_mysql", False):
            latest_same_day_query += " FOR UPDATE"
        latest_same_day = c.execute(latest_same_day_query, (trade_date,)).fetchone()
    latest_same_day_status = str(dict(latest_same_day or {}).get("status") or "")
    controlled_rebuild = _controlled_rebuild_allowed(
        latest_same_day_status,
        rebuild_blocked=rebuild_blocked,
    )
    if latest_same_day_status == "planned":
        frozen_plan = get_latest_daily_rebalance_plan(trade_date)
        frozen_plan["generation_policy"] = (
            "同一信号日已有正式计划；计划合同已冻结，后续研究证据变化不得重生成或替换 T+1 计划"
        )
        return frozen_plan
    if latest_same_day_status in {
        "blocked_by_contract_revision",
        "blocked_by_contract_validation",
        "blocked_by_system_release",
        "blocked_by_decision_data",
        "blocked_by_market_context",
        "blocked_by_position_valuation",
        "blocked_by_selection",
        "blocked_by_ledger",
        "blocked_by_risk",
        "blocked_by_optimizer",
        "blocked_by_execution_checks",
        "blocked_by_execution_recovery",
    } and not controlled_rebuild:
        blocked_plan = get_latest_daily_rebalance_plan(trade_date)
        blocked_plan["generation_policy"] = (
            "同一信号日已有不可执行合同；保留原始证据且禁止重复生成，"
            "新版合同从下一个有效信号日开始生成"
        )
        return blocked_plan
    with data_conn() as c:
        next_row = c.execute(
            "SELECT MIN(cal_date) value FROM dc_trade_calendar WHERE is_open=1 AND cal_date>?", (trade_date,)
        ).fetchone() if trade_date else None
    planned_execute_date = str(dict(next_row or {}).get("value") or "")
    planned_execute_at = _planned_execute_at(planned_execute_date)
    contract = build_daily_decision_contract(
        portfolio=portfolio,
        trade_date=trade_date,
        planned_execute_date=planned_execute_date,
    )
    from app.services.quant_run_plan_input import validate_unified_decision_contract
    try:
        validate_unified_decision_contract(contract)
    except Exception as exc:
        from app.services.quant_run_contracts import ReplayRiskGateError
        if not isinstance(exc, ReplayRiskGateError):
            raise
        return _persist_contract_validation_failure(
            contract=contract,
            trade_date=trade_date,
            planned_execute_date=planned_execute_date,
            error=exc,
        )
    proposal = contract["proposal"]
    decision_version_manifest = contract["decision_version_manifest"]
    execution_authorized = bool(contract.get("execution_authorized"))
    plan_status, blocked_reason = _plan_block_status(contract)
    if execution_authorized:
        plan_status, blocked_reason = "planned", ""
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    _ensure_tables()
    with business_conn() as c:
        inflight = c.execute(
            """SELECT * FROM daily_rebalance_plans
               WHERE trade_date=? AND status IN ('executing','blocked_by_execution_recovery')
               ORDER BY version DESC LIMIT 1""",
            (trade_date,),
        ).fetchone()
        existing = c.execute(
            "SELECT * FROM daily_rebalance_plans WHERE trade_date=? AND input_hash=?",
            (trade_date, input_hash),
        ).fetchone()
        if inflight:
            row = dict(inflight)
        elif existing:
            row = dict(existing)
        else:
            latest = c.execute(
                "SELECT COALESCE(MAX(version),0) version FROM daily_rebalance_plans WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            version = int(dict(latest or {}).get("version") or 0) + 1
            if version > 1:
                c.execute("UPDATE daily_rebalance_plans SET status='superseded' WHERE trade_date=? AND status='planned'", (trade_date,))
            c.execute("""INSERT INTO daily_rebalance_plans
                (trade_date,planned_execute_date,planned_execute_at,version,input_hash,status,proposal_json,generated_at,release_id)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (trade_date, planned_execute_date, planned_execute_at, version, input_hash, plan_status, canonical, _now(),
                 int(((decision_version_manifest.get("release") or {}).get("id")) or 0) or None))
            inserted = c.execute(
                "SELECT id FROM daily_rebalance_plans WHERE trade_date=? AND version=?",
                (trade_date, version),
            ).fetchone()
            if inserted:
                _persist_plan_projection(c, int(dict(inserted).get("id") or 0), contract, input_hash)
            # The frozen contract is stored in proposal_json and can be very
            # large for a full-market selection. This response only needs
            # plan metadata; selecting the full row here can transfer the
            # entire JSON payload over the production DB connection and hit
            # statement_timeout immediately after the INSERT. Keep the
            # authoritative contract in the row, but read back only the
            # columns needed to build the materialization result.
            row = dict(c.execute(
                """SELECT id,trade_date,planned_execute_date,planned_execute_at,
                          version,input_hash,status,execution_id,generated_at,
                          executed_at,release_id
                     FROM daily_rebalance_plans
                    WHERE trade_date=? AND version=?""",
                (trade_date, version),
            ).fetchone())
    row_status = str(row.get("status") or plan_status)
    row_blocked_reason = {
        "executing": "正式调仓计划已被执行器领取，正在原子落账",
        "blocked_by_execution_recovery": "上次执行中断；为防止重复成交，已隔离并等待账本复核",
        "blocked_by_contract_revision": "旧 V1 计划缺少冻结整手数量和完整成本合同，保留原始证据但禁止执行",
        "blocked_by_contract_validation": "正式决策合同校验失败，已保留候选证据但禁止执行",
        "missed_execution_window": "已错过冻结的 T+1 执行日；仅保留审计证据，禁止跨日补成交",
    }.get(row_status, "" if execution_authorized else blocked_reason)
    return {
        "id": int(row["id"]),
        "trade_date": trade_date,
        "data_as_of_date": trade_date,
        "planned_execute_date": str(row.get("planned_execute_date") or planned_execute_date),
        "planned_execute_at": str(row.get("planned_execute_at") or planned_execute_at),
        "valid_until_date": str(row.get("planned_execute_date") or planned_execute_date),
        "version": int(row["version"]),
        "plan_version": f"{trade_date}-v{int(row['version'])}",
        "status": row_status,
        "persisted": True,
        "is_executed": str(row.get("status") or "") == "executed",
        "execution_authorized": execution_authorized and row_status in {"planned", "executed"},
        "blocked_reason": row_blocked_reason,
        "execution_id": row.get("execution_id"),
        "generated_at": row.get("generated_at"),
        "executed_at": row.get("executed_at"),
        "input_hash": input_hash,
        "decision_version_manifest": decision_version_manifest,
        "release_id": row.get("release_id"),
        "selection_snapshot": contract.get("selection_snapshot") or {},
        "decision_contract_schema_version": contract.get("schema_version"),
        "generation_policy": (
            "受控合同重建：保留旧阻断版本作为审计证据，创建新的同信号日版本"
            if controlled_rebuild
            else "仅在最新交易日决策数据门通过后生成可执行版本；未通过时保留不可执行预览"
        ),
        "execution_policy": (
            f"执行日{effective_rebalance_clock().strftime('%H:%M')}后随发布探针持续检查；"
            "收盘数据与冻结执行切片完整时立即自动执行，每个交易日最多一次"
        ),
    }


def claim_daily_rebalance_execution_input(
    signal_date: str,
    execute_date: str,
    *,
    claim_token: str,
    expected_strategy_id: int | None = None,
) -> dict[str, Any] | None:
    """Return the latest immutable dashboard plan when it is due for execution.

    Historical replay deliberately gets no override: only a still-planned row
    whose signal and execution dates exactly match may drive live simulation.
    """
    from app.services.quant_run_contracts import ReplayRiskGateError

    _ensure_tables()
    signal = str(signal_date or "").replace("-", "")[:8]
    execute = str(execute_date or "").replace("-", "")[:8]
    token = str(claim_token or "").strip()
    if not token:
        raise ValueError("execution claim token is required")
    claimed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    order_audit_error: ReplayRiskGateError | None = None
    with business_conn() as c:
        query = """SELECT * FROM daily_rebalance_plans
                   WHERE trade_date=?
                     AND planned_execute_date=?
                   ORDER BY version DESC LIMIT 1"""
        if getattr(c, "_is_postgres", False) or getattr(c, "_is_mysql", False):
            query += " FOR UPDATE"
        row = c.execute(
            query,
            (signal, execute),
        ).fetchone()
        if not row:
            return None
        record = dict(row)
        if str(record.get("status") or "") != "planned":
            return None

        try:
            if not _formal_plan_is_due(record):
                # A future frozen plan is normal scheduler state, not a
                # contract failure.  Keep it unclaimed and let the scheduler
                # retain its normal waiting state rather than treating this
                # like an already-claimed plan.
                raise ReplayRiskGateError(
                    "正式调仓计划尚未到达冻结执行时刻",
                    code="formal_plan_not_due",
                )
        except ValueError as exc:
            c.execute(
                """UPDATE daily_rebalance_plans
                   SET status='blocked_by_contract_validation',
                       validation_error_code='invalid_planned_execute_at',
                       validation_errors_json=?,validated_at=?
                   WHERE id=? AND status='planned'""",
                (json.dumps([str(exc)], ensure_ascii=False), _now(), int(row["id"])),
            )
            raise ReplayRiskGateError(str(exc), code="invalid_planned_execute_at") from exc

        def reject_contract(message: str, code: str) -> None:
            raise ReplayRiskGateError(str(message), code=code or "invalid_authoritative_plan")

        try:
            contract = json.loads(record.get("proposal_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            reject_contract("正式调仓计划 JSON 合同无法解析", "invalid_authoritative_plan")
        schema_version = str(contract.get("schema_version") or "")
        if schema_version not in {
            UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
            PREVIOUS_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        }:
            # Autonomous execution may consume only the current frozen
            # contract. Preserve older evidence, but quarantine it before any
            # execution claim can be written.
            c.execute(
                """UPDATE daily_rebalance_plans
                   SET status='blocked_by_contract_revision',
                       validation_error_code='legacy_authoritative_plan',
                       validation_errors_json=?,validated_at=?
                   WHERE id=? AND status='planned'""",
                (json.dumps(["旧正式调仓计划合同已隔离，禁止执行"], ensure_ascii=False), _now(), int(row["id"])),
            )
            raise ReplayRiskGateError(
                "旧正式调仓计划合同已隔离，禁止执行",
                code="legacy_authoritative_plan",
            )
        canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        actual_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual_hash != str(record.get("input_hash") or ""):
            reject_contract(
                "正式调仓计划完整合同哈希校验失败",
                "invalid_authoritative_plan_hash",
            )
        if str(contract.get("schema_version") or "") in {
            UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
            PREVIOUS_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
            LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        }:
            from app.services.quant_run_plan_input import validate_unified_decision_contract

            try:
                validate_unified_decision_contract(contract)
            except ReplayRiskGateError as exc:
                reject_contract(str(exc), str(getattr(exc, "code", "") or "invalid_authoritative_plan"))
            from app.services.system_release_center import validate_frozen_release_for_execution
            frozen_release = (contract.get("decision_version_manifest") or {}).get("release") or {}
            release_gate = validate_frozen_release_for_execution(
                int(record.get("release_id") or frozen_release.get("id") or 0),
                str(frozen_release.get("manifest_hash") or ""),
            )
            if not release_gate.get("ok"):
                reject_contract(
                    f"冻结系统发布与当前执行器不兼容：{release_gate.get('reason')}",
                    "incompatible_frozen_system_release",
                )
            if (
                str(contract.get("signal_date") or "").replace("-", "")[:8] != signal
                or str(contract.get("planned_execute_date") or "").replace("-", "")[:8] != execute
            ):
                reject_contract(
                    "统一正式计划日期与数据库领取条件不一致",
                    "invalid_authoritative_plan",
                )
            frozen_strategy_id = int(
                ((contract.get("selection_snapshot") or {}).get("strategy_id")) or 0
            )
            if (
                expected_strategy_id is not None
                and frozen_strategy_id != int(expected_strategy_id)
            ):
                reject_contract(
                    "统一正式计划与执行任务的策略身份不一致",
                    "invalid_authoritative_plan",
                )
        proposal = contract.get("proposal") if isinstance(contract, dict) else None
        if not isinstance(proposal, list):
            reject_contract("正式调仓计划缺少冻结提案", "invalid_authoritative_plan")
        order_audit = audit_frozen_execution_orders(contract, execute)
        _persist_frozen_order_audit(plan_id=int(row["id"]), audit=order_audit, conn=c)
        if not order_audit.get("ok"):
            order_audit_error = frozen_order_data_gate_error(order_audit)
        if order_audit_error is None:
            changed = c.execute(
                """UPDATE daily_rebalance_plans
                   SET status='executing',execution_claim_token=?,execution_claimed_at=?
                   WHERE id=? AND status='planned'""",
                (token, claimed_at, int(row["id"])),
            )
            if int(getattr(changed, "rowcount", 1) or 0) != 1:
                return None
    if order_audit_error is not None:
        raise order_audit_error
    if str(contract.get("schema_version") or "") in {
        UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        PREVIOUS_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
    }:
        target_weights = dict(contract.get("target_weights") or {})
        target_symbols = list(contract.get("target_symbols") or [])
    else:
        target_weights = {
            str(item.get("symbol") or "").strip(): max(0.0, _safe_float(item.get("target_weight")))
            for item in proposal if str(item.get("symbol") or "").strip()
        }
        target_symbols = [symbol for symbol, weight in target_weights.items() if weight > 0.002]
    return {
        "plan_id": int(record["id"]),
        "plan_version": f"{record['trade_date']}-v{int(record['version'])}",
        "input_hash": str(record.get("input_hash") or ""),
        "signal_date": signal,
        "execute_date": execute,
        "target_symbols": target_symbols,
        "target_weights": target_weights,
        "proposal": proposal,
        "risk_budget": contract.get("risk_budget") or {},
        "selection_snapshot": contract.get("selection_snapshot") or {},
        "decision_version_manifest": contract.get("decision_version_manifest") or {},
        "release_id": record.get("release_id"),
        "risk_limit_contract": contract.get("risk_limit_contract") or {},
        "validation_error_code": str(record.get("validation_error_code") or ""),
        "validation_errors": json.loads(record.get("validation_errors_json") or "[]") if record.get("validation_errors_json") else [],
        "decision_contract": contract,
        "decision_contract_schema_version": str(contract.get("schema_version") or ""),
        "execution_claim_token": token,
        # The claim-time audit is the execution clock. The engine reuses this
        # cutoff so a later source revision cannot change the prices that were
        # just approved for this immutable plan.
        "execution_observation_cutoff": str(order_audit.get("checked_at") or ""),
        "execution_data_evidence": order_audit,
    }


def block_daily_rebalance_plan_contract(
    signal_date: str,
    execute_date: str,
    *,
    code: str,
    message: str,
) -> bool:
    """Persist a claim-time rejection after the failed claim transaction rolls back."""
    signal = str(signal_date or "").replace("-", "")[:8]
    execute = str(execute_date or "").replace("-", "")[:8]
    with business_conn() as c:
        changed = c.execute(
            """UPDATE daily_rebalance_plans
               SET status='blocked_by_contract_validation',
                   validation_error_code=?,validation_errors_json=?,validated_at=?
               WHERE id=(
                   SELECT id FROM daily_rebalance_plans
                   WHERE trade_date=? AND planned_execute_date=? AND status='planned'
                   ORDER BY version DESC LIMIT 1
               ) AND status='planned'""",
            (
                str(code or "invalid_authoritative_plan")[:128],
                json.dumps([str(message)[:500]], ensure_ascii=False),
                _now(),
                signal,
                execute,
            ),
        )
        return int(getattr(changed, "rowcount", 0) or 0) == 1


def list_pending_daily_rebalance_execution_inputs() -> list[dict[str, Any]]:
    """List the latest unclaimed formal plan for each signal date.

    This is intentionally read-only.  The simulator still acquires the plan
    through ``claim_daily_rebalance_execution_input`` immediately before it
    writes any ledger state.
    """
    _ensure_tables()
    conn_factory = (
        business_conn
        if getattr(business_conn, "__module__", "") != "app.db.store"
        else business_read_conn
    )
    with conn_factory() as c:
        metadata_rows = c.execute(
            """SELECT id,trade_date,planned_execute_date,version,status
               FROM daily_rebalance_plans
               WHERE status IN ('planned','blocked_by_contract_validation','blocked_by_contract_revision','blocked_by_execution_recovery','missed_execution_window')
               ORDER BY trade_date ASC, version DESC"""
        ).fetchall()
        latest_by_signal: dict[str, dict[str, Any]] = {}
        for row in metadata_rows:
            item = dict(row)
            signal_date = str(item.get("trade_date") or "").replace("-", "")[:8]
            if signal_date and signal_date not in latest_by_signal:
                latest_by_signal[signal_date] = item
        planned_rows = [
            item for item in latest_by_signal.values()
            if str(item.get("status") or "") == "planned"
            and len(str(item.get("trade_date") or "").replace("-", "")[:8]) == 8
            and len(str(item.get("planned_execute_date") or "").replace("-", "")[:8]) == 8
        ]
        if not planned_rows:
            return []
        planned_ids = [int(item.get("id") or 0) for item in planned_rows if int(item.get("id") or 0) > 0]
        if not planned_ids:
            return []
        placeholders = ",".join("?" for _ in planned_ids)
        legacy_contracts: dict[int, dict[str, Any]] = {}
        try:
            projection_rows = c.execute(
                    f"""SELECT p.plan_id AS id,p.schema_version,p.target_symbols_json,p.target_weights_json,
                         p.decision_manifest_json,p.risk_budget_json,p.selection_snapshot_json,
                         p.execution_authorized,o.order_index,o.order_json
                    FROM daily_rebalance_plan_projection p
                    LEFT JOIN daily_rebalance_plan_orders o ON o.plan_id=p.plan_id
                   WHERE p.plan_id IN ({placeholders})
                   ORDER BY p.plan_id,o.order_index""",
                    tuple(planned_ids),
                ).fetchall()
        except Exception:
            # Isolated legacy fixtures may not have the projection tables.
            # Production PostgreSQL creates them in _ensure_tables_once and
            # therefore never takes this compatibility path.
            # A failed projection query aborts a PostgreSQL transaction; reset
            # it before attempting the legacy fallback on the same connection.
            try:
                c.rollback()
            except Exception:
                pass
            projection_rows = []
            legacy_rows = c.execute(
                f"SELECT id,proposal_json FROM daily_rebalance_plans WHERE id IN ({placeholders})",
                tuple(planned_ids),
            ).fetchall()
            for legacy in legacy_rows:
                item = dict(legacy)
                try:
                    contract = json.loads(item.get("proposal_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    contract = {}
                legacy_contracts[int(item.get("id") or 0)] = contract
        if projection_rows and "proposal_json" in dict(projection_rows[0]) and "schema_version" not in dict(projection_rows[0]):
            legacy_contracts = {}
            for legacy in projection_rows:
                item = dict(legacy)
                try:
                    contract = json.loads(item.get("proposal_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    contract = {}
                legacy_contracts[int(item.get("id") or 0)] = contract
            projection_rows = []
    contracts_by_id: dict[int, dict[str, Any]] = {}
    contracts_by_id.update(legacy_contracts)
    for row in projection_rows:
        item = dict(row)
        plan_id = int(item.get("id") or 0)
        current = contracts_by_id.setdefault(plan_id, {
            "schema_version": str(item.get("schema_version") or ""),
            "execution_authorized": bool(item.get("execution_authorized")),
            "target_symbols": json.loads(item.get("target_symbols_json") or "[]"),
            "target_weights": json.loads(item.get("target_weights_json") or "{}"),
            "decision_version_manifest": json.loads(item.get("decision_manifest_json") or "{}"),
            "risk_budget": json.loads(item.get("risk_budget_json") or "{}"),
            "selection_snapshot": json.loads(item.get("selection_snapshot_json") or "{}"),
            "proposal": [],
            "execution_orders": [],
        })
        if item.get("order_json"):
            try:
                order = json.loads(item.get("order_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                order = {}
            if isinstance(order, dict):
                current["execution_orders"].append(order)
    # End the metadata read transaction before loading positions and decoding
    # the remaining execution contract. A scheduler read must not keep the
    # projection SELECT open while doing Python-side work.
    try:
        c.commit()
    except Exception:
        pass
    from app.services.defensive_assets import DEFENSIVE_ASSETS

    registered = {
        str(asset["ts_code"]).split(".")[0]: str(asset["ts_code"])
        for asset in DEFENSIVE_ASSETS
    }
    held_defensive_symbols = {
        str(position.get("symbol") or "").split(".")[0]
        for position in get_positions()
        if str(position.get("symbol") or "").split(".")[0] in registered
        and int(position.get("quantity") or 0) > 0
    }
    pending_inputs: list[dict[str, Any]] = []
    for item in planned_rows:
        signal_date = str(item.get("trade_date") or "").replace("-", "")[:8]
        execute_date = str(item.get("planned_execute_date") or "").replace("-", "")[:8]
        try:
            contract = contracts_by_id.get(int(item.get("id") or 0), {})
        except (TypeError, json.JSONDecodeError):
            contract = {}
        plan_symbols = {
            str(value or "").split(".")[0]
            for value in contract.get("target_symbols") or []
            if str(value or "").split(".")[0] in registered
        }
        proposal_symbols = {
            str(proposal.get("symbol") or "").split(".")[0]
            for proposal in contract.get("proposal") or []
            if isinstance(proposal, dict)
            and str(proposal.get("symbol") or "").split(".")[0] in registered
        }
        required_symbols = held_defensive_symbols | plan_symbols | proposal_symbols
        pending_item = {
            "plan_id": int(item.get("id") or 0),
            "signal_date": signal_date,
            "execute_date": execute_date,
            "required_defensive_ts_codes": sorted(registered[symbol] for symbol in required_symbols),
        }
        execution_orders = [
            order for order in (contract.get("execution_orders") or [])
            if isinstance(order, dict)
        ]
        if execution_orders:
            # Keep only the exact order slice for the execution-data probe;
            # broad research evidence remains private to the frozen contract.
            pending_item["execution_contract"] = {
                "schema_version": str(contract.get("schema_version") or ""),
                "execution_orders": execution_orders,
            }
        pending_inputs.append(pending_item)
    return pending_inputs


def count_pending_daily_rebalance_execution_inputs() -> int:
    """Count pending plans without loading their frozen contracts.

    The management readiness summary only needs the count.  Keep contract and
    defensive-asset expansion in the execution-facing reader above so a
    dashboard read cannot pull large JSON blobs or positions just to render a
    badge.
    """
    _ensure_tables()
    with business_conn() as c:
        rows = c.execute(
            """SELECT trade_date,planned_execute_date,version,status
               FROM daily_rebalance_plans
               WHERE status IN ('planned','blocked_by_contract_validation',
                                'blocked_by_contract_revision',
                                'blocked_by_execution_recovery',
                                'missed_execution_window')
               ORDER BY trade_date ASC,version DESC"""
        ).fetchall()
    latest_by_signal: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        signal_date = str(item.get("trade_date") or "").replace("-", "")[:8]
        if signal_date in latest_by_signal:
            continue
        latest_by_signal[signal_date] = item
    return sum(
        1
        for item in latest_by_signal.values()
        if str(item.get("status") or "") == "planned"
        and len(str(item.get("trade_date") or "").replace("-", "")[:8]) == 8
        and len(str(item.get("planned_execute_date") or "").replace("-", "")[:8]) == 8
    )


def mark_missed_daily_rebalance_execution_inputs(current_execute_date: str) -> int:
    """Close plans whose exact frozen execution session has already passed.

    A missed T+1 plan is audit evidence, not a catch-up order. Keeping it in
    ``planned`` would falsely advertise an executable action and lets stale
    rows accumulate indefinitely.
    """
    _ensure_tables()
    current = str(current_execute_date or "").replace("-", "")[:8]
    if len(current) != 8 or not current.isdigit():
        return 0
    with business_conn() as c:
        changed = c.execute(
            """UPDATE daily_rebalance_plans
               SET status='missed_execution_window',
                   execution_claim_token='',execution_claimed_at=NULL
               WHERE status='planned'
                 AND COALESCE(planned_execute_date,'')<>''
                 AND replace(planned_execute_date,'-','')<?""",
            (current,),
        )
    return int(getattr(changed, "rowcount", 0) or 0)


def release_daily_rebalance_execution_claim(signal_date: str, *, claim_token: str, reason: Any) -> bool:
    signal = str(signal_date or "").replace("-", "")[:8]
    detail = str(reason or "execution_data_not_ready")[:500]
    evidence = getattr(reason, "execution_data_evidence", None)
    if not isinstance(evidence, dict):
        evidence = {
            "schema_version": FROZEN_ORDER_DATA_GATE_SCHEMA_VERSION,
            "status": "retryable_execution_data_gap",
            "reason": detail,
            "checked_at": _now(),
        }
    next_probe = evidence.get("next_probe_at")
    with business_conn() as c:
        changed = c.execute(
            """UPDATE daily_rebalance_plans
               SET status='planned',execution_claim_token='',execution_claimed_at=NULL,
                   execution_data_evidence_json=?,next_probe_at=?,
                   validation_error_code='paused_frozen_execution_order_data_not_ready',
                   validation_errors_json=?,validated_at=?
               WHERE trade_date=? AND status='executing' AND execution_claim_token=?""",
            (
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                next_probe,
                json.dumps([detail], ensure_ascii=False),
                _now(),
                signal,
                str(claim_token or ""),
            ),
        )
    return int(getattr(changed, "rowcount", 0) or 0) == 1


def quarantine_daily_rebalance_execution_claim(
    signal_date: str, *, claim_token: str, reason: str,
) -> bool:
    """Isolate a claimed plan after an execution failure without releasing it for replay."""
    signal = str(signal_date or "").replace("-", "")[:8]
    detail = str(reason or "execution_failed")[:500]
    now = _now()
    with business_conn() as c:
        changed = c.execute(
            """UPDATE daily_rebalance_plans
               SET status='blocked_by_execution_recovery',
                   validation_error_code='execution_failed_recovery',
                   validation_errors_json=?,validated_at=?
               WHERE replace(trade_date,'-','')=?
                 AND status='executing' AND execution_claim_token=?""",
            (
                json.dumps([f"执行失败，计划已隔离：{detail}"], ensure_ascii=False),
                now,
                signal,
                str(claim_token or ""),
            ),
        )
    return int(getattr(changed, "rowcount", 0) or 0) == 1


def recover_interrupted_daily_rebalance_plans() -> int:
    """Quarantine interrupted executions and obsolete pending V1 contracts."""
    _ensure_tables()
    with business_conn() as c:
        changed = c.execute(
            """UPDATE daily_rebalance_plans
               SET status='blocked_by_execution_recovery'
               WHERE status='executing'"""
        )
        interrupted = int(getattr(changed, "rowcount", 0) or 0)
        legacy_rows = c.execute(
            "SELECT id,proposal_json FROM daily_rebalance_plans WHERE status='planned'"
        ).fetchall()
        legacy_ids: list[int] = []
        for row in legacy_rows:
            item = dict(row)
            try:
                contract = json.loads(item.get("proposal_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if str(contract.get("schema_version") or "") == LEGACY_UNIFIED_DAILY_DECISION_SCHEMA_VERSION:
                legacy_ids.append(int(item["id"]))
        for plan_id in legacy_ids:
            c.execute(
                """UPDATE daily_rebalance_plans
                   SET status='blocked_by_contract_revision',
                       execution_claim_token='',execution_claimed_at=NULL
                   WHERE id=? AND status='planned'""",
                (plan_id,),
            )
    return interrupted + len(legacy_ids)


def _compact_plan_proposal_item(item: Any) -> dict[str, Any]:
    """Project an order for the console without returning nested frozen evidence.

    The immutable JSON contract remains the execution and audit source of
    truth.  Console pages only need the order fields listed here; sending the
    complete selection snapshot once per refresh made a small decision card
    transfer hundreds of kilobytes of unrelated research evidence.
    """
    if not isinstance(item, dict):
        return {}
    fields = (
        "name", "symbol_name", "symbol", "ts_code", "reason", "action_reason", "thesis",
        "controller_action", "controller_intent", "target_weight", "weight", "current_weight",
        "planned_quantity", "planned_lots", "planned_side", "reference_close",
        "estimated_execution_amount", "planned_cost", "controller_obligation",
    )
    return {field: item[field] for field in fields if field in item}


def _compact_frozen_plan_contract(contract: dict[str, Any]) -> dict[str, Any]:
    proposal = contract.get("proposal")
    execution_orders = contract.get("execution_orders")
    controller = contract.get("pretrade_controller") or {}
    expected = contract.get("expected_return_contract") or {}
    risk_budget = contract.get("risk_budget") or {}
    risk_limits = contract.get("risk_limit_contract") or {}
    compact_proposal = [_compact_plan_proposal_item(item) for item in proposal] if isinstance(proposal, list) else []
    if not compact_proposal and isinstance(execution_orders, list):
        # V2 contracts keep executable orders in the authoritative frozen
        # execution_orders list. Project its display fields for the console.
        compact_proposal = []
        for order in execution_orders:
            if not isinstance(order, dict):
                continue
            side = str(order.get("side") or "").upper()
            intent = str(order.get("intent") or "")
            compact_proposal.append({
                "symbol": str(order.get("symbol") or ""),
                "controller_action": intent or ("buy_candidate" if side == "BUY" else "sell_exit"),
                "controller_intent": intent,
                "target_weight": order.get("post_lot_target_weight"),
                "planned_quantity": order.get("planned_quantity"),
                "planned_lots": order.get("planned_lots"),
                "planned_side": side,
                "reference_close": order.get("reference_close"),
                "estimated_execution_amount": order.get("estimated_execution_amount"),
                "planned_cost": order.get("cost") or {},
                "controller_obligation": order.get("obligation") or {},
                "reason": "冻结执行订单",
            })
    return {
        "proposal": compact_proposal,
        "risk_budget": {
            field: risk_budget[field]
            for field in ("status", "equity_budget", "cash_target", "opportunity_status", "tiered_equity_budget")
            if isinstance(risk_budget, dict) and field in risk_budget
        },
        "research_tier_contract": contract.get("research_tier_contract") or {},
        "tiered_equity_budget": contract.get("tiered_equity_budget") or {},
        "decision_data_gate": contract.get("decision_data_gate") or {},
        "pretrade_controller": {
            "actions": [
                {
                    field: action[field]
                    for field in ("trigger_models", "risk_budget", "holding_period", "exit_conditions", "forbidden_conditions")
                    if field in action
                }
                for action in (controller.get("actions") or [])
                if isinstance(action, dict)
            ],
            "schema_version": controller.get("schema_version") if isinstance(controller, dict) else "",
        },
        "expected_return_contract": {
            field: expected[field]
            for field in ("schema_version", "horizon_sessions")
            if isinstance(expected, dict) and field in expected
        },
        "decision_contract_schema_version": str(contract.get("schema_version") or ""),
        "risk_limit_contract": {
            field: risk_limits[field]
            for field in ("schema_version", "global_single_asset_limit", "contract_hash")
            if isinstance(risk_limits, dict) and field in risk_limits
        },
        "decision_version_manifest": {
            field: (contract.get("decision_version_manifest") or {}).get(field)
            for field in ("schema_version", "manifest_hash", "trade_date")
            if field in (contract.get("decision_version_manifest") or {})
        },
    }


def get_latest_daily_rebalance_plan(trade_date: str = "", *, compact: bool = False) -> dict[str, Any]:
    """Read the latest persisted plan without generating a new version."""
    _ensure_tables()
    base_columns = (
        "id,trade_date,planned_execute_date,planned_execute_at,version,status,{proposal_column},"
        "execution_id,generated_at,executed_at,execution_claim_token,execution_claimed_at,"
        "validation_error_code,validation_errors_json,validated_at,"
        "execution_data_evidence_json,next_probe_at"
    )
    today = datetime.now(_BUSINESS_TZ).strftime("%Y%m%d")
    with business_conn() as c:
        proposal_column = "proposal_json"
        if compact and bool(getattr(c, "_is_postgres", False)):
            proposal_column = """jsonb_build_object(
                'proposal',COALESCE(proposal_json::jsonb->'proposal','[]'::jsonb),
                'execution_orders',COALESCE(proposal_json::jsonb->'execution_orders','[]'::jsonb),
                'risk_budget',COALESCE(proposal_json::jsonb->'risk_budget','{}'::jsonb),
                'research_tier_contract',COALESCE(proposal_json::jsonb->'research_tier_contract','{}'::jsonb),
                'tiered_equity_budget',COALESCE(proposal_json::jsonb->'tiered_equity_budget','{}'::jsonb),
                'decision_data_gate',COALESCE(proposal_json::jsonb->'decision_data_gate','{}'::jsonb),
                'pretrade_controller',COALESCE(proposal_json::jsonb->'pretrade_controller','{}'::jsonb),
                'expected_return_contract',COALESCE(proposal_json::jsonb->'expected_return_contract','{}'::jsonb),
                'schema_version',COALESCE(proposal_json::jsonb->'schema_version','null'::jsonb),
                'risk_limit_contract',COALESCE(proposal_json::jsonb->'risk_limit_contract','{}'::jsonb),
                'decision_version_manifest',COALESCE(proposal_json::jsonb->'decision_version_manifest','{}'::jsonb),
                'execution_authorized',COALESCE(proposal_json::jsonb->'execution_authorized','false'::jsonb)
            )::text AS proposal_json"""
        columns = base_columns.format(proposal_column=proposal_column)
        row = c.execute(
            f"""SELECT {columns}
               FROM daily_rebalance_plans
               WHERE (?='' OR trade_date=?)
               ORDER BY CASE WHEN status='planned'
                                  AND replace(planned_execute_date,'-','')<=? THEN 0 ELSE 1 END,
                        trade_date DESC,version DESC LIMIT 1""",
            (trade_date, trade_date, today),
        ).fetchone()
    if not row:
        return {"status": "not_generated", "execution_authorized": False}
    item = dict(row)
    try:
        contract = json.loads(item.get("proposal_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        contract = {}
    projection = _compact_frozen_plan_contract(contract) if compact else {
        "proposal": contract.get("proposal") or [],
        "risk_budget": contract.get("risk_budget") or {},
        "research_tier_contract": contract.get("research_tier_contract") or {},
        "tiered_equity_budget": contract.get("tiered_equity_budget") or {},
        "decision_data_gate": contract.get("decision_data_gate") or {},
        "selection_snapshot": contract.get("selection_snapshot") or {},
        "pretrade_controller": contract.get("pretrade_controller") or {},
        "expected_return_contract": contract.get("expected_return_contract") or {},
        "decision_contract_schema_version": str(contract.get("schema_version") or ""),
        "risk_limit_contract": contract.get("risk_limit_contract") or {},
        "decision_version_manifest": contract.get("decision_version_manifest") or {},
    }
    return {
        "id": int(item["id"]), "trade_date": str(item.get("trade_date") or ""),
        "data_as_of_date": str(item.get("trade_date") or ""),
        "planned_execute_date": str(item.get("planned_execute_date") or ""),
        "planned_execute_at": str(item.get("planned_execute_at") or ""),
        "valid_until_date": str(item.get("planned_execute_date") or ""),
        "version": int(item.get("version") or 0),
        "plan_version": f"{item.get('trade_date')}-v{int(item.get('version') or 0)}",
        "status": str(item.get("status") or ""),
        "persisted": True,
        "is_executed": str(item.get("status") or "") == "executed",
        "execution_authorized": (
            str(item.get("status") or "") in {"planned", "executed"}
            and bool(contract.get("execution_authorized"))
        ),
        "execution_id": item.get("execution_id"),
        "execution_claim_token_present": bool(str(item.get("execution_claim_token") or "")),
        "execution_claimed_at": item.get("execution_claimed_at"),
        "blocked_reason": {
            "executing": "正式计划已被唯一执行器领取，正在原子落账",
            "blocked_by_system_release": "当前运行组件与已激活系统发布不一致，禁止执行",
            "blocked_by_decision_data": "最新交易日决策数据门未通过",
            "blocked_by_position_valuation": "持仓缺少冻结点时行情，仅有成本价估值；禁止执行",
            "blocked_by_selection": "统一全市场选股链路未完成",
            "blocked_by_ledger": "模拟账户账本不一致",
            "blocked_by_risk": "风险门拒绝会增加风险的方案",
            "blocked_by_optimizer": "组合优化没有形成安全可执行解",
            "blocked_by_execution_checks": "执行前置检查未全部通过",
            "blocked_by_execution_recovery": "上次执行在成交与任务确认之间中断；为防止重复成交，已隔离并等待账本复核",
            "blocked_by_contract_revision": "旧 V1 计划缺少冻结整手数量和完整成本合同，保留原始证据但禁止执行",
            "blocked_by_contract_validation": "正式决策合同校验失败，已保留候选证据但禁止执行",
            "missed_execution_window": "已错过冻结的 T+1 执行日；仅保留审计证据，禁止跨日补成交",
        }.get(str(item.get("status") or ""), ""),
        "proposal": projection["proposal"],
        "risk_budget": projection["risk_budget"],
        "decision_data_gate": projection["decision_data_gate"],
        "selection_snapshot": projection.get("selection_snapshot") or {},
        # These are part of the already-frozen T-day contract.  Expose them
        # for read-only consoles rather than reconstructing decision rationale
        # from a later live controller run.
        "pretrade_controller": projection["pretrade_controller"],
        "expected_return_contract": projection["expected_return_contract"],
        "decision_contract_schema_version": projection["decision_contract_schema_version"],
        "risk_limit_contract": projection["risk_limit_contract"],
        "validation_error_code": str(item.get("validation_error_code") or ""),
        "validation_errors": json.loads(item.get("validation_errors_json") or "[]") if item.get("validation_errors_json") else [],
        "execution_data_evidence": (
            json.loads(item.get("execution_data_evidence_json") or "{}")
            if item.get("execution_data_evidence_json") else {}
        ),
        "next_probe_at": item.get("next_probe_at"),
        "decision_version_manifest": projection["decision_version_manifest"],
        "generated_at": item.get("generated_at"), "executed_at": item.get("executed_at"),
        "generation_policy": "计划只由自治周期生成；本接口只读已有版本",
        "execution_policy": "到计划执行时间后由模拟执行调度器检查，每个交易日最多一次",
        "read_only": True,
    }


def mark_daily_rebalance_plan_executed(
    signal_date: str, execute_date: str, execution_id: int | None, *, claim_token: str = "",
) -> None:
    """Bind execution to the immutable plan built from the prior close signal."""
    signal = str(signal_date or "").replace("-", "")[:8]
    executed_at = datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")
    with business_conn() as c:
        changed = c.execute("""UPDATE daily_rebalance_plans SET status='executed',execution_id=?,executed_at=?
            WHERE id=(SELECT id FROM daily_rebalance_plans
                      WHERE trade_date=? AND status='executing' AND execution_claim_token=?
                      ORDER BY version DESC LIMIT 1)""",
            (execution_id, executed_at, signal, str(claim_token or "")))
        if int(getattr(changed, "rowcount", 0) or 0) != 1:
            raise RuntimeError("正式调仓计划执行状态回写失败：执行租约不存在或已转移")


EXECUTION_SLO_WINDOW_PLANS = 20
EXECUTION_SLO_TARGETS = {
    "resolved_due_plan_rate": 0.95,
    "on_schedule_execution_rate": 0.95,
    "execution_binding_integrity_rate": 1.0,
}


def _plan_has_actionable_orders(plan: dict[str, Any]) -> bool:
    if "actionable" in plan:
        return bool(plan.get("actionable"))
    try:
        contract = json.loads(plan.get("proposal_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return True
    proposal = contract.get("proposal") if isinstance(contract, dict) else []
    return any(
        str(item.get("action") or "") not in {"", "hold", "observe"}
        and abs(_safe_float(item.get("weight_change"))) > 1e-9
        for item in proposal if isinstance(item, dict)
    )


def _plan_execution_authorized(plan: dict[str, Any]) -> bool:
    if "execution_authorized" in plan:
        return bool(plan.get("execution_authorized"))
    try:
        contract = json.loads(plan.get("proposal_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(contract.get("execution_authorized")) if isinstance(contract, dict) else False


def _frozen_execution_order_limits(plan: dict[str, Any]) -> dict[tuple[str, str], int] | None:
    """Return V2 frozen order quantities, or ``None`` for historical plans.

    A T+1 hard veto may legitimately leave a frozen order unfilled.  It may
    never create an order outside the frozen set or fill more shares than the
    frozen quantity, so those two properties remain auditable post-trade.
    """
    frozen_orders = plan.get("frozen_execution_orders")
    if frozen_orders is None:
        try:
            contract = json.loads(plan.get("proposal_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if str(contract.get("schema_version") or "") not in {
            UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
            PREVIOUS_UNIFIED_DAILY_DECISION_SCHEMA_VERSION,
        }:
            return None
        frozen_orders = contract.get("execution_orders")
    if isinstance(frozen_orders, str):
        try:
            frozen_orders = json.loads(frozen_orders)
        except json.JSONDecodeError:
            return {}
    if not isinstance(frozen_orders, list):
        return {}
    limits: dict[tuple[str, str], int] = {}
    for item in frozen_orders:
        if not isinstance(item, dict):
            return {}
        symbol = str(item.get("symbol") or "").strip().upper()
        side = str(item.get("side") or "").strip().upper()
        try:
            quantity_value = float(item.get("planned_quantity"))
            quantity = int(quantity_value)
        except (TypeError, ValueError):
            return {}
        if not math.isfinite(quantity_value) or quantity_value != quantity:
            return {}
        if not symbol or side not in {"BUY", "SELL"} or quantity <= 0:
            return {}
        key = (symbol, side)
        if key in limits:
            return {}
        limits[key] = quantity
    return limits


def _execution_operations_within_frozen_limits(
    plan: dict[str, Any], operations: list[dict[str, Any]], execution_id: int,
) -> bool:
    """Check that actual V2 operations cannot exceed or escape frozen orders."""
    limits = _frozen_execution_order_limits(plan)
    if limits is None:
        return True
    if not limits:
        return False
    actual: dict[tuple[str, str], int] = {}
    for operation in operations:
        if int(operation.get("execution_id") or 0) != execution_id:
            continue
        symbol = str(operation.get("symbol") or "").strip().upper()
        side = str(operation.get("side") or "").strip().upper()
        try:
            quantity = int(operation.get("quantity"))
        except (TypeError, ValueError):
            return False
        if not symbol or side not in {"BUY", "SELL"} or quantity <= 0:
            return False
        key = (symbol, side)
        actual[key] = actual.get(key, 0) + quantity
    return all(key in limits and quantity <= limits[key] for key, quantity in actual.items())


def build_execution_chain_slo(
    plans: list[dict[str, Any]] | None = None,
    operations: list[dict[str, Any]] | None = None,
    *,
    as_of_date: str | None = None,
    window_plans: int = EXECUTION_SLO_WINDOW_PLANS,
    compact: bool = False,
) -> dict[str, Any]:
    """Measure plan-to-execution integrity from immutable plan and ledger links."""
    _ensure_tables()
    cutoff = str(as_of_date or datetime.now(_BUSINESS_TZ).date().isoformat()).replace("-", "")[:8]
    if plans is None:
        with business_conn() as c:
            if compact and getattr(c, "_is_postgres", False):
                # PostgreSQL evaluates authorization and actionability in
                # place, so the dashboard does not transfer historical
                # contracts over the database connection.
                rows = c.execute(
                    """SELECT id,trade_date,planned_execute_date,version,status,
                              execution_id,executed_at,
                              COALESCE((proposal_json::jsonb ->> 'execution_authorized') = 'true', false)
                                AS execution_authorized,
                              proposal_json::jsonb -> 'execution_orders' AS frozen_execution_orders,
                              EXISTS (
                                  SELECT 1
                                    FROM jsonb_array_elements(
                                        COALESCE(proposal_json::jsonb -> 'proposal', '[]'::jsonb)
                                    ) AS proposal_item
                                   WHERE COALESCE(proposal_item ->> 'action', '') NOT IN ('', 'hold', 'observe')
                                     AND ABS(COALESCE(NULLIF(proposal_item ->> 'weight_change', '')::double precision, 0)) > 1e-9
                              ) AS actionable
                       FROM daily_rebalance_plans
                       WHERE status<>'superseded'
                       ORDER BY trade_date DESC,version DESC LIMIT 200"""
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT id,trade_date,planned_execute_date,version,status,proposal_json,execution_id,executed_at
                       FROM daily_rebalance_plans
                       WHERE status<>'superseded' ORDER BY trade_date DESC,version DESC LIMIT 200"""
        ).fetchall()
        plans = [dict(row) for row in rows]
    latest_by_signal: dict[str, dict[str, Any]] = {}
    for plan in plans:
        signal_date = str(plan.get("trade_date") or "").replace("-", "")[:8]
        execute_date = str(plan.get("planned_execute_date") or "").replace("-", "")[:8]
        if not signal_date or not execute_date or execute_date > cutoff:
            continue
        current = latest_by_signal.get(signal_date)
        if current is None or int(plan.get("version") or 0) > int(current.get("version") or 0):
            latest_by_signal[signal_date] = plan
    due = sorted(
        (item for item in latest_by_signal.values() if _plan_execution_authorized(item)),
        key=lambda item: str(item.get("planned_execute_date") or ""), reverse=True,
    )
    due = due[:max(1, int(window_plans))]
    selected_execution_ids = {int(item.get("execution_id") or 0) for item in due if int(item.get("execution_id") or 0) > 0}
    if operations is None:
        if selected_execution_ids:
            placeholders = ",".join("?" for _ in selected_execution_ids)
            with business_conn() as c:
                rows = c.execute(
                    f"SELECT execution_id,operation_uid,symbol,side,quantity FROM operations WHERE execution_id IN ({placeholders})",
                    tuple(sorted(selected_execution_ids)),
                ).fetchall()
            operations = [dict(row) for row in rows]
        else:
            operations = []
    scoped_operations = [item for item in operations if int(item.get("execution_id") or 0) in selected_execution_ids]
    operation_execution_ids = {int(item.get("execution_id") or 0) for item in scoped_operations}
    operation_uids = [str(item.get("operation_uid") or "") for item in scoped_operations if str(item.get("operation_uid") or "")]
    duplicate_operation_uid_count = len(operation_uids) - len(set(operation_uids))
    observations: list[dict[str, Any]] = []
    for plan in reversed(due):
        status = str(plan.get("status") or "")
        execution_id = int(plan.get("execution_id") or 0)
        actionable = _plan_has_actionable_orders(plan)
        executed_at = str(plan.get("executed_at") or "")
        executed_date = executed_at[:10].replace("-", "") if executed_at else ""
        execute_date = str(plan.get("planned_execute_date") or "").replace("-", "")[:8]
        resolved = status == "executed"
        binding_valid = bool(
            resolved
            and ((actionable and execution_id in operation_execution_ids) or (not actionable and execution_id == 0))
            and (not actionable or _execution_operations_within_frozen_limits(plan, scoped_operations, execution_id))
        )
        # A quarantined plan with no execution id or completion timestamp is a
        # terminal safety state: it cannot be replayed and has no transaction
        # to reconcile. Keep it in the historical SLO sample, but do not call
        # it an active stranded execution for the current readiness gate.
        isolated_recovery = bool(
            status == "blocked_by_execution_recovery"
            and execution_id <= 0
            and not executed_at
        )
        stranded_execution = bool(
            status == "executing"
            or (status == "blocked_by_execution_recovery" and not isolated_recovery)
        )
        observations.append({
            "plan_id": int(plan.get("id") or 0),
            "signal_date": str(plan.get("trade_date") or "").replace("-", "")[:8],
            "planned_execute_date": execute_date,
            "status": status,
            "actionable": actionable,
            "execution_id": execution_id or None,
            "resolved": resolved,
            "on_schedule": bool(resolved and executed_date == execute_date),
            "binding_valid": binding_valid,
            "isolated_recovery": isolated_recovery,
            "stranded_execution": stranded_execution,
        })
    count = len(observations)

    def rate(key: str) -> float | None:
        return round(sum(bool(item.get(key)) for item in observations) / count, 6) if count else None

    execution_ids = [int(item["execution_id"]) for item in observations if item.get("execution_id")]
    metrics = {
        "resolved_due_plan_rate": rate("resolved"),
        "on_schedule_execution_rate": rate("on_schedule"),
        "execution_binding_integrity_rate": rate("binding_valid"),
        "duplicate_plan_execution_id_count": len(execution_ids) - len(set(execution_ids)),
        "duplicate_operation_uid_count": duplicate_operation_uid_count,
        "stranded_execution_count": sum(bool(item["stranded_execution"]) for item in observations),
        "isolated_recovery_count": sum(bool(item["isolated_recovery"]) for item in observations),
        "executed_binding_anomaly_count": sum(
            1 for item in observations
            if item.get("status") == "executed" and not item.get("binding_valid")
        ),
        "frozen_order_integrity_anomaly_count": sum(
            1 for item in due
            if str(item.get("status") or "") == "executed"
            and int(item.get("execution_id") or 0) > 0
            and not _execution_operations_within_frozen_limits(
                item, scoped_operations, int(item.get("execution_id") or 0),
            )
        ),
    }
    enough_samples = count >= max(1, int(window_plans))
    gates = {
        key: bool(enough_samples and metrics[key] is not None and float(metrics[key]) >= target)
        for key, target in EXECUTION_SLO_TARGETS.items()
    }
    gates.update({
        "no_duplicate_plan_execution_ids": bool(enough_samples and metrics["duplicate_plan_execution_id_count"] == 0),
        "no_duplicate_operation_uids": bool(enough_samples and metrics["duplicate_operation_uid_count"] == 0),
        "no_stranded_executions": bool(enough_samples and metrics["stranded_execution_count"] == 0),
    })
    status = "observing" if not enough_samples else "passed" if all(gates.values()) else "failed"
    current_blockers: list[str] = []
    if metrics["stranded_execution_count"]:
        current_blockers.append("存在仍在执行或带执行记录的隔离计划")
    if metrics["duplicate_plan_execution_id_count"]:
        current_blockers.append("存在重复的计划执行 ID")
    if metrics["duplicate_operation_uid_count"]:
        current_blockers.append("存在重复的操作 UID")
    if metrics["executed_binding_anomaly_count"]:
        current_blockers.append("已完成计划缺少可验证的成交绑定")
    if metrics["frozen_order_integrity_anomaly_count"]:
        current_blockers.append("实际成交超出冻结订单的标的、方向或数量")
    return {
        "schema_version": "alis_execution_chain_rolling_slo_v3",
        "status": status,
        "window_plans": int(window_plans),
        "observed_due_plans": count,
        "targets": EXECUTION_SLO_TARGETS,
        "metrics": metrics,
        "gates": gates,
        "current_blocking": bool(current_blockers),
        "current_blockers": current_blockers,
        "observations": [] if compact else observations,
        "policy": "latest_execution_authorized_non_superseded_due_plan_per_signal_date; v2_actual_operations_must_not_escape_or_exceed_frozen_orders; isolated_recovery_without_execution_id_is_historical_only; current_blocking_requires_active_or_unreconciled_execution_or_duplicate_binding",
    }


def build_sell_rebalance_diagnostics(
    model_monitor: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    *, persist: bool = True,
) -> dict[str, Any]:
    positions = get_positions()
    decision_gate = (portfolio or {}).get("decision_data_gate") if isinstance(portfolio, dict) else {}
    trade_date = str((decision_gate or {}).get("target_trade_date") or "")
    if not trade_date:
        return {
            "ok": False,
            "status": "blocked_by_position_market_data",
            "retryable": True,
            "schema_version": "sell_rebalance_diagnostics_v2",
            "items": [],
            "learning_labels_written": 0,
            "missing_symbols": [],
            "detail": "缺少权威目标交易日，禁止从历史快照或成本价计算持仓盈亏。",
        }
    try:
        valuation_cache = build_position_market_series_cache(positions, as_of=trade_date, exact_date=True)
        missing = missing_exact_position_valuations(positions, valuation_cache, as_of=trade_date)
    except Exception as exc:  # noqa: BLE001 - return a retryable diagnostic data gap
        return {
            "ok": False,
            "status": "blocked_by_position_market_data",
            "retryable": True,
            "schema_version": "sell_rebalance_diagnostics_v2",
            "items": [],
            "learning_labels_written": 0,
            "missing_symbols": [],
            "detail": f"读取持仓目标日行情失败：{str(exc)[:300]}",
        }
    if missing:
        return {
            "ok": False,
            "status": "blocked_by_position_market_data",
            "retryable": True,
            "schema_version": "sell_rebalance_diagnostics_v2",
            "items": [],
            "learning_labels_written": 0,
            "missing_symbols": sorted(missing),
            "detail": "持仓缺少目标交易日精确行情，禁止用成本价或前收计算盈亏。",
        }
    model_degraded = bool((model_monitor or {}).get("degraded_count"))
    target_weights = ((portfolio or {}).get("optimizer") or {}).get("weights") or {}
    current_weights = (portfolio or {}).get("current_weights") or {}
    items = []
    learning_written = 0
    for pos in positions:
        symbol = str(pos.get("symbol") or "")
        cost = _safe_float(pos.get("price"))
        current = _safe_float((valuation_cache.get(symbol) or {}).get("price"))
        pnl_pct = current / cost - 1.0 if cost > 0 else 0.0
        current_weight = _safe_float(current_weights.get(symbol))
        target_weight = _safe_float(target_weights.get(symbol), current_weight)
        reasons = []
        if pnl_pct < -0.06:
            reasons.append({"code": "loss_exceeds_expected", "label": "亏损超预期", "detail": f"浮亏 {pnl_pct:.2%}，需要复核买入假设"})
        if target_weight + 0.01 < current_weight:
            reasons.append({"code": "risk_budget_shrink", "label": "风险预算收缩", "detail": f"目标权重 {target_weight:.2%} 低于当前 {current_weight:.2%}"})
        if model_degraded and target_weight + 0.01 < current_weight:
            reasons.append({"code": "model_decay", "label": "模型失效降权", "detail": "生产模型健康监控触发降权"})
        if pnl_pct > 0.25 and target_weight < current_weight:
            reasons.append({"code": "valuation_overextension", "label": "估值/涨幅透支", "detail": "已有较大盈利且组合目标降权"})
        action = "sell_or_reduce" if any(x["code"] in {"risk_budget_shrink", "model_decay", "loss_exceeds_expected"} for x in reasons) else "hold"
        if reasons and persist:
            with business_conn() as c:
                c.execute("""INSERT INTO simulation_trade_learning_labels
                    (symbol,label_date,label,reason,pnl_pct,expected_vs_actual_json,created_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (symbol, trade_date, str(reasons[0]["code"]), str(reasons[0]["detail"]), pnl_pct,
                     json.dumps({"cost": cost, "current": current, "target_weight": target_weight, "current_weight": current_weight}, ensure_ascii=False), _now()))
            learning_written += 1
        items.append({
            "symbol": symbol,
            "name": pos.get("name") or "",
            "pnl_pct": round(pnl_pct, 6),
            "current_weight": round(current_weight, 6),
            "target_weight": round(target_weight, 6),
            "action": action,
            "reasons": reasons or [{"code": "thesis_intact", "label": "继续持有", "detail": "未触发模型、风险或亏损退出条件"}],
        })
    return {
        "ok": True,
        "schema_version": "sell_rebalance_diagnostics_v2",
        "items": items,
        "learning_labels_written": learning_written,
        "missing_symbols": [],
    }


def build_simulation_learning_loop(*, persist: bool = True) -> dict[str, Any]:
    materialized = materialize_decision_outcomes(limit=2000) if persist else {
        "ok": True, "status": "read_only", "written": 0,
    }
    overview = review_learning_overview()
    with business_conn() as c:
        labels = [dict(row) for row in c.execute(
            "SELECT label,COUNT(*) count FROM simulation_trade_learning_labels GROUP BY label ORDER BY count DESC"
        ).fetchall()]
    return {"ok": True, "schema_version": "simulation_learning_loop_v1", "materialized": materialized, "overview": overview, "sell_labels": labels}


def build_profit_loop(*, persist: bool = True, signal_date: str = "") -> dict[str, Any]:
    attribution = build_return_attribution_loop(persist=persist)
    monitor = build_model_live_monitor(persist=persist)
    if persist and signal_date:
        # The formal plan owner builds the expensive portfolio under the same
        # cross-process lease as persistence.  This prevents the API scheduler
        # and control-worker autonomy cycle from doing the same 4k-row PIT
        # snapshot read concurrently.
        rebalance_plan, portfolio = _materialize_daily_rebalance_plan_with_portfolio(
            signal_date=signal_date,
        )
    else:
        portfolio = (
            build_portfolio_reoptimization(signal_date=signal_date)
            if signal_date
            else build_portfolio_reoptimization()
        )
        rebalance_plan = materialize_daily_rebalance_plan(portfolio) if persist else get_latest_daily_rebalance_plan()
    portfolio_candles = build_portfolio_candles()
    sells = build_sell_rebalance_diagnostics(monitor, portfolio, persist=persist)
    learning = build_simulation_learning_loop(persist=persist)
    model_contribution = build_model_return_contribution()
    portfolio = portfolio if isinstance(portfolio, dict) else {
        "ok": False,
        "status": "blocked",
        "execution_authorized": False,
        "blocked_reasons": [{"key": "portfolio", "detail": "组合重优化没有返回结构化结果"}],
    }
    rebalance_plan = rebalance_plan if isinstance(rebalance_plan, dict) else {
        "status": "blocked_by_plan_result",
        "execution_authorized": False,
        "blocked_reason": "正式计划没有返回结构化结果",
    }
    portfolio_blocked = str(portfolio.get("status") or "") == "blocked" or not bool(portfolio.get("execution_authorized"))
    plan_blocked = str(rebalance_plan.get("status") or "").startswith("blocked") or (
        rebalance_plan.get("execution_authorized") is False
        and str(rebalance_plan.get("status") or "") not in {"executed", "superseded"}
    )
    blocked_reasons = list(portfolio.get("blocked_reasons") or [])
    sells_blocked = not bool(sells.get("ok"))
    if sells_blocked:
        blocked_reasons.append({
            "key": "sell_rebalance_diagnostics",
            "detail": str(sells.get("detail") or sells.get("status") or "持仓卖出诊断数据不可用"),
        })
    if plan_blocked:
        blocked_reasons.append({
            "key": "rebalance_plan",
            "detail": str(rebalance_plan.get("blocked_reason") or rebalance_plan.get("status") or "正式计划不可执行"),
        })
    return {
        "ok": not (portfolio_blocked or plan_blocked or sells_blocked),
        "status": "blocked" if (portfolio_blocked or plan_blocked or sells_blocked) else "ready",
        "blocked_reasons": blocked_reasons,
        "schema_version": "profit_loop_v1",
        "generated_at": _now(),
        "attribution": attribution,
        "model_monitor": monitor,
        "model_return_contribution": model_contribution,
        "portfolio_optimization": portfolio,
        "portfolio_candles": portfolio_candles,
        "rebalance_plan": rebalance_plan,
        "sell_rebalance": sells,
        "learning": learning,
        "disclaimer": "仅用于研究和模拟交易，不连接券商、不自动实盘下单。",
    }
