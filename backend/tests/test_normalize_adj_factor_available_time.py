from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "normalize_adj_factor_available_time.py"
SPEC = importlib.util.spec_from_file_location("normalize_adj_factor_available_time", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


class _Result:
    def __init__(self, *, dates: bool = False) -> None:
        self._dates = dates

    def fetchall(self):
        return [{"cal_date": "20240102"}] if self._dates else []

    def fetchone(self):
        return {"count": 1}


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql, _params=()):
        statement = " ".join(str(sql).split())
        self.statements.append(statement)
        if "SELECT cal_date" in statement:
            return _Result(dates=True)
        if "SELECT COUNT(*)" in statement:
            return _Result()
        raise AssertionError(f"unexpected mutating SQL: {statement}")


def _run(monkeypatch, *args: str):
    connection = _Connection()

    @contextmanager
    def fake_conn():
        yield connection

    monkeypatch.setattr(SCRIPT, "_conn", fake_conn)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH), *args])
    return SCRIPT.main(), connection


def test_execute_refuses_to_rewrite_immutable_observation_time(monkeypatch, capsys) -> None:
    return_code, connection = _run(
        monkeypatch, "--execute", "--start-date", "20240102", "--end-date", "20240102",
    )

    assert return_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused_immutable_rewrite"
    assert payload["reason"] == "adj_factor_available_time_is_immutable"
    assert payload["row_count"] == 1
    assert not any(statement.startswith("UPDATE ") for statement in connection.statements)


def test_audit_mode_reports_late_rows_without_mutation(monkeypatch, capsys) -> None:
    return_code, connection = _run(
        monkeypatch, "--start-date", "20240102", "--end-date", "20240102",
    )

    assert return_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "audit"
    assert payload["date_count"] == 1
    assert not any("UPDATE dc_tushare_raw_records" in statement for statement in connection.statements)
