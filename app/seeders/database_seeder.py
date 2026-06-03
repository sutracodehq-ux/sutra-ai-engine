"""
Database Seeder — orchestrates all seeders in order.

Like Laravel's DatabaseSeeder → calls sub-seeders in dependency order.
Software Factory: adding a new seeder = add to SEEDER_REGISTRY + create the class.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.seeders.base import BaseSeeder
from app.seeders.tenant_seeder import TenantSeeder
from app.seeders.chatbot_config_seeder import ChatbotConfigSeeder
from app.seeders.voice_profile_seeder import VoiceProfileSeeder

logger = logging.getLogger(__name__)

# ─── Seeder Registry (order matters — dependencies first) ──────

SEEDER_REGISTRY: list[BaseSeeder] = [
    TenantSeeder(),
    ChatbotConfigSeeder(),       # After TenantSeeder (needs tenant to exist)
    VoiceProfileSeeder(),
    # Add new seeders here ↓
]


class DatabaseSeeder:
    """
    Master seeder — runs all seeders in dependency order.

    Usage:
        python -m app.seeders.run          # Run all seeders
        python -m app.seeders.run tenant   # Run only TenantSeeder
    """

    @staticmethod
    async def run_all(db: AsyncSession) -> None:
        """Run all registered seeders in order."""
        logger.info("🌱 Starting database seeding...")
        for seeder in SEEDER_REGISTRY:
            await seeder.execute(db)
        logger.info("🌱 Database seeding complete!")

    @staticmethod
    async def run_one(db: AsyncSession, name: str) -> None:
        """Run a single seeder by name (case-insensitive, partial match)."""
        name_lower = name.lower()
        for seeder in SEEDER_REGISTRY:
            if name_lower in seeder.name.lower():
                await seeder.execute(db)
                return
        available = ", ".join(s.name for s in SEEDER_REGISTRY)
        raise ValueError(f"Seeder '{name}' not found. Available: {available}")
