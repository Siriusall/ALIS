"""Point-in-time theme revenue exposure from retained business segments."""

from __future__ import annotations

import json
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


def _sales(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _matching_themes(item: str) -> list[str]:
    text = str(item or "").lower()
    return [name for name, keywords in INDUSTRY_KEYWORDS.items() if any(str(keyword).lower() in text for keyword in keywords)]


def materialize_theme_revenue_exposure(*, as_of_time: str | None = None, limit_companies: int = 8_000) -> dict[str, Any]:
    """Persist revenue-share evidence using only business segments visible at cutoff."""
    _ensure_tables()
    cutoff = str(as_of_time or "")
    cutoff_date = cutoff[:10].replace("-", "")
    with _conn() as conn:
        if not cutoff_date:
            row = conn.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone()
            cutoff_date = str(dict(row).get("value") or "") if row else ""
        clause = "WHERE available_time<=?" if cutoff else ""
        params: tuple[Any, ...] = (cutoff,) if cutoff else ()
        rows = [dict(row) for row in conn.execute(
            f"""WITH eligible AS (
                    SELECT ts_code,period,available_time,payload_json,
                           ROW_NUMBER() OVER(PARTITION BY ts_code ORDER BY period DESC,available_time DESC,id DESC) AS company_rank
                    FROM dc_tushare_raw_records WHERE api_name='fina_mainbz' {('AND available_time<=?' if cutoff else '')}
                ), latest_period AS (
                    SELECT ts_code,MAX(period) period FROM eligible GROUP BY ts_code
                ) SELECT e.ts_code,e.period,e.available_time,e.payload_json
                  FROM eligible e JOIN latest_period p ON p.ts_code=e.ts_code AND p.period=e.period
                 ORDER BY e.ts_code,e.available_time DESC LIMIT ?""",
            params + (max(1, min(int(limit_companies), 12_000)) * 30,),
        ).fetchall()]
    # Limit by companies after fetching their full latest-period segments.
    by_company: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("ts_code") or "")
        if code and (code in by_company or len(by_company) < max(1, min(int(limit_companies), 8_000))):
            by_company.setdefault(code, []).append(row)
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    with _conn() as conn:
        for code, segments in by_company.items():
            parsed: list[dict[str, Any]] = []
            for row in segments:
                try:
                    payload = json.loads(row.get("payload_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                parsed.append({"item": str(payload.get("bz_item") or ""), "sales": _sales(payload.get("bz_sales")), "period": str(row.get("period") or ""), "available_time": str(row.get("available_time") or "")})
            total = sum(item["sales"] for item in parsed)
            if total <= 0:
                continue
            for theme in INDUSTRY_KEYWORDS:
                matched = [item for item in parsed if theme in _matching_themes(item["item"])]
                matched_sales = sum(item["sales"] for item in matched)
                if matched_sales <= 0:
                    continue
                source = max(parsed, key=lambda item: (item["period"], item["available_time"]))
                evidence = {"method": "fina_mainbz_business_segment_keyword_v1", "matched_items": [item["item"] for item in matched][:20], "segment_count": len(parsed), "matched_segment_count": len(matched), "limitation": "主营业务分部可能包含产品、地区或客户口径，按公开原始项目保留证据，不与新闻关联度混淆。"}
                conn.execute(
                    """INSERT INTO dc_theme_revenue_exposure
                       (as_of_date,theme_code,ts_code,revenue_share,matched_sales,total_segment_sales,source_period,source_available_time,evidence_json,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(as_of_date,theme_code,ts_code) DO UPDATE SET
                       revenue_share=excluded.revenue_share,matched_sales=excluded.matched_sales,total_segment_sales=excluded.total_segment_sales,
                       source_period=excluded.source_period,source_available_time=excluded.source_available_time,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                    (cutoff_date, f"revenue:{theme}", code, round(matched_sales / total, 8), matched_sales, total, source["period"], source["available_time"], json.dumps(evidence, ensure_ascii=False), now),
                )
                written += 1
    return {"ok": True, "schema_version": "alis_theme_revenue_exposure_v1", "as_of_date": cutoff_date, "companies_examined": len(by_company), "exposures_written": written, "policy": "point_in_time_fina_mainbz_only; revenue_share_is_not_news_or_price_exposure"}
