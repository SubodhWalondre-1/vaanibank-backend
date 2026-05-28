"""
VaaniBank AI — WebSocket Message Handlers Mixin
PSBs Hackathon 2026 | Team Vectora
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import func, select, update

from websocket.helpers import _event, _safe_send, _now_iso

# Language maps
from core.language import LANG_CODE_TO_ATTR as _LANG_CODE_MAP, lang_code_to_attr as _lang_code_to_attr

logger = logging.getLogger("vaanibank.websocket.handlers")


class HandlersMixin:
    """
    Mixin for ConnectionManager containing staff and customer client packet handlers.
    """

    async def handle_staff_message(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Route incoming Client→Server messages from the staff panel.
        """
        event_type: str = data.get("type", "") or data.get("event", "")

        if event_type == "ping":
            await self.send_to_staff(
                token_number, "pong", {"timestamp": _now_iso()}
            )
            return

        if event_type in ("staff_approved_response", "staff_edited_response"):
            await self._handle_staff_response(token_number, data, event_type)
            return

        if event_type == "step_completed":
            await self._handle_step_completed(token_number, data)
            return

        if event_type == "end_session":
            await self._handle_end_session(token_number, data)
            return

        if event_type == "regenerate_suggestion":
            await self._handle_regenerate_suggestion(token_number, data)
            return

        if event_type == "trigger_input_request":
            await self._handle_trigger_input_request(token_number, data)
            return

        if event_type == "submit_verification":
            await self._handle_submit_verification(token_number, data)
            return

        if event_type == "send_to_saral_form":
            await self._handle_send_to_saral_form(token_number, data)
            return

        logger.warning(
            "Unknown staff WS event | token=%s | event=%s", token_number, event_type
        )
        await self.send_error(
            token_number,
            "staff",
            "unknown_event",
            f"Unknown event type: {event_type}",
        )

    async def handle_customer_message(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Route incoming Client→Server messages from the customer panel.
        """
        event_type: str = data.get("type", "") or data.get("event", "")

        if event_type == "ping":
            await self.send_to_customer(
                token_number, "pong", {"timestamp": _now_iso()}
            )
            return

        # ── Streaming audio: session start ───────────────────────────────────
        if event_type == "start_speaking":
            await self._handle_start_speaking(token_number, data)
            return

        # ── Streaming audio: session end → trigger full pipeline ─────────────
        if event_type == "stop_speaking":
            await self._handle_stop_speaking(token_number, data)
            return

        if event_type == "customer_service_selected":
            # Forward the service selection to the staff panel
            payload = data.get("data", data)
            service_id = payload.get("service_id", "general")
            service_name = payload.get("service_name", "General")

            # Update DB with customer selected intent
            try:
                from models import Session as SessionModel
                async with await self._get_db() as db:
                    await db.execute(
                        update(SessionModel)
                        .where(SessionModel.token_number == token_number)
                        .values(intent_detected=service_id)
                    )
                    await db.commit()
            except Exception as db_exc:
                logger.warning(
                    "Failed to update intent on customer_service_selected | token=%s | %s",
                    token_number, db_exc
                )

            await self.send_to_staff(
                token_number,
                "customer_service_selected",
                {
                    "service_id": service_id,
                    "service_name": service_name,
                    "message": f"Customer selected: {service_name}",
                },
            )
            logger.info(
                "Customer service selected | token=%s | service=%s",
                token_number,
                service_name,
            )

            # ── Auto-greeting: when customer selects service and staff is present ──
            # Sends a localized welcome message + TTS audio to the customer
            staff_ws = self.active_connections[token_number].get("staff")
            if staff_ws is not None:
                if token_number not in self.greeted_tokens:
                    self.greeted_tokens.add(token_number)
                    asyncio.create_task(self._send_auto_greeting(token_number))

            return

        if event_type == "end_session":
            # Customer ended the session
            # session_id may not be in payload — look up in DB via token_number
            await self._handle_end_session_by_token(token_number, data)
            return

        if event_type == "input_submitted":
            await self._handle_input_submitted(token_number, data)
            return

        if event_type == "document_confirmed":
            await self._handle_document_confirmed(token_number, data)
            return

        if event_type == "demo_customer_message":
            payload = data.get("data", data)
            await self.handle_demo_message(token_number, payload)
            return

        # Silently ignore unknown customer events
        logger.debug(
            "Unknown customer WS event | token=%s | event=%s", token_number, event_type
        )

    async def _handle_staff_response(
        self,
        token_number: str,
        data: Dict[str, Any],
        event_type: str,
    ) -> None:
        """
        Process staff_approved_response or staff_edited_response.
        """
        payload = data.get("data", data)
        response_text: str = payload.get("response_text", "")
        target_language_code: str = payload.get("target_language_code", "hi")
        use_suggestion: bool = payload.get("use_suggestion", True)
        session_id: Optional[int] = payload.get("session_id")
        exchange_id: Optional[int] = payload.get("exchange_id")
        step_id: Optional[int] = payload.get("step_id")
        provided_translation: Optional[str] = payload.get("translated_text")

        if not response_text:
            await self.send_error(
                token_number, "staff", "invalid_payload", "response_text is required."
            )
            return

        # ── Immediate typing indicator → customer sees "Staff is responding..." ──
        await self.send_to_customer(
            token_number,
            "staff_typing",
            {"typing": True},
        )

        from models import Session as SessionModel, ProcessStep

        real_lang_code = target_language_code  # fallback to what frontend sent
        customer_text = provided_translation or response_text  # use provided if available
        short_lang = "hi"  # default

        try:
            async with await self._get_db() as db:
                # 1. Look up real customer language
                if session_id:
                    sess_result = await db.execute(
                        select(SessionModel.customer_language_code).where(
                            SessionModel.id == session_id
                        )
                    )
                    db_lang = sess_result.scalar_one_or_none()
                    if db_lang:
                        real_lang_code = db_lang
                        logger.info(
                            "Resolved customer language from DB: %s | session_id=%s",
                            real_lang_code, session_id,
                        )

                # 2. Resolve customer-language text from ProcessStep (if not provided)
                short_lang = real_lang_code.split("-")[0].lower() if real_lang_code else "hi"
                if not provided_translation and short_lang and short_lang != "hi" and step_id:
                    lang_suffix = _LANG_CODE_MAP.get(short_lang, "")
                    if lang_suffix:
                        result = await db.execute(
                            select(ProcessStep).where(ProcessStep.id == step_id)
                        )
                        step_row = result.scalar_one_or_none()
                        if step_row:
                            col_name = f"step_text_{lang_suffix}"
                            translated = getattr(step_row, col_name, None)
                            if translated:
                                customer_text = translated
                                logger.info(
                                    "Resolved %s text from DB step_id=%s | token=%s",
                                    col_name, step_id, token_number,
                                )
                            else:
                                logger.warning(
                                    "DB step has no %s text, will use LLM translate | step_id=%s",
                                    col_name, step_id,
                                )
        except Exception as exc:
            logger.warning(
                "DB read operations failed in staff response | token=%s | %s",
                token_number, exc,
            )

        # ── Fallback: LLM translate if customer_text is still same as source and lang != hi ──
        if short_lang and short_lang != "hi" and customer_text == response_text:
            try:
                from services.ai_service import ai_service
                translated = await ai_service.translate_text(
                    text=response_text,
                    target_language_code=real_lang_code,
                )
                if translated:
                    customer_text = translated
                    logger.info(
                        "LLM fallback translation used | token=%s | lang=%s",
                        token_number, real_lang_code,
                    )
            except Exception as exc:
                logger.warning(
                    "LLM translate fallback failed | token=%s | %s", token_number, exc
                )

        # ── Send text to customer panel immediately (shows before/regardless of TTS) ──
        await self.send_to_customer(
            token_number,
            "staff_message",
            {
                "text": customer_text,
                "language_code": real_lang_code,
                "exchange_id": exchange_id,
            },
        )
        logger.info(
            "staff_message sent to customer | token=%s | lang=%s",
            token_number, real_lang_code,
        )

        # ── Check staff speech for PII keywords → trigger customer input popup ────────────
        await self._trigger_staff_input_if_needed(token_number, response_text)

        # ── Generate TTS (in customer language) ────────────────────────────────
        try:
            from services.ai_service import ai_service
            tts_result = await ai_service.generate_tts(
                text=customer_text,
                language_code=real_lang_code,
                session_id=session_id,
            )
        except Exception as exc:
            logger.error(
                "TTS failed in staff response handler | token=%s | %s",
                token_number,
                exc,
            )
            # Text already sent via staff_message — don't block, just skip audio
            await self.send_error(
                token_number,
                "staff",
                "tts_error",
                "Audio generation failed — text was delivered to customer.",
            )
            return

        # ── Persist to exchanges table ─────────────────────────────────────────
        if exchange_id and session_id:
            try:
                async with await self._get_db() as db:
                    from models import Exchange
                    await db.execute(
                        update(Exchange)
                        .where(Exchange.id == exchange_id)
                        .values(
                            staff_response_final=response_text,
                            staff_response_translated=customer_text,
                            staff_audio_url=tts_result.audio_url,
                            staff_used_suggestion=use_suggestion,
                        )
                    )
                    await db.commit()
            except Exception as exc:
                logger.warning(
                    "Exchange update failed | exchange_id=%s | %s", exchange_id, exc
                )

        # ── Update analytics (ai_suggestion_used / edited / ignored) ──────────
        if session_id:
            await self._update_suggestion_analytics(
                session_id=session_id,
                event_type=event_type,
                use_suggestion=use_suggestion,
            )

        # ── Send audio to customer (with translated text) ──────────────────────
        await self.broadcast_audio(
            token_number=token_number,
            audio_url=tts_result.audio_url,
            duration_seconds=tts_result.duration_seconds,
            response_text=customer_text,
        )

        logger.info(
            "Staff response sent | token=%s | use_suggestion=%s | tts_cache=%s",
            token_number,
            use_suggestion,
            tts_result.from_cache,
        )

    async def _handle_regenerate_suggestion(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Regenerate a suggestion for the current session.
        """
        payload = data.get("data", data)
        session_id: Optional[int] = payload.get("session_id")

        if not session_id:
            await self.send_error(
                token_number, "staff", "invalid_payload", "session_id is required for regeneration."
            )
            return

        try:
            async with await self._get_db() as db:
                from models import Session, Exchange
                from services.ai_service import ai_service
                from services.llm_utils import (
                    build_conversation_history,
                    fetch_history_rows,
                    pre_detect_intent,
                )
                from services.pipeline_orchestrator import _load_ubi_knowledge_base

                # 1. Fetch session and latest exchange
                sess_result = await db.execute(
                    select(Session).where(Session.id == session_id)
                )
                session_obj = sess_result.scalar_one_or_none()
                if not session_obj:
                    return

                # Fetch the most recent customer message to re-process
                last_exchange_result = await db.execute(
                    select(Exchange)
                    .where(Exchange.session_id == session_id, Exchange.direction == "customer_to_staff")
                    .order_by(Exchange.id.desc())
                    .limit(1)
                )
                last_exchange = last_exchange_result.scalar_one_or_none()

                if not last_exchange:
                    await self.send_error(
                        token_number, "staff", "no_history", "No customer message found to regenerate suggestion for."
                    )
                    return

                # 2. Re-run LLM pipeline logic
                customer_text = last_exchange.pii_masked_text or last_exchange.customer_text_original
                history_rows = await fetch_history_rows(db, session_id, last_exchange.id)
                history = build_conversation_history(history_rows, session_obj.collected_data or {})

                pre_intent = session_obj.intent_detected or pre_detect_intent(customer_text)

                # Add a hint to LLM to be different
                history.append({
                    "role": "user",
                    "content": "[SYSTEM HINT: The previous suggestion was ignored or rejected by staff. Please provide a DIFFERENT, alternative suggestion that moves the conversation forward based on the last customer input.]"
                })

                # Load KB
                rag_context = await _load_ubi_knowledge_base(intent=pre_intent)

                result = await ai_service.process_with_llm(
                    text=customer_text,
                    source_language=session_obj.customer_language or "Marathi",
                    detected_intent=pre_intent,
                    conversation_history=history,
                    rag_context=rag_context,
                )

                # 3. Broadcast new suggestion
                await self.broadcast_suggestion(
                    token_number=token_number,
                    suggested_hindi=result.suggested_response_hindi,
                    suggested_customer_lang=result.suggested_response_customer_lang,
                    intent=result.intent,
                    process_triggered=result.process_triggered,
                    exchange_id=last_exchange.id,
                )

                logger.info(
                    "Suggestion regenerated | token=%s | session_id=%s | exchange_id=%s",
                    token_number, session_id, last_exchange.id,
                )

        except Exception as exc:
            logger.error(
                "Regenerate suggestion failed | token=%s | %s",
                token_number, exc, exc_info=True,
            )
            await self.send_error(
                token_number, "staff", "regen_failed", "Failed to regenerate suggestion."
            )

    async def _handle_step_completed(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Mark a process step as completed in DB, then broadcast step_updated.
        """
        payload = data.get("data", data)
        session_id: Optional[int] = payload.get("session_id")
        step_id: Optional[int] = payload.get("step_id")
        tracking_id: Optional[int] = payload.get("tracking_id")

        if not session_id or not step_id:
            await self.send_error(
                token_number,
                "staff",
                "invalid_payload",
                "session_id and step_id are required for step_completed.",
            )
            return

        current_step = 0
        total_steps = 0
        step_text_hindi: Optional[str] = None
        step_text_customer: Optional[str] = None

        try:
            async with await self._get_db() as db:
                from models import (
                    ProcessStep,
                    Session,
                    SessionProcessTracking,
                )

                # ── Lazy-insert: ensure SessionProcessTracking rows exist ──────
                existing_count_result = await db.execute(
                    select(func.count(SessionProcessTracking.id)).where(
                        SessionProcessTracking.session_id == session_id
                    )
                )
                existing_count = existing_count_result.scalar() or 0

                if existing_count == 0:
                    # Fetch the session to get intent_detected
                    sess_result = await db.execute(
                        select(Session).where(Session.id == session_id)
                    )
                    sess_obj = sess_result.scalar_one_or_none()
                    intent = sess_obj.intent_detected if sess_obj else None

                    if intent:
                        # Load all ProcessStep rows for this intent
                        steps_result = await db.execute(
                            select(ProcessStep)
                            .where(
                                ProcessStep.intent_type == str(intent),
                                ProcessStep.is_active == True,
                            )
                            .order_by(ProcessStep.step_number)
                        )
                        intent_steps = steps_result.scalars().all()

                        # Bulk-insert tracking rows as 'pending'
                        for ps in intent_steps:
                            db.add(SessionProcessTracking(
                                session_id=session_id,
                                step_id=ps.id,
                                status="pending",
                            ))
                        await db.flush()
                        logger.info(
                            "Lazy-inserted %d tracking rows | session=%s | intent=%s",
                            len(intent_steps), session_id, intent,
                        )

                # ── Mark this step completed ───────────────────────────────────
                now = datetime.now(timezone.utc)
                if tracking_id:
                    await db.execute(
                        update(SessionProcessTracking)
                        .where(SessionProcessTracking.id == tracking_id)
                        .values(status="completed", completed_at=now)
                    )
                else:
                    await db.execute(
                        update(SessionProcessTracking)
                        .where(
                            SessionProcessTracking.session_id == session_id,
                            SessionProcessTracking.step_id == step_id,
                        )
                        .values(status="completed", completed_at=now)
                    )

                # ── Count progress ─────────────────────────────────────────────
                session_result = await db.execute(
                    select(Session).where(Session.id == session_id)
                )
                session_obj = session_result.scalar_one_or_none()

                if session_obj:
                    total_result = await db.execute(
                        select(func.count(SessionProcessTracking.id)).where(
                            SessionProcessTracking.session_id == session_id
                        )
                    )
                    total_steps = total_result.scalar() or 0

                    completed_result = await db.execute(
                        select(func.count(SessionProcessTracking.id)).where(
                            SessionProcessTracking.session_id == session_id,
                            SessionProcessTracking.status == "completed",
                        )
                    )
                    current_step = completed_result.scalar() or 0

                    # Get step text for next pending step
                    next_tracking_result = await db.execute(
                        select(SessionProcessTracking)
                        .where(
                            SessionProcessTracking.session_id == session_id,
                            SessionProcessTracking.status == "pending",
                        )
                        .order_by(SessionProcessTracking.id)
                        .limit(1)
                    )
                    next_tracking = next_tracking_result.scalar_one_or_none()

                    if next_tracking:
                        step_result = await db.execute(
                            select(ProcessStep).where(
                                ProcessStep.id == next_tracking.step_id
                            )
                        )
                        next_step = step_result.scalar_one_or_none()
                        if next_step:
                            step_text_hindi = next_step.step_text_hindi
                            # Use customer language step text
                            lang_code = session_obj.customer_language_code or "hi"
                            lang_attr = f"step_text_{_lang_code_to_attr(lang_code)}"
                            step_text_customer = getattr(
                                next_step, lang_attr, step_text_hindi
                            )

                await db.commit()

        except Exception as exc:
            logger.error(
                "step_completed DB update failed | token=%s | %s", token_number, exc
            )
            await self.send_error(
                token_number,
                "staff",
                "db_error",
                "Step update failed. Please try again.",
            )
            return

        progress = (current_step / total_steps * 100) if total_steps else 0.0

        await self.broadcast_step_update(
            token_number=token_number,
            current_step=current_step,
            total_steps=total_steps,
            progress_percentage=progress,
            step_status="completed",
            step_text_hindi=step_text_hindi,
            step_text_customer=step_text_customer,
        )

        logger.info(
            "Step completed | token=%s | step=%d/%d", token_number, current_step, total_steps
        )

    async def _handle_end_session_by_token(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Customer panel end_session — session_id payload mein nahi hota.
        """
        try:
            async with await self._get_db() as db:
                from models import Session as SessionModel
                result = await db.execute(
                    select(SessionModel).where(
                        SessionModel.token_number == token_number
                    )
                )
                session_obj = result.scalar_one_or_none()

            if not session_obj:
                logger.warning(
                    "end_session_by_token: session not found | token=%s", token_number
                )
                return

            # Already ended — just broadcast session_ended so customer navigates
            if session_obj.status in ("completed", "abandoned"):
                await self.broadcast_session_ended(
                    token_number=token_number,
                    summary_url=None,
                    duration_seconds=session_obj.duration_seconds,
                    total_exchanges=session_obj.total_exchanges or 0,
                    session_id=session_obj.id,
                    collected_data=session_obj.collected_data or {},
                    intent=str(session_obj.intent_detected or "general"),
                    language_code=session_obj.customer_language_code or "hi",
                )
                return

            # Inject session_id into data so _handle_end_session can use it
            patched_data = dict(data)
            inner = dict(data.get("data", data))
            inner["session_id"] = session_obj.id
            patched_data["data"] = inner

            await self._handle_end_session(token_number, patched_data)

        except Exception as exc:
            logger.error(
                "_handle_end_session_by_token failed | token=%s | %s", token_number, exc
            )

    async def _handle_end_session(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        End a session.
        """
        payload = data.get("data", data)
        session_id: Optional[int] = payload.get("session_id")

        if not session_id:
            await self.send_error(
                token_number, "staff", "invalid_payload", "session_id is required."
            )
            return

        summary_url: Optional[str] = None
        duration_seconds: Optional[int] = None
        total_exchanges: int = 0

        try:
            async with await self._get_db() as db:
                from models import BilingualSummary, Session

                # Fetch session
                result = await db.execute(
                    select(Session).where(Session.id == session_id)
                )
                session_obj = result.scalar_one_or_none()

                if not session_obj:
                    await self.send_error(
                        token_number, "staff", "not_found", "Session not found."
                    )
                    return

                now = datetime.now(timezone.utc)
                started = session_obj.started_at

                if started:
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    duration_seconds = int((now - started).total_seconds())

                total_exchanges = session_obj.total_exchanges or 0

                # Mark session completed
                await db.execute(
                    update(Session)
                    .where(Session.id == session_id)
                    .values(
                        status="completed",
                        ended_at=now,
                        duration_seconds=duration_seconds,
                    )
                )

                # Fetch summary if exists
                summary_result = await db.execute(
                    select(BilingualSummary).where(
                        BilingualSummary.session_id == session_id
                    )
                )
                summary_obj = summary_result.scalar_one_or_none()
                if summary_obj:
                    summary_url = summary_obj.pdf_url

                await db.commit()

            # ── Generate PDF if no summary yet ────────────────────────────────
            if summary_url is None:
                try:
                    summary_url = await self._generate_session_summary(
                        session_id=session_id,
                        token_number=token_number,
                    )
                except Exception as exc:
                    logger.warning(
                        "PDF generation failed on end_session | %s", exc
                    )

        except Exception as exc:
            logger.error(
                "end_session DB update failed | token=%s | %s", token_number, exc
            )
            await self.send_error(
                token_number, "staff", "db_error", "Session end failed. Please retry."
            )
            return

        # ── Clear Redis active_session key ────────────────────────────────────
        try:
            redis = await self._get_redis()
            await redis.delete(f"active_session:{token_number}")
        except Exception as exc:
            logger.warning("Redis cleanup failed on end_session: %s", exc)

        # Auto-farewell: send thank-you + "any other assistance" message before ending
        await self._send_auto_farewell(token_number, session_id)

        # Re-fetch session to get latest collected_data + language for SaralForm
        _final_collected_data: dict = {}
        _final_intent: str = "general"
        _final_language: str = "hi"
        try:
            async with await self._get_db() as db:
                from models import Session as _FinalSession
                _res = await db.execute(
                    select(_FinalSession).where(_FinalSession.id == session_id)
                )
                _final_sess = _res.scalar_one_or_none()
                if _final_sess:
                    _final_collected_data = _final_sess.collected_data or {}
                    _final_intent = str(_final_sess.intent_detected or "general")
                    _final_language = _final_sess.customer_language_code or "hi"
        except Exception:
            pass  # non-fatal

        await self.broadcast_session_ended(
            token_number=token_number,
            summary_url=summary_url,
            duration_seconds=duration_seconds,
            total_exchanges=total_exchanges,
            session_id=session_id,
            collected_data=_final_collected_data,
            intent=_final_intent,
            language_code=_final_language,
        )

        logger.info(
            "Session ended | token=%s | duration=%ss | exchanges=%d",
            token_number,
            duration_seconds,
            total_exchanges,
        )

    async def _handle_trigger_input_request(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Staff panel triggered input request popup.
        """
        payload = data.get("data", data)
        field_type: str = payload.get("field_type", "")
        field_label: str = payload.get("field_label", "")
        field_label_customer: str = payload.get("field_label_customer", "")
        request_id: str = payload.get("request_id", "")

        if not field_type or not field_label:
            logger.warning(
                "trigger_input_request missing fields | token=%s | payload=%s",
                token_number, payload,
            )
            return

        await self.broadcast_input_request(
            token_number=token_number,
            field_type=field_type,
            field_label=field_label,
            field_label_customer=field_label_customer,
            request_id=request_id,
        )
        logger.info(
            "trigger_input_request handled | token=%s | field=%s | id=%s",
            token_number, field_type, request_id,
        )

    async def _handle_submit_verification(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Process submit_verification.
        """
        payload = data.get("data", data)
        session_id: Optional[int] = payload.get("session_id")

        if not session_id:
            await self.send_error(
                token_number, "staff", "invalid_payload", "session_id is required."
            )
            return

        # ── Immediate typing indicator → customer sees "Staff is responding..." ──
        await self.send_to_customer(
            token_number,
            "staff_typing",
            {"typing": True},
        )

        from models import Session as SessionModel

        real_lang_code = "hi"
        collected = {}
        intent = "general"

        VERIFICATION_SUBMITTED_MULTILINGUAL = {
            "hi": "सत्यापन प्रक्रिया शुरू हो गई है। क्या आपको किसी और चीज़ में मदद चाहिए?",
            "mr": "सत्यापन प्रक्रिया सुरू झाली आहे. तुम्हाला इतर कशात मदत हवी आहे का?",
            "ta": "சரிபார்ப்பு செயல்முறை தொடங்கப்பட்டுள்ளது. உங்களுக்கு வேறு ஏதேனும் உதவி தேவையா?",
            "te": "ధృవీకరణ ప్రక్రియ ప్రారంభమైంది. మీకు ఇంకా ఏదైనా సహాయం కావాలా?",
            "bn": "যাচাইকরণ প্রক্রিয়া শুরু হয়েছে। আপনার কি অন্য কিছুতে সাহায্য লাগবে?",
            "kn": "ಪರಿಶೀಲನೆ ಪ್ರಕ್ರಿಯೆಯು ಪ್ರಾರಂಭವಾಗಿದೆ. ನಿಮಗೆ ಬೇರೆ ಯಾವುದಾದರೂ ಸಹಾಯ ಬೇಕೇ?",
            "or": "ଯାଞ୍ચ ପ୍ରକ୍ରିୟା ଆରମ୍ଭ ହୋଇଛି | ଆପଣଙ୍କୁ ଅନ୍ୟ କିଛି ସାହାଯ୍ୟ ଦରକାର କି?",
            "pa": "ਤਸਦੀਕ ਪ੍ਰਕਿਰਿਆ ਸ਼ੁਰੂ ਹੋ ਗਈ ਹੈ। ਕੀ ਤੁਹਾਨੂੰ ਕਿਸੇ ਹੋष ਚੀਜ਼ ਵਿੱਚ ਮਦद ਚਾਹੀਦੀ ਹੈ?",
            "gu": "ચકાસણી પ્રક્રિયા શરૂ થઈ ગઈ છે. શું તમારે બીજી કોઈ મદદની જરૂર છે?",
            "ml": "സ്ഥിരീകരണ പ്രക്രിയ ആരംഭിച്ചു. നിങ്ങൾക്ക് മറ്റെന്തെങ്കിലും സഹായം ആവശ്യമുണ്ടോ?",
            "en": "The verification process has started. Do you need help with anything else?",
        }

        try:
            async with await self._get_db() as db:
                result = await db.execute(
                    select(SessionModel).where(SessionModel.id == session_id)
                )
                session_obj = result.scalar_one_or_none()
                if session_obj:
                    real_lang_code = session_obj.customer_language_code or "hi"
                    collected = session_obj.collected_data or {}
                    intent = session_obj.intent_detected or "general"

                    # Update collected_data
                    collected["verification_submitted"] = True
                    session_obj.collected_data = collected
                    await db.commit()
                    logger.info(
                        "Session verification_submitted set to True | token=%s | session_id=%s",
                        token_number, session_id,
                    )
        except Exception as exc:
            logger.warning(
                "DB update failed in submit_verification | token=%s | %s",
                token_number, exc,
            )

        short_lang = real_lang_code.split("-")[0].lower() if real_lang_code else "hi"
        customer_text = VERIFICATION_SUBMITTED_MULTILINGUAL.get(
            short_lang, VERIFICATION_SUBMITTED_MULTILINGUAL["en"]
        )

        # ── Send text to customer panel immediately ──
        await self.send_to_customer(
            token_number,
            "staff_message",
            {
                "text": customer_text,
                "language_code": real_lang_code,
            },
        )

        # ── Generate TTS (in customer language) ──
        try:
            from services.ai_service import ai_service
            tts_result = await ai_service.generate_tts(
                text=customer_text,
                language_code=real_lang_code,
                session_id=session_id,
            )

            # Send audio to customer
            await self.broadcast_audio(
                token_number=token_number,
                audio_url=tts_result.audio_url,
                duration_seconds=tts_result.duration_seconds,
                response_text=customer_text,
            )
        except Exception as exc:
            logger.error(
                "TTS failed in submit_verification handler | token=%s | %s",
                token_number, exc,
            )
            await self.send_to_customer(
                token_number,
                "staff_typing",
                {"typing": False},
            )

        # ── Broadcast updated navigator state to staff ──
        try:
            from services.session_navigator import compute_next_actions
            from services.document_service import compute_readiness

            _rd = compute_readiness(intent, collected)
            _doc_score = _rd.get("score", 0)

            nav_state = compute_next_actions(
                intent=intent,
                collected_info=collected,
                doc_readiness_score=_doc_score,
                conversation_stage="applying",
                exchange_count=0,
            )

            await self.send_to_staff(
                token_number,
                "navigator_update",
                nav_state,
            )
        except Exception as nav_exc:
            logger.warning(
                "Navigator update after submit_verification failed | token=%s | %s",
                token_number, nav_exc,
            )

    async def _handle_input_submitted(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Customer submitted a typed PII value via the input popup.
        """
        from services.pii_service import pii_service

        payload = data.get("data", data)
        raw_value: str = payload.get("value", "")
        field_type: str = payload.get("field_type", "")
        field_label: str = payload.get("field_label", "")
        request_id: str = payload.get("request_id", "")

        if not raw_value or not field_type:
            return

        # Mask using existing PII service
        pii_result = pii_service.detect_and_mask(raw_value)
        masked_value = pii_result.masked_text if pii_result.pii_found else "****" + raw_value[-4:] if len(raw_value) >= 4 else "****"

        # ── Save raw value to Session table ────────────────────────────────────────
        _PII_FIELD_MAP = {
            "account_number": "customer_account_number",
            "phone": "customer_mobile_number",
            "pan": "customer_pan",
            "dob": "customer_dob",
            "aadhaar": "customer_aadhaar_last4",
            "ifsc": None,
        }
        col = _PII_FIELD_MAP.get(field_type)
        if col:
            try:
                from database import AsyncSessionLocal
                from models import Session as SessionModel
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(SessionModel)
                        .where(SessionModel.token_number == token_number)
                        .values({col: raw_value})
                    )
                    await db.commit()
            except Exception as exc:
                logger.warning(
                    "Session PII save failed | token=%s | field=%s | %s",
                    token_number, col, exc,
                )

        # ── Update collected_data → broadcast info_board_update ──
        _POPUP_TO_COLLECTED_INFO_KEY = {
            "phone": "phone_number_provided",
            "aadhaar": "aadhaar_provided",
            "pan": "pan_provided",
            "account_number": "account_number_provided",
            "dob": "identity_verified",
        }
        ci_key = _POPUP_TO_COLLECTED_INFO_KEY.get(field_type)
        if ci_key:
            try:
                from database import AsyncSessionLocal
                from models import Session as SessionModel
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(
                            SessionModel.collected_data,
                            SessionModel.intent_detected,
                            SessionModel.customer_language_code,
                        ).where(SessionModel.token_number == token_number)
                    )
                    row = result.one_or_none()
                    if row:
                        collected = row.collected_data or {}
                        intent = row.intent_detected or "general"

                        collected[ci_key] = masked_value

                        await db.execute(
                            update(SessionModel)
                            .where(SessionModel.token_number == token_number)
                            .values(collected_data=collected)
                        )
                        await db.commit()

                        total_fields = len(collected)
                        filled = sum(
                            1 for v in collected.values()
                            if v is not None and v is not False and v != ""
                        )
                        completion_pct = int((filled / max(total_fields, 1)) * 100)

                        await self.send_to_staff(
                            token_number,
                            "info_board_update",
                            {
                                "collected_info": collected,
                                "completion_percent": completion_pct,
                                "popup_field_updated": ci_key,
                            },
                        )

                        # Broadcast navigator_update
                        try:
                            from services.session_navigator import compute_next_actions
                            from services.document_service import compute_readiness
                            _rd = compute_readiness(intent, collected)
                            _doc_score = _rd.get("score", 0)
                            nav_state = compute_next_actions(
                                intent=intent,
                                collected_info=collected,
                                doc_readiness_score=_doc_score,
                                conversation_stage="applying",
                                exchange_count=0,
                            )
                            await self.send_to_staff(
                                token_number,
                                "navigator_update",
                                nav_state,
                            )
                        except Exception as nav_exc:
                            logger.debug("Navigator update after popup failed: %s", nav_exc)

                        # Broadcast doc readiness
                        try:
                            from services.document_service import build_checklist
                            checklist = build_checklist(intent, collected)
                            await self.send_to_staff(
                                token_number,
                                "doc_readiness_update",
                                checklist,
                            )
                        except Exception as drv_exc:
                            logger.debug("DRV update after popup failed: %s", drv_exc)

            except Exception as sync_exc:
                logger.warning(
                    "Popup→InfoBoard sync failed (non-fatal) | token=%s | %s",
                    token_number, sync_exc,
                )

        # Forward masked value to staff
        await self.send_to_staff(
            token_number,
            "input_received",
            {
                "field_type": field_type,
                "field_label": field_label,
                "masked_value": masked_value,
                "request_id": request_id,
                "message": f"Customer provided {field_label} (masked for security).",
            },
        )

        # Acknowledge customer
        await self.send_to_customer(
            token_number,
            "input_acknowledged",
            {
                "request_id": request_id,
                "message": "Thank you! Your information has been received securely.",
            },
        )

        logger.info(
            "input_submitted handled | token=%s | field=%s | id=%s",
            token_number, field_type, request_id,
        )

    async def _handle_document_confirmed(
        self,
        token_number: str,
        data: dict,
    ) -> None:
        """
        Customer confirmed document checkbox on phone.
        """
        payload = data.get("data", data)
        doc_id: str = payload.get("doc_id", "")
        doc_label: str = payload.get("doc_label", "")
        confirmed: bool = payload.get("confirmed", True)

        if not doc_id:
            return

        # Forward to staff
        await self.send_to_staff(
            token_number,
            "document_confirmed",
            {
                "doc_id": doc_id,
                "doc_label": doc_label,
                "confirmed": confirmed,
                "message": f"Customer confirmed: {doc_label}" if confirmed
                           else f"Customer unchecked: {doc_label}",
            },
        )
        logger.info(
            "document_confirmed | token=%s | doc=%s | confirmed=%s",
            token_number, doc_id, confirmed,
        )

    async def handle_demo_message(self, session_token: str, data: dict):
        """Broadcast simulation exchanges during demonstration mode."""
        customer_text = data.get("customerText")
        sentiment = data.get("sentiment", "calm")
        intent = data.get("intent", "account_opening")
        translated_text = data.get("translatedText", customer_text)

        await self.send_to_staff(
            session_token,
            "transcription_ready",
            {
                "text_original": customer_text,
                "text_translated": translated_text,
                "confidence": 0.97,
                "sentiment": sentiment,
                "intent": intent,
                "pii_detected": False,
                "is_demo": True
            }
        )

        await asyncio.sleep(1.5)

        demo_id = data.get("id", 1) - 1
        demo_responses = [
            {"suggestedHindi": "बिल्कुल! मैं आपको नया खाता खोलने में मदद करूंगा।", "suggestedCustomerLang": "बिल्कुल! मदद करूंगा।", "processStep": 1},
            {"suggestedHindi": "Aadhaar और PAN दोनों documents जमा करें।", "suggestedCustomerLang": "Documents जमा करें।", "processStep": 2},
            {"suggestedHindi": "Savings Account minimum balance ₹500 है।", "suggestedCustomerLang": "Minimum balance ₹500 है।", "processStep": 2},
            {"suggestedHindi": "Form भरने में मदद करूंगा।", "suggestedCustomerLang": "मदद करूंगा।", "processStep": 3},
            {"suggestedHindi": "खाता 24 घंटे में active होगा।", "suggestedCustomerLang": "24 घंटे में active होगा।", "processStep": 4}
        ]
        resp = demo_responses[max(0, min(demo_id, 4))]

        await self.send_to_staff(
            session_token,
            "ai_suggestion_ready",
            {
                "suggested_hindi": resp["suggestedHindi"],
                "suggested_customer_lang": resp["suggestedCustomerLang"],
                "intent": "account_opening",
                "process_triggered": True,
                "current_step": resp["processStep"],
                "is_demo": True
            }
        )

    async def _trigger_staff_input_if_needed(
        self,
        token_number: str,
        staff_text: str,
    ) -> None:
        """
        Detect PII keywords spoken by staff in Hindi/Hinglish to trigger automated inputs.
        """
        if not staff_text:
            return

        lower = staff_text.lower()

        _STAFF_KEYWORD_MAP = [
            (
                ["aadhar", "aadhaar", "adhar", "aadhar number", "aadhaar number",
                 "aadhar batao", "aadhaar de", "aadhar card", "aadhaar card",
                 "aadhar card number", "आधार", "आधार कार्ड", "आधार नंबर"],
                "aadhaar",
                "Aadhaar Number",
                "आधार नंबर दर्ज करें",
            ),
            (
                ["pan", "pan card", "pan number", "pan batao", "pan card number",
                 "पैन", "पैन कार्ड", "पैन नंबर"],
                "pan",
                "PAN Number",
                "PAN नंबर दर्ज करें",
            ),
            (
                ["account number", "account no", "account batao", "khata number",
                 "khata no", "khata de", "bank account",
                 "खाता नंबर", "खाते का नंबर", "खाता नं.",
                 "अकाउंट", "अकाउंट नंबर", "अकाउंट नं.",
                 "बैंक अकाउंट", "अकाउंट बताओ"],
                "account_number",
                "Account Number",
                "खाता नंबर दर्ज करें",
            ),
            (
                ["dob", "date of birth", "janm tithi", "janam tithi", "d.o.b",
                 "जन्म तिथि", "जन्म दिनांक", "janam din"],
                "dob",
                "Date of Birth",
                "जन्म तिथि दर्ज करें (DD/MM/YYYY)",
            ),
            (
                ["mobile", "mobile number", "phone number", "number batao",
                 "phone batao", "mobile no", "मोबाइल", "मोबाइल नंबर", "फ़ोन नंबर"],
                "phone",
                "Mobile Number",
                "मोबाइल नंबर दर्ज करें",
            ),
            (
                ["ifsc", "ifsc code"],
                "ifsc",
                "IFSC Code",
                "IFSC कोड दर्ज करें",
            ),
        ]

        for keywords, field_type, field_label, field_label_customer in _STAFF_KEYWORD_MAP:
            if any(kw in lower for kw in keywords):
                try:
                    await self.broadcast_input_request(
                        token_number=token_number,
                        field_type=field_type,
                        field_label=field_label,
                        field_label_customer=field_label_customer,
                        request_id=str(uuid.uuid4())[:8],
                    )
                    logger.info(
                        "Staff input trigger fired | token=%s | field=%s | text='%s'",
                        token_number, field_type, staff_text[:60],
                    )
                except Exception as exc:
                    logger.warning(
                        "_trigger_staff_input_if_needed failed | token=%s | %s",
                        token_number, exc,
                    )
                break

    async def _update_suggestion_analytics(
        self,
        session_id: int,
        event_type: str,
        use_suggestion: bool,
    ) -> None:
        """
        Increment analytics_daily counters for suggestion usage.
        """
        try:
            from models import AnalyticsDaily, Session

            async with await self._get_db() as db:
                session_result = await db.execute(
                    select(Session).where(Session.id == session_id)
                )
                session_obj = session_result.scalar_one_or_none()
                if not session_obj:
                    return

                today = datetime.now(timezone.utc).date()
                analytics_result = await db.execute(
                    select(AnalyticsDaily).where(
                        AnalyticsDaily.branch_id == session_obj.branch_id,
                        AnalyticsDaily.date == today,
                    )
                )
                analytics = analytics_result.scalar_one_or_none()
                if not analytics:
                    return

                if event_type == "staff_approved_response" and use_suggestion:
                    analytics.ai_suggestion_used = (
                        analytics.ai_suggestion_used or 0
                    ) + 1
                elif event_type == "staff_edited_response":
                    analytics.ai_suggestion_edited = (
                        analytics.ai_suggestion_edited or 0
                    ) + 1
                else:
                    analytics.ai_suggestion_ignored = (
                        analytics.ai_suggestion_ignored or 0
                    ) + 1

                await db.commit()

        except Exception as exc:
            logger.warning("Analytics update failed: %s", exc)

    async def send_verification_time_message(
        self,
        token_number: str,
        intent: str,
        lang_code: str,
        session_id: Optional[int] = None,
    ) -> None:
        """
        Send processing/verification time message to customer based on intent.
        """
        try:
            from services.session_navigator import VERIFICATION_TIME_MAP

            time_map = VERIFICATION_TIME_MAP.get(intent.lower())
            if not time_map:
                return

            short_lang = lang_code.split("-")[0].lower() if lang_code else "hi"
            time_text = time_map.get(short_lang, time_map.get("en", time_map.get("hi", "")))
            if not time_text:
                return

            await self.send_to_customer(
                token_number,
                "staff_message",
                {
                    "text": time_text,
                    "language_code": short_lang,
                    "is_verification_time": True,
                },
            )

            try:
                from services.ai_service import ai_service
                tts_result = await ai_service.generate_tts(
                    text=time_text,
                    language_code=lang_code or "hi",
                    session_id=session_id,
                )
                await self.broadcast_audio(
                    token_number=token_number,
                    audio_url=tts_result.audio_url,
                    duration_seconds=tts_result.duration_seconds,
                    response_text=time_text,
                )
            except Exception as tts_exc:
                logger.warning(
                    "Verification time TTS failed (text was sent) | token=%s | %s",
                    token_number, tts_exc,
                )

            logger.info(
                "Verification time message sent | token=%s | intent=%s | time=%s",
                token_number, intent, time_map.get("time", "?"),
            )
        except Exception as exc:
            logger.warning(
                "send_verification_time_message failed | token=%s | %s",
                token_number, exc,
            )

    async def _handle_send_to_saral_form(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Staff clicks "Send to Form Verification" in InfoBoard.
        Fetches latest collected_data + intent + language from DB
        and pushes saral_form_trigger WS event to the customer panel.
        Customer panel navigates to /saral-form without ending the session.
        """
        payload = data.get("data", data)
        session_id: Optional[int] = payload.get("session_id")

        if not session_id:
            await self.send_error(
                token_number, "staff", "invalid_payload",
                "session_id is required for send_to_saral_form."
            )
            return

        collected_data: dict = {}
        intent: str = "general"
        language_code: str = "hi"

        try:
            async with await self._get_db() as db:
                from models import Session as SessionModel
                result = await db.execute(
                    select(SessionModel).where(SessionModel.id == session_id)
                )
                session_obj = result.scalar_one_or_none()
                if session_obj:
                    collected_data = session_obj.collected_data or {}
                    intent = str(session_obj.intent_detected or "general")
                    language_code = session_obj.customer_language_code or "hi"
        except Exception as exc:
            logger.warning(
                "_handle_send_to_saral_form DB read failed | token=%s | %s",
                token_number, exc,
            )

        # ── Step 1: Send warm pre-form message to customer (like a greeting) ────────
        # Pick message in customer's language, fall back to Hindi
        short_lang = language_code.split("-")[0].lower() if language_code else "hi"
        PRE_FORM_MESSAGES = {
            "hi": (
                "🎉 आपकी सारी जानकारी सफलतापूर्वक इकट्ठा की गई है! "
                "अब आपकी वेरिफिकेशन प्रक्रिया शुरू होने वाली है। "
                "हम आपको एक सरल फॉर्म भेज रहे हैं जिसमें आप AI द्वारा भरी गई जानकारी को देख सकते हैं, "
                "ज़रूरत होने पर सुधार सकते हैं और अंत में अपना हस्ताक्षर कर सकते हैं। "
                "कृपया नीचे फॉर्म भरें — इसमें कुछ ही मिनट लगेंगे।"
            ),
            "mr": (
                "तुमची सर्व माहिती यशस्वीरित्या गोळा केली आहे! "
                "आता तुमची पडताळणी प्रक्रिया सुरू होणार आहे। "
                "आम्ही तुम्हाला एक सरळ फॉर्म पाठवत आहोत — कृपया ते भरा आणि सही करा।"
            ),
            "ta": (
                "🎉 உங்கள் தகவல்கள் வெற்றிகரமாக சேகரிக்கப்பட்டது! "
                "உங்கள் சரிபார்ப்பு விரைவில் தொடங்கக்கிருக்கிறது. "
                "தயவுசெய்து கீழே உள்ள படிவத்தை பூர்த்துங்கள்."
            ),
            "te": (
                "🎉 మీ సమాచారం విజయవంతంగా సంగ్రహించబడింది! "
                "ఇప్పుడు మీ ధ్రువీకరణ ప్రక్రియ ప్రారంభమవుతుంది. "
                "దయచేసి క్రింది ఫార్మ్ పూర్తి చేయండి."
            ),
            "bn": (
                "🎉 আপনার সমস্ত তথ্য সফলভাবে সংগ্রহ করা হয়েছে! "
                "এখন আপনার যাচাই প্রক্রিয়া শুরু হবে। "
                "দয়া করে নিচের ফর্মটি পূরণ করুন।"
            ),
            "gu": (
                "🎉 તમારી બધી માહિતી સફળતાપૂર્વક એકત્ર કરવામાં આવી છે! "
                "હવે તમારી ચકાસણી પ્રક્રિયા શરૂ થવાની છે। "
                "અમે તમને એક સરળ ફોર્મ મોકલી રહ્યા છીએ — કૃપા કરીને ભરો અને સહી કરો।"
            ),
            "ml": (
                "🎉 നിങ്ങളുടെ എല്ലാ വിവരങ്ങളും വിജയകരമായി ശേഖരിച്ചു! "
                "ഇപ്പോൾ നിങ്ങളുടെ സ്ഥിരീകരണ പ്രക്രിയ ആരംഭിക്കാൻ പോകുന്നു. "
                "ദയവായി താഴെയുള്ള ഫോം പൂരിപ്പിക്കുക."
            ),
            "kn": (
                "🎉 ನಿಮ್ಮ ಎಲ್ಲಾ ಮಾಹಿತಿಯನ್ನು ಯಶಸ್ವಿಯಾಗಿ ಸಂಗ್ರಹಿಸಲಾಗಿದೆ! "
                "ಈಗ ನಿಮ್ಮ ಪರಿಶೀಲನೆ ಪ್ರಕ್ರಿಯೆ ಪ್ರಾರಂಭವಾಗಲಿದೆ. "
                "ದಯವಿಟ್ಟು ಕೆಳಗಿನ ಫಾರ್ಮ್ ಭರ್ತಿ ಮಾಡಿ."
            ),
            "or": (
                "🎉 ଆପଣଙ୍କ ସମସ୍ତ ତଥ୍ୟ ସଫଳତାର ସହ ସଂଗ୍ରହ ହୋଇଛି! "
                "ଏବେ ଆପଣଙ୍କ ଯାଞ୍ଚ ପ୍ରକ୍ରିୟା ଆରମ୍ଭ ହେବ। "
                "ଦୟାକରି ନିମ୍ନରେ ଥିବା ଫର୍ମ ପୂରଣ କରନ୍ତୁ।"
            ),
            "pa": (
                "🎉 ਤੁਹਾਡੀ ਸਾਰੀ ਜਾਣਕਾਰੀ ਸਫਲਤਾਪੂਰਵਕ ਇਕੱਠੀ ਕੀਤੀ ਗਈ ਹੈ! "
                "ਹੁਣ ਤੁਹਾਡੀ ਤਸਦੀਕ ਪ੍ਰਕਿਰਿਆ ਸ਼ੁਰੂ ਹੋਣ ਵਾਲੀ ਹੈ। "
                "ਕਿਰਪਾ ਕਰਕੇ ਹੇਠਾਂ ਦਿੱਤਾ ਫਾਰਮ ਭਰੋ ਅਤੇ ਦਸਤਖਤ ਕਰੋ।"
            ),
            "en": (
                "🎉 All your information has been successfully collected! "
                "Your verification process is about to begin. "
                "We are sending you a simple form where you can review the details filled by our AI, "
                "make corrections if needed, and sign it. "
                "Please fill in the form below — it will only take a few minutes."
            ),
        }
        pre_msg = PRE_FORM_MESSAGES.get(short_lang, PRE_FORM_MESSAGES["hi"])

        # Send text message first (shows in chat bubble like greeting)
        await self.send_to_both(
            token_number,
            "staff_message",
            {
                "text": pre_msg,
                "language_code": short_lang,
                "is_saral_intro": True,
            },
        )

        # Generate TTS audio so it plays aloud (same pattern as greeting)
        tts_sleep = 4.0  # default pause if TTS fails
        try:
            from services.ai_service import ai_service
            tts_result = await ai_service.generate_tts(
                text=pre_msg,
                language_code=language_code or "hi",
                session_id=session_id,
            )
            await self.broadcast_audio(
                token_number=token_number,
                audio_url=tts_result.audio_url,
                duration_seconds=tts_result.duration_seconds,
                response_text=pre_msg,
            )
            # audio_ready fires setStaffMessage — message now visible in chat
            tts_sleep = min(tts_result.duration_seconds or 4.0, 7.0)
        except Exception as tts_exc:
            logger.warning(
                "Saral intro TTS failed (text was sent) | token=%s | %s",
                token_number, tts_exc,
            )
            # Fallback: send audio_ready with no audio so the pending
            # staff_message text is immediately shown in the chat bubble
            await self.send_to_both(
                token_number,
                "audio_ready",
                {
                    "audio_url": None,
                    "duration_seconds": 0,
                    "staff_response": pre_msg,
                },
            )

        # Wait for customer to read/hear the full message before form opens
        import asyncio
        await asyncio.sleep(tts_sleep)

        # ── Step 2: Now open the form on customer panel ────────────────────────
        # nav_delay_ms tells the frontend exactly how long to stay on LiveSessionPage
        # after receiving saral_form_trigger, so the customer finishes reading/hearing
        # the Hindi greeting message before being navigated to /saral-form.
        # We add a 2s buffer on top of tts_sleep (the time we already waited above).
        nav_delay_ms = max(int(tts_sleep * 1000) + 2000, 4000)
        await self.send_to_customer(
            token_number,
            "saral_form_trigger",
            {
                "token_number":   token_number,
                "session_id":     session_id,
                "collected_data": collected_data,
                "intent":         intent,
                "language_code":  language_code,
                "nav_delay_ms":   nav_delay_ms,
                "message":        "Staff has sent your form for verification.",
            },
        )

        logger.info(
            "saral_form_trigger sent to customer | token=%s | session=%s | intent=%s",
            token_number, session_id, intent,
        )
