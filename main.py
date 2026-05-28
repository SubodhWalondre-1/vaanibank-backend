"""
VaaniBank AI — FastAPI Application Entry Point
PSBs Hackathon 2026 | Team Vectora

Mounts:
  /audio      → storage/audio      (TTS + customer audio)
  /summaries  → storage/summaries  (PDF files)
  /storage/signatures → storage/signatures  (SaralForm signature PNGs)

Routers:
  auth_router        → /auth/...
  sessions_router    → /sessions/... + /ws/...
  ai_pipeline_router → /stt/... /llm/... /tts/...
  summary_router     → /summary/... /process/... /analytics/... /branches/...
  forms_router       → /forms/...       ← SaralForm (Phase 1)

Health:
  GET /health          → DB + Redis + Sarvam + Groq
  GET /health/services → per-service detail
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from core.exceptions import register_exception_handlers
from database import engine
from routers.auth import router as auth_router
from routers.sessions import router as sessions_router
from routers.ai_pipeline import router as ai_pipeline_router
from routers.summary import router as summary_router
from routers.staff import router as staff_router

# ── SaralForm router (Phase 1) ────────────────────────────────────────────────
# Provides POST /forms/submit and GET /forms/signature/{token}
from routers.forms import router as forms_router

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vaanibank.main")


# ══════════════════════════════════════════════════════════════════════════════
# LIFESPAN — startup + shutdown
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Create storage directories ─────────────────────────────────────────────
    audio_dir = Path(settings.AUDIO_STORAGE_PATH)
    summary_dir = Path(settings.SUMMARY_STORAGE_PATH)
    # SaralForm signatures directory — created here so it's guaranteed to exist
    # before any request hits POST /forms/submit.
    signatures_dir = Path(settings.SIGNATURE_STORAGE_PATH)

    for d in (audio_dir, summary_dir, signatures_dir):
        d.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Storage directories ready | audio=%s | summaries=%s | signatures=%s",
        audio_dir, summary_dir, signatures_dir,
    )

    # ── Test PostgreSQL ────────────────────────────────────────────────────────
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("PostgreSQL connection OK")
    except Exception as exc:
        logger.error("PostgreSQL connection FAILED: %s", exc)

    # ── Test Redis ─────────────────────────────────────────────────────────────
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.ping()
        # Flush stale TTS cache (old entries have wrong /storage/audio/ path)
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await r.scan(cursor, match="tts_cache:*", count=100)
            if keys:
                await r.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        if deleted:
            logger.info("Flushed %d stale tts_cache:* Redis keys", deleted)
        await r.aclose()
        logger.info("Redis connection OK")
    except Exception as exc:
        logger.warning("Redis connection FAILED: %s", exc)

    # ── Auto-seed missing demo staff accounts ──────────────────────────────────
    try:
        from passlib.context import CryptContext
        from sqlalchemy import select as sa_select
        from models import StaffMember, Branch
        pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        DEMO_STAFF = [
            {"staff_id": "UBI-NGP-001", "username": "demo",    "password_plain": "demo123",    "full_name": "Rajesh Kumar",  "role": "teller",     "branch_code": "NGP-CVL-01", "languages_known": ["Hindi", "Marathi"]},
            {"staff_id": "UBI-NGP-002", "username": "manager", "password_plain": "manager123", "full_name": "Priya Sharma",  "role": "manager",    "branch_code": "NGP-CVL-01", "languages_known": ["Hindi", "English"]},
            {"staff_id": "UBI-MUM-042", "username": "admin",   "password_plain": "admin123",   "full_name": "Amit Patel",    "role": "admin",      "branch_code": "MUM-AND-01", "languages_known": ["Hindi", "Gujarati", "English"]},
        ]
        async with AsyncSession(engine) as seed_db:
            for sdata in DEMO_STAFF:
                existing = (await seed_db.execute(
                    sa_select(StaffMember).where(StaffMember.staff_id == sdata["staff_id"])
                )).scalar_one_or_none()
                if existing:
                    continue
                branch = (await seed_db.execute(
                    sa_select(Branch).where(Branch.branch_code == sdata["branch_code"])
                )).scalar_one_or_none()
                if not branch:
                    logger.warning("Auto-seed: branch %s not found, skipping %s", sdata["branch_code"], sdata["username"])
                    continue
                staff = StaffMember(
                    staff_id=sdata["staff_id"],
                    username=sdata["username"],
                    password_hash=pwd_ctx.hash(sdata["password_plain"]),
                    full_name=sdata["full_name"],
                    role=sdata["role"],
                    branch_id=branch.id,
                    languages_known=sdata["languages_known"],
                    is_active=True,
                )
                seed_db.add(staff)
                logger.info("Auto-seeded staff: %s (%s)", sdata["username"], sdata["role"])
            await seed_db.commit()
    except Exception as exc:
        logger.warning("Auto-seed staff check failed (non-fatal): %s", exc)

    # ── Pre-warm RAG service (run in background to avoid blocking port binding) ──
    async def _prewarm_rag():
        try:
            from services.rag_service import rag_service
            await rag_service.ensure_ready()
            logger.info("RAG service pre-warmed successfully")
        except Exception as exc:
            logger.warning("RAG service warmup failed (non-fatal, will retry on first query): %s", exc)

    asyncio.create_task(_prewarm_rag())

    logger.info(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  VaaniBank AI started  |  Union Bank of India  |  PSBs 2026\n"
        "  Env: %-12s  Port: %s\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        settings.APP_ENV,
        settings.APP_PORT,
    )

    # ── Keep-alive self-ping (prevents Render free tier from sleeping) ─────
    keep_alive_task: asyncio.Task | None = None

    async def _keep_alive_ping():
        """Ping our own /health endpoint every 14 minutes to prevent Render
        free-tier instances from spinning down after 15 min of inactivity."""
        INTERVAL = 14 * 60  # 14 minutes in seconds
        port = settings.APP_PORT
        url = f"http://localhost:{port}/health"
        logger.info("Keep-alive pinger started (interval=%ds, url=%s)", INTERVAL, url)
        while True:
            await asyncio.sleep(INTERVAL)
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url)
                logger.info("Keep-alive ping OK — status=%d", resp.status_code)
            except Exception as exc:
                logger.warning("Keep-alive ping failed: %s", exc)

    if settings.APP_ENV == "production":
        keep_alive_task = asyncio.create_task(_keep_alive_ping())
        logger.info("Keep-alive background task scheduled (production mode)")

    yield  # ── Application running ────────────────────────────────────────────

    # ── Cleanup ────────────────────────────────────────────────────────────────
    if keep_alive_task and not keep_alive_task.done():
        keep_alive_task.cancel()
        try:
            await keep_alive_task
        except asyncio.CancelledError:
            pass
        logger.info("Keep-alive background task cancelled")

    await engine.dispose()
    logger.info("VaaniBank AI shut down — DB connections closed")


# ══════════════════════════════════════════════════════════════════════════════
# APPLICATION
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="VaaniBank AI",
    description=(
        "Multilingual AI-powered banking assistant for Union Bank of India. "
        "PSBs Hackathon 2026 — Team Vectora."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ── Exception handlers (registered before middleware & routers) ───────────────
register_exception_handlers(app)

# ── Rate Limiting (AI endpoints — STT/LLM/TTS) ───────────────────────────────
from middleware.rate_limit import RateLimitMiddleware

app.add_middleware(
    RateLimitMiddleware,
    max_requests=30,
    window_seconds=60,
)

# ── GZip Compression ──────────────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=500)

# ── CORS ──────────────────────────────────────────────────────────────────────
_allowed_origins: list[str] = [
    o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# ── Static file mounts ────────────────────────────────────────────────────────

def _mount_static(route: str, directory: str, name: str) -> None:
    """Create the directory if absent, then mount it as a static file route."""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    app.mount(route, StaticFiles(directory=str(path)), name=name)


_mount_static("/audio",               settings.AUDIO_STORAGE_PATH,     "audio")
_mount_static("/summaries",           settings.SUMMARY_STORAGE_PATH,   "summaries")
# SaralForm signature PNGs — served at /storage/signatures/<filename>
# The /forms/signature/{token} endpoint handles the named download route;
# this mount is a direct fallback for internal tooling and admin downloads.
_mount_static(
    "/storage/signatures",
    settings.SIGNATURE_STORAGE_PATH,
    "signatures",
)

# ── CORS headers for static files (browser fetch needs this) ─────────────────
@app.middleware("http")
async def add_cors_to_static(request: Request, call_next):
    response = await call_next(request)
    if (
        request.url.path.startswith("/audio/")
        or request.url.path.startswith("/summaries/")
        or request.url.path.startswith("/storage/signatures/")
    ):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router)         # /auth/login  /auth/logout  /auth/refresh  /auth/me
app.include_router(sessions_router)     # /sessions/*  /ws/{token_number}
app.include_router(ai_pipeline_router)  # /stt/*  /llm/*  /tts/*
app.include_router(summary_router)      # /summary/*  /process/*  /analytics/*  /branches/*
app.include_router(staff_router)        # /staff/*  /admin/*  /analytics/branch/*
app.include_router(forms_router)        # /forms/submit  /forms/signature/{token}  ← SaralForm


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

async def _check_db() -> dict:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def _check_redis() -> dict:
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.ping()
        await r.aclose()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def _check_sarvam() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.sarvam.ai/")
        return {"status": "ok", "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def _check_groq() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.groq.com/",
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
            )
        return {"status": "ok", "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.get("/health", summary="Aggregated health check", tags=["health"])
async def health_check() -> JSONResponse:
    """HTTP 200 if all services reachable; 503 if any are down."""
    db_s, redis_s, sarvam_s, groq_s = await asyncio.gather(
        _check_db(), _check_redis(), _check_sarvam(), _check_groq()
    )
    all_ok = all(s["status"] == "ok" for s in [db_s, redis_s, sarvam_s, groq_s])
    return JSONResponse(
        content={
            "status": "ok" if all_ok else "degraded",
            "environment": settings.APP_ENV,
            "services": {
                "database": db_s["status"],
                "redis":    redis_s["status"],
                "sarvam":   sarvam_s["status"],
                "groq":     groq_s["status"],
            },
        },
        status_code=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@app.get("/health/services", summary="Detailed per-service health", tags=["health"])
async def health_services() -> JSONResponse:
    """Detailed status with error messages, model names, and storage paths."""
    db_s, redis_s, sarvam_s, groq_s = await asyncio.gather(
        _check_db(), _check_redis(), _check_sarvam(), _check_groq()
    )
    return JSONResponse(
        content={
            "database": {**db_s, "url": _redact_url(settings.DATABASE_URL)},
            "redis":    {**redis_s, "url": _redact_url(settings.REDIS_URL)},
            "sarvam":   {**sarvam_s, "stt_model": "saarika:v2.5", "tts_model": settings.SARVAM_TTS_MODEL},
            "groq":     {**groq_s, "model": settings.GROQ_MODEL, "max_tokens": settings.GROQ_MAX_TOKENS},
            "storage": {
                "audio_path":          settings.AUDIO_STORAGE_PATH,
                "audio_exists":        Path(settings.AUDIO_STORAGE_PATH).exists(),
                "summaries_path":      settings.SUMMARY_STORAGE_PATH,
                "summaries_exists":    Path(settings.SUMMARY_STORAGE_PATH).exists(),
                "signatures_path":     settings.SIGNATURE_STORAGE_PATH,
                "signatures_exists":   Path(settings.SIGNATURE_STORAGE_PATH).exists(),
            },
        },
        status_code=status.HTTP_200_OK,
    )


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _redact_url(url: str) -> str:
    """Replace the password in a DB/Redis connection URL with *** for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        if parsed.password:
            port_part = f":{parsed.port}" if parsed.port else ""
            netloc = f"{parsed.username}:***@{parsed.hostname}{port_part}"
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return url


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    dev_mode = settings.APP_ENV == "development"
    uvicorn.run(
        "main:app",
        host="127.0.0.1" if dev_mode else "0.0.0.0",
        port=int(settings.APP_PORT),
        reload=dev_mode,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
