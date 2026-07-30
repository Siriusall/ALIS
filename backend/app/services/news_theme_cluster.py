"""
新闻主题聚类引擎

从 major_news API 数据中提取关键词、聚类主题，映射到公司。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from collections import Counter

from app.data_platform.db import _conn


# 行业关键词映射
INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "新能源": ["新能源", "光伏", "风电", "锂电池", "宁德时代", "隆基", "比亚迪", "新能源车"],
    "人工智能": ["AI", "人工智能", "大模型", "ChatGPT", "智能", "算法", "算力", "机器人"],
    "半导体": ["芯片", "半导体", "集成电路", "光刻", "封测", "中芯国际"],
    "医药": ["医药", "创新药", "CXO", "恒瑞", "药明", "生物", "医疗", "疫苗"],
    "消费": ["消费", "白酒", "食品", "饮料", "茅台", "五粮液", "家电", "零售"],
    "金融": ["银行", "证券", "保险", "券商", "金融", "信贷", "利率", "降息"],
    "房地产": ["房地产", "地产", "楼市", "购房", "房贷", "万科", "保利"],
    "汽车": ["汽车", "新能源车", "特斯拉", "比亚迪", "自动驾驶", "零部件"],
    "通信": ["5G", "通信", "华为", "中兴", "光通信", "6G"],
    "军工": ["军工", "国防", "航天", "航空", "船舶", "装备"],
    "周期": ["有色", "钢铁", "煤炭", "化工", "石油", "大宗商品", "原材料"],
    "政策": ["政策", "国务院", "央行", "证监会", "财政", "减税", "改革"],
    "数字经济": ["数字经济", "数据要素", "云计算", "信创", "软件", "数字化"],
    "光伏": ["光伏", "太阳能", "逆变器", "硅料", "组件"],
    "储能": ["储能", "钠离子", "氢能", "电池"],
}

# 股票代码到公司简称的映射（从dc_stock_master获取）
_STOCK_NAMES: dict[str, str] = {}
_STOCK_CODE_LOOKUP: dict[str, tuple[str, str]] = {}
_STOCK_NAME_LOOKUP: dict[str, tuple[str, str]] = {}
_STOCK_NAME_PATTERN: re.Pattern[str] | None = None


def _load_stock_names() -> dict[str, str]:
    global _STOCK_NAMES, _STOCK_CODE_LOOKUP, _STOCK_NAME_LOOKUP, _STOCK_NAME_PATTERN
    if not _STOCK_NAMES:
        with _conn() as c:
            rows = [dict(row) for row in c.execute(
                "SELECT ts_code, name FROM dc_stock_master"
            ).fetchall()]
            _STOCK_NAMES = {str(r.get("ts_code", "")): str(r.get("name", "")) for r in rows}
        _STOCK_CODE_LOOKUP = {
            code.split(".")[0]: (code, name)
            for code, name in _STOCK_NAMES.items()
        }
        _STOCK_NAME_LOOKUP = {
            name: (code, name)
            for code, name in _STOCK_NAMES.items()
            if len(name) >= 3
        }
        names = sorted(_STOCK_NAME_LOOKUP, key=len, reverse=True)
        _STOCK_NAME_PATTERN = re.compile("|".join(re.escape(name) for name in names)) if names else None
    return _STOCK_NAMES


def _extract_keywords(text: str) -> list[str]:
    """Extract Chinese keywords from text."""
    if not text:
        return []
    # Remove English punctuation, keep Chinese chars
    text = re.sub(r'[a-zA-Z0-9_@#$%^&*()\[\]{}<>:;"\'\\|,./?~`！@#￥%……&*（）——+【】；：""''，。？、]', ' ', text)
    # Simple keyword matching against industry keywords
    found = set()
    for industry, keywords in INDUSTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                found.add(industry)
                break
    return list(found)


def _match_companies(text: str) -> list[dict[str, str]]:
    """Match company names/stock codes in text."""
    names = _load_stock_names()
    code_lookup = _STOCK_CODE_LOOKUP or {
        code.split(".")[0]: (code, name)
        for code, name in names.items()
    }
    name_lookup = _STOCK_NAME_LOOKUP or {
        name: (code, name)
        for code, name in names.items()
        if len(name) >= 3
    }
    name_pattern = _STOCK_NAME_PATTERN
    if name_pattern is None and name_lookup:
        name_pattern = re.compile(
            "|".join(re.escape(name) for name in sorted(name_lookup, key=len, reverse=True))
        )
    matched = []
    seen: set[str] = set()
    # Long-form news commonly embeds six-digit A-share codes in links. Reading
    # those codes avoids scanning every one of ~5,500 company names through
    # every full article during historical reconstruction.
    for raw_code in re.findall(r"(?<!\d)([036]\d{5})(?!\d)", text):
        item = code_lookup.get(raw_code)
        if not item or item[0] in seen:
            continue
        seen.add(item[0])
        matched.append({"ts_code": item[0], "name": item[1]})
        if len(matched) >= 10:
            break
    # Titles without embedded codes still get a bounded exact-name fallback.
    if not matched:
        title_text = text[:300]
        for name in dict.fromkeys(
            match.group(0)
            for match in (name_pattern.finditer(title_text) if name_pattern else [])
        ):
            code, canonical_name = name_lookup[name]
            matched.append({"ts_code": code, "name": canonical_name})
            if len(matched) >= 10:
                break
    return matched


def cluster_news_themes(*, as_of_time: str | None = None) -> dict[str, Any]:
    """Cluster news articles into themes and map to companies."""
    cutoff = str(as_of_time or "")[:10].replace("-", "")
    cutoff_iso = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T23:59:59"
        if len(cutoff) == 8 else ""
    )
    # major_news is an event dataset: its generic trade_date column is empty.
    # Point-in-time filtering must use the publisher timestamp and the raw
    # record's available_time, otherwise every historical article is silently
    # excluded from reconstruction.
    cutoff_clause = """
      AND COALESCE(NULLIF(payload_json::jsonb->>'pub_time',''),available_time)<=?
      AND available_time<=?
    """ if cutoff_iso else ""
    params: tuple[Any, ...] = (cutoff_iso, cutoff_iso) if cutoff_iso else ()

    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT payload_json,
                       REPLACE(SUBSTRING(
                         COALESCE(NULLIF(payload_json::jsonb->>'pub_time',''),available_time),
                         1,10
                       ),'-','') AS trade_date,
                       ts_code,id
                FROM dc_tushare_raw_records
                WHERE api_name='major_news' {cutoff_clause}
                ORDER BY COALESCE(NULLIF(payload_json::jsonb->>'pub_time',''),available_time) DESC
                LIMIT 2000""", params
        ).fetchall()]

    if not rows:
        return {"ok": False, "reason": "no_news_data", "count": 0}

    # Parse news articles
    articles = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        title = str(payload.get("title", "") or "")
        content = str(payload.get("content", "") or "")
        text = title + " " + content
        if len(text) < 10:
            continue
        articles.append({
            "title": title,
            "text": text,
            "trade_date": row.get("trade_date", ""),
            "keywords": _extract_keywords(text),
            "companies": _match_companies(text),
        })

    # Cluster by keyword overlap
    theme_articles: dict[str, list[dict]] = {}
    for article in articles:
        for kw in article["keywords"]:
            theme_articles.setdefault(kw, []).append(article)

    # Score each theme
    now = datetime.now().isoformat(timespec="seconds")
    themes_written = 0
    company_mappings = 0
    as_of_date = cutoff or datetime.now().strftime("%Y%m%d")

    with _conn() as c:
        for theme_name, articles_list in sorted(theme_articles.items(), key=lambda x: len(x[1]), reverse=True):
            if len(articles_list) < 2:
                continue
            theme_code = f"news:{theme_name}"
            all_companies: list[dict] = []
            for article in articles_list:
                all_companies.extend(article.get("companies", []))
            unique_companies = {c["ts_code"]: c for c in all_companies}.values()
            score = min(100, len(articles_list) * 5 + len(unique_companies) * 3)
            promotable = 1 if score >= 20 and len(unique_companies) >= 3 else 0

            payload = {
                "theme": theme_name,
                "source": "news_cluster_v1",
                "article_count": len(articles_list),
                "company_count": len(unique_companies),
                "score": score,
                "latest_articles": [a["title"][:60] for a in articles_list[:5]],
            }

            c.execute(
                """INSERT INTO dc_theme_snapshot
                   (as_of_date, theme_code, theme_name, lifecycle, score, promotable, payload_json, model_version, available_time, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(as_of_date, theme_code) DO UPDATE SET
                   theme_name=excluded.theme_name, score=excluded.score, promotable=excluded.promotable,
                   payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                (as_of_date, theme_code, theme_name, "扩散", score, promotable,
                 json.dumps(payload, ensure_ascii=False), "news_cluster_v1", cutoff or now, now, now),
            )
            themes_written += 1

            for company in unique_companies:
                c.execute(
                    """INSERT INTO dc_theme_company_mapping(as_of_date, theme_code, ts_code, exposure_score, evidence_json, updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(as_of_date, theme_code, ts_code) DO UPDATE SET
                       exposure_score=excluded.exposure_score, evidence_json=excluded.evidence_json, updated_at=excluded.updated_at""",
                    (as_of_date, theme_code, company["ts_code"],
                     round(70.0 + 30.0 * (1.0 / max(1, len(unique_companies))), 2),
                     json.dumps({"source": "news_mention", "article_count": len(articles_list), "trade_date": as_of_date}, ensure_ascii=False),
                     now),
                )
                company_mappings += 1

    return {
        "ok": True,
        "as_of_date": as_of_date,
        "total_articles": len(articles),
        "themes_found": themes_written,
        "company_mappings": company_mappings,
        "model_version": "news_cluster_v1",
    }
