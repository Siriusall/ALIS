from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from app.data_platform.db import _conn
from app.services.market_context import score_theme_narratives


NARRATIVE_LIFECYCLE_STATES = ("形成", "扩散", "成熟/拥挤", "衰退")
NARRATIVE_LIFECYCLE_ALLOWED_TRANSITIONS = {
    "形成": ("形成", "扩散", "衰退"),
    "扩散": ("形成", "扩散", "成熟/拥挤", "衰退"),
    "成熟/拥挤": ("扩散", "成熟/拥挤", "衰退"),
    "衰退": ("形成", "衰退"),
}


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _lifecycle(
    *,
    score: float,
    base_score: float,
    momentum: float,
    breadth: float,
    crowding: float,
    price_confirmation: float,
) -> str:
    """Map observable theme evidence to a four-state lifecycle.

    ``score`` already contains the crowding penalty, so using
    ``score >= 80 and crowding >= .65`` made the old crowded state
    mathematically unreachable.  Maturity therefore uses the pre-penalty
    evidence score and an independently observed turnover concentration.
    """
    if momentum < -.01 or (score < 40 and price_confirmation < .42):
        return "衰退"
    if base_score >= 55 and crowding >= .60 and price_confirmation >= .50:
        return "成熟/拥挤"
    if base_score >= 55 and breadth >= .55 and price_confirmation >= .50:
        return "扩散"
    return "形成"


def materialize_theme_snapshots(*, as_of_time: str | None = None, lookback: int = 20) -> dict[str, Any]:
    cutoff = str(as_of_time or "")
    cutoff_date = cutoff[:10].replace("-", "")
    with _conn() as c:
        if cutoff_date:
            date_rows = c.execute(
                "SELECT DISTINCT trade_date FROM dc_stock_daily_snapshot WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                (cutoff_date, max(5, min(int(lookback), 60))),
            ).fetchall()
        else:
            date_rows = c.execute(
                "SELECT DISTINCT trade_date FROM dc_stock_daily_snapshot ORDER BY trade_date DESC LIMIT ?",
                (max(5, min(int(lookback), 60)),),
            ).fetchall()
        dates = sorted(str(dict(row).get("trade_date") or "") for row in date_rows)
        if len(dates) < 3:
            return {"ok": False, "status": "insufficient_data", "reason": "fewer_than_three_market_sessions"}
        placeholders = ",".join("?" for _ in dates)
        aggregates = [dict(row) for row in c.execute(
            f"""SELECT industry,trade_date,COUNT(*) company_count,
                       AVG(change_pct) average_change_pct,
                       SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END)::double precision/NULLIF(COUNT(*),0) breadth,
                       SUM(COALESCE(turnover,0)) turnover
                FROM dc_stock_daily_snapshot
                WHERE trade_date IN ({placeholders}) AND COALESCE(industry,'')<>''
                GROUP BY industry,trade_date ORDER BY industry,trade_date""", tuple(dates),
        ).fetchall()]
        state_rows = [dict(row) for row in c.execute(
            """SELECT * FROM (
                   SELECT industry_name,state_json,evidence_json,as_of_date,
                          ROW_NUMBER() OVER(PARTITION BY industry_name ORDER BY as_of_date DESC) rn
                   FROM dc_industry_state_snapshot WHERE as_of_date<=?
               ) ranked WHERE rn=1""", (dates[-1],),
        ).fetchall()]
        latest_companies = [dict(row) for row in c.execute(
            """SELECT ts_code,name,industry,change_pct,turnover,total_mv,pe_ttm,pb
               FROM dc_stock_daily_snapshot WHERE trade_date=? AND COALESCE(industry,'')<>''""",
            (dates[-1],),
        ).fetchall()]

        by_industry: dict[str, list[dict[str, Any]]] = {}
        for row in aggregates:
            by_industry.setdefault(str(row.get("industry") or ""), []).append(row)
        companies_by_industry: dict[str, list[dict[str, Any]]] = {}
        for row in latest_companies:
            companies_by_industry.setdefault(str(row.get("industry") or ""), []).append(row)
        states: dict[str, dict[str, Any]] = {}
        for row in state_rows:
            try:
                row["state"] = json.loads(row.get("state_json") or "{}")
                row["evidence"] = json.loads(row.get("evidence_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                row["state"], row["evidence"] = {}, {}
            states[str(row.get("industry_name") or "")] = row

        candidates: list[dict[str, Any]] = []
        raw_by_name: dict[str, dict[str, Any]] = {}
        for industry, sessions in by_industry.items():
            companies = companies_by_industry.get(industry) or []
            if len(sessions) < 3 or len(companies) < 5:
                continue
            changes = [float(item.get("average_change_pct") or 0) / 100.0 for item in sessions]
            breadth_values = [float(item.get("breadth") or 0) for item in sessions]
            turnovers = [float(item.get("turnover") or 0) for item in sessions]
            recent_n = min(5, len(changes))
            momentum = sum(changes[-recent_n:])
            prior = sum(changes[:-recent_n]) / max(1, len(changes) - recent_n)
            price_confirmation = _clip(.5 + (momentum - prior * recent_n) * 5)
            breadth = _clip(sum(breadth_values[-recent_n:]) / recent_n)
            persistence = _clip(sum(1 for value in changes[-recent_n:] if value > 0) / recent_n)
            average_turnover = sum(turnovers[:-recent_n]) / max(1, len(turnovers) - recent_n) if len(turnovers) > recent_n else sum(turnovers) / len(turnovers)
            liquidity = _clip((sum(turnovers[-recent_n:]) / recent_n) / max(average_turnover, 1e-9) / 2)
            company_turnover = sorted((float(item.get("turnover") or 0) for item in companies), reverse=True)
            crowding = _clip(sum(company_turnover[:max(1, len(company_turnover) // 10)]) / max(sum(company_turnover), 1e-9))
            state = (states.get(industry) or {}).get("state") or {}
            fundamental = _clip(float(state.get("score") or 0) / 100.0) if state.get("status") == "ready" else 0.0
            # A tradable short-term narrative is independently confirmed by
            # price, breadth and capital persistence. Fundamentals are useful
            # corroboration, but are not a prerequisite for a new story.
            narrative_strength = _clip((price_confirmation + breadth + persistence + liquidity) / 4)
            source_types = int(price_confirmation >= .55) + int(breadth >= .5) + int((persistence + liquidity) / 2 >= .5) + int(fundamental > 0)
            item = {
                "theme": f"{industry}景气",
                "narrative_strength": narrative_strength,
                "price_confirmation": price_confirmation,
                "breadth": breadth,
                "capital_persistence": (persistence + liquidity) / 2,
                "fundamental_verifiability": fundamental,
                "catalyst_duration": persistence,
                "source_policy_credibility": 1.0 if source_types >= 3 else .75 if source_types >= 2 else .5,
                "crowding": crowding,
                "source_types": source_types,
                "related_instruments": len(companies),
                "sessions": len(sessions),
            }
            candidates.append(item)
            raw_by_name[item["theme"]] = {"industry": industry, "companies": companies, "momentum": momentum, "breadth": breadth, "crowding": crowding, "state": state}

        scored = score_theme_narratives(candidates)
        now = datetime.now().isoformat(timespec="seconds")
        written = mappings = 0
        for item in scored:
            raw = raw_by_name[item["theme"]]
            item["lifecycle"] = _lifecycle(
                score=float(item["score"]),
                base_score=float(item.get("base_score") or 0.0),
                momentum=float(raw["momentum"]),
                breadth=float(raw["breadth"]),
                crowding=float(raw["crowding"]),
                price_confirmation=float(
                    (item.get("components") or {}).get("price_confirmation")
                    or 0.0
                ),
            )
            item["lifecycle_contract"] = {
                "schema_version": "alis_narrative_lifecycle_observable_v2",
                "states": list(NARRATIVE_LIFECYCLE_STATES),
                "allowed_transitions": {
                    state: list(destinations)
                    for state, destinations
                    in NARRATIVE_LIFECYCLE_ALLOWED_TRANSITIONS.items()
                },
                "observations": {
                    "score_after_crowding_penalty": round(float(item["score"]), 8),
                    "base_score_before_crowding_penalty": round(
                        float(item.get("base_score") or 0.0),
                        8,
                    ),
                    "momentum_5_session": round(float(raw["momentum"]), 8),
                    "breadth_5_session": round(float(raw["breadth"]), 8),
                    "turnover_top_decile_concentration": round(
                        float(raw["crowding"]),
                        8,
                    ),
                },
            }
            # A confirmed score is still not buyable once price has rolled
            # over; this explicitly retires old stories such as a faded CPO.
            item["promotable"] = bool(
                item["promotable"] and item["lifecycle"] != "衰退"
            )
            item["state"] = (
                "confirmed"
                if item["promotable"]
                else "fading"
                if item["lifecycle"] == "衰退"
                else "watch"
            )
            item["knowledge_graph"] = {"theme": item["theme"], "industry": raw["industry"], "company_count": len(raw["companies"]), "evidence_types": ["price_volume"] + (["company_financial_aggregate"] if raw["state"].get("status") == "ready" else [])}
            theme_code = f"industry:{raw['industry']}"
            c.execute(
                """INSERT INTO dc_theme_snapshot
                   (as_of_date,theme_code,theme_name,lifecycle,score,promotable,payload_json,model_version,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(as_of_date,theme_code) DO UPDATE SET
                   theme_name=excluded.theme_name,lifecycle=excluded.lifecycle,score=excluded.score,
                   promotable=excluded.promotable,payload_json=excluded.payload_json,
                   model_version=excluded.model_version,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (dates[-1], theme_code, item["theme"], item["lifecycle"], item["score"], 1 if item["promotable"] else 0,
                 json.dumps(item, ensure_ascii=False), "industry_price_fundamental_theme_v2", cutoff or now, now, now),
            )
            written += 1
            max_turnover = max((float(company.get("turnover") or 0) for company in raw["companies"]), default=1.0)
            for company in sorted(raw["companies"], key=lambda value: float(value.get("turnover") or 0), reverse=True)[:50]:
                leadership = _clip(float(company.get("turnover") or 0) / max(max_turnover, 1e-9))
                exposure = round((.6 + .4 * leadership) * 100, 4)
                evidence = {"industry_membership": raw["industry"], "price_leadership": leadership, "trade_date": dates[-1], "source": "dc_stock_daily_snapshot"}
                c.execute(
                    """INSERT INTO dc_theme_company_mapping(as_of_date,theme_code,ts_code,exposure_score,evidence_json,updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(as_of_date,theme_code,ts_code) DO UPDATE SET
                       exposure_score=excluded.exposure_score,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                    (dates[-1], theme_code, company.get("ts_code"), exposure, json.dumps(evidence, ensure_ascii=False), now),
                )
                mappings += 1
    return {"ok": True, "as_of_date": dates[-1], "themes_written": written, "company_mappings_written": mappings, "promotable": sum(1 for item in scored if item["promotable"]), "model_version": "industry_price_fundamental_theme_v2"}


def materialize_theme_snapshot_history(*, end_date: str = "", target_sessions: int = 250, limit_dates: int = 20) -> dict[str, Any]:
    target = max(1, min(int(target_sessions or 250), 2400))
    limit = max(1, min(int(limit_dates or 20), 250))
    cutoff = str(end_date or "").replace("-", "")[:8]
    with _conn() as c:
        if not cutoff:
            row = c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone()
            cutoff = str(dict(row).get("value") or "") if row else ""
        usable_row = c.execute(
            """
            SELECT trade_date value
            FROM dc_stock_daily_snapshot
            WHERE trade_date<=?
            GROUP BY trade_date
            HAVING SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) > 0
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        usable_cutoff = str(dict(usable_row).get("value") or cutoff) if usable_row else cutoff
        rows = c.execute(
            """
            SELECT cal_date FROM dc_trade_calendar
            WHERE is_open=1 AND cal_date<=?
            ORDER BY cal_date DESC LIMIT ?
            """,
            (usable_cutoff, target),
        ).fetchall()
        target_dates = [str(dict(row).get("cal_date") or "") for row in rows if dict(row).get("cal_date")]
        placeholders = ",".join("?" for _ in target_dates)
        existing_rows = c.execute(
            f"""SELECT as_of_date,payload_json,model_version
                FROM dc_theme_snapshot
                WHERE as_of_date IN ({placeholders})
                  AND LEFT(theme_code,9)='industry:'""",
            tuple(target_dates),
        ).fetchall() if target_dates else []
        existing = {
            str(dict(row).get("as_of_date") or "")
            for row in existing_rows
        }
        quality_by_date: dict[str, list[bool]] = {}
        for row in existing_rows:
            item = dict(row)
            payload = item.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
            components = (
                payload.get("components")
                if isinstance(payload, dict)
                and isinstance(payload.get("components"), dict)
                else {}
            )
            evidence = (
                payload.get("evidence")
                if isinstance(payload, dict)
                and isinstance(payload.get("evidence"), dict)
                else {}
            )
            degenerate = (
                float(components.get("breadth") or 0.0) == 0.0
                and float(components.get("capital_persistence") or 0.0) == 0.0
                and int(evidence.get("source_types") or 0) == 0
            )
            quality_by_date.setdefault(
                str(item.get("as_of_date") or ""),
                [],
            ).append(degenerate)
    missing = sorted(
        trade_date for trade_date in target_dates if trade_date not in existing
    )
    invalid = sorted(
        trade_date
        for trade_date, flags in quality_by_date.items()
        if flags and sum(flags) / len(flags) >= .80
    )
    pending = sorted(set(missing) | set(invalid))
    batch = pending[:limit]
    results = []
    for trade_date in batch:
        try:
            results.append(materialize_theme_snapshots(as_of_time=f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T23:59:59", lookback=60))
        except Exception as exc:
            results.append({"ok": False, "as_of_date": trade_date, "error": str(exc)[:300]})
    written_dates = [str(item.get("as_of_date") or "") for item in results if item.get("ok")]
    return {
        "ok": all(bool(item.get("ok")) for item in results) if results else True,
        "target_sessions": target,
        "requested_end_date": cutoff,
        "effective_end_date": usable_cutoff,
        "existing_sessions": len(existing),
        "missing_sessions": len(missing),
        "invalid_sessions": len(invalid),
        "invalid_dates": invalid[:20],
        "processed_sessions": len(batch),
        "written_sessions": len(written_dates),
        "written_dates": written_dates,
        "remaining_sessions": max(0, len(pending) - len(written_dates)),
        "results": results[:10],
    }


def list_themes(*, as_of_time: str | None = None, promotable_only: bool = False, limit: int = 100) -> dict[str, Any]:
    cutoff = str(as_of_time or "")[:10].replace("-", "")
    with _conn() as c:
        clauses = []
        params: list[Any] = []
        if cutoff:
            clauses.append("as_of_date<=?"); params.append(cutoff)
        if promotable_only:
            clauses.append("promotable=1")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = [dict(row) for row in c.execute(
            f"""SELECT * FROM (
                   SELECT s.*,ROW_NUMBER() OVER(PARTITION BY theme_code ORDER BY as_of_date DESC) rn
                   FROM dc_theme_snapshot s {where}
               ) ranked WHERE rn=1 ORDER BY promotable DESC,score DESC LIMIT ?""", tuple(params + [max(1, min(int(limit), 500))]),
        ).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
            mappings = [dict(item) for item in c.execute(
                """SELECT ts_code,exposure_score,evidence_json FROM dc_theme_company_mapping
                   WHERE as_of_date=? AND theme_code=? ORDER BY exposure_score DESC LIMIT 50""",
                (row["as_of_date"], row["theme_code"]),
            ).fetchall()]
            for mapping in mappings:
                mapping["evidence"] = json.loads(mapping.pop("evidence_json") or "{}")
            row["companies"] = mappings
    return {"ok": True, "items": rows, "count": len(rows), "as_of_time": as_of_time}


def best_theme_for_stock(ts_code: str, *, as_of_time: str | None = None) -> dict[str, Any] | None:
    code = str(ts_code or "").strip().upper()
    cutoff = str(as_of_time or "")[:10].replace("-", "")
    with _conn() as c:
        clauses = ["m.ts_code=?", "s.promotable=1"]
        params: list[Any] = [code]
        if cutoff:
            clauses.append("m.as_of_date<=?"); params.append(cutoff)
        row = c.execute(
            f"""SELECT s.*,m.exposure_score,m.evidence_json FROM dc_theme_company_mapping m
                JOIN dc_theme_snapshot s ON s.as_of_date=m.as_of_date AND s.theme_code=m.theme_code
                WHERE {' AND '.join(clauses)} ORDER BY m.as_of_date DESC,s.score DESC LIMIT 1""", tuple(params),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
    return item
