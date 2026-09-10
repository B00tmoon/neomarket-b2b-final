"""Pydantic schemas for moderation events (B2B-9 / apply-moderation)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BlockingReasonIn(BaseModel):
    id: Optional[UUID] = None
    title: Optional[str] = None
    comment: Optional[str] = None


class FieldReportIn(BaseModel):
    field_name: str
    sku_id: Optional[UUID] = None
    comment: str = ""


class ModerationEventRequest(BaseModel):
    """
    Incoming decision from Moderation Service.

    Canon (B2B-9): status = MODERATED | BLOCKED, optional hard_block + blocking_reason.
    Also accepts legacy field event_type for backward compatibility.
    """

    model_config = ConfigDict(extra="ignore")

    idempotency_key: UUID
    product_id: UUID
    status: Optional[str] = Field(
        None, description="MODERATED or BLOCKED (canon)"
    )
    event_type: Optional[str] = Field(
        None, description="Legacy alias for status"
    )
    hard_block: bool = False
    blocking_reason: Optional[BlockingReasonIn | str] = None
    field_reports: Optional[List[FieldReportIn | dict]] = None
    moderator_id: Optional[str] = None
    moderator_comment: Optional[str] = None
    blocking_reason_id: Optional[UUID] = None
    occurred_at: Optional[datetime] = None

    @model_validator(mode="after")
    def normalize_status(self) -> "ModerationEventRequest":
        raw = (self.status or self.event_type or "").strip().upper()
        if raw not in ("MODERATED", "BLOCKED"):
            raise ValueError("status must be MODERATED or BLOCKED")
        self.status = raw
        self.event_type = raw
        return self

    def resolved_blocking_reason(self) -> tuple[Optional[UUID], Optional[str], Optional[str]]:
        """Return (reason_id, title, comment)."""
        br = self.blocking_reason
        if br is None:
            return self.blocking_reason_id, None, self.moderator_comment
        if isinstance(br, str):
            return self.blocking_reason_id, None, br
        return (
            br.id or self.blocking_reason_id,
            br.title,
            br.comment or self.moderator_comment,
        )

    def resolved_field_reports(self) -> list[dict]:
        reports = self.field_reports or []
        out: list[dict] = []
        for item in reports:
            if isinstance(item, dict):
                out.append(
                    {
                        "field_name": item.get("field_name") or item.get("field") or "",
                        "sku_id": str(item["sku_id"]) if item.get("sku_id") else None,
                        "comment": item.get("comment") or "",
                    }
                )
            else:
                out.append(
                    {
                        "field_name": item.field_name,
                        "sku_id": str(item.sku_id) if item.sku_id else None,
                        "comment": item.comment or "",
                    }
                )
        return out


class ModerationEventResponse(BaseModel):
    product_id: UUID
    status: str
    accepted: bool = True


class ProductApproveRequest(BaseModel):
    comment: Optional[str] = Field(None, max_length=2000)


class ProductApproveResponse(BaseModel):
    product_id: UUID
    status: str
    seller_id: UUID
    approved_at: datetime
    approved_by: Optional[str] = None
    comment: Optional[str] = None


from src.schemas.errors import ErrorResponse  # noqa: E402, F401
