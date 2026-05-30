"""
VaaniBank AI — ORM Models (All 9 tables)
PSBs Hackathon 2026 | Team Vectora

Import order matters for FK resolution:
  Branch → StaffMember → Session → Exchange
                       → SessionProcessTracking ← ProcessStep
                       → BilingualSummary
                       → PIILog ← Exchange
  Branch → AnalyticsDaily
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from database import Base


# Utility

def _now() -> datetime:
    """Timezone-aware UTC now — used as column server_default callable."""
    return datetime.now(timezone.utc)


# Enums

class StaffRole(str, enum.Enum):
    teller = "teller"
    manager = "manager"
    supervisor = "supervisor"
    admin = "admin"


class EntryMethod(str, enum.Enum):
    qr_scan = "qr_scan"
    manual = "manual"
    walk_in = "walk_in"


class SessionStatus(str, enum.Enum):
    waiting = "waiting"
    active = "active"
    completed = "completed"
    abandoned = "abandoned"


class SentimentType(str, enum.Enum):
    calm = "calm"
    frustrated = "frustrated"
    confused = "confused"
    urgent = "urgent"


class IntentType(str, enum.Enum):
    account_opening = "account_opening"
    loan_enquiry = "loan_enquiry"
    kyc_update = "kyc_update"
    card_services = "card_services"
    balance_enquiry = "balance_enquiry"
    fixed_deposit = "fixed_deposit"
    general = "general"


class ExchangeDirection(str, enum.Enum):
    customer_to_staff = "customer_to_staff"
    staff_to_customer = "staff_to_customer"


class TrackingStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    skipped = "skipped"


class PIIType(str, enum.Enum):
    aadhaar = "aadhaar"
    pan = "pan"
    account_number = "account_number"
    phone = "phone"
    dob = "dob"


# 1. Branch

class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    branch_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    branch_name: Mapped[str] = mapped_column(String(200), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(200), nullable=False, default="Union Bank of India")
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pincode: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Relationships
    staff_members: Mapped[List["StaffMember"]] = relationship(
        "StaffMember", back_populates="branch", lazy="select"
    )
    sessions: Mapped[List["Session"]] = relationship(
        "Session", back_populates="branch", lazy="select"
    )
    analytics: Mapped[List["AnalyticsDaily"]] = relationship(
        "AnalyticsDaily", back_populates="branch", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<Branch {self.branch_code} — {self.branch_name}>"


# 2. StaffMember

class StaffMember(Base):
    __tablename__ = "staff_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    staff_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[StaffRole] = mapped_column(
        String(20), nullable=False, default=StaffRole.teller
    )
    branch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    languages_known: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Relationships
    branch: Mapped["Branch"] = relationship("Branch", back_populates="staff_members")
    sessions: Mapped[List["Session"]] = relationship(
        "Session", back_populates="staff", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<StaffMember {self.staff_id} — {self.full_name} ({self.role})>"


# 3. Session

class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    staff_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("staff_members.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_language: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    customer_language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    staff_language: Mapped[str] = mapped_column(String(100), nullable=False, default="Hindi")
    entry_method: Mapped[EntryMethod] = mapped_column(
        String(20), nullable=False, default=EntryMethod.walk_in
    )
    status: Mapped[SessionStatus] = mapped_column(
        String(20), nullable=False, default=SessionStatus.waiting, index=True
    )
    sentiment_overall: Mapped[Optional[SentimentType]] = mapped_column(
        String(20), nullable=True
    )
    intent_detected: Mapped[Optional[IntentType]] = mapped_column(
        String(30), nullable=True
    )
    total_exchanges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    offline_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stt_model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    pii_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pii_types_found: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True, default=list)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Customer PII — captured during session (popup input or voice)
    customer_account_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    customer_mobile_number: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    customer_dob: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    customer_pan: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    customer_aadhaar_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    customer_account_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    customer_kyc_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    customer_balance: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # AI Conversation Intelligence — accumulated collected_info from LLM
    collected_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=None)

    # SaralForm — timestamp set when customer submits the signed form
    #
    # NULL  → form not yet signed (all sessions before SaralForm feature, or
    #         sessions where customer did not reach the form step).
    # value → UTC time of POST /forms/submit call (set by routers/forms.py).
    #
    # This column requires an Alembic migration before it can be used:
    #   cd backend
    # alembic revision autogenerate m "add_form_signed_at_to_sessions"
    #   alembic upgrade head
    #
    # The migration will generate:
    #   op.add_column('sessions',
    #       sa.Column('form_signed_at', sa.DateTime(timezone=True), nullable=True))
    form_signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="UTC timestamp when customer submitted the signed SaralForm",
    )

    # Relationships
    branch: Mapped["Branch"] = relationship("Branch", back_populates="sessions")
    staff: Mapped[Optional["StaffMember"]] = relationship("StaffMember", back_populates="sessions")
    exchanges: Mapped[List["Exchange"]] = relationship(
        "Exchange", back_populates="session", cascade="all, delete-orphan", lazy="select"
    )
    process_tracking: Mapped[List["SessionProcessTracking"]] = relationship(
        "SessionProcessTracking", back_populates="session", cascade="all, delete-orphan", lazy="select"
    )
    bilingual_summary: Mapped[Optional["BilingualSummary"]] = relationship(
        "BilingualSummary", back_populates="session", uselist=False, cascade="all, delete-orphan"
    )
    pii_logs: Mapped[List["PIILog"]] = relationship(
        "PIILog", back_populates="session", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<Session {self.token_number} — {self.status}>"


# 4. Exchange

class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exchange_number: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[ExchangeDirection] = mapped_column(String(30), nullable=False)
    customer_audio_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    customer_text_original: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    customer_text_translated: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    staff_response_suggested: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    staff_response_final: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    staff_response_translated: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    staff_audio_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    stt_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stt_model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    sentiment: Mapped[Optional[SentimentType]] = mapped_column(String(20), nullable=True)
    intent: Mapped[Optional[IntentType]] = mapped_column(String(30), nullable=True)
    pii_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pii_masked_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    staff_used_suggestion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Performance indexes
    __table_args__ = (
        Index('ix_exchange_session_number', 'session_id', 'exchange_number'),
    )

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="exchanges")
    pii_logs: Mapped[List["PIILog"]] = relationship(
        "PIILog", back_populates="exchange", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<Exchange #{self.exchange_number} session={self.session_id} dir={self.direction}>"


# 5. ProcessStep

class ProcessStep(Base):
    __tablename__ = "process_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step_text_hindi: Mapped[str] = mapped_column(Text, nullable=False)
    step_text_marathi: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_tamil: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_telugu: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_bengali: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_kannada: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_odia: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_punjabi: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_gujarati: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_text_malayalam: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    speak_to_customer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("intent_type", "step_number", name="uq_intent_step"),
    )

    # Relationships
    tracking_entries: Mapped[List["SessionProcessTracking"]] = relationship(
        "SessionProcessTracking", back_populates="step", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<ProcessStep {self.intent_type}[{self.step_number}]>"


# 6. SessionProcessTracking

class SessionProcessTracking(Base):
    __tablename__ = "session_process_tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("process_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[TrackingStatus] = mapped_column(
        String(20), nullable=False, default=TrackingStatus.pending
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Performance indexes
    __table_args__ = (
        Index('ix_tracking_session_status', 'session_id', 'status'),
    )

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="process_tracking")
    step: Mapped["ProcessStep"] = relationship("ProcessStep", back_populates="tracking_entries")

    def __repr__(self) -> str:
        return f"<SessionProcessTracking session={self.session_id} step={self.step_id} {self.status}>"


# 7. BilingualSummary

class BilingualSummary(Base):
    __tablename__ = "bilingual_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    summary_hindi: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    summary_customer_lang: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    customer_language: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    key_points_hindi: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    key_points_customer: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    next_steps_hindi: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    next_steps_customer: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pdf_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    whatsapp_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    whatsapp_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="bilingual_summary")

    def __repr__(self) -> str:
        return f"<BilingualSummary session={self.session_id} pdf={self.pdf_generated}>"


# 8. PIILog

class PIILog(Base):
    __tablename__ = "pii_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exchange_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("exchanges.id", ondelete="CASCADE"), nullable=True, index=True
    )
    pii_type: Mapped[PIIType] = mapped_column(String(20), nullable=False)
    masked_value: Mapped[str] = mapped_column(String(100), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="pii_logs")
    exchange: Mapped[Optional["Exchange"]] = relationship("Exchange", back_populates="pii_logs")

    def __repr__(self) -> str:
        return f"<PIILog {self.pii_type} session={self.session_id}>"


# 9. AuditLog

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Actor (who did the action)
    actor_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("staff_members.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_staff_id: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)

    # Action
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Target (who was affected — nullable for branch_created etc.)
    target_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("staff_members.id", ondelete="SET NULL"), nullable=True
    )
    target_staff_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Branch
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    branch_code: Mapped[str] = mapped_column(String(50), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} actor={self.actor_staff_id} target={self.target_staff_id}>"


# 10. AnalyticsDaily

class AnalyticsDaily(Base):
    __tablename__ = "analytics_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[datetime] = mapped_column(Date, nullable=False, index=True)
    total_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    abandoned_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    languages_used: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=dict)
    intents_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=dict)
    sentiments_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=dict)
    offline_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pii_detected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_suggestion_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_suggestion_edited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_suggestion_ignored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("branch_id", "date", name="uq_branch_date"),
    )

    # Relationships
    branch: Mapped["Branch"] = relationship("Branch", back_populates="analytics")

    def __repr__(self) -> str:
        return f"<AnalyticsDaily branch={self.branch_id} date={self.date}>"
