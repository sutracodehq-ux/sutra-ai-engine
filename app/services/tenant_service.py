"""
Tenant Service — multi-key auth with expiry, scopes, and O(1) indexed lookup.

Each tenant can have unlimited API keys, each with:
  - environment: "live" or "test"
  - label: human-readable name ("Frontend App", "CI/CD")
  - scopes: permission list (default ["*"] = full access)
  - expires_at: optional TTL
  - last_used_at: touch on each auth
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.api_key import ApiKey
from app.models.tenant import Tenant


class TenantService:
    """Factory for tenant lifecycle and API key operations."""

    # ─── Key Utilities ──────────────────────────────────────

    @staticmethod
    def generate_api_key(prefix: str = "sk_live") -> str:
        """Generate a cryptographically secure API key."""
        random_part = secrets.token_urlsafe(32)
        return f"{prefix}_{random_part}"

    @staticmethod
    def hash_api_key(raw_key: str) -> str:
        """Hash an API key for storage. SHA-256 (keys are already high-entropy)."""
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def verify_api_key(raw_key: str, hashed: str) -> bool:
        """Verify a raw API key against its hash."""
        return hashlib.sha256(raw_key.encode()).hexdigest() == hashed

    @staticmethod
    def key_prefix(raw_key: str) -> str:
        """Extract a displayable prefix from an API key."""
        parts = raw_key.split("_", 2)
        if len(parts) >= 3:
            return f"{parts[0]}_{parts[1]}_{parts[2][:8]}..."
        return raw_key[:16] + "..."

    @staticmethod
    def key_environment(raw_key: str) -> str:
        """Detect if a key is live or test from its prefix."""
        if raw_key.startswith("sk_test_"):
            return "test"
        return "live"

    # ─── Tenant CRUD ────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        name: str,
        slug: str,
        identity_org_id: str | None = None,
        **kwargs,
    ) -> tuple[Tenant, str, str]:
        """
        Create a new tenant with initial live + test API keys.

        Returns (tenant, raw_live_key, raw_test_key).
        Raw keys are only available at creation time — save them.
        """
        raw_live_key = cls.generate_api_key("sk_live")
        raw_test_key = cls.generate_api_key("sk_test")

        # Create tenant (keep legacy columns for backcompat)
        tenant = Tenant(
            name=name,
            slug=slug,
            identity_org_id=identity_org_id,
            live_key_hash=cls.hash_api_key(raw_live_key),
            live_key_prefix=cls.key_prefix(raw_live_key),
            test_key_hash=cls.hash_api_key(raw_test_key),
            test_key_prefix=cls.key_prefix(raw_test_key),
            **kwargs,
        )
        db.add(tenant)
        await db.flush()

        # Create keys in the dedicated api_keys table
        live_key_record = ApiKey(
            tenant_id=tenant.id,
            key_hash=cls.hash_api_key(raw_live_key),
            key_prefix=cls.key_prefix(raw_live_key),
            raw_key=raw_live_key,
            environment="live",
            label="Default",
            scopes=["*"],
        )
        test_key_record = ApiKey(
            tenant_id=tenant.id,
            key_hash=cls.hash_api_key(raw_test_key),
            key_prefix=cls.key_prefix(raw_test_key),
            raw_key=raw_test_key,
            environment="test",
            label="Default",
            scopes=["*"],
        )
        db.add_all([live_key_record, test_key_record])
        await db.flush()

        return tenant, raw_live_key, raw_test_key

    @classmethod
    async def list_all(cls, db: AsyncSession) -> list[Tenant]:
        """List all tenants."""
        result = await db.execute(select(Tenant).order_by(Tenant.id))
        return list(result.scalars().all())

    @classmethod
    async def get_by_id(cls, db: AsyncSession, tenant_id: int) -> Tenant | None:
        """Get a tenant by ID."""
        result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        return result.scalar_one_or_none()

    @classmethod
    async def get_by_slug(cls, db: AsyncSession, slug: str) -> Tenant | None:
        """Get a tenant by slug."""
        result = await db.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()

    @classmethod
    async def update(cls, db: AsyncSession, tenant: Tenant, **updates) -> Tenant:
        """Update tenant fields."""
        allowed = {"name", "contact_email", "description", "config", "rate_limits", "webhook_url", "is_active", "website_url", "re_enrich_interval_days"}
        for key, value in updates.items():
            if key in allowed and value is not None:
                setattr(tenant, key, value)
        await db.flush()
        return tenant

    @classmethod
    async def deactivate(cls, db: AsyncSession, tenant: Tenant) -> Tenant:
        """Soft-deactivate a tenant and revoke all its API keys."""
        tenant.is_active = False
        # Revoke all keys
        await db.execute(
            update(ApiKey)
            .where(ApiKey.tenant_id == tenant.id)
            .values(is_active=False)
        )
        await db.flush()
        return tenant

    # ─── API Key Resolution (O(1) indexed lookup) ──────────

    @classmethod
    async def resolve_by_api_key(cls, db: AsyncSession, raw_key: str) -> tuple[Tenant | None, str, ApiKey | None]:
        """
        O(1) indexed hash lookup on api_keys table.

        Returns (tenant, environment, api_key_record) or (None, env, None).
        Also checks: is_active, not expired, tenant is_active.
        Touches last_used_at on success.
        """
        key_hash = cls.hash_api_key(raw_key)
        env = cls.key_environment(raw_key)

        # O(1): indexed unique lookup on key_hash
        result = await db.execute(
            select(ApiKey)
            .where(ApiKey.key_hash == key_hash)
            .options(selectinload(ApiKey.tenant))
        )
        api_key = result.scalar_one_or_none()

        if api_key is None:
            return None, env, None

        # Check key is active
        if not api_key.is_active:
            return None, env, None

        # Check expiry
        if api_key.is_expired:
            return None, env, None

        # Check tenant is active
        if not api_key.tenant.is_active:
            return None, env, None

        # Touch last_used_at (fire-and-forget, non-blocking)
        api_key.last_used_at = datetime.now(timezone.utc)

        return api_key.tenant, api_key.environment, api_key

    # ─── API Key Management ────────────────────────────────

    @classmethod
    async def create_api_key(
        cls,
        db: AsyncSession,
        tenant_id: int,
        *,
        environment: str = "live",
        tier: str = "standard",
        label: str | None = None,
        scopes: list[str] | None = None,
        expires_in_days: int | None = None,
    ) -> tuple[ApiKey, str]:
        """
        Create a new API key for a tenant.

        Validates:
          - tier is not protected (master/internal)
          - scopes are valid against access_control.yaml

        Returns (api_key_record, raw_key).
        Raw key is only available at creation time.
        """
        from app.lib.access_engine import get_access_engine
        engine = get_access_engine()

        # Validate tier — prevent creating protected tiers via API
        if engine.is_tier_protected(tier):
            raise ValueError(f"Cannot create keys with protected tier '{tier}'")

        # Validate tier exists
        if not engine.get_tier(tier):
            raise ValueError(f"Unknown tier '{tier}'. Available: {engine.get_available_tiers()}")

        # Validate scopes against YAML canonical list
        effective_scopes = scopes or ["*"]
        invalid = engine.validate_scopes(effective_scopes)
        if invalid:
            raise ValueError(f"Invalid scopes: {invalid}. See access_control.yaml for available scopes.")

        prefix = f"sk_{environment}"
        raw_key = cls.generate_api_key(prefix)

        expires_at = None
        if expires_in_days is not None and expires_in_days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

        api_key = ApiKey(
            tenant_id=tenant_id,
            key_hash=cls.hash_api_key(raw_key),
            key_prefix=cls.key_prefix(raw_key),
            raw_key=raw_key,
            environment=environment,
            tier=tier,
            label=label,
            scopes=effective_scopes,
            expires_at=expires_at,
        )
        db.add(api_key)
        await db.flush()

        return api_key, raw_key

    @classmethod
    async def list_api_keys(cls, db: AsyncSession, tenant_id: int) -> list[ApiKey]:
        """List all API keys for a tenant (active only)."""
        result = await db.execute(
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id, ApiKey.is_active.is_(True))
            .order_by(ApiKey.created_at.desc())
        )
        return list(result.scalars().all())

    @classmethod
    async def get_api_key(cls, db: AsyncSession, key_id: int, tenant_id: int) -> ApiKey | None:
        """Get a specific API key by ID, scoped to a tenant."""
        result = await db.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    @classmethod
    async def revoke_api_key(cls, db: AsyncSession, api_key: ApiKey) -> ApiKey:
        """Soft-revoke an API key."""
        api_key.is_active = False
        await db.flush()
        return api_key

    @classmethod
    async def rotate_api_key(cls, db: AsyncSession, api_key: ApiKey) -> tuple[ApiKey, str]:
        """
        Rotate: revoke old key + create new key with same label/environment/scopes.

        Returns (new_api_key, raw_key).
        """
        # Revoke old
        api_key.is_active = False

        # Create new with same config (including tier)
        new_key, raw_key = await cls.create_api_key(
            db,
            api_key.tenant_id,
            environment=api_key.environment,
            tier=api_key.tier,
            label=api_key.label,
            scopes=api_key.scopes,
        )
        return new_key, raw_key

    @classmethod
    async def cleanup_expired(cls, db: AsyncSession) -> int:
        """Delete expired keys older than 30 days. Returns count of deleted keys."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        result = await db.execute(
            select(ApiKey).where(
                ApiKey.expires_at.isnot(None),
                ApiKey.expires_at < cutoff,
            )
        )
        expired = result.scalars().all()
        for key in expired:
            await db.delete(key)
        await db.flush()
        return len(expired)

    # ─── Legacy Compatibility ──────────────────────────────

    @classmethod
    async def rotate_live_key(cls, db: AsyncSession, tenant: Tenant) -> str:
        """Legacy: rotate the default live key."""
        result = await db.execute(
            select(ApiKey).where(
                ApiKey.tenant_id == tenant.id,
                ApiKey.environment == "live",
                ApiKey.is_active.is_(True),
            ).order_by(ApiKey.created_at.asc()).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            new_key, raw_key = await cls.rotate_api_key(db, existing)
        else:
            new_key, raw_key = await cls.create_api_key(db, tenant.id, environment="live", label="Default")

        # Update legacy columns too
        tenant.live_key_hash = cls.hash_api_key(raw_key)
        tenant.live_key_prefix = cls.key_prefix(raw_key)
        await db.flush()
        return raw_key

    @classmethod
    async def rotate_test_key(cls, db: AsyncSession, tenant: Tenant) -> str:
        """Legacy: rotate the default test key."""
        result = await db.execute(
            select(ApiKey).where(
                ApiKey.tenant_id == tenant.id,
                ApiKey.environment == "test",
                ApiKey.is_active.is_(True),
            ).order_by(ApiKey.created_at.asc()).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            new_key, raw_key = await cls.rotate_api_key(db, existing)
        else:
            new_key, raw_key = await cls.create_api_key(db, tenant.id, environment="test", label="Default")

        # Update legacy columns too
        tenant.test_key_hash = cls.hash_api_key(raw_key)
        tenant.test_key_prefix = cls.key_prefix(raw_key)
        await db.flush()
        return raw_key
