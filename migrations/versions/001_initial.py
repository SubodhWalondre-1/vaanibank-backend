"""Initial schema — all 9 tables

Revision ID: 001
Revises:
Create Date: 2026-03-01 00:00:00.000000 UTC

Table creation order (respects FK dependencies):
  1. branches
  2. staff_members          → FK: branches
  3. sessions               → FK: branches, staff_members
  4. exchanges              → FK: sessions
  5. process_steps
  6. session_process_tracking → FK: sessions, process_steps
  7. bilingual_summaries    → FK: sessions
  8. pii_logs               → FK: sessions, exchanges
  9. analytics_daily        → FK: branches
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic revision identifiers
revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


# UPGRADE

def upgrade() -> None:

    # 1. branches
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("branch_code", sa.String(50), nullable=False),
        sa.Column("branch_name", sa.String(200), nullable=False),
        sa.Column("bank_name", sa.String(200), nullable=False, server_default="Union Bank of India"),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("state", sa.String(100), nullable=False),
        sa.Column("region", sa.String(100), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("pincode", sa.String(10), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_branches_branch_code", "branches", ["branch_code"], unique=True)

    # 2. staff_members
    op.create_table(
        "staff_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("staff_id", sa.String(50), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="teller"),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("languages_known", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], ondelete="RESTRICT", name="fk_staff_branch"
        ),
    )
    op.create_index("ix_staff_members_staff_id", "staff_members", ["staff_id"], unique=True)
    op.create_index("ix_staff_members_username", "staff_members", ["username"], unique=True)
    op.create_index("ix_staff_members_branch_id", "staff_members", ["branch_id"])

    # 3. sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("token_number", sa.String(50), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=True),
        sa.Column("customer_language", sa.String(100), nullable=True),
        sa.Column("customer_language_code", sa.String(10), nullable=True),
        sa.Column("staff_language", sa.String(100), nullable=False, server_default="Hindi"),
        sa.Column("entry_method", sa.String(20), nullable=False, server_default="walk_in"),
        sa.Column("status", sa.String(20), nullable=False, server_default="waiting"),
        sa.Column("sentiment_overall", sa.String(20), nullable=True),
        sa.Column("intent_detected", sa.String(30), nullable=True),
        sa.Column("total_exchanges", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("offline_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stt_model_used", sa.String(100), nullable=True),
        sa.Column("pii_detected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pii_types_found", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], ondelete="RESTRICT", name="fk_session_branch"
        ),
        sa.ForeignKeyConstraint(
            ["staff_id"], ["staff_members.id"], ondelete="SET NULL", name="fk_session_staff"
        ),
    )
    op.create_index("ix_sessions_token_number", "sessions", ["token_number"], unique=True)
    op.create_index("ix_sessions_branch_id", "sessions", ["branch_id"])
    op.create_index("ix_sessions_staff_id", "sessions", ["staff_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_sessions_created_at", "sessions", ["created_at"])

    # 4. exchanges
    op.create_table(
        "exchanges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("exchange_number", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(30), nullable=False),
        sa.Column("customer_audio_url", sa.String(500), nullable=True),
        sa.Column("customer_text_original", sa.Text(), nullable=True),
        sa.Column("customer_text_translated", sa.Text(), nullable=True),
        sa.Column("staff_response_suggested", sa.Text(), nullable=True),
        sa.Column("staff_response_final", sa.Text(), nullable=True),
        sa.Column("staff_response_translated", sa.Text(), nullable=True),
        sa.Column("staff_audio_url", sa.String(500), nullable=True),
        sa.Column("stt_confidence", sa.Float(), nullable=True),
        sa.Column("stt_model_used", sa.String(100), nullable=True),
        sa.Column("sentiment", sa.String(20), nullable=True),
        sa.Column("intent", sa.String(30), nullable=True),
        sa.Column("pii_detected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pii_masked_text", sa.Text(), nullable=True),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("staff_used_suggestion", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE", name="fk_exchange_session"
        ),
    )
    op.create_index("ix_exchanges_session_id", "exchanges", ["session_id"])
    op.create_index(
        "ix_exchanges_session_exchange",
        "exchanges",
        ["session_id", "exchange_number"],
    )

    # 5. process_steps
    op.create_table(
        "process_steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("intent_type", sa.String(30), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("step_text_hindi", sa.Text(), nullable=False),
        sa.Column("step_text_marathi", sa.Text(), nullable=True),
        sa.Column("step_text_tamil", sa.Text(), nullable=True),
        sa.Column("step_text_telugu", sa.Text(), nullable=True),
        sa.Column("step_text_bengali", sa.Text(), nullable=True),
        sa.Column("step_text_kannada", sa.Text(), nullable=True),
        sa.Column("step_text_odia", sa.Text(), nullable=True),
        sa.Column("step_text_punjabi", sa.Text(), nullable=True),
        sa.Column("speak_to_customer", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("intent_type", "step_number", name="uq_intent_step"),
    )
    op.create_index("ix_process_steps_intent_type", "process_steps", ["intent_type"])

    # 6. session_process_tracking
    op.create_table(
        "session_process_tracking",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name="fk_tracking_session",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["process_steps.id"],
            ondelete="CASCADE",
            name="fk_tracking_step",
        ),
    )
    op.create_index("ix_tracking_session_id", "session_process_tracking", ["session_id"])
    op.create_index("ix_tracking_step_id", "session_process_tracking", ["step_id"])

    # 7. bilingual_summaries
    op.create_table(
        "bilingual_summaries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("summary_hindi", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary_customer_lang", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("customer_language", sa.String(100), nullable=True),
        sa.Column("key_points_hindi", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("key_points_customer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("next_steps_hindi", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("next_steps_customer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pdf_url", sa.String(500), nullable=True),
        sa.Column("pdf_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("whatsapp_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("whatsapp_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name="fk_summary_session",
        ),
        sa.UniqueConstraint("session_id", name="uq_summary_session"),
    )
    op.create_index(
        "ix_bilingual_summaries_session_id", "bilingual_summaries", ["session_id"], unique=True
    )

    # 8. pii_logs
    op.create_table(
        "pii_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("exchange_id", sa.Integer(), nullable=True),
        sa.Column("pii_type", sa.String(20), nullable=False),
        sa.Column("masked_value", sa.String(100), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE", name="fk_pii_session"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"], ["exchanges.id"], ondelete="CASCADE", name="fk_pii_exchange"
        ),
    )
    op.create_index("ix_pii_logs_session_id", "pii_logs", ["session_id"])
    op.create_index("ix_pii_logs_exchange_id", "pii_logs", ["exchange_id"])
    op.create_index("ix_pii_logs_pii_type", "pii_logs", ["pii_type"])

    # 9. analytics_daily
    op.create_table(
        "analytics_daily",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("total_sessions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_sessions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("abandoned_sessions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_duration_seconds", sa.Float(), nullable=True),
        sa.Column("languages_used", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("intents_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sentiments_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("offline_sessions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pii_detected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_suggestion_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_suggestion_edited", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_suggestion_ignored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], ondelete="CASCADE", name="fk_analytics_branch"
        ),
        sa.UniqueConstraint("branch_id", "date", name="uq_branch_date"),
    )
    op.create_index("ix_analytics_daily_branch_id", "analytics_daily", ["branch_id"])
    op.create_index("ix_analytics_daily_date", "analytics_daily", ["date"])
    op.create_index(
        "ix_analytics_daily_branch_date",
        "analytics_daily",
        ["branch_id", "date"],
        unique=True,
    )


# DOWNGRADE — drop in exact reverse FK order

def downgrade() -> None:
    # 9 → 1 reverse order
    op.drop_table("analytics_daily")
    op.drop_table("pii_logs")
    op.drop_table("bilingual_summaries")
    op.drop_table("session_process_tracking")
    op.drop_table("process_steps")
    op.drop_table("exchanges")
    op.drop_table("sessions")
    op.drop_table("staff_members")
    op.drop_table("branches")