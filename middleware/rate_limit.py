"""
VaaniBank AI — Rate Limiting Middleware
PSBs Hackathon 2026 | Team Vectora

Simple in-memory rate limiter for AI-heavy endpoints.
Prevents abuse of STT, LLM, and TTS endpoints which consume
expensive external API calls (Sarvam, Groq).

Uses a sliding-window counter per client IP.
For production at scale, replace with Redis-backed limiter.

Usage in main.py:
    from middleware.rate_limit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware, max_requests=30, window_seconds=60)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger("vaanibank.rate_limit")

# Paths that consume expensive external AI APIs
_RATE_LIMITED_PREFIXES = (
    "/stt/",
    "/llm/",
    "/tts/generate",
)

# Public (no-auth) endpoints get stricter limits to prevent abuse/DoS
_PUBLIC_STRICT_PREFIXES = (
    "/stt/customer-transcribe",
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter for AI endpoints.

    Args:
        app: ASGI application
        max_requests: Maximum requests per window (default 30)
        window_seconds: Window duration in seconds (default 60)
    """

    def __init__(
        self,
        app: ASGIApp,
        max_requests: int = 30,
        window_seconds: int = 60,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # {client_ip: [timestamp, timestamp, ...]}
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP, respecting X-Forwarded-For for reverse proxies."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _is_rate_limited_path(self, path: str) -> bool:
        """Check if the request path should be rate-limited."""
        return any(path.startswith(prefix) for prefix in _RATE_LIMITED_PREFIXES)

    def _cleanup_old_entries(self, now: float) -> None:
        """Periodically purge expired entries to prevent memory leak."""
        if now - self._last_cleanup < 30:  # cleanup every 30s
            return
        self._last_cleanup = now
        cutoff = now - self.window_seconds
        stale_keys = []
        for ip, timestamps in self._requests.items():
            self._requests[ip] = [t for t in timestamps if t > cutoff]
            if not self._requests[ip]:
                stale_keys.append(ip)
        for key in stale_keys:
            del self._requests[key]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Only rate-limit AI-heavy endpoints
        if not self._is_rate_limited_path(path):
            return await call_next(request)

        # Skip rate limiting for GET requests (serving audio files)
        if request.method == "GET":
            return await call_next(request)

        now = time.time()
        client_ip = self._get_client_ip(request)

        # Cleanup old entries periodically
        self._cleanup_old_entries(now)

        # Sliding window: keep only recent timestamps
        cutoff = now - self.window_seconds
        self._requests[client_ip] = [
            t for t in self._requests[client_ip] if t > cutoff
        ]

        # Determine effective limit — public (no-auth) endpoints get stricter limits
        is_public = any(path.startswith(p) for p in _PUBLIC_STRICT_PREFIXES)
        effective_limit = self.max_requests // 2 if is_public else self.max_requests

        # Check limit
        if len(self._requests[client_ip]) >= effective_limit:
            logger.warning(
                "Rate limit exceeded | ip=%s | path=%s | count=%d/%d | public=%s",
                client_ip, path, len(self._requests[client_ip]), effective_limit, is_public,
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "Too many requests. Please wait before trying again.",
                    "retry_after_seconds": self.window_seconds,
                },
                headers={
                    "Retry-After": str(self.window_seconds),
                    "X-RateLimit-Limit": str(effective_limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        # Record this request
        self._requests[client_ip].append(now)
        remaining = effective_limit - len(self._requests[client_ip])

        # Process request
        response = await call_next(request)

        # Add rate limit headers to response
        response.headers["X-RateLimit-Limit"] = str(effective_limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))

        return response
