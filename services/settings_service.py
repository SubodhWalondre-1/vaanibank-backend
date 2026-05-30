"""
VaaniBank AI — Dynamic System Settings Service
PSBs Hackathon 2026 | Team Vectora

Provides persistent, live-adjustable system settings cached in Redis
and persisted in backend/config/dynamic_settings.json to avoid database migrations.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

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


class SettingsService:
    """
    Manages runtime system settings.
    Saves to Redis (sub-millisecond lookups) and /backend/config/dynamic_settings.json (persistence).
    """

    def __init__(self) -> None:
        # Create config directory if not exists
        SETTINGS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async def get_all_settings(self) -> Dict[str, Any]:
        """
        Retrieve active settings.
        Checks Redis first. If missed, reads from JSON file.
        If file doesn't exist, returns DEFAULT_SETTINGS.
        """
        # 1. Try Redis cache
        try:
            cached = await redis_client.get(REDIS_SETTINGS_KEY)
            if cached:
                return json.loads(cached)
        except Exception as exc:
            logger.warning("Redis settings read failed: %s", exc)

        # 2. Try JSON file fallback
        if SETTINGS_FILE_PATH.exists():
            try:
                with open(SETTINGS_FILE_PATH, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                # Fill in any missing keys with defaults
                merged = {**DEFAULT_SETTINGS, **settings}
                # Warm up Redis cache
                await self._cache_in_redis(merged)
                return merged
            except Exception as exc:
                logger.error("Failed to read settings from file: %s", exc)

        # 3. Return defaults
        return DEFAULT_SETTINGS.copy()

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

        # Save to Redis
        await self._cache_in_redis(current)

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
