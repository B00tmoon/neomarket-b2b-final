"""Moderation event processing service (B2B-9 apply-moderation)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.moderation_events import ProcessedModerationEvent
from src.models.product import Product, ProductStatus
from src.models.sku import SKU
from src.schemas.moderation import ModerationEventRequest
from src.settings import settings

logger = logging.getLogger(__name__)


class ModerationEventError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ProductNotFoundError(ModerationEventError):
    def __init__(self, product_id: object):
        super().__init__(
            code="PRODUCT_NOT_FOUND",
            message=f"Product {product_id} not found",
            status_code=404,
        )


class ProductWrongStatusError(ModerationEventError):
    def __init__(self, product_id: object, current_status: str):
        super().__init__(
            code="PRODUCT_WRONG_STATUS",
            message=(
                f"Product {product_id} has status {current_status}, "
                "expected ON_MODERATION"
            ),
            status_code=409,
        )


class ProductNoSKUError(ModerationEventError):
    def __init__(self, product_id: object):
        super().__init__(
            code="PRODUCT_NO_SKU",
            message=f"Product {product_id} has no SKU — cannot be approved",
            status_code=409,
        )


class ProductAlreadyModeratedError(ModerationEventError):
    def __init__(self, product_id: object):
        super().__init__(
            code="PRODUCT_ALREADY_APPROVED",
            message=f"Product {product_id} is already MODERATED",
            status_code=409,
        )


class ProductHardBlockedError(ModerationEventError):
    def __init__(self, product_id: object, action: str = "modify"):
        super().__init__(
            code="PRODUCT_HARD_BLOCKED",
            message=f"Product {product_id} is HARD_BLOCKED — {action} is forbidden",
            status_code=403,
        )


class ModerationService:
    """Applies moderation decisions; idempotent via processed_moderation_events."""

    def __init__(self, b2c_api_url: str | None = None):
        self.b2c_api_url = b2c_api_url or settings.b2c_service_url

    async def _already_processed(
        self, db: AsyncSession, key: UUID
    ) -> ProcessedModerationEvent | None:
        return await db.get(ProcessedModerationEvent, key)

    async def _mark_processed(
        self,
        db: AsyncSession,
        *,
        key: UUID,
        product_id: UUID,
        event_status: str,
        result: dict | None = None,
    ) -> None:
        db.add(
            ProcessedModerationEvent(
                idempotency_key=key,
                product_id=product_id,
                event_status=event_status,
                result=result,
            )
        )

    async def apply_decision(
        self,
        db: AsyncSession,
        event: ModerationEventRequest,
    ) -> tuple[Product, bool]:
        """
        Apply moderation decision.

        Returns (product, applied) where applied=False means idempotent replay.
        """
        key = event.idempotency_key
        existing = await self._already_processed(db, key)
        if existing is not None:
            product = await db.get(Product, event.product_id)
            if product is None:
                raise ProductNotFoundError(event.product_id)
            return product, False

        if event.status == "MODERATED":
            product = await self.process_moderated_event(
                db=db,
                product_id=event.product_id,
                idempotency_key=str(key),
            )
            await self._mark_processed(
                db,
                key=key,
                product_id=event.product_id,
                event_status="MODERATED",
            )
            await db.commit()
            await self.push_to_b2c_catalog(
                product_id=event.product_id,
                idempotency_key=str(key),
            )
            return product, True

        # BLOCKED soft or hard
        reason_id, title, comment = event.resolved_blocking_reason()
        field_reports = event.resolved_field_reports()
        product = await self.process_blocked_event(
            db=db,
            product_id=event.product_id,
            idempotency_key=str(key),
            hard_block=bool(event.hard_block),
            blocking_reason_id=reason_id,
            blocking_title=title,
            blocking_comment=comment,
            field_reports=field_reports,
        )
        final_status = (
            "HARD_BLOCKED" if event.hard_block else "BLOCKED"
        )
        await self._mark_processed(
            db,
            key=key,
            product_id=event.product_id,
            event_status=final_status,
        )
        await db.commit()
        await self.push_product_blocked_to_b2c(
            product_id=event.product_id,
            idempotency_key=str(key),
            hard_block=bool(event.hard_block),
        )
        return product, True

    async def process_moderated_event(
        self,
        db: AsyncSession,
        product_id: object,
        idempotency_key: str,
        moderator_id: Optional[str] = None,
        moderator_comment: Optional[str] = None,
    ) -> Product:
        product = await db.get(Product, product_id)
        if not product or product.deleted:
            raise ProductNotFoundError(product_id)

        # Idempotent if already MODERATED
        if product.status == ProductStatus.MODERATED:
            return product

        if product.status != ProductStatus.ON_MODERATION:
            raise ProductWrongStatusError(product_id, product.status.value)

        skus_result = await db.execute(
            select(SKU).where(SKU.product_id == product_id)
        )
        if not list(skus_result.scalars().all()):
            raise ProductNoSKUError(product_id)

        product.status = ProductStatus.MODERATED
        # Clear blocking data for seller card
        product.blocking_reason_id = None
        product.blocking_comment = None
        product.field_reports = None
        await db.flush()
        return product

    async def process_blocked_event(
        self,
        db: AsyncSession,
        product_id: object,
        idempotency_key: str,
        hard_block: bool = False,
        blocking_reason_id: Optional[UUID] = None,
        blocking_title: Optional[str] = None,
        blocking_comment: Optional[str] = None,
        blocking_reason: Optional[str] = None,
        moderator_comment: Optional[str] = None,
        field_reports: Optional[list] = None,
    ) -> Product:
        product = await db.get(Product, product_id)
        if not product or product.deleted:
            raise ProductNotFoundError(product_id)

        target = (
            ProductStatus.HARD_BLOCKED if hard_block else ProductStatus.BLOCKED
        )
        # Already in target state — treat as success (caller still uses processed table)
        if product.status == target:
            return product
        if product.status == ProductStatus.HARD_BLOCKED:
            return product

        if product.status not in (
            ProductStatus.ON_MODERATION,
            ProductStatus.BLOCKED,
        ):
            # Allow BLOCKED → HARD_BLOCKED escalation; otherwise require ON_MODERATION
            if not (
                hard_block and product.status == ProductStatus.BLOCKED
            ) and product.status != ProductStatus.ON_MODERATION:
                raise ProductWrongStatusError(product_id, product.status.value)

        product.status = target

        if blocking_reason_id is not None:
            product.blocking_reason_id = blocking_reason_id
        # Prefer structured comment; fall back to legacy string fields
        comment = (
            blocking_comment
            or moderator_comment
            or blocking_reason
            or blocking_title
        )
        if comment:
            product.blocking_comment = comment
        if field_reports is not None:
            product.field_reports = field_reports
        await db.flush()
        return product

    async def push_product_blocked_to_b2c(
        self,
        product_id: object,
        idempotency_key: str,
        hard_block: bool,
    ) -> None:
        """Cascade PRODUCT_BLOCKED to B2C (soft and hard)."""
        url = f"{self.b2c_api_url.rstrip('/')}/api/v1/events/product-blocked"
        payload = {
            "event_type": "PRODUCT_BLOCKED",
            "product_id": str(product_id),
            "hard_block": hard_block,
            "idempotency_key": idempotency_key,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        headers = {"X-Service-Key": settings.b2b_to_mod_key}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                logger.info(
                    "PRODUCT_BLOCKED for %s hard=%s → %s",
                    product_id,
                    hard_block,
                    resp.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to emit PRODUCT_BLOCKED for %s: %s", product_id, exc
            )

    # Backward-compatible alias used by older route code
    async def push_blocked_event_to_b2b(
        self,
        product_id: str,
        idempotency_key: str,
        hard_block: bool,
    ) -> None:
        await self.push_product_blocked_to_b2c(
            product_id=product_id,
            idempotency_key=idempotency_key,
            hard_block=hard_block,
        )

    async def push_to_b2c_catalog(
        self, product_id: object, idempotency_key: str
    ) -> None:
        """Notify B2C that product is MODERATED and can appear in catalog."""
        url = f"{self.b2c_api_url.rstrip('/')}/api/v1/events/product-moderated"
        payload = {
            "event_type": "PRODUCT_MODERATED",
            "product_id": str(product_id),
            "idempotency_key": idempotency_key,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        headers = {"X-Service-Key": settings.b2b_to_mod_key}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload, headers=headers)
        except Exception:  # noqa: BLE001
            pass


class ModeratorApproveService:
    """Direct approve path used by optional moderator UI endpoints."""

    def __init__(self, b2c_api_url: str | None = None):
        self.b2c_api_url = b2c_api_url or settings.b2c_service_url
        self._mod = ModerationService(b2c_api_url=self.b2c_api_url)

    async def approve(
        self,
        db: AsyncSession,
        product_id: UUID,
        seller_id: UUID,
        comment: Optional[str] = None,
        approved_by: Optional[str] = None,
    ) -> Product:
        product = await db.get(Product, product_id)
        if not product or product.deleted:
            raise ProductNotFoundError(product_id)
        if product.seller_id != seller_id:
            raise ModerationEventError(
                code="FORBIDDEN",
                message="Cannot approve another seller's product",
                status_code=403,
            )
        if product.status == ProductStatus.HARD_BLOCKED:
            raise ProductHardBlockedError(product_id, action="approve")
        product = await self._mod.process_moderated_event(
            db=db,
            product_id=product_id,
            idempotency_key=str(uuid.uuid4()),
            moderator_comment=comment,
        )
        await db.commit()
        await self._mod.push_to_b2c_catalog(
            product_id=product_id,
            idempotency_key=str(uuid.uuid4()),
        )
        return product
