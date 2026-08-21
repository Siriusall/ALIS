import os

from app.core.db_connection_gate import (
    DEFAULT_BUSINESS_DB_CONNECTIONS,
    DEFAULT_DATA_PLATFORM_DB_CONNECTIONS,
)
from app.data_platform import db as data_db
from app.db import store


def test_postgres_connection_defaults_are_bounded_across_runtime_processes():
    """The three-process deployment must not multiply defaults past max_connections."""

    assert DEFAULT_BUSINESS_DB_CONNECTIONS == 4
    assert DEFAULT_DATA_PLATFORM_DB_CONNECTIONS == 3
    assert store._BUSINESS_DB_CONNECTION_GATE.limit == int(
        os.getenv("ALIS_BUSINESS_DB_MAX_CONNECTIONS", str(DEFAULT_BUSINESS_DB_CONNECTIONS))
    )
    assert data_db._DATA_PLATFORM_DB_CONNECTION_GATE.limit == int(
        os.getenv("ALIS_DATA_DB_MAX_CONNECTIONS", str(DEFAULT_DATA_PLATFORM_DB_CONNECTIONS))
    )

    # Both databases share one 30-connection PostgreSQL cluster. Three
    # processes consume at most 21 gated sessions; the API's one advisory
    # lease connection leaves five ordinary client slots below the 27-slot
    # non-superuser budget.
    gated_sessions = (
        DEFAULT_BUSINESS_DB_CONNECTIONS + DEFAULT_DATA_PLATFORM_DB_CONNECTIONS
    ) * 3
    assert gated_sessions == 21
    assert (30 - 3) - (gated_sessions + 1) == 5
