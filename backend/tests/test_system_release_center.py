import json

from app.services import system_release_center as releases


def _components(commit="a" * 40):
    return {
        "code.backend": {"version": commit, "commit": commit, "branch": "main", "dirty": False, "available": True},
        "database.sim": {"version": releases.SIM_SCHEMA_HEAD, "expected": releases.SIM_SCHEMA_HEAD},
        "database.data": {"version": releases.DATA_SCHEMA_HEAD, "expected": releases.DATA_SCHEMA_HEAD},
        "strategy.champion": {"version": "champion-v1", "native_table": "model_versions", "native_id": 50},
        "strategy.controller": {"version": "controller-v1", "native_table": "model_versions", "native_id": 31},
        "factor.production_set": {"version": "alis_factor_registry_v1", "bindings": {"momentum_20": {"id": 1}}},
        "config.runtime": {"version": "config-v1", "values": {}},
        **{key: dict(value) for key, value in releases.CONTRACT_CATALOG.items()},
    }


def test_release_manifest_is_stable_and_compatibility_gated(monkeypatch):
    monkeypatch.setattr(releases, "_component_payloads", _components)
    first = releases.build_release_manifest()
    second = releases.build_release_manifest()
    assert first == second
    assert releases.validate_release_manifest(first)["ok"] is True

    broken = json.loads(json.dumps(first))
    broken["components"]["contract.decision_data"]["version"] = "old"
    broken["manifest_hash"] = releases._hash({key: value for key, value in broken.items() if key != "manifest_hash"})
    validation = releases.validate_release_manifest(broken)
    assert validation["ok"] is False
    assert validation["gates"]["component_compatibility"] is False


def test_release_catalog_publishes_immutable_sealed_holdout_contract():
    assert releases.CONTRACT_CATALOG["contract.sealed_holdout"]["version"] == (
        "alis_sealed_strategy_holdout_v4_immutable_observation"
    )


def test_release_creation_is_idempotent_and_only_validated_release_activates(monkeypatch):
    monkeypatch.setattr(releases, "_component_payloads", _components)
    with releases.business_conn() as conn:
        conn.execute("DELETE FROM system_release_events")
        conn.execute("DELETE FROM system_release_components")
        conn.execute("DELETE FROM system_component_dependencies")
        conn.execute("DELETE FROM system_releases")
        conn.execute("DELETE FROM system_component_versions")
        conn.execute("DELETE FROM system_components")
    first = releases.create_release(actor="test")
    second = releases.create_release(actor="test")
    assert first["id"] == second["id"]
    assert first["validation_status"] == "passed"
    active = releases.activate_release(first["id"], actor="test")
    assert active["status"] == "active"
    assert releases.active_release()["id"] == first["id"]


def test_change_discovery_requires_restart_when_repository_commit_differs(monkeypatch):
    monkeypatch.setattr(releases, "active_release", lambda: {"id": 1, "manifest_hash": "old"})
    monkeypatch.setattr(releases, "build_release_manifest", lambda: {"manifest_hash": "new"})
    monkeypatch.setattr(releases, "process_git_state", lambda: {"available": True, "commit": "a" * 40})
    monkeypatch.setattr(releases, "git_state", lambda: {"available": True, "commit": "b" * 40, "dirty": False})
    result = releases.discover_release_change()
    assert result["changed"] is True
    assert result["restart_required"] is True


def test_governance_cycle_is_noop_when_release_is_unchanged(monkeypatch):
    active = {"id": 7, "manifest_hash": "same", "status": "active"}
    monkeypatch.setattr(releases, "discover_release_change", lambda: {
        "changed": False, "restart_required": False,
    })
    monkeypatch.setattr(releases, "active_release", lambda: active)
    result = releases.run_release_governance_cycle()
    assert result["status"] == "unchanged"
    assert result["production_change"] is False
    assert result["active_release"]["id"] == 7


def test_governance_cycle_does_not_create_candidate_for_dirty_worktree(monkeypatch):
    monkeypatch.setattr(releases, "discover_release_change", lambda: {
        "changed": True,
        "restart_required": False,
        "repository_dirty": True,
    })

    def fail_create_release(**kwargs):
        raise AssertionError("dirty worktree must be rejected before candidate creation")

    monkeypatch.setattr(releases, "create_release", fail_create_release)
    result = releases.run_release_governance_cycle()

    assert result["ok"] is False
    assert result["status"] == "worktree_dirty"
    assert result["production_change"] is False
    assert result["reason"] == "repository_worktree_must_be_clean_before_release"


def test_governance_cycle_activates_only_after_preflight(monkeypatch):
    monkeypatch.setattr(releases, "_component_payloads", lambda: _components("b" * 40))
    monkeypatch.setattr(releases, "discover_release_change", lambda: {
        "changed": True, "restart_required": False,
    })
    monkeypatch.setattr(releases, "validate_release_preflight", lambda _: {
        "ok": True, "gates": {"all": True}, "failed_gates": [],
    })
    result = releases.run_release_governance_cycle()
    assert result["status"] == "activated"
    assert result["production_change"] is True
    assert result["active_release"]["status"] == "active"
