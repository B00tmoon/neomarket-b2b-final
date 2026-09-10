"""Tests for US-B2B-09: POST /api/v1/events/moderation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import get_db as real_get_db
from src.models.moderation_events import ProcessedModerationEvent
from src.models.product import Product, ProductStatus
from src.models.sku import SKU
from src.settings import settings

PRODUCT_UUID = UUID("00000000-0000-4000-8000-000000000100")
SELLER_UUID = UUID("00000000-0000-4000-8000-000000000001")
REASON_UUID = UUID("00000000-0000-4000-8000-000000000777")
KEY = UUID("00000000-0000-4000-8000-000000000900")


def _product(status=ProductStatus.ON_MODERATION):
    p = MagicMock(spec=Product)
    p.id = PRODUCT_UUID
    p.seller_id = SELLER_UUID
    p.status = status
    p.deleted = False
    p.blocking_reason_id = REASON_UUID if status == ProductStatus.BLOCKED else None
    p.blocking_comment = "old comment" if status == ProductStatus.BLOCKED else None
    p.field_reports = (
        [{"field_name": "title", "comment": "bad"}]
        if status == ProductStatus.BLOCKED
        else None
    )
    p.title = "P"
    return p


def _sku():
    s = MagicMock(spec=SKU)
    s.id = uuid4()
    s.product_id = PRODUCT_UUID
    s.active = True
    return s


class _Session:
    def __init__(self, product, skus=None, processed=None):
        self.product = product
        self.skus = skus or [_sku()]
        self.processed = processed or {}
        self.added = []
        self.committed = False

    async def get(self, model, ident, **kwargs):
        if model is ProcessedModerationEvent:
            return self.processed.get(ident)
        if model is Product:
            if self.product and ident == self.product.id:
                return self.product
            return None
        return None

    async def execute(self, query):
        result = MagicMock()
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=self.skus))
        )
        result.scalar_one_or_none = MagicMock(return_value=self.product)
        return result

    def add(self, obj):
        self.added.append(obj)
        key = getattr(obj, "idempotency_key", None)
        if key is not None:
            self.processed[key] = obj

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    async def rollback(self):
        return None


def make_client(session, *, service_key: str | None = settings.b2b_to_mod_key):
    async def override():
        yield session

    app.dependency_overrides[real_get_db] = override
    headers = {}
    if service_key is not None:
        headers["X-Service-Key"] = service_key
    return TestClient(app, base_url="http://test", headers=headers)


@pytest.mark.asyncio
async def test_moderated_event_clears_blocking_data():
    product = _product(status=ProductStatus.ON_MODERATION)
    product.blocking_reason_id = REASON_UUID
    product.blocking_comment = "was blocked"
    product.field_reports = [{"field_name": "description", "comment": "x"}]
    session = _Session(product)
    client = make_client(session)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/events/moderation",
            json={
                "idempotency_key": str(KEY),
                "product_id": str(PRODUCT_UUID),
                "status": "MODERATED",
            },
        )

    assert response.status_code == 200, response.text
    assert product.status == ProductStatus.MODERATED
    assert product.blocking_reason_id is None
    assert product.blocking_comment is None
    assert product.field_reports is None


@pytest.mark.asyncio
async def test_blocked_soft_saves_field_reports():
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)
    captured = {}

    async def mock_post(url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return MagicMock(status_code=200)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/events/moderation",
            json={
                "idempotency_key": str(uuid4()),
                "product_id": str(PRODUCT_UUID),
                "status": "BLOCKED",
                "hard_block": False,
                "blocking_reason": {
                    "id": str(REASON_UUID),
                    "title": "Описание не соответствует товару",
                    "comment": "Несоответствие описания и фотографий",
                },
                "field_reports": [
                    {
                        "field_name": "description",
                        "sku_id": None,
                        "comment": "Текст описания скопирован с другого товара",
                    }
                ],
            },
        )

    assert response.status_code == 200, response.text
    assert product.status == ProductStatus.BLOCKED
    assert product.field_reports is not None
    assert product.field_reports[0]["field_name"] == "description"
    assert product.blocking_reason_id == REASON_UUID
    assert captured.get("json", {}).get("event_type") == "PRODUCT_BLOCKED"
    assert captured["json"]["hard_block"] is False


@pytest.mark.asyncio
async def test_blocked_hard_sets_terminal_status():
    product = _product(status=ProductStatus.ON_MODERATION)
    session = _Session(product)
    client = make_client(session)
    captured = {}

    async def mock_post(url, json=None, headers=None, **kwargs):
        captured["json"] = json
        return MagicMock(status_code=200)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/events/moderation",
            json={
                "idempotency_key": str(uuid4()),
                "product_id": str(PRODUCT_UUID),
                "status": "BLOCKED",
                "hard_block": True,
                "blocking_reason": {
                    "id": str(REASON_UUID),
                    "title": "Fraud",
                    "comment": "Permanent",
                },
            },
        )

    assert response.status_code == 200, response.text
    assert product.status == ProductStatus.HARD_BLOCKED
    assert captured.get("json", {}).get("event_type") == "PRODUCT_BLOCKED"
    assert captured["json"]["hard_block"] is True


@pytest.mark.asyncio
async def test_hard_blocked_product_rejects_seller_edits():
    product = _product(status=ProductStatus.HARD_BLOCKED)
    session = _Session(product)
    client = make_client(session, service_key=None)
    # seller header
    client.headers["X-Seller-Id"] = str(SELLER_UUID)

    response = client.put(
        f"/api/v1/products/{PRODUCT_UUID}",
        json={"title": "New title"},
    )

    assert response.status_code == 403, response.text
    data = response.json()
    assert data.get("code") == "PRODUCT_HARD_BLOCKED" or "HARD_BLOCKED" in str(data)


@pytest.mark.asyncio
async def test_duplicate_event_same_idempotency_key_no_side_effects():
    product = _product(status=ProductStatus.MODERATED)
    # already processed key
    prev = ProcessedModerationEvent(
        idempotency_key=KEY,
        product_id=PRODUCT_UUID,
        event_status="MODERATED",
        result={},
    )
    session = _Session(product, processed={KEY: prev})
    client = make_client(session)

    with patch("src.services.moderation.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/events/moderation",
            json={
                "idempotency_key": str(KEY),
                "product_id": str(PRODUCT_UUID),
                "status": "MODERATED",
            },
        )

    assert response.status_code == 200, response.text
    # no new cascade on replay
    mock_client.post.assert_not_called()
    # status unchanged
    assert product.status == ProductStatus.MODERATED


@pytest.mark.asyncio
async def test_missing_service_key_returns_401():
    product = _product()
    session = _Session(product)
    client = make_client(session, service_key=None)

    response = client.post(
        "/api/v1/events/moderation",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(PRODUCT_UUID),
            "status": "MODERATED",
        },
    )

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "UNAUTHORIZED"
