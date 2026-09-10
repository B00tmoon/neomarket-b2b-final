"""Tests for US-B2B-08: POST /api/v1/reserve and /unreserve."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import get_db as real_get_db
from src.models.inventory import ReserveOperation
from src.models.sku import SKU
from src.settings import settings

SKU_A = UUID("00000000-0000-4000-8000-000000000701")
SKU_B = UUID("00000000-0000-4000-8000-000000000702")
KEY = UUID("00000000-0000-4000-8000-000000000800")


def _sku(sku_id: UUID, active: int, reserved: int = 0) -> MagicMock:
    sku = MagicMock(spec=SKU)
    sku.id = sku_id
    sku.active_quantity = active
    sku.blocked_quantity = reserved
    sku.stock_quantity = active + reserved
    return sku


class _Session:
    def __init__(self, skus: dict[UUID, MagicMock], ops: dict | None = None):
        self.skus = skus
        self.ops = ops or {}
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    async def get(self, model, ident, **kwargs):
        if model is ReserveOperation:
            return self.ops.get(ident)
        if model is SKU:
            return self.skus.get(ident)
        return None

    async def execute(self, query):
        # Return all locked SKUs
        result = MagicMock()
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=list(self.skus.values())))
        )
        return result

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, ReserveOperation) or getattr(obj, "idempotency_key", None):
            self.ops[obj.idempotency_key] = obj

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def make_client(session: _Session):
    async def override_get_db():
        yield session

    app.dependency_overrides[real_get_db] = override_get_db
    return TestClient(
        app,
        base_url="http://test",
        headers={"X-Service-Key": settings.b2c_to_b2b_key},
    )


@pytest.mark.asyncio
async def test_reserve_all_skus_succeeds():
    """All items in stock → reserved=true, quantities moved active→reserved."""
    a = _sku(SKU_A, active=5, reserved=0)
    b = _sku(SKU_B, active=3, reserved=1)
    session = _Session({SKU_A: a, SKU_B: b})
    client = make_client(session)

    with patch("src.services.inventory.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/reserve",
            json={
                "idempotency_key": str(KEY),
                "items": [
                    {"sku_id": str(SKU_A), "quantity": 2},
                    {"sku_id": str(SKU_B), "quantity": 1},
                ],
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reserved"] is True
    assert a.active_quantity == 3
    assert a.blocked_quantity == 2
    assert b.active_quantity == 2
    assert b.blocked_quantity == 2
    assert session.committed is True


@pytest.mark.asyncio
async def test_partial_insufficient_stock_returns_409_all_rollback():
    """One SKU short → 409, no quantity changes (full rollback)."""
    a = _sku(SKU_A, active=5, reserved=0)
    b = _sku(SKU_B, active=1, reserved=0)
    session = _Session({SKU_A: a, SKU_B: b})
    client = make_client(session)

    response = client.post(
        "/api/v1/reserve",
        json={
            "idempotency_key": str(uuid4()),
            "items": [
                {"sku_id": str(SKU_A), "quantity": 2},
                {"sku_id": str(SKU_B), "quantity": 5},  # insufficient
            ],
        },
    )

    assert response.status_code == 409, response.text
    # quantities unchanged
    assert a.active_quantity == 5
    assert a.blocked_quantity == 0
    assert b.active_quantity == 1
    assert b.blocked_quantity == 0
    assert session.rolled_back is True
    data = response.json()
    # flat error or detail with failed_items
    body = data.get("detail") if isinstance(data.get("detail"), dict) else data
    assert body.get("reserved") is False or body.get("code") == "INSUFFICIENT_STOCK"


@pytest.mark.asyncio
async def test_idempotent_reserve_returns_200_without_double_deduction():
    """Same idempotency_key twice → second call returns cached result, no double deduct."""
    a = _sku(SKU_A, active=5, reserved=0)
    cached = ReserveOperation(
        idempotency_key=KEY,
        result={
            "reserved": True,
            "items": [
                {
                    "sku_id": str(SKU_A),
                    "quantity": 2,
                    "active_quantity": 3,
                    "reserved_quantity": 2,
                }
            ],
            "failed_items": [],
        },
    )
    session = _Session({SKU_A: a}, ops={KEY: cached})
    client = make_client(session)

    response = client.post(
        "/api/v1/reserve",
        json={
            "idempotency_key": str(KEY),
            "items": [{"sku_id": str(SKU_A), "quantity": 2}],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["reserved"] is True
    # must not change stock again
    assert a.active_quantity == 5
    assert a.blocked_quantity == 0


@pytest.mark.asyncio
async def test_sku_out_of_stock_event_emitted():
    """When active_quantity reaches 0 → SKU_OUT_OF_STOCK event is sent."""
    a = _sku(SKU_A, active=2, reserved=0)
    session = _Session({SKU_A: a})
    client = make_client(session)

    captured: dict = {}

    async def mock_post(url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return MagicMock(status_code=200)

    with patch("src.services.inventory.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_ctx

        response = client.post(
            "/api/v1/reserve",
            json={
                "idempotency_key": str(uuid4()),
                "items": [{"sku_id": str(SKU_A), "quantity": 2}],
            },
        )

    assert response.status_code == 200, response.text
    assert a.active_quantity == 0
    assert a.blocked_quantity == 2
    assert "json" in captured
    assert captured["json"]["event_type"] == "SKU_OUT_OF_STOCK"
    assert captured["json"]["sku_id"] == str(SKU_A)


@pytest.mark.asyncio
async def test_unreserve_restores_quantities():
    """unreserve moves reserved back to active."""
    a = _sku(SKU_A, active=1, reserved=3)
    session = _Session({SKU_A: a})
    client = make_client(session)

    response = client.post(
        "/api/v1/unreserve",
        json={
            "order_id": str(uuid4()),
            "items": [{"sku_id": str(SKU_A), "quantity": 2}],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert a.active_quantity == 3
    assert a.blocked_quantity == 1
    assert session.committed is True
