from datetime import datetime
from zoneinfo import ZoneInfo

from app.api.routes.health import _frozen_plan_execution_is_due


TZ = ZoneInfo("Asia/Shanghai")


def test_frozen_plan_is_not_due_before_its_precise_execution_time():
    plan = {
        "planned_execute_date": "2026-08-21",
        "planned_execute_at": "2026-08-21T15:01:00+08:00",
    }

    assert not _frozen_plan_execution_is_due(plan, datetime(2026, 8, 21, 12, 0, tzinfo=TZ))
    assert _frozen_plan_execution_is_due(plan, datetime(2026, 8, 21, 15, 1, tzinfo=TZ))


def test_legacy_plan_without_execution_timestamp_only_becomes_due_after_its_date():
    plan = {"planned_execute_date": "20260821"}

    assert not _frozen_plan_execution_is_due(plan, datetime(2026, 8, 21, 23, 59, tzinfo=TZ))
    assert _frozen_plan_execution_is_due(plan, datetime(2026, 8, 22, 0, 0, tzinfo=TZ))
