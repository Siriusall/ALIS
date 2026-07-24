"""Manual-label benchmark contract for announcement extraction quality."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.data_platform.db import _conn


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
    return {"ok": True, "labels": len(labels), "contract": "announcement_label_benchmark_v1"}


def announcement_label_benchmark() -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) labels,
                      SUM(CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END) matched_announcements,
                      SUM(CASE WHEN a.id IS NOT NULL AND l.is_event=CASE WHEN e.id IS NULL THEN 0 ELSE 1 END THEN 1 ELSE 0 END) event_presence_correct,
                      SUM(CASE WHEN l.is_event=1 AND e.id IS NOT NULL AND l.event_type=e.event_type THEN 1 ELSE 0 END) event_type_correct,
                      SUM(CASE WHEN l.is_event=1 THEN 1 ELSE 0 END) labelled_events
                 FROM dc_announcement_label l
                 LEFT JOIN dc_announcement_raw a ON a.source_code=l.source_code AND a.announcement_id=l.announcement_id
                 LEFT JOIN dc_announcement_event e ON e.announcement_id=a.id"""
        ).fetchone()
    result = dict(row or {})
    labels = int(result.get("labels") or 0)
    matched = int(result.get("matched_announcements") or 0)
    presence = int(result.get("event_presence_correct") or 0)
    event_labels = int(result.get("labelled_events") or 0)
    type_correct = int(result.get("event_type_correct") or 0)
    return {
        "labels": labels,
        "matched_announcements": matched,
        "event_presence_accuracy": round(presence / matched, 4) if matched else None,
        "event_type_accuracy": round(type_correct / event_labels, 4) if event_labels else None,
        "status": "ready" if labels >= 200 and matched >= 200 else "unmeasured_accuracy",
        "limitation": "至少200条与原始公告匹配的分层人工标注后，准确率才可作为验收证据。",
    }
