"""One-shot historical holdout isolated from strategy research and diagnosis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from time import monotonic
from typing import Any, Callable

from app.data_platform.db import _conn as _data_conn
from app.data_platform.services.oos_hard_data import (
    BENCHMARK_API_KEY,
    HARD_APIS,
    MINIMUM_DAILY_ROWS,
    MINIMUM_HARD_API_ROWS,
    MINIMUM_MARKET_FIELD_RATIO,
    _date_api_sets,
    _benchmark_date_set,
    repair_oos_hard_data_batch,
)
from app.data_platform.services.trade_calendar_contract import (
    audit_trade_calendar_range,
    repair_trade_calendar_range,
)
from app.db.store import _conn, get_model_version
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_replay import run_strategy_replay


SEALED_HOLDOUT_CONTRACT = "alis_sealed_strategy_holdout_v2"
SEALED_HOLDOUT_START = "20190910"
SEALED_HOLDOUT_END = "20200320"
SEALED_HOLDOUT_SESSIONS = 126
SEALED_HOLDOUT_DATES = (
    "20190910", "20190911", "20190912", "20190916", "20190917", "20190918", "20190919", "20190920",
    "20190923", "20190924", "20190925", "20190926", "20190927", "20190930", "20191008", "20191009",
    "20191010", "20191011", "20191014", "20191015", "20191016", "20191017", "20191018", "20191021",
    "20191022", "20191023", "20191024", "20191025", "20191028", "20191029", "20191030", "20191031",
    "20191101", "20191104", "20191105", "20191106", "20191107", "20191108", "20191111", "20191112",
    "20191113", "20191114", "20191115", "20191118", "20191119", "20191120", "20191121", "20191122",
    "20191125", "20191126", "20191127", "20191128", "20191129", "20191202", "20191203", "20191204",
    "20191205", "20191206", "20191209", "20191210", "20191211", "20191212", "20191213", "20191216",
    "20191217", "20191218", "20191219", "20191220", "20191223", "20191224", "20191225", "20191226",
    "20191227", "20191230", "20191231", "20200102", "20200103", "20200106", "20200107", "20200108",
    "20200109", "20200110", "20200113", "20200114", "20200115", "20200116", "20200117", "20200120",
    "20200121", "20200122", "20200123", "20200203", "20200204", "20200205", "20200206", "20200207",
    "20200210", "20200211", "20200212", "20200213", "20200214", "20200217", "20200218", "20200219",
    "20200220", "20200221", "20200224", "20200225", "20200226", "20200227", "20200228", "20200302",
    "20200303", "20200304", "20200305", "20200306", "20200309", "20200310", "20200311", "20200312",
    "20200313", "20200316", "20200317", "20200318", "20200319", "20200320",
)


def _ensure_table() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS strategy_sealed_holdout_results (
            id BIGSERIAL PRIMARY KEY,
            cohort_key VARCHAR(128) NOT NULL UNIQUE,
            model_version_id BIGINT NOT NULL,
            champion_version_id BIGINT NOT NULL,
            contract_hash VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            passed INTEGER NOT NULL DEFAULT 0,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL
        )""")


def sealed_strategy_holdout_asset() -> dict[str, Any]:
    audit = audit_sealed_strategy_holdout(include_dates=False)
    return {
        "key": "sealed_strategy_holdout",
        "name": "策略晋升密封历史保留集",
        "domain": "模型验证",
        "consumer": "formal_candidate_gate only",
        "weight": 100,
        "target_scope": f"固定 {SEALED_HOLDOUT_SESSIONS} 个交易日（研究过程不可见）",
        "frequency": "fixed_once",
        "source": "TuShare retained hard slices + 标准化日截面",
        "window_start": SEALED_HOLDOUT_START,
        "window_end": SEALED_HOLDOUT_END,
        "window_depth": f"{SEALED_HOLDOUT_SESSIONS} sessions",
        "rationale": "仅在正式 OOS、同批 Champion 对照和多重试验校正全部通过后消费一次",
        "actual_records": audit["eligible_sessions"],
        "expected_records": SEALED_HOLDOUT_SESSIONS,
        "coverage": round(audit["eligible_sessions"] / SEALED_HOLDOUT_SESSIONS, 6),
        "coverage_basis": audit["eligible_sessions"],
        "coverage_expected": SEALED_HOLDOUT_SESSIONS,
        "symbols": 0,
        "dates": audit["eligible_sessions"],
        "periods_actual": None,
        "periods_expected": None,
        "min_date": SEALED_HOLDOUT_START,
        "max_date": SEALED_HOLDOUT_END,
        "status": "complete" if audit["status"] == "ready" else "partial",
        "measurement": "eligible_sessions",
        "period_details": None,
        "hard_requirement": True,
    }


def audit_sealed_strategy_holdout(*, include_dates: bool = False) -> dict[str, Any]:
    calendar = audit_trade_calendar_range(
        SEALED_HOLDOUT_START,
        SEALED_HOLDOUT_END,
        expected_dates=SEALED_HOLDOUT_DATES,
    )
    calendar_dates = set(calendar.get("present_dates") or [])
    with _data_conn() as conn:
        rows = conn.execute(
            """SELECT trade_date,COUNT(*) AS rows_total,
                      SUM(CASE WHEN close IS NOT NULL AND volume IS NOT NULL AND turnover IS NOT NULL THEN 1 ELSE 0 END) AS market_rows,
                      MAX(available_time) AS max_available_time
               FROM dc_stock_daily_snapshot WHERE trade_date BETWEEN ? AND ?
               GROUP BY trade_date""",
            (SEALED_HOLDOUT_START, SEALED_HOLDOUT_END),
        ).fetchall()
    compact = list(SEALED_HOLDOUT_DATES)
    api_dates = _date_api_sets(SEALED_HOLDOUT_START, SEALED_HOLDOUT_END)
    benchmark_dates = _benchmark_date_set(
        SEALED_HOLDOUT_START,
        SEALED_HOLDOUT_END,
    )
    snapshots = {str(dict(row).get("trade_date") or ""): dict(row) for row in rows}
    details: list[dict[str, Any]] = []
    for trade_date in compact:
        snapshot = snapshots.get(trade_date) or {}
        rows_total = int(snapshot.get("rows_total") or 0)
        market_rows = int(snapshot.get("market_rows") or 0)
        ratio = market_rows / rows_total if rows_total else 0.0
        cutoff = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00"
        missing_apis = [api for api in HARD_APIS if trade_date not in api_dates[api]]
        if trade_date not in benchmark_dates:
            missing_apis.append(BENCHMARK_API_KEY)
        calendar_ok = trade_date in calendar_dates
        pit_ok = bool(snapshot.get("max_available_time")) and str(snapshot.get("max_available_time")) <= cutoff
        eligible = (
            calendar_ok
            and
            rows_total >= MINIMUM_DAILY_ROWS
            and ratio >= MINIMUM_MARKET_FIELD_RATIO
            and not missing_apis
            and pit_ok
        )
        reasons = []
        if not calendar_ok:
            reasons.append("trade_calendar_missing")
        if rows_total < MINIMUM_DAILY_ROWS:
            reasons.append("snapshot_rows_insufficient")
        if ratio < MINIMUM_MARKET_FIELD_RATIO:
            reasons.append("market_fields_incomplete")
        if missing_apis:
            reasons.append("missing_hard_api_slices")
        if BENCHMARK_API_KEY in missing_apis:
            reasons.append("benchmark_index_missing")
        if not pit_ok:
            reasons.append("point_in_time_invalid")
        details.append({
            "trade_date": trade_date,
            "eligible": eligible,
            "rows_total": rows_total,
            "market_field_ratio": round(ratio, 6),
            "missing_apis": missing_apis,
            "trade_calendar_present": calendar_ok,
            "point_in_time_passed": pit_ok,
            "reasons": reasons,
        })
    eligible_dates = [item["trade_date"] for item in details if item["eligible"]]
    repair_dates = [item["trade_date"] for item in details if not item["eligible"]]
    ready = len(compact) == SEALED_HOLDOUT_SESSIONS and len(eligible_dates) == SEALED_HOLDOUT_SESSIONS
    result = {
        "schema_version": SEALED_HOLDOUT_CONTRACT,
        "status": "ready" if ready else "blocked",
        "start_date": SEALED_HOLDOUT_START,
        "end_date": SEALED_HOLDOUT_END,
        "required_sessions": SEALED_HOLDOUT_SESSIONS,
        "calendar_sessions": len(compact),
        "calendar_present_sessions": calendar["present_sessions"],
        "calendar_missing_sessions": calendar["missing_sessions"],
        "calendar_missing_dates": calendar["missing_dates"],
        "eligible_sessions": len(eligible_dates),
        "missing_sessions": max(0, SEALED_HOLDOUT_SESSIONS - len(eligible_dates)),
        "repair_dates": repair_dates,
        "hard_apis": [*HARD_APIS, BENCHMARK_API_KEY],
        "thresholds": {
            "minimum_daily_rows": MINIMUM_DAILY_ROWS,
            "minimum_hard_api_rows": MINIMUM_HARD_API_ROWS,
            "minimum_market_field_ratio": MINIMUM_MARKET_FIELD_RATIO,
        },
        "policy": "details_are_not_consumable_by_candidate_generation_or_failure_attribution",
    }
    if include_dates:
        result["dates"] = details
    return result


def repair_sealed_strategy_holdout(*, limit: int = 5) -> dict[str, Any]:
    before = audit_sealed_strategy_holdout(include_dates=True)
    calendar_repair = repair_trade_calendar_range(
        SEALED_HOLDOUT_START,
        SEALED_HOLDOUT_END,
        expected_dates=SEALED_HOLDOUT_DATES,
    )
    audit = audit_sealed_strategy_holdout(include_dates=True)
    dates = list(audit.get("repair_dates") or [])[:max(1, min(int(limit), 20))]
    detail_by_date = {item["trade_date"]: item for item in audit.get("dates") or []}
    repair = repair_oos_hard_data_batch(dates, date_gaps=detail_by_date) if dates else {"ok": True, "results": []}
    verification = audit_sealed_strategy_holdout(include_dates=False)
    return {
        "schema_version": "alis_sealed_strategy_holdout_repair_v2",
        "status": "repaired_batch" if dates and repair.get("ok") else "already_ready" if not dates else "repair_partial",
        "dates": dates,
        "calendar_repair": calendar_repair,
        "repair": repair,
        "verification": verification,
        "progress": {
            "before": int(before.get("eligible_sessions") or 0),
            "after": int(verification.get("eligible_sessions") or 0),
            "delta": int(verification.get("eligible_sessions") or 0) - int(before.get("eligible_sessions") or 0),
        },
    }


def sealed_holdout_adj_factor_only_gap() -> dict[str, Any]:
    """Identify the one safe bulk-repair case without exposing holdout returns."""
    audit = audit_sealed_strategy_holdout(include_dates=True)
    gaps = [item for item in audit.get("dates") or [] if not bool(item.get("eligible"))]
    homogeneous = bool(gaps) and all(
        set(item.get("missing_apis") or []) == {"adj_factor"}
        and set(item.get("reasons") or []) == {"missing_hard_api_slices"}
        for item in gaps
    )
    return {
        "ok": True,
        "homogeneous": homogeneous,
        "eligible_sessions": int(audit.get("eligible_sessions") or 0),
        "required_sessions": int(audit.get("required_sessions") or SEALED_HOLDOUT_SESSIONS),
        "missing_sessions": int(audit.get("missing_sessions") or 0),
        "repair_dates": [str(item.get("trade_date") or "") for item in gaps] if homogeneous else [],
        "gap_type": "adj_factor_only" if homogeneous else "mixed_or_unknown",
    }


def run_sealed_holdout_adj_factor_backfill(
    *,
    task_id: int,
    batch_size: int = 20,
    heartbeat: Callable[..., None],
    ensure_active: Callable[[int], None],
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drain a homogeneous adj_factor gap with durable task checkpoints."""
    size = max(5, min(int(batch_size), 30))
    prior_checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    completed_batches = max(0, int(prior_checkpoint.get("completed_batches") or 0))
    total_repaired_sessions = max(0, int(prior_checkpoint.get("total_repaired_sessions") or 0))
    total_repair_seconds = max(0.0, float(prior_checkpoint.get("total_repair_seconds") or 0.0))
    batches: list[dict[str, Any]] = []
    while True:
        ensure_active(task_id)
        gap = sealed_holdout_adj_factor_only_gap()
        if not gap.get("homogeneous"):
            verification = audit_sealed_strategy_holdout(include_dates=False)
            if verification.get("status") == "ready":
                return {
                    "ok": True, "status": "ready", "batches": batches,
                    "verification": verification, "batch_size": size,
                }
            raise RuntimeError("密封验证集缺口不再是单一 adj_factor，已停止批量补全并等待有界诊断")
        dates = list(gap.get("repair_dates") or [])
        if not dates:
            verification = audit_sealed_strategy_holdout(include_dates=False)
            return {"ok": verification.get("status") == "ready", "status": verification.get("status"),
                    "batches": batches, "verification": verification, "batch_size": size}
        batch = dates[:size]
        before = int(gap.get("eligible_sessions") or 0)
        detail = {
            value: {"trade_date": value, "missing_apis": ["adj_factor"], "reasons": ["missing_hard_api_slices"]}
            for value in batch
        }
        batch_started = monotonic()
        repair = repair_oos_hard_data_batch(batch, date_gaps=detail)
        batch_seconds = max(0.001, monotonic() - batch_started)
        verification = audit_sealed_strategy_holdout(include_dates=False)
        after = int(verification.get("eligible_sessions") or 0)
        repaired_sessions = max(0, after - before)
        completed_batches += 1
        total_repaired_sessions += repaired_sessions
        total_repair_seconds += batch_seconds
        sessions_per_minute = total_repaired_sessions / total_repair_seconds * 60.0 if total_repair_seconds else 0.0
        remaining_sessions = max(0, SEALED_HOLDOUT_SESSIONS - after)
        estimated_remaining_seconds = int(remaining_sessions / sessions_per_minute * 60.0) if sessions_per_minute > 0 else None
        batches.append({"dates": batch, "before": before, "after": after, "delta": after - before,
                        "ok": bool(repair.get("ok")), "duration_seconds": batch_seconds})
        heartbeat(
            task_id,
            phase="sealed_holdout_adj_factor_backfill",
            progress=after / max(1, SEALED_HOLDOUT_SESSIONS),
            message=f"密封验证集 adj_factor 补全 {after}/{SEALED_HOLDOUT_SESSIONS}，本批 {len(batch)} 日",
            checkpoint={
                "schema_version": "alis_sealed_holdout_adj_factor_backfill_v1",
                "eligible_sessions": after,
                "required_sessions": SEALED_HOLDOUT_SESSIONS,
                "remaining_sessions": remaining_sessions,
                "last_batch_dates": batch,
                "current_batch_size": len(batch),
                "completed_batches": completed_batches,
                "batch_size": size,
                "total_repaired_sessions": total_repaired_sessions,
                "total_repair_seconds": round(total_repair_seconds, 3),
                "sessions_per_minute": round(sessions_per_minute, 2),
                "estimated_remaining_seconds": estimated_remaining_seconds,
            },
        )
        if after <= before:
            raise RuntimeError(f"密封验证集 adj_factor 批量补全无进展：{before}/{SEALED_HOLDOUT_SESSIONS}")
        if verification.get("status") == "ready":
            return {"ok": True, "status": "ready", "batches": batches,
                    "verification": verification, "batch_size": size}


def _contract_hash(cohort_key: str) -> str:
    payload = f"{SEALED_HOLDOUT_CONTRACT}:{SEALED_HOLDOUT_START}:{SEALED_HOLDOUT_END}:{cohort_key}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_sealed_strategy_holdout(
    model_version_id: int,
    *,
    reference_contract: dict[str, Any],
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
) -> dict[str, Any]:
    """Evaluate once and return only a redacted gate result."""
    audit = audit_sealed_strategy_holdout(include_dates=False)
    if audit["status"] != "ready":
        return {
            "schema_version": SEALED_HOLDOUT_CONTRACT,
            "status": "blocked_by_data",
            "passed": False,
            "eligible_sessions": audit["eligible_sessions"],
            "required_sessions": audit["required_sessions"],
        }
    version = get_model_version(int(model_version_id)) or {}
    config = version.get("config") if isinstance(version.get("config"), dict) else {}
    strategy_id = int(config.get("strategy_id") or 0)
    if not strategy_id:
        raise ValueError("候选模型版本缺少 strategy_id")
    champion = resolve_production_strategy_champion()
    cohort_key = hashlib.sha256(json.dumps({
        "hard_data_window": reference_contract.get("hard_data_window"),
        "production_strategy_version_id": reference_contract.get("production_strategy_version_id"),
        "strategy_controller_version": reference_contract.get("strategy_controller_version"),
    }, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    contract_hash = _contract_hash(cohort_key)
    _ensure_table()
    with _conn() as conn:
        insert_sql = """INSERT INTO strategy_sealed_holdout_results(
                            cohort_key,model_version_id,champion_version_id,contract_hash,status,passed,evidence_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?)"""
        if conn._is_postgres:
            insert_sql += " ON CONFLICT(cohort_key) DO NOTHING"
        elif conn._is_mysql:
            insert_sql = insert_sql.replace("INSERT INTO", "INSERT IGNORE INTO", 1)
        else:
            insert_sql = insert_sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
        conn.execute(
            insert_sql,
            (
                cohort_key, int(model_version_id), int(champion["champion_id"]), contract_hash,
                "running", 0, "{}", datetime.now().astimezone().isoformat(),
            ),
        )
        existing = conn.execute(
            "SELECT model_version_id,status,passed,contract_hash FROM strategy_sealed_holdout_results WHERE cohort_key=?",
            (cohort_key,),
        ).fetchone()
    if existing:
        item = dict(existing)
        if int(item["model_version_id"]) != int(model_version_id):
            return {
                "schema_version": SEALED_HOLDOUT_CONTRACT,
                "status": "cohort_already_consumed",
                "passed": False,
                "contract_hash": str(item["contract_hash"]),
            }
        if str(item["status"]) != "running":
            return {
                "schema_version": SEALED_HOLDOUT_CONTRACT,
                "status": str(item["status"]),
                "passed": bool(item["passed"]),
                "contract_hash": str(item["contract_hash"]),
                "reused": True,
            }
    candidate = replay_runner(
        strategy_id=strategy_id,
        model_version_id=int(model_version_id),
        start_date=SEALED_HOLDOUT_START,
        end_date=SEALED_HOLDOUT_END,
    )
    reference = replay_runner(
        strategy_id=int(champion["strategy_id"]),
        model_version_id=None,
        start_date=SEALED_HOLDOUT_START,
        end_date=SEALED_HOLDOUT_END,
    )
    candidate_metrics = candidate.get("metrics") or {}
    reference_metrics = reference.get("metrics") or {}
    candidate_return = float(candidate_metrics.get("total_return") or 0.0)
    reference_return = float(reference_metrics.get("total_return") or 0.0)
    benchmark_return = float(candidate_metrics.get("benchmark_return") or 0.0)
    candidate_pit = bool(((candidate.get("data_quality") or {}).get("point_in_time") or {}).get("passed"))
    reference_pit = bool(((reference.get("data_quality") or {}).get("point_in_time") or {}).get("passed"))
    behavior_passed = not any(
        str(item.get("code") or "") in {"cash_insufficient", "oversell", "empty_snapshot"}
        for replay in (candidate, reference)
        for item in (replay.get("blocked_events") or [])
    )
    passed = (
        candidate_pit and reference_pit and behavior_passed
        and candidate_return > reference_return
        and candidate_return > benchmark_return
    )
    status = "passed" if passed else "failed"
    evidence = {
        "candidate_metrics": candidate_metrics,
        "reference_metrics": reference_metrics,
        "candidate_summary": candidate.get("summary") or {},
        "reference_summary": reference.get("summary") or {},
        "point_in_time_passed": candidate_pit and reference_pit,
        "behavior_passed": behavior_passed,
    }
    with _conn() as conn:
        conn.execute(
            """UPDATE strategy_sealed_holdout_results
               SET status=?,passed=?,evidence_json=?
               WHERE cohort_key=? AND model_version_id=?""",
            (
                status, 1 if passed else 0, json.dumps(evidence, ensure_ascii=False, default=str),
                cohort_key, int(model_version_id),
            ),
        )
    return {
        "schema_version": SEALED_HOLDOUT_CONTRACT,
        "status": status,
        "passed": passed,
        "contract_hash": contract_hash,
        "policy": "redacted_result_only",
    }
