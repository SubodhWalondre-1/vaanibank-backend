"""
VaaniBank AI — Dynamic System Settings Service
PSBs Hackathon 2026 | Team Vectora

Provides persistent, live-adjustable system settings cached in Redis
and persisted in backend/config/dynamic_settings.json to avoid database migrations.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from database import redis_client

logger = logging.getLogger("vaanibank.settings")

# Dynamic Settings JSON persistence path
SETTINGS_FILE_PATH = Path(__file__).resolve().parent.parent / "config" / "dynamic_settings.json"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "demo_mode": False,
    # Session Configuration
    "default_session_timeout": 15,       # minutes
    "max_exchanges_per_session": 50,
    "pii_detection": True,
    "idle_timeout": 5,                  # minutes
    # AI Pipeline Configuration
    "primary_stt": "sarvam_saarika_2.5",
    "fallback_stt_1": "groq_whisper",
    "fallback_stt_2": "reverie",
    "llm_model": "groq_llama_3.3_70b",
    "translation_engine": "sarvam_translate",
    "tts_engine": "sarvam_bulbul_v3",
}

REDIS_SETTINGS_KEY = "system_settings"

# In-memory TTL for settings cache (seconds).
# Avoids 4+ Redis roundtrips per pipeline execution.
_SETTINGS_CACHE_TTL: float = 30.0


class SettingsService:
    """
    Manages runtime system settings.
    Saves to Redis (sub-millisecond lookups) and /backend/config/dynamic_settings.json (persistence).
    Uses a process-level in-memory cache (30s TTL) to eliminate repeated Redis hits
    within the same pipeline run.
    """

    def __init__(self) -> None:
        # Create config directory if not exists
        SETTINGS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # In-memory cache to avoid Redis roundtrips on every call
        self._mem_cache: Optional[Dict[str, Any]] = None
        self._mem_cache_ts: float = 0.0

    async def get_all_settings(self) -> Dict[str, Any]:
        """
        Retrieve active settings.
        1. In-memory cache (zero-cost, 30s TTL)
        2. Redis cache
        3. JSON file fallback
        4. DEFAULT_SETTINGS
        """
        # 0. In-memory cache — eliminates 4+ Redis roundtrips per pipeline run
        now = time.monotonic()
        if self._mem_cache is not None and (now - self._mem_cache_ts) < _SETTINGS_CACHE_TTL:
            return self._mem_cache

        # 1. Try Redis cache
        try:
            cached = await redis_client.get(REDIS_SETTINGS_KEY)
            if cached:
                result = json.loads(cached)
                self._mem_cache = result
                self._mem_cache_ts = now
                return result
        except Exception as exc:
            logger.warning("Redis settings read failed: %s", exc)

        # 2. Try JSON file fallback
        if SETTINGS_FILE_PATH.exists():
            try:
                with open(SETTINGS_FILE_PATH, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                # Fill in any missing keys with defaults
                merged = {**DEFAULT_SETTINGS, **settings}
                # Warm up Redis + in-memory cache
                await self._cache_in_redis(merged)
                self._mem_cache = merged
                self._mem_cache_ts = time.monotonic()
                return merged
            except Exception as exc:
                logger.error("Failed to read settings from file: %s", exc)

        # 3. Return defaults
        defaults = DEFAULT_SETTINGS.copy()
        self._mem_cache = defaults
        self._mem_cache_ts = time.monotonic()
        return defaults

    async def update_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge and update system settings.
        Saves to dynamic_settings.json and updates Redis.
        """
        current = await self.get_all_settings()
        
        # Validate types/keys and merge
        for key, value in new_settings.items():
            if key in DEFAULT_SETTINGS:
                # Basic cast validation
                expected_type = type(DEFAULT_SETTINGS[key])
                if expected_type is bool:
                    current[key] = bool(value)
                elif expected_type is int:
                    current[key] = int(value)
                else:
                    current[key] = str(value)

        # Save to file
        try:
            with open(SETTINGS_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=4)
            logger.info("Dynamic settings saved to %s", SETTINGS_FILE_PATH.name)
        except Exception as exc:
            logger.error("Failed to write dynamic settings to file: %s", exc)

        # Save to Redis + invalidate in-memory cache
        await self._cache_in_redis(current)
        self._mem_cache = current
        self._mem_cache_ts = time.monotonic()

        return current

    async def _cache_in_redis(self, settings: Dict[str, Any]) -> None:
        try:
            # Cache for 7 days
            await redis_client.setex(
                REDIS_SETTINGS_KEY,
                7 * 24 * 3600,
                json.dumps(settings),
            )
            logger.info("Dynamic settings cached in Redis under '%s'", REDIS_SETTINGS_KEY)
        except Exception as exc:
            logger.warning("Redis settings cache update failed: %s", exc)


# Singleton
settings_service = SettingsService()
