from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.store import create_model_train_run
from app.services.model_registry import get_model_status, get_train_run_status
from app.services.model_training import run_model_training

router = APIRouter()


class ModelTrainRequest(BaseModel):
    start_date: str | None = Field(None, description="YYYY-MM-DD，可选")
    end_date: str | None = Field(None, description="YYYY-MM-DD，可选")
    exchange_group: str = Field("HS", description="ALL|HS|CYB|BJ")


@router.get("/model-strategy/status")
def model_strategy_status() -> dict:
    return get_model_status()


@router.get("/model-strategy/train-runs/{run_id}")
def model_strategy_train_run(run_id: int) -> dict:
    row = get_train_run_status(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="训练任务不存在")
    return row


@router.post("/model-strategy/train")
def model_strategy_train(req: ModelTrainRequest) -> dict:
    run_id = create_model_train_run("训练任务已创建")

    def _worker() -> None:
        run_model_training(
            run_id=run_id,
            start_date=req.start_date,
            end_date=req.end_date,
            exchange_group=req.exchange_group,
        )

    threading.Thread(target=_worker, daemon=True, name=f"model-train-{run_id}").start()
    return {"ok": True, "run_id": run_id, "status": "running"}
