"""
VaaniBank AI — WebSocket Connection Manager
PSBs Hackathon 2026 | Team Vectora

Manages all real-time communication between:
  • Staff Panel   (role=staff)
  • Customer Panel (role=customer)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from websocket.helpers import _now_iso, _event, _safe_send
from websocket.connection import ConnectionMixin
from websocket.audio_pipeline import AudioPipelineMixin
from websocket.handlers import HandlersMixin

logger = logging.getLogger("vaanibank.websocket")


class ConnectionManager(ConnectionMixin, AudioPipelineMixin, HandlersMixin):
    """
    Manages active WebSocket connections grouped by session token.
    Acts as the entrypoint facade delegating to mixins.

    active_connections layout:
        {
            "TKN-001": {
                "staff":    <WebSocket | None>,
                "customer": <WebSocket | None>,
            },
            ...
        }
    """

    def __init__(self) -> None:
        self.active_connections: Dict[str, Dict[str, Optional[WebSocket]]] = {}
        self.greeted_tokens: set[str] = set()
        self._redis: Any = None

        # Streaming audio state (per session token)
        self._audio_buffers: Dict[str, list[bytes]] = {}
        self._audio_lang: Dict[str, str] = {}
        self._audio_session_id: Dict[str, int] = {}
        self._partial_stt_running: Dict[str, bool] = {}
        self._last_partial_ts: Dict[str, float] = {}

    # Redis lazy accessor

    async def _get_redis(self):
        if self._redis is None:
            from database import get_redis_client
            self._redis = await get_redis_client()
        return self._redis

    # DB session helper

    async def _get_db(self) -> AsyncSession:
        from database import AsyncSessionLocal
        return AsyncSessionLocal()

    # SEND HELPERS

    async def send_to_staff(
        self, token_number: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send an event to the staff WebSocket for this session."""
        ws = self.active_connections.get(token_number, {}).get("staff")
        return await _safe_send(ws, _event(event_type, data))

    async def send_to_customer(
        self, token_number: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send an event to the customer WebSocket for this session."""
        ws = self.active_connections.get(token_number, {}).get("customer")
        return await _safe_send(ws, _event(event_type, data))

    async def send_to_both(
        self, token_number: str, event_type: str, data: Dict[str, Any]
    ) -> None:
        """Send an event to both staff and customer connections."""
        payload = _event(event_type, data)
        slot = self.active_connections.get(token_number, {})
        for role in ("staff", "customer"):
            await _safe_send(slot.get(role), payload)

    async def broadcast_to_session(
        self,
        token_number: str,
        event: Dict[str, Any],
        exclude_role: Optional[str] = None,
    ) -> None:
        """
        Send a pre-built event envelope to session participants.
        """
        slot = self.active_connections.get(token_number, {})
        for role in ("staff", "customer"):
            if role == exclude_role:
                continue
            await _safe_send(slot.get(role), event)

    async def send_error(
        self,
        token_number: str,
        role: str,
        code: str,
        message: str,
    ) -> None:
        """Send an error event to a specific role."""
        ws = self.active_connections.get(token_number, {}).get(role)
        await _safe_send(
            ws,
            _event("error", {"code": code, "message": message}),
        )

    # BROADCAST METHODS

    async def broadcast_transcription(
        self,
        token_number: str,
        text_original: str,
        text_translated: str,
        confidence: float,
        sentiment: str,
        intent: str,
        pii_detected: bool,
        exchange_id: Optional[int] = None,
    ) -> None:
        """
        event: transcription_ready → staff and customer.
        """
        payload = {
            "text_original": text_original,
            "text_translated": text_translated,
            "confidence": confidence,
            "sentiment": sentiment,
            "intent": intent,
            "pii_detected": pii_detected,
            "exchange_id": exchange_id,
        }
        await self.send_to_staff(token_number, "transcription_ready", payload)
        await self.send_to_customer(token_number, "transcription_ready", payload)

    async def broadcast_suggestion(
        self,
        token_number: str,
        suggested_hindi: str,
        suggested_customer_lang: str,
        intent: str,
        process_triggered: Optional[str],
        exchange_id: Optional[int] = None,
        intent_confidence: float = 0.0,
    ) -> None:
        """
        event: ai_suggestion_ready → staff only.
        """
        await self.send_to_staff(
            token_number,
            "ai_suggestion_ready",
            {
                "suggested_hindi": suggested_hindi,
                "suggested_customer_lang": suggested_customer_lang,
                "intent": intent,
                "process_triggered": process_triggered,
                "exchange_id": exchange_id,
                "intent_confidence": intent_confidence,
            },
        )

        if intent == "balance_enquiry":
            await self.send_to_customer(
                token_number,
                "intent_notification",
                {
                    "intent": "balance_enquiry",
                    "title": "Balance Enquiry",
                    "message": "Aapka balance check kiya ja raha hai.",
                    "message_en": "Your balance is being checked by staff.",
                    "icon": "balance",
                },
            )
            logger.info(
                "balance_enquiry notification sent to customer | token=%s", token_number
            )

    async def broadcast_audio(
        self,
        token_number: str,
        audio_url: str,
        duration_seconds: float,
        response_text: str = "",
    ) -> None:
        """
        event: audio_ready → both customer and staff.
        """
        payload = {
            "audio_url": audio_url,
            "duration_seconds": duration_seconds,
            "staff_response": response_text,
        }
        await self.send_to_customer(token_number, "audio_ready", payload)
        await self.send_to_staff(token_number, "audio_ready", payload)

    async def broadcast_step_update(
        self,
        token_number: str,
        current_step: int,
        total_steps: int,
        progress_percentage: float,
        step_status: str,
        step_text_hindi: Optional[str] = None,
        step_text_customer: Optional[str] = None,
    ) -> None:
        """
        event: step_updated → both staff and customer.
        """
        await self.send_to_both(
            token_number,
            "step_updated",
            {
                "current_step": current_step,
                "total_steps": total_steps,
                "progress_percentage": round(progress_percentage, 1),
                "step_status": step_status,
                "step_text_hindi": step_text_hindi,
                "step_text_customer": step_text_customer,
            },
        )

    async def broadcast_process_update(
        self,
        token_number: str,
        intent: str,
        process_data: dict,
        staff_message: str,
        detected_language: str,
        key_entities: dict,
        key_info: dict,
        product_name: str,
        tts_voice: str,
        confidence: float = 0.0,
    ) -> None:
        """
        event: PROCESS_UPDATE → staff only.
        """
        await self.send_to_staff(
            token_number,
            "PROCESS_UPDATE",
            {
                "intent":             intent,
                "confidence":         confidence,
                "process_data":       process_data,
                "staff_message":      staff_message,
                "detected_language":  detected_language,
                "key_entities":       key_entities,
                "key_info":           key_info,
                "product_name":       product_name,
                "tts_voice":          tts_voice,
            },
        )
        logger.info(
            "PROCESS_UPDATE sent | token=%s | intent=%s | lang=%s",
            token_number, intent, detected_language,
        )

    async def broadcast_pii_alert(
        self,
        token_number: str,
        pii_types: list,
        masked_text: str,
        exchange_id: Optional[int] = None,
    ) -> None:
        """
        event: pii_detected → staff only.
        """
        await self.send_to_staff(
            token_number,
            "pii_detected",
            {
                "pii_types": pii_types,
                "masked_text": masked_text,
                "exchange_id": exchange_id,
                "message": "PII detected and masked per RBI guidelines.",
            },
        )

    async def broadcast_input_request(
        self,
        token_number: str,
        field_type: str,
        field_label: str,
        field_label_customer: str,
        request_id: str,
    ) -> None:
        """
        event: input_request → customer only.
        """
        await self.send_to_customer(
            token_number,
            "input_request",
            {
                "field_type": field_type,
                "field_label": field_label,
                "field_label_customer": field_label_customer,
                "request_id": request_id,
                "message": f"Please provide your {field_label}.",
            },
        )
        logger.info(
            "input_request sent | token=%s | field=%s | id=%s",
            token_number, field_type, request_id,
        )

    async def broadcast_customer_speaking(self, token_number: str) -> None:
        """
        event: customer_speaking → staff only.
        """
        await self.send_to_staff(
            token_number,
            "customer_speaking",
            {"speaking": True},
        )

    async def broadcast_transcription_partial(
        self,
        token_number: str,
        text: str,
        language_code: str = "hi",
    ) -> None:
        """
        event: transcription_partial → staff only.
        """
        await self.send_to_staff(
            token_number,
            "transcription_partial",
            {
                "text": text,
                "is_final": False,
                "language_code": language_code,
            },
        )

    async def broadcast_document_checklist(
        self,
        token_number: str,
        intent: str,
        checklist: list,
        language_code: str,
    ) -> None:
        """
        event: document_checklist → customer only.
        """
        await self.send_to_customer(
            token_number,
            "document_checklist",
            {
                "intent": intent,
                "checklist": checklist,
                "language_code": language_code,
            },
        )
        logger.info(
            "document_checklist sent | token=%s | intent=%s | docs=%d",
            token_number, intent, len(checklist),
        )

    async def broadcast_doc_readiness(
        self,
        token_number: str,
        readiness: dict,
    ) -> None:
        """
        event: doc_readiness_update → staff only.
        """
        await self.send_to_staff(
            token_number,
            "doc_readiness_update",
            readiness,
        )
        logger.info(
            "doc_readiness_update sent | token=%s | score=%s%% | missing=%s",
            token_number,
            readiness.get("score", "?"),
            readiness.get("missing", []),
        )

    async def broadcast_session_ended(
        self,
        token_number: str,
        summary_url: Optional[str],
        duration_seconds: Optional[int],
        total_exchanges: int,
        session_id: Optional[int] = None,
        collected_data: Optional[dict] = None,
        intent: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> None:
        """
        event: session_ended → both staff and customer.
        """
        await self.send_to_both(
            token_number,
            "session_ended",
            {
                "token_number":     token_number,
                "session_id":       session_id,
                "summary_url":      summary_url,
                "duration_seconds": duration_seconds,
                "total_exchanges":  total_exchanges,
                "collected_data":   collected_data or {},
                "intent":           intent or "general",
                "language_code":    language_code or "hi",
                "message":          "Session has ended. Thank you.",
            },
        )

    async def broadcast_all_info_collected(
        self,
        token_number: str,
        lang_code: str,
        intent: str,
        session_id: Optional[int] = None,
    ) -> None:
        """
        Auto-trigger: jab saari info + docs collect ho jaaye toh customer ko
        unki language mein message + TTS bhejo. Once-per-session (Redis guard).

        event: all_info_collected → customer only (text + audio).
        """
        # Redis guard — only send once per session
        _redis_key = f"all_info_collected_sent:{token_number}"
        try:
            redis = await self._get_redis()
            if redis:
                already_sent = await redis.get(_redis_key)
                if already_sent:
                    logger.info(
                        "all_info_collected already sent | token=%s — skipping",
                        token_number,
                    )
                    return
                # Mark as sent (TTL = 2 hours — session lifetime)
                await redis.setex(_redis_key, 7200, "1")
        except Exception as redis_exc:
            logger.warning("Redis guard check failed (will send anyway): %s", redis_exc)

        # Multilingual messages
        _ALL_COLLECTED_MESSAGES = {
            "hi": "🎉 आपकी सारी जानकारी और दस्तावेज़ इकट्ठी कर ली गई है। हम अभी आपकी verification process शुरू कर रहे हैं। आपको कहीं जाने की ज़रूरत नहीं, हम जल्द ही आपको update देंगे।",
            "mr": "🎉 तुमची सर्व माहिती आणि कागदपत्रे गोळा केली गेली आहेत। आम्ही आता तुमची verification प्रक्रिया सुरू करत आहोत। तुम्हाला कुठेही जाण्याची गरज नाही, आम्ही लवकरच तुम्हाला update देऊ.",
            "ta": "🎉 உங்கள் அனைத்து தகவல்களும் ஆவணங்களும் சேகரிக்கப்பட்டன. நாங்கள் இப்போது உங்கள் சரிபார்ப்பு செயல்முறையை தொடங்குகிறோம். நீங்கள் எங்கும் போக வேண்டியதில்லை, நாங்கள் விரைவில் உங்களுக்கு தெரிவிப்போம்.",
            "te": "🎉 మీ సమాచారం మరియు పత్రాలు అన్నీ సేకరించబడ్డాయి. మేము ఇప్పుడు మీ verification ప్రక్రియను ప్రారంభిస్తున్నాం. మీరు ఎక్కడికీ వెళ్ళాల్సిన అవసరం లేదు, మేము త్వరలో మీకు update ఇస్తాం.",
            "bn": "🎉 আপনার সমস্ত তথ্য এবং নথিপত্র সংগ্রহ করা হয়েছে। আমরা এখন আপনার verification প্রক্রিয়া শুরু করছি। আপনাকে কোথাও যেতে হবে না, আমরা শীঘ্রই আপনাকে update জানাব।",
            "kn": "🎉 ನಿಮ್ಮ ಎಲ್ಲಾ ಮಾಹಿತಿ ಮತ್ತು ದಾಖಲೆಗಳನ್ನು ಸಂಗ್ರಹಿಸಲಾಗಿದೆ. ನಾವು ಈಗ ನಿಮ್ಮ verification ಪ್ರಕ್ರಿಯೆಯನ್ನು ಪ್ರಾರಂಭಿಸುತ್ತಿದ್ದೇವೆ. ನೀವು ಎಲ್ಲಿಯೂ ಹೋಗಬೇಕಾಗಿಲ್ಲ, ನಾವು ಶೀಘ್ರದಲ್ಲೇ ನಿಮಗೆ update ನೀಡುತ್ತೇವೆ.",
            "or": "🎉 ଆପଣଙ୍କ ସମସ୍ତ ତଥ୍ୟ ଏବଂ ଦଲିଲ ସଂଗ୍ରହ କରାଯାଇଛି। ଆମେ ଏବେ ଆପଣଙ୍କ verification ପ୍ରକ୍ରିୟା ଆରମ୍ଭ କରୁଛୁ। ଆପଣଙ୍କୁ କୌଣସି ସ୍ଥାନକୁ ଯିବାର ଆବଶ୍ୟକତା ନାହିଁ, ଆମେ ଶୀଘ୍ର ଆପଣଙ୍କୁ update ଦେବୁ।",
            "pa": "🎉 ਤੁਹਾਡੀ ਸਾਰੀ ਜਾਣਕਾਰੀ ਅਤੇ ਦਸਤਾਵੇਜ਼ ਇਕੱਠੇ ਕਰ ਲਏ ਗਏ ਹਨ। ਅਸੀਂ ਹੁਣ ਤੁਹਾਡੀ verification ਪ੍ਰਕਿਰਿਆ ਸ਼ੁਰੂ ਕਰ ਰਹੇ ਹਾਂ। ਤੁਹਾਨੂੰ ਕਿਤੇ ਜਾਣ ਦੀ ਲੋੜ ਨਹੀਂ, ਅਸੀਂ ਜਲਦੀ ਤੁਹਾਨੂੰ update ਦੇਵਾਂਗੇ।",
            "gu": "🎉 તમારી બધી માહિતી અને દસ્તાવેજો એકઠા કરી લેવામાં આવ્યા છે। અમે હવે તમારી verification પ્રક્રિયા શરૂ કરી રહ્યા છીએ। તમારે ક્યાંય જવાની જરૂર નથી, અમે જલ્દી તમને update આપીશું.",
            "ml": "🎉 നിങ്ങളുടെ എല്ലാ വിവരങ്ങളും രേഖകളും ശേഖരിക്കപ്പെട്ടിരിക്കുന്നു. ഞങ്ങൾ ഇപ്പോൾ നിങ്ങളുടെ verification പ്രക്രിയ ആരംഭിക്കുന്നു. നിങ്ങൾ എവിടെയും പോകേണ്ടതില്ല, ഞങ്ങൾ ഉടൻ നിങ്ങളെ update ചെയ്യും.",
            "en": "🎉 All your information and documents have been collected. We are now starting your verification process. You don't need to go anywhere — we will update you shortly.",
        }

        short_lang = lang_code.split("-")[0].lower() if lang_code else "hi"
        message_text = _ALL_COLLECTED_MESSAGES.get(
            short_lang, _ALL_COLLECTED_MESSAGES["en"]
        )

        # Send text to customer panel
        await self.send_to_customer(
            token_number,
            "all_info_collected",
            {
                "text": message_text,
                "language_code": short_lang,
                "intent": intent,
                "is_auto": True,
            },
        )

        # Also show as staff_message so it appears in customer chat UI
        await self.send_to_customer(
            token_number,
            "staff_message",
            {
                "text": message_text,
                "language_code": short_lang,
                "is_auto_completion": True,
            },
        )

        logger.info(
            "all_info_collected sent to customer | token=%s | lang=%s | intent=%s",
            token_number, short_lang, intent,
        )

        # Generate TTS + send audio to customer
        try:
            from services.ai_service import ai_service
            tts_result = await ai_service.generate_tts(
                text=message_text,
                language_code=short_lang,
                session_id=session_id,
            )
            await self.broadcast_audio(
                token_number=token_number,
                audio_url=tts_result.audio_url,
                duration_seconds=tts_result.duration_seconds,
                response_text=message_text,
            )
            logger.info(
                "all_info_collected TTS sent | token=%s | duration=%.1fs",
                token_number, tts_result.duration_seconds,
            )
        except Exception as tts_exc:
            logger.warning(
                "all_info_collected TTS failed (text was delivered) | token=%s | %s",
                token_number, tts_exc,
            )


# Module-level singleton
ws_manager = ConnectionManager()