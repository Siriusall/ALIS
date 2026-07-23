"""Official public policy and supply-document ingestion.

Government and regulator pages are intentionally treated as document sources,
not as a fabricated numeric feed.  A policy score is only materialised after
the original public URL, published time and matching industry evidence have
been retained.  Failed public sites remain visible in the sync result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from app.data_platform.db import _conn
from app.data_platform.services.data_center import _now, _upsert_industry_metric_definitions, _upsert_industry_metric_values


OFFICIAL_POLICY_SOURCE = "official_policy"
HTTP_TIMEOUT = 20
MAX_DOCUMENTS_PER_SOURCE = 80


@dataclass(frozen=True)
class OfficialSource:
    code: str
    name: str
    index_url: str


# The pages are public index pages rather than search-engine results.  Their
# layout can change; parser failures are reported and never converted to an
# empty policy conclusion.
OFFICIAL_SOURCES = (
    OfficialSource("official.ndrc", "国家发展改革委", "https://www.ndrc.gov.cn/xxgk/zcfb/tz/"),
    OfficialSource("official.miit", "工业和信息化部", "https://www.miit.gov.cn/zwgk/zcwj/wjfb/index.html"),
    OfficialSource("official.nea", "国家能源局", "https://www.nea.gov.cn/zwgk/zfxxgkzl/zfxxgkml/index.html"),
    OfficialSource("official.customs", "海关总署", "http://www.customs.gov.cn/customs/xwfb34/302425/"),
    OfficialSource("official.stats", "国家统计局", "https://www.stats.gov.cn/sj/zxfb/"),
    OfficialSource("association.caam", "中国汽车工业协会", "http://www.caam.org.cn/"),
    OfficialSource("association.cncma", "中国工程机械工业协会", "https://www.cncma.org/"),
    OfficialSource("association.cisa", "中国钢铁工业协会", "http://www.chinaisa.org.cn/"),
)


POLICY_TERMS = ("政策", "通知", "办法", "规划", "方案", "意见", "产能", "产量", "供给", "保供", "开工", "利用率", "进口", "出口", "销量", "库存")
SUPPORT_TERMS = ("支持", "促进", "鼓励", "扩大", "下达", "投产", "保供", "增产", "增长", "恢复")
RESTRICT_TERMS = ("限制", "整治", "淘汰", "压减", "停产", "减产", "风险", "下滑", "下降", "收紧")

# Names align to the system's current industry master.  A policy may map to
# more than one industry, but only when its retained text contains a concrete
# matching keyword.
INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "煤炭开采": ("煤炭", "焦煤", "焦炭"),
    "石油开采": ("原油", "石油", "天然气"),
    "化工原料": ("化工", "化肥", "乙烯", "聚丙烯"),
    "普钢": ("钢铁", "粗钢", "螺纹钢"),
    "有色": ("有色金属", "铜", "铝", "锂"),
    "电气设备": ("电力设备", "新能源", "光伏", "锂电", "储能"),
    "汽车整车": ("汽车", "乘用车", "新能源汽车"),
    "工程机械": ("工程机械", "挖掘机", "装载机"),
    "半导体": ("半导体", "芯片", "集成电路"),
    "家用电器": ("家电", "空调", "冰箱", "洗衣机"),
}


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            title = re.sub(r"\s+", " ", unescape("".join(self._parts))).strip()
            if title:
                self.links.append((self._href, title))
            self._href = ""
            self._parts = []


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _published_time(text: str) -> str:
    match = re.search(r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})", text)
    if not match:
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}T00:00:00"


def _source_links(source: OfficialSource, html: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(html)
    source_host = urlparse(source.index_url).netloc
    candidates: list[tuple[str, str]] = []
    for href, title in parser.links:
        url = urljoin(source.index_url, href)
        if urlparse(url).netloc != source_host or url == source.index_url:
            continue
        if len(title) < 8 or not any(term in title for term in POLICY_TERMS):
            continue
        candidates.append((url, title))
    # Stable deduplication matters because government pages commonly repeat
    # the same link in desktop/mobile navigation.
    return list(dict.fromkeys(candidates))[:MAX_DOCUMENTS_PER_SOURCE]


def _request_text(url: str) -> str:
    session = requests.Session()
    # A stale desktop proxy must not turn an official public source into an
    # artificial outage.  TLS verification remains enabled.
    session.trust_env = False
    response = session.get(
        url,
        headers={"User-Agent": "ALIS/1.0 public-policy research collector"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def _save_documents(rows: list[dict[str, str]]) -> int:
    if not rows:
        return 0
    now = _now()
    with _conn() as conn:
        for row in rows:
            conn.execute(
                """INSERT INTO dc_announcement_raw
                   (source_code,announcement_id,ts_code,title,publish_time,url,raw_text,file_path,content_hash,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_code,announcement_id) DO UPDATE SET
                    title=excluded.title,publish_time=excluded.publish_time,url=excluded.url,raw_text=excluded.raw_text,
                    content_hash=excluded.content_hash,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (row["source_code"], row["announcement_id"], "", row["title"], row["publish_time"], row["url"],
                 row["raw_text"], "", row["content_hash"], row["available_time"], now, now),
            )
    return len(rows)


def sync_official_policy_documents(*, sources: tuple[OfficialSource, ...] = OFFICIAL_SOURCES) -> dict[str, Any]:
    """Fetch public official directories and retain policy/supply documents.

    Source failure is recorded per source and returned as `partial`; it is not
    interpreted as a no-policy period.  This lets daily autonomy continue
    without pretending a blocked public web site provided a valid empty set.
    """
    retained: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    now = _now()
    for source in sources:
        try:
            index_html = _request_text(source.index_url)
            links = _source_links(source, index_html)
            documents = 0
            for url, fallback_title in links:
                try:
                    html = _request_text(url)
                except requests.RequestException:
                    continue
                text = _text(html)
                title = fallback_title
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if title_match:
                    title = re.sub(r"\s+", " ", unescape(title_match.group(1))).strip() or title
                body = f"{title} {text[:12000]}"
                if not any(term in body for term in POLICY_TERMS):
                    continue
                retained.append({
                    "source_code": source.code,
                    "announcement_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
                    "title": title[:1000], "publish_time": _published_time(body), "url": url,
                    "raw_text": text[:12000], "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "available_time": now,
                })
                documents += 1
            results.append({"source": source.code, "status": "success", "links": len(links), "documents": documents})
        except requests.RequestException as exc:
            results.append({"source": source.code, "status": "failed", "error": str(exc)[:240]})
    written = _save_documents(retained)
    failed = [item["source"] for item in results if item["status"] == "failed"]
    return {"ok": True, "status": "partial" if failed else "ready", "documents_written": written,
            "sources": results, "failed_sources": failed, "contract": "official_public_policy_documents_v1"}


def _metric_code(industry: str) -> str:
    return f"official_policy_supply:{hashlib.sha256(industry.encode('utf-8')).hexdigest()[:12]}"


def _direction_score(text: str) -> float | None:
    support = sum(text.count(term) for term in SUPPORT_TERMS)
    restrict = sum(text.count(term) for term in RESTRICT_TERMS)
    if support == restrict == 0:
        return None
    return 75.0 if support > restrict else 25.0 if restrict > support else 50.0


def materialize_official_policy_supply() -> dict[str, Any]:
    """Turn retained official documents into transparent industry policy events."""
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT source_code,announcement_id,title,publish_time,url,raw_text,available_time
                 FROM dc_announcement_raw WHERE source_code LIKE ? OR source_code LIKE ?
                 ORDER BY publish_time,available_time""",
            ("official.%", "association.%"),
        ).fetchall()]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        body = f"{row.get('title') or ''} {row.get('raw_text') or ''}"
        score = _direction_score(body)
        publish = str(row.get("publish_time") or "")
        if score is None or len(publish) < 10:
            continue
        for industry, terms in INDUSTRY_KEYWORDS.items():
            matched = [term for term in terms if term in body]
            if matched:
                grouped.setdefault((industry, publish[:10]), []).append({
                    "score": score, "source_code": row["source_code"], "announcement_id": row["announcement_id"],
                    "url": row["url"], "title": row["title"], "matched_terms": matched,
                    "available_time": row["available_time"],
                })
    definitions: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    for (industry, data_date), evidence in grouped.items():
        code = _metric_code(industry)
        definitions.append({"metric_code": code, "industry_code": f"master:{industry}", "industry_name": industry,
                            "metric_name": "行业政策与供给（官方公开文件）", "unit": "score", "frequency": "event",
                            "source_code": OFFICIAL_POLICY_SOURCE,
                            "description": "主管部门公开文件的行业政策/供给事件分数；保留原文链接与可用时间，不等同于执行效果或实际供给量。"})
        values.append({"metric_code": code, "data_date": data_date,
                       "value": sum(float(item["score"]) for item in evidence) / len(evidence),
                       "raw_value": json.dumps(evidence, ensure_ascii=False), "source_code": OFFICIAL_POLICY_SOURCE,
                       "available_time": max(str(item["available_time"] or "") for item in evidence)})
    return {"ok": True, "definitions": _upsert_industry_metric_definitions(definitions),
            "values": _upsert_industry_metric_values(values), "documents_examined": len(rows),
            "matched_industry_dates": len(grouped), "contract": "official_policy_supply_v1",
            "limitation": "事件分数只表达公开文本方向，不代表政策落地、实际产量或行业盈利。"}


def materialize_official_capacity_utilization() -> dict[str, Any]:
    """Extract national industrial capacity utilisation from retained NBS releases.

    The statistic is a broad manufacturing proxy mapped to manufacturing
    industries, not a claim that every mapped industry has its own utilisation
    survey.  The original NBS document remains in the evidence payload.
    """
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT announcement_id,title,publish_time,url,raw_text,available_time
                 FROM dc_announcement_raw WHERE source_code=? ORDER BY publish_time,available_time""",
            ("official.stats",),
        ).fetchall()]
        known = {str(row.get("industry") or "").strip() for row in conn.execute(
            "SELECT DISTINCT industry FROM dc_stock_master WHERE COALESCE(industry,'')<>''"
        ).fetchall()}
    manufacturing = sorted(known & {industry for industry in INDUSTRY_KEYWORDS if industry not in {"石油开采", "煤炭开采"}})
    measurements: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        text = f"{row.get('title') or ''} {row.get('raw_text') or ''}"
        if "产能利用率" not in text or len(str(row.get("publish_time") or "")) < 10:
            continue
        matches = re.findall(r"产能利用率[^。；;]{0,80}?(\d{1,3}(?:\.\d+)?)\s*%", text)
        for value in matches:
            number = float(value)
            if 0 < number <= 100:
                measurements.setdefault(str(row["publish_time"])[:10], []).append({
                    "value": number, "announcement_id": row["announcement_id"], "url": row["url"],
                    "title": row["title"], "available_time": row["available_time"],
                })
    definitions: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    for industry in manufacturing:
        code = f"official_stats_capacity_proxy:{hashlib.sha256(industry.encode('utf-8')).hexdigest()[:12]}"
        definitions.append({"metric_code": code, "industry_code": f"master:{industry}", "industry_name": industry,
                            "metric_name": "工业产能利用率（国家统计局宏观代理）", "unit": "percent", "frequency": "quarterly",
                            "source_code": "official.stats_capacity_proxy",
                            "description": "国家统计局工业产能利用率公开发布的宏观制造业代理，不等同于细分行业开工率。"})
        for data_date, evidence in measurements.items():
            values.append({"metric_code": code, "data_date": data_date,
                           "value": sum(item["value"] for item in evidence) / len(evidence),
                           "raw_value": json.dumps(evidence, ensure_ascii=False), "source_code": "official.stats_capacity_proxy",
                           "available_time": max(str(item["available_time"] or "") for item in evidence)})
    return {"ok": True, "definitions": _upsert_industry_metric_definitions(definitions),
            "values": _upsert_industry_metric_values(values), "documents_examined": len(rows),
            "release_dates": len(measurements), "contract": "official_stats_capacity_proxy_v1",
            "limitation": "国家统计局总量产能利用率仅作为宏观制造业代理，不能替代细分行业产能或开工率。"}
