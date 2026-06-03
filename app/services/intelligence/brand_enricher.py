"""
BrandEnricher — Automated brand context enrichment pipeline.

Software Factory: Standardized → Automated → Repeatable.

Pipeline stages:
  1. SCRAPE  → WebScraperService.analyze_url(website_url)
  2. EXTRACT → LLM parses scraped content into structured brand JSON
  3. GATE    → Quality check (min_fields, min_description_length)
  4. STORE   → Merge into tenant.config (manual overrides preserved)
  5. STAMP   → Set brand_enriched_at to prevent re-scraping

Config-driven: extraction prompt, field list, quality gates, re-enrich interval
all read from intelligence_config.yaml → chatbot.brand_enricher.
Adding new fields = edit YAML. Zero Python changes needed.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.intelligence.config_loader import get_intelligence_config

logger = logging.getLogger(__name__)

# ─── In-memory lock: prevent concurrent enrichment for the same tenant ──
_enrichment_locks: dict[str, asyncio.Lock] = {}


def _get_enricher_config() -> dict:
    """Load enricher config from intelligence_config.yaml → chatbot.brand_enricher."""
    config = get_intelligence_config()
    return config.get("chatbot", {}).get("brand_enricher", {})


def _needs_enrichment(tenant, config: dict) -> bool:
    """
    Check if a tenant needs (re-)enrichment.

    Software Factory: staleness is config-driven.
    Per-tenant override via tenant.re_enrich_interval_days.
    """
    if not tenant.website_url:
        return False

    if not tenant.brand_enriched_at:
        return True  # Never enriched

    # Per-tenant interval override → YAML default fallback
    interval_days = tenant.re_enrich_interval_days or config.get("re_enrich_interval_days", 30)
    stale_after = tenant.brand_enriched_at + timedelta(days=interval_days)
    return datetime.utcnow() > stale_after


class BrandEnricher:
    """
    Automated brand context enrichment — Software Factory pipeline.

    Usage:
        enricher = get_brand_enricher()
        result = await enricher.enrich(tenant, db)
    """

    async def enrich(self, tenant, db: AsyncSession) -> dict[str, Any] | None:
        """
        Run the full enrichment pipeline for a tenant.

        Returns the extracted brand profile dict, or None if quality gate fails.
        """
        config = _get_enricher_config()

        if not config.get("enabled", True):
            return None

        if not tenant.website_url:
            logger.debug(f"BrandEnricher: no website_url for {tenant.slug}, skipping")
            return None

        # ─── Concurrency guard (per-tenant) ──────────────
        slug = tenant.slug
        if slug not in _enrichment_locks:
            _enrichment_locks[slug] = asyncio.Lock()

        if _enrichment_locks[slug].locked():
            logger.debug(f"BrandEnricher: enrichment already in progress for {slug}")
            return None

        async with _enrichment_locks[slug]:
            try:
                return await self._run_pipeline(tenant, db, config)
            except Exception as e:
                logger.error(f"BrandEnricher: pipeline failed for {slug}: {e}")
                return None

    async def _run_pipeline(self, tenant, db: AsyncSession, config: dict) -> dict | None:
        """
        5-stage Software Factory pipeline:
        SCRAPE → EXTRACT → GATE → STORE → STAMP
        """
        slug = tenant.slug
        logger.info(f"BrandEnricher: starting enrichment for {slug} ({tenant.website_url})")

        # ─── Stage 1: SCRAPE ──────────────────────────
        from app.services.intelligence.web_scraper import WebScraperService

        max_pages = config.get("max_pages", 3)
        scraper = WebScraperService()

        try:
            scraped = await asyncio.wait_for(
                scraper.analyze_url(tenant.website_url, max_pages=max_pages),
                timeout=30,
            )
        except asyncio.TimeoutError:
            logger.warning(f"BrandEnricher: scrape timed out for {slug}")
            return None
        except Exception as e:
            logger.warning(f"BrandEnricher: scrape failed for {slug}: {e}")
            return None

        # ─── Stage 2: EXTRACT (LLM — prompt from YAML) ──
        content = self._build_extraction_input(scraped)
        if not content or len(content.strip()) < 50:
            logger.warning(f"BrandEnricher: insufficient scraped content for {slug}")
            return None

        extraction_prompt = config.get(
            "extraction_prompt",
            "Extract a brand profile as JSON with keys: brand_description, industry, "
            "target_audience, product_info, tone.",
        )

        extracted = await self._llm_extract(content, extraction_prompt)
        if not extracted:
            logger.warning(f"BrandEnricher: LLM extraction returned nothing for {slug}")
            return None

        # ─── Stage 3: QUALITY GATE ────────────────────
        gate = config.get("quality_gate", {})
        if not self._passes_gate(extracted, gate):
            logger.warning(f"BrandEnricher: quality gate failed for {slug}: {extracted}")
            return None

        # ─── Stage 4: STORE (merge — preserve manual overrides) ──
        extract_fields = config.get("extract_fields", [])
        existing = tenant.config or {}

        for field in extract_fields:
            if field not in existing and extracted.get(field):
                existing[field] = extracted[field]

        # Also update top-level description if empty
        if not tenant.description and extracted.get("brand_description"):
            tenant.description = extracted["brand_description"]

        tenant.config = existing

        # ─── Stage 5: STAMP ───────────────────────────
        tenant.brand_enriched_at = datetime.utcnow()
        await db.commit()

        logger.info(
            f"BrandEnricher: enriched {slug} — "
            f"{len([f for f in extract_fields if extracted.get(f)])} fields extracted"
        )
        return extracted

    def _build_extraction_input(self, scraped: dict) -> str:
        """
        Build a text summary from scraped data for LLM extraction.

        Combines page titles, meta descriptions, headings, and content
        into a structured prompt input.
        """
        parts = []
        pages = scraped.get("pages", [])

        for i, page in enumerate(pages[:5]):
            parts.append(f"--- Page {i + 1}: {page.get('url', 'unknown')} ---")

            if page.get("title"):
                parts.append(f"Title: {page['title']}")
            if page.get("meta_description"):
                parts.append(f"Meta Description: {page['meta_description']}")

            headings = page.get("headings", {})
            for level in ("h1", "h2"):
                for h in headings.get(level, [])[:5]:
                    parts.append(f"{level.upper()}: {h}")

        # Tech stack
        tech = scraped.get("tech_stack", [])
        if tech:
            parts.append(f"\nTech Stack: {', '.join(tech)}")

        # Social profiles
        socials = scraped.get("social_profiles", {})
        if socials:
            social_list = [f"{k}: {v[0]}" for k, v in socials.items() if v]
            if social_list:
                parts.append(f"Social Profiles: {', '.join(social_list)}")

        # Structured data (Schema.org)
        schemas = scraped.get("structured_data", [])
        for s in schemas[:3]:
            if s.get("type") == "Organization":
                data = s.get("data", {})
                if data.get("name"):
                    parts.append(f"Organization Name: {data['name']}")
                if data.get("description"):
                    parts.append(f"Organization Description: {data['description']}")

        return "\n".join(parts)

    async def _llm_extract(self, content: str, prompt: str) -> dict | None:
        """
        Use the LLM to extract structured brand profile from scraped content.

        Returns a dict with keys like brand_description, industry, etc.
        """
        try:
            from app.services.intelligence.driver import get_driver_registry

            registry = get_driver_registry()
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Website content:\n\n{content[:4000]}"},
            ]

            response = await registry.chat(messages=messages)
            raw = response.content or ""

            # Strip markdown code fences if present
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw.rsplit("```", 1)[0]
            raw = raw.strip()

            return json.loads(raw)

        except json.JSONDecodeError as e:
            logger.warning(f"BrandEnricher: LLM returned invalid JSON: {e}")
            return None
        except Exception as e:
            logger.warning(f"BrandEnricher: LLM extraction error: {e}")
            return None

    def _passes_gate(self, extracted: dict, gate_config: dict) -> bool:
        """
        Quality gate — config-driven from YAML.

        Checks:
        - Minimum number of non-empty extracted fields
        - Minimum description length
        """
        min_fields = gate_config.get("min_fields", 3)
        min_desc_len = gate_config.get("min_description_length", 20)

        non_empty = sum(1 for v in extracted.values() if v and str(v).strip())
        if non_empty < min_fields:
            return False

        desc = extracted.get("brand_description", "")
        if desc and len(str(desc).strip()) < min_desc_len:
            return False

        return True


# ─── Singleton ────────────────────────────────────────────────
_instance: BrandEnricher | None = None


def get_brand_enricher() -> BrandEnricher:
    """Get singleton BrandEnricher instance."""
    global _instance
    if _instance is None:
        _instance = BrandEnricher()
    return _instance
