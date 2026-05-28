"""
VaaniBank AI — Streaming Audio Buffer & VAD
PSBs Hackathon 2026 | Team Vectora

Manages per-session PCM audio state for the streaming transcription pipeline.

Usage (inside ConnectionManager):
    from websocket.audio_streamer import AudioStreamSession, float32_bytes_to_wav

    session = AudioStreamSession(lang_code="hi", session_id=42)
    session.append_chunk(raw_bytes)
    wav_bytes = session.build_wav_snapshot()
"""

from __future__ import annotations

import struct
import time
import wave
import io
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("vaanibank.audio_streamer")

# ── Tuning constants ────────────────────────────────────────────────────────────
PCM_SAMPLE_RATE: int   = 16_000          # Hz — must match AudioContext on frontend
PCM_CHANNELS:   int   = 1               # mono
PCM_BYTES_PER_SAMPLE: int = 4           # Float32 = 4 bytes per sample

# Partial-STT fires at most once every N seconds while customer is speaking.
# 0.4s gives fast-enough updates without overloading Sarvam/Groq STT API.
PARTIAL_INTERVAL_SEC: float = 0.4

# After this many seconds with NO new chunks, backend considers speech complete
# (used as a fallback in case stop_speaking JSON event is delayed)
SILENCE_TIMEOUT_SEC: float  = 2.0

# Minimum accumulated bytes before we bother calling STT (≈ 0.5s of audio)
MIN_BYTES_FOR_STT: int = PCM_SAMPLE_RATE * PCM_BYTES_PER_SAMPLE // 2   # 32 000 bytes


# ══════════════════════════════════════════════════════════════════════════════
# AudioStreamSession  (one per active token_number that is speaking)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AudioStreamSession:
    """
    Holds all per-session streaming audio state.

    Attributes:
        lang_code       Language code for STT (e.g. "hi", "mr", "ta")
        session_id      DB session id — forwarded to ai_service.transcribe()
        chunks          Accumulated raw Float32 PCM byte buffers
        started_at      Epoch timestamp when first chunk arrived
        last_chunk_at   Epoch timestamp of most recent chunk (for silence detection)
        last_partial_at Epoch timestamp of last partial-STT call (rate-limiting)
        partial_running Whether a partial-STT coroutine is currently in flight
        first_chunk_notified  Whether customer_speaking was sent to staff yet
        final_triggered Whether the final full-pipeline has already been kicked off
        last_partial_text  Most recently received partial transcript (for dedup)
    """
    lang_code:              str
    session_id:             Optional[int]   = None
    chunks:                 List[bytes]     = field(default_factory=list)
    started_at:             float           = field(default_factory=time.monotonic)
    last_chunk_at:          float           = field(default_factory=time.monotonic)
    last_partial_at:        float           = 0.0
    partial_running:        bool            = False
    first_chunk_notified:   bool            = False
    final_triggered:        bool            = False
    last_partial_text:      str             = ""

    # ── Helpers ──────────────────────────────────────────────────────────────

    def append_chunk(self, raw_bytes: bytes) -> None:
        """Append a new PCM binary frame from the WebSocket."""
        self.chunks.append(raw_bytes)
        self.last_chunk_at = time.monotonic()

    def total_bytes(self) -> int:
        return sum(len(c) for c in self.chunks)

    def total_samples(self) -> int:
        return self.total_bytes() // PCM_BYTES_PER_SAMPLE

    def duration_seconds(self) -> float:
        return self.total_samples() / PCM_SAMPLE_RATE

    def is_silent(self) -> bool:
        """True if no new chunk has arrived for SILENCE_TIMEOUT_SEC."""
        return (time.monotonic() - self.last_chunk_at) >= SILENCE_TIMEOUT_SEC

    def should_run_partial(self) -> bool:
        """True if enough time has passed AND we have enough audio."""
        elapsed = time.monotonic() - self.last_partial_at
        return (
            not self.partial_running
            and not self.final_triggered
            and elapsed >= PARTIAL_INTERVAL_SEC
            and self.total_bytes() >= MIN_BYTES_FOR_STT
        )

    def build_wav_snapshot(self) -> bytes:
        """
        Convert the currently accumulated Float32 PCM chunks into a valid
        16-bit 16kHz mono WAV file in memory.

        Float32 range is [-1.0, 1.0].  We clamp and convert to int16 (s16le).
        Pure Python — no ffmpeg, no temp files.
        """
        if not self.chunks:
            return b""

        # Concatenate all raw bytes
        raw = b"".join(self.chunks)
        n_floats = len(raw) // 4   # each Float32 is 4 bytes

        # Unpack Float32 samples
        floats = struct.unpack(f"<{n_floats}f", raw[: n_floats * 4])

        # Convert Float32 → int16 (with clamping)
        int16_samples = [
            max(-32768, min(32767, int(s * 32767)))
            for s in floats
        ]

        # Pack as little-endian int16
        int16_bytes = struct.pack(f"<{len(int16_samples)}h", *int16_samples)

        # Write WAV container in memory
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(PCM_CHANNELS)
            wf.setsampwidth(2)        # 16-bit = 2 bytes
            wf.setframerate(PCM_SAMPLE_RATE)
            wf.writeframes(int16_bytes)

        return buf.getvalue()


# ── Convenience float32→wav function (also usable standalone) ──────────────────

def float32_bytes_to_wav(
    raw_bytes: bytes,
    sample_rate: int = PCM_SAMPLE_RATE,
    channels: int = PCM_CHANNELS,
) -> bytes:
    """
    Convert raw Float32 PCM bytes → 16-bit WAV bytes.

    Args:
        raw_bytes   Raw bytes sent from AudioWorklet (Float32Array.buffer)
        sample_rate Sample rate in Hz (default 16000)
        channels    Number of channels (default 1 — mono)

    Returns:
        Complete WAV file as bytes, ready to send to any STT API.
    """
    n_floats = len(raw_bytes) // 4
    if n_floats == 0:
        return b""

    floats = struct.unpack(f"<{n_floats}f", raw_bytes[: n_floats * 4])
    int16_samples = [max(-32768, min(32767, int(s * 32767))) for s in floats]
    int16_bytes = struct.pack(f"<{len(int16_samples)}h", *int16_samples)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16_bytes)
    return buf.getvalue()
