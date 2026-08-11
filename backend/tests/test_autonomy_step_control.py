import threading

from app.core.autonomy_step_control import (
    autonomy_step_scope,
    cancel_autonomy_scope,
    cancellation_requested,
    register_postgres_connection,
    unregister_postgres_connection,
)
from app.services import system_autonomy


def test_scope_cancels_all_registered_postgres_connections() -> None:
    class RawConnection:
        def __init__(self) -> None:
            self.cancel_count = 0

        def cancel(self) -> None:
            self.cancel_count += 1

    raw = RawConnection()
    with autonomy_step_scope() as scope_id:
        registration = register_postgres_connection(raw)
        assert cancellation_requested() is False
        assert cancel_autonomy_scope(scope_id) == 1
        assert cancellation_requested() is True
        unregister_postgres_connection(registration)
    assert raw.cancel_count == 1


def test_distributed_step_lease_blocks_second_owner_until_release() -> None:
    name = f"test-distributed-lease-{threading.get_ident()}"
    first = system_autonomy._claim_distributed_step_lease(name, 30)
    try:
        assert first
        assert system_autonomy._claim_distributed_step_lease(name, 30) is None
    finally:
        if first:
            system_autonomy._release_distributed_step_lease(name, first)
    second = system_autonomy._claim_distributed_step_lease(name, 30)
    assert second
    system_autonomy._release_distributed_step_lease(name, second)
