"""Independent benchmarks for announcement classification quality.

Manual labels remain the only contract that can measure broad false positives,
false negatives and extracted-field correctness.  A second, source-native
reference set uses CNINFO's own category filters.  It is independent of the
local title rules, but it only measures the event types covered by those
provider categories and therefore must never be presented as a full gold set.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

import pandas as pd

from app.data_platform.db import _conn


CNINFO_CATEGORY_REFERENCE_CONTRACT = "alis_cninfo_official_category_reference_v1"
CNINFO_CATEGORY_HOLDOUT_CONTRACT = "alis_cninfo_official_category_reference_holdout_v1_2024"
CNINFO_CATEGORY_REFERENCE_SOURCE = "cninfo_official_category_filter"
CNINFO_CATEGORY_REFERENCE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "event_type": "earnings_preannouncement",
        "allowed_local_event_types": ("earnings_forecast", "earnings_flash"),
        "categories": (("category_yjygjxz_szsh", "业绩预告"),),
    },
    {
        "event_type": "dividend",
        "allowed_local_event_types": ("dividend",),
        "categories": (("category_qyfpxzcs_szsh", "权益分派"),),
    },
    {
        "event_type": "earnings",
        "allowed_local_event_types": ("earnings",),
        "categories": (
            ("category_ndbg_szsh", "年报"),
            ("category_bndbg_szsh", "半年报"),
            ("category_yjdbg_szsh", "一季报"),
            ("category_sjdbg_szsh", "三季报"),
        ),
    },
)
MIN_PROVIDER_REFERENCE_LABELS = 200
MIN_PROVIDER_REFERENCE_PER_TYPE = 50
MIN_MANUAL_LABELS = 200
MIN_MANUAL_EVENT_LABELS = 100
MIN_MANUAL_NON_EVENT_LABELS = 50
MIN_MANUAL_EVENT_TYPES = 3
MANUAL_REVIEW_QUEUE_CONTRACT = "alis_announcement_manual_gold_review_queue_v1"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalise_label(row: dict[str, Any]) -> dict[str, Any]:
    source_code = str(row.get("source_code") or "cninfo").strip()
    announcement_id = str(row.get("announcement_id") or "").strip()
    if not announcement_id:
        raise ValueError("announcement_id is required")
    event_type = str(row.get("event_type") or "").strip()
    raw_event = row.get("is_event")
    is_event = bool(event_type) if raw_event in (None, "") else str(raw_event).strip().lower() in {"1", "true", "yes", "y"}
    if is_event and not event_type:
        raise ValueError(f"event_type is required when is_event=true: {announcement_id}")
    return {
        "source_code": source_code,
        "announcement_id": announcement_id,
        "label_version": str(row.get("label_version") or "v1").strip(),
        "is_event": int(is_event),
        "event_type": event_type if is_event else "",
        "impact_direction": str(row.get("impact_direction") or "neutral").strip(),
        "severity": str(row.get("severity") or "medium").strip(),
        "annotator": str(row.get("annotator") or "").strip(),
        "labelled_at": str(row.get("labelled_at") or _now()).strip(),
        "evidence_note": str(row.get("evidence_note") or "").strip(),
    }


def import_announcement_labels(path: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    if not source.exists():
        raise FileNotFoundError(str(source))
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        rows = payload.get("labels", []) if isinstance(payload, dict) else payload
    else:
        rows = pd.read_csv(source).to_dict(orient="records")
    if not isinstance(rows, list):
        raise ValueError("label file must contain a JSON list/object.labels or CSV rows")
    labels = [_normalise_label(dict(row)) for row in rows]
    now = _now()
    with _conn() as conn:
        for label in labels:
            params = (*label.values(), now)
            conn.execute(
                """INSERT INTO dc_announcement_label
                   (source_code,announcement_id,label_version,is_event,event_type,impact_direction,severity,annotator,labelled_at,evidence_note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_code,announcement_id,label_version) DO UPDATE SET
                     is_event=excluded.is_event,event_type=excluded.event_type,impact_direction=excluded.impact_direction,
                     severity=excluded.severity,annotator=excluded.annotator,labelled_at=excluded.labelled_at,
                     evidence_note=excluded.evidence_note,updated_at=excluded.updated_at""",
                params,
            )
            conn.execute(
                """UPDATE dc_announcement_label_review_queue
                      SET status='labelled',updated_at=?
                    WHERE source_code=? AND announcement_id=?""",
                (now, label["source_code"], label["announcement_id"]),
            )
    return {"ok": True, "labels": len(labels), "contract": "announcement_label_benchmark_v1"}


def announcement_label_benchmark() -> dict[str, Any]:
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT l.source_code,l.announcement_id,l.is_event,l.event_type,l.impact_direction,l.severity,
                      a.id AS raw_id,e.event_type AS predicted_event_type,
                      e.impact_direction AS predicted_direction,e.severity AS predicted_severity
                 FROM dc_announcement_label l
                 LEFT JOIN dc_announcement_raw a
                   ON a.source_code=l.source_code AND a.announcement_id=l.announcement_id
                 LEFT JOIN dc_announcement_event e ON e.announcement_id=a.id"""
        ).fetchall()]
    labels = len(rows)
    matched_rows = [row for row in rows if row.get("raw_id") is not None]
    matched = len(matched_rows)
    event_labels = sum(int(row.get("is_event") or 0) == 1 for row in matched_rows)
    non_event_labels = matched - event_labels
    tp = sum(int(row.get("is_event") or 0) == 1 and bool(row.get("predicted_event_type")) for row in matched_rows)
    fn = sum(int(row.get("is_event") or 0) == 1 and not row.get("predicted_event_type") for row in matched_rows)
    fp = sum(int(row.get("is_event") or 0) == 0 and bool(row.get("predicted_event_type")) for row in matched_rows)
    tn = sum(int(row.get("is_event") or 0) == 0 and not row.get("predicted_event_type") for row in matched_rows)
    event_rows = [row for row in matched_rows if int(row.get("is_event") or 0) == 1]
    event_types = sorted({str(row.get("event_type") or "") for row in event_rows if row.get("event_type")})
    type_correct = sum(
        str(row.get("event_type") or "") == str(row.get("predicted_event_type") or "")
        for row in event_rows
    )
    direction_correct = sum(
        str(row.get("impact_direction") or "") == str(row.get("predicted_direction") or "")
        for row in event_rows
    )
    severity_correct = sum(
        str(row.get("severity") or "") == str(row.get("predicted_severity") or "")
        for row in event_rows
    )
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = _f1(precision, recall)
    sufficient = (
        matched >= MIN_MANUAL_LABELS
        and event_labels >= MIN_MANUAL_EVENT_LABELS
        and non_event_labels >= MIN_MANUAL_NON_EVENT_LABELS
        and len(event_types) >= MIN_MANUAL_EVENT_TYPES
    )
    return {
        "labels": labels,
        "matched_announcements": matched,
        "labelled_events": event_labels,
        "labelled_non_events": non_event_labels,
        "labelled_event_types": event_types,
        "confusion": {"true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn},
        "event_presence_accuracy": _safe_ratio(tp + tn, matched),
        "event_precision": precision,
        "event_recall": recall,
        "event_f1": f1,
        "false_positive_rate": _safe_ratio(fp, fp + tn),
        "false_negative_rate": _safe_ratio(fn, fn + tp),
        "event_type_accuracy": _safe_ratio(type_correct, event_labels),
        "impact_direction_accuracy": _safe_ratio(direction_correct, event_labels),
        "severity_accuracy": _safe_ratio(severity_correct, event_labels),
        "status": "ready" if sufficient else "unmeasured_accuracy",
        "evidence_sufficiency": {
            "minimum_total": MIN_MANUAL_LABELS,
            "minimum_events": MIN_MANUAL_EVENT_LABELS,
            "minimum_non_events": MIN_MANUAL_NON_EVENT_LABELS,
            "minimum_event_types": MIN_MANUAL_EVENT_TYPES,
        },
        "limitation": "需满足分层人工标注数量、正负例和事件类型覆盖后，准确率、漏检率及字段正确率才可作为完整验收证据。",
    }


def _safe_ratio(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 6)


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall <= 0:
        return None
    return round(2 * precision * recall / (precision + recall), 6)


def _wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> dict[str, float] | None:
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return {"lower": round(max(0.0, centre - margin), 6), "upper": round(min(1.0, centre + margin), 6)}


def sync_cninfo_category_reference_labels(
    *,
    start_date: str,
    end_date: str,
    target_per_type: int = 100,
    page_size: int = 50,
    max_pages_per_category: int = 4,
    reference_contract: str = CNINFO_CATEGORY_REFERENCE_CONTRACT,
    benchmark_role: str = "development_non_promotable",
) -> dict[str, Any]:
    """Build a source-native reference set without using local title rules.

    CNINFO category filters are provider metadata selected by the disclosure
    platform.  They are independent from ``classify_announcement_event``.
    Only three category families have a defensible mapping to the local event
    vocabulary; all other event types deliberately remain unmeasured.  The
    provider's "业绩预告" family includes both forecasts and earnings flashes,
    so both local types are accepted instead of pretending it is a finer label.
    """

    from app.data_platform.services.data_center import (  # local import avoids route import cycles
        SOURCE_CNINFO,
        _fetch_cninfo_announcement_page,
        _upsert_announcements,
        ensure_data_center_seeded,
    )
    from app.data_platform.db import init_db

    init_db()
    ensure_data_center_seeded()
    contract = str(reference_contract or CNINFO_CATEGORY_REFERENCE_CONTRACT).strip()
    if not contract.startswith("alis_cninfo_official_category_reference"):
        raise ValueError("unsupported CNINFO category reference contract")
    role = str(benchmark_role or "development_non_promotable").strip()
    if role not in {"development_non_promotable", "frozen_holdout"}:
        raise ValueError("benchmark_role must be development_non_promotable or frozen_holdout")
    if role == "frozen_holdout":
        if contract != CNINFO_CATEGORY_HOLDOUT_CONTRACT:
            raise ValueError("frozen holdout must use the registered holdout contract")
        if str(start_date).replace("-", "") != "20240101" or str(end_date).replace("-", "") != "20241231":
            raise ValueError("registered frozen holdout period is exactly 2024-01-01 through 2024-12-31")
    target = max(MIN_PROVIDER_REFERENCE_PER_TYPE, min(int(target_per_type), 500))
    # Values above 30 make the provider silently repeat page one.
    size = max(1, min(int(page_size), 30))
    max_pages = max(1, min(int(max_pages_per_category), 20))
    collected: dict[str, dict[str, Any]] = {}
    ambiguous_ids: set[str] = set()
    fetch_calls = 0

    for spec in CNINFO_CATEGORY_REFERENCE_SPECS:
        event_type = str(spec["event_type"])
        allowed_local_event_types = tuple(spec["allowed_local_event_types"])
        categories = tuple(spec["categories"])
        per_category_target = math.ceil(target / len(categories))
        event_type_count = 0
        for category, category_name in categories:
            category_count = 0
            for page_num in range(1, max_pages + 1):
                rows = _fetch_cninfo_announcement_page(
                    start_date=start_date,
                    end_date=end_date,
                    ts_code=None,
                    page_num=page_num,
                    page_size=size,
                    category=category,
                )
                fetch_calls += 1
                if not rows:
                    break
                for row in rows:
                    announcement_id = str(row.get("announcement_id") or "")
                    if not announcement_id:
                        continue
                    existing = collected.get(announcement_id)
                    if existing and existing["event_type"] != event_type:
                        ambiguous_ids.add(announcement_id)
                        continue
                    if existing:
                        continue
                    collected[announcement_id] = {
                        **row,
                        "event_type": event_type,
                        "allowed_local_event_types": allowed_local_event_types,
                        "official_category": category,
                        "official_category_name": category_name,
                    }
                    category_count += 1
                    event_type_count += 1
                    if category_count >= per_category_target or event_type_count >= target:
                        break
                # CNINFO currently caps responses at 30 rows even when the
                # requested page size is 50.  A short non-empty page therefore
                # is not proof that pagination is exhausted.
                if category_count >= per_category_target or event_type_count >= target:
                    break
                # Keep provider load gentle and reduce short-term anti-bot responses.
                time.sleep(0.35)
            if event_type_count >= target:
                break

    for announcement_id in ambiguous_ids:
        collected.pop(announcement_id, None)
    rows = list(collected.values())
    written, events = _upsert_announcements(rows)
    now = _now()
    with _conn() as conn:
        for row in rows:
            evidence = {
                "official_category": row["official_category"],
                "official_category_name": row["official_category_name"],
                "provider_type_codes": row.get("provider_type_codes") or "",
                "provider_type_name": row.get("provider_type_name") or "",
                "allowed_local_event_types": list(row["allowed_local_event_types"]),
                "query_period": {"start_date": start_date, "end_date": end_date},
                "independent_of_local_title_rules": True,
                "benchmark_role": role,
            }
            conn.execute(
                """INSERT INTO dc_announcement_reference_label
                   (source_code,announcement_id,reference_contract,reference_source,official_category,
                    is_event,event_type,ts_code,publish_time,evidence_json,available_time,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_code,announcement_id,reference_contract) DO UPDATE SET
                     reference_source=excluded.reference_source,official_category=excluded.official_category,
                     is_event=excluded.is_event,event_type=excluded.event_type,ts_code=excluded.ts_code,
                     publish_time=excluded.publish_time,evidence_json=excluded.evidence_json,
                     available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (
                    SOURCE_CNINFO,
                    row["announcement_id"],
                    contract,
                    CNINFO_CATEGORY_REFERENCE_SOURCE,
                    row["official_category"],
                    1,
                    row["event_type"],
                    row.get("ts_code") or "",
                    row.get("publish_time") or "",
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
    return {
        "ok": True,
        "contract": contract,
        "benchmark_role": role,
        "reference_rows": len(rows),
        "raw_rows_written": written,
        "rule_events_created": events,
        "ambiguous_rows_excluded": len(ambiguous_ids),
        "fetch_calls": fetch_calls,
        "target_per_type": target,
        "event_type_counts": _count_values(row["event_type"] for row in rows),
        "benchmark": announcement_provider_reference_benchmark(
            reference_contract=contract,
            benchmark_role=role,
        ),
    }


def _count_values(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def announcement_provider_reference_benchmark(
    *,
    reference_contract: str = CNINFO_CATEGORY_REFERENCE_CONTRACT,
    benchmark_role: str = "development_non_promotable",
) -> dict[str, Any]:
    """Evaluate persisted rules on the covered CNINFO provider taxonomy."""

    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT r.announcement_id,r.event_type AS expected_event_type,r.official_category,
                      r.evidence_json,a.id AS raw_id,a.title,e.event_type AS predicted_event_type
                 FROM dc_announcement_reference_label r
                 LEFT JOIN dc_announcement_raw a
                   ON a.source_code=r.source_code AND a.announcement_id=r.announcement_id
                 LEFT JOIN dc_announcement_event e ON e.announcement_id=a.id
                WHERE r.reference_contract=?""",
            (reference_contract,),
        ).fetchall()]
    # Defensive de-duplication in case a legacy database contains multiple
    # derived event rows for the same announcement.
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(str(row.get("announcement_id") or ""), row)
    rows = list(unique.values())
    matched_rows = [row for row in rows if row.get("raw_id") is not None]
    expected_types = sorted({str(row.get("expected_event_type") or "") for row in rows if row.get("expected_event_type")})
    allowed_by_reference_type = {
        str(spec["event_type"]): set(spec["allowed_local_event_types"])
        for spec in CNINFO_CATEGORY_REFERENCE_SPECS
    }
    per_type: dict[str, dict[str, Any]] = {}
    exact_correct = 0
    predicted_present = 0
    for event_type in expected_types:
        references = [row for row in matched_rows if row.get("expected_event_type") == event_type]
        allowed_types = allowed_by_reference_type.get(event_type, {event_type})
        predicted_as_type = [row for row in matched_rows if row.get("predicted_event_type") in allowed_types]
        true_positive = sum(row.get("predicted_event_type") in allowed_types for row in references)
        precision = _safe_ratio(true_positive, len(predicted_as_type))
        recall = _safe_ratio(true_positive, len(references))
        per_type[event_type] = {
            "references": len(references),
            "allowed_local_event_types": sorted(allowed_types),
            "true_positive": true_positive,
            "predicted_as_type": len(predicted_as_type),
            "precision_within_covered_reference_universe": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "recall_wilson_95": _wilson_interval(true_positive, len(references)),
        }
        exact_correct += true_positive
    predicted_present = sum(bool(row.get("predicted_event_type")) for row in matched_rows)
    reference_counts = {event_type: per_type[event_type]["references"] for event_type in expected_types}
    sufficiently_sized = (
        len(rows) >= MIN_PROVIDER_REFERENCE_LABELS
        and len(expected_types) >= len(CNINFO_CATEGORY_REFERENCE_SPECS)
        and all(count >= MIN_PROVIDER_REFERENCE_PER_TYPE for count in reference_counts.values())
    )
    precisions = [item["precision_within_covered_reference_universe"] for item in per_type.values() if item["precision_within_covered_reference_universe"] is not None]
    recalls = [item["recall"] for item in per_type.values() if item["recall"] is not None]
    return {
        "contract": reference_contract,
        "benchmark_role": benchmark_role,
        "status": (
            "covered_type_holdout_ready"
            if sufficiently_sized and benchmark_role == "frozen_holdout"
            else "development_reference_ready_non_promotable"
            if sufficiently_sized
            else "insufficient_reference_labels"
        ),
        "reference_rows": len(rows),
        "matched_announcements": len(matched_rows),
        "covered_event_types": expected_types,
        "reference_counts": reference_counts,
        "prediction_presence_recall": _safe_ratio(predicted_present, len(matched_rows)),
        "reference_family_accuracy": _safe_ratio(exact_correct, len(matched_rows)),
        "macro_precision_within_covered_reference_universe": round(sum(precisions) / len(precisions), 6) if precisions else None,
        "macro_recall": round(sum(recalls) / len(recalls), 6) if recalls else None,
        "per_type": per_type,
        "full_acceptance_status": "blocked_by_manual_gold_and_field_truth",
        "limitation": (
            "巨潮官方分类只覆盖业绩预告、权益分派和定期报告三类正例；"
            "它可以独立验证这些类别的分类召回和类别混淆，但不能测量全市场非事件误报、"
            "其余事件类型漏检或正文数值字段准确率，也不能替代分层人工金标准。"
        ),
    }


def _stable_review_rank(announcement_id: str) -> str:
    return hashlib.sha256(
        f"{MANUAL_REVIEW_QUEUE_CONTRACT}|{announcement_id}".encode("utf-8")
    ).hexdigest()


def materialize_announcement_manual_review_queue(*, target_size: int = 320) -> dict[str, Any]:
    """Freeze a stratified, non-provider-reference queue for human review.

    The queue contains model/rule predictions only as sampling strata.  They
    are never copied into the manual label table and cannot satisfy the gold
    benchmark until a reviewer submits independent labels.
    """

    from app.data_platform.db import init_db

    init_db()
    target = max(MIN_MANUAL_LABELS, min(int(target_size), 1000))
    with _conn() as conn:
        candidates = [dict(row) for row in conn.execute(
            """SELECT a.source_code,a.announcement_id,a.ts_code,a.title,a.publish_time,
                      COALESCE(e.event_type,'') predicted_event_type
                 FROM dc_announcement_raw a
                 LEFT JOIN dc_announcement_event e
                   ON e.id=(SELECT MIN(e2.id) FROM dc_announcement_event e2 WHERE e2.announcement_id=a.id)
                WHERE COALESCE(a.title,'')<>''
                  AND NOT EXISTS (
                        SELECT 1 FROM dc_announcement_label l
                         WHERE l.source_code=a.source_code AND l.announcement_id=a.announcement_id
                  )
                  AND NOT EXISTS (
                        SELECT 1 FROM dc_announcement_reference_label r
                         WHERE r.source_code=a.source_code AND r.announcement_id=a.announcement_id
                  )"""
        ).fetchall()]

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        event_type = str(row.get("predicted_event_type") or "")
        stratum = f"predicted:{event_type}" if event_type else "unclassified_candidate"
        row["stratum"] = stratum
        groups.setdefault(stratum, []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: _stable_review_rank(str(row.get("announcement_id") or "")))

    selected: list[dict[str, Any]] = []
    selected_ids: set[tuple[str, str]] = set()

    def take(row: dict[str, Any]) -> None:
        key = (str(row.get("source_code") or ""), str(row.get("announcement_id") or ""))
        if key in selected_ids or len(selected) >= target:
            return
        selected_ids.add(key)
        selected.append(row)

    # Oversample unclassified rows because these are the only candidates that
    # can reveal false negatives and supply genuine non-event controls.
    unclassified_target = max(MIN_MANUAL_NON_EVENT_LABELS * 2, round(target * 0.4))
    for row in groups.get("unclassified_candidate", [])[:unclassified_target]:
        take(row)

    event_strata = sorted(key for key in groups if key.startswith("predicted:"))
    depth = 0
    while len(selected) < target and event_strata:
        progressed = False
        for stratum in event_strata:
            rows = groups[stratum]
            if depth < len(rows):
                take(rows[depth])
                progressed = True
                if len(selected) >= target:
                    break
        if not progressed:
            break
        depth += 1

    if len(selected) < target:
        leftovers = sorted(candidates, key=lambda row: _stable_review_rank(str(row.get("announcement_id") or "")))
        for row in leftovers:
            take(row)
            if len(selected) >= target:
                break

    now = _now()
    with _conn() as conn:
        for rank, row in enumerate(selected, start=1):
            conn.execute(
                """INSERT INTO dc_announcement_label_review_queue
                   (queue_contract,source_code,announcement_id,stratum,predicted_event_type,
                    selection_rank,status,selected_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(queue_contract,source_code,announcement_id) DO NOTHING""",
                (
                    MANUAL_REVIEW_QUEUE_CONTRACT,
                    row.get("source_code") or "",
                    row.get("announcement_id") or "",
                    row["stratum"],
                    row.get("predicted_event_type") or "",
                    rank,
                    "pending",
                    now,
                    now,
                ),
            )
    return {
        "ok": True,
        "contract": MANUAL_REVIEW_QUEUE_CONTRACT,
        "eligible_candidates": len(candidates),
        "selected_this_call": len(selected),
        "target_size": target,
        "selection_strata": _count_values(row["stratum"] for row in selected),
        "status": announcement_manual_review_queue_status(include_items=False),
    }


def announcement_manual_review_queue_status(
    *,
    include_items: bool = False,
    include_benchmark: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    cap = max(1, min(int(limit), 500))
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT q.queue_contract,q.source_code,q.announcement_id,q.stratum,
                      q.predicted_event_type,q.selection_rank,q.status,q.selected_at,
                      a.ts_code,a.title,a.publish_time,a.url,
                      CASE WHEN l.announcement_id IS NULL THEN 0 ELSE 1 END AS has_manual_label
                 FROM dc_announcement_label_review_queue q
                 JOIN dc_announcement_raw a
                   ON a.source_code=q.source_code AND a.announcement_id=q.announcement_id
                 LEFT JOIN dc_announcement_label l
                   ON l.source_code=q.source_code AND l.announcement_id=q.announcement_id
                WHERE q.queue_contract=?
                ORDER BY q.selection_rank ASC""",
            (MANUAL_REVIEW_QUEUE_CONTRACT,),
        ).fetchall()]
    reviewed = sum(int(row.get("has_manual_label") or 0) == 1 for row in rows)
    strata = _count_values(row.get("stratum") for row in rows)
    result = {
        "contract": MANUAL_REVIEW_QUEUE_CONTRACT,
        "status": "ready_for_human_review" if len(rows) >= MIN_MANUAL_LABELS else "insufficient_queue",
        "selected": len(rows),
        "reviewed": reviewed,
        "pending": len(rows) - reviewed,
        "strata": strata,
        "policy": "predictions_are_sampling_hints_only_and_never_become_gold_without_independent_review",
    }
    # The research-assets audit already computes this benchmark once.  Keep
    # the historical default for callers that need a standalone queue report,
    # but allow aggregate readers to avoid opening a second connection and
    # repeating the same join.
    if include_benchmark:
        result["manual_gold"] = announcement_label_benchmark()
    if include_items:
        result["items"] = rows[:cap]
    return result
