from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

from app.db.store import _conn
from app.data_platform.db import _conn as data_conn

HORIZONS = (5, 20, 60)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_tables() -> None:
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS decision_outcome_attributions (
            id BIGSERIAL PRIMARY KEY, decision_snapshot_id BIGINT NOT NULL,
            horizon_sessions INTEGER NOT NULL, entry_date VARCHAR(16) NOT NULL,
            exit_date VARCHAR(16) NOT NULL, stock_return DOUBLE PRECISION NOT NULL,
            directional_return DOUBLE PRECISION NOT NULL, benchmark_return DOUBLE PRECISION,
            industry_return DOUBLE PRECISION, market_contribution DOUBLE PRECISION,
            industry_contribution DOUBLE PRECISION, selection_contribution DOUBLE PRECISION,
            correct INTEGER NOT NULL, attribution_json TEXT NOT NULL DEFAULT '{}',
            calculated_at VARCHAR(64) NOT NULL,
            UNIQUE(decision_snapshot_id,horizon_sessions)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS model_training_feedback (
            id BIGSERIAL PRIMARY KEY, review_record_id BIGINT,
            decision_snapshot_id BIGINT NOT NULL, model_version VARCHAR(128) NOT NULL,
            label DOUBLE PRECISION NOT NULL, sample_weight DOUBLE PRECISION NOT NULL,
            source VARCHAR(32) NOT NULL, error_type VARCHAR(64),
            features_json TEXT NOT NULL DEFAULT '{}', outcome_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
            UNIQUE(decision_snapshot_id,source)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_training_feedback_model ON model_training_feedback(model_version)")
        c.execute("""CREATE TABLE IF NOT EXISTS manual_review_triage (
            review_key VARCHAR(160) PRIMARY KEY, item_kind VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL, can_handle INTEGER NOT NULL DEFAULT 0,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
            escalation_reason TEXT NOT NULL DEFAULT '', draft_json TEXT NOT NULL DEFAULT '{}',
            model VARCHAR(128) NOT NULL DEFAULT '', updated_at VARCHAR(64) NOT NULL
        )""")


def _payload(row: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        value = json.loads(row.get(key) or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _benchmark_prices(dates: tuple[str, str]) -> dict[str, float]:
    with data_conn() as c:
        rows = c.execute("""SELECT trade_date,payload_json FROM dc_tushare_raw_records
            WHERE api_name='index_daily' AND ts_code IN ('000300.SH','000300')
            AND trade_date IN (?,?) ORDER BY ts_code DESC""", dates).fetchall()
    result: dict[str, float] = {}
    for raw in rows:
        row = dict(raw)
        try:
            close = float(json.loads(row.get("payload_json") or "{}").get("close") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            close = 0
        if close > 0:
            result[str(row.get("trade_date"))] = close
    return result


def _industry_return(industry: str, entry_date: str, exit_date: str) -> float | None:
    if not industry:
        return None
    with data_conn() as c:
        row = c.execute("""WITH entry_prices AS (
              SELECT ts_code,close FROM dc_stock_daily_snapshot WHERE trade_date=? AND industry=? AND close>0
            ), exit_prices AS (
              SELECT ts_code,close FROM dc_stock_daily_snapshot WHERE trade_date=? AND industry=? AND close>0
            ) SELECT AVG(x.close/e.close-1.0) value FROM entry_prices e JOIN exit_prices x USING(ts_code)""",
            (entry_date, industry, exit_date, industry)).fetchone()
    value = dict(row).get("value") if row else None
    return float(value) if value is not None else None


def _outcome_needs_benchmark_recompute(row: dict[str, Any] | None) -> bool:
    """Allow one deterministic refresh after a previously missing benchmark is repaired."""

    if not isinstance(row, dict) or row.get("benchmark_return") is not None:
        return False
    try:
        details = json.loads(row.get("attribution_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        details = {}
    limitations = details.get("limitations") if isinstance(details, dict) else []
    return "缺少沪深300同期收盘价" in (limitations if isinstance(limitations, list) else [])


def materialize_decision_outcomes(limit: int = 1000) -> dict[str, Any]:
    """Persist point-in-time 5/20/60-session outcomes and market/industry/selection attribution."""
    _ensure_tables()
    with _conn() as c:
        decisions = [dict(row) for row in c.execute(
            # Outcome materialisation only needs the decision identity and
            # direction.  Frozen evidence JSON is loaded by audit/detail paths,
            # never by this batch job.
            "SELECT id,ts_code,decision_type,decision_time FROM decision_snapshots "
            "ORDER BY decision_time DESC,id DESC LIMIT ?", (limit,)
        ).fetchall()]
        existing = {
            (int(row["decision_snapshot_id"]), int(row["horizon_sessions"])): dict(row)
            for row in c.execute(
                "SELECT decision_snapshot_id,horizon_sessions,benchmark_return,attribution_json "
                "FROM decision_outcome_attributions"
            ).fetchall()
        }
    created = 0
    pending = 0
    for decision in decisions:
        decision_id = int(decision["id"])
        code = str(decision.get("ts_code") or "")
        bare_code = code.split(".")[0]
        normalized_code = code if "." in code else f"{bare_code}.SH" if bare_code.startswith(("5", "6", "9")) else f"{bare_code}.SZ"
        date = str(decision.get("decision_time") or "").replace("-", "")[:8]
        with data_conn() as c:
            prices = [dict(row) for row in c.execute("""SELECT trade_date,close,industry FROM dc_stock_daily_snapshot
                WHERE ts_code IN (?,?) AND trade_date>=? AND close>0 ORDER BY trade_date LIMIT 61""",
                (code, normalized_code, date)).fetchall()]
        if not prices:
            pending += len(HORIZONS)
            continue
        for horizon in HORIZONS:
            existing_row = existing.get((decision_id, horizon))
            if existing_row and not _outcome_needs_benchmark_recompute(existing_row):
                continue
            if len(prices) <= horizon:
                pending += 1
                continue
            entry, exit_row = prices[0], prices[horizon]
            entry_price = float(entry["close"]); exit_price = float(exit_row["close"])
            stock_return = exit_price / entry_price - 1.0
            direction = str(decision.get("decision_type") or "").lower()
            directional = -stock_return if direction in {"sell", "reduce", "exit"} else stock_return
            entry_date, exit_date = str(entry["trade_date"]), str(exit_row["trade_date"])
            benchmarks = _benchmark_prices((entry_date, exit_date))
            bm_return = benchmarks.get(exit_date) / benchmarks.get(entry_date) - 1.0 if benchmarks.get(entry_date) and benchmarks.get(exit_date) else None
            industry = str(entry.get("industry") or "")
            ind_return = _industry_return(industry, entry_date, exit_date)
            market = bm_return
            industry_component = (ind_return - bm_return) if ind_return is not None and bm_return is not None else None
            selection = stock_return - (ind_return if ind_return is not None else bm_return) if (ind_return is not None or bm_return is not None) else None
            correct = directional > 0
            details = {"method": "fixed_horizon_brinson_v1", "industry": industry, "direction": direction,
                "entry_price": entry_price, "exit_price": exit_price, "limitations": []}
            if bm_return is None: details["limitations"].append("缺少沪深300同期收盘价")
            if ind_return is None: details["limitations"].append("缺少行业同期横截面")
            with _conn() as c:
                statement = """INSERT INTO decision_outcome_attributions
                    (decision_snapshot_id,horizon_sessions,entry_date,exit_date,stock_return,directional_return,
                     benchmark_return,industry_return,market_contribution,industry_contribution,selection_contribution,
                     correct,attribution_json,calculated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
                params = (decision_id,horizon,entry_date,exit_date,stock_return,directional,bm_return,ind_return,market,
                          industry_component,selection,int(correct),json.dumps(details,ensure_ascii=False),_now())
                if existing_row:
                    statement += """ ON CONFLICT(decision_snapshot_id,horizon_sessions) DO UPDATE SET
                        entry_date=excluded.entry_date,exit_date=excluded.exit_date,stock_return=excluded.stock_return,
                        directional_return=excluded.directional_return,benchmark_return=excluded.benchmark_return,
                        industry_return=excluded.industry_return,market_contribution=excluded.market_contribution,
                        industry_contribution=excluded.industry_contribution,selection_contribution=excluded.selection_contribution,
                        correct=excluded.correct,attribution_json=excluded.attribution_json,calculated_at=excluded.calculated_at"""
                else:
                    statement += " ON CONFLICT DO NOTHING"
                c.execute(statement, params)
                c.execute("DELETE FROM manual_review_triage WHERE review_key=? AND status IN ('escalated','awaiting_outcome')", (f"decision:{decision_id}",))
            created += 1
    feedback = materialize_training_feedback()
    return {"ok": True, "decisions": len(decisions), "created": created, "pending": pending, "feedback": feedback}


def materialize_training_feedback() -> dict[str, Any]:
    """Convert reviewed decisions plus mature outcomes into versioned supervised feedback samples."""
    _ensure_tables()
    with _conn() as c:
        rows = [dict(row) for row in c.execute("""SELECT d.*,r.id review_id,r.outcome_json,r.error_type,r.lessons,
            a.directional_return,a.correct,a.horizon_sessions,a.attribution_json
            FROM decision_snapshots d
            JOIN review_records r ON r.decision_snapshot_id=d.id
            LEFT JOIN decision_outcome_attributions a ON a.decision_snapshot_id=d.id
              AND a.horizon_sessions=(SELECT MAX(a2.horizon_sessions) FROM decision_outcome_attributions a2 WHERE a2.decision_snapshot_id=d.id AND a2.horizon_sessions<=20)
            ORDER BY r.id""").fetchall()]
    written = 0
    for row in rows:
        outcome = _payload(row, "outcome_json")
        verdict = str(outcome.get("human_verdict") or "")
        observed = row.get("directional_return")
        label = float(int(row.get("correct") or 0)) if observed is not None else (0.0 if verdict == "rejected" else 1.0)
        weight = 1.5 if verdict in {"approved", "rejected", "modified_approved"} else 1.0
        if observed is None: weight *= 0.5
        features = {"market_state": _payload(row, "market_state_json"), "research_state": _payload(row, "research_state_json"),
            "valuation": _payload(row, "valuation_json"), "portfolio_context": _payload(row, "portfolio_context_json"),
            "decision_type": row.get("decision_type"), "horizon_sessions": row.get("horizon_sessions")}
        result = {"directional_return": observed, "human_verdict": verdict, "lessons": row.get("lessons"),
            "attribution": _payload(row, "attribution_json")}
        with _conn() as c:
            c.execute("""INSERT INTO model_training_feedback
                (review_record_id,decision_snapshot_id,model_version,label,sample_weight,source,error_type,features_json,outcome_json,created_at,updated_at)
                VALUES(?,?,?,?,?,'human_review',?,?,?,?,?) ON CONFLICT(decision_snapshot_id,source) DO UPDATE SET
                review_record_id=excluded.review_record_id,label=excluded.label,sample_weight=excluded.sample_weight,
                error_type=excluded.error_type,features_json=excluded.features_json,outcome_json=excluded.outcome_json,updated_at=excluded.updated_at""",
                (row["review_id"],row["id"],str(row.get("model_version") or "unknown"),label,weight,row.get("error_type"),
                 json.dumps(features,ensure_ascii=False),json.dumps(result,ensure_ascii=False),_now(),_now()))
        written += 1
    with _conn() as c:
        outcomes = [dict(row) for row in c.execute("""SELECT a.*,d.model_version,d.market_state_json,d.research_state_json,
            d.valuation_json,d.portfolio_context_json,d.decision_type,d.ts_code
            FROM decision_outcome_attributions a JOIN decision_snapshots d ON d.id=a.decision_snapshot_id""").fetchall()]
    auto_written = 0
    for row in outcomes:
        source = f"outcome_{int(row['horizon_sessions'])}d"
        features = {"decision_type": row.get("decision_type"), "horizon_sessions": row.get("horizon_sessions"),
            "decision_date": row.get("entry_date"), "ts_code": row.get("ts_code")}
        result = {"directional_return": row.get("directional_return"), "stock_return": row.get("stock_return"),
            "benchmark_return": row.get("benchmark_return"), "industry_return": row.get("industry_return")}
        with _conn() as c:
            c.execute("""INSERT INTO model_training_feedback
                (review_record_id,decision_snapshot_id,model_version,label,sample_weight,source,error_type,features_json,outcome_json,created_at,updated_at)
                VALUES(NULL,?,?,?,?,?,NULL,?,?,?,?) ON CONFLICT(decision_snapshot_id,source) DO UPDATE SET
                label=excluded.label,outcome_json=excluded.outcome_json,updated_at=excluded.updated_at""",
                (row["decision_snapshot_id"],str(row.get("model_version") or "unknown"),float(int(row.get("correct") or 0)),
                 0.5,source,json.dumps(features,ensure_ascii=False),json.dumps(result,ensure_ascii=False),_now(),_now()))
        auto_written += 1
    return {"written": written, "automatic_outcomes": auto_written}


def training_feedback_rows(horizon: int) -> list[dict[str, Any]]:
    """Return mature, point-in-time rows that the ranking trainer can append to its dataset."""
    _ensure_tables()
    with _conn() as c:
        rows = [dict(row) for row in c.execute("""SELECT f.*,d.ts_code,d.decision_type,a.entry_date,a.directional_return
            FROM model_training_feedback f JOIN decision_snapshots d ON d.id=f.decision_snapshot_id
            JOIN decision_outcome_attributions a ON a.decision_snapshot_id=f.decision_snapshot_id
              AND a.horizon_sessions=? WHERE f.source='human_review' AND d.decision_type IN ('buy','hold')""",
            (int(horizon),)).fetchall()]
    return rows


def model_feedback_gate(model_version: str, *, strict_version: bool = False) -> dict[str, Any]:
    """Gate model changes on observed outcomes, never on operator availability.

    Human reviews remain useful diagnostic evidence, but a simulation-only
    autonomous system must be able to validate and degrade itself from frozen
    20-session outcomes when nobody supplies a label.
    """
    _ensure_tables()
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            "SELECT label,sample_weight,error_type,source FROM model_training_feedback WHERE model_version=?", (model_version,)
        ).fetchall()]
        scope = "model_version"
        if not rows and not strict_version:
            rows = [dict(row) for row in c.execute(
                "SELECT label,sample_weight,error_type,source FROM model_training_feedback"
            ).fetchall()]
            scope = "review_pipeline_baseline"
    total = len(rows)
    weighted = sum(float(row["sample_weight"]) for row in rows)
    accuracy = sum(float(row["label"]) * float(row["sample_weight"]) for row in rows) / weighted if weighted else None
    human = [row for row in rows if row.get("source") == "human_review"]
    mature = [row for row in rows if row.get("source") == "outcome_20d"]
    mature_weight = sum(float(row["sample_weight"]) for row in mature)
    mature_accuracy = (sum(float(row["label"]) * float(row["sample_weight"]) for row in mature) / mature_weight) if mature_weight else None
    severe = sum(str(row.get("error_type") or "") in {"数据质量","模型失效","风险执行","交易执行"} for row in human)
    severe_rate = severe / len(human) if human else None
    gates = {
        "minimum_20d_outcomes": len(mature) >= 5,
        "outcome_accuracy": mature_accuracy is not None and mature_accuracy >= 0.45,
    }
    should_degrade = len(mature) >= 10 and (
        (mature_accuracy is not None and mature_accuracy < 0.35)
        or (len(human) >= 5 and severe_rate is not None and severe_rate > 0.5)
    )
    return {"model_version": model_version, "scope": scope, "samples": total, "human_reviews": len(human), "mature_20d_outcomes": len(mature), "weighted_accuracy": accuracy,
        "outcome_accuracy": mature_accuracy, "severe_error_rate": severe_rate, "gates": gates, "passed": all(gates.values()),
        "should_degrade": should_degrade, "blocked_reasons": [key for key,value in gates.items() if not value]}


def review_learning_overview() -> dict[str, Any]:
    _ensure_tables()
    with _conn() as c:
        totals = dict(c.execute("""SELECT COUNT(*) outcomes,COUNT(DISTINCT decision_snapshot_id) decisions,
            SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) correct FROM decision_outcome_attributions""").fetchone())
        feedback = dict(c.execute("SELECT COUNT(*) samples,COUNT(DISTINCT model_version) models FROM model_training_feedback").fetchone())
        errors = [dict(row) for row in c.execute("""SELECT COALESCE(error_type,'无错误') error_type,COUNT(*) count
            FROM review_records GROUP BY COALESCE(error_type,'无错误') ORDER BY count DESC""").fetchall()]
        latest = [dict(row) for row in c.execute("""SELECT a.*,d.ts_code,d.decision_type,d.model_version
            FROM decision_outcome_attributions a JOIN decision_snapshots d ON d.id=a.decision_snapshot_id
            ORDER BY a.calculated_at DESC,a.id DESC LIMIT 20""").fetchall()]
    outcomes = int(totals.get("outcomes") or 0)
    return {"ok": True, "counts": {"outcomes": outcomes, "decisions": int(totals.get("decisions") or 0),
        "feedback_samples": int(feedback.get("samples") or 0), "models": int(feedback.get("models") or 0)},
        "accuracy": int(totals.get("correct") or 0) / outcomes if outcomes else None,
        "error_statistics": errors, "latest_outcomes": latest}


def decision_outcomes(decision_snapshot_id: int) -> list[dict[str, Any]]:
    _ensure_tables()
    with _conn() as c:
        return [dict(row) for row in c.execute("""SELECT * FROM decision_outcome_attributions
            WHERE decision_snapshot_id=? ORDER BY horizon_sessions""", (int(decision_snapshot_id),)).fetchall()]
