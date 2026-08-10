from contextlib import contextmanager

from app.services import factor_discovery


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


def test_open_factor_registration_is_idempotent(monkeypatch):
    stored = set()

    class Connection:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT id FROM factor_definitions"):
                return _Cursor({"id": 1} if tuple(params) in stored else None)
            if normalized.startswith("INSERT INTO factor_definitions"):
                stored.add((params[0], params[1]))
            return _Cursor()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(factor_discovery, "_conn", fake_conn)
    first = factor_discovery.register_open_factor_candidates()
    second = factor_discovery.register_open_factor_candidates()

    assert first["candidate_count"] == len(factor_discovery.CLASSIC_ALPHA_SEEDS)
    assert len(first["created"]) == len(factor_discovery.CLASSIC_ALPHA_SEEDS)
    assert second["created"] == []
    assert len(second["reused"]) == len(factor_discovery.CLASSIC_ALPHA_SEEDS)
    assert first["rejected"] == []
