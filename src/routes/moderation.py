"""Routes for moderation events and product approval (B2B-9)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.base import get_db
from src.models.product import Product, ProductStatus
from src.schemas.errors import ErrorCode
from src.schemas.moderation import (
    ErrorResponse,
    ModerationEventRequest,
    ModerationEventResponse,
    ProductApproveRequest,
    ProductApproveResponse,
)
from src.services.moderation import (
    ModerationEventError,
    ModerationService,
    ModeratorApproveService,
    ProductAlreadyModeratedError,
    ProductHardBlockedError,
    ProductNoSKUError,
    ProductNotFoundError,
    ProductWrongStatusError,
)
from src.settings import settings

router = APIRouter()
approve_router = APIRouter()


def _get_moderation_service() -> ModerationService:
    return ModerationService()


def _get_approve_service() -> ModeratorApproveService:
    return ModeratorApproveService()


def _require_moderation_key(x_service_key: str | None) -> None:
    if not x_service_key or x_service_key != settings.b2b_to_mod_key:
        raise HTTPException(
            status_code=401,
            detail={
                "code": ErrorCode.UNAUTHORIZED,
                "message": "X-Service-Key is required and must be valid",
            },
        )


async def _handle_moderation_event(
    event: ModerationEventRequest,
    db: AsyncSession,
) -> ModerationEventResponse:
    service = _get_moderation_service()
    try:
        product, _applied = await service.apply_decision(db, event)
    except (ProductNotFoundError, ProductWrongStatusError, ProductNoSKUError) as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        )
    except ProductAlreadyModeratedError:
        # treat as success
        product = await db.get(Product, event.product_id)
        if product is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
            )
    except ModerationEventError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        )

    status_val = (
        product.status.value
        if hasattr(product.status, "value")
        else str(product.status)
    )
    return ModerationEventResponse(
        product_id=product.id,
        status=status_val,
        accepted=True,
    )


@router.post(
    "/events/moderation",
    response_model=ModerationEventResponse,
    status_code=200,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def receive_moderation_event_canon(
    event: ModerationEventRequest,
    x_service_key: str | None = Header(None, alias="X-Service-Key"),
    db: AsyncSession = Depends(get_db),
):
    """
    POST /api/v1/events/moderation — apply Moderation decision (B2B-9).

    MODERATED → clear blocking data.
    BLOCKED soft → BLOCKED + field_reports + PRODUCT_BLOCKED to B2C.
    BLOCKED hard → HARD_BLOCKED + PRODUCT_BLOCKED to B2C.
    Idempotent by idempotency_key.
    """
    _require_moderation_key(x_service_key)
    return await _handle_moderation_event(event, db)


@router.post(
    "/moderation/events",
    response_model=ModerationEventResponse,
    status_code=200,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def receive_moderation_event_legacy(
    event: ModerationEventRequest,
    x_service_key: str | None = Header(None, alias="X-Service-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Legacy path POST /api/v1/moderation/events — same handler as canon."""
    _require_moderation_key(x_service_key)
    return await _handle_moderation_event(event, db)


@approve_router.post(
    "/products/{product_id}/approve",
    response_model=ProductApproveResponse,
    status_code=200,
    responses={
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def approve_product(
    product_id: UUID,
    body: ProductApproveRequest,
    seller_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Optional direct approve endpoint (moderator UI)."""
    service = _get_approve_service()
    try:
        product = await service.approve(
            db=db,
            product_id=product_id,
            seller_id=seller_id,
            comment=body.comment,
        )
    except (ProductNotFoundError, ProductWrongStatusError, ProductNoSKUError) as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        )
    except ProductHardBlockedError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        )
    except ModerationEventError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        )

    return ProductApproveResponse(
        product_id=product.id,
        status=product.status.value,
        seller_id=product.seller_id,
        approved_at=datetime.now(timezone.utc),
        comment=body.comment,
    )
