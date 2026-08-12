from uuid import uuid4

from app.db import store
from app.services.candidate_simulation_ledger import persist_candidate_replay


def _cleanup(version_id: int) -> None:
    with store._conn() as conn:
        accounts = conn.execute(
            "SELECT id FROM candidate_simulation_accounts WHERE model_version_id=?", (int(version_id),)
        ).fetchall()
        for row in accounts:
            account_id = int(dict(row)["id"])
            for table in (
                "candidate_simulation_daily_equity", "candidate_simulation_decisions",
                "candidate_simulation_operations", "candidate_simulation_blocked_events",
            ):
                conn.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM candidate_simulation_accounts WHERE model_version_id=?", (int(version_id),))
        conn.execute("DELETE FROM candidate_lifecycle_events WHERE model_version_id=?", (int(version_id),))
        conn.execute("DELETE FROM candidate_lifecycle_records WHERE model_version_id=?", (int(version_id),))
        conn.execute("DELETE FROM model_versions WHERE id=?", (int(version_id),))


def test_candidate_replay_persists_isolated_auditable_ledger():
    version_id = store.create_model_version(
        model_key="alis_strategy_challenger",
        version=f"test-ledger-{uuid4().hex}",
        status="candidate", artifact_path="builtin://test/candidate-ledger",
        config={"candidate_type": "strategy"}, metrics={},
        validation={"production_eligible": False}, is_active=False,
    )
    replay = {
        "initial_cash": 200000.0,
        "research_package_hash": "frozen-test",
        "component_contract": {"execution": "A-share open constraints + shared fee/slippage rules"},
        "equity_series": [
            {"date": "2026-08-11", "cash": 100000.0, "stock_value": 100000.0, "total_value": 200000.0, "by_symbol": {"000001": 100000.0}},
            {"date": "2026-08-12", "cash": 99990.0, "stock_value": 101000.0, "total_value": 200990.0, "by_symbol": {"000001": 101000.0}},
        ],
        "benchmark_series": [
            {"date": "2026-08-11", "total_value": 200000.0},
            {"date": "2026-08-12", "total_value": 200500.0},
        ],
        "decisions": [{"signal_date": "2026-08-11", "execute_date": "2026-08-12", "status": "optimal", "target_weights": {"000001": 0.5}}],
        "trades": [{"date": "2026-08-12", "symbol": "000001", "side": "BUY", "price": 10.0, "quantity": 10000, "fees": 10.0}],
        "blocked_events": [],
        "data_quality": {"point_in_time": {"passed": True}},
    }
    try:
        with store._conn() as conn:
            placeholder = dict(conn.execute(
                "SELECT id,status FROM candidate_simulation_accounts WHERE model_version_id=?", (int(version_id),)
            ).fetchone())
            production_before = {
                table: int(dict(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone())["n"])
                for table in ("executions", "operations", "positions")
            }
        first = persist_candidate_replay(version_id, replay)
        second = persist_candidate_replay(version_id, replay)
        with store._conn() as conn:
            production_after = {
                table: int(dict(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone())["n"])
                for table in ("executions", "operations", "positions")
            }
            operation_count = int(dict(conn.execute(
                "SELECT COUNT(*) AS n FROM candidate_simulation_operations WHERE account_id=?", (first["account_id"],)
            ).fetchone())["n"])

        assert first["status"] == "passed"
        assert first["account_id"] == int(placeholder["id"])
        assert second["account_id"] == first["account_id"]
        assert second["equity_sessions"] == 2
        assert operation_count == 1
        assert production_after == production_before
        assert first["production_account_effect"] == "none"
    finally:
        _cleanup(version_id)
