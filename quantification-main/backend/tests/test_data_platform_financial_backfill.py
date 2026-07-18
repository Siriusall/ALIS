from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data_platform import db  # noqa: E402
from app.data_platform.services.data_center import (  # noqa: E402
    _load_financial_missing_codes,
    _record_failed_tushare_request,
    _resolve_failed_tushare_request,
    classify_tushare_error,
    list_failed_tushare_requests,
    sync_financial_batch,
    sync_market_bundle,
)
from app.data_platform.services.assets import _report_periods  # noqa: E402


class FinancialBackfillSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        raw = sqlite3.connect(db.DB_PATH)
        raw.executescript(
            """
            CREATE TABLE dc_stock_master (ts_code TEXT PRIMARY KEY);
            CREATE TABLE dc_tushare_raw_records (
                api_name TEXT NOT NULL,
                period TEXT NOT NULL,
                ts_code TEXT NOT NULL
            );
            CREATE TABLE dc_tushare_request_coverage (
                api_name TEXT NOT NULL,
                period TEXT NOT NULL,
                ts_code TEXT NOT NULL,
                result_status TEXT NOT NULL
            );
            INSERT INTO dc_stock_master VALUES ('000001.SZ'), ('000002.SZ'), ('000003.SZ');
            """
        )
        raw.commit()
        raw.close()

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_next_batch_skips_symbols_already_completed_for_period(self) -> None:
        self.assertEqual(
            _load_financial_missing_codes("20260331", limit=2, completion_api="income"),
            ["000001.SZ", "000002.SZ"],
        )
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO dc_tushare_raw_records VALUES (?, ?, ?)",
                ("income", "20260331", "000001.SZ"),
            )
            conn.execute(
                "INSERT INTO dc_tushare_raw_records VALUES (?, ?, ?)",
                ("income", "20251231", "000002.SZ"),
            )
        self.assertEqual(
            _load_financial_missing_codes("20260331", limit=2, completion_api="income"),
            ["000002.SZ", "000003.SZ"],
        )

    def test_annual_completion_can_use_audit_api(self) -> None:
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO dc_tushare_raw_records VALUES (?, ?, ?)",
                ("fina_audit", "20231231", "000001.SZ"),
            )
        self.assertEqual(
            _load_financial_missing_codes("20231231", limit=3, completion_api="fina_audit"),
            ["000002.SZ", "000003.SZ"],
        )

    def test_annual_completion_skips_completed_request_coverage(self) -> None:
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO dc_tushare_request_coverage VALUES (?, ?, ?, ?)",
                ("fina_audit", "20251231", "000001.SZ", "not_applicable"),
            )
            conn.execute(
                "INSERT INTO dc_tushare_request_coverage VALUES (?, ?, ?, ?)",
                ("fina_audit", "20251231", "000002.SZ", "deferred_empty"),
            )
        self.assertEqual(
            _load_financial_missing_codes("20251231", limit=3, completion_api="fina_audit"),
            ["000003.SZ"],
        )

    def test_empty_pending_is_sorted_after_never_checked_symbols(self) -> None:
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO dc_tushare_request_coverage VALUES (?, ?, ?, ?)",
                ("fina_audit", "20251231", "000001.SZ", "empty_pending"),
            )
        self.assertEqual(
            _load_financial_missing_codes("20251231", limit=3, completion_api="fina_audit"),
            ["000002.SZ", "000003.SZ", "000001.SZ"],
        )


class TushareErrorClassificationTest(unittest.TestCase):
    def test_categories_drive_distinct_recovery_policies(self) -> None:
        cases = {
            "HTTP 401 invalid token": "authentication",
            "抱歉，您没有接口访问权限": "permission",
            "HTTP 429 too many requests": "rate_limit",
            "connection timed out": "transient",
            "invalid parameter trade_date": "invalid_request",
            "unexpected upstream response": "unknown",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_tushare_error(RuntimeError(message)), expected)


class FailedRequestPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "queue.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_same_failure_is_deduplicated_and_can_be_resolved(self) -> None:
        params = {"trade_date": "20260710"}
        _record_failed_tushare_request("daily", params, None, "transient", "timeout")
        _record_failed_tushare_request("daily", params, None, "transient", "timeout again")
        pending = list_failed_tushare_requests(status="pending")
        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["items"][0]["attempts"], 2)
        _resolve_failed_tushare_request("daily", params, None)
        self.assertEqual(list_failed_tushare_requests(status="pending")["total"], 0)
        self.assertEqual(list_failed_tushare_requests(status="resolved")["total"], 1)


class PlanningAndBundleCorrectnessTest(unittest.TestCase):
    def test_report_period_waits_for_disclosure_deadline(self) -> None:
        self.assertEqual(_report_periods(date(2026, 7, 13))[0], "20260331")
        self.assertEqual(_report_periods(date(2026, 9, 1))[0], "20260630")

    @patch("app.data_platform.services.data_center.build_stock_daily_snapshot", return_value={"ok": True})
    @patch("app.data_platform.services.data_center._safe_sync_raw")
    def test_market_bundle_propagates_sub_api_failure_and_only_calls_requested(self, sync, _snapshot) -> None:
        sync.return_value = {"ok": False, "api": "daily_basic", "error": "timeout"}
        result = sync_market_bundle("20260710", apis=["daily_basic"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["apis"], ["daily_basic"])
        sync.assert_called_once_with("daily_basic", {"trade_date": "20260710"})

    @patch("app.data_platform.services.data_center.build_stock_daily_snapshot", return_value={"ok": True})
    @patch("app.data_platform.services.data_center._safe_sync_raw", return_value={"ok": True})
    def test_market_bundle_checks_trading_constraints_and_moneyflow_by_default(self, sync, _snapshot) -> None:
        result = sync_market_bundle("20260710")
        self.assertTrue(result["ok"])
        self.assertIn("suspend_d", result["apis"])
        self.assertIn("moneyflow", result["apis"])
        sync.assert_any_call("suspend_d", {"trade_date": "20260710"})
        sync.assert_any_call("moneyflow", {"trade_date": "20260710"})

    @patch("app.data_platform.services.data_center.ensure_data_center_seeded")
    @patch("app.data_platform.services.data_center._load_financial_missing_codes", return_value=["000001.SZ"])
    @patch("app.data_platform.services.data_center.sync_financial_bundle")
    @patch("app.data_platform.services.data_center._parallel_tushare_map")
    def test_audit_backfill_does_not_repeat_core_or_main_business(self, parallel, bundle, _missing, _seed) -> None:
        bundle.return_value = {"ok": True, "results": []}
        parallel.side_effect = lambda targets, fn, **_kwargs: ([fn(code) for code in targets], 1)

        result = sync_financial_batch(
            "20241231",
            limit=20,
            include_yearly=True,
            completion_api="fina_audit",
        )

        self.assertTrue(result["ok"])
        bundle.assert_called_once_with(
            "000001.SZ",
            "20241231",
            include_yearly=False,
            apis_override=["fina_audit"],
        )


if __name__ == "__main__":
    unittest.main()
