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
