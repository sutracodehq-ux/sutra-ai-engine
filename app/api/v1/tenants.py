"""
Tenant management routes — admin-only (requires master API key).

Full CRUD for tenants + multi-key management with expiry and scopes.
All endpoints accessible via Swagger: http://localhost:8090/docs
"""

from fastapi import APIRouter, HTTPException, status

from app.dependencies import DbSession, MasterKeyAuth
from app.schemas.tenant import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyResponse,
    ApiKeyRotated,
    TenantCreate,
    TenantCreated,
    TenantList,
    TenantResponse,
    TenantUpdate,
)
from app.services.tenant_service import TenantService

router = APIRouter(prefix="/tenants", tags=["tenants"])


# ─── Tenant CRUD ─────────────────────────────────────────────


@router.get("", response_model=TenantList)
async def list_tenants(db: DbSession, _: MasterKeyAuth):
    """List all registered tenants."""
    tenants = await TenantService.list_all(db)
    items = []
    for t in tenants:
        api_keys = await TenantService.list_api_keys(db, t.id)
        live_key = next((k.raw_key for k in api_keys if k.environment == "live" and k.raw_key), None)
        test_key = next((k.raw_key for k in api_keys if k.environment == "test" and k.raw_key), None)
        items.append(TenantResponse(
            id=t.id,
            name=t.name,
            slug=t.slug,
            is_active=t.is_active,
            contact_email=t.contact_email,
            description=t.description,
            config=t.config,
            live_api_key=live_key or t.live_key_prefix,
            test_api_key=test_key or t.test_key_prefix,
            api_key_count=len(api_keys),
            created_at=t.created_at.isoformat(),
        ))
    return TenantList(tenants=items, total=len(items))


@router.post("", response_model=TenantCreated, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, db: DbSession, _: MasterKeyAuth):
    """
    Register a new consuming product.

    Returns BOTH API keys — production (sk_live_*) and sandbox (sk_test_*).
    These are shown only once — save them.
    """
    existing = await TenantService.get_by_slug(db, body.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant '{body.slug}' already exists",
        )

    tenant, live_key, test_key = await TenantService.create(
        db,
        name=body.name,
        slug=body.slug,
        contact_email=body.contact_email,
        description=body.description,
        website_url=body.website_url,
        config=body.config or {},
        rate_limits=body.rate_limits,
    )

    return TenantCreated(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        is_active=tenant.is_active,
        contact_email=tenant.contact_email,
        description=tenant.description,
        config=tenant.config,
        live_api_key=live_key,
        test_api_key=test_key,
        live_key_prefix=tenant.live_key_prefix,
        test_key_prefix=tenant.test_key_prefix,
        created_at=tenant.created_at.isoformat(),
    )


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(tenant_id: int, db: DbSession, _: MasterKeyAuth):
    """Get tenant info by ID. Shows key prefixes but NOT raw keys."""
    tenant = await TenantService.get_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    api_keys = await TenantService.list_api_keys(db, tenant.id)
    live_key = next((k.raw_key for k in api_keys if k.environment == "live" and k.raw_key), None)
    test_key = next((k.raw_key for k in api_keys if k.environment == "test" and k.raw_key), None)

    return TenantResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        is_active=tenant.is_active,
        contact_email=tenant.contact_email,
        description=tenant.description,
        config=tenant.config,
        live_api_key=live_key or tenant.live_key_prefix,
        test_api_key=test_key or tenant.test_key_prefix,
        api_key_count=len(api_keys),
        created_at=tenant.created_at.isoformat(),
    )


@router.patch("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(tenant_id: int, body: TenantUpdate, db: DbSession, _: MasterKeyAuth):
    """Update tenant fields (name, config, rate limits, etc.)."""
    tenant = await TenantService.get_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    updates = body.model_dump(exclude_unset=True)
    tenant = await TenantService.update(db, tenant, **updates)
    api_keys = await TenantService.list_api_keys(db, tenant.id)
    live_key = next((k.raw_key for k in api_keys if k.environment == "live" and k.raw_key), None)
    test_key = next((k.raw_key for k in api_keys if k.environment == "test" and k.raw_key), None)

    return TenantResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        is_active=tenant.is_active,
        contact_email=tenant.contact_email,
        description=tenant.description,
        config=tenant.config,
        live_api_key=live_key or tenant.live_key_prefix,
        test_api_key=test_key or tenant.test_key_prefix,
        api_key_count=len(api_keys),
        created_at=tenant.created_at.isoformat(),
    )


@router.delete("/{tenant_id}", status_code=status.HTTP_200_OK)
async def deactivate_tenant(tenant_id: int, db: DbSession, _: MasterKeyAuth):
    """Deactivate a tenant and revoke all its API keys."""
    tenant = await TenantService.get_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    await TenantService.deactivate(db, tenant)
    return {"status": "deactivated", "tenant_id": tenant.id, "slug": tenant.slug}


# ─── API Key Management ─────────────────────────────────────


@router.get("/{tenant_id}/api-keys", response_model=list[ApiKeyResponse], tags=["api-keys"])
async def list_api_keys(tenant_id: int, db: DbSession, _: MasterKeyAuth):
    """List all active API keys for a tenant."""
    tenant = await TenantService.get_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    keys = await TenantService.list_api_keys(db, tenant_id)
    return [
        ApiKeyResponse(
            id=k.id,
            environment=k.environment,
            tier=k.tier,
            label=k.label,
            key_prefix=k.key_prefix,
            api_key=k.raw_key,
            scopes=k.scopes,
            expires_at=k.expires_at.isoformat() if k.expires_at else None,
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
            is_active=k.is_active,
            created_at=k.created_at.isoformat(),
        )
        for k in keys
    ]


@router.post("/{tenant_id}/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED, tags=["api-keys"])
async def create_api_key(tenant_id: int, body: ApiKeyCreate, db: DbSession, _: MasterKeyAuth):
    """
    Create an additional API key for a tenant.

    **Tiers:**
    - `standard` — full access (default)
    - `restricted` — only explicit scopes apply

    Returns the raw key — save it, cannot be retrieved again.
    """
    tenant = await TenantService.get_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant is deactivated")

    try:
        api_key, raw_key = await TenantService.create_api_key(
            db,
            tenant_id,
            environment=body.environment,
            tier=body.tier,
            label=body.label,
            scopes=body.scopes,
            expires_in_days=body.expires_in_days,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return ApiKeyCreated(
        id=api_key.id,
        api_key=raw_key,
        key_prefix=api_key.key_prefix,
        environment=api_key.environment,
        tier=api_key.tier,
        label=api_key.label,
        scopes=api_key.scopes,
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None,
    )


@router.delete("/{tenant_id}/api-keys/{key_id}", status_code=status.HTTP_200_OK, tags=["api-keys"])
async def revoke_api_key(tenant_id: int, key_id: int, db: DbSession, _: MasterKeyAuth):
    """Revoke (soft-delete) a specific API key. It will immediately stop working."""
    api_key = await TenantService.get_api_key(db, key_id, tenant_id)
    if not api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    await TenantService.revoke_api_key(db, api_key)
    return {"status": "revoked", "key_id": key_id, "key_prefix": api_key.key_prefix}


@router.post("/{tenant_id}/api-keys/{key_id}/rotate", response_model=ApiKeyRotated, tags=["api-keys"])
async def rotate_api_key(tenant_id: int, key_id: int, db: DbSession, _: MasterKeyAuth):
    """
    Rotate a specific API key: revokes the old key and creates a new one
    with the same environment, label, and scopes.

    Returns the new raw key — save it, cannot be retrieved again.
    """
    api_key = await TenantService.get_api_key(db, key_id, tenant_id)
    if not api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if not api_key.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot rotate a revoked key")

    new_key, raw_key = await TenantService.rotate_api_key(db, api_key)

    return ApiKeyRotated(
        old_key_id=key_id,
        new_key=ApiKeyCreated(
            id=new_key.id,
            api_key=raw_key,
            key_prefix=new_key.key_prefix,
            environment=new_key.environment,
            tier=new_key.tier,
            label=new_key.label,
            scopes=new_key.scopes,
            expires_at=new_key.expires_at.isoformat() if new_key.expires_at else None,
        ),
    )


# ─── Brand Auto-Enrichment (Software Factory: manual pipeline trigger) ──

@router.post("/{slug}/enrich", summary="Enrich brand context from website")
async def enrich_tenant_brand(slug: str, db: DbSession, _: MasterKeyAuth):
    """
    Manually trigger the Brand Enrichment pipeline for a tenant.

    Software Factory: SCRAPE → EXTRACT → GATE → STORE → STAMP.
    Requires tenant.website_url to be set.

    Use cases:
    - Initial setup before chatbot goes live
    - Force re-enrich after website redesign
    - Admin dashboard "Refresh Brand Context" button
    """
    tenant = await TenantService.get_by_slug(db, slug)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    if not tenant.website_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tenant has no website_url set. Update the tenant first.",
        )

    from app.services.intelligence.brand_enricher import get_brand_enricher

    result = await get_brand_enricher().enrich(tenant, db)

    if result:
        return {
            "status": "enriched",
            "slug": slug,
            "extracted_fields": list(result.keys()),
            "brand_description": result.get("brand_description", ""),
        }
    else:
        return {
            "status": "failed",
            "slug": slug,
            "message": "Enrichment failed — check quality gate or website accessibility.",
        }

