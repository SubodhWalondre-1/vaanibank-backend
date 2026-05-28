"""
VaaniBank AI — WebSocket Audio Pipeline Mixin
PSBs Hackathon 2026 | Team Vectora
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import select, update

from websocket.helpers import _event, _safe_send

logger = logging.getLogger("vaanibank.websocket.audio_pipeline")


class AudioPipelineMixin:
    """
    Mixin for ConnectionManager handling streaming PCM audio chunks,
    VAD snapshot generation, rate-limited partial STT and full AI pipeline.
    """

    async def _handle_start_speaking(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Customer pressed the mic button.

        Registers language + session_id for this streaming session and
        initialises the per-token AudioStreamSession buffer.
        Sends customer_speaking → staff immediately.
        """
        from websocket.audio_streamer import AudioStreamSession

        payload = data.get("data", data)
        lang_code: str = payload.get("lang_code", "hi")
        session_id: Optional[int] = payload.get("session_id")

        # Initialise (or reset) the streaming session for this token
        self._audio_buffers[token_number] = []
        self._audio_lang[token_number] = lang_code
        self._audio_session_id[token_number] = session_id
        self._partial_stt_running[token_number] = False
        self._last_partial_ts[token_number] = 0.0

        # Store the full AudioStreamSession object for VAD / rate-limiting
        if not hasattr(self, "_stream_sessions"):
            self._stream_sessions: Dict[str, Any] = {}
        self._stream_sessions[token_number] = AudioStreamSession(
            lang_code=lang_code,
            session_id=session_id,
        )

        await self.broadcast_customer_speaking(token_number)
        logger.info(
            "Streaming session started | token=%s | lang=%s | db_session=%s",
            token_number, lang_code, session_id,
        )

    async def handle_customer_audio_chunk(
        self,
        token_number: str,
        raw_bytes: bytes,
    ) -> None:
        """
        Handle an incoming binary WebSocket frame (raw Float32 PCM audio).
        """
        if not hasattr(self, "_stream_sessions"):
            self._stream_sessions: Dict[str, Any] = {}

        session = self._stream_sessions.get(token_number)
        if session is None:
            # start_speaking not received yet — auto-init with defaults
            from websocket.audio_streamer import AudioStreamSession
            lang_code = self._audio_lang.get(token_number, "hi")
            session_id = self._audio_session_id.get(token_number)
            session = AudioStreamSession(lang_code=lang_code, session_id=session_id)
            self._stream_sessions[token_number] = session

        # Append raw PCM bytes to the buffer
        session.append_chunk(raw_bytes)

        # Notify staff on first chunk only (avoids repeated events)
        if not session.first_chunk_notified:
            session.first_chunk_notified = True
            await self.broadcast_customer_speaking(token_number)

        # Fire partial-STT if rate-limit allows
        if session.should_run_partial():
            session.partial_running = True
            asyncio.create_task(
                self._run_partial_stt(token_number, session)
            )

    async def _run_partial_stt(
        self,
        token_number: str,
        session: Any,
    ) -> None:
        """
        Convert the currently accumulated PCM buffer → WAV → Whisper partial.
        """
        import time as _time
        try:
            wav_bytes = session.build_wav_snapshot()
            if not wav_bytes or len(wav_bytes) < 500:
                return

            from services.ai_service import ai_service
            result = await ai_service.transcribe(
                audio_bytes=wav_bytes,
                language_code=session.lang_code,
                session_id=session.session_id,
                skip_pii=True,  # partials don't need PII masking — speed matters
            )

            partial_text = result.text.strip()

            # Skip broadcast if text is empty or unchanged (dedup)
            if partial_text and partial_text != session.last_partial_text:
                session.last_partial_text = partial_text
                await self.broadcast_transcription_partial(
                    token_number=token_number,
                    text=partial_text,
                    language_code=session.lang_code,
                )
                logger.debug(
                    "Partial STT | token=%s | text=%s",
                    token_number, partial_text[:80],
                )

        except Exception as exc:
            logger.warning(
                "Partial STT failed (non-fatal) | token=%s | %s", token_number, exc
            )
        finally:
            import time as _time
            session.partial_running = False
            session.last_partial_at = _time.monotonic()

    async def _handle_stop_speaking(
        self,
        token_number: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Customer released the mic button.
        """
        if not hasattr(self, "_stream_sessions"):
            self._stream_sessions: Dict[str, Any] = {}

        session = self._stream_sessions.get(token_number)
        if session is None or session.final_triggered:
            logger.warning(
                "stop_speaking received but no active stream session | token=%s", token_number
            )
            return

        # Prevent double-firing (e.g. if silence timeout also fires)
        session.final_triggered = True

        payload = data.get("data", data)
        lang_code = session.lang_code
        session_id = session.session_id

        logger.info(
            "Streaming session stopped | token=%s | duration=%.1fs | bytes=%d",
            token_number,
            session.duration_seconds(),
            session.total_bytes(),
        )

        # Build final WAV from accumulated buffer
        wav_bytes = session.build_wav_snapshot()

        # Clean up streaming state
        self._stream_sessions.pop(token_number, None)
        self._audio_buffers.pop(token_number, None)
        self._partial_stt_running.pop(token_number, None)
        self._last_partial_ts.pop(token_number, None)

        if not wav_bytes or len(wav_bytes) < 500:
            logger.warning(
                "stop_speaking: buffer too small to transcribe | token=%s", token_number
            )
            return

        # Run the full AI pipeline in a background task so we don't block the WS loop
        asyncio.create_task(
            self._run_final_pipeline(token_number, wav_bytes, lang_code, session_id)
        )

    async def _run_final_pipeline(
        self,
        token_number: str,
        wav_bytes: bytes,
        lang_code: str,
        session_id: Optional[int],
    ) -> None:
        """
        Full AI pipeline on the final WAV buffer.
        """
        try:
            from database import AsyncSessionLocal
            from services.pipeline_orchestrator import run_transcription_pipeline

            async with AsyncSessionLocal() as db:
                await run_transcription_pipeline(
                    audio_bytes=wav_bytes,
                    session_id=session_id,
                    token_number=token_number,
                    language_code=lang_code,
                    exchange_number=None,  # auto-detect from DB
                    db=db,
                    source_label="customer",
                )

        except Exception as exc:
            logger.error(
                "Final pipeline failed after streaming | token=%s | %s", token_number, exc
            )
            # Notify staff that transcription failed (non-fatal)
            await self.send_to_staff(
                token_number,
                "transcription_partial",
                {
                    "text": "[Transcription failed — please ask customer to repeat]",
                    "is_final": True,
                    "language_code": lang_code,
                    "error": True,
                },
            )

    async def _generate_session_summary(
        self,
        session_id: int,
        token_number: str,
    ) -> Optional[str]:
        """
        LLM se bilingual summary generate karo + PDF banao.
        Returns pdf_url or None on failure.
        """
        from models import BilingualSummary, Branch, Exchange, Session, StaffMember
        from services.ai_service import ai_service
        from services.pdf_service import pdf_service

        async with await self._get_db() as db:
            result = await db.execute(select(Session).where(Session.id == session_id))
            session_obj = result.scalar_one_or_none()
            if not session_obj:
                return None

            branch_result = await db.execute(select(Branch).where(Branch.id == session_obj.branch_id))
            branch = branch_result.scalar_one_or_none()

            staff_result = await db.execute(select(StaffMember).where(StaffMember.id == session_obj.staff_id))
            staff = staff_result.scalar_one_or_none()

            exchange_result = await db.execute(
                select(Exchange)
                .where(Exchange.session_id == session_id)
                .order_by(Exchange.exchange_number)
            )
            exchanges = exchange_result.scalars().all()

            # Existing summary check
            summary_result = await db.execute(
                select(BilingualSummary).where(BilingualSummary.session_id == session_id)
            )
            summary_obj = summary_result.scalar_one_or_none()

        # Build conversation text
        conversation_text = "\n".join(
            f"Customer: {ex.customer_text_translated or ex.customer_text_original or ''}\n"
            f"Staff: {ex.staff_response_final or ex.staff_response_suggested or ''}"
            for ex in exchanges if ex.customer_text_original
        ).strip()

        customer_language = session_obj.customer_language or "Hindi"

        # Generate summary from LLM
        summary_data = {}
        if conversation_text:
            try:
                summary_prompt = (
                    f"Summarize this Union Bank of India branch conversation in both Hindi and "
                    f"{customer_language}.\n\nConversation:\n{conversation_text}\n\n"
                    f"Return JSON only (no markdown):\n"
                    f'{{"summary_hindi": ["..."], "summary_customer_lang": ["..."], '
                    f'"key_points_hindi": ["..."], "key_points_customer": ["..."], '
                    f'"next_steps_hindi": ["..."], "next_steps_customer": ["..."]}}'
                )
                llm_result = await ai_service.process_with_llm(
                    text=summary_prompt,
                    source_language=customer_language,
                )
                summary_data = json.loads(llm_result.raw_response)
            except Exception as exc:
                logger.warning("LLM summary generation failed | session=%d | %s", session_id, exc)
                summary_data = {
                    "summary_hindi": ["सत्र पूरा हुआ।"],
                    "summary_customer_lang": ["Session completed."],
                    "key_points_hindi": [], "key_points_customer": [],
                    "next_steps_hindi": [], "next_steps_customer": [],
                }
        else:
            summary_data = {
                "summary_hindi": ["ग्राहक से बातचीत हुई।"],
                "summary_customer_lang": ["Session completed."],
                "key_points_hindi": [], "key_points_customer": [],
                "next_steps_hindi": [], "next_steps_customer": [],
            }

        # Save/update summary in DB
        async with await self._get_db() as db:
            now = datetime.now(timezone.utc)
            if summary_obj is None:
                summary_obj = BilingualSummary(
                    session_id=session_id,
                    customer_language=customer_language,
                    summary_hindi=summary_data.get("summary_hindi", []),
                    summary_customer_lang=summary_data.get("summary_customer_lang", []),
                    key_points_hindi=summary_data.get("key_points_hindi", []),
                    key_points_customer=summary_data.get("key_points_customer", []),
                    next_steps_hindi=summary_data.get("next_steps_hindi", []),
                    next_steps_customer=summary_data.get("next_steps_customer", []),
                    generated_at=now,
                )
                db.add(summary_obj)
                await db.flush()
            else:
                await db.execute(
                    update(BilingualSummary)
                    .where(BilingualSummary.session_id == session_id)
                    .values(
                        summary_hindi=summary_data.get("summary_hindi", []),
                        summary_customer_lang=summary_data.get("summary_customer_lang", []),
                        key_points_hindi=summary_data.get("key_points_hindi", []),
                        key_points_customer=summary_data.get("key_points_customer", []),
                        next_steps_hindi=summary_data.get("next_steps_hindi", []),
                        next_steps_customer=summary_data.get("next_steps_customer", []),
                        generated_at=now,
                    )
                )
                await db.refresh(summary_obj)

            # Generate PDF
            pdf_url = pdf_service.generate_bilingual_summary(
                session_id=session_id,
                token_number=token_number,
                branch_name=branch.branch_name if branch else "Union Bank of India",
                staff_name=staff.full_name if staff else "Staff",
                customer_language=customer_language,
                intent_detected=session_obj.intent_detected,
                sentiment_overall=session_obj.sentiment_overall,
                started_at=session_obj.started_at,
                ended_at=session_obj.ended_at,
                duration_seconds=session_obj.duration_seconds,
                summary_hindi=summary_data.get("summary_hindi", []),
                summary_customer_lang=summary_data.get("summary_customer_lang", []),
                key_points_hindi=summary_data.get("key_points_hindi", []),
                key_points_customer=summary_data.get("key_points_customer", []),
                next_steps_hindi=summary_data.get("next_steps_hindi", []),
                next_steps_customer=summary_data.get("next_steps_customer", []),
                collected_data=session_obj.collected_data,
            )

            await db.execute(
                update(BilingualSummary)
                .where(BilingualSummary.session_id == session_id)
                .values(pdf_url=pdf_url, pdf_generated=True)
            )
            await db.commit()

        logger.info("Summary+PDF generated | session=%d | pdf=%s", session_id, pdf_url)
        return pdf_url
