from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from app.data_platform import db
from app.services.analyst_consensus import build_analyst_consensus


def test_analyst_consensus_revision_window_rolls_with_as_of_time():
    tempdir = tempfile.TemporaryDirectory()
    original_db_path = db.DB_PATH
    db.DB_PATH = Path(tempdir.name) / "consensus.db"
    raw = sqlite3.connect(db.DB_PATH)
    raw.executescript(
        """
        CREATE TABLE dc_tushare_raw_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_name TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        """
    )
    rows = [
        ("report_rc", "000001.SZ", "20260715", {"rating": "买入", "max_price": 12, "org_name": "A"}),
        ("report_rc", "000001.SZ", "20260701", {"rating": "增持", "max_price": 11, "org_name": "B"}),
        ("report_rc", "000001.SZ", "20260610", {"rating": "中性", "max_price": 10, "org_name": "C"}),
        ("report_rc", "000001.SZ", "20260520", {"rating": "卖出", "max_price": 9, "org_name": "D"}),
    ]
    raw.executemany(
        "INSERT INTO dc_tushare_raw_records(api_name,ts_code,trade_date,payload_json) VALUES(?,?,?,?)",
        [(api, code, trade_date, json.dumps(payload, ensure_ascii=False)) for api, code, trade_date, payload in rows],
    )
    raw.commit()
    raw.close()

    try:
        result = build_analyst_consensus("000001.SZ", as_of_time="2026-07-20")
    finally:
        db.DB_PATH = original_db_path
        tempdir.cleanup()

    assert result["revision_window"]["anchor_date"] == "20260720"
    assert result["revision_window"]["recent_start"] == "20260623"
    assert result["revision_window"]["prior_start"] == "20260526"
    assert result["revision_window"]["recent_reports"] == 2
    assert result["revision_window"]["prior_reports"] == 1
    assert result["rating_revision"] == 1.5
