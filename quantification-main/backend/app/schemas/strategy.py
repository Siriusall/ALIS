from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


StrategyType = Literal["formula", "code", "preset"]


class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1)
    type: StrategyType
    content: Any  # dict for formula/preset, str for code


class StrategyUpdate(BaseModel):
    name: str | None = Field(None, min_length=1)
    type: StrategyType | None = None
    content: Any | None = None


class StrategyResponse(BaseModel):
    id: int
    name: str
    type: str
    content: Any
    created_at: str
    updated_at: str
