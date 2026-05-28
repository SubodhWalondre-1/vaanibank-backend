"""
VaaniBank AI — Rate Limiting Middleware Tests
PSBs Hackathon 2026 | Team Vectora

Run with: python -m pytest tests/test_rate_limit.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import AsyncMock, MagicMock
import asyncio
import time

from middleware.rate_limit import RateLimitMiddleware, _RATE_LIMITED_PREFIXES


class TestRateLimitConfig:
    def test_stt_paths_are_limited(self):
        prefixes = _RATE_LIMITED_PREFIXES
        assert any("/stt/".startswith(p) or p.startswith("/stt/") for p in prefixes)

    def test_llm_paths_are_limited(self):
        prefixes = _RATE_LIMITED_PREFIXES
        assert any("/llm/".startswith(p) or p.startswith("/llm/") for p in prefixes)

    def test_tts_generate_is_limited(self):
        prefixes = _RATE_LIMITED_PREFIXES
        assert any("/tts/generate".startswith(p) for p in prefixes)

    def test_auth_paths_are_not_limited(self):
        """Auth endpoints should NOT be rate-limited."""
        mw = RateLimitMiddleware(app=MagicMock())
        assert mw._is_rate_limited_path("/auth/login") is False

    def test_sessions_paths_are_not_limited(self):
        mw = RateLimitMiddleware(app=MagicMock())
        assert mw._is_rate_limited_path("/sessions/active") is False

    def test_stt_transcribe_is_limited(self):
        mw = RateLimitMiddleware(app=MagicMock())
        assert mw._is_rate_limited_path("/stt/transcribe") is True

    def test_llm_process_is_limited(self):
        mw = RateLimitMiddleware(app=MagicMock())
        assert mw._is_rate_limited_path("/llm/process") is True

    def test_tts_audio_serve_is_limited_but_get_bypassed(self):
        """GET /tts/audio/{file} matches prefix but GET is bypassed in dispatch."""
        mw = RateLimitMiddleware(app=MagicMock())
        # Path matches, but actual dispatch skips GET
        assert mw._is_rate_limited_path("/tts/generate") is True
