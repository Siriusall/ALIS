from __future__ import annotations

from app.core.sql_compat import qmark_to_pyformat


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.calls.append((sql, params))


class _RecordingRaw:
    def __init__(self) -> None:
        self.cursor_instance = _RecordingCursor()

    def cursor(self) -> _RecordingCursor:
        return self.cursor_instance


def test_qmark_adapter_preserves_percent_literals_and_converts_parameters() -> None:
    sql = "SELECT * FROM t WHERE code=? AND provenance NOT LIKE 'legacy_%' AND name LIKE 'a%'"
    assert qmark_to_pyformat(sql) == (
        "SELECT * FROM t WHERE code=%s AND provenance NOT LIKE 'legacy_%%' "
        "AND name LIKE 'a%%'"
    )


def test_qmark_adapter_keeps_existing_psycopg_placeholders() -> None:
    assert qmark_to_pyformat("SELECT %s, %b, %t, %% WHERE id=?") == (
        "SELECT %s, %b, %t, %% WHERE id=%s"
    )


def test_qmark_adapter_does_not_rewrite_question_mark_in_literals_or_comments() -> None:
    sql = "SELECT '?' AS q -- ? 50%\n/* ? 25% */ WHERE id=?"
    assert qmark_to_pyformat(sql) == "SELECT '?' AS q -- ? 50%%\n/* ? 25%% */ WHERE id=%s"


def test_qmark_adapter_does_not_rewrite_question_mark_in_dollar_quoted_literals() -> None:
    sql = "SELECT $$? 50% $$ AS plain, $tag$? 25%$tag$ AS tagged WHERE id=?"
    assert qmark_to_pyformat(sql) == (
        "SELECT $$? 50%% $$ AS plain, $tag$? 25%%$tag$ AS tagged WHERE id=%s"
    )


def test_qmark_adapter_keeps_backslash_escaped_quote_inside_e_string() -> None:
    sql = r"SELECT E'can\'t ? 50%' AS value WHERE id=?"
    assert qmark_to_pyformat(sql) == r"SELECT E'can\'t ? 50%%' AS value WHERE id=%s"


def test_business_postgres_wrapper_uses_safe_conversion() -> None:
    from app.db.store import _ConnWrapper

    raw = _RecordingRaw()
    wrapper = _ConnWrapper(raw, is_mysql=False, is_postgres=True)
    wrapper.execute("SELECT * FROM t WHERE code=? AND name LIKE 'a%'", ("000001.SZ",))

    assert raw.cursor_instance.calls == [
        ("SELECT * FROM t WHERE code=%s AND name LIKE 'a%%'", ("000001.SZ",))
    ]


def test_data_platform_postgres_wrapper_uses_safe_conversion() -> None:
    from app.data_platform.db import Connection

    raw = _RecordingRaw()
    wrapper = Connection(raw, is_postgres=True)
    wrapper.execute("SELECT * FROM t WHERE code=? AND name LIKE '%需求%'", ("000001.SZ",))

    assert raw.cursor_instance.calls == [
        ("SELECT * FROM t WHERE code=%s AND name LIKE '%%需求%%'", ("000001.SZ",))
    ]
