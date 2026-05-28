"""002 — Manager Panel DB additions

  1. audit_logs table              → persistent audit trail (replaces in-memory list)
  2. ix_staff_members_role         → fast role-filter queries
  3. ix_staff_members_is_active    → fast is_active filter
  4. ix_staff_members_branch_active → composite branch+active index
  5. ix_sessions_branch_created    → fast branch+date analytics queries
  6. ix_sessions_branch_status     → fast branch+status filter
  7. ix_sessions_staff_created     → fast staff session history queries

Revision ID: 002
Revises: e4ef62d0f1cb
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# ── Revision identifiers ──────────────────────────────────────────────────────
# 002 branches off e4ef62d0f1cb (parallel to 003/004 chain).
# This is intentional — both branches must be merged before adding new columns.
# Use: alembic upgrade heads   (applies both branch tips)
# Then: alembic merge heads -m "merge_002_and_004"  (creates a merge point)
revision: str = "002"
down_revision: str = "e4ef62d0f1cb"   # restored to original
branch_labels = None
depends_on = None


def upgrade() -> None:

    # 1. audit_logs — persistent replacement for the in-memory _AUDIT_LOG list
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("actor_id",       sa.Integer(),     nullable=True),
        sa.Column("actor_staff_id", sa.String(50),    nullable=False),
        sa.Column("actor_name",     sa.String(200),   nullable=False),
        sa.Column("actor_role",     sa.String(20),    nullable=False),
        sa.Column("action",  sa.String(50), nullable=False),
        sa.Column("detail",  sa.Text(),     nullable=True),
        sa.Column("target_id",       sa.Integer(),    nullable=True),
        sa.Column("target_staff_id", sa.String(50),   nullable=True),
        sa.Column("target_name",     sa.String(200),  nullable=True),
        sa.Column("branch_id",   sa.Integer(),   nullable=True),
        sa.Column("branch_code", sa.String(50),  nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],  ["staff_members.id"], ondelete="SET NULL", name="fk_audit_actor",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["staff_members.id"], ondelete="SET NULL", name="fk_audit_target",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"],      ondelete="SET NULL", name="fk_audit_branch",
        ),
    )
    op.create_index("ix_audit_logs_actor_id",   "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_branch_id",  "audit_logs", ["branch_id"])
    op.create_index("ix_audit_logs_action",     "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # 2. staff_members performance indexes
    op.create_index("ix_staff_members_role",          "staff_members", ["role"])
    op.create_index("ix_staff_members_is_active",     "staff_members", ["is_active"])
    op.create_index("ix_staff_members_branch_active", "staff_members", ["branch_id", "is_active"])

    # 3. sessions performance indexes
    op.create_index("ix_sessions_branch_created", "sessions", ["branch_id", "created_at"])
    op.create_index("ix_sessions_branch_status",  "sessions", ["branch_id", "status"])
    op.create_index("ix_sessions_staff_created",  "sessions", ["staff_id",  "created_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_staff_created",         table_name="sessions")
    op.drop_index("ix_sessions_branch_status",         table_name="sessions")
    op.drop_index("ix_sessions_branch_created",        table_name="sessions")
    op.drop_index("ix_staff_members_branch_active",    table_name="staff_members")
    op.drop_index("ix_staff_members_is_active",        table_name="staff_members")
    op.drop_index("ix_staff_members_role",             table_name="staff_members")
    op.drop_index("ix_audit_logs_created_at",          table_name="audit_logs")
    op.drop_index("ix_audit_logs_action",              table_name="audit_logs")
    op.drop_index("ix_audit_logs_branch_id",           table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id",            table_name="audit_logs")
    op.drop_table("audit_logs")
