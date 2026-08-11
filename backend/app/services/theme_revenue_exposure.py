"""Point-in-time theme revenue exposure from retained business segments."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any

from app.data_platform.db import _conn
from app.services.news_theme_cluster import INDUSTRY_KEYWORDS


def _ensure_tables() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_theme_revenue_exposure (
            as_of_date TEXT NOT NULL,
            theme_code TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            revenue_share REAL NOT NULL,
            matched_sales REAL NOT NULL,
            total_segment_sales REAL NOT NULL,
            source_period TEXT NOT NULL,
            source_available_time TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(as_of_date,theme_code,ts_code)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_theme_revenue_exposure_theme ON dc_theme_revenue_exposure(theme_code,as_of_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_theme_revenue_exposure_current (
            theme_code TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            revenue_share REAL NOT NULL,
            matched_sales REAL NOT NULL,
            total_segment_sales REAL NOT NULL,
            source_period TEXT NOT NULL,
            source_available_time TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(theme_code,ts_code)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_theme_revenue_current_code ON dc_theme_revenue_exposure_current(ts_code)")
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_theme_revenue_exposure_runs (
            as_of_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            source_updated_at TEXT NOT NULL DEFAULT '',
            source_id INTEGER NOT NULL DEFAULT 0,
            companies_examined INTEGER NOT NULL DEFAULT 0,
            companies_changed INTEGER NOT NULL DEFAULT 0,
            exposures_written INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )""")
        if getattr(conn, "_is_postgres", False):
            conn.execute("SELECT set_config('statement_timeout', ?, false)", ("120000",))
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_dc_raw_fina_mainbz_update_cursor
            ON dc_tushare_raw_records(updated_at,id) WHERE api_name='fina_mainbz'""")


def _sales(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _matching_themes(item: str) -> list[str]:
    text = str(item or "").lower()
    return [name for name, keywords in INDUSTRY_KEYWORDS.items() if any(str(keyword).lower() in text for keyword in keywords)]


def _company_exposures(segments: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str]:
    parsed: list[dict[str, Any]] = []
    fingerprint_rows: list[dict[str, Any]] = []
    for row in segments:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        parsed.append({
            "item": str(payload.get("bz_item") or ""), "sales": _sales(payload.get("bz_sales")),
            "period": str(row.get("period") or ""), "available_time": str(row.get("available_time") or ""),
        })
        fingerprint_rows.append({
            "id": int(row.get("id") or 0), "period": str(row.get("period") or ""),
            "available_time": str(row.get("available_time") or ""), "payload": payload,
        })
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    total = sum(item["sales"] for item in parsed)
    if total <= 0:
        return {}, fingerprint
    source = max(parsed, key=lambda item: (item["period"], item["available_time"]))
    exposures: dict[str, dict[str, Any]] = {}
    for theme in INDUSTRY_KEYWORDS:
        matched = [item for item in parsed if theme in _matching_themes(item["item"])]
        matched_sales = sum(item["sales"] for item in matched)
        if matched_sales <= 0:
            continue
        exposures[f"revenue:{theme}"] = {
            "revenue_share": round(matched_sales / total, 8), "matched_sales": matched_sales,
            "total_segment_sales": total, "source_period": source["period"],
            "source_available_time": source["available_time"],
            "evidence": {"method": "fina_mainbz_business_segment_keyword_v2", "matched_items": [item["item"] for item in matched][:20], "segment_count": len(parsed), "matched_segment_count": len(matched), "limitation": "主营业务分部可能包含产品、地区或客户口径，按公开原始项目保留证据，不与新闻关联度混淆。"},
        }
    return exposures, fingerprint


def materialize_theme_revenue_exposure(*, as_of_time: str | None = None, limit_companies: int = 8_000) -> dict[str, Any]:
    """Incrementally persist latest revenue exposure while retaining PIT history."""
    _ensure_tables()
    cutoff = str(as_of_time or "")
    cutoff_date = cutoff[:10].replace("-", "")
    company_limit = max(1, min(int(limit_companies), 8_000))
    with _conn() as conn:
        if not cutoff_date:
            row = conn.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone()
            cutoff_date = str(dict(row).get("value") or "") if row else ""
        last_run = None if cutoff else conn.execute(
            """SELECT source_updated_at,source_id FROM dc_theme_revenue_exposure_runs
                 WHERE status='completed' ORDER BY source_updated_at DESC,source_id DESC LIMIT 1"""
        ).fetchone()
        cursor = dict(last_run) if last_run else {"source_updated_at": "", "source_id": 0}
        cursor_time = str(cursor.get("source_updated_at") or "")
        cursor_id = int(cursor.get("source_id") or 0)
        change_clause = ""
        change_params: list[Any] = []
        if cutoff:
            change_clause = "AND available_time<=?"
            change_params.append(cutoff)
        elif last_run:
            change_clause = "AND (updated_at>? OR (updated_at=? AND id>?))"
            change_params.extend((cursor_time, cursor_time, cursor_id))
        changed_rows = [dict(row) for row in conn.execute(
            f"""SELECT ts_code,MAX(updated_at) source_updated_at,MAX(id) source_id
                   FROM dc_tushare_raw_records
                  WHERE api_name='fina_mainbz' {change_clause}
                  GROUP BY ts_code ORDER BY ts_code LIMIT ?""",
            tuple(change_params + [company_limit + 1]),
        ).fetchall()]
        partial = len(changed_rows) > company_limit
        changed_rows = changed_rows[:company_limit]
        changed_codes = [str(row.get("ts_code") or "") for row in changed_rows if row.get("ts_code")]
        source_cursor_row = conn.execute(
            f"""SELECT updated_at source_updated_at,id source_id
                   FROM dc_tushare_raw_records
                  WHERE api_name='fina_mainbz' {change_clause}
                  ORDER BY updated_at DESC,id DESC LIMIT 1""",
            tuple(change_params),
        ).fetchone()
        if not changed_codes:
            now = datetime.now().isoformat(timespec="seconds")
            run_status = "historical_no_change" if cutoff else "completed"
            result_status = "historical_no_change" if cutoff else "no_change"
            if not cutoff:
                conn.execute(
                    """INSERT INTO dc_theme_revenue_exposure_runs(as_of_date,status,source_updated_at,source_id,companies_examined,companies_changed,exposures_written,updated_at)
                       VALUES(?, ?, ?, ?, 0, 0, 0, ?) ON CONFLICT(as_of_date) DO UPDATE SET
                       status=excluded.status,source_updated_at=excluded.source_updated_at,source_id=excluded.source_id,
                       companies_examined=0,companies_changed=0,exposures_written=0,updated_at=excluded.updated_at""",
                    (cutoff_date, run_status, cursor_time, cursor_id, now),
                )
            return {"ok": True, "schema_version": "alis_theme_revenue_exposure_v2", "status": result_status, "as_of_date": cutoff_date, "companies_examined": 0, "companies_changed": 0, "exposures_written": 0, "policy": "incremental_fina_mainbz_cursor; complete_current_snapshot; point_in_time_history"}
        placeholders = ",".join("?" for _ in changed_codes)
        cutoff_filter = "AND available_time<=?" if cutoff else ""
        cutoff_params: tuple[Any, ...] = (cutoff,) if cutoff else ()
        rows = [dict(row) for row in conn.execute(
            f"""WITH latest_period AS (
                    SELECT ts_code,MAX(period) AS period
                      FROM dc_tushare_raw_records
                     WHERE api_name='fina_mainbz' AND ts_code IN ({placeholders}) {cutoff_filter}
                     GROUP BY ts_code
                )
                SELECT r.id,r.ts_code,r.period,r.available_time,r.payload_json
                  FROM dc_tushare_raw_records r
                  JOIN latest_period p
                    ON p.ts_code=r.ts_code AND p.period=r.period
                 WHERE r.api_name='fina_mainbz' {('AND r.available_time<=?' if cutoff else '')}
                 ORDER BY r.ts_code,r.available_time DESC,r.id DESC""",
            tuple(changed_codes) + cutoff_params + cutoff_params,
        ).fetchall()]
    by_company: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("ts_code") or "")
        if code:
            by_company.setdefault(code, []).append(row)
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    companies_changed = 0
    with _conn() as conn:
        for code, segments in by_company.items():
            exposures, fingerprint = _company_exposures(segments)
            current_rows = [] if cutoff else [dict(row) for row in conn.execute(
                "SELECT theme_code,source_fingerprint FROM dc_theme_revenue_exposure_current WHERE ts_code=?", (code,),
            ).fetchall()]
            if not cutoff and current_rows and {str(row.get("source_fingerprint") or "") for row in current_rows} == {fingerprint}:
                continue
            companies_changed += 1
            previous_themes = {str(row.get("theme_code") or "") for row in current_rows}
            if not cutoff:
                conn.execute("DELETE FROM dc_theme_revenue_exposure_current WHERE ts_code=?", (code,))
            for theme_code, exposure in exposures.items():
                evidence_json = json.dumps(exposure["evidence"], ensure_ascii=False)
                conn.execute(
                    """INSERT INTO dc_theme_revenue_exposure
                       (as_of_date,theme_code,ts_code,revenue_share,matched_sales,total_segment_sales,source_period,source_available_time,evidence_json,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(as_of_date,theme_code,ts_code) DO UPDATE SET
                       revenue_share=excluded.revenue_share,matched_sales=excluded.matched_sales,total_segment_sales=excluded.total_segment_sales,
                       source_period=excluded.source_period,source_available_time=excluded.source_available_time,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                    (cutoff_date, theme_code, code, exposure["revenue_share"], exposure["matched_sales"], exposure["total_segment_sales"], exposure["source_period"], exposure["source_available_time"], evidence_json, now),
                )
                if not cutoff:
                    conn.execute(
                        """INSERT INTO dc_theme_revenue_exposure_current(theme_code,ts_code,as_of_date,revenue_share,matched_sales,total_segment_sales,source_period,source_available_time,source_fingerprint,evidence_json,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(theme_code,ts_code) DO UPDATE SET
                           as_of_date=excluded.as_of_date,revenue_share=excluded.revenue_share,matched_sales=excluded.matched_sales,
                           total_segment_sales=excluded.total_segment_sales,source_period=excluded.source_period,
                           source_available_time=excluded.source_available_time,source_fingerprint=excluded.source_fingerprint,
                           evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                        (theme_code, code, cutoff_date, exposure["revenue_share"], exposure["matched_sales"], exposure["total_segment_sales"], exposure["source_period"], exposure["source_available_time"], fingerprint, evidence_json, now),
                    )
                written += 1
            for removed_theme in previous_themes - set(exposures):
                conn.execute(
                    """INSERT INTO dc_theme_revenue_exposure(as_of_date,theme_code,ts_code,revenue_share,matched_sales,total_segment_sales,source_period,source_available_time,evidence_json,updated_at)
                       VALUES(?,?,?,0,0,0,'','',?,?) ON CONFLICT(as_of_date,theme_code,ts_code) DO UPDATE SET
                       revenue_share=0,matched_sales=0,total_segment_sales=0,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                    (cutoff_date, removed_theme, code, json.dumps({"method": "fina_mainbz_theme_removed_v2", "active": False}, ensure_ascii=False), now),
                )
        max_cursor = dict(source_cursor_row) if source_cursor_row else {}
        if cutoff:
            status = "historical_partial" if partial else "historical_completed"
        else:
            status = "partial" if partial else "completed"
        next_cursor_time = cursor_time if partial or cutoff else str(max_cursor.get("source_updated_at") or cursor_time)
        next_cursor_id = cursor_id if partial or cutoff else int(max_cursor.get("source_id") or cursor_id)
        if not cutoff:
            conn.execute(
                """INSERT INTO dc_theme_revenue_exposure_runs(as_of_date,status,source_updated_at,source_id,companies_examined,companies_changed,exposures_written,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(as_of_date) DO UPDATE SET
                   status=excluded.status,source_updated_at=excluded.source_updated_at,source_id=excluded.source_id,
                   companies_examined=excluded.companies_examined,companies_changed=excluded.companies_changed,
                   exposures_written=excluded.exposures_written,updated_at=excluded.updated_at""",
                (cutoff_date, status, next_cursor_time, next_cursor_id, len(by_company), companies_changed, written, now),
            )
    return {"ok": True, "schema_version": "alis_theme_revenue_exposure_v2", "status": status, "as_of_date": cutoff_date, "companies_examined": len(by_company), "companies_changed": companies_changed, "exposures_written": written, "cursor": {"source_updated_at": next_cursor_time, "source_id": next_cursor_id}, "policy": "incremental_fina_mainbz_cursor; complete_current_snapshot; point_in_time_history; explicit_theme_removal"}
