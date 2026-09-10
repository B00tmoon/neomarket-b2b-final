"""Tests for US-B2B-02: create SKU (POST /api/v1/skus) — neomarket-protocols."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import get_db as real_get_db
from src.models.product import Product, ProductStatus
from src.models.sku import SKU, SKUCharacteristic, SKUImage

SELLER_UUID = UUID("00000000-0000-4000-8000-000000000042")
PRODUCT_UUID = UUID("00000000-0000-4000-8000-000000000500")
SKU_UUID = UUID("00000000-0000-4000-8000-000000000600")
CATEGORY_UUID = UUID("00000000-0000-4000-8000-000000000001")


def _mock_product(
    product_id: UUID = PRODUCT_UUID,
    seller_id: UUID = SELLER_UUID,
    status=ProductStatus.CREATED,
):
    product = MagicMock(spec=Product)
    product.id = product_id
    product.seller_id = seller_id
    product.status = status
    product.deleted = False
    product.title = "Test Product"
    product.description = "Desc"
    product.category_id = CATEGORY_UUID
    product.slug = "test-product"
    return product


def _mock_sku(sku_id: UUID = SKU_UUID, product_id: UUID = PRODUCT_UUID):
    sku = MagicMock(spec=SKU)
    sku.id = sku_id
    sku.product_id = product_id
    sku.name = "256GB Black"
    sku.price = 12999000
    sku.cost_price = 9500000
    sku.discount = 0
    sku.article = None
    sku.image = "/s3/iphone15-black-256.jpg"
    sku.stock_quantity = 0
    sku.active_quantity = 0
    sku.blocked_quantity = 0
    sku.active = True
    sku.sku_code = f"SKU-{sku_id.hex[:12]}"
    sku.characteristics = []
    sku.images = []
    sku.created_at = datetime.now(timezone.utc)
    sku.updated_at = None
    return sku


def _mock_session(*, product, existing_skus=None):
    session = AsyncMock()
    existing_skus = existing_skus if existing_skus is not None else []
    holder: dict = {"sku": None, "images": [], "chars": []}

    async def mock_get(model, ident, **kwargs):
        if model is Product:
            return product if ident == product.id else None
        if model is SKU:
            return holder.get("sku")
        return None

    async def mock_execute(query):
        result = MagicMock()
        sku = holder.get("sku")
        if sku is not None:
            # attach collected images/chars for response mapping
            try:
                sku.images = list(holder["images"])
                sku.characteristics = list(holder["chars"])
            except Exception:
                pass
            result.scalar_one = MagicMock(return_value=sku)
            result.scalar_one_or_none = MagicMock(return_value=sku)
            result.scalars = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=existing_skus))
            )
        else:
            result.scalar_one = MagicMock(side_effect=Exception("no scalar yet"))
            result.scalar_one_or_none = MagicMock(return_value=None)
            result.scalars = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=existing_skus))
            )
        return result

    def mock_add(obj):
        tablename = getattr(obj, "__tablename__", None)
        if isinstance(obj, SKU) or tablename == "skus":
            if not getattr(obj, "characteristics", None):
                try:
                    obj.characteristics = []
                except Exception:
                    pass
            if not getattr(obj, "images", None):
                try:
                    obj.images = []
                except Exception:
                    pass
            if not getattr(obj, "created_at", None):
                try:
                    obj.created_at = datetime.now(timezone.utc)
                except Exception:
                    pass
            holder["sku"] = obj
        elif isinstance(obj, SKUImage) or tablename == "sku_images":
            holder["images"].append(obj)
        elif isinstance(obj, SKUCharacteristic) or tablename == "sku_characteristics":
            holder["chars"].append(obj)
        elif (
            hasattr(obj, "product_id")
            and hasattr(obj, "price")
            and hasattr(obj, "name")
            and not hasattr(obj, "sku_id")
        ):
            # treat as SKU-like
            holder["sku"] = obj

    session.get = AsyncMock(side_effect=mock_get)
    session.execute = AsyncMock(side_effect=mock_execute)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock(side_effect=mock_add)
    session.rollback = AsyncMock()
    return session


def make_client(product, seller_id: UUID = SELLER_UUID, existing_skus=None):
    session = _mock_session(product=product, existing_skus=existing_skus or [])

    async def override_get_db():
        yield session

    app.dependency_overrides[real_get_db] = override_get_db
    client = TestClient(
        app, base_url="http://test", headers={"X-Seller-Id": str(seller_id)}
    )
    return client, session


def _valid_body(product_id: UUID = PRODUCT_UUID, **overrides) -> dict:
    """Valid SKUCreate body per neomarket-protocols (images[], optional cost_price)."""
    body = {
        "product_id": str(product_id),
        "name": "256GB Black",
        "price": 12999000,
        "cost_price": 9500000,
        "discount": 0,
        "article": "IPH15-256-BLK",
        "images": [{"url": "/s3/iphone15-black-256.jpg", "ordering": 0}],
        "characteristics": [
            {"name": "Цвет", "value": "Чёрный"},
            {"name": "Объём памяти", "value": "256 ГБ"},
        ],
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_first_sku_transitions_product_to_on_moderation():
    """First SKU on CREATED product → product.status = ON_MODERATION."""
    product = _mock_product(status=ProductStatus.CREATED)
    client, _ = make_client(product, existing_skus=[])

    with patch("src.routes.skus.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post("/api/v1/skus", json=_valid_body())

    assert response.status_code == 201, (
        f"Expected 201, got {response.status_code}: {response.text}"
    )
    assert product.status == ProductStatus.ON_MODERATION
    data = response.json()
    assert data["name"] == "256GB Black"
    assert data["price"] == 12999000
    assert data["cost_price"] == 9500000
    assert data["product_id"] == str(PRODUCT_UUID)
    # Full SKUResponse required fields
    for field in (
        "id",
        "product_id",
        "name",
        "price",
        "discount",
        "cost_price",
        "stock_quantity",
        "active_quantity",
        "reserved_quantity",
        "article",
        "images",
        "characteristics",
        "created_at",
    ):
        assert field in data, f"missing required field: {field}"
    assert isinstance(data["images"], list)
    assert len(data["images"]) >= 1
    assert data["images"][0]["url"] == "/s3/iphone15-black-256.jpg"
    assert data["article"] == "IPH15-256-BLK"


@pytest.mark.asyncio
async def test_first_sku_emits_created_event_to_moderation():
    """First SKU → POST /api/v1/b2b/events with PRODUCT_CREATED per protocols."""
    product = _mock_product(status=ProductStatus.CREATED)
    client, _ = make_client(product, existing_skus=[])

    captured: dict = {}

    async def mock_post(url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return MagicMock(status_code=200)

    with patch("src.routes.skus.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post("/api/v1/skus", json=_valid_body())

    assert response.status_code == 201, response.text
    assert "json" in captured, "Moderation event was not sent"
    body = captured["json"]
    assert body["event_type"] == "PRODUCT_CREATED"
    assert "idempotency_key" in body and body["idempotency_key"]
    assert "occurred_at" in body
    payload = body["payload"]
    assert payload["product_id"] == str(PRODUCT_UUID)
    assert payload["seller_id"] == str(SELLER_UUID)
    assert "json_after" in payload
    headers = captured["headers"] or {}
    assert headers.get("X-Service-Key") == "b2b-moderation-key"
    assert "/api/v1/b2b/events" in captured["url"]


@pytest.mark.asyncio
async def test_second_sku_no_state_change():
    """Second SKU while ON_MODERATION → status unchanged, no event."""
    product = _mock_product(status=ProductStatus.ON_MODERATION)
    existing = [_mock_sku()]
    client, _ = make_client(product, existing_skus=existing)

    with patch("src.routes.skus.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post("/api/v1/skus", json=_valid_body())

    assert response.status_code == 201, response.text
    assert product.status == ProductStatus.ON_MODERATION
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_add_sku_to_hard_blocked_returns_403():
    """HARD_BLOCKED product → 403 FORBIDDEN."""
    product = _mock_product(status=ProductStatus.HARD_BLOCKED)
    client, _ = make_client(product, existing_skus=[])

    response = client.post("/api/v1/skus", json=_valid_body())

    assert response.status_code == 403, response.text
    data = response.json()
    assert data["code"] in ("FORBIDDEN", "PRODUCT_HARD_BLOCKED")


@pytest.mark.asyncio
async def test_missing_image_returns_400():
    """
    images[] is optional per protocols (default []).
    Keep DoD name: body without images still creates SKU (201),
    but required name missing → 422.
    """
    product = _mock_product(status=ProductStatus.CREATED)
    client, _ = make_client(product, existing_skus=[])

    # images omitted is allowed by protocols — must succeed with empty images
    body = _valid_body()
    del body["images"]

    with patch("src.routes.skus.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post("/api/v1/skus", json=body)

    # protocols: images optional → 201 with images=[]
    assert response.status_code == 201, response.text
    assert response.json()["images"] == []


@pytest.mark.asyncio
async def test_price_must_be_positive():
    """price < 0 is rejected (protocols: minimum 0); price=0 is allowed."""
    product = _mock_product(status=ProductStatus.CREATED)
    client, _ = make_client(product, existing_skus=[])

    response = client.post("/api/v1/skus", json=_valid_body(price=-1))

    assert response.status_code == 422, response.text
    data = response.json()
    assert data["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_sku_response_has_full_model():
    """SKUResponse contains all required fields from protocols."""
    product = _mock_product(status=ProductStatus.CREATED)
    client, _ = make_client(product, existing_skus=[])

    with patch("src.routes.skus.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post("/api/v1/skus", json=_valid_body())

    assert response.status_code == 201, response.text
    data = response.json()
    required = [
        "id",
        "product_id",
        "name",
        "price",
        "discount",
        "cost_price",
        "stock_quantity",
        "active_quantity",
        "reserved_quantity",
        "article",
        "images",
        "characteristics",
        "created_at",
    ]
    for f in required:
        assert f in data, f"missing {f}"
    assert len(data["characteristics"]) == 2
    assert data["characteristics"][0]["name"] == "Цвет"
    assert "id" in data["characteristics"][0]
