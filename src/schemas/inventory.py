"""Schemas for POST /api/v1/reserve and POST /api/v1/unreserve (B2B-8)."""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReserveItem(BaseModel):
    sku_id: UUID
    quantity: int = Field(..., gt=0)


class ReserveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    items: List[ReserveItem] = Field(..., min_length=1)


class ReserveItemResult(BaseModel):
    sku_id: UUID
    quantity: int
    active_quantity: int
    reserved_quantity: int


class FailedReserveItem(BaseModel):
    sku_id: UUID
    requested: int
    available: int
    reason: str = "INSUFFICIENT_STOCK"


class ReserveResponse(BaseModel):
    reserved: bool
    items: List[ReserveItemResult] = []
    failed_items: List[FailedReserveItem] = []


class UnreserveItem(BaseModel):
    sku_id: UUID
    quantity: int = Field(..., gt=0)


class UnreserveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: UUID
    items: List[UnreserveItem] = Field(..., min_length=1)


class UnreserveResponse(BaseModel):
    ok: bool = True
