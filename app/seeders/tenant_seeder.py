"""
Tenant Seeder — creates default tenants with dual API keys.

Like Laravel's DatabaseSeeder → TenantSeeder pattern.
Idempotent: skips tenants that already exist (checks by slug).
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.seeders.base import BaseSeeder
from app.services.tenant_service import TenantService

logger = logging.getLogger(__name__)

# ─── Seed Data (config-driven) ─────────────────────────────────

TENANTS = [
    {
        "name": "Tryambaka",
        "slug": "tryambaka",
        "description": "Digital marketing SaaS platform",
        "contact_email": "admin@tryambaka.com",
        "config": {
            "monthly_token_limit": 1_000_000,
            "preferred_driver": "groq",
        },
        "rate_limits": {"rpm": 60, "rpd": 10000},
    },
    {
        "name": "SutraCode Internal",
        "slug": "sutracode-internal",
        "description": "Internal SutraCode testing tenant",
        "contact_email": "dev@sutracodehq.com",
        "config": {
            "monthly_token_limit": 500_000,
            "preferred_driver": "mock",
        },
        "rate_limits": {"rpm": 120, "rpd": 50000},
    },
    {
        "name": "SutraCode",
        "slug": "sutracode",
        "description": "SutraCode — Darbhanga's leading IT company. Custom software, SaaS, ERP, AI products, digital marketing.",
        "contact_email": "contact@sutracode.in",
        "website_url": "https://sutracode.in",
        "config": {
            "monthly_token_limit": 1_000_000,
            "preferred_driver": "groq",
        },
        "rate_limits": {"rpm": 60, "rpd": 10000},
    },
]


class TenantSeeder(BaseSeeder):
    name = "TenantSeeder"

    async def run(self, db: AsyncSession) -> None:
        for tenant_data in TENANTS:
            slug = tenant_data["slug"]

            # Idempotent: skip if exists
            existing = await db.execute(select(Tenant).where(Tenant.slug == slug))
            if existing.scalar_one_or_none():
                logger.info(f"  ⏩ Tenant '{slug}' already exists, skipping")
                continue

            tenant, live_key, test_key = await TenantService.create(
                db,
                name=tenant_data["name"],
                slug=tenant_data["slug"],
                description=tenant_data.get("description"),
                contact_email=tenant_data.get("contact_email"),
                config=tenant_data.get("config", {}),
                rate_limits=tenant_data.get("rate_limits"),
            )

            logger.info(f"  ✅ Created tenant: {slug}")
            logger.info(f"     🔑 Live key: {live_key}")
            logger.info(f"     🔑 Test key: {test_key}")
            logger.info(f"     ⚠️  Save these keys — they cannot be retrieved again!")
