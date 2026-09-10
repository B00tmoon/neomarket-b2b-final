from __future__ import annotations

from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.base import get_db
from src.models.product import Product, ProductBlockingReason, ProductStatus
from src.models.sku import SKU
from src.schemas.errors import ErrorCode
from src.schemas.product import (
    BlockingReasonResponse,
    CatalogListResponse,
    FieldReportResponse,
    ProductCreate,
    ProductPublicResponse,
    ProductResponse,
    ProductStatusEnum,
    ProductUpdate,
    SKUPublicResponse,
    SKUResponse,
)
from src.settings import settings

router = APIRouter()


def _parse_seller_id(request: Request) -> UUID | None:
    """Extract seller_id from JWT / request.state.user / X-Seller-Id as UUID."""
    seller_id = getattr(request.state, "user", None)
    if seller_id is None:
        seller_id = request.headers.get("X-Seller-Id")
    if seller_id is None:
        return None
    try:
        return UUID(str(seller_id))
    except (ValueError, TypeError, AttributeError):
        return None


def _check_hard_blocked(product: Product) -> None:
    """Raise 403 if product is HARD_BLOCKED."""
    if product.status == ProductStatus.HARD_BLOCKED:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PRODUCT_HARD_BLOCKED",
                "message": "Product is HARD_BLOCKED — modification is forbidden",
                "details": {"field": "status"},
            },
        )


@router.post("/products", response_model=ProductResponse, status_code=201)
async def create_product(
    product_data: ProductCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ProductResponse:
    """
    Create a product card only (US-B2B-01 / B2B-1).

    seller_id is taken from JWT / request.state.user / X-Seller-Id — never from body.
    Body: title (1-255), description (1-5000), category_id, images (≥1),
    optional characteristics. SKUs are NOT accepted and NOT created
    (use POST /api/v1/skus). On success: status=CREATED, skus=[], HTTP 201.
    """
    seller_id = _parse_seller_id(request)
    if seller_id is None:
        raw = getattr(request.state, "user", None) or request.headers.get("X-Seller-Id")
        if raw is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "UNAUTHORIZED",
                    "message": "JWT token required",
                },
            )
        raise HTTPException(
            status_code=401,
            detail={
                "code": "UNAUTHORIZED",
                "message": "Invalid JWT seller_id (must be UUID)",
            },
        )

    from src.cruds.products import create_product as crud_create_product

    db_product = await crud_create_product(product_data, seller_id, db)
    return db_product


def _parse_ids_param(ids: Optional[str]) -> list[UUID] | None:
    if not ids:
        return None
    id_list: list[UUID] = []
    for part in ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            id_list.append(UUID(part))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": ErrorCode.VALIDATION_ERROR,
                    "message": f"Invalid UUID: {part}",
                },
            )
    return id_list


def _to_public_product(product: Product) -> ProductPublicResponse:
    skus = [SKUPublicResponse.from_orm_sku(s) for s in (product.skus or [])]
    return ProductPublicResponse(
        id=product.id,
        seller_id=product.seller_id,
        title=product.title,
        description=product.description or "",
        category_id=product.category_id,
        status=(
            product.status.value
            if hasattr(product.status, "value")
            else str(product.status)
        ),
        slug=product.slug or "",
        images=list(product.images or []),
        characteristics=list(product.characteristics or []),
        skus=skus,
        created_at=getattr(product, "created_at", None),
        updated_at=getattr(product, "updated_at", None),
    )


@router.get("/products")
async def list_products(
    request: Request,
    seller_id: Optional[UUID] = Query(None, description="Фильтр по продавцу"),
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
    status: Optional[ProductStatusEnum] = Query(None, description="Фильтр по статусу"),
    category_id: Optional[UUID] = Query(None, description="Фильтр по категории"),
    ids: Optional[str] = Query(
        None, description="Batch IDs for B2C catalog, comma-separated UUIDs"
    ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    limit: Optional[int] = Query(None, ge=1, le=100),
    offset: Optional[int] = Query(None, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /api/v1/products — dual mode (B2B-7 + seller list).

    Catalog mode (X-Service-Key == b2c_to_b2b_key):
      - no seller JWT required
      - visibility: status=MODERATED, deleted=false, ≥1 SKU with active_quantity>0
      - HARD_BLOCKED excluded (not MODERATED)
      - response: CatalogListResponse without cost_price / reserved_quantity

    Seller mode (JWT / X-Seller-Id, no service key):
      - own products (or filter by seller_id query)
      - full ProductResponse list
    """
    service_key = x_service_key or request.headers.get("X-Service-Key")
    is_catalog_mode = bool(service_key)

    if is_catalog_mode:
        if service_key != settings.b2c_to_b2b_key:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": ErrorCode.UNAUTHORIZED,
                    "message": "Invalid or missing X-Service-Key",
                },
            )
        return await _list_catalog(
            db=db,
            category_id=category_id,
            ids=_parse_ids_param(ids),
            limit=limit if limit is not None else size,
            offset=offset if offset is not None else (page - 1) * size,
        )

    # Seller mode — require seller identity
    parsed_seller = _parse_seller_id(request)
    effective_seller = seller_id or parsed_seller
    if effective_seller is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": ErrorCode.UNAUTHORIZED,
                "message": "X-Service-Key or seller JWT required",
            },
        )

    query = select(Product).where(Product.deleted == False)  # noqa: E712
    query = query.where(Product.seller_id == effective_seller)
    if status:
        query = query.where(Product.status == ProductStatus(status.value))
    if category_id:
        query = query.where(Product.category_id == category_id)
    id_list = _parse_ids_param(ids)
    if id_list:
        query = query.where(Product.id.in_(id_list))

    query = query.options(
        selectinload(Product.images),
        selectinload(Product.characteristics),
        selectinload(Product.skus).selectinload(SKU.characteristics),
        selectinload(Product.skus).selectinload(SKU.images),
    )
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    return list(result.scalars().all())


async def _list_catalog(
    *,
    db: AsyncSession,
    category_id: Optional[UUID],
    ids: list[UUID] | None,
    limit: int,
    offset: int,
) -> CatalogListResponse:
    """B2C catalog: MODERATED + not deleted + in-stock SKU; public payload only."""
    in_stock = exists(
        select(SKU.id).where(
            SKU.product_id == Product.id,
            SKU.active_quantity > 0,
        )
    )
    filters = [
        Product.deleted == False,  # noqa: E712
        Product.status == ProductStatus.MODERATED,
        in_stock,
    ]
    if category_id is not None:
        filters.append(Product.category_id == category_id)
    if ids:
        filters.append(Product.id.in_(ids))

    count_q = select(func.count()).select_from(Product).where(*filters)
    total = (await db.execute(count_q)).scalar_one()

    query = (
        select(Product)
        .where(*filters)
        .options(
            selectinload(Product.images),
            selectinload(Product.characteristics),
            selectinload(Product.skus).selectinload(SKU.characteristics),
            selectinload(Product.skus).selectinload(SKU.images),
        )
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    products = list(result.scalars().all())
    items = [_to_public_product(p) for p in products]
    return CatalogListResponse(
        items=items,
        total_count=int(total),
        limit=limit,
        offset=offset,
    )


@router.post("/products/batch")
async def get_products_batch(
    product_ids: list[UUID], db: AsyncSession = Depends(get_db)
):
    """Получить несколько товаров по списку UUID (batch запрос для B2C)."""
    products = await db.execute(select(Product).where(Product.id.in_(product_ids)))
    product_list = products.scalars().all()

    result = {}
    for product in product_list:
        min_price = min([sku.price for sku in product.skus], default=0)
        result[str(product.id)] = {
            "product_id": str(product.id),
            "title": product.title,
            "description": product.description,
            "category_id": str(product.category_id),
            "status": product.status.value,
            "main_image_url": product.images[0].url if product.images else "",
            "images": [img.url for img in product.images],
            "min_price": min_price,
            "is_available": product.status == ProductStatus.MODERATED
            and not product.deleted,
            "characteristics": {
                char.name: char.value for char in product.characteristics
            },
            "skus": [
                {
                    "sku_id": str(sku.id),
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "price": sku.price,
                    "active_quantity": sku.active_quantity,
                    "blocked_quantity": sku.blocked_quantity,
                    "active": sku.active,
                    "characteristics": [
                        {"name": c.name, "value": c.value} for c in sku.characteristics
                    ],
                }
                for sku in product.skus
            ],
        }
    return result


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": ErrorCode.NOT_FOUND,
            "message": "Product not found",
        },
    )


async def _build_product_detail(product: Product, db: AsyncSession) -> ProductResponse:
    """Assemble full seller-view payload including blocking_reason / field_reports."""
    status_val = (
        product.status.value
        if hasattr(product.status, "value")
        else str(product.status)
    )
    blocked = status_val in (
        ProductStatus.BLOCKED.value,
        ProductStatus.HARD_BLOCKED.value,
    )

    blocking_reason: BlockingReasonResponse | None = None
    if blocked:
        title = None
        reason_id = product.blocking_reason_id
        if reason_id is not None:
            br = await db.get(ProductBlockingReason, reason_id)
            if br is not None:
                title = br.name
        comment = product.blocking_comment or ""
        title = title or comment or "Blocked"
        # Always expose structured reason when product is blocked
        blocking_reason = BlockingReasonResponse(
            id=reason_id or uuid4(),
            title=title,
            comment=comment,
        )

    field_reports: list[FieldReportResponse] = []
    raw_reports = product.field_reports or []
    if isinstance(raw_reports, list):
        for item in raw_reports:
            if not isinstance(item, dict):
                continue
            field_name = item.get("field_name") or item.get("field") or ""
            comment = item.get("comment") or ""
            if not field_name and not comment:
                continue
            sku_raw = item.get("sku_id")
            sku_id = None
            if sku_raw:
                try:
                    sku_id = UUID(str(sku_raw))
                except (ValueError, TypeError):
                    sku_id = None
            field_reports.append(
                FieldReportResponse(
                    field_name=str(field_name),
                    sku_id=sku_id,
                    comment=str(comment),
                )
            )

    skus = [SKUResponse.from_orm_sku(s) for s in (product.skus or [])]

    return ProductResponse(
        id=product.id,
        seller_id=product.seller_id,
        title=product.title,
        description=product.description or "",
        category_id=product.category_id,
        status=status_val,
        slug=product.slug or "",
        images=list(product.images or []),
        characteristics=list(product.characteristics or []),
        skus=skus,
        created_at=product.created_at,
        updated_at=product.updated_at,
        deleted=bool(product.deleted),
        blocked=blocked,
        blocking_comment=product.blocking_comment,
        blocking_reason_id=product.blocking_reason_id,
        moderator_comment=product.blocking_comment,
        blocking_reason=blocking_reason,
        field_reports=field_reports,
    )


@router.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
):
    """
    GET /api/v1/products/{id} — B2B-5 view product card.

    Seller mode (JWT / X-Seller-Id): own products only; foreign → 404 (not 403).
    Service mode (X-Service-Key from Moderation): any product, no ownership check.
    BLOCKED/HARD_BLOCKED → blocking_reason + field_reports filled.
    MODERATED → blocking_reason=null, field_reports=[].
    """
    seller_id = _parse_seller_id(request)
    is_service = bool(x_service_key)

    result = await db.execute(
        select(Product)
        .where(Product.id == product_id)
        .options(
            selectinload(Product.images),
            selectinload(Product.characteristics),
            selectinload(Product.skus).selectinload(SKU.characteristics),
            selectinload(Product.skus).selectinload(SKU.images),
        )
    )
    product = result.scalar_one_or_none()

    if product is None or product.deleted:
        raise _not_found()

    # IDOR: seller may only see own products; service key bypasses ownership
    if not is_service:
        if seller_id is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": ErrorCode.UNAUTHORIZED,
                    "message": "JWT token required",
                },
            )
        if product.seller_id != seller_id:
            raise _not_found()

    return await _build_product_detail(product, db)


@router.put("/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: UUID,
    product_update: ProductUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Обновить товар."""
    db_product = await db.get(Product, product_id)
    if not db_product or db_product.deleted:
        raise HTTPException(status_code=404, detail="Product not found")

    _check_hard_blocked(db_product)

    update_data = product_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_product, field, value)

    await db.commit()
    await db.refresh(db_product)
    return db_product


@router.post("/products/{product_id}/submit-moderation", status_code=204)
async def submit_for_moderation(
    product_id: UUID, db: AsyncSession = Depends(get_db)
):
    """Отправить товар на модерацию."""
    db_product = await db.get(Product, product_id)
    if not db_product or db_product.deleted:
        raise HTTPException(status_code=404, detail="Product not found")

    _check_hard_blocked(db_product)

    if db_product.status != ProductStatus.CREATED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot submit product with status {db_product.status.value}",
        )

    db_product.status = ProductStatus.ON_MODERATION
    await db.commit()
    return None


@router.post("/products/{product_id}/delete", status_code=204)
async def delete_product(product_id: UUID, db: AsyncSession = Depends(get_db)):
    """Удалить товар (мягкое удаление)."""
    db_product = await db.get(Product, product_id)
    if not db_product or db_product.deleted:
        raise HTTPException(status_code=404, detail="Product not found")

    _check_hard_blocked(db_product)

    db_product.deleted = True
    await db.commit()
    return None
