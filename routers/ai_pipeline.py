"""
VaaniBank AI — AI Pipeline Router
PSBs Hackathon 2026 | Team Vectora

Endpoints:
  POST /stt/transcribe                — Staff-side: full AI pipeline (STT + LLM + WS broadcast)
  POST /stt/customer-transcribe       — Customer-side: public transcription (no auth)
  POST /stt/staff-transcribe          — Staff speech transcription (for "Speak to Customer")
  POST /stt/detect-language           — Detect language from audio
  POST /llm/process                   — Run LLM processing on text
  POST /llm/translate-staff-response  — Translate Hindi staff response to customer language
  POST /tts/generate                  — Generate TTS audio from text
  GET  /tts/audio/{filename}          — Serve generated audio file
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from core.exceptions import ResourceNotFoundError, STTError
from core.security import get_current_staff
from database import get_db
from models import Exchange, Session, SessionStatus, StaffMember
from schemas import (
    DetectLanguageResponse,
    LLMProcessRequest,
    LLMProcessResponse,
    StaffTranscribeResponse,
    TranslateStaffResponse,
    TranslateStaffResponseRequest,
    TranscribeResponse,
    TranslateToEnglishRequest,
    TranslateToEnglishResponse,
    TTSGenerateRequest,
    TTSGenerateResponse,
)
from services.ai_service import ai_service
from services.pipeline_orchestrator import run_transcription_pipeline
from websocket.manager import ws_manager

logger = logging.getLogger("vaanibank.ai_pipeline")

router = APIRouter(tags=["ai-pipeline"])


# POST /stt/transcribe

@router.post(
    "/stt/transcribe",
    response_model=TranscribeResponse,
    status_code=status.HTTP_200_OK,
    summary="Transcribe customer audio — STT with fallback chain",
)
async def transcribe_audio(
    audio: UploadFile = File(..., description="Customer audio file (WAV/OGG/MP3)"),
    token_number: str = Form(...),
    language_code: str = Form(default="hi"),
    session_id: int = Form(...),
    exchange_number: Optional[int] = Form(default=None),
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> TranscribeResponse:
    """
    Transcribe customer speech and run the full AI pipeline.

    Auth: Staff JWT required.
    Delegates to PipelineOrchestrator for STT → LLM → DB → WebSocket flow.
    """
    # Validate session is still active
    session_check = await db.execute(
        select(Session.status).where(Session.id == session_id)
    )
    session_status = session_check.scalar_one_or_none()
    if session_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    if session_status in (SessionStatus.completed, SessionStatus.abandoned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has already ended. Cannot accept new audio.",
        )

    audio_bytes = await audio.read()

    result = await run_transcription_pipeline(
        audio_bytes=audio_bytes,
        session_id=session_id,
        token_number=token_number,
        language_code=language_code,
        exchange_number=exchange_number,
        db=db,
        source_label="staff",
    )

    return TranscribeResponse(
        exchange_id=result.exchange_id,
        text_original=result.text_original,
        text_translated=result.text_translated,
        masked_text=result.masked_text,
        confidence=result.confidence,
        model_used=result.model_used,
        language_detected=result.language_detected,
        pii_detected=result.pii_detected,
        pii_types=result.pii_types,
        sentiment=result.sentiment,
        intent=result.intent,
        suggested_response_hindi=result.suggested_response_hindi,
        suggested_response_customer_lang=result.suggested_response_customer_lang,
        response_time_ms=result.response_time_ms,
    )


# POST /stt/customer-transcribe  (PUBLIC — no auth, for customer panel)

@router.post(
    "/stt/customer-transcribe",
    response_model=TranscribeResponse,
    status_code=status.HTTP_200_OK,
    summary="Transcribe customer audio — public endpoint for customer panel",
)
async def customer_transcribe_audio(
    audio: UploadFile = File(..., description="Customer audio file (WAV/OGG/MP3/WEBM)"),
    token_number: str = Form(...),
    language_code: str = Form(default="hi"),
    session_id: int = Form(...),
    exchange_number: Optional[int] = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> TranscribeResponse:
    """
    Public transcription endpoint for the customer panel.
    No JWT required — validates by checking the session exists.
    Delegates to PipelineOrchestrator for the shared pipeline.
    """
    # Validate session exists
    session_result = await db.execute(
        select(Session).where(
            Session.token_number == token_number,
            Session.id == session_id,
        )
    )
    session_obj = session_result.scalar_one_or_none()
    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{token_number}' not found.",
        )
    if session_obj.status in (SessionStatus.completed, SessionStatus.abandoned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has already ended.",
        )

    audio_bytes = await audio.read()

    # STT with graceful fallback (customer panel never sees 500s)
    try:
        result = await run_transcription_pipeline(
            audio_bytes=audio_bytes,
            session_id=session_id,
            token_number=token_number,
            language_code=language_code,
            exchange_number=exchange_number,
            db=db,
            source_label="customer",
        )
    except STTError as exc:
        logger.error("STT failed — sending graceful fallback | %s", exc)
        await ws_manager.send_to_staff(
            token_number,
            "transcription_ready",
            {
                "text_original": "...",
                "text_translated": "Audio unclear — please ask customer to repeat.",
                "confidence": 0.0,
                "sentiment": "calm",
                "intent": "general",
                "pii_detected": False,
                "stt_failed": True,
            },
        )
        return TranscribeResponse(
            exchange_id=0,
            text_original="[Audio unclear]",
            text_translated="[Audio unclear — please ask customer to repeat]",
            masked_text="",
            confidence=0.0,
            model_used="none",
            language_detected=language_code,
            pii_detected=False,
            pii_types=[],
            sentiment="calm",
            intent="general",
            suggested_response_hindi="कृपया ग्राहक से दोबारा बोलने को कहें।",
            suggested_response_customer_lang="Please speak again clearly.",
            response_time_ms=0,
        )

    # Smart Input Trigger (customer panel only)
    await _trigger_input_request_if_needed(
        token_number=token_number,
        text=result.text_original,
    )

    return TranscribeResponse(
        exchange_id=result.exchange_id,
        text_original=result.text_original,
        text_translated=result.text_translated,
        masked_text=result.masked_text,
        confidence=result.confidence,
        model_used=result.model_used,
        language_detected=result.language_detected,
        pii_detected=result.pii_detected,
        pii_types=result.pii_types,
        sentiment=result.sentiment,
        intent=result.intent,
        suggested_response_hindi=result.suggested_response_hindi,
        suggested_response_customer_lang=result.suggested_response_customer_lang,
        response_time_ms=result.response_time_ms,
    )


# POST /stt/staff-transcribe  (staff speak → STT only, no DB, no WS, no LLM)




@router.post(
    "/stt/staff-transcribe",
    status_code=status.HTTP_200_OK,
    summary="Transcribe staff speech only — no DB write, no WS, no LLM",
)
async def staff_transcribe_audio(
    audio: UploadFile = File(...),
    language_code: str = Form(default="hi"),
    current_staff: StaffMember = Depends(get_current_staff),
) -> dict:
    """
    Lightweight STT-only endpoint for the Staff Speak feature.
    Staff speaks in their language (Hindi/Marathi) and receives a transcript.
    Frontend handles translation and TTS calls separately.
    No Exchange saved, no WebSocket broadcast, no LLM call.
    """
    audio_bytes = await audio.read()
    # Strip BCP-47 suffix if frontend sends 'hi-IN' instead of 'hi'
    lang = language_code.split("-")[0].lower()

    try:
        stt_result = await ai_service.transcribe(
            audio_bytes=audio_bytes,
            language_code=lang,
            session_id=0,   # no session context needed
        )
    except STTError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"STT failed: {exc}",
        )

    return {
        "transcript": stt_result.text,
        "confidence": stt_result.confidence,
        "model_used": stt_result.model_used,
        "language_detected": stt_result.language_detected or lang,
    }


# POST /stt/detect-language

@router.post(
    "/stt/detect-language",
    response_model=DetectLanguageResponse,
    status_code=status.HTTP_200_OK,
    summary="Detect spoken language from a short audio clip",
)
async def detect_language(
    audio: UploadFile = File(...),
    current_staff: StaffMember = Depends(get_current_staff),
) -> DetectLanguageResponse:
    """Detect the language spoken in a short audio sample."""
    audio_bytes = await audio.read()
    result = await ai_service.detect_language(audio_bytes)

    return DetectLanguageResponse(
        language_code=result.language_code,
        language_name=result.language_name,
        confidence=result.confidence,
    )


# POST /llm/process

@router.post(
    "/llm/process",
    response_model=LLMProcessResponse,
    status_code=status.HTTP_200_OK,
    summary="Process text through Groq LLM — translate, intent, sentiment, suggest",
)
async def llm_process(
    body: LLMProcessRequest,
    current_staff: StaffMember = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
) -> LLMProcessResponse:
    """
    Run text through the Groq LLM banking pipeline.

    Useful for manual text input (offline mode / typed queries).
    Broadcasts suggestion via WebSocket if token_number is provided.
    """
    result = await ai_service.process_with_llm(
        text=body.text,
        source_language=body.source_language,
        conversation_history=body.conversation_history,
    )

    if body.token_number:
        await ws_manager.broadcast_suggestion(
            token_number=body.token_number,
            suggested_hindi=result.suggested_response_hindi,
            suggested_customer_lang=result.suggested_response_customer_lang,
            intent=result.intent,
            process_triggered=result.process_triggered,
        )

        if body.session_id and result.process_triggered:
            await _initialize_process_steps(
                session_id=body.session_id,
                token_number=body.token_number,
                intent_type=result.intent,
                customer_language_code=getattr(body, 'language_code', 'hi'),
                db=db,
            )

    return LLMProcessResponse(
        translation=result.translation,
        intent=result.intent,
        intent_confidence=result.intent_confidence,
        sentiment=result.sentiment,
        sentiment_score=result.sentiment_score,
        suggested_response_hindi=result.suggested_response_hindi,
        suggested_response_customer_lang=result.suggested_response_customer_lang,
        banking_terms_detected=result.banking_terms_detected,
        process_triggered=result.process_triggered,
    )


# POST /llm/translate-staff-response

@router.post(
    "/llm/translate-staff-response",
    response_model=TranslateStaffResponse,
    status_code=status.HTTP_200_OK,
    summary="Translate a Hindi staff response to the customer language",
)
async def translate_staff_response(
    body: TranslateStaffResponseRequest,
    current_staff: StaffMember = Depends(get_current_staff),
) -> TranslateStaffResponse:
    """
    Translate a staff-written Hindi response into the customer's language.

    Used when staff writes their own response (Own button) and needs it
    translated before TTS generation.
    """
    translated = await ai_service.translate_text(
        text=body.text,
        target_language_code=body.target_language_code,
        source_language_code="hi",
    )

    if not translated:
        translated = body.text

    return TranslateStaffResponse(
        original_text=body.text,
        translated_text=translated,
        target_language=body.target_language,
        target_language_code=body.target_language_code,
    )


# POST /tts/generate

@router.post(
    "/tts/generate",
    response_model=TTSGenerateResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate TTS audio from text",
)
async def generate_tts(
    body: TTSGenerateRequest,
    current_staff: StaffMember = Depends(get_current_staff),
) -> TTSGenerateResponse:
    """
    Convert text to speech using Sarvam Bulbul v3 (with MMS fallback).

    - Checks Redis TTS cache first (7-day TTL).
    - Broadcasts audio_ready to the customer WebSocket if token_number given.
    """
    from core.exceptions import TTSError as _TTSError

    result = await ai_service.generate_tts(
        text=body.text,
        language_code=body.language_code,
        session_id=body.session_id,
    )

    if body.token_number:
        # First send translated text to customer panel (appears before audio)
        if body.text:
            await ws_manager.send_to_customer(
                body.token_number,
                "staff_message",
                {
                    "text": body.text,
                    "language_code": body.language_code,
                },
            )
        # Then send audio (also pass response_text so audio_ready event includes text)
        await ws_manager.broadcast_audio(
            token_number=body.token_number,
            audio_url=result.audio_url,
            duration_seconds=result.duration_seconds,
            response_text=body.text or "",
        )

    return TTSGenerateResponse(
        audio_url=result.audio_url,
        duration_seconds=result.duration_seconds,
        model_used=result.model_used,
        from_cache=result.from_cache,
        language_code=body.language_code,
    )


# GET /tts/audio/{filename}

@router.get(
    "/tts/audio/{filename}",
    summary="Serve generated audio file",
    response_class=FileResponse,
)
async def serve_audio(filename: str) -> FileResponse:
    """Serve a TTS-generated audio file from local storage."""
    audio_path = Path(settings.AUDIO_STORAGE_PATH) / filename
    if not audio_path.exists() or not audio_path.is_file():
        raise ResourceNotFoundError(resource="Audio file", identifier=filename)

    return FileResponse(
        path=str(audio_path),
        media_type="audio/wav",
        filename=filename,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


# POST /llm/translate-to-english




@router.post(
    "/llm/translate-to-english",
    response_model=TranslateToEnglishResponse,
    status_code=status.HTTP_200_OK,
    summary="Translate any Indian language text to English using Groq",
)
async def translate_to_english(
    body: TranslateToEnglishRequest,
    current_staff: StaffMember = Depends(get_current_staff),
) -> TranslateToEnglishResponse:
    """
    For staff language toggle — Hindi/regional text → English.
    Delegates to ai_service for async, connection-pooled Groq calls.
    """
    result = await ai_service.translate_to_english(body.text)
    return TranslateToEnglishResponse(english_text=result)


# INTERNAL HELPERS — extracted to routers/_pipeline_helpers.py
# Re-export for backward compatibility with callers inside this file
from routers._pipeline_helpers import (
    initialize_process_steps as _initialize_process_steps,
    lang_code_to_name as _lang_code_to_name,
    trigger_input_request_if_needed as _trigger_input_request_if_needed,
    INPUT_KEYWORD_MAP as _INPUT_KEYWORD_MAP,
)