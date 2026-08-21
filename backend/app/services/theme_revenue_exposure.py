"""Point-in-time theme revenue exposure from retained business segments."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.observation_clock import load_raw_record_versions
from app.core.sql_compat import qmark_to_pyformat
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


def _initial_company_rows(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    """Discover current companies from the bounded overwrite projection.

    This is the initial *current* materialization pass, not a historical PIT
    replay.  Calling ``load_raw_record_versions`` here forces PostgreSQL to
    join/rank the append-only ``fina_mainbz`` history before applying the
    caller's limit; that table is large enough for the otherwise harmless
    discovery step to become a long ``raw_ranked`` query.  The current
    projection already contains the accepted winner and is the correct source
    for this non-historical discovery.  Historical materialization below keeps
    the immutable loader and its explicit cutoff unchanged.
    """
    cap = max(1, int(limit)) + 1
    try:
        rows = conn.execute(
            """SELECT DISTINCT ts_code
                 FROM dc_tushare_raw_records
                WHERE api_name='fina_mainbz'
                  AND available_time<>''
                  AND COALESCE(ts_code,'')<>''
                ORDER BY ts_code LIMIT ?""",
            (cap,),
        ).fetchall()
    except Exception:
        # A missing current projection is a real production data gap.  Keep
        # PostgreSQL fail-closed instead of widening back to the raw history;
        # tiny legacy SQLite fixtures may still expose only the old table.
        if bool(getattr(conn, "_is_postgres", False)):
            return []
        return []
    return [
        {"ts_code": str(dict(row).get("ts_code") or "")}
        for row in rows
        if str(dict(row).get("ts_code") or "")
    ][:cap]


def _latest_segment_rows(
    conn: Any,
    codes: list[str],
    *,
    cutoff: str,
    batch_size: int = 250,
) -> list[dict[str, Any]]:
    """Read bounded company batches so one statement never scans the full domain."""
    result: list[dict[str, Any]] = []
    size = max(25, min(int(batch_size), 500))
    for offset in range(0, len(codes), size):
        batch = codes[offset:offset + size]
        versioned = load_raw_record_versions(
            conn, "fina_mainbz", cutoff=cutoff, ts_codes=batch,
        )
        if versioned is not None:
            result.extend({
                **dict(row),
                "available_time": str(row.get("observed_at") or ""),
            } for row in versioned)
            continue
        if bool(getattr(conn, "_is_postgres", False)):
            continue
        placeholders = ",".join("?" for _ in batch)
        cutoff_filter = "AND available_time<=?" if cutoff else ""
        cutoff_params: tuple[Any, ...] = (cutoff,) if cutoff else ()
        result.extend(dict(row) for row in conn.execute(
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
            tuple(batch) + cutoff_params + cutoff_params,
        ).fetchall())
    return result


def _execute_many(conn: Any, sql: str, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    if getattr(conn, "_is_postgres", False):
        cursor = conn.raw.cursor()
        cursor.executemany(qmark_to_pyformat(sql), rows)
    elif getattr(conn, "_is_mysql", False):
        cursor = conn.raw.cursor()
        cursor.executemany(qmark_to_pyformat(sql), rows)
    else:
        conn.raw.executemany(sql, rows)


def _current_exposure_rows(conn: Any, codes: list[str], *, batch_size: int = 500) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    size = max(25, min(int(batch_size), 500))
    for offset in range(0, len(codes), size):
        batch = codes[offset:offset + size]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""SELECT theme_code,ts_code,source_fingerprint
                  FROM dc_theme_revenue_exposure_current
                 WHERE ts_code IN ({placeholders})""",
            tuple(batch),
        ).fetchall():
            item = dict(row)
            result.setdefault(str(item.get("ts_code") or ""), []).append(item)
    return result


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
        if not cutoff and not last_run:
            changed_rows = _initial_company_rows(conn, limit=company_limit)
        elif cutoff:
            # Historical materialization must discover companies from the
            # immutable observation log.  The current projection can contain
            # a later replacement whose availability is no longer replayable.
            versioned = load_raw_record_versions(
                conn, "fina_mainbz", cutoff=cutoff,
                limit=company_limit + 1,
            )
            if versioned is not None:
                changed_rows = [
                    {"ts_code": str(row.get("ts_code") or "")}
                    for row in versioned
                    if str(row.get("ts_code") or "")
                ]
            elif bool(getattr(conn, "_is_postgres", False)):
                changed_rows = []
            else:
                changed_rows = [dict(row) for row in conn.execute(
                    f"""SELECT ts_code,MAX(updated_at) source_updated_at,MAX(id) source_id
                           FROM dc_tushare_raw_records
                          WHERE api_name='fina_mainbz' {change_clause}
                          GROUP BY ts_code ORDER BY ts_code LIMIT ?""",
                    tuple(change_params + [company_limit + 1]),
                ).fetchall()]
        else:
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
        rows = _latest_segment_rows(conn, changed_codes, cutoff=cutoff)
    by_company: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("ts_code") or "")
        if code:
            by_company.setdefault(code, []).append(row)
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    companies_changed = 0
    with _conn() as conn:
        current_by_company = {} if cutoff else _current_exposure_rows(conn, list(by_company))
        changed_company_codes: list[str] = []
        history_values: list[tuple[Any, ...]] = []
        current_values: list[tuple[Any, ...]] = []
        removed_values: list[tuple[Any, ...]] = []
        for code, segments in by_company.items():
            exposures, fingerprint = _company_exposures(segments)
            current_rows = current_by_company.get(code) or []
            if not cutoff and current_rows and {str(row.get("source_fingerprint") or "") for row in current_rows} == {fingerprint}:
                continue
            companies_changed += 1
            previous_themes = {str(row.get("theme_code") or "") for row in current_rows}
            if not cutoff:
                changed_company_codes.append(code)
            for theme_code, exposure in exposures.items():
                evidence_json = json.dumps(exposure["evidence"], ensure_ascii=False)
                history_values.append((cutoff_date, theme_code, code, exposure["revenue_share"], exposure["matched_sales"], exposure["total_segment_sales"], exposure["source_period"], exposure["source_available_time"], evidence_json, now))
                if not cutoff:
                    current_values.append((theme_code, code, cutoff_date, exposure["revenue_share"], exposure["matched_sales"], exposure["total_segment_sales"], exposure["source_period"], exposure["source_available_time"], fingerprint, evidence_json, now))
                written += 1
            for removed_theme in previous_themes - set(exposures):
                removed_values.append((cutoff_date, removed_theme, code, json.dumps({"method": "fina_mainbz_theme_removed_v2", "active": False}, ensure_ascii=False), now))
        if not cutoff:
            for offset in range(0, len(changed_company_codes), 500):
                batch = changed_company_codes[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                conn.execute(f"DELETE FROM dc_theme_revenue_exposure_current WHERE ts_code IN ({placeholders})", tuple(batch))
        _execute_many(conn, """INSERT INTO dc_theme_revenue_exposure
               (as_of_date,theme_code,ts_code,revenue_share,matched_sales,total_segment_sales,source_period,source_available_time,evidence_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(as_of_date,theme_code,ts_code) DO UPDATE SET
               revenue_share=excluded.revenue_share,matched_sales=excluded.matched_sales,total_segment_sales=excluded.total_segment_sales,
               source_period=excluded.source_period,source_available_time=excluded.source_available_time,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""", history_values)
        _execute_many(conn, """INSERT INTO dc_theme_revenue_exposure_current(theme_code,ts_code,as_of_date,revenue_share,matched_sales,total_segment_sales,source_period,source_available_time,source_fingerprint,evidence_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(theme_code,ts_code) DO UPDATE SET
               as_of_date=excluded.as_of_date,revenue_share=excluded.revenue_share,matched_sales=excluded.matched_sales,
               total_segment_sales=excluded.total_segment_sales,source_period=excluded.source_period,
               source_available_time=excluded.source_available_time,source_fingerprint=excluded.source_fingerprint,
               evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""", current_values)
        _execute_many(conn, """INSERT INTO dc_theme_revenue_exposure(as_of_date,theme_code,ts_code,revenue_share,matched_sales,total_segment_sales,source_period,source_available_time,evidence_json,updated_at)
               VALUES(?,?,?,0,0,0,'','',?,?) ON CONFLICT(as_of_date,theme_code,ts_code) DO UPDATE SET
               revenue_share=0,matched_sales=0,total_segment_sales=0,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""", removed_values)
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
