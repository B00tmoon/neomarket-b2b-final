"""Processed moderation events for idempotency (B2B-9)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProcessedModerationEvent(Base):
    """Records applied moderation decisions by idempotency_key."""

    __tablename__ = "processed_moderation_events"

    idempotency_key = Column(UUID(as_uuid=True), primary_key=True)
    product_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    event_status = Column(String(32), nullable=False)  # MODERATED / BLOCKED / HARD_BLOCKED
    result = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
