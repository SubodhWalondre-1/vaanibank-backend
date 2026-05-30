"""
VaaniBank AI — Custom Exceptions & FastAPI Exception Handlers
PSBs Hackathon 2026 | Team Vectora

Usage in routers:
    from core.exceptions import SessionNotFoundError
    raise SessionNotFoundError(token_number="TKN-001")

Register handlers in main.py:
    from core.exceptions import register_exception_handlers
    register_exception_handlers(app)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger("vaanibank.exceptions")


# BASE EXCEPTION

class VaaniBankException(Exception):
    """
    Base class for all application-level exceptions.

    Attributes:
        status_code : HTTP status code to return
        error_code  : Machine-readable error identifier (snake_case)
        message     : Human-readable description
        detail      : Optional extra context (logged server-side, not always exposed)
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "internal_error"

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        detail: Optional[str] = None,
        **context: Any,
    ) -> None:
        self.message = message
        self.detail = detail
        self.context: Dict[str, Any] = context
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert the custom exception attributes into a serialized dictionary format for JSON responses."""
        return {
            "error": self.error_code,
            "message": self.message,
            **({"detail": self.detail} if self.detail else {}),
        }


# AUTH & ACCESS

class AuthenticationError(VaaniBankException):
    """Raised when credentials are invalid or token is expired/missing."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "authentication_error"

    def __init__(
        self,
        message: str = "Authentication failed. Please check your credentials.",
        detail: Optional[str] = None,
        **context: Any,
    ) -> None:
        super().__init__(message=message, detail=detail, **context)


class AuthorizationError(VaaniBankException):
    """Raised when an authenticated staff lacks permission for an operation."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "authorization_error"

    def __init__(
        self,
        message: str = "You do not have permission to perform this action.",
        required_role: Optional[str] = None,
        **context: Any,
    ) -> None:
        detail = f"Required role: {required_role}" if required_role else None
        super().__init__(message=message, detail=detail, required_role=required_role, **context)


# SESSION

class SessionNotFoundError(VaaniBankException):
    """Raised when a session lookup by token number or ID returns nothing."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "session_not_found"

    def __init__(
        self,
        token_number: Optional[str] = None,
        session_id: Optional[int] = None,
        **context: Any,
    ) -> None:
        if token_number:
            message = f"Session with token '{token_number}' not found."
            detail = f"token_number={token_number}"
        elif session_id:
            message = f"Session ID {session_id} not found."
            detail = f"session_id={session_id}"
        else:
            message = "Session not found."
            detail = None
        super().__init__(
            message=message,
            detail=detail,
            token_number=token_number,
            session_id=session_id,
            **context,
        )


class SessionAlreadyEndedError(VaaniBankException):
    """Raised when an operation is attempted on a completed/abandoned session."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "session_already_ended"

    def __init__(
        self,
        token_number: Optional[str] = None,
        current_status: Optional[str] = None,
        **context: Any,
    ) -> None:
        token_part = f" '{token_number}'" if token_number else ""
        status_part = f" (status: {current_status})" if current_status else ""
        message = f"Session{token_part} has already ended{status_part}."
        super().__init__(
            message=message,
            token_number=token_number,
            current_status=current_status,
            **context,
        )


class SessionNotActiveError(VaaniBankException):
    """Raised when an action requires an active session but it is not active."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "session_not_active"

    def __init__(
        self,
        token_number: Optional[str] = None,
        current_status: Optional[str] = None,
        **context: Any,
    ) -> None:
        token_part = f" '{token_number}'" if token_number else ""
        status_part = f" Current status: {current_status}." if current_status else ""
        message = f"Session{token_part} is not active.{status_part}"
        super().__init__(
            message=message,
            token_number=token_number,
            current_status=current_status,
            **context,
        )


# AI PIPELINE

class STTError(VaaniBankException):
    """Raised when all STT fallback attempts fail."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "stt_error"

    def __init__(
        self,
        message: str = "Speech-to-text transcription failed on all fallback models.",
        model_attempted: Optional[str] = None,
        **context: Any,
    ) -> None:
        detail = f"Last model attempted: {model_attempted}" if model_attempted else None
        super().__init__(
            message=message,
            detail=detail,
            model_attempted=model_attempted,
            **context,
        )


class LLMError(VaaniBankException):
    """Raised when the Groq LLM call fails or returns unparseable output."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "llm_error"

    def __init__(
        self,
        message: str = "Language model processing failed. Please try again.",
        model: Optional[str] = None,
        **context: Any,
    ) -> None:
        detail = f"Model: {model}" if model else None
        super().__init__(message=message, detail=detail, model=model, **context)


class TTSError(VaaniBankException):
    """Raised when TTS audio generation fails on all providers."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "tts_error"

    def __init__(
        self,
        message: str = "Text-to-speech audio generation failed.",
        language_code: Optional[str] = None,
        **context: Any,
    ) -> None:
        detail = f"Language: {language_code}" if language_code else None
        super().__init__(
            message=message,
            detail=detail,
            language_code=language_code,
            **context,
        )


class PIIDetectionError(VaaniBankException):
    """Raised when the PII detection/masking service encounters an error."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "pii_detection_error"

    def __init__(
        self,
        message: str = "PII detection failed. Text could not be processed safely.",
        **context: Any,
    ) -> None:
        super().__init__(message=message, **context)


class PDFGenerationError(VaaniBankException):
    """Raised when ReportLab PDF generation fails."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "pdf_generation_error"

    def __init__(
        self,
        message: str = "Bilingual summary PDF could not be generated.",
        session_id: Optional[int] = None,
        **context: Any,
    ) -> None:
        detail = f"session_id={session_id}" if session_id else None
        super().__init__(
            message=message,
            detail=detail,
            session_id=session_id,
            **context,
        )


# RESOURCE / GENERIC

class ResourceNotFoundError(VaaniBankException):
    """Generic 404 for any resource not found."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"

    def __init__(
        self,
        resource: str = "Resource",
        identifier: Optional[Any] = None,
        **context: Any,
    ) -> None:
        id_part = f" '{identifier}'" if identifier is not None else ""
        message = f"{resource}{id_part} not found."
        super().__init__(message=message, resource=resource, identifier=identifier, **context)


class ValidationError(VaaniBankException):
    """Raised for domain-level validation failures (beyond Pydantic)."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "validation_error"

    def __init__(
        self,
        message: str = "Validation failed.",
        field: Optional[str] = None,
        **context: Any,
    ) -> None:
        detail = f"Field: {field}" if field else None
        super().__init__(message=message, detail=detail, field=field, **context)


# FASTAPI EXCEPTION HANDLERS

def vaanibank_exception_handler(
    request: Request,
    exc: VaaniBankException,
) -> JSONResponse:
    """
    Handle all VaaniBankException subclasses.
    Logs at WARNING for 4xx, ERROR for 5xx.
    """
    if exc.status_code >= 500:
        logger.error(
            "5xx %s | %s %s | %s | context=%s",
            exc.error_code,
            request.method,
            request.url.path,
            exc.message,
            exc.context,
            exc_info=True,
        )
    else:
        logger.warning(
            "4xx %s | %s %s | %s",
            exc.error_code,
            request.method,
            request.url.path,
            exc.message,
        )

    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
    )


def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """
    Catch-all for any unhandled Python exception.
    Returns a generic 500 — never leaks internal details to the client.
    """
    logger.exception(
        "Unhandled exception | %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred. Please try again.",
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all VaaniBank exception handlers on the FastAPI app.

    Call this in main.py after creating the app instance:

        from core.exceptions import register_exception_handlers
        register_exception_handlers(app)
    """
    app.add_exception_handler(VaaniBankException, vaanibank_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # Register each concrete subclass explicitly so FastAPI's
    # most-specific matching works correctly
    for exc_class in (
        AuthenticationError,
        AuthorizationError,
        SessionNotFoundError,
        SessionAlreadyEndedError,
        SessionNotActiveError,
        STTError,
        LLMError,
        TTSError,
        PIIDetectionError,
        PDFGenerationError,
        ResourceNotFoundError,
        ValidationError,
    ):
        app.add_exception_handler(exc_class, vaanibank_exception_handler)