"""Pydantic schemas for tenant management and API key operations."""

from pydantic import BaseModel, Field


# ─── Tenant Schemas ─────────────────────────────────────────

class TenantCreate(BaseModel):
    """POST /v1/tenants request body."""
    name: str = Field(..., min_length=1, max_length=255, description="Organization name")
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$", description="URL-safe identifier")
    contact_email: str | None = Field(None, description="Admin contact email")
    description: str | None = Field(None, description="Organization description")
    website_url: str | None = Field(None, description="Website URL — triggers auto brand enrichment")
    config: dict | None = Field(default_factory=dict, description="Tenant-level config overrides")
    rate_limits: dict | None = Field(None, description="Rate limit overrides (null = use global)")


class TenantUpdate(BaseModel):
    """PATCH /v1/tenants/{id} request body."""
    name: str | None = Field(None, min_length=1, max_length=255)
    contact_email: str | None = None
    description: str | None = None
    website_url: str | None = None
    config: dict | None = None
    rate_limits: dict | None = None
    webhook_url: str | None = None


class TenantResponse(BaseModel):
    """Tenant info response with full keys."""
    id: int
    name: str
    slug: str
    is_active: bool
    contact_email: str | None = None
    description: str | None = None
    website_url: str | None = None
    config: dict | None = None
    live_api_key: str | None = Field(None, description="Full production API key")
    test_api_key: str | None = Field(None, description="Full sandbox API key")
    api_key_count: int = Field(0, description="Total active API keys")
    created_at: str


class TenantCreated(BaseModel):
    """Response after creating a tenant — includes BOTH raw API keys (shown only once)."""
    id: int
    name: str
    slug: str
    is_active: bool
    contact_email: str | None = None
    description: str | None = None
    website_url: str | None = None
    config: dict | None = None
    live_api_key: str = Field(..., description="Production API key (sk_live_*) — save it, cannot be retrieved again")
    test_api_key: str = Field(..., description="Sandbox API key (sk_test_*) — save it, cannot be retrieved again")
    live_key_prefix: str
    test_key_prefix: str
    created_at: str


class TenantList(BaseModel):
    """GET /v1/tenants response."""
    tenants: list[TenantResponse]
    total: int


# ─── API Key Schemas ────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    """POST /v1/tenants/{id}/api-keys request body."""
    environment: str = Field("live", pattern=r"^(live|test)$", description="'live' or 'test'")
    tier: str = Field("standard", pattern=r"^(standard|restricted)$", description="Access tier: 'standard' (full) or 'restricted' (scoped)")
    label: str | None = Field(None, max_length=100, description="Human-readable label, e.g. 'Frontend App'")
    scopes: list[str] | None = Field(None, description="Permission scopes (default: ['*'] = full access)")
    expires_in_days: int | None = Field(None, ge=1, le=3650, description="Expiry in days (null = never)")


class ApiKeyResponse(BaseModel):
    """API key info with full key."""
    id: int
    environment: str
    tier: str = "standard"
    label: str | None = None
    key_prefix: str
    api_key: str | None = Field(None, description="Full API key")
    scopes: list[str] | None = None
    expires_at: str | None = None
    last_used_at: str | None = None
    is_active: bool
    created_at: str


class ApiKeyCreated(BaseModel):
    """Response after creating an API key — raw key included (shown only once)."""
    id: int
    api_key: str = Field(..., description="Raw API key — save it, cannot be retrieved again")
    key_prefix: str
    environment: str
    tier: str = "standard"
    label: str | None = None
    scopes: list[str] | None = None
    expires_at: str | None = None


class ApiKeyRotated(BaseModel):
    """Response after rotating an API key."""
    old_key_id: int = Field(..., description="ID of the revoked key")
    new_key: ApiKeyCreated = Field(..., description="The new key (save it)")


# ─── Legacy (backward compat) ──────────────────────────────

class TenantUsage(BaseModel):
    """GET /v1/tenants/{id}/usage response."""
    tenant_id: int
    period: str
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    breakdown: list[dict] = []
