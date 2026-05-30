"""
VaaniBank AI — Speech-to-Text Service (STT)
PSBs Hackathon 2026 | Team Vectora

Single Responsibility: Audio → Text transcription with a multi-engine
fallback chain and per-engine telemetry.

Architecture:
    Audio bytes → format validation → WAV conversion → fallback chain → PII scan → result

Fallback chain (configurable at runtime via settings_service):
    1. Sarvam Saarika 2.5       — Primary, Indian-language optimised
    2. Groq Whisper Large-v3-T  — Fast GPU inference via LPU
    3. Reverie RevUp BFSI       — Banking-domain trained, all 10 langs

Usage:
    from services.stt_service import stt_service

    result = await stt_service.transcribe(
        audio_bytes=raw_audio,
        language_code="mr",
        session_id=42,
    )
    # result.text, result.confidence, result.metrics are available
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from groq import AsyncGroq

from config import settings
from core.exceptions import STTError
from core.language import (
    REVERIE_LANG_MAP,
    SARVAM_LANG_MAP,
    SARVAM_LANGUAGES,
)

logger = logging.getLogger("vaanibank.stt")


# CONSTANTS

CONFIDENCE_THRESHOLD: float = 0.6
"""Minimum confidence score to accept a transcription from an STT engine."""

HTTP_TIMEOUT_SECONDS: float = 30.0
"""Maximum time to wait for any single STT HTTP request."""

FFMPEG_TIMEOUT_SECONDS: float = 15.0
"""Maximum time to wait for ffmpeg audio conversion."""

MIN_AUDIO_SIZE_BYTES: int = 100
"""Reject audio payloads smaller than this — likely noise or empty."""

MAX_AUDIO_SIZE_BYTES: int = 25 * 1024 * 1024  # 25 MB
"""Reject audio payloads larger than this to prevent memory abuse."""


# ENUMS & DATA CLASSES

class STTEngine(str, enum.Enum):
    """Identifiers for each STT provider in the fallback chain."""

    SARVAM = "sarvam_saarika_2.5"
    GROQ_WHISPER = "groq_whisper"
    REVERIE = "reverie_revup_bfsi"


# Map from settings_service string keys → enum values
_ENGINE_KEY_TO_ENUM: Dict[str, STTEngine] = {
    "sarvam_saarika_2.5": STTEngine.SARVAM,
    "groq_whisper": STTEngine.GROQ_WHISPER,
    "reverie": STTEngine.REVERIE,
}


@dataclass(frozen=True)
class EngineAttempt:
    """Telemetry for a single STT engine attempt."""

    engine: str
    success: bool
    latency_ms: float
    error: Optional[str] = None
    confidence: float = 0.0


@dataclass(frozen=True)
class STTMetrics:
    """Aggregated telemetry for the full STT fallback chain execution."""

    total_latency_ms: float
    engine_used: str
    engines_attempted: int
    attempts: Tuple[EngineAttempt, ...] = ()


@dataclass
class TranscriptionResult:
    """Output of a successful transcription."""

    text: str
    confidence: float
    model_used: str
    language_detected: str
    pii_detected: bool = False
    pii_types: List[str] = field(default_factory=list)
    masked_text: str = ""
    metrics: Optional[STTMetrics] = None


# AUDIO FORMAT DETECTION

class AudioFormat(str, enum.Enum):
    """Detectable audio container formats from file header bytes."""

    WAV = "wav"
    OGG = "ogg"
    WEBM = "webm"
    UNKNOWN = "unknown"


def detect_audio_format(header_bytes: bytes) -> AudioFormat:
    """
    Identify audio container format from the first 12 bytes.

    Returns:
        AudioFormat enum value.
    """
    if len(header_bytes) < 4:
        return AudioFormat.UNKNOWN

    if header_bytes[:4] == b"RIFF":
        return AudioFormat.WAV
    if header_bytes[:4] == b"OggS":
        return AudioFormat.OGG
    if header_bytes[:4] == b"\x1aE\xdf\xa3":
        return AudioFormat.WEBM
    if b"webm" in header_bytes[4:12]:
        return AudioFormat.WEBM

    return AudioFormat.UNKNOWN


# AUDIO CONVERTER

_ffmpeg_available: Optional[bool] = None


def check_ffmpeg_available() -> bool:
    """
    Check if ffmpeg binary is accessible on the system PATH.

    Caches the result after the first check to avoid repeated lookups.
    """
    global _ffmpeg_available
    if _ffmpeg_available is not None:
        return _ffmpeg_available

    _ffmpeg_available = shutil.which("ffmpeg") is not None
    if _ffmpeg_available:
        logger.info("ffmpeg binary found — audio conversion enabled")
    else:
        logger.warning(
            "ffmpeg binary NOT found on PATH — "
            "audio conversion disabled, raw bytes will be sent to STT engines"
        )
    return _ffmpeg_available


async def convert_to_wav(audio_bytes: bytes) -> bytes:
    """
    Convert incoming audio (WebM/Opus/OGG/MP3) to 16 kHz mono WAV.

    If the audio is already WAV format, returns it unchanged.
    If ffmpeg is unavailable, returns the original bytes and logs a warning.

    Args:
        audio_bytes: Raw audio bytes from the client.

    Returns:
        WAV-formatted audio bytes (16 kHz, mono, s16le).
    """
    import aiofiles

    audio_format = detect_audio_format(audio_bytes[:12])

    if audio_format == AudioFormat.WAV:
        return audio_bytes

    if not check_ffmpeg_available():
        logger.warning(
            "Skipping audio conversion (ffmpeg unavailable) — "
            "sending raw %s audio (%d bytes) to STT engine",
            audio_format.value,
            len(audio_bytes),
        )
        return audio_bytes

    input_format = audio_format.value if audio_format != AudioFormat.UNKNOWN else "webm"
    source_extension = f".{input_format}"

    # Create temp files for ffmpeg I/O
    source_fd, source_path = await asyncio.to_thread(
        tempfile.mkstemp, suffix=source_extension
    )
    await asyncio.to_thread(os.close, source_fd)
    destination_path = source_path.rsplit(".", 1)[0] + ".wav"

    try:
        async with aiofiles.open(source_path, "wb") as source_file:
            await source_file.write(audio_bytes)

        ffmpeg_command = [
            "ffmpeg", "-y",
            "-f", input_format,
            "-i", source_path,
            "-ar", "16000",
            "-ac", "1",
            "-sample_fmt", "s16",
            "-avoid_negative_ts", "make_zero",
            "-strict", "-2",
            "-f", "wav",
            destination_path,
        ]

        process = await asyncio.create_subprocess_exec(
            *ffmpeg_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )

        if process.returncode != 0:
            error_output = stderr.decode(errors="replace")[:500]
            logger.error(
                "ffmpeg conversion failed | return_code=%d | stderr=%s",
                process.returncode,
                error_output,
            )
            return audio_bytes

        async with aiofiles.open(destination_path, "rb") as wav_file:
            wav_bytes = await wav_file.read()

        logger.info(
            "Audio converted to WAV | format=%s | source_bytes=%d | wav_bytes=%d",
            input_format,
            len(audio_bytes),
            len(wav_bytes),
        )
        return wav_bytes

    except asyncio.TimeoutError:
        logger.error("ffmpeg conversion timed out after %.0fs", FFMPEG_TIMEOUT_SECONDS)
        return audio_bytes

    except Exception as exc:
        logger.error("Audio conversion failed — using original bytes | error=%s", exc)
        return audio_bytes

    finally:
        for path in (source_path, destination_path):
            try:
                await asyncio.to_thread(os.unlink, path)
            except OSError:
                pass


# STT ENGINE IMPLEMENTATIONS

async def transcribe_with_sarvam(
    http_client: httpx.AsyncClient,
    audio_bytes: bytes,
    language_code: str,
) -> Tuple[str, float, str]:
    """
    Call Sarvam Saarika 2.5 STT API.

    Args:
        http_client:   Shared async HTTP client.
        audio_bytes:   WAV audio bytes.
        language_code: ISO 639-1 code (e.g. "mr", "ta").

    Returns:
        Tuple of (transcript, confidence, model_name).

    Raises:
        STTError: If the API returns a non-2xx status or empty transcript.
    """
    api_key = settings.SARVAM_API_KEY
    if not api_key or api_key.strip() in ("", "<your-sarvam-key>"):
        raise STTError(message="Sarvam API key not configured.")

    short_code = language_code.split("-")[0].lower()
    if short_code not in SARVAM_LANGUAGES:
        raise STTError(
            message=f"Language '{short_code}' not supported by Sarvam STT.",
            model_attempted=STTEngine.SARVAM.value,
        )

    bcp47_code = SARVAM_LANG_MAP.get(short_code, "hi-IN")

    # Detect MIME type from header
    is_webm = (
        audio_bytes[:4] == b"\x1aE\xdf\xa3"
        or audio_bytes[4:8] == b"webm"
    )
    mime_type = "audio/webm" if is_webm else "audio/wav"
    filename = "audio.webm" if is_webm else "audio.wav"

    response = await http_client.post(
        settings.SARVAM_STT_URL,
        headers={"api-subscription-key": api_key},
        files={"file": (filename, audio_bytes, mime_type)},
        data={"language_code": bcp47_code, "with_timestamps": "false"},
    )

    if response.status_code >= 400:
        logger.error(
            "Sarvam STT HTTP error | status=%d | body=%s",
            response.status_code,
            response.text[:300],
        )
    response.raise_for_status()

    data = response.json()
    transcript = (data.get("transcript") or data.get("text", "")).strip()
    confidence = float(data.get("confidence", 0.0))

    if not transcript:
        raise STTError(
            message="Sarvam STT returned empty transcript.",
            model_attempted=STTEngine.SARVAM.value,
        )

    return transcript, confidence, STTEngine.SARVAM.value


async def transcribe_with_groq_whisper(
    audio_bytes: bytes,
    language_code: str,
    groq_sync_client: Any = None,
) -> Tuple[str, float, str]:
    """
    Call Groq Whisper Large-v3-Turbo STT API.

    Uses a synchronous Groq client wrapped in asyncio.to_thread
    because the Groq SDK audio API requires file-like objects.

    Args:
        audio_bytes:      WAV audio bytes.
        language_code:    ISO 639-1 code.
        groq_sync_client: Optional pre-initialised sync Groq client.

    Returns:
        Tuple of (transcript, confidence, model_name).

    Raises:
        STTError: If the API returns empty or fails.
    """
    import aiofiles

    temp_fd, temp_path = await asyncio.to_thread(
        tempfile.mkstemp, suffix=".wav"
    )
    await asyncio.to_thread(os.close, temp_fd)

    try:
        async with aiofiles.open(temp_path, "wb") as temp_file:
            await temp_file.write(audio_bytes)

        transcript = await asyncio.to_thread(
            _groq_whisper_sync_call,
            temp_path,
            language_code,
            groq_sync_client,
        )

        text = transcript.strip()
        if not text:
            raise STTError(
                message="Groq Whisper returned empty transcript.",
                model_attempted=STTEngine.GROQ_WHISPER.value,
            )

        # Whisper does not return a confidence score; use a fixed value
        return text, 0.90, STTEngine.GROQ_WHISPER.value

    finally:
        try:
            await asyncio.to_thread(os.unlink, temp_path)
        except OSError:
            pass


def _groq_whisper_sync_call(
    audio_path: str,
    language_code: str,
    groq_sync_client: Any = None,
) -> str:
    """
    Synchronous Groq Whisper transcription call.

    Designed to run inside asyncio.to_thread so the event loop
    is not blocked by file I/O or the Groq SDK.
    """
    if groq_sync_client is None:
        from groq import Groq
        groq_sync_client = Groq(api_key=settings.GROQ_API_KEY)

    short_code = language_code.split("-")[0].lower()
    # Odia: Whisper uses ISO 639-3 "ori" instead of ISO 639-1 "or"
    whisper_language = short_code if short_code != "or" else "ori"

    with open(audio_path, "rb") as audio_file:
        transcription = groq_sync_client.audio.transcriptions.create(
            file=("audio.wav", audio_file),
            model="whisper-large-v3-turbo",
            language=whisper_language,
            response_format="text",
        )

    return str(transcription).strip()


async def transcribe_with_reverie(
    http_client: httpx.AsyncClient,
    audio_bytes: bytes,
    language_code: str,
) -> Tuple[str, float, str]:
    """
    Call Reverie RevUp BFSI STT API — banking-domain trained model.

    Args:
        http_client:   Shared async HTTP client.
        audio_bytes:   WAV audio bytes.
        language_code: ISO 639-1 code.

    Returns:
        Tuple of (transcript, confidence, model_name).

    Raises:
        STTError: If credentials are missing, API fails, or empty transcript.
    """
    app_id = settings.REVERIE_APP_ID
    api_key = settings.REVERIE_API_KEY

    if not app_id or not api_key or app_id.strip() in ("", "your_reverie_app_id_here"):
        raise STTError(message="Reverie credentials not configured.")

    short_code = language_code.split("-")[0].lower()
    source_language = REVERIE_LANG_MAP.get(short_code, "hi")

    response = await http_client.post(
        "https://revapi.reverieinc.com/",
        headers={
            "REV-APP-ID": app_id,
            "REV-API-KEY": api_key,
            "REV-APPNAME": "stt_file",
            "src_lang": source_language,
            "domain": "bfsi",
        },
        files={
            "audio_file": ("audio.wav", audio_bytes, "audio/wav"),
        },
    )

    if response.status_code >= 400:
        logger.error(
            "Reverie STT HTTP error | status=%d | body=%s",
            response.status_code,
            response.text[:300],
        )
    response.raise_for_status()

    data = response.json()
    # Prefer display_text (punctuated) over raw text
    text = (data.get("display_text") or data.get("text") or "").strip()
    confidence = float(data.get("confidence", 0.0))

    if not text:
        raise STTError(
            message="Reverie STT returned empty transcript.",
            model_attempted=STTEngine.REVERIE.value,
        )

    return text, confidence, STTEngine.REVERIE.value


# STT SERVICE — Main Orchestrator

class STTService:
    """
    Central speech-to-text service with configurable fallback chain
    and per-engine latency telemetry.

    Usage:
        stt_service = STTService()
        result = await stt_service.transcribe(audio_bytes, "mr", session_id=42)
    """

    def __init__(self) -> None:
        self._http_client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
        self._groq_sync_client: Any = None

    # Public API

    async def transcribe(
        self,
        audio_bytes: bytes,
        language_code: str,
        session_id: Optional[int] = None,
        skip_pii: bool = False,
    ) -> TranscriptionResult:
        """
        Transcribe audio using the configured fallback chain.

        Steps:
            1. Validate input (size, format)
            2. Convert to 16 kHz mono WAV (if not already)
            3. Walk fallback chain: attempt each engine, track latency
            4. Apply PII detection on successful transcript
            5. Return result with metrics

        Args:
            audio_bytes:   Raw audio bytes from client.
            language_code: ISO 639-1 code (e.g. "mr", "ta", "hi").
            session_id:    Optional session ID for logging context.
            skip_pii:      Skip PII masking (used for streaming partials).

        Returns:
            TranscriptionResult with text, confidence, metrics.
        """
        pipeline_start = time.perf_counter()

        # Step 1: Validate input
        self._validate_audio_input(audio_bytes, language_code)

        # Normalise language code
        clean_language_code = language_code.split("-")[0].strip().lower()

        # Step 2: Convert to WAV
        wav_bytes = await convert_to_wav(audio_bytes)

        # Step 3: Build and execute fallback chain
        engine_chain = await self._build_engine_chain()
        text, confidence, model_used, attempts = await self._execute_fallback_chain(
            engine_chain=engine_chain,
            audio_bytes=wav_bytes,
            language_code=clean_language_code,
            session_id=session_id,
        )

        # Build metrics
        total_latency_ms = (time.perf_counter() - pipeline_start) * 1000
        metrics = STTMetrics(
            total_latency_ms=round(total_latency_ms, 2),
            engine_used=model_used or "none",
            engines_attempted=len(attempts),
            attempts=tuple(attempts),
        )

        # Handle total failure
        if text is None:
            logger.warning(
                "All STT engines failed | session_id=%s | engines_attempted=%d | "
                "total_latency_ms=%.1f",
                session_id,
                len(attempts),
                total_latency_ms,
            )
            return TranscriptionResult(
                text="[Voice message received — transcription unavailable]",
                confidence=0.0,
                model_used=model_used or "none",
                language_detected=clean_language_code,
                pii_detected=False,
                pii_types=[],
                masked_text="[Voice message received — transcription unavailable]",
                metrics=metrics,
            )

        # Step 4: PII detection
        if skip_pii:
            return TranscriptionResult(
                text=text,
                confidence=confidence,
                model_used=model_used,
                language_detected=clean_language_code,
                pii_detected=False,
                pii_types=[],
                masked_text=text,
                metrics=metrics,
            )

        from services.pii_service import pii_service
        pii_result = pii_service.detect_and_mask(text)

        logger.info(
            "STT pipeline complete | session_id=%s | engine=%s | confidence=%.2f | "
            "pii_types=%s | latency_ms=%.1f | engines_attempted=%d",
            session_id,
            model_used,
            confidence,
            pii_result.pii_types,
            total_latency_ms,
            len(attempts),
        )

        return TranscriptionResult(
            text=text,
            confidence=confidence,
            model_used=model_used,
            language_detected=clean_language_code,
            pii_detected=pii_result.pii_found,
            pii_types=pii_result.pii_types,
            masked_text=pii_result.masked_text if pii_result.pii_found else text,
            metrics=metrics,
        )

    # Input validation

    @staticmethod
    def _validate_audio_input(audio_bytes: bytes, language_code: str) -> None:
        """
        Validate audio payload before processing.

        Raises:
            STTError: If audio is too small, too large, or language is empty.
        """
        if not audio_bytes:
            raise STTError(message="Audio payload is empty.")

        if len(audio_bytes) < MIN_AUDIO_SIZE_BYTES:
            raise STTError(
                message=f"Audio payload too small ({len(audio_bytes)} bytes). "
                f"Minimum is {MIN_AUDIO_SIZE_BYTES} bytes."
            )

        if len(audio_bytes) > MAX_AUDIO_SIZE_BYTES:
            raise STTError(
                message=f"Audio payload too large ({len(audio_bytes)} bytes). "
                f"Maximum is {MAX_AUDIO_SIZE_BYTES // (1024 * 1024)} MB."
            )

        if not language_code or not language_code.strip():
            raise STTError(message="language_code is required for transcription.")

    # Engine chain builder

    async def _build_engine_chain(self) -> List[STTEngine]:
        """
        Build the ordered STT engine chain from dynamic settings.

        Falls back to the default chain [Sarvam, Groq, Reverie]
        if settings_service is unavailable.
        """
        default_chain = [STTEngine.SARVAM, STTEngine.GROQ_WHISPER, STTEngine.REVERIE]

        try:
            from services.settings_service import settings_service
            dynamic_settings = await settings_service.get_all_settings()

            engine_keys = [
                dynamic_settings.get("primary_stt", "sarvam_saarika_2.5"),
                dynamic_settings.get("fallback_stt_1", "groq_whisper"),
                dynamic_settings.get("fallback_stt_2", "reverie"),
            ]

            chain = []
            for key in engine_keys:
                if key and key != "none":
                    engine = _ENGINE_KEY_TO_ENUM.get(key)
                    if engine and engine not in chain:
                        chain.append(engine)

            return chain if chain else default_chain

        except Exception as exc:
            logger.debug(
                "settings_service unavailable — using default STT chain | error=%s",
                exc,
            )
            return default_chain

    # Fallback chain executor

    async def _execute_fallback_chain(
        self,
        engine_chain: List[STTEngine],
        audio_bytes: bytes,
        language_code: str,
        session_id: Optional[int],
    ) -> Tuple[Optional[str], float, str, List[EngineAttempt]]:
        """
        Walk the engine chain, trying each engine in order.

        Stops at the first engine that produces an accepted transcript
        (text is non-empty and confidence ≥ threshold, or confidence is
        not reported by the engine).

        Returns:
            (text, confidence, model_used, list_of_attempts)
        """
        attempts: List[EngineAttempt] = []
        final_text: Optional[str] = None
        final_confidence: float = 0.0
        final_model: str = ""

        for engine in engine_chain:
            if final_text is not None:
                break

            engine_start = time.perf_counter()
            try:
                text, confidence, model_name = await self._call_engine(
                    engine, audio_bytes, language_code
                )
                latency_ms = (time.perf_counter() - engine_start) * 1000

                # Evaluate confidence — if engine reports 0.0, it means
                # "confidence not available", NOT "zero confidence"
                effective_confidence = confidence if confidence > 0.0 else None

                if effective_confidence is not None and effective_confidence < CONFIDENCE_THRESHOLD:
                    logger.info(
                        "STT engine rejected (low confidence) | engine=%s | "
                        "confidence=%.2f | threshold=%.2f | latency_ms=%.1f",
                        engine.value,
                        confidence,
                        CONFIDENCE_THRESHOLD,
                        latency_ms,
                    )
                    attempts.append(EngineAttempt(
                        engine=engine.value,
                        success=False,
                        latency_ms=round(latency_ms, 2),
                        error=f"Confidence {confidence:.2f} below threshold {CONFIDENCE_THRESHOLD}",
                        confidence=confidence,
                    ))
                    continue

                # Accepted
                final_text = text
                final_confidence = confidence
                final_model = model_name
                attempts.append(EngineAttempt(
                    engine=engine.value,
                    success=True,
                    latency_ms=round(latency_ms, 2),
                    confidence=confidence,
                ))

                logger.info(
                    "STT engine accepted | engine=%s | confidence=%.2f | "
                    "latency_ms=%.1f | session_id=%s",
                    engine.value,
                    confidence,
                    latency_ms,
                    session_id,
                )

            except Exception as exc:
                latency_ms = (time.perf_counter() - engine_start) * 1000
                error_message = str(exc)[:200]
                logger.warning(
                    "STT engine failed | engine=%s | error=%s | "
                    "latency_ms=%.1f | session_id=%s",
                    engine.value,
                    error_message,
                    latency_ms,
                    session_id,
                )
                attempts.append(EngineAttempt(
                    engine=engine.value,
                    success=False,
                    latency_ms=round(latency_ms, 2),
                    error=error_message,
                ))

        return final_text, final_confidence, final_model, attempts

    # Engine dispatcher

    async def _call_engine(
        self,
        engine: STTEngine,
        audio_bytes: bytes,
        language_code: str,
    ) -> Tuple[str, float, str]:
        """
        Dispatch to the correct STT engine implementation.

        Args:
            engine:        Which STT engine to call.
            audio_bytes:   WAV audio bytes.
            language_code: ISO 639-1 code.

        Returns:
            Tuple of (transcript, confidence, model_name).
        """
        if engine == STTEngine.SARVAM:
            return await transcribe_with_sarvam(
                self._http_client, audio_bytes, language_code
            )
        elif engine == STTEngine.GROQ_WHISPER:
            return await transcribe_with_groq_whisper(
                audio_bytes, language_code, self._groq_sync_client
            )
        elif engine == STTEngine.REVERIE:
            return await transcribe_with_reverie(
                self._http_client, audio_bytes, language_code
            )
        else:
            raise STTError(message=f"Unknown STT engine: {engine}")


# MODULE-LEVEL SINGLETON

stt_service: STTService = STTService()
"""Pre-initialised STT service singleton. Import this across the application."""
