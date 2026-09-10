"""Tests for US-B2B-07: B2C catalog mode of GET /api/v1/products."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import get_db as real_get_db
from src.models.product import Product, ProductStatus
from src.models.sku import SKU
from src.settings import settings

SELLER_UUID = UUID("00000000-0000-4000-8000-000000000042")
PRODUCT_A = UUID("00000000-0000-4000-8000-000000000501")
PRODUCT_B = UUID("00000000-0000-4000-8000-000000000502")
PRODUCT_HARD = UUID("00000000-0000-4000-8000-000000000503")
CATEGORY_UUID = UUID("00000000-0000-4000-8000-000000000001")


def _sku(
    *,
    product_id: UUID,
    active_quantity: int = 5,
    cost_price: int = 1000,
    reserved: int = 2,
):
    sku = MagicMock(spec=SKU)
    sku.id = uuid4()
    sku.product_id = product_id
    sku.name = "Variant"
    sku.price = 5000
    sku.cost_price = cost_price
    sku.discount = 0
    sku.article = "ART-1"
    sku.image = "/s3/sku.jpg"
    sku.stock_quantity = active_quantity + reserved
    sku.active_quantity = active_quantity
    sku.blocked_quantity = reserved
    sku.active = True
    sku.characteristics = []
    sku.images = []
    sku.created_at = datetime.now(timezone.utc)
    sku.updated_at = None
    return sku


def _product(
    product_id: UUID,
    *,
    status=ProductStatus.MODERATED,
    deleted: bool = False,
    skus=None,
):
    product = MagicMock(spec=Product)
    product.id = product_id
    product.seller_id = SELLER_UUID
    product.title = f"Product {product_id.hex[:4]}"
    product.description = "Desc"
    product.status = status
    product.category_id = CATEGORY_UUID
    product.slug = f"product-{product_id.hex[:4]}"
    product.deleted = deleted
    product.created_at = datetime.now(timezone.utc)
    product.updated_at = None
    product.images = []
    product.characteristics = []
    product.skus = skus if skus is not None else [_sku(product_id=product_id)]
    product.blocking_reason_id = None
    product.blocking_comment = None
    product.field_reports = None
    return product


class _CatalogSession:
    """AsyncSession mock that applies B2C visibility filters in Python."""

    def __init__(self, products: list):
        self._all = products

    def _visible(self, products: list) -> list:
        out = []
        for p in products:
            if p.deleted:
                continue
            if p.status != ProductStatus.MODERATED:
                continue
            if not any(getattr(s, "active_quantity", 0) > 0 for s in (p.skus or [])):
                continue
            out.append(p)
        return out

    async def execute(self, query):
        # Extremely simplified: return all visible products; batch filter via ids
        # is applied by inspecting query string representation for UUID literals.
        visible = self._visible(self._all)
        qstr = str(query)
        filtered = []
        for p in visible:
            # if query mentions specific product ids subset
            if "IN" in qstr.upper() or "in_" in qstr:
                if str(p.id) in qstr or p.id.hex in qstr:
                    filtered.append(p)
                else:
                    # also accept if no id literals found at all
                    filtered.append(p)
            else:
                filtered.append(p)

        # Prefer exact match if any product id appears in query
        id_matched = [p for p in visible if str(p.id) in qstr]
        if id_matched:
            filtered = id_matched
        else:
            filtered = visible

        result = MagicMock()
        result.scalar_one = MagicMock(return_value=len(filtered))
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=filtered))
        )
        return result

    async def get(self, *args, **kwargs):
        return None


def make_catalog_client(products: list, *, service_key: str | None = None):
    session = _CatalogSession(products)

    async def override_get_db():
        yield session

    app.dependency_overrides[real_get_db] = override_get_db
    headers = {}
    if service_key is not None:
        headers["X-Service-Key"] = service_key
    return TestClient(app, base_url="http://test", headers=headers)


@pytest.mark.asyncio
async def test_catalog_returns_moderated_in_stock_products():
    """Catalog returns only MODERATED products with active_quantity > 0."""
    ok = _product(PRODUCT_A, status=ProductStatus.MODERATED)
    out_of_stock = _product(
        PRODUCT_B,
        status=ProductStatus.MODERATED,
        skus=[_sku(product_id=PRODUCT_B, active_quantity=0)],
    )
    created = _product(
        uuid4(),
        status=ProductStatus.CREATED,
        skus=[_sku(product_id=PRODUCT_A, active_quantity=3)],
    )
    client = make_catalog_client(
        [ok, out_of_stock, created],
        service_key=settings.b2c_to_b2b_key,
    )

    response = client.get("/api/v1/products")

    assert response.status_code == 200, response.text
    data = response.json()
    assert "items" in data
    ids = {item["id"] for item in data["items"]}
    assert str(PRODUCT_A) in ids
    assert str(PRODUCT_B) not in ids
    for item in data["items"]:
        assert item["status"] == "MODERATED"


@pytest.mark.asyncio
async def test_catalog_excludes_hard_blocked():
    """HARD_BLOCKED products never appear in catalog."""
    moderated = _product(PRODUCT_A, status=ProductStatus.MODERATED)
    hard = _product(PRODUCT_HARD, status=ProductStatus.HARD_BLOCKED)
    client = make_catalog_client(
        [moderated, hard],
        service_key=settings.b2c_to_b2b_key,
    )

    response = client.get("/api/v1/products")

    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert str(PRODUCT_A) in ids
    assert str(PRODUCT_HARD) not in ids


@pytest.mark.asyncio
async def test_catalog_missing_service_key_returns_401():
    """Without X-Service-Key (and without seller JWT) → 401."""
    client = make_catalog_client([_product(PRODUCT_A)], service_key=None)

    response = client.get("/api/v1/products")

    assert response.status_code == 401, response.text
    data = response.json()
    assert data["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_catalog_response_has_no_cost_price():
    """Catalog SKU payload must not contain cost_price or reserved_quantity."""
    product = _product(
        PRODUCT_A,
        skus=[_sku(product_id=PRODUCT_A, cost_price=99999, reserved=7)],
    )
    client = make_catalog_client([product], service_key=settings.b2c_to_b2b_key)

    response = client.get("/api/v1/products")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"], "expected at least one item"
    for item in payload["items"]:
        for sku in item.get("skus", []):
            assert "cost_price" not in sku, f"cost_price leaked: {sku}"
            assert "reserved_quantity" not in sku, f"reserved_quantity leaked: {sku}"
            assert "price" in sku
            assert "active_quantity" in sku


@pytest.mark.asyncio
async def test_batch_ids_returns_visible_subset():
    """?ids= returns only visible products from the list (no 404 for hidden)."""
    visible = _product(PRODUCT_A, status=ProductStatus.MODERATED)
    hidden = _product(PRODUCT_HARD, status=ProductStatus.HARD_BLOCKED)
    client = make_catalog_client(
        [visible, hidden],
        service_key=settings.b2c_to_b2b_key,
    )

    response = client.get(
        f"/api/v1/products?ids={PRODUCT_A},{PRODUCT_HARD}"
    )

    assert response.status_code == 200, response.text
    data = response.json()
    ids = {item["id"] for item in data["items"]}
    assert str(PRODUCT_A) in ids
    assert str(PRODUCT_HARD) not in ids
