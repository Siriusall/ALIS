"""Candidate-only extraction of operating KPIs from official annual reports."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pypdf import PdfReader
import requests

from app.data_platform.db import _conn
from app.core.runtime_paths import backend_cache_root


SOURCE_CODE = "cninfo"
CONTRACT_VERSION = "alis_professional_operating_metric_pdf_candidate_v1"
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 800
MAX_CANDIDATES_PER_METRIC = 5
CACHE_ROOT = Path(
    os.getenv(
        "ALIS_PROFESSIONAL_METRIC_CACHE_DIR",
        str(backend_cache_root() / "professional_metric_documents"),
    )
)


METRIC_KEYWORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "cyclical_resource_v1": {
        "company_production_capacity": ("设计产能", "核定产能", "现有产能", "生产能力"),
        "company_capacity_utilization": ("产能利用率",),
        "company_production_volume": ("产品产量", "生产量", "原煤产量", "发电量"),
        "company_sales_volume": ("产品销量", "销售量", "商品煤销量", "售电量"),
    },
    "asset_heavy_v1": {
        "reported_nav_per_share": ("每股净资产", "每股NAV", "NAV/股"),
        "concession_term": ("特许经营期", "特许经营期限", "经营权期限"),
        "occupancy_rate": ("出租率", "入住率", "使用率"),
        "traffic_volume": ("车流量", "通行量", "旅客吞吐量", "货物吞吐量"),
    },
    "platform_service_v1": {
        "monthly_active_users": ("月活跃用户", "月活用户", "MAU"),
        "paying_users": ("付费用户", "付费会员"),
        "retention_rate": ("用户留存率", "客户留存率", "留存率"),
        "renewal_rate": ("续费率", "续约率"),
    },
    "consumer_brand_v1": {
        "same_store_sales_growth": ("同店销售", "同店收入", "同店增长"),
        "store_count": ("门店数量", "门店总数", "门店数"),
        "market_share": ("市场份额", "市占率"),
        "channel_inventory": ("渠道库存", "经销商库存"),
    },
}


ALLOWED_UNITS: dict[str, set[str]] = {
    "company_production_capacity": {
        "万吨/年", "万立方米/年", "亿立方米/年", "万吨", "吨", "万千瓦时", "亿千瓦时",
    },
    "company_capacity_utilization": {"%"},
    "company_production_volume": {
        "万吨", "吨", "万立方米", "亿立方米", "万千瓦时", "亿千瓦时", "万辆", "万件", "万台",
    },
    "company_sales_volume": {
        "万吨", "吨", "万立方米", "亿立方米", "万千瓦时", "亿千瓦时", "万辆", "万件", "万台",
    },
    "reported_nav_per_share": {"元/股", "元"},
    "concession_term": {"年", "个月", "月"},
    "occupancy_rate": {"%"},
    "traffic_volume": {"万辆", "万车次", "万人次", "辆", "人"},
    "monthly_active_users": {"万户", "万人", "万名", "户", "人"},
    "paying_users": {"万户", "万人", "万名", "户", "人"},
    "retention_rate": {"%"},
    "renewal_rate": {"%"},
    "same_store_sales_growth": {"%"},
    "store_count": {"万家", "家"},
    "market_share": {"%"},
    "channel_inventory": {"年", "个月", "月", "天"},
}


_NUMBER_UNIT_RE = re.compile(
    r"(?P<value>-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>万吨/年|万立方米/年|亿立方米/年|万吨|万立方米|亿立方米|"
    r"万千瓦时|亿千瓦时|万辆|万车次|万人次|万户|万人|万名|万家|"
    r"亿元|万元|元/股|元|年|个月|月|天|家|户|人|辆|吨|%|％)"
)
_TABLE_NUMBER = r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?"
_PRODUCTION_SALES_ROW_RE = re.compile(
    rf"^(?P<product>[\u4e00-\u9fffA-Za-z0-9（）()·\-]{{2,40}})\s+"
    rf"(?P<unit>万吨|吨|千克|公斤|KG|kg|万件|件|万套|套|万台|台|万辆|辆|"
    rf"万立方米|立方米|亿千瓦时|万千瓦时)\s+"
    rf"(?P<values>(?:{_TABLE_NUMBER})(?:\s+(?:{_TABLE_NUMBER}))+)"
)
_TABLE_NUMBER_RE = re.compile(_TABLE_NUMBER)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _report_period(title: str) -> str:
    match = re.search(r"(20\d{2})\s*年\s*(年度|半年度)报告", str(title or ""))
    if not match:
        return ""
    return f"{match.group(1)}{'1231' if match.group(2) == '年度' else '0630'}"


def _supported_report_title(title: str) -> bool:
    text = str(title or "")
    if not re.search(r"20\d{2}\s*年\s*年度报告", text):
        return False
    excluded = ("摘要", "英文", "问询", "回复", "意见", "提示性公告", "审计报告")
    return not any(marker in text for marker in excluded)


def _safe_cninfo_pdf_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname == "static.cninfo.com.cn"
        and parsed.path.lower().endswith(".pdf")
    )


def _table_candidate(
    *,
    metric_key: str,
    report_period: str,
    value: float,
    unit: str,
    product: str,
    evidence_text: str,
    page_number: int,
) -> dict[str, Any]:
    raw_value = f"{value:g}{unit}"
    evidence_hash = _sha256_text(
        f"{metric_key}|{report_period}|{page_number}|{product}|{evidence_text}|{raw_value}"
    )
    return {
        "metric_key": metric_key,
        "report_period": report_period,
        "value": value,
        "unit": unit,
        "raw_value_text": raw_value,
        "evidence_text": evidence_text,
        "page_number": page_number,
        "confidence": 0.88,
        "extraction_method": "cninfo_production_sales_table_row_v1",
        "validation_status": "pending_manual_verification",
        "production_eligible": False,
        "evidence_hash": evidence_hash,
        "matched_keyword": "产销量情况分析表",
        "product": product,
    }


def _extract_production_sales_table(
    pages: list[str],
    *,
    report_period: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    matched_rows = 0
    for page_index, page_text in enumerate(pages):
        previous = str(pages[page_index - 1] or "") if page_index > 0 else ""
        context = previous[-4000:] + "\n" + str(page_text or "")[:8000]
        if "产销量情况分析表" not in context or "生产量" not in context or "销售量" not in context:
            continue
        normalized_context = re.sub(r"\s+", "", context)
        production_header = normalized_context.find("生产量")
        purchase_header = normalized_context.find("外购量", production_header)
        sales_header = normalized_context.find("销售量", production_header)
        sales_value_index = (
            2
            if production_header >= 0 and production_header < purchase_header < sales_header
            else 1
        )
        for raw_line in str(page_text or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            match = _PRODUCTION_SALES_ROW_RE.match(line)
            if not match:
                continue
            unit = match.group("unit")
            if unit == "kg":
                unit = "KG"
            product = match.group("product")
            values = [
                float(item.group(0).replace(",", ""))
                for item in _TABLE_NUMBER_RE.finditer(match.group("values"))
            ]
            if len(values) <= sales_value_index:
                continue
            production = values[0]
            sales = values[sales_value_index]
            page_number = page_index + 1
            candidates.extend([
                _table_candidate(
                    metric_key="company_production_volume",
                    report_period=report_period,
                    value=production,
                    unit=unit,
                    product=product,
                    evidence_text=line,
                    page_number=page_number,
                ),
                _table_candidate(
                    metric_key="company_sales_volume",
                    report_period=report_period,
                    value=sales,
                    unit=unit,
                    product=product,
                    evidence_text=line,
                    page_number=page_number,
                ),
            ])
            matched_rows += 1
            if matched_rows >= MAX_CANDIDATES_PER_METRIC:
                return candidates
    return candidates


def extract_metric_candidates(
    pages: list[str],
    *,
    template_kind: str,
    report_period: str,
) -> list[dict[str, Any]]:
    """Extract conservative same-clause value candidates; never mark them verified."""

    definitions = METRIC_KEYWORDS.get(str(template_kind), {})
    if not definitions:
        return []
    candidates: list[dict[str, Any]] = (
        _extract_production_sales_table(pages, report_period=report_period)
        if template_kind == "cyclical_resource_v1"
        else []
    )
    counts: Counter[str] = Counter()
    seen: set[str] = {str(item.get("evidence_hash") or "") for item in candidates}
    for item in candidates:
        counts[str(item.get("metric_key") or "")] += 1
    for page_number, page_text in enumerate(pages, start=1):
        normalized = str(page_text or "").replace("\u3000", " ")
        clauses = re.split(r"[。；;\n\r]+", normalized)
        for raw_clause in clauses:
            clause = re.sub(r"\s+", " ", raw_clause).strip()
            if len(clause) < 6 or len(clause) > 360:
                continue
            for metric_key, keywords in definitions.items():
                matched_keyword = next((item for item in keywords if item in clause), "")
                if not matched_keyword or counts[metric_key] >= MAX_CANDIDATES_PER_METRIC:
                    continue
                allowed_units = ALLOWED_UNITS.get(metric_key, set())
                matches = [
                    item
                    for item in _NUMBER_UNIT_RE.finditer(clause)
                    if item.group("unit").replace("％", "%") in allowed_units
                ]
                if not matches:
                    continue
                keyword_start = clause.index(matched_keyword)
                keyword_end = keyword_start + len(matched_keyword)
                keyword_position = keyword_start + len(matched_keyword) / 2
                def _match_priority(item: re.Match[str]) -> tuple[int, float]:
                    distance = abs((item.start() + item.end()) / 2 - keyword_position)
                    if item.start() >= keyword_end and item.start() - keyword_end <= 40:
                        return 0, distance
                    if item.end() <= keyword_start and keyword_start - item.end() <= 40:
                        return 1, distance
                    return 2, distance
                match = min(
                    matches,
                    key=_match_priority,
                )
                distance = abs((match.start() + match.end()) / 2 - keyword_position)
                if distance > 80:
                    continue
                raw_value = match.group(0)
                unit = match.group("unit").replace("％", "%")
                try:
                    value = float(match.group("value").replace(",", ""))
                except ValueError:
                    continue
                evidence_hash = _sha256_text(
                    f"{metric_key}|{report_period}|{page_number}|{clause}|{raw_value}"
                )
                if evidence_hash in seen:
                    continue
                seen.add(evidence_hash)
                candidates.append({
                    "metric_key": metric_key,
                    "report_period": report_period,
                    "value": value,
                    "unit": unit,
                    "raw_value_text": raw_value,
                    "evidence_text": clause,
                    "page_number": page_number,
                    "confidence": 0.70,
                    "extraction_method": "same_clause_nearest_keyword_number_unit_regex_v1",
                    "validation_status": "pending_manual_verification",
                    "production_eligible": False,
                    "evidence_hash": evidence_hash,
                    "matched_keyword": matched_keyword,
                })
                counts[metric_key] += 1
    return candidates


def _download_pdf(url: str, *, document_id: str) -> tuple[Path, str]:
    if not _safe_cninfo_pdf_url(url):
        raise ValueError("unsupported_or_unsafe_cninfo_pdf_url")
    target_dir = CACHE_ROOT / SOURCE_CODE
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{re.sub(r'[^0-9A-Za-z_-]', '_', document_id)}.pdf"
    if target.exists():
        content = target.read_bytes()
        if content.startswith(b"%PDF") and len(content) <= MAX_PDF_BYTES:
            return target, _sha256_bytes(content)
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"},
        timeout=(10, 45),
        stream=True,
    )
    response.raise_for_status()
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if "pdf" not in content_type:
        raise ValueError(f"unexpected_content_type:{content_type or 'missing'}")
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    with tempfile.NamedTemporaryFile(dir=target_dir, suffix=".part", delete=False) as handle:
        temp_path = Path(handle.name)
        try:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                if not prefix:
                    prefix = chunk[:4]
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise ValueError("pdf_exceeds_size_limit")
                digest.update(chunk)
                handle.write(chunk)
        except Exception:
            handle.close()
            temp_path.unlink(missing_ok=True)
            raise
    if prefix != b"%PDF":
        temp_path.unlink(missing_ok=True)
        raise ValueError("downloaded_content_is_not_pdf")
    temp_path.replace(target)
    return target, digest.hexdigest()


def _read_pdf_pages(path: Path) -> tuple[list[str], int, str]:
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    if page_count <= 0 or page_count > MAX_PDF_PAGES:
        raise ValueError(f"pdf_page_count_out_of_bounds:{page_count}")
    pages = [str(page.extract_text() or "") for page in reader.pages]
    joined_hash = _sha256_text("\n\f\n".join(pages))
    return pages, page_count, joined_hash


def _upsert_result(
    announcement: dict[str, Any],
    *,
    template_kind: str,
    status: str,
    candidates: list[dict[str, Any]],
    local_path: str = "",
    file_sha256: str = "",
    page_count: int = 0,
    text_sha256: str = "",
    error: str = "",
) -> None:
    now = _now()
    document_id = str(announcement.get("announcement_id") or "")
    available_time = str(announcement.get("available_time") or announcement.get("publish_time") or now)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO dc_professional_metric_document
               (source_code,document_id,ts_code,title,publish_time,source_url,local_file_path,
                file_sha256,page_count,text_sha256,template_kind,status,candidates_count,error,
                available_time,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_code,document_id) DO UPDATE SET
                ts_code=excluded.ts_code,title=excluded.title,publish_time=excluded.publish_time,
                source_url=excluded.source_url,local_file_path=excluded.local_file_path,
                file_sha256=excluded.file_sha256,page_count=excluded.page_count,
                text_sha256=excluded.text_sha256,template_kind=excluded.template_kind,
                status=excluded.status,candidates_count=excluded.candidates_count,error=excluded.error,
                available_time=excluded.available_time,updated_at=excluded.updated_at""",
            (
                SOURCE_CODE,
                document_id,
                str(announcement.get("ts_code") or ""),
                str(announcement.get("title") or ""),
                str(announcement.get("publish_time") or ""),
                str(announcement.get("url") or ""),
                local_path,
                file_sha256,
                int(page_count),
                text_sha256,
                template_kind,
                status,
                len(candidates),
                str(error or "")[:1000],
                available_time,
                now,
                now,
            ),
        )
        conn.execute(
            """UPDATE dc_professional_metric_candidate
               SET validation_status='superseded_by_reextraction',production_eligible=0,updated_at=?
               WHERE source_code=? AND document_id=?
                 AND validation_status='pending_manual_verification'""",
            (now, SOURCE_CODE, document_id),
        )
        for item in candidates:
            evidence = {
                "schema_version": CONTRACT_VERSION,
                "source_url": str(announcement.get("url") or ""),
                "title": str(announcement.get("title") or ""),
                "publish_time": str(announcement.get("publish_time") or ""),
                "file_sha256": file_sha256,
                "text_sha256": text_sha256,
                "matched_keyword": item.get("matched_keyword"),
                "product": item.get("product"),
                "review_required": True,
            }
            conn.execute(
                """INSERT INTO dc_professional_metric_candidate
                   (source_code,document_id,metric_key,report_period,evidence_hash,ts_code,
                    template_kind,value,unit,raw_value_text,evidence_text,page_number,confidence,
                    extraction_method,validation_status,production_eligible,evidence_json,
                    available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_code,document_id,metric_key,report_period,evidence_hash)
                   DO UPDATE SET value=excluded.value,unit=excluded.unit,
                    raw_value_text=excluded.raw_value_text,evidence_text=excluded.evidence_text,
                    page_number=excluded.page_number,confidence=excluded.confidence,
                    extraction_method=excluded.extraction_method,
                    validation_status=excluded.validation_status,production_eligible=0,
                    evidence_json=excluded.evidence_json,available_time=excluded.available_time,
                    updated_at=excluded.updated_at""",
                (
                    SOURCE_CODE,
                    document_id,
                    item["metric_key"],
                    item["report_period"],
                    item["evidence_hash"],
                    str(announcement.get("ts_code") or ""),
                    template_kind,
                    float(item["value"]),
                    item["unit"],
                    item["raw_value_text"],
                    item["evidence_text"],
                    int(item["page_number"]),
                    float(item["confidence"]),
                    item["extraction_method"],
                    "pending_manual_verification",
                    0,
                    json.dumps(evidence, ensure_ascii=False),
                    available_time,
                    now,
                    now,
                ),
            )


def _next_documents(limit: int) -> list[dict[str, Any]]:
    from app.services.research_engine import _industry_template_kind

    with _conn() as conn:
        rows = conn.execute(
            """SELECT a.*,m.industry
               FROM dc_announcement_raw a
               LEFT JOIN dc_stock_master m ON m.ts_code=a.ts_code
               WHERE a.source_code=? AND NULLIF(a.url,'') IS NOT NULL
                 AND a.title LIKE ?
                 AND NOT EXISTS(
                    SELECT 1 FROM dc_professional_metric_document d
                    WHERE d.source_code=a.source_code AND d.document_id=a.announcement_id
                 )
               ORDER BY a.publish_time DESC,a.announcement_id DESC LIMIT 2000""",
            (SOURCE_CODE, "%年度报告%"),
        ).fetchall()
    selected: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if not _supported_report_title(str(item.get("title") or "")):
            continue
        template_kind = _industry_template_kind(str(item.get("industry") or ""))
        if template_kind not in METRIC_KEYWORDS:
            continue
        item["template_kind"] = template_kind
        selected.append(item)
        if len(selected) >= max(1, min(int(limit), 5)):
            break
    return selected


def sync_professional_metric_candidates(*, limit: int = 1) -> dict[str, Any]:
    """Process at most five reports; intended default is one report per cycle."""

    documents = _next_documents(limit)
    # The ordinary daily announcement feed is intentionally narrow and may
    # not include the annual-report category.  Repair that boundary lazily
    # when the candidate queue is empty, keeping the discovery bounded and
    # candidate-only so it cannot affect production signals.
    if not documents:
        from app.data_platform.services.data_center import sync_cninfo_announcements

        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        sync_cninfo_announcements(
            start_date=(today - timedelta(days=210)).strftime("%Y%m%d"),
            end_date=today.strftime("%Y%m%d"),
            page_size=30,
            max_pages=20,
            category="category_ndbg_szsh",
        )
        documents = _next_documents(limit)
    results: list[dict[str, Any]] = []
    for announcement in documents:
        template_kind = str(announcement.get("template_kind") or "")
        try:
            path, file_sha256 = _download_pdf(
                str(announcement.get("url") or ""),
                document_id=str(announcement.get("announcement_id") or ""),
            )
            pages, page_count, text_sha256 = _read_pdf_pages(path)
            candidates = extract_metric_candidates(
                pages,
                template_kind=template_kind,
                report_period=_report_period(str(announcement.get("title") or "")),
            )
            status = "candidate_extracted" if candidates else "no_supported_metric_candidates"
            _upsert_result(
                announcement,
                template_kind=template_kind,
                status=status,
                candidates=candidates,
                local_path=str(path),
                file_sha256=file_sha256,
                page_count=page_count,
                text_sha256=text_sha256,
            )
            results.append({
                "document_id": announcement.get("announcement_id"),
                "ts_code": announcement.get("ts_code"),
                "template_kind": template_kind,
                "status": status,
                "page_count": page_count,
                "candidates_count": len(candidates),
            })
        except Exception as exc:  # noqa: BLE001
            _upsert_result(
                announcement,
                template_kind=template_kind,
                status="extraction_failed",
                candidates=[],
                error=str(exc),
            )
            results.append({
                "document_id": announcement.get("announcement_id"),
                "ts_code": announcement.get("ts_code"),
                "template_kind": template_kind,
                "status": "extraction_failed",
                "error": str(exc)[:500],
            })
    return {
        "ok": all(item.get("status") != "extraction_failed" for item in results),
        "schema_version": CONTRACT_VERSION,
        "requested_limit": max(1, min(int(limit), 5)),
        "processed": len(results),
        "items": results,
        "production_change_forbidden": True,
    }


def reprocess_professional_metric_document(document_id: str) -> dict[str, Any]:
    """Re-extract one retained document after a contract upgrade."""

    from app.services.research_engine import _industry_template_kind

    identifier = str(document_id or "").strip()
    with _conn() as conn:
        row = conn.execute(
            """SELECT a.*,m.industry,d.local_file_path,d.file_sha256
               FROM dc_announcement_raw a
               LEFT JOIN dc_stock_master m ON m.ts_code=a.ts_code
               LEFT JOIN dc_professional_metric_document d
                 ON d.source_code=a.source_code AND d.document_id=a.announcement_id
               WHERE a.source_code=? AND a.announcement_id=? LIMIT 1""",
            (SOURCE_CODE, identifier),
        ).fetchone()
    if not row:
        return {"ok": False, "status": "document_not_found", "document_id": identifier}
    announcement = dict(row)
    title = str(announcement.get("title") or "")
    template_kind = _industry_template_kind(str(announcement.get("industry") or ""))
    if not _supported_report_title(title) or template_kind not in METRIC_KEYWORDS:
        return {
            "ok": False,
            "status": "unsupported_document",
            "document_id": identifier,
            "template_kind": template_kind,
        }
    try:
        retained_path = Path(str(announcement.get("local_file_path") or ""))
        if retained_path.exists():
            path = retained_path
            file_sha256 = str(announcement.get("file_sha256") or "") or _sha256_bytes(path.read_bytes())
        else:
            path, file_sha256 = _download_pdf(
                str(announcement.get("url") or ""),
                document_id=identifier,
            )
        pages, page_count, text_sha256 = _read_pdf_pages(path)
        candidates = extract_metric_candidates(
            pages,
            template_kind=template_kind,
            report_period=_report_period(title),
        )
        status = "candidate_extracted" if candidates else "no_supported_metric_candidates"
        _upsert_result(
            announcement,
            template_kind=template_kind,
            status=status,
            candidates=candidates,
            local_path=str(path),
            file_sha256=file_sha256,
            page_count=page_count,
            text_sha256=text_sha256,
        )
        return {
            "ok": True,
            "schema_version": CONTRACT_VERSION,
            "document_id": identifier,
            "ts_code": announcement.get("ts_code"),
            "template_kind": template_kind,
            "status": status,
            "page_count": page_count,
            "candidates_count": len(candidates),
            "production_change_forbidden": True,
        }
    except Exception as exc:  # noqa: BLE001
        _upsert_result(
            announcement,
            template_kind=template_kind,
            status="extraction_failed",
            candidates=[],
            error=str(exc),
        )
        return {
            "ok": False,
            "document_id": identifier,
            "status": "extraction_failed",
            "error": str(exc)[:500],
        }


def list_professional_metric_candidates(
    ts_code: str,
    *,
    as_of_time: str | None = None,
) -> dict[str, Any]:
    """Expose unverified candidates to research without granting score authority."""

    code = str(ts_code or "").strip().upper()
    query = """SELECT c.*,d.title,d.publish_time,d.source_url,d.file_sha256,d.text_sha256
               FROM dc_professional_metric_candidate c
               JOIN dc_professional_metric_document d
                 ON d.source_code=c.source_code AND d.document_id=c.document_id
               WHERE c.ts_code=? AND c.validation_status='pending_manual_verification'
                 AND c.production_eligible=0"""
    params: list[Any] = [code]
    if as_of_time:
        query += " AND c.available_time<=?"
        params.append(str(as_of_time))
    query += " ORDER BY c.report_period DESC,c.metric_key,c.page_number,c.evidence_hash"
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        items.append({
            "document_id": row.get("document_id"),
            "ts_code": row.get("ts_code"),
            "template_kind": row.get("template_kind"),
            "metric_key": row.get("metric_key"),
            "report_period": row.get("report_period"),
            "value": row.get("value"),
            "unit": row.get("unit"),
            "raw_value_text": row.get("raw_value_text"),
            "evidence_text": row.get("evidence_text"),
            "page_number": row.get("page_number"),
            "confidence": row.get("confidence"),
            "validation_status": row.get("validation_status"),
            "production_eligible": False,
            "available_time": row.get("available_time"),
            "title": row.get("title"),
            "publish_time": row.get("publish_time"),
            "source_url": row.get("source_url"),
            "evidence": evidence,
        })
    return {
        "ok": True,
        "schema_version": CONTRACT_VERSION,
        "ts_code": code,
        "items": items,
        "count": len(items),
        "status": "candidate_only_pending_manual_verification" if items else "no_candidates",
        "production_eligible": 0,
    }


def audit_professional_metric_candidates() -> dict[str, Any]:
    with _conn() as conn:
        documents = [
            dict(row)
            for row in conn.execute(
                """SELECT status,template_kind,COUNT(*) count,SUM(candidates_count) candidates
                   FROM dc_professional_metric_document GROUP BY status,template_kind
                   ORDER BY template_kind,status"""
            ).fetchall()
        ]
        candidates = [
            dict(row)
            for row in conn.execute(
                """SELECT validation_status,template_kind,metric_key,COUNT(*) count
                   FROM dc_professional_metric_candidate
                   GROUP BY validation_status,template_kind,metric_key
                   ORDER BY template_kind,metric_key,validation_status"""
            ).fetchall()
        ]
        totals = dict(conn.execute(
            """SELECT COUNT(*) documents,
                      COALESCE(SUM(candidates_count),0) candidates,
                      COALESCE(SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END),0) failed
               FROM dc_professional_metric_document"""
        ).fetchone())
    return {
        "ok": int(totals.get("failed") or 0) == 0,
        "schema_version": CONTRACT_VERSION,
        "documents": int(totals.get("documents") or 0),
        "candidates": int(totals.get("candidates") or 0),
        "failed": int(totals.get("failed") or 0),
        "document_groups": documents,
        "candidate_groups": candidates,
        "production_eligible": 0,
        "status": "candidate_only_pending_manual_verification",
    }
