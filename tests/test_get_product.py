"""Tests for US-B2B-05: GET /api/v1/products/{id} (view card + blocking)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import get_db as real_get_db
from src.models.product import Product, ProductBlockingReason, ProductStatus

SELLER_UUID = UUID("00000000-0000-4000-8000-000000000042")
OTHER_SELLER = UUID("00000000-0000-4000-8000-000000000099")
PRODUCT_UUID = UUID("00000000-0000-4000-8000-000000000500")
CATEGORY_UUID = UUID("00000000-0000-4000-8000-000000000001")
REASON_UUID = UUID("00000000-0000-4000-8000-000000000777")


def _base_product(
    *,
    status=ProductStatus.MODERATED,
    seller_id: UUID = SELLER_UUID,
    product_id: UUID = PRODUCT_UUID,
):
    product = MagicMock(spec=Product)
    product.id = product_id
    product.seller_id = seller_id
    product.title = "Test Product"
    product.description = "Test description"
    product.status = status
    product.category_id = CATEGORY_UUID
    product.slug = "test-product"
    product.deleted = False
    product.created_at = datetime.now(timezone.utc)
    product.updated_at = None
    product.blocking_reason_id = None
    product.blocking_comment = None
    product.field_reports = None
    product.images = []
    product.characteristics = []
    product.skus = []
    return product


def _mock_session(product=None, reason=None):
    session = AsyncMock()

    async def mock_execute(query):
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=product)
        result.scalar_one = MagicMock(return_value=product)
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        return result

    async def mock_get(model, ident, **kwargs):
        if model is ProductBlockingReason and reason is not None:
            if ident == reason.id:
                return reason
        if model is Product:
            return product if product and ident == product.id else None
        return None

    session.execute = AsyncMock(side_effect=mock_execute)
    session.get = AsyncMock(side_effect=mock_get)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


def make_client(product=None, seller_id: UUID | None = SELLER_UUID, reason=None):
    session = _mock_session(product=product, reason=reason)

    async def override_get_db():
        yield session

    app.dependency_overrides[real_get_db] = override_get_db
    headers = {}
    if seller_id is not None:
        headers["X-Seller-Id"] = str(seller_id)
    return TestClient(app, base_url="http://test", headers=headers)


@pytest.mark.asyncio
async def test_get_moderated_product_returns_full_payload():
    """MODERATED product → full payload, blocked=false, blocking_reason=null."""
    product = _base_product(status=ProductStatus.MODERATED)
    img = MagicMock()
    img.id = uuid4()
    img.product_id = PRODUCT_UUID
    img.url = "https://example.com/img.jpg"
    img.ordering = 0
    product.images = [img]

    client = make_client(product=product, seller_id=SELLER_UUID)
    response = client.get(f"/api/v1/products/{PRODUCT_UUID}")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"] == str(PRODUCT_UUID)
    assert data["status"] == "MODERATED"
    assert data["blocked"] is False
    assert data["blocking_reason"] is None
    assert data["field_reports"] == []
    assert data["title"] == "Test Product"
    assert data["description"] == "Test description"
    assert data["seller_id"] == str(SELLER_UUID)
    assert data["slug"] == "test-product"
    assert data["images"]


@pytest.mark.asyncio
async def test_get_blocked_product_returns_blocking_reason_and_field_reports():
    """BLOCKED product → blocking_reason (with title) + field_reports."""
    product = _base_product(status=ProductStatus.BLOCKED)
    product.blocking_reason_id = REASON_UUID
    product.blocking_comment = "Несоответствие описания и фотографий"
    product.field_reports = [
        {
            "field_name": "description",
            "sku_id": None,
            "comment": "В описании указан материал 'натуральная кожа', на фото -- синтетика",
        },
        {
            "field_name": "sku_image",
            "sku_id": "f6a7b8c9-0123-4567-def0-789012345678",
            "comment": "Фото SKU не соответствует указанному цвету",
        },
    ]

    reason = MagicMock(spec=ProductBlockingReason)
    reason.id = REASON_UUID
    reason.name = "Описание не соответствует товару"
    reason.description = "Mismatch"

    client = make_client(product=product, seller_id=SELLER_UUID, reason=reason)
    response = client.get(f"/api/v1/products/{PRODUCT_UUID}")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "BLOCKED"
    assert data["blocked"] is True
    assert data["blocking_reason"] is not None
    assert data["blocking_reason"]["id"] == str(REASON_UUID)
    assert data["blocking_reason"]["title"] == "Описание не соответствует товару"
    assert "Несоответствие" in data["blocking_reason"]["comment"]
    assert isinstance(data["field_reports"], list)
    assert len(data["field_reports"]) == 2
    assert data["field_reports"][0]["field_name"] == "description"
    assert data["field_reports"][1]["field_name"] == "sku_image"
    assert data["field_reports"][1]["sku_id"] == "f6a7b8c9-0123-4567-def0-789012345678"


@pytest.mark.asyncio
async def test_get_others_product_returns_404():
    """Another seller's product → 404 (not 403) — IDOR protection."""
    product = _base_product(status=ProductStatus.MODERATED, seller_id=OTHER_SELLER)
    client = make_client(product=product, seller_id=SELLER_UUID)

    response = client.get(f"/api/v1/products/{PRODUCT_UUID}")

    assert response.status_code == 404, response.text
    data = response.json()
    assert data["code"] == "NOT_FOUND"
    assert "not found" in data["message"].lower()


@pytest.mark.asyncio
async def test_get_nonexistent_returns_404():
    """Unknown product id → 404."""
    client = make_client(product=None, seller_id=SELLER_UUID)

    response = client.get(f"/api/v1/products/{uuid4()}")

    assert response.status_code == 404, response.text
    data = response.json()
    assert data["code"] == "NOT_FOUND"
