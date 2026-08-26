"""Low-priority CAAM image OCR research candidates.

These observations are deliberately isolated from industry metrics and model
features.  They become eligible only after reconciliation with a structured
source; OCR confidence is evidence quality, not permission to trade.
"""

from __future__ import annotations

import calendar
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from app.data_platform.db import _conn, _read_conn
from app.data_platform.services.data_center import _now
from app.services.official_policy_data import _LinkParser, _published_time, _request_text, _save_documents, _text


CAAM_OCR_SOURCE = "association.caam.ocr_candidate"
CAAM_INDEX_URLS = ("http://www.caam.org.cn/", "http://www.caam.org.cn/tjsj")
OCR_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "macos_vision_ocr.swift"
MAX_NEW_DOCUMENTS_PER_CYCLE = 1
OCR_TIMEOUT_SECONDS = 90
OCR_AUDIT_WRITE_BATCH_SIZE = 200
MIIT_AUTO_INDEX_URL = "https://www.miit.gov.cn/jgsj/zbys/qcgy/index.html"
MIIT_AUTO_SOURCE = "official.miit.auto_operations"


def _sales_links(index_url: str, html: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(html)
    rows: list[tuple[str, str]] = []
    for href, raw_title in parser.links:
        title = re.sub(r"^[·.\s]+|\s*\[\d{2}\.\d{2}\]\s*$", "", unescape(raw_title)).strip()
        url = urljoin(index_url, href)
        if urlparse(url).netloc != "www.caam.org.cn" or "con_" not in url:
            continue
        if not re.search(r"20\d{2}年\d{1,2}月.*(?:汽车|乘用车|商用车).*产销情况", title):
            continue
        rows.append((url, title))
    return list(dict.fromkeys(rows))


def _document_id(page_url: str) -> str:
    match = re.search(r"con_(\d+)\.html", page_url)
    return f"caam-con-{match.group(1)}" if match else sha256(page_url.encode("utf-8")).hexdigest()[:32]


def _article_images(page_url: str, html: str) -> list[str]:
    match = re.search(
        r'<div\s+class=["\']fourBox["\'][^>]*>(.*?)</div>\s*</div>\s*<!--\s*right',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    body = match.group(1) if match else html
    urls = [urljoin(page_url, unescape(value)) for value in re.findall(r'<img[^>]+src=["\']([^"\']+)', body, re.I)]
    return list(dict.fromkeys(url for url in urls if urlparse(url).netloc == "file.caam.org.cn"))


def _request_bytes(url: str) -> bytes:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        url,
        headers={"User-Agent": "ALIS/1.0 CAAM public research OCR collector"},
        timeout=(5, 25),
    )
    response.raise_for_status()
    return response.content


def _ocr_image(content: bytes) -> list[dict[str, Any]]:
    swift = shutil.which("swift")
    command_line_swift = Path("/Library/Developer/CommandLineTools/usr/bin/swift")
    if not swift and command_line_swift.exists():
        swift = str(command_line_swift)
    if platform.system() != "Darwin" or not swift or not OCR_SCRIPT.exists():
        raise RuntimeError("macos_vision_ocr_unavailable")
    with tempfile.TemporaryDirectory(prefix="alis-caam-ocr-") as directory:
        image_path = Path(directory) / "source-image"
        image_path.write_bytes(content)
        completed = subprocess.run(
            [swift, str(OCR_SCRIPT), str(image_path)],
            capture_output=True,
            check=True,
            timeout=OCR_TIMEOUT_SECONDS,
        )
    payload = json.loads(completed.stdout.decode("utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("macos_vision_ocr_invalid_payload")
    return [
        {"text": str(item.get("text") or "").strip(), "confidence": round(float(item.get("confidence") or 0), 6)}
        for item in payload if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]


def _field_confidence(lines: list[dict[str, Any]], marker: str, subject: str) -> float:
    candidates = []
    for item in lines:
        text = re.sub(r"\s+", "", str(item.get("text") or ""))
        if marker not in text or subject not in text:
            continue
        if subject == "汽车产销" and any(prefix in text for prefix in ("乘用车", "商用车", "新能源汽车")):
            continue
        candidates.append(float(item.get("confidence") or 0))
    return round(max(candidates, default=0.0), 6)


def _parse_monthly_observations(title: str, lines: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    period = re.search(r"(20\d{2})年(\d{1,2})月", title)
    if not period:
        return "", []
    year, month = int(period.group(1)), int(period.group(2))
    observation_date = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
    marker = f"{year}年{month}月"
    short_marker = rf"(?:{year}年)?{month}月"
    text = re.sub(r"\s+", "", "".join(str(item.get("text") or "") for item in lines))
    specifications = (
        ("vehicle_production_10k", "汽车产销", rf"{marker}[，,]汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("vehicle_sales_10k", "汽车产销", rf"{marker}[，,]汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("passenger_vehicle_production_10k", "乘用车产销", rf"{marker}[，,]乘用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("passenger_vehicle_sales_10k", "乘用车产销", rf"{marker}[，,]乘用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("commercial_vehicle_production_10k", "商用车产销", rf"{marker}[，,]商用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("commercial_vehicle_sales_10k", "商用车产销", rf"{marker}[，,]商用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("new_energy_vehicle_production_10k", "新能源汽车产销", rf"{marker}[，,]新能源汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("new_energy_vehicle_sales_10k", "新能源汽车产销", rf"{marker}[，,]新能源汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("vehicle_domestic_sales_10k", "汽车国内销量", rf"{marker}[，,]汽车国内销量([\d.]+)万辆", 1),
        ("vehicle_export_10k", "汽车出口", rf"{marker}[，,]汽车出口([\d.]+)万辆", 1),
    )
    observations: list[dict[str, Any]] = []
    for field_code, subject, pattern, group in specifications:
        match = re.search(pattern, text)
        if not match:
            continue
        value = float(match.group(group))
        if value <= 0 or value > 10000:
            continue
        observations.append({
            "field_code": field_code,
            "observation_date": observation_date,
            "value": value,
            "unit": "10k_vehicles",
            "confidence": _field_confidence(lines, marker, subject),
            "matched_text": match.group(0),
        })
    return observation_date, observations


def _cross_document_assessment(rows: list[dict[str, Any]], *, tolerance: float = 0.05) -> dict[tuple[str, str], str]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row.get("observation_date") or ""), str(row.get("field_code") or "")), []).append(row)
    result: dict[tuple[str, str], str] = {}
    for key, items in grouped.items():
        documents = {str(item.get("document_id") or "") for item in items}
        if len(documents) < 2:
            result[key] = "pending_structured_reconciliation"
            continue
        values = [float(item.get("value") or 0) for item in items]
        result[key] = (
            "cross_document_consistent_pending_structured"
            if max(values) - min(values) <= tolerance
            else "cross_document_conflict"
        )
    return result


def audit_caam_ocr_candidates() -> dict[str, Any]:
    """Cross-check duplicate CAAM images without promoting OCR to production."""
    with _read_conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM dc_research_ocr_observation WHERE source_code=? ORDER BY observation_date,field_code,document_id",
            (CAAM_OCR_SOURCE,),
        ).fetchall()]
    assessments = _cross_document_assessment(rows)
    peers_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    document_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row.get("observation_date") or ""), str(row.get("field_code") or ""))
        peers_by_key.setdefault(key, []).append({
            "document_id": row.get("document_id"),
            "value": row.get("value"),
            "confidence": row.get("confidence"),
        })
    for key, peers in peers_by_key.items():
        document_counts[key] = len({str(item["document_id"]) for item in peers})

    now = _now()
    updates: list[tuple[Any, ...]] = []
    for row in rows:
        if str(row.get("validation_status") or "").startswith("structured_"):
            continue
        key = (str(row.get("observation_date") or ""), str(row.get("field_code") or ""))
        status = assessments[key]
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        evidence["cross_document_validation"] = {
            "status": status,
            "same_publisher_independent_images": document_counts[key],
            "peers": peers_by_key[key],
            "structured_source_reconciled": False,
            "production_eligible": False,
        }
        updates.append((
            status, json.dumps(evidence, ensure_ascii=False), now, CAAM_OCR_SOURCE,
            row["document_id"], row["field_code"], row["observation_date"],
        ))
    # JSON decoding/grouping happens before any write transaction.  Bound each
    # mutation transaction without dropping rows from the complete audit.
    for start in range(0, len(updates), OCR_AUDIT_WRITE_BATCH_SIZE):
        with _conn() as conn:
            for params in updates[start:start + OCR_AUDIT_WRITE_BATCH_SIZE]:
                conn.execute(
                    """UPDATE dc_research_ocr_observation SET validation_status=?,evidence_json=?,updated_at=?
                       WHERE source_code=? AND document_id=? AND field_code=? AND observation_date=?""",
                    params,
                )
    updated = len(updates)
    counts: dict[str, int] = {}
    for status in assessments.values():
        counts[status] = counts.get(status, 0) + 1
    return {
        "ok": True,
        "observations": len(rows),
        "field_date_groups": len(assessments),
        "group_status_counts": counts,
        "rows_updated": updated,
        "production_eligible": False,
        "contract": "alis_caam_ocr_cross_document_audit_v1",
    }


def _miit_auto_links(html: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(html)
    rows: list[tuple[str, str]] = []
    for href, raw_title in parser.links:
        title = re.sub(r"\s+", " ", unescape(raw_title)).strip()
        url = urljoin(MIIT_AUTO_INDEX_URL, href)
        if urlparse(url).netloc != "www.miit.gov.cn":
            continue
        if re.search(r"20\d{2}年\d{1,2}月汽车工业经济运行情况", title):
            rows.append((url, title))
    return list(dict.fromkeys(rows))


def _parse_miit_auto_observations(title: str, page_text: str) -> tuple[str, list[dict[str, Any]]]:
    period = re.search(r"(20\d{2})年(\d{1,2})月", title)
    if not period:
        return "", []
    year, month = int(period.group(1)), int(period.group(2))
    observation_date = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
    marker = f"{year}年{month}月"
    short_marker = rf"(?:{year}年)?{month}月"
    text = re.sub(r"\s+", "", page_text)
    specifications = (
        ("vehicle_production_10k", rf"{marker}[，,]?(?:我国)?汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("vehicle_sales_10k", rf"{marker}[，,]?(?:我国)?汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("passenger_vehicle_production_10k", rf"{short_marker}[，,]?乘用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("passenger_vehicle_sales_10k", rf"{short_marker}[，,]?乘用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("commercial_vehicle_production_10k", rf"{short_marker}[，,]?商用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("commercial_vehicle_sales_10k", rf"{short_marker}[，,]?商用车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("new_energy_vehicle_production_10k", rf"{short_marker}[，,]?新能源汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 1),
        ("new_energy_vehicle_sales_10k", rf"{short_marker}[，,]?新能源汽车产销分别完成([\d.]+)万辆和([\d.]+)万辆", 2),
        ("vehicle_export_10k", rf"{short_marker}[，,]?汽车(?:整车)?出口([\d.]+)万辆", 1),
    )
    observations: list[dict[str, Any]] = []
    for field_code, pattern, group in specifications:
        match = re.search(pattern, text)
        if match:
            observations.append({"field_code": field_code, "observation_date": observation_date,
                                 "value": float(match.group(group)), "matched_text": match.group(0)})
    return observation_date, observations


def _miit_article_text(html: str) -> str:
    rendered = _text(html)
    publish_marker = rendered.find("发布时间")
    if publish_marker >= 0:
        rendered = rendered[publish_marker:]
    boundaries = [rendered.find(marker) for marker in ("网站地图", "主办单位") if rendered.find(marker) >= 0]
    return rendered[:min(boundaries)].strip() if boundaries else rendered.strip()


def reconcile_caam_ocr_with_miit(*, max_documents: int = 5) -> dict[str, Any]:
    """Reconcile OCR candidates with official MIIT text when it is reachable."""
    max_documents = max(1, min(int(max_documents), 12))
    available_time = _now()
    try:
        index_html = _request_text(MIIT_AUTO_INDEX_URL)
    except requests.RequestException as exc:
        return {"ok": True, "status": "external_source_unavailable", "error": str(exc)[:240],
                "documents": 0, "rows_reconciled": 0, "conflicts": 0,
                "contract": "alis_caam_ocr_miit_structured_reconciliation_v1", "production_eligible": False}
    links = _miit_auto_links(index_html)[:max_documents]
    retained: list[dict[str, str]] = []
    reconciled = conflicts = 0
    documents: list[dict[str, Any]] = []
    for page_url, fallback_title in links:
        try:
            html = _request_text(page_url)
        except requests.RequestException as exc:
            documents.append({"url": page_url, "status": "failed", "error": str(exc)[:240]})
            continue
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", unescape(title_match.group(1))).strip() if title_match else fallback_title
        page_text = _miit_article_text(html)
        observation_date, observations = _parse_miit_auto_observations(title, page_text)
        document_id = _document_id(page_url)
        retained.append({
            "source_code": MIIT_AUTO_SOURCE, "announcement_id": document_id, "title": title,
            "publish_time": _published_time(html), "url": page_url, "raw_text": page_text[:12000],
            "content_hash": sha256(page_text.encode("utf-8")).hexdigest(), "available_time": available_time,
        })
        matched = 0
        with _conn() as conn:
            for item in observations:
                peers = [dict(row) for row in conn.execute(
                    """SELECT * FROM dc_research_ocr_observation
                       WHERE source_code=? AND observation_date=? AND field_code=?
                       ORDER BY document_id""",
                    (CAAM_OCR_SOURCE, observation_date, item["field_code"]),
                ).fetchall()]
                for peer in peers:
                    difference = abs(float(peer.get("value") or 0) - float(item["value"]))
                    status = "structured_reconciled" if difference <= 0.05 else "structured_source_conflict"
                    try:
                        evidence = json.loads(peer.get("evidence_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        evidence = {}
                    evidence["structured_reconciliation"] = {
                        "status": status, "source_code": MIIT_AUTO_SOURCE, "page_url": page_url,
                        "document_id": document_id, "value": item["value"], "matched_text": item["matched_text"],
                        "absolute_difference": round(difference, 6), "checked_at": available_time,
                        "production_eligible": False,
                    }
                    conn.execute(
                        """UPDATE dc_research_ocr_observation SET validation_status=?,evidence_json=?,updated_at=?
                           WHERE source_code=? AND document_id=? AND field_code=? AND observation_date=?""",
                        (status, json.dumps(evidence, ensure_ascii=False), available_time, CAAM_OCR_SOURCE,
                         peer["document_id"], peer["field_code"], peer["observation_date"]),
                    )
                    reconciled += int(status == "structured_reconciled")
                    conflicts += int(status == "structured_source_conflict")
                    matched += 1
        documents.append({"url": page_url, "status": "checked", "structured_fields": len(observations),
                          "ocr_rows_matched": matched})
    _save_documents(retained)
    return {"ok": True, "status": "conflict" if conflicts else "reconciled" if reconciled else "no_matching_ocr",
            "documents": len(documents), "rows_reconciled": reconciled, "conflicts": conflicts,
            "details": documents, "contract": "alis_caam_ocr_miit_structured_reconciliation_v1",
            "production_eligible": False}


def _save_observations(
    *, document_id: str, observations: list[dict[str, Any]], evidence: dict[str, Any], available_time: str
) -> int:
    now = _now()
    with _conn() as conn:
        for item in observations:
            item_evidence = {**evidence, "matched_text": item["matched_text"], "field_confidence": item["confidence"]}
            conn.execute(
                """INSERT INTO dc_research_ocr_observation
                   (source_code,document_id,field_code,observation_date,value,unit,confidence,validation_status,evidence_json,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_code,document_id,field_code,observation_date) DO UPDATE SET
                     value=excluded.value,unit=excluded.unit,confidence=excluded.confidence,
                     validation_status=excluded.validation_status,evidence_json=excluded.evidence_json,
                     available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (
                    CAAM_OCR_SOURCE, document_id, item["field_code"], item["observation_date"], item["value"],
                    item["unit"], item["confidence"], "pending_structured_reconciliation",
                    json.dumps(item_evidence, ensure_ascii=False), available_time, now, now,
                ),
            )
    return len(observations)


def _save_document_status(
    *, document_id: str, page_url: str, title: str = "", publish_time: str = "", image_url: str = "",
    image_sha256: str = "", status: str, observations_count: int = 0, error: str = "", evidence: dict[str, Any] | None = None,
    available_time: str,
) -> None:
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT attempts,created_at FROM dc_research_ocr_document WHERE source_code=? AND document_id=?",
            (CAAM_OCR_SOURCE, document_id),
        ).fetchone()
        attempts = int(dict(existing).get("attempts") or 0) + 1 if existing else 1
        created_at = str(dict(existing).get("created_at") or now) if existing else now
        conn.execute(
            """INSERT INTO dc_research_ocr_document
               (source_code,document_id,page_url,title,publish_time,image_url,image_sha256,status,attempts,
                observations_count,error,evidence_json,available_time,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_code,document_id) DO UPDATE SET
                 page_url=excluded.page_url,title=excluded.title,publish_time=excluded.publish_time,
                 image_url=excluded.image_url,image_sha256=excluded.image_sha256,status=excluded.status,
                 attempts=excluded.attempts,observations_count=excluded.observations_count,error=excluded.error,
                 evidence_json=excluded.evidence_json,available_time=excluded.available_time,updated_at=excluded.updated_at""",
            (
                CAAM_OCR_SOURCE, document_id, page_url, title, publish_time, image_url, image_sha256, status,
                attempts, observations_count, error[:500], json.dumps(evidence or {}, ensure_ascii=False),
                available_time, created_at, now,
            ),
        )


def sync_caam_ocr_candidates(*, max_documents: int = MAX_NEW_DOCUMENTS_PER_CYCLE) -> dict[str, Any]:
    """Fetch a bounded set of new CAAM image documents and retain isolated OCR fields."""
    max_documents = max(1, min(int(max_documents), 5))
    available_time = _now()
    links: list[tuple[str, str]] = []
    index_results: list[dict[str, Any]] = []
    for index_url in CAAM_INDEX_URLS:
        try:
            html = _request_text(index_url)
            found = _sales_links(index_url, html)
            links.extend(found)
            index_results.append({"url": index_url, "status": "success", "links": len(found)})
        except requests.RequestException as exc:
            index_results.append({"url": index_url, "status": "failed", "error": str(exc)[:240]})
    links_by_document: dict[str, tuple[str, str]] = {}
    for page_url, title in links:
        links_by_document.setdefault(_document_id(page_url), (page_url, title))
    links = list(links_by_document.values())
    with _conn() as conn:
        known = {str(row["document_id"]) for row in conn.execute(
            """SELECT document_id FROM dc_research_ocr_document
               WHERE source_code=? AND status IN ('candidate_saved','ocr_no_structured_fields','no_article_image')
               UNION SELECT DISTINCT document_id FROM dc_research_ocr_observation WHERE source_code=?""",
            (CAAM_OCR_SOURCE, CAAM_OCR_SOURCE),
        ).fetchall()}
    attempted = written = 0
    documents: list[dict[str, Any]] = []
    for page_url, fallback_title in links:
        document_id = _document_id(page_url)
        if document_id in known or attempted >= max_documents:
            continue
        attempted += 1
        try:
            html = _request_text(page_url)
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = re.sub(r"\s+", " ", unescape(title_match.group(1))).strip() if title_match else fallback_title
            image_urls = _article_images(page_url, html)
            if not image_urls:
                _save_document_status(
                    document_id=document_id, page_url=page_url, title=title, publish_time=_published_time(html),
                    status="no_article_image", available_time=available_time,
                )
                documents.append({"url": page_url, "status": "no_article_image"})
                continue
            image_content = _request_bytes(image_urls[0])
            lines = _ocr_image(image_content)
            observation_date, observations = _parse_monthly_observations(title, lines)
            if not observations:
                _save_document_status(
                    document_id=document_id, page_url=page_url, title=title, publish_time=_published_time(html),
                    image_url=image_urls[0], image_sha256=sha256(image_content).hexdigest(),
                    status="ocr_no_structured_fields", evidence={"ocr_lines": lines, "production_eligible": False},
                    available_time=available_time,
                )
                documents.append({"url": page_url, "status": "ocr_no_structured_fields", "ocr_lines": len(lines)})
                continue
            evidence = {
                "contract": "alis_caam_macos_vision_ocr_candidate_v1",
                "page_url": page_url,
                "image_url": image_urls[0],
                "title": title,
                "publish_time": _published_time(html),
                "observation_date": observation_date,
                "image_sha256": sha256(image_content).hexdigest(),
                "ocr_engine": "macos_vision_accurate_zh_hans",
                "ocr_lines": lines,
                "validation_status": "pending_structured_reconciliation",
                "production_eligible": False,
            }
            count = _save_observations(
                document_id=document_id, observations=observations, evidence=evidence, available_time=available_time
            )
            _save_document_status(
                document_id=document_id, page_url=page_url, title=title, publish_time=evidence["publish_time"],
                image_url=image_urls[0], image_sha256=evidence["image_sha256"], status="candidate_saved",
                observations_count=count, evidence={"contract": evidence["contract"], "production_eligible": False},
                available_time=available_time,
            )
            written += count
            documents.append({"url": page_url, "status": "candidate_saved", "fields": count})
        except (requests.RequestException, subprocess.SubprocessError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            _save_document_status(
                document_id=document_id, page_url=page_url, title=fallback_title, status="failed",
                error=str(exc), available_time=available_time,
            )
            documents.append({"url": page_url, "status": "failed", "error": str(exc)[:240]})
    return {
        "ok": True,
        "status": "candidate_ready" if written else "partial" if any(item["status"] == "failed" for item in index_results + documents) else "no_new_candidate",
        "links": len(links),
        "attempted_documents": attempted,
        "max_documents": max_documents,
        "observations_written": written,
        "documents": documents,
        "indexes": index_results,
        "contract": "alis_caam_macos_vision_ocr_candidate_v1",
        "production_eligible": False,
    }
