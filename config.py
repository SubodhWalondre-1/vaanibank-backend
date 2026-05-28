"""
VaaniBank AI — Application Configuration
PSBs Hackathon 2026 | Team Vectora

Centralised settings loaded from environment / .env file via Pydantic v2 BaseSettings.
Import the singleton `settings` object wherever config is needed.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────
    DATABASE_URL: str = Field(
        default="postgresql://vaanibank:vaanibank123@localhost:5432/vaanibank_db",
        description="PostgreSQL connection string",
    )

    # ── Redis ─────────────────────────────────────────────
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for caching and session state",
    )
    CELERY_BROKER_URL: str = Field(
        default="redis://localhost:6379/1",
        description="Redis connection URL used as Celery task broker",
    )

    # ── JWT ───────────────────────────────────────────────
    JWT_SECRET_KEY: str = Field(
        default="vaanibank-super-secret-key-2026",
        description="HMAC secret for signing JWT tokens",
    )
    JWT_ALGORITHM: str = Field(
        default="HS256",
        description="JWT signing algorithm",
    )
    JWT_EXPIRE_HOURS: int = Field(
        default=8,
        ge=1,
        le=72,
        description="Token expiry in hours (1–72)",
    )

    # ── Sarvam AI ─────────────────────────────────────────
    SARVAM_API_KEY: str = Field(
        default="",
        description="API key for Sarvam AI (STT + TTS)",
    )
    SARVAM_STT_URL: str = Field(
        default="https://api.sarvam.ai/speech-to-text",
        description="Sarvam Saarika 2.5 STT endpoint",
    )
    SARVAM_TTS_URL: str = Field(
        default="https://api.sarvam.ai/text-to-speech",
        description="Sarvam Bulbul v3 TTS endpoint",
    )
    SARVAM_TTS_MODEL: str = Field(
        default="bulbul:v3",
        description="Sarvam TTS model identifier",
    )

    # ── Reverie RevUp (STT fallback 2) ────────────────────
    REVERIE_APP_ID: str = Field(
        default="",
        description="Reverie RevUp App ID (from revup.reverieinc.com dashboard)",
    )
    REVERIE_API_KEY: str = Field(
        default="",
        description="Reverie RevUp API Key (from revup.reverieinc.com dashboard)",
    )

    # ── Groq (LLM + STT fallback 1) ──────────────────────
    GROQ_API_KEY: str = Field(
        default="",
        description="API key for Groq inference",
    )
    GROQ_MODEL: str = Field(
        default="llama-3.3-70b-versatile",
        description="Groq model identifier",
    )
    GROQ_MAX_TOKENS: int = Field(
        default=1000,
        ge=100,
        le=8192,
        description="Maximum tokens for LLM completion",
    )

    # ── Google Gemini (LLM backup) ────────────────────────
    GEMINI_API_KEY: str = Field(
        default="",
        description="Google Gemini API key (backup LLM)",
    )
    GEMINI_MODEL: str = Field(
        default="gemini-2.0-flash",
        description="Gemini model identifier for backup LLM",
    )

    # ── File Storage ──────────────────────────────────────
    AUDIO_STORAGE_PATH: str = Field(
        default="./storage/audio",
        description="Local directory for STT/TTS audio files",
    )
    SUMMARY_STORAGE_PATH: str = Field(
        default="./storage/summaries",
        description="Local directory for generated PDF summaries",
    )
    # SaralForm: customer signature PNGs written by POST /forms/submit
    # and served by GET /forms/signature/{token}.
    # Override via SIGNATURE_STORAGE_PATH in .env if deploying to a
    # mounted volume (e.g. Render Disk, EBS, or Cloudflare R2 via FUSE).
    SIGNATURE_STORAGE_PATH: str = Field(
        default="./storage/signatures",
        description="Local directory for SaralForm customer signature PNG files",
    )

    R2_ACCOUNT_ID: str = Field(default="", description="Cloudflare Account ID")
    R2_ACCESS_KEY_ID: str = Field(default="", description="Cloudflare R2 Access Key ID")
    R2_SECRET_ACCESS_KEY: str = Field(default="", description="Cloudflare R2 Secret Access Key")
    R2_BUCKET_NAME: str = Field(default="", description="Cloudflare R2 Bucket Name")
    R2_PUBLIC_URL: str = Field(default="", description="Cloudflare R2 Public URL")

    # ── App ───────────────────────────────────────────────
    APP_ENV: str = Field(
        default="development",
        description="Runtime environment: development | staging | production",
    )
    APP_PORT: int = Field(
        default=8000,
        ge=1024,
        le=65535,
        description="Port on which Uvicorn listens",
    )
    ALLOWED_ORIGINS: str = Field(
        default="http://localhost:5173,http://localhost:5174",
        description="Comma-separated list of CORS origins",
    )

    # ── Derived helpers ───────────────────────────────────
    @field_validator("APP_ENV")
    @classmethod
    def validate_app_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"APP_ENV must be one of {allowed}, got '{v}'")
        return v

    @property
    def allowed_origins_list(self) -> List[str]:
        """Parse ALLOWED_ORIGINS string into a Python list."""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    # ── Redis key helpers ─────────────────────────────────
    @staticmethod
    def redis_active_session_key(token_number: str) -> str:
        return f"active_session:{token_number}"

    @staticmethod
    def redis_tts_cache_key(text_md5: str) -> str:
        return f"tts_cache:{text_md5}"

    @staticmethod
    def redis_staff_online_key(staff_id: str) -> str:
        return f"staff_online:{staff_id}"

    @staticmethod
    def redis_branch_config_key(branch_id: int) -> str:
        return f"branch_config:{branch_id}"

    # ── Redis TTLs (seconds) ──────────────────────────────
    REDIS_SESSION_TTL: int = 7200         # 2 hours
    REDIS_TTS_CACHE_TTL: int = 604800     # 7 days
    REDIS_STAFF_ONLINE_TTL: int = 28800   # 8 hours
    REDIS_BRANCH_CONFIG_TTL: int = 86400  # 24 hours


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


# Module-level singleton — import this everywhere
settings: Settings = get_settings()
