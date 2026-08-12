from uuid import uuid4

from app.db import store
from app.services.candidate_lifecycle import (
    get_candidate_lifecycle,
    reconcile_existing_candidates,
    transition_candidate_lifecycle,
)


def _delete_test_version(version_id: int) -> None:
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


def test_new_candidate_starts_observing_without_production_permission():
    version_id = store.create_model_version(
        model_key="test_immediate_shadow_model",
        version=f"v-{uuid4().hex}",
        status="candidate",
        artifact_path="builtin://test/immediate-shadow",
        config={
            "candidate_type": "model",
            "change_type": "performance_tuning",
            "lineage": {
                "root_experiment_id": "experiment-1",
                "hypothesis_family": "ranking_quality",
            },
            "changed_fields": ["learning_rate"],
        },
        metrics={},
        validation={"status": "candidate", "production_eligible": False},
        is_active=False,
    )

    try:
        lifecycle = get_candidate_lifecycle(version_id)

        assert lifecycle is not None
        assert lifecycle["runtime_status"] == "observing"
        assert lifecycle["eligibility_status"] == "ineligible"
        assert lifecycle["change_type"] == "performance_tuning"
        assert lifecycle["production_change_forbidden"] in (1, True)
        assert lifecycle["changed_fields"] == ["learning_rate"]
        assert lifecycle["events"][0]["event_type"] == "candidate_observation_started"
        with store._conn() as conn:
            account = dict(conn.execute(
                "SELECT * FROM candidate_simulation_accounts WHERE model_version_id=?", (int(version_id),)
            ).fetchone())
        assert account["status"] == "awaiting_first_replay"
        assert float(account["initial_cash"]) == 200000.0
        assert float(account["latest_equity"]) == 200000.0
    finally:
        _delete_test_version(version_id)


def test_active_version_is_not_registered_as_candidate_observation():
    version_id = store.create_model_version(
        model_key="test_active_model",
        version=f"v-{uuid4().hex}",
        status="active",
        artifact_path="builtin://test/active",
        config={},
        metrics={},
        validation={"status": "passed", "production_eligible": True},
        is_active=True,
    )

    try:
        assert get_candidate_lifecycle(version_id) is None
    finally:
        _delete_test_version(version_id)


def test_runtime_stop_does_not_overwrite_version_or_grant_production():
    version_id = store.create_model_version(
        model_key="test_lifecycle_stop",
        version=f"v-{uuid4().hex}",
        status="candidate",
        artifact_path="builtin://test/stop",
        config={"change_type": "bugfix"},
        metrics={},
        validation={"status": "candidate", "production_eligible": False},
        is_active=False,
    )
    try:
        result = transition_candidate_lifecycle(
            version_id,
            runtime_status="stopped",
            event_type="candidate_stopped",
            reason="point_in_time_violation",
            evidence={"hard_failure": True},
        )

        assert result["runtime_status"] == "stopped"
        assert result["eligibility_status"] == "ineligible"
        assert result["production_change_forbidden"] in (1, True)
        assert result["events"][-1]["reason"] == "point_in_time_violation"
        assert store.get_model_version(version_id)["status"] == "candidate"
    finally:
        _delete_test_version(version_id)


def test_reconcile_stops_rejected_candidate_without_overwriting_version():
    version_id = store.create_model_version(
        model_key="test_rejected_candidate",
        version=f"v-{uuid4().hex}",
        status="candidate",
        artifact_path="builtin://test/rejected",
        config={},
        metrics={},
        validation={"status": "rejected", "production_eligible": False},
        is_active=False,
    )
    try:
        result = reconcile_existing_candidates()
        lifecycle = get_candidate_lifecycle(version_id)

        assert version_id in result["stopped_model_version_ids"]
        assert lifecycle["runtime_status"] == "stopped"
        assert lifecycle["stop_reason"] == "rejected"
        assert store.get_model_version(version_id)["status"] == "candidate"
    finally:
        _delete_test_version(version_id)
