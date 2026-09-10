"""Tests for hard-block flow (aligned with B2B-9 canon)."""

from __future__ import annotations

from uuid import uuid4

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.product import ProductStatus
from tests.conftest import PRODUCT_UUID, SELLER_UUID, _mock_product, _mock_sku, make_test_client


@pytest.mark.asyncio
async def test_hard_block_transitions_to_terminal_and_emits_event():
    """Hard block: ON_MODERATION → HARD_BLOCKED, PRODUCT_BLOCKED emitted."""
    product = _mock_product(
        product_id=PRODUCT_UUID,
        status=ProductStatus.ON_MODERATION,
        seller_id=SELLER_UUID,
        has_sku=True,
    )
    sku = _mock_sku()

    with make_test_client(product, [sku]) as client:
        with patch("src.services.moderation.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_ctx

            response = client.post(
                "/api/v1/moderation/events",
                headers={"X-Service-Key": "b2b-moderation-key"},
                json={
                    "product_id": "00000000-0000-4000-8000-000000000100",
                    "status": "BLOCKED",
                    "occurred_at": "2026-07-30T12:00:00Z",
                    "hard_block": True,
                    "idempotency_key": str(uuid4()),
                    "moderator_comment": "Counterfeit goods",
                },
            )

    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}: {response.text}"
    )
    data = response.json()
    assert data["status"] == "HARD_BLOCKED"
    assert data["accepted"] is True
    assert str(data["product_id"]) == "00000000-0000-4000-8000-000000000100"
    assert product.status == ProductStatus.HARD_BLOCKED


@pytest.mark.asyncio
async def test_hard_block_event_carries_hard_block_true():
    """Cascade payload includes event_type=PRODUCT_BLOCKED and hard_block=true."""
    product = _mock_product(
        product_id=PRODUCT_UUID,
        status=ProductStatus.ON_MODERATION,
        seller_id=SELLER_UUID,
        has_sku=True,
    )
    sku = _mock_sku()
    captured_payload = {}

    async def mock_post(url, json=None, **kwargs):
        if json:
            captured_payload.update(json)
        return MagicMock(status_code=200)

    with make_test_client(product, [sku]) as client:
        with patch("src.services.moderation.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_ctx

            client.post(
                "/api/v1/moderation/events",
                headers={"X-Service-Key": "b2b-moderation-key"},
                json={
                    "product_id": "00000000-0000-4000-8000-000000000100",
                    "status": "BLOCKED",
                    "occurred_at": "2026-07-30T12:00:00Z",
                    "hard_block": True,
                    "idempotency_key": str(uuid4()),
                },
            )

    assert captured_payload.get("event_type") == "PRODUCT_BLOCKED"
    assert captured_payload.get("hard_block") is True


@pytest.mark.asyncio
async def test_any_modify_on_hard_blocked_returns_403():
    """PUT on a HARD_BLOCKED product returns 403."""
    product = _mock_product(
        product_id=PRODUCT_UUID,
        status=ProductStatus.HARD_BLOCKED,
        seller_id=SELLER_UUID,
        has_sku=True,
    )

    with make_test_client(product, []) as client:
        response = client.put(
            "/api/v1/products/00000000-0000-4000-8000-000000000100",
            json={"title": "Attempted change"},
        )

    assert response.status_code == 403, (
        f"Expected 403, got {response.status_code}: {response.text}"
    )
    data = response.json()
    assert data["code"] == "PRODUCT_HARD_BLOCKED"


@pytest.mark.asyncio
async def test_edited_event_on_hard_blocked_is_ignored():
    """
    EDITED is not a B2B-9 decision status (only MODERATED/BLOCKED).
    Invalid status → 422 validation; HARD_BLOCKED product stays terminal.
    """
    product = _mock_product(
        product_id=PRODUCT_UUID,
        status=ProductStatus.HARD_BLOCKED,
        seller_id=SELLER_UUID,
        has_sku=True,
    )
    sku = _mock_sku()

    with make_test_client(product, [sku]) as client:
        response = client.post(
            "/api/v1/moderation/events",
            headers={"X-Service-Key": "b2b-moderation-key"},
            json={
                "product_id": "00000000-0000-4000-8000-000000000100",
                "event_type": "EDITED",
                "occurred_at": "2026-07-30T12:00:00Z",
                "idempotency_key": str(uuid4()),
            },
        )

    assert response.status_code == 422, (
        f"Expected 422 for non-canon EDITED status, got {response.status_code}: {response.text}"
    )
    assert product.status == ProductStatus.HARD_BLOCKED


@pytest.mark.asyncio
async def test_deleted_event_removes_hard_blocked():
    """DELETE on HARD_BLOCKED product is rejected with 403."""
    product = _mock_product(
        product_id=PRODUCT_UUID,
        status=ProductStatus.HARD_BLOCKED,
        seller_id=SELLER_UUID,
        has_sku=True,
    )

    with make_test_client(product, []) as client:
        response = client.post(
            "/api/v1/products/00000000-0000-4000-8000-000000000100/delete"
        )

    assert response.status_code == 403, (
        f"Expected 403, got {response.status_code}: {response.text}"
    )
