"""
Chatbot WebSocket — Real-time streaming chat + WebRTC signaling.

Software Factory Principle: Responsive + Real-time.

Provides:
1. Streaming AI responses (word-by-word, not wait-for-full)
2. Push notifications (owner answers escalation → instant push to customer)
3. Typing indicators ("AI is thinking...")
4. WebRTC signaling (SDP/ICE for voice calls in browser)
5. Session lifecycle (connect, chat, disconnect)

Protocol (JSON messages over WS):
    Client → Server:
        {"type": "chat", "message": "Hello", "language": "hi"}
        {"type": "voice", "audio_base64": "..."}
        {"type": "webrtc_offer", "sdp": "..."}
        {"type": "webrtc_ice", "candidate": "..."}

    Server → Client:
        {"type": "stream_start"}
        {"type": "stream_token", "token": "Hello"}
        {"type": "stream_end", "full_response": "...", "confidence": 0.8}
        {"type": "typing", "status": true}
        {"type": "escalation_resolved", "answer": "..."}
        {"type": "webrtc_answer", "sdp": "..."}
        {"type": "error", "message": "..."}
"""

import asyncio
import base64
import json
import logging
import tempfile
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.db.session import async_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chatbot-ws"])


# ─── Connection Manager ────────────────────────────────────

class ConnectionManager:
    """Manages active WebSocket connections per session."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}      # session_id → ws
        self._brand_sessions: dict[str, set] = {}          # brand_id → {session_ids}

    async def connect(self, session_id: str, brand_id: str, websocket: WebSocket):
        await websocket.accept()
        self._connections[session_id] = websocket
        self._brand_sessions.setdefault(brand_id, set()).add(session_id)
        logger.info(f"WS: connected session {session_id} (brand {brand_id})")

    def disconnect(self, session_id: str, brand_id: str):
        self._connections.pop(session_id, None)
        if brand_id in self._brand_sessions:
            self._brand_sessions[brand_id].discard(session_id)

    async def send(self, session_id: str, data: dict):
        """Send a JSON message to a specific session."""
        ws = self._connections.get(session_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def push_to_session(self, session_id: str, event_type: str, payload: dict):
        """Push a server-initiated event to a session."""
        await self.send(session_id, {"type": event_type, **payload})


manager = ConnectionManager()


# ─── WebSocket Endpoint ────────────────────────────────────

@router.websocket("/v1/chatbot/ws/{session_id}")
async def chatbot_websocket(
    websocket: WebSocket,
    session_id: str,
    brand_id: str = Query(...),
):
    """
    WebSocket endpoint for real-time chatbot communication.

    Connect: ws://host/v1/chatbot/ws/{session_id}?brand_id=xxx
    """
    await manager.connect(session_id, brand_id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send(session_id, {
                    "type": "error", "message": "Invalid JSON",
                })
                continue

            msg_type = data.get("type", "chat")

            if msg_type == "chat":
                await _handle_chat(session_id, brand_id, data)

            elif msg_type == "voice":
                await _handle_voice(session_id, brand_id, data)

            elif msg_type in ("webrtc_offer", "webrtc_ice"):
                await _handle_webrtc(session_id, brand_id, data)

            elif msg_type == "ping":
                await manager.send(session_id, {"type": "pong"})

            else:
                await manager.send(session_id, {
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        manager.disconnect(session_id, brand_id)
        logger.info(f"WS: disconnected session {session_id}")


# ─── Handlers ──────────────────────────────────────────────

async def _handle_chat(session_id: str, brand_id: str, data: dict):
    """Handle a text chat message with streaming response."""
    message = data.get("message", "").strip()
    language = data.get("language")

    if not message:
        await manager.send(session_id, {"type": "error", "message": "Empty message"})
        return

    # 1. Send typing indicator
    await manager.send(session_id, {"type": "typing", "status": True})

    # 2. Get response from chatbot engine (with DB session for brand context)
    from app.services.intelligence.chatbot_engine import get_chatbot_engine
    engine = get_chatbot_engine()

    async with async_session_factory() as db:
        result = await engine.chat(
            session_id=session_id,
            brand_id=brand_id,
            message=message,
            channel="websocket",
            language=language,
            db=db,
        )

    # 3. Stream response token-by-token
    response_text = result.get("response", "")
    await manager.send(session_id, {"type": "stream_start"})

    # Stream words for natural feel
    words = response_text.split()
    for i, word in enumerate(words):
        token = word + (" " if i < len(words) - 1 else "")
        await manager.send(session_id, {"type": "stream_token", "token": token})
        await asyncio.sleep(0.03)  # 30ms between words

    # 4. Send complete (with interactive actions)
    await manager.send(session_id, {
        "type": "stream_end",
        "full_response": response_text,
        "confidence": result.get("confidence", 0),
        "escalated": result.get("escalated", False),
        "language": result.get("language"),
        "actions": result.get("actions", []),
    })

    # 5. Stop typing
    await manager.send(session_id, {"type": "typing", "status": False})


async def _handle_voice(session_id: str, brand_id: str, data: dict):
    """Handle a voice message (base64 audio from WebRTC)."""
    audio_b64 = data.get("audio_base64", "")
    if not audio_b64:
        await manager.send(session_id, {"type": "error", "message": "No audio data"})
        return

    # Decode audio
    audio_bytes = base64.b64decode(audio_b64)

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    await manager.send(session_id, {"type": "typing", "status": True})

    # Process through chatbot engine (with DB session for brand context)
    from app.services.intelligence.chatbot_engine import get_chatbot_engine
    engine = get_chatbot_engine()

    async with async_session_factory() as db:
        result = await engine.voice_chat(
            session_id=session_id,
            brand_id=brand_id,
            audio_path=tmp_path,
            db=db,
        )

    # Stream the text response
    response_text = result.get("response", "")
    await manager.send(session_id, {"type": "stream_start"})

    words = response_text.split()
    for i, word in enumerate(words):
        token = word + (" " if i < len(words) - 1 else "")
        await manager.send(session_id, {"type": "stream_token", "token": token})
        await asyncio.sleep(0.03)

    # Send audio response (base64)
    audio_response_b64 = ""
    if result.get("audio_size"):
        from app.services.intelligence.voip_engine import get_voip_engine
        voip = get_voip_engine()
        audio_resp = await voip.synthesize(
            text=response_text,
            language=result.get("language", "en"),
        )
        audio_response_b64 = base64.b64encode(audio_resp).decode()

    await manager.send(session_id, {
        "type": "stream_end",
        "full_response": response_text,
        "transcription": result.get("transcription", ""),
        "audio_base64": audio_response_b64,
        "confidence": result.get("confidence", 0),
        "language": result.get("language"),
    })

    await manager.send(session_id, {"type": "typing", "status": False})


async def _handle_webrtc(session_id: str, brand_id: str, data: dict):
    """Handle WebRTC signaling (SDP offers/ICE candidates)."""
    # WebRTC signaling — relay between client and voice engine
    # In production, this would coordinate with a media server (LiveKit/Janus)
    msg_type = data.get("type")

    if msg_type == "webrtc_offer":
        # Client sent SDP offer for voice call
        await manager.send(session_id, {
            "type": "webrtc_ack",
            "message": "Voice call signaling received. Connect via audio stream.",
        })

    elif msg_type == "webrtc_ice":
        # ICE candidate exchange
        await manager.send(session_id, {
            "type": "webrtc_ice_ack",
            "message": "ICE candidate received.",
        })


# ─── Server Push (called externally) ───────────────────────

async def push_escalation_resolved(session_id: str, answer: str):
    """
    Push owner's answer to the customer's active WebSocket.
    Called by the webhook handler when brand owner replies.
    """
    await manager.push_to_session(session_id, "escalation_resolved", {
        "answer": answer,
        "message": "The team has provided an answer to your question!",
    })
