from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes import model_strategy
from app.services.model_training import run_model_training


def test_retired_generic_training_entry_cannot_create_a_model_version():
    result = run_model_training(
        run_id=99,
        start_date="20200101",
        end_date="20260728",
        exchange_group="HS",
    )

    assert result["started"] is False
    assert result["status"] == "retired_legacy_generic_model_protocol"
    assert result["production_change_forbidden"] is True


@pytest.mark.parametrize(
    ("call", "args"),
    [
        (model_strategy.model_strategy_promote, (9,)),
        (model_strategy.model_strategy_rollback, (9,)),
        (model_strategy.model_strategy_compare, (8, 9)),
    ],
)
def test_retired_generic_governance_apis_are_gone(call, args):
    with pytest.raises(HTTPException) as exc:
        call(*args)

    assert exc.value.status_code == 410
