from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from src.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SKU(Base):
    __tablename__ = "skus"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    product_id = Column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Internal unique code (auto-generated; not part of public create body)
    sku_code = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    price = Column(Integer, nullable=False)  # kopecks, >= 0
    cost_price = Column(Integer, nullable=True)  # optional seller-only
    discount = Column(Integer, nullable=False, server_default="0")  # kopecks, >= 0
    article = Column(String(100), nullable=True)
    # Legacy single-image column kept for migration compat; primary image also in sku_images
    image = Column(String(500), nullable=True, server_default="")
    stock_quantity = Column(Integer, nullable=False, server_default="0")
    active_quantity = Column(Integer, default=0, nullable=False, server_default="0")
    blocked_quantity = Column(Integer, default=0, nullable=False, server_default="0")  # reserved
    active = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=_utcnow)

    characteristics = relationship(
        "SKUCharacteristic", back_populates="sku", cascade="all, delete-orphan"
    )
    images = relationship(
        "SKUImage",
        back_populates="sku",
        cascade="all, delete-orphan",
        order_by="SKUImage.ordering",
    )
    product = relationship("Product", back_populates="skus")

    def __repr__(self) -> str:
        return (
            f"<SKU(id={self.id}, code='{self.sku_code}', "
            f"price={self.price}, qty={self.active_quantity})>"
        )


class SKUImage(Base):
    __tablename__ = "sku_images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    sku_id = Column(
        UUID(as_uuid=True),
        ForeignKey("skus.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url = Column(String(500), nullable=False)
    ordering = Column(Integer, nullable=False, server_default="0")

    sku = relationship("SKU", back_populates="images")


class SKUCharacteristic(Base):
    __tablename__ = "sku_characteristics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    sku_id = Column(
        UUID(as_uuid=True),
        ForeignKey("skus.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    value = Column(String(500), nullable=False)

    sku = relationship("SKU", back_populates="characteristics")

    def __repr__(self) -> str:
        return (
            f"<SKUCharacteristic(sku_id={self.sku_id}, "
            f"name='{self.name}', value='{self.value}')>"
        )
