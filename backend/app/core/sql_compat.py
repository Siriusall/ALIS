"""Small SQL placeholder adapter used by the PostgreSQL compatibility layer.

ALIS domain queries use SQLite's qmark placeholders (``?``) so the same
queries can run in the isolated test database and in the production
PostgreSQL database.  A plain ``str.replace('?', '%s')`` is not sufficient:
psycopg also parses percent signs in SQL literals, so a predicate such as
``LIKE 'legacy_%'`` becomes an invalid placeholder.  This module performs a
minimal lexical conversion and leaves quoted SQL text intact for the server
while escaping percent signs for psycopg.
"""

from __future__ import annotations


def qmark_to_pyformat(sql: str) -> str:
    """Convert qmark parameters to psycopg placeholders safely.

    Percent signs are escaped everywhere except an existing psycopg
    placeholder (``%s``, ``%b`` or ``%t``) and an already escaped ``%%``.
    Escaping is also applied inside quoted literals because psycopg parses
    the complete query before PostgreSQL sees it.  SQL comments, quoted
    identifiers, and PostgreSQL dollar-quoted literals are treated as literal
    text; a question mark inside any of them is never a bind parameter.
    """

    text = str(sql)
    out: list[str] = []
    i = 0
    n = len(text)
    single_quote = False
    double_quote = False
    line_comment = False
    block_comment = False
    dollar_quote: str | None = None

    def dollar_delimiter(start: int) -> str | None:
        """Return a PostgreSQL dollar-quote delimiter beginning at ``start``."""
        if text[start:start + 1] != "$":
            return None
        end = start + 1
        if end < n and text[end] == "$":
            return "$$"
        if end >= n or not (text[end].isalpha() or text[end] == "_"):
            return None
        end += 1
        while end < n and (text[end].isalnum() or text[end] == "_"):
            end += 1
        if end < n and text[end] == "$":
            return text[start:end + 1]
        return None

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if dollar_quote is not None:
            if text.startswith(dollar_quote, i):
                out.append(dollar_quote)
                i += len(dollar_quote)
                dollar_quote = None
                continue
            # Psycopg still scans percent signs in the complete query, so
            # preserve the existing escaping rule while leaving ? literal.
            out.append("%%" if ch == "%" else ch)
            i += 1
            continue

        if line_comment:
            out.append("%%" if ch == "%" else ch)
            if ch == "\n":
                line_comment = False
            i += 1
            continue

        if block_comment:
            if ch == "*" and nxt == "/":
                out.extend(("*", "/"))
                i += 2
                block_comment = False
                continue
            out.append("%%" if ch == "%" else ch)
            i += 1
            continue

        if not single_quote and not double_quote and ch == "-" and nxt == "-":
            out.extend(("-", "-"))
            i += 2
            line_comment = True
            continue
        if not single_quote and not double_quote and ch == "/" and nxt == "*":
            out.extend(("/", "*"))
            i += 2
            block_comment = True
            continue

        if single_quote:
            if ch == "\\" and nxt:
                # PostgreSQL E'' strings allow a backslash-escaped quote.
                # Copy both characters so the quote cannot terminate the
                # lexical state prematurely.
                out.extend((ch, nxt))
                i += 2
                continue
            if ch == "'":
                if nxt == "'":  # SQL escaped quote
                    out.extend(("'", "'"))
                    i += 2
                    continue
                single_quote = False
            out.append("%%" if ch == "%" else ch)
            i += 1
            continue

        if double_quote:
            if ch == '"':
                if nxt == '"':  # SQL escaped identifier quote
                    out.extend(('"', '"'))
                    i += 2
                    continue
                double_quote = False
            out.append("%%" if ch == "%" else ch)
            i += 1
            continue

        if ch == "'":
            single_quote = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            double_quote = True
            out.append(ch)
            i += 1
            continue
        if ch == "$":
            delimiter = dollar_delimiter(i)
            if delimiter is not None:
                out.append(delimiter)
                i += len(delimiter)
                dollar_quote = delimiter
                continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        if ch == "%":
            if nxt in {"s", "b", "t", "%"}:
                out.extend(("%", nxt))
                i += 2
            else:
                out.extend(("%", "%"))
                i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)
