"""
VaaniBank AI — Pydantic v2 Schemas (All request/response models)
PSBs Hackathon 2026 | Team Vectora

Sections:
  1. Auth
  2. Session
  3. Exchange
  4. STT
  5. LLM
  6. TTS
  7. Process
  8. Summary
  9. Analytics
  10. WebSocket
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Shared base

class _Base(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,       # ORM → schema conversion
        populate_by_name=True,
        str_strip_whitespace=True,
        protected_namespaces=(),    # suppress model_ namespace warning
    )


# 1. AUTH

class LoginRequest(_Base):
    staff_id: Optional[str] = Field(None, description="Staff ID e.g. UBI-NGP-001")
    username: str = Field(..., min_length=2, max_length=100)
    password: str = Field(..., min_length=4, max_length=128)


# StaffResponse — used by all routers (auth.py uses this name)
class StaffResponse(_Base):
    id: int
    staff_id: str
    username: str
    full_name: str
    role: str
    branch_id: int
    branch_code: str
    branch_name: str
    languages_known: Optional[List[str]] = Field(default_factory=list)


# StaffInfo kept as alias so any code referencing either name works
StaffInfo = StaffResponse


class LoginResponse(_Base):
    access_token: str
    token_type: str = "bearer"
    expires_in: int                 # routers use expires_in (not expires_in_seconds)
    staff: StaffResponse


class TokenRefreshRequest(_Base):
    token: str = Field(..., description="Existing JWT to refresh")  # routers use .token


class TokenRefreshResponse(_Base):
    access_token: str
    token_type: str = "bearer"
    expires_in: int                 # routers use expires_in


class LogoutResponse(_Base):
    message: str = "Logged out successfully"


# 2. SESSION

# SessionCreateRequest — name used by sessions.py router
class SessionCreateRequest(_Base):
    customer_language: Optional[str] = Field(None, description="Language name e.g. Marathi")
    customer_language_code: Optional[str] = Field(None, description="ISO code e.g. mr")
    entry_method: Optional[str] = Field(default="walk_in", description="qr_scan | manual | walk_in")
    offline_mode: Optional[bool] = False

    @field_validator("entry_method")
    @classmethod
    def validate_entry_method(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"qr_scan", "manual", "walk_in"}:
            raise ValueError("entry_method must be one of qr_scan, manual, walk_in")
        return v


# SessionCreate kept as alias
SessionCreate = SessionCreateRequest


# CustomerSessionCreateRequest — public (no auth) for customer panel QR-scan flow
class CustomerSessionCreateRequest(_Base):
    branch_code: str = Field(..., min_length=1, description="Branch code from QR e.g. NGP-CVL-01")
    customer_language: Optional[str] = Field(None, description="Language name e.g. Marathi")
    customer_language_code: Optional[str] = Field(None, description="ISO code e.g. mr")
    entry_method: Optional[str] = Field(default="qr_scan", description="qr_scan | manual | walk_in")

    @field_validator("entry_method")
    @classmethod
    def validate_entry_method(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"qr_scan", "manual", "walk_in"}:
            raise ValueError("entry_method must be one of qr_scan, manual, walk_in")
        return v


class SessionCreateResponse(_Base):
    session_id: int
    token_number: str
    websocket_url: str
    customer_panel_url: str
    customer_language: Optional[str] = None
    customer_language_code: Optional[str] = None
    status: str
    created_at: datetime


class SessionResponse(_Base):
    id: int
    token_number: str
    branch_id: int
    branch_name: Optional[str] = None          # joined from branch table
    staff_id: Optional[int]
    customer_language: Optional[str]
    customer_language_code: Optional[str]
    staff_language: str
    entry_method: str
    status: str
    sentiment_overall: Optional[str]
    intent_detected: Optional[str]
    total_exchanges: int
    duration_seconds: Optional[int]
    offline_mode: bool
    stt_model_used: Optional[str] = None
    pii_detected: bool
    pii_types_found: Optional[List[str]]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    created_at: datetime


class SessionUpdate(_Base):
    customer_language: Optional[str] = None
    customer_language_code: Optional[str] = None
    status: Optional[str] = None
    sentiment_overall: Optional[str] = None
    intent_detected: Optional[str] = None
    stt_model_used: Optional[str] = None
    offline_mode: Optional[bool] = None


class SessionEndResponse(_Base):
    session_id: int
    token_number: str
    status: str
    duration_seconds: Optional[int]
    message: str


class SessionListResponse(_Base):
    sessions: List[SessionResponse]
    total: int
    page: int = 1
    page_size: int = 20
    total_pages: int = 1            # required by sessions.py router


# 3. EXCHANGE

class ExchangeCreate(_Base):
    session_id: int
    exchange_number: int
    direction: str = Field(..., description="customer_to_staff | staff_to_customer")
    customer_audio_url: Optional[str] = None
    customer_text_original: Optional[str] = None
    customer_text_translated: Optional[str] = None
    staff_response_suggested: Optional[str] = None
    staff_response_final: Optional[str] = None
    staff_response_translated: Optional[str] = None
    staff_audio_url: Optional[str] = None
    stt_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    stt_model_used: Optional[str] = None
    sentiment: Optional[str] = None
    intent: Optional[str] = None
    pii_detected: bool = False
    pii_masked_text: Optional[str] = None
    response_time_ms: Optional[int] = None
    staff_used_suggestion: bool = False


class ExchangeResponse(_Base):
    id: int
    session_id: int
    exchange_number: int
    direction: str
    customer_audio_url: Optional[str]
    customer_text_original: Optional[str]
    customer_text_translated: Optional[str]
    staff_response_suggested: Optional[str]
    staff_response_final: Optional[str]
    staff_response_translated: Optional[str]
    staff_audio_url: Optional[str]
    stt_confidence: Optional[float]
    stt_model_used: Optional[str]
    sentiment: Optional[str]
    intent: Optional[str]
    pii_detected: bool
    pii_masked_text: Optional[str]
    response_time_ms: Optional[int]
    staff_used_suggestion: bool
    created_at: datetime


# 4. STT

class TranscribeResponse(_Base):
    exchange_id: int                            # added — ai_pipeline.py returns this
    text_original: str
    text_translated: Optional[str] = None
    masked_text: Optional[str] = None          # added — ai_pipeline.py returns this
    confidence: float = Field(..., ge=0.0, le=1.0)
    model_used: str
    language_detected: Optional[str] = None
    pii_detected: bool = False
    pii_types: List[str] = Field(default_factory=list)   # added
    pii_masked_text: Optional[str] = None
    duration_seconds: Optional[float] = None
    sentiment: Optional[str] = None            # added — ai_pipeline.py returns this
    intent: Optional[str] = None               # added
    suggested_response_hindi: Optional[str] = None       # added
    suggested_response_customer_lang: Optional[str] = None  # added
    response_time_ms: Optional[int] = None     # added


class DetectLanguageResponse(_Base):
    language_code: str
    language_name: str
    confidence: float = Field(..., ge=0.0, le=1.0)


# 5. LLM

class LLMProcessRequest(_Base):
    text: str = Field(..., min_length=1)
    source_language: str
    source_language_code: Optional[str] = None
    session_id: Optional[int] = None
    exchange_number: Optional[int] = None
    token_number: Optional[str] = None                         # added — used by router
    conversation_history: Optional[List[Dict[str, Any]]] = None  # added — used by ai_service


class LLMProcessResponse(_Base):
    translation: str
    intent: str
    intent_confidence: float = Field(..., ge=0.0, le=1.0)
    sentiment: str
    sentiment_score: float = Field(..., ge=0.0, le=1.0)
    suggested_response_hindi: str
    suggested_response_customer_lang: str
    banking_terms_detected: List[str] = Field(default_factory=list)
    process_triggered: Optional[str] = None


# TranslateStaffResponseRequest — name used by ai_pipeline.py router
class TranslateStaffResponseRequest(_Base):
    text: str = Field(..., min_length=1, description="Staff response in Hindi")
    target_language_code: str
    target_language: str


# TranslateStaffRequest kept as alias
TranslateStaffRequest = TranslateStaffResponseRequest


class TranslateStaffResponse(_Base):
    original_text: str
    translated_text: str
    target_language: str
    target_language_code: str


# 6. TTS

class TTSGenerateRequest(_Base):
    text: str = Field(..., min_length=1, max_length=2000)
    language_code: str
    speaker: Optional[str] = None
    session_id: Optional[int] = None
    token_number: Optional[str] = None   # added — router broadcasts audio via WS


class TTSGenerateResponse(_Base):
    audio_url: str
    duration_seconds: Optional[float] = None
    model_used: str
    from_cache: bool = False
    language_code: str


# 7. PROCESS STEPS

class ProcessStepResponse(_Base):
    id: int
    intent_type: str
    step_number: int
    step_text_hindi: str
    step_text_marathi: Optional[str] = None
    step_text_tamil: Optional[str] = None
    step_text_telugu: Optional[str] = None
    step_text_bengali: Optional[str] = None
    step_text_kannada: Optional[str] = None
    step_text_odia: Optional[str] = None
    step_text_punjabi: Optional[str] = None
    speak_to_customer: bool
    is_active: bool

    def text_for_language(self, language_code: str) -> str:
        mapping: Dict[str, Optional[str]] = {
            "mr": self.step_text_marathi,
            "ta": self.step_text_tamil,
            "te": self.step_text_telugu,
            "bn": self.step_text_bengali,
            "kn": self.step_text_kannada,
            "or": self.step_text_odia,
            "pa": self.step_text_punjabi,
        }
        return mapping.get(language_code) or self.step_text_hindi


# ProcessStepsResponse — used by summary.py router
class ProcessStepsResponse(_Base):
    intent_type: str
    language_code: str
    steps: List[Dict[str, Any]]   # list of step dicts built in router
    total: int


# ProcessStepCompleteRequest — used by summary.py router
class ProcessStepCompleteRequest(_Base):
    session_id: int
    step_id: int
    token_number: Optional[str] = None     # for WS broadcast
    status: str = Field(default="completed")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in {"completed", "skipped"}:
            raise ValueError("status must be 'completed' or 'skipped'")
        return v


# StepCompleteRequest kept as alias
StepCompleteRequest = ProcessStepCompleteRequest


# ProcessStepCompleteResponse — used by summary.py router
class ProcessStepCompleteResponse(_Base):
    session_id: int
    step_id: int
    current_step: int
    total_steps: int
    progress_percentage: float


class ProcessProgressResponse(_Base):
    session_id: int
    current_step: int
    total_steps: int
    progress_percentage: float
    next_step_id: Optional[int] = None
    is_complete: bool = False


# 8. SUMMARY

class SummaryContent(_Base):
    title: Optional[str] = None
    body: Optional[str] = None
    key_points: List[str] = Field(default_factory=list)
    next_steps: List[str] = Field(default_factory=list)


# GenerateSummaryRequest — name used by summary.py router
class GenerateSummaryRequest(_Base):
    session_id: int
    force_regenerate: bool = False


# SummaryGenerateRequest kept as alias
SummaryGenerateRequest = GenerateSummaryRequest


class SummaryResponse(_Base):
    id: int
    session_id: int
    customer_language: Optional[str]
    summary_hindi: List[str] = Field(default_factory=list)
    summary_customer_lang: List[str] = Field(default_factory=list)
    key_points_hindi: List[str] = Field(default_factory=list)
    key_points_customer: List[str] = Field(default_factory=list)
    next_steps_hindi: List[str] = Field(default_factory=list)
    next_steps_customer: List[str] = Field(default_factory=list)
    pdf_url: Optional[str]
    pdf_generated: bool
    whatsapp_sent: bool
    whatsapp_sent_at: Optional[datetime]
    generated_at: datetime


class WhatsAppSendRequest(_Base):
    phone_number: str = Field(..., min_length=10, max_length=15)



# WhatsAppSendResponse — fields used by summary.py router: summary_id, queued, message
class WhatsAppSendResponse(_Base):
    summary_id: int
    queued: bool
    message: str
    phone_number: Optional[str] = None
    whatsapp_sent_at: Optional[datetime] = None


# 9. ANALYTICS

# AnalyticsTodayResponse — imported by summary.py router
class AnalyticsTodayResponse(_Base):
    branch_id: int
    date: str
    total_sessions: int
    completed_sessions: int
    abandoned_sessions: int
    avg_duration_seconds: int
    languages_used: Dict[str, int] = Field(default_factory=dict)
    intents_breakdown: Dict[str, int] = Field(default_factory=dict)
    sentiments_breakdown: Dict[str, int] = Field(default_factory=dict)
    offline_sessions: int
    pii_detected_count: int
    ai_suggestion_used: int
    ai_suggestion_edited: int
    ai_suggestion_ignored: int


# BranchAnalyticsResponse — same shape, kept as distinct class
class BranchAnalyticsResponse(_Base):
    branch_id: int
    date: str
    total_sessions: int
    completed_sessions: int
    abandoned_sessions: int
    avg_duration_seconds: int
    languages_used: Dict[str, int] = Field(default_factory=dict)
    intents_breakdown: Dict[str, int] = Field(default_factory=dict)
    sentiments_breakdown: Dict[str, int] = Field(default_factory=dict)
    offline_sessions: int
    pii_detected_count: int
    ai_suggestion_used: int
    ai_suggestion_edited: int
    ai_suggestion_ignored: int
    # Optional enriched fields (populated when branch is joined)
    branch_code: Optional[str] = None
    branch_name: Optional[str] = None
    completion_rate: float = Field(default=0.0, ge=0.0, le=100.0)
    ai_suggestion_acceptance_rate: float = Field(default=0.0, ge=0.0, le=100.0)


class StaffPerformanceResponse(_Base):
    staff_id: str
    full_name: str
    role: str
    total_sessions_today: int
    avg_session_duration_seconds: Optional[float]
    ai_suggestion_used: int
    ai_suggestion_edited: int
    ai_suggestion_ignored: int
    languages_served: List[str] = Field(default_factory=list)


# 10. WEBSOCKET EVENTS

# Server → Client payloads

class TranscriptionReadyPayload(_Base):
    text_original: str
    text_translated: Optional[str]
    confidence: float
    sentiment: Optional[str]
    intent: Optional[str]
    pii_detected: bool = False
    model_used: Optional[str] = None


class AISuggestionPayload(_Base):
    suggested_hindi: str
    suggested_customer_lang: str
    intent: str
    intent_confidence: float
    sentiment: str
    process_triggered: Optional[str] = None
    banking_terms_detected: List[str] = Field(default_factory=list)


class AudioReadyPayload(_Base):
    audio_url: str
    duration_seconds: Optional[float]
    language_code: str


class StepUpdatedPayload(_Base):
    current_step: int
    total_steps: int
    progress_percentage: float
    step_status: str = Field(..., description="pending | completed | skipped")
    step_text_hindi: Optional[str] = None
    step_text_customer_lang: Optional[str] = None


class PIIDetectedPayload(_Base):
    pii_types: List[str]
    exchange_id: Optional[int] = None


class SessionConnectedPayload(_Base):
    token_number: str
    role: str
    session_id: int
    branch_name: str
    staff_name: Optional[str] = None


class SessionEndedPayload(_Base):
    token_number: str
    duration_seconds: Optional[int]
    total_exchanges: int
    summary_available: bool


class WSErrorPayload(_Base):
    code: str
    message: str
    detail: Optional[str] = None


# Client → Server payloads

class StaffApprovedPayload(_Base):
    response_text: str = Field(..., min_length=1)
    use_suggestion: bool
    target_language_code: str
    exchange_id: Optional[int] = None


class StaffEditedPayload(_Base):
    response_text: str = Field(..., min_length=1)
    original_suggestion: Optional[str] = None
    target_language_code: str
    exchange_id: Optional[int] = None


class StepCompletedPayload(_Base):
    step_id: int
    status: str = "completed"


class EndSessionPayload(_Base):
    generate_summary: bool = True


# Generic WS envelope

class WSEvent(_Base):
    event: str = Field(
        ...,
        description=(
            "session_connected | customer_speaking | transcription_ready | "
            "ai_suggestion_ready | audio_ready | step_updated | pii_detected | "
            "session_ended | error | "
            "staff_approved_response | staff_edited_response | "
            "step_completed | end_session | ping | pong"
        ),
    )
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None


class WSPongPayload(_Base):
    timestamp: datetime


# 11. Staff Speech / Translation (migrated from ai_pipeline.py — P3)

class StaffTranscribeResponse(_Base):
    """Response for /stt/staff-transcribe — lightweight STT-only endpoint."""
    transcript: str
    confidence: float
    model_used: str
    language_detected: str


class TranslateToEnglishRequest(_Base):
    """Request body for /llm/translate-to-english."""
    text: str


class TranslateToEnglishResponse(_Base):
    """Response for /llm/translate-to-english."""
    english_text: str


# Convenience union types

ServerPayload = (
    TranscriptionReadyPayload
    | AISuggestionPayload
    | AudioReadyPayload
    | StepUpdatedPayload
    | PIIDetectedPayload
    | SessionConnectedPayload
    | SessionEndedPayload
    | WSErrorPayload
    | WSPongPayload
)

ClientPayload = (
    StaffApprovedPayload
    | StaffEditedPayload
    | StepCompletedPayload
    | EndSessionPayload
)