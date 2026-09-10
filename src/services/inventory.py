"""Inventory reserve / unreserve service (B2B-8)."""

from __future__ import annotations

import logging
import uuid
from typing import Iterable
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.inventory import ReserveOperation
from src.models.sku import SKU
from src.schemas.errors import ErrorCode
from src.schemas.inventory import (
    FailedReserveItem,
    ReserveItem,
    ReserveItemResult,
    ReserveRequest,
    ReserveResponse,
    UnreserveItem,
    UnreserveRequest,
    UnreserveResponse,
)
from src.settings import settings

logger = logging.getLogger(__name__)


async def _emit_sku_out_of_stock(sku_id: UUID) -> None:
    """Notify B2C that SKU active_quantity reached zero (fire-and-forget)."""
    url = f"{settings.b2c_service_url.rstrip('/')}/api/v1/events/sku-out-of-stock"
    payload = {
        "event_type": "SKU_OUT_OF_STOCK",
        "sku_id": str(sku_id),
        "idempotency_key": str(uuid.uuid4()),
    }
    headers = {"X-Service-Key": settings.b2c_to_b2b_key}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            logger.info("SKU_OUT_OF_STOCK for %s → %s", sku_id, resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to emit SKU_OUT_OF_STOCK for %s: %s", sku_id, exc)


def _require_service_key(x_service_key: str | None) -> None:
    if not x_service_key or x_service_key != settings.b2c_to_b2b_key:
        raise HTTPException(
            status_code=401,
            detail={
                "code": ErrorCode.UNAUTHORIZED,
                "message": "X-Service-Key is required and must be valid",
            },
        )


async def reserve_skus(
    body: ReserveRequest,
    db: AsyncSession,
    *,
    x_service_key: str | None,
) -> ReserveResponse:
    """
    All-or-nothing reserve with SELECT FOR UPDATE + idempotency_key.

    On success: active_quantity -= N, blocked_quantity (reserved) += N.
    If any SKU insufficient → 409, full rollback, no partial reserves.
    """
    _require_service_key(x_service_key)

    # Idempotent replay
    existing = await db.get(ReserveOperation, body.idempotency_key)
    if existing is not None:
        data = existing.result or {}
        return ReserveResponse.model_validate(data)

    sku_ids = [item.sku_id for item in body.items]
    # Lock rows in stable order to reduce deadlock risk
    ordered_ids = sorted(set(sku_ids), key=str)
    result = await db.execute(
        select(SKU).where(SKU.id.in_(ordered_ids)).with_for_update()
    )
    locked = {sku.id: sku for sku in result.scalars().all()}

    failed: list[FailedReserveItem] = []
    for item in body.items:
        sku = locked.get(item.sku_id)
        if sku is None:
            failed.append(
                FailedReserveItem(
                    sku_id=item.sku_id,
                    requested=item.quantity,
                    available=0,
                    reason="NOT_FOUND",
                )
            )
            continue
        available = int(sku.active_quantity or 0)
        if available < item.quantity:
            failed.append(
                FailedReserveItem(
                    sku_id=item.sku_id,
                    requested=item.quantity,
                    available=available,
                    reason="INSUFFICIENT_STOCK",
                )
            )

    if failed:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INSUFFICIENT_STOCK",
                "message": "Insufficient stock for one or more SKUs",
                "reserved": False,
                "failed_items": [f.model_dump(mode="json") for f in failed],
            },
        )

    out_of_stock: list[UUID] = []
    results: list[ReserveItemResult] = []
    # Apply quantities (may reserve same sku_id multiple times in one request)
    # Aggregate first to keep invariant clear
    needed: dict[UUID, int] = {}
    for item in body.items:
        needed[item.sku_id] = needed.get(item.sku_id, 0) + item.quantity

    for sku_id, qty in needed.items():
        sku = locked[sku_id]
        sku.active_quantity = int(sku.active_quantity) - qty
        sku.blocked_quantity = int(sku.blocked_quantity or 0) + qty
        if sku.active_quantity == 0:
            out_of_stock.append(sku_id)

    for item in body.items:
        sku = locked[item.sku_id]
        results.append(
            ReserveItemResult(
                sku_id=item.sku_id,
                quantity=item.quantity,
                active_quantity=int(sku.active_quantity),
                reserved_quantity=int(sku.blocked_quantity or 0),
            )
        )

    response = ReserveResponse(reserved=True, items=results, failed_items=[])
    db.add(
        ReserveOperation(
            idempotency_key=body.idempotency_key,
            result=response.model_dump(mode="json"),
        )
    )
    await db.commit()

    for sku_id in out_of_stock:
        await _emit_sku_out_of_stock(sku_id)

    return response


async def unreserve_skus(
    body: UnreserveRequest,
    db: AsyncSession,
    *,
    x_service_key: str | None,
) -> UnreserveResponse:
    """Compensating unreserve: reserved → active (all-or-nothing)."""
    _require_service_key(x_service_key)

    sku_ids = sorted({item.sku_id for item in body.items}, key=str)
    result = await db.execute(
        select(SKU).where(SKU.id.in_(sku_ids)).with_for_update()
    )
    locked = {sku.id: sku for sku in result.scalars().all()}

    needed: dict[UUID, int] = {}
    for item in body.items:
        needed[item.sku_id] = needed.get(item.sku_id, 0) + item.quantity

    for sku_id, qty in needed.items():
        sku = locked.get(sku_id)
        if sku is None:
            await db.rollback()
            raise HTTPException(
                status_code=404,
                detail={
                    "code": ErrorCode.NOT_FOUND,
                    "message": f"SKU {sku_id} not found",
                },
            )
        reserved = int(sku.blocked_quantity or 0)
        if reserved < qty:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": ErrorCode.CONFLICT,
                    "message": (
                        f"Cannot unreserve {qty} for SKU {sku_id}: "
                        f"only {reserved} reserved"
                    ),
                },
            )

    for sku_id, qty in needed.items():
        sku = locked[sku_id]
        sku.blocked_quantity = int(sku.blocked_quantity or 0) - qty
        sku.active_quantity = int(sku.active_quantity or 0) + qty

    await db.commit()
    return UnreserveResponse(ok=True)
