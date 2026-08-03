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
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from app.data_platform.db import _conn
from app.data_platform.services.data_center import _now, _upsert_industry_metric_definitions, _upsert_industry_metric_values


OFFICIAL_POLICY_SOURCE = "official_policy"
HTTP_TIMEOUT = 20
MAX_DOCUMENTS_PER_SOURCE = 80
REQUEST_CONNECT_TIMEOUT = 3.0
REQUEST_READ_TIMEOUT = 8.0
SYNC_TIME_BUDGET_SECONDS = 90.0
MAX_NEW_DOCUMENTS_PER_SOURCE_PER_CYCLE = 3


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
POLICY_OR_SUPPLY_TITLE_TERMS = ("政策", "通知", "办法", "规划", "方案", "意见", "规定", "措施", "产能", "产量", "供给", "保供", "开工", "利用率", "库存")
NON_INVESTMENT_ASSOCIATION_TITLE_TERMS = ("征集", "参编", "委员", "召开", "培训", "征求意见", "会员单位")
SUPPORT_TERMS = ("支持", "促进", "鼓励", "推动", "推进", "加快", "提升", "扩大", "下达", "投产", "保供", "增产", "增长", "恢复")
RESTRICT_TERMS = ("限制", "严控", "禁止", "整治", "淘汰", "压减", "化解过剩", "过剩产能", "节能", "能耗", "停产", "减产", "风险", "下滑", "下降", "收紧")

# Names align to the system's current industry master.  A policy may map to
# more than one industry, but only when its retained text contains a concrete
# matching keyword.
INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "煤炭开采": ("煤炭", "焦煤", "焦炭"),
    "石油开采": ("原油", "油气开采", "石油开采", "天然气开采"),
    "化工原料": ("化工", "化肥", "乙烯", "聚丙烯"),
    "普钢": ("钢铁", "粗钢", "螺纹钢"),
    "有色": ("有色金属", "电解铜", "电解铝", "锂矿", "碳酸锂"),
    "电气设备": ("电力设备", "光伏", "锂电", "储能"),
    "汽车整车": ("汽车", "乘用车", "新能源汽车"),
    "工程机械": ("工程机械", "挖掘机", "装载机"),
    "半导体": ("半导体", "芯片", "集成电路"),
    "家用电器": ("家用电器", "家电", "房间空气调节器", "电冰箱", "洗衣机"),
}

# The NBS releases use GB/T 4754 categories while the application currently
# uses a compact research-industry master.  Every non-identical mapping is
# retained in metric metadata instead of being presented as an exact match.
NBS_CAPACITY_INDUSTRY_MAP: dict[str, tuple[str, str]] = {
    "煤炭开采和洗选业": ("煤炭开采", "exact_family"),
    "石油和天然气开采业": ("石油开采", "exact_family"),
    "化学原料和化学制品制造业": ("化工原料", "exact_family"),
    "黑色金属冶炼和压延加工业": ("普钢", "exact_family"),
    "有色金属冶炼和压延加工业": ("有色", "exact_family"),
    "专用设备制造业": ("工程机械", "broad_proxy"),
    "汽车制造业": ("汽车整车", "exact_family"),
    "电气机械和器材制造业": ("电气设备", "broad_proxy"),
    "计算机、通信和其他电子设备制造业": ("半导体", "broad_proxy"),
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
    match = re.search(r"发布时间\s*[:：]?\s*(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})", text)
    if not match:
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
        timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def _caam_article_text(title: str, html_or_text: str) -> str:
    """Remove CAAM navigation/latest-news text from an article.

    Older retained rows contain the text rendering of the whole page.  New
    fetches still pass HTML here.  Supporting both lets materialisation repair
    its evidence boundary without rewriting the immutable fetch timestamp.
    """
    if "<" in html_or_text and ">" in html_or_text:
        match = re.search(
            r'<div\s+class=["\']fourBox["\'][^>]*>(.*?)</div>\s*</div>\s*<!--\s*right',
            html_or_text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _text(match.group(1))
        rendered = _text(html_or_text)
    else:
        rendered = re.sub(r"\s+", " ", html_or_text).strip()
    breadcrumb = rendered.find("首页>")
    article_start = rendered.find("正文", breadcrumb if breadcrumb >= 0 else 0)
    title_start = rendered.find(title, article_start if article_start >= 0 else 0)
    if title_start >= 0:
        rendered = rendered[title_start + len(title):]
    rendered = re.sub(r"^\s*发布时间\s*[:：]\s*20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?", "", rendered)
    rendered = re.sub(r"^\s*来源\s*[:：]\s*\S{1,40}", "", rendered)
    boundaries = [rendered.find(marker) for marker in ("最新新闻", "友情链接") if rendered.find(marker) >= 0]
    if boundaries:
        rendered = rendered[:min(boundaries)]
    return rendered.strip()


def _document_text(source_code: str, title: str, html_or_text: str) -> str:
    if source_code == "association.caam":
        return _caam_article_text(title, html_or_text)
    return _text(html_or_text) if "<" in html_or_text and ">" in html_or_text else re.sub(r"\s+", " ", html_or_text).strip()


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
    started = monotonic()
    with _conn() as conn:
        known_ids = {
            str(row["announcement_id"])
            for row in conn.execute(
                "SELECT announcement_id FROM dc_announcement_raw WHERE source_code LIKE ? OR source_code LIKE ?",
                ("official.%", "association.%"),
            ).fetchall()
        }
    for source in sources:
        if monotonic() - started >= SYNC_TIME_BUDGET_SECONDS:
            results.append({"source": source.code, "status": "deferred_budget", "reason": "cycle_time_budget_exhausted"})
            continue
        try:
            index_html = _request_text(source.index_url)
            links = _source_links(source, index_html)
            documents = 0
            skipped_existing = 0
            attempted_new = 0
            for url, fallback_title in links:
                announcement_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
                if announcement_id in known_ids:
                    skipped_existing += 1
                    continue
                if attempted_new >= MAX_NEW_DOCUMENTS_PER_SOURCE_PER_CYCLE or monotonic() - started >= SYNC_TIME_BUDGET_SECONDS:
                    break
                attempted_new += 1
                try:
                    html = _request_text(url)
                except requests.RequestException:
                    continue
                title = fallback_title
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if title_match:
                    title = re.sub(r"\s+", " ", unescape(title_match.group(1))).strip() or title
                text = _document_text(source.code, title, html)
                body = f"{title} {text[:12000]}"
                if not any(term in body for term in POLICY_TERMS):
                    continue
                retained.append({
                    "source_code": source.code,
                    "announcement_id": announcement_id,
                    "title": title[:1000], "publish_time": _published_time(body), "url": url,
                    "raw_text": text[:12000], "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "available_time": now,
                })
                known_ids.add(announcement_id)
                documents += 1
            results.append({"source": source.code, "status": "success", "links": len(links), "documents": documents,
                            "attempted_new": attempted_new, "skipped_existing": skipped_existing})
        except requests.RequestException as exc:
            results.append({"source": source.code, "status": "failed", "error": str(exc)[:240]})
    written = _save_documents(retained)
    failed = [item["source"] for item in results if item["status"] == "failed"]
    deferred = [item["source"] for item in results if item["status"] == "deferred_budget"]
    return {"ok": True, "status": "partial" if failed or deferred else "ready", "documents_written": written,
            "sources": results, "failed_sources": failed, "deferred_sources": deferred,
            "elapsed_seconds": round(monotonic() - started, 3), "time_budget_seconds": SYNC_TIME_BUDGET_SECONDS,
            "contract": "official_public_policy_documents_v2"}


def _metric_code(industry: str) -> str:
    return f"official_policy_supply:{hashlib.sha256(industry.encode('utf-8')).hexdigest()[:12]}"


def _direction_score(text: str) -> float | None:
    support = sum(text.count(term) for term in SUPPORT_TERMS)
    restrict = sum(text.count(term) for term in RESTRICT_TERMS)
    if support == restrict == 0:
        return None
    return 75.0 if support > restrict else 25.0 if restrict > support else 50.0


def _industry_evidence(text: str, terms: tuple[str, ...]) -> tuple[float | None, list[str], str]:
    fragments = [fragment.strip() for fragment in re.split(r"[。！？；;，,：:\n]", text) if fragment.strip()]
    relevant = [fragment for fragment in fragments if any(term in fragment for term in terms)]
    evidence_text = "。".join(relevant)
    return _direction_score(evidence_text), [term for term in terms if term in evidence_text], evidence_text[:1200]


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
    seen_association_titles: set[tuple[str, str]] = set()
    for row in rows:
        title = str(row.get("title") or "")
        if not any(term in title for term in POLICY_OR_SUPPLY_TITLE_TERMS):
            continue
        if str(row.get("source_code") or "").startswith("association.") and any(
            term in title for term in NON_INVESTMENT_ASSOCIATION_TITLE_TERMS
        ):
            continue
        association_key = (str(row.get("source_code") or ""), title)
        if association_key[0].startswith("association."):
            if association_key in seen_association_titles:
                continue
            seen_association_titles.add(association_key)
        article_text = _document_text(str(row.get("source_code") or ""), title, str(row.get("raw_text") or ""))
        body = f"{title} {article_text}"
        publish = str(row.get("publish_time") or "")
        if len(publish) < 10:
            continue
        for industry, terms in INDUSTRY_KEYWORDS.items():
            score, matched, evidence_excerpt = _industry_evidence(body, terms)
            if score is not None and matched:
                grouped.setdefault((industry, publish[:10]), []).append({
                    "score": score, "source_code": row["source_code"], "announcement_id": row["announcement_id"],
                    "url": row["url"], "title": row["title"], "matched_terms": matched,
                    "evidence_excerpt": evidence_excerpt,
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
    with _conn() as conn:
        conn.execute("DELETE FROM dc_industry_metric_value WHERE source_code=?", (OFFICIAL_POLICY_SOURCE,))
    return {"ok": True, "definitions": _upsert_industry_metric_definitions(definitions),
            "values": _upsert_industry_metric_values(values), "documents_examined": len(rows),
            "matched_industry_dates": len(grouped), "contract": "official_policy_supply_v2",
            "limitation": "事件分数只表达公开文本方向，不代表政策落地、实际产量或行业盈利。"}


def _capacity_period_end(text: str) -> str:
    match = re.search(r"(20\d{2})年\s*([一二三四1-4])季度", text)
    if not match:
        return ""
    quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(match.group(2), int(match.group(2)) if match.group(2).isdigit() else 0)
    return {
        1: f"{match.group(1)}-03-31",
        2: f"{match.group(1)}-06-30",
        3: f"{match.group(1)}-09-30",
        4: f"{match.group(1)}-12-31",
    }.get(quarter, "")


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
    measurements: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        text = f"{row.get('title') or ''} {row.get('raw_text') or ''}"
        if "产能利用率" not in text or len(str(row.get("publish_time") or "")) < 10:
            continue
        period_end = _capacity_period_end(text)
        if not period_end:
            continue
        for official_name, (industry, mapping_quality) in NBS_CAPACITY_INDUSTRY_MAP.items():
            if industry not in known:
                continue
            match = re.search(rf"{re.escape(official_name)}(?:产能利用率)?\s*为\s*(\d{{1,3}}(?:\.\d+)?)\s*%", text)
            if not match:
                continue
            number = float(match.group(1))
            if 0 < number <= 100:
                measurements.setdefault((industry, period_end), []).append({
                    "value": number, "official_industry": official_name, "mapping_quality": mapping_quality,
                    "announcement_id": row["announcement_id"], "url": row["url"], "title": row["title"],
                    "publish_time": row["publish_time"], "available_time": row["available_time"],
                })
    definitions: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    configured_industries = sorted({industry for industry, _quality in NBS_CAPACITY_INDUSTRY_MAP.values()} & known)
    for industry in configured_industries:
        code = f"official_stats_capacity_industry:{hashlib.sha256(industry.encode('utf-8')).hexdigest()[:12]}"
        qualities = {quality for _official, (mapped, quality) in NBS_CAPACITY_INDUSTRY_MAP.items() if mapped == industry}
        mapping_label = "国家统计局同类行业" if qualities == {"exact_family"} else "国家统计局宽口径行业代理"
        definitions.append({"metric_code": code, "industry_code": f"master:{industry}", "industry_name": industry,
                            "metric_name": f"工业产能利用率（{mapping_label}）", "unit": "percent", "frequency": "quarterly",
                            "source_code": "official.stats_capacity_industry",
                            "description": "国家统计局季度公开发布的行业产能利用率；证据中保留官方行业名和映射质量，宽口径映射不等同于细分行业开工率。"})
        for (measurement_industry, data_date), evidence in measurements.items():
            if measurement_industry != industry:
                continue
            values.append({"metric_code": code, "data_date": data_date,
                           "value": sum(item["value"] for item in evidence) / len(evidence),
                           "raw_value": json.dumps(evidence, ensure_ascii=False), "source_code": "official.stats_capacity_industry",
                           "available_time": max(str(item["available_time"] or "") for item in evidence)})
    return {"ok": True, "definitions": _upsert_industry_metric_definitions(definitions),
            "values": _upsert_industry_metric_values(values), "documents_examined": len(rows),
            "release_dates": len({key[1] for key in measurements}), "industry_observations": len(measurements),
            "contract": "official_stats_capacity_industry_v2",
            "limitation": "国家统计局行业口径会宽于应用细分行业；映射质量随每条证据保留，不能替代更细的企业开工率。"}
