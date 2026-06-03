"""
Chatbot Config Seeder — seeds chatbot intelligence config from YAML into tenant.config.

Software Factory: YAML is the seed template, DB is the runtime source of truth.
- Edit YAML → run seeder → DB updated → chatbot uses new data instantly.
- Zero Python changes needed to update pricing, knowledge, or actions.

Idempotent: skips if tenant.config already has 'system_prompt' key (unless force=True).
"""

import logging
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.seeders.base import BaseSeeder

logger = logging.getLogger(__name__)

# Fields to extract from the agent YAML and seed into tenant.config
CHATBOT_CONFIG_KEYS = [
    "system_prompt",
    "knowledge_base",
    "guardrail",
    "interactive_actions",
    "action_instruction",
]

# Map of tenant slug → agent config YAML filename
TENANT_BOT_MAP = {
    "sutracode": "sutracode_website_bot.yaml",
}


class ChatbotConfigSeeder(BaseSeeder):
    name = "ChatbotConfigSeeder"

    def __init__(self, force: bool = False):
        self.force = force

    async def run(self, db: AsyncSession) -> None:
        config_dir = Path("agent_config")

        for slug, yaml_file in TENANT_BOT_MAP.items():
            yaml_path = config_dir / yaml_file
            if not yaml_path.exists():
                logger.warning(f"  ⚠️  Agent config not found: {yaml_path}")
                continue

            # Load YAML seed template
            with open(yaml_path, "r") as f:
                agent_config = yaml.safe_load(f)

            # Find tenant
            result = await db.execute(select(Tenant).where(Tenant.slug == slug))
            tenant = result.scalar_one_or_none()

            if not tenant:
                logger.warning(f"  ⚠️  Tenant '{slug}' not found — run TenantSeeder first")
                continue

            # Idempotent check
            existing_config = tenant.config or {}
            if not self.force and "system_prompt" in existing_config:
                logger.info(f"  ⏩ Tenant '{slug}' already has chatbot config, skipping (use --force to override)")
                continue

            # Extract chatbot-specific fields from YAML
            chatbot_fields = {}
            for key in CHATBOT_CONFIG_KEYS:
                if key in agent_config:
                    chatbot_fields[key] = agent_config[key]

            # Also store the agent identifier for routing
            if "identifier" in agent_config:
                chatbot_fields["chatbot_agent"] = agent_config["identifier"]

            # Deep merge: preserve existing config, overlay chatbot fields
            merged_config = {**existing_config, **chatbot_fields}
            tenant.config = merged_config

            await db.flush()

            seeded_keys = list(chatbot_fields.keys())
            logger.info(f"  ✅ Seeded chatbot config for '{slug}': {seeded_keys}")
