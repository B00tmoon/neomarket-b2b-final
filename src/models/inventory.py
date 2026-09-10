"""Idempotency store for reserve operations (B2B-8)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReserveOperation(Base):
    """Stores successful reserve results keyed by client idempotency_key."""

    __tablename__ = "reserve_operations"

    idempotency_key = Column(UUID(as_uuid=True), primary_key=True)
    result = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
