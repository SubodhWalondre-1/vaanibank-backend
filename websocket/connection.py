"""
VaaniBank AI — WebSocket Connection Mixin
PSBs Hackathon 2026 | Team Vectora
"""

import asyncio
import logging
from typing import Any, Dict, Optional
from fastapi import WebSocket
from sqlalchemy import select

from websocket.helpers import _event, _safe_send

logger = logging.getLogger("vaanibank.websocket.connection")


class ConnectionMixin:
    """
    Mixin for ConnectionManager handling connect/disconnect lifecycle,
    reconnections, replaying context, auto-greetings and auto-farewell.
    """

    async def connect(
        self,
        websocket: WebSocket,
        token_number: str,
        role: str,
    ) -> None:
        """
        Register an already-accepted WebSocket connection.

        Sends session_connected to the joining client.
        Notifies the other role (if already connected) that the peer joined.
        If staff reconnects mid-session, replays last exchange for context.
        """
        if token_number not in self.active_connections:
            self.active_connections[token_number] = {
                "staff": None,
                "customer": None,
            }

        # Track whether this is a reconnection (slot was previously held)
        is_reconnect = self.active_connections[token_number].get(role) is not None
        customer_already_connected = self.active_connections[token_number]["customer"] is not None

        self.active_connections[token_number][role] = websocket

        # Confirm connection to the joining client
        await _safe_send(
            websocket,
            _event(
                "session_connected",
                {
                    "token_number": token_number,
                    "role": role,
                    "staff_connected": self.active_connections[token_number]["staff"] is not None,
                    "customer_connected": self.active_connections[token_number]["customer"] is not None,
                    "message": f"Connected as {role} to session {token_number}",
                },
            ),
        )

        # Notify the other role that the peer has joined (NOT an error — peer_joined: true)
        other_role = "customer" if role == "staff" else "staff"
        other_ws = self.active_connections[token_number].get(other_role)
        if other_ws:
            await _safe_send(
                other_ws,
                _event(
                    "session_connected",
                    {
                        "token_number": token_number,
                        "peer_role": role,
                        "peer_joined": True,
                        "message": f"{role.title()} has joined the session.",
                    },
                ),
            )

        # Staff reconnect: replay last exchange so they don't miss context
        if role == "staff" and customer_already_connected:
            await self._replay_last_exchange(token_number)

        # Auto-greeting: when staff connects and customer is already here and has selected service
        if role == "staff" and customer_already_connected:
            if token_number not in self.greeted_tokens:
                try:
                    from models import Session as SessionModel
                    async with await self._get_db() as db:
                        result = await db.execute(
                            select(SessionModel.intent_detected).where(
                                SessionModel.token_number == token_number
                            )
                        )
                        intent_det = result.scalar_one_or_none()
                        if intent_det:
                            if token_number not in self.greeted_tokens:
                                self.greeted_tokens.add(token_number)
                                asyncio.create_task(self._send_auto_greeting(token_number))
                except Exception as exc:
                    logger.warning("Failed checking intent_detected on staff connect: %s", exc)

        logger.info("WS connected | token=%s | role=%s | reconnect=%s", token_number, role, is_reconnect)

    async def disconnect(
        self,
        websocket: WebSocket,
        token_number: str,
        role: str,
    ) -> None:
        """
        Remove a WebSocket from active connections.

        Notifies the other role that the peer has disconnected.
        Cleans up the token entry if both sides are gone.
        """
        if token_number in self.active_connections:
            self.active_connections[token_number][role] = None

            # Notify peer
            other_role = "customer" if role == "staff" else "staff"
            other_ws = self.active_connections[token_number].get(other_role)
            if other_ws:
                await _safe_send(
                    other_ws,
                    _event(
                        "peer_status",
                        {
                            "code": "peer_disconnected",
                            "message": f"{role.title()} has disconnected.",
                        },
                    ),
                )

            # Clean up token entry if both sides gone
            if (
                self.active_connections[token_number]["staff"] is None
                and self.active_connections[token_number]["customer"] is None
            ):
                del self.active_connections[token_number]
                if token_number in self.greeted_tokens:
                    self.greeted_tokens.remove(token_number)
                logger.info("WS token cleaned | token=%s", token_number)

        logger.info("WS disconnected | token=%s | role=%s", token_number, role)

    async def _replay_last_exchange(
        self,
        token_number: str,
    ) -> None:
        """
        On staff reconnect, fetch the last customer exchange from DB
        and replay transcription_ready so staff has context.
        Best-effort — never raises.
        """
        try:
            from models import Exchange, Session

            async with await self._get_db() as db:
                # Find the session by token_number
                session_result = await db.execute(
                    select(Session).where(Session.token_number == token_number)
                )
                session_obj = session_result.scalar_one_or_none()
                if not session_obj or session_obj.status not in ("active", "waiting"):
                    return

                # Find last customer→staff exchange
                exchange_result = await db.execute(
                    select(Exchange)
                    .where(
                        Exchange.session_id == session_obj.id,
                        Exchange.direction == "customer_to_staff",
                    )
                    .order_by(Exchange.exchange_number.desc())
                    .limit(1)
                )
                last_exchange = exchange_result.scalar_one_or_none()

                if not last_exchange:
                    return

                # Replay as transcription_ready with replay flag
                await self.send_to_staff(
                    token_number,
                    "transcription_ready",
                    {
                        "exchange_id": last_exchange.id,
                        "text_original": last_exchange.customer_text_original or "",
                        "text_translated": last_exchange.customer_text_translated or "",
                        "confidence": last_exchange.stt_confidence or 0.0,
                        "sentiment": last_exchange.sentiment or "calm",
                        "intent": last_exchange.intent or "general",
                        "pii_detected": last_exchange.pii_detected or False,
                        "is_replay": True,
                    },
                )
                logger.info(
                    "Replayed last exchange to reconnected staff | token=%s | exchange=%d",
                    token_number,
                    last_exchange.exchange_number,
                )

        except Exception as exc:
            logger.warning("Replay last exchange failed: %s", exc)

    async def _send_auto_greeting(
        self,
        token_number: str,
    ) -> None:
        """
        Send an automatic multilingual greeting to the customer when they
        connect and staff is already present.
        """
        try:
            # Small delay to let WebSocket handshake settle
            await asyncio.sleep(0.8)

            from models import Session as SessionModel
            from services.session_navigator import GREETING_MULTILINGUAL

            async with await self._get_db() as db:
                result = await db.execute(
                    select(
                        SessionModel.customer_language_code,
                        SessionModel.id,
                    ).where(SessionModel.token_number == token_number)
                )
                row = result.one_or_none()

            if not row:
                logger.warning("Auto-greeting: session not found | token=%s", token_number)
                return

            lang_code = (row.customer_language_code or "hi").split("-")[0].lower()
            session_id = row.id

            # Pick greeting text
            greeting_text = GREETING_MULTILINGUAL.get(
                lang_code, GREETING_MULTILINGUAL["hi"]
            )

            # Send text message first (customer sees bubble immediately)
            await self.send_to_customer(
                token_number,
                "staff_message",
                {
                    "text": greeting_text,
                    "language_code": lang_code,
                    "is_auto_greeting": True,
                },
            )

            # Generate TTS audio and send to customer
            try:
                from services.ai_service import ai_service
                tts_result = await ai_service.generate_tts(
                    text=greeting_text,
                    language_code=row.customer_language_code or "hi",
                    session_id=session_id,
                )

                await self.broadcast_audio(
                    token_number=token_number,
                    audio_url=tts_result.audio_url,
                    duration_seconds=tts_result.duration_seconds,
                    response_text=greeting_text,
                )
                logger.info(
                    "Auto-greeting sent with TTS | token=%s | lang=%s",
                    token_number, lang_code,
                )
            except Exception as tts_exc:
                logger.warning(
                    "Auto-greeting TTS failed (text was sent) | token=%s | %s",
                    token_number, tts_exc,
                )

            # Also notify staff that auto-greeting was sent
            await self.send_to_staff(
                token_number,
                "ai_suggestion_ready",
                {
                    "suggested_hindi": GREETING_MULTILINGUAL["hi"],
                    "suggested_customer_lang": greeting_text,
                    "intent": "greeting",
                    "process_triggered": None,
                    "is_auto_greeting": True,
                    "message": "Auto-greeting sent to customer in their language.",
                },
            )

        except Exception as exc:
            logger.warning(
                "Auto-greeting failed (non-fatal) | token=%s | %s",
                token_number, exc,
            )

    async def _send_auto_farewell(
        self,
        token_number: str,
        session_id: Optional[int],
    ) -> float:
        """
        Send a thank-you + verification message in the customer's language
        before the session_ended event is broadcast.

        Also sends the verification/processing time message based on intent.

        Returns the farewell TTS duration in seconds so the caller can wait
        for the audio to finish playing before broadcasting session_ended.
        """
        tts_duration: float = 0.0

        try:
            from models import Session as SessionModel
            from services.session_navigator import (
                FAREWELL_MULTILINGUAL,
                VERIFICATION_TIME_MAP,
            )

            if not session_id:
                return 0.0

            async with await self._get_db() as db:
                result = await db.execute(
                    select(
                        SessionModel.customer_language_code,
                        SessionModel.intent_detected,
                    ).where(SessionModel.id == session_id)
                )
                row = result.one_or_none()

            if not row:
                return 0.0

            lang_code = (row.customer_language_code or "hi").split("-")[0].lower()
            intent = (row.intent_detected or "general").lower()

            # Step 1: Send verification time message
            time_map = VERIFICATION_TIME_MAP.get(intent)
            if time_map:
                time_text = time_map.get(lang_code, time_map.get("en", time_map.get("hi", "")))
                if time_text:
                    await self.send_to_customer(
                        token_number,
                        "staff_message",
                        {
                            "text": time_text,
                            "language_code": lang_code,
                            "is_verification_time": True,
                        },
                    )
                    logger.info(
                        "Verification time sent | token=%s | intent=%s | time=%s",
                        token_number, intent, time_map.get("time", "?"),
                    )
                    # Small delay between verification and farewell messages
                    await asyncio.sleep(0.5)

            # Step 2: Send farewell message
            farewell_text = FAREWELL_MULTILINGUAL.get(
                lang_code, FAREWELL_MULTILINGUAL["hi"]
            )

            await self.send_to_customer(
                token_number,
                "staff_message",
                {
                    "text": farewell_text,
                    "language_code": lang_code,
                    "is_farewell": True,
                },
            )

            # Generate farewell TTS
            try:
                from services.ai_service import ai_service
                tts_result = await ai_service.generate_tts(
                    text=farewell_text,
                    language_code=row.customer_language_code or "hi",
                    session_id=session_id,
                )
                await self.broadcast_audio(
                    token_number=token_number,
                    audio_url=tts_result.audio_url,
                    duration_seconds=tts_result.duration_seconds,
                    response_text=farewell_text,
                )
                tts_duration = tts_result.duration_seconds or 0.0
                logger.info(
                    "Auto-farewell sent with TTS | token=%s | lang=%s | duration=%.1fs",
                    token_number, lang_code, tts_duration,
                )
            except Exception as tts_exc:
                logger.warning(
                    "Farewell TTS failed (text was sent) | token=%s | %s",
                    token_number, tts_exc,
                )

        except Exception as exc:
            logger.warning(
                "Auto-farewell failed (non-fatal) | token=%s | %s",
                token_number, exc,
            )

        return tts_duration
