"""
Chatbot Engine — Per-brand embeddable AI chatbot brain.

Software Factory Principle: Polymorphic + Self-Learning.

Each brand gets its own personal AI agent that:
1. Answers using brand-specific knowledge base
2. Auto-detects language and responds accordingly
3. Supports text chat + WebRTC voice
4. Escalates unknown queries to brand owner via WhatsApp
5. Learns from owner answers for future use

Architecture:
    Customer → Chat/Voice → ChatbotEngine(brand_id)
        → Brand Knowledge lookup
            → FOUND: respond instantly
            → NOT FOUND: respond with "let me check" + escalate to owner
                → Owner replies on WhatsApp
                    → Learn answer → reply to customer
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

import yaml

from app.config import get_settings

logger = logging.getLogger(__name__)


def _resolve_actions(response_text: str, user_message: str, config: dict) -> tuple[str, list[dict]]:
    """
    Extract interactive actions from AI response + keyword fallback.

    Two-pass strategy:
    1. Parse [ACTIONS: id1, id2] tags from LLM response
    2. Keyword fallback: match user message against action keywords

    Returns (clean_response_text, resolved_actions_list).
    Config-driven: actions defined in intelligence_config.yaml.
    """
    actions_cfg = config.get("interactive_actions", {})
    if not actions_cfg.get("enabled"):
        return response_text, []

    available = {a["id"]: a for a in actions_cfg.get("actions", [])}
    max_actions = actions_cfg.get("max_per_response", 3)

    resolved_ids: list[str] = []

    # Pass 1: Extract [ACTIONS: ...] from LLM response
    action_pattern = r'\[ACTIONS?:\s*([^\]]+)\]'
    match = re.search(action_pattern, response_text, re.IGNORECASE)
    if match:
        ids = [x.strip() for x in match.group(1).split(",")]
        resolved_ids.extend(i for i in ids if i in available)
        # Clean the tag from the response text
        response_text = re.sub(action_pattern, "", response_text, flags=re.IGNORECASE).strip()

    # Pass 2: Keyword fallback (if LLM didn't suggest enough)
    if len(resolved_ids) < max_actions:
        msg_lower = user_message.lower()
        for action in actions_cfg.get("actions", []):
            if action["id"] in resolved_ids:
                continue
            keywords = action.get("keywords", [])
            if any(kw in msg_lower for kw in keywords):
                resolved_ids.append(action["id"])
                if len(resolved_ids) >= max_actions:
                    break

    # Resolve IDs to full action objects
    actions = []
    for aid in resolved_ids[:max_actions]:
        if aid in available:
            a = available[aid]
            actions.append({
                "id": a["id"],
                "label": a["label"],
                "description": a.get("description", ""),
                "type": a.get("type", "link"),
                "url": a.get("url", ""),
                "action": a.get("action", ""),
            })

    return response_text, actions


def _load_chatbot_config() -> dict:
    """Load chatbot config from YAML."""
    config_path = Path("intelligence_config.yaml")
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    return config.get("chatbot", {})


class ChatSession:
    """Tracks a single conversation session."""

    def __init__(self, session_id: str, brand_id: str, channel: str = "text"):
        self.session_id = session_id
        self.brand_id = brand_id
        self.channel = channel  # "text", "voice", "webrtc"
        self.history: list[dict] = []
        self.language: str | None = None
        self.visitor_id: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self.pending_escalations: list[str] = []

    def add_message(self, role: str, content: str):
        self.history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "brand_id": self.brand_id,
            "channel": self.channel,
            "language": self.language,
            "message_count": len(self.history),
            "started_at": self.started_at.isoformat(),
        }


class ChatbotEngine:
    """
    Per-brand chatbot brain.

    Flow:
    1. Customer sends message (text or voice)
    2. Check brand knowledge base for answer
    3. If found → respond via AI agent with brand context
    4. If NOT found (low confidence) → respond with holding msg + escalate
    5. When owner answers via WhatsApp → learn + reply to customer
    """

    def __init__(self):
        self._sessions: dict[str, ChatSession] = {}

    # ─── Session Management ─────────────────────────────────

    def get_or_create_session(
        self, session_id: str, brand_id: str, channel: str = "text",
    ) -> ChatSession:
        """Get existing session or create new one."""
        if session_id not in self._sessions:
            self._sessions[session_id] = ChatSession(session_id, brand_id, channel)
            logger.info(f"Chatbot: new session {session_id} for brand {brand_id}")
        return self._sessions[session_id]

    # ─── Brand Config Resolution ────────────────────────────

    async def _get_brand_config(self, brand_id: str, db=None) -> dict | None:
        """
        Auto-resolve brand/product metadata from the tenant record.

        Reads from existing DB fields first (name, description),
        then overlays any explicit overrides from tenant.config JSON.

        Software Factory: if website_url is set but brand hasn't been enriched,
        triggers the BrandEnricher pipeline in background (non-blocking).
        First chat uses tenant.name fallback. Second chat uses enriched data.
        """
        if not db:
            return None

        try:
            from sqlalchemy import select
            from app.models.tenant import Tenant

            # brand_id could be tenant slug or ID
            if brand_id.isdigit():
                result = await db.execute(
                    select(Tenant).where(Tenant.id == int(brand_id))
                )
            else:
                result = await db.execute(
                    select(Tenant).where(Tenant.slug == brand_id)
                )
            tenant = result.scalar_one_or_none()

            if not tenant:
                return None

            # ─── Auto-Enrich Hook (Software Factory: automated pipeline) ──
            # Golden path: website_url set + not enriched → trigger in background.
            # Config-driven: reads enabled/auto_enrich flags from YAML.
            try:
                from app.services.intelligence.brand_enricher import (
                    get_brand_enricher, _get_enricher_config, _needs_enrichment,
                )
                enricher_cfg = _get_enricher_config()
                if (enricher_cfg.get("enabled") and
                    enricher_cfg.get("auto_enrich_on_first_chat") and
                    _needs_enrichment(tenant, enricher_cfg)):
                    import asyncio
                    asyncio.create_task(
                        get_brand_enricher().enrich(tenant, db)
                    )
                    logger.info(f"Chatbot: triggered background enrichment for {tenant.slug}")
            except Exception as e:
                logger.debug(f"Chatbot: auto-enrich check skipped: {e}")

            # Auto-resolve from existing tenant fields
            brand_config = {
                "brand_name": tenant.name,
            }
            if tenant.description:
                brand_config["brand_description"] = tenant.description

            # Overlay explicit overrides from config JSON (if any)
            if tenant.config and isinstance(tenant.config, dict):
                brand_config.update(tenant.config)

            return brand_config

        except Exception as e:
            logger.debug(f"Chatbot: brand config lookup skipped: {e}")

        return None

    # ─── Core Chat ──────────────────────────────────────────

    async def chat(
        self,
        session_id: str,
        brand_id: str,
        message: str,
        channel: str = "text",
        visitor_id: str | None = None,
        language: str | None = None,
        db=None,
    ) -> dict:
        """
        Process a chat message from a customer.

        Returns:
        - response: AI's response text
        - confidence: how confident the AI is (0-1)
        - escalated: whether the query was escalated to brand owner
        - session_id: for tracking
        """
        config = _load_chatbot_config()
        session = self.get_or_create_session(session_id, brand_id, channel)
        session.visitor_id = visitor_id
        if language:
            session.language = language

        # Record user message
        session.add_message("user", message)

        # 1. Check brand knowledge base
        from app.services.intelligence.memory import get_memory
        mem = get_memory()

        knowledge_result = await mem.brand_search(brand_id, message)
        has_knowledge = knowledge_result.get("found", False)
        confidence = knowledge_result.get("confidence", 0.0)

        confidence_threshold = config.get("confidence_threshold", 0.6)

        # 2. Generate response via AI agent hub
        # The hub's _resolve_agent() auto-detects specialist intents
        # (e.g., "generate quiz" → quiz_generator) transparently.
        from app.services.agents.hub import AiAgentHub
        hub = AiAgentHub()

        # ─── Agent Resolution ─────────────────────────────
        # Priority: brand config → chatbot config → fallback
        # Intent routing is handled centrally by hub._resolve_agent()
        agent_id = config.get("default_agent", "chatbot_trainer")

        brand_config = await self._get_brand_config(brand_id, db)
        if brand_config:
            agent_id = brand_config.get("chatbot_agent", agent_id)

        # ─── Smart Context Population ─────────────────────
        context = {
            "brand_id": brand_id,
            "language": session.language or language,
            "channel": channel,
            "chatbot": True,
        }

        if brand_config:
            for key in (
                "brand_name", "brand_description", "organization_name",
                "organization_description", "product_name", "product_info",
                "website_url", "website_summary", "custom_instructions",
            ):
                if brand_config.get(key):
                    context[key] = brand_config[key]

        # ─── Build Enhanced Prompt ────────────────────────
        prompt_parts = []

        # ─── Brand Identity Injection (Software Factory: config-driven) ──
        # Reads template + field map from intelligence_config.yaml → chatbot.brand_identity_prompt.
        # Changing tone/format = edit YAML. Zero Python changes needed.
        identity_cfg = config.get("brand_identity_prompt", {})
        if identity_cfg.get("enabled", True) and brand_config:
            template = identity_cfg.get("template", "")
            fields_map = identity_cfg.get("context_fields", {})

            # Resolve placeholders from brand_config → fallback
            values = {
                cfg["placeholder"]: brand_config.get(key, cfg.get("fallback", ""))
                for key, cfg in fields_map.items()
            }

            if template and values.get("brand_name"):
                try:
                    prompt_parts.append(template.format(**values))
                except KeyError as e:
                    logger.warning(f"Chatbot: brand template placeholder missing: {e}")

        if has_knowledge:
            brand_context = knowledge_result.get("context", "")
            if brand_context:
                prompt_parts.append(f"Relevant Brand Knowledge:\n{brand_context}")

        prompt_parts.append(f"Customer Question: {message}")

        # Guardrail instruction (from YAML) to prevent brand hallucination
        guardrail = identity_cfg.get(
            "guardrail",
            "Respond helpfully and conversationally as this brand's representative. "
            "Be specific to this brand — never give generic advice. "
            "NEVER mention or pretend to be any other brand or company."
        )
        prompt_parts.append(guardrail)

        # ─── Interactive Actions instruction (from YAML) ──────────
        actions_cfg = config.get("interactive_actions", {})
        if actions_cfg.get("enabled"):
            action_instruction = actions_cfg.get("action_instruction", "")
            if action_instruction:
                prompt_parts.append(action_instruction)

        enhanced_prompt = "\n\n".join(prompt_parts)

        # Execute via hub — _resolve_agent() handles specialist routing
        response = await hub.run_in_conversation(
            agent_type=agent_id,
            prompt=enhanced_prompt,
            history=session.history[:-1],
            db=db,
            context=context,
        )

        # Extract human-readable text from JSON response
        # The agent returns JSON with fields like "response", "advice", etc.
        # We need the clean text for the chatbot, not the raw JSON.
        response_text = response.content
        try:
            parsed = json.loads(response_text)
            if isinstance(parsed, dict):
                # Priority: response > advice > content
                response_text = (
                    parsed.get("response")
                    or parsed.get("advice")
                    or parsed.get("content")
                    or response_text
                )
        except (json.JSONDecodeError, TypeError):
            pass  # Not JSON — use raw text as-is (already markdown)

        # ─── Resolve Interactive Actions (Software Factory: config-driven) ──
        response_text, actions = _resolve_actions(response_text, message, config)

        session.add_message("assistant", response_text)

        # 3. Escalate if low confidence
        escalated = False
        if not has_knowledge or confidence < confidence_threshold:
            from app.services.intelligence.brain import get_brain
            brain = get_brain()

            escalated = await brain.escalate(
                brand_id=brand_id,
                session_id=session_id,
                question=message,
                ai_response=response_text,
                confidence=confidence,
            )

            if escalated:
                # Add a "checking" note to the response
                holding_messages = config.get("holding_messages", {})
                lang = session.language or "en"
                holding = holding_messages.get(lang, holding_messages.get(
                    "en", "Let me check on that for you. I'll get back shortly!"
                ))
                response_text = f"{response_text}\n\n{holding}"
                session.pending_escalations.append(message)

        result = {
            "session_id": session_id,
            "response": response_text,
            "confidence": round(confidence, 2),
            "escalated": escalated,
            "language": session.language,
            "channel": channel,
            "actions": actions,
        }

        # 4. Log for analytics
        self._log_chat(session, message, response_text, confidence, escalated)

        return result

    # ─── Voice Chat (WebRTC) ────────────────────────────────

    async def voice_chat(
        self,
        session_id: str,
        brand_id: str,
        audio_path: str,
        db=None,
    ) -> dict:
        """
        Process a voice message via WebRTC.
        Transcribes → chats → synthesizes response audio.
        """
        # 1. Transcribe with VoIP engine (Faster-Whisper)
        from app.services.intelligence.voip_engine import get_voip_engine
        voip = get_voip_engine()

        transcription = await voip.transcribe(audio_path)
        caller_text = transcription["text"]
        detected_lang = transcription["language"]

        if not caller_text.strip():
            return {"status": "silence", "message": "No speech detected"}

        # 2. Chat (same pipeline as text)
        chat_result = await self.chat(
            session_id=session_id,
            brand_id=brand_id,
            message=caller_text,
            channel="webrtc",
            language=detected_lang,
            db=db,
        )

        # 3. Synthesize response audio
        audio_bytes = await voip.synthesize(
            text=chat_result["response"],
            language=detected_lang,
        )

        chat_result["audio_size"] = len(audio_bytes)
        chat_result["transcription"] = caller_text

        return chat_result

    # ─── Learn from Owner Answer ────────────────────────────

    async def learn_from_owner(
        self,
        brand_id: str,
        question: str,
        answer: str,
        session_id: str | None = None,
    ) -> dict:
        """
        When brand owner answers an escalated question,
        store it in knowledge base so AI knows next time.
        """
        # 1. Store in brand knowledge base
        from app.services.intelligence.memory import get_memory
        mem = get_memory()

        await mem.brand_learn(brand_id, question, answer)

        # 2. If session is still active, send the answer to customer
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            session.add_message("assistant", f"Update: {answer}")

        logger.info(f"Chatbot: learned new answer for brand {brand_id}")

        return {
            "status": "learned",
            "brand_id": brand_id,
            "question": question[:100],
            "answer": answer[:200],
        }

    # ─── Logging ────────────────────────────────────────────

    def _log_chat(
        self, session: ChatSession, message: str,
        response: str, confidence: float, escalated: bool,
    ):
        """Log chat interaction for analytics."""
        log_dir = Path("training/chat_logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"chat_{session.brand_id}.jsonl"

        entry = {
            "session_id": session.session_id,
            "brand_id": session.brand_id,
            "channel": session.channel,
            "language": session.language,
            "message": message[:500],
            "response": response[:500],
            "confidence": confidence,
            "escalated": escalated,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            with open(log_path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Chatbot: logging failed: {e}")


# ─── Singleton ──────────────────────────────────────────────
_engine: ChatbotEngine | None = None


def get_chatbot_engine() -> ChatbotEngine:
    global _engine
    if _engine is None:
        _engine = ChatbotEngine()
    return _engine
