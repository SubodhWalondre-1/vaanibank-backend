"""005 — Add form_signed_at to sessions (SaralForm Phase 1)

VaaniBank AI | PSBs Hackathon 2026 | Team Vectora

Adds the `form_signed_at` column to the `sessions` table.

  NULL  → customer did not complete SaralForm (pre-feature sessions, or
          sessions where session ended before reaching the form step)
  value → UTC timestamp of the successful POST /forms/submit call

Revision ID: 005_add_form_signed_at
Revises: merge_002_004
Create Date: 2026-05-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# ── Revision identifiers ──────────────────────────────────────────────────────
# Full linear chain after merge:
#   001 → e4ef62d0f1cb → [003 → 004, 002] → merge_002_004 → 005_add_form_signed_at
revision: str = "005_add_form_signed_at"
down_revision: str = "merge_002_004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add form_signed_at (nullable TIMESTAMP WITH TIME ZONE) to sessions.

    nullable=True with no server_default = pure catalog change on PostgreSQL.
    No table rewrite, no row lock — safe on production tables of any size.
    """
    op.add_column(
        "sessions",
        sa.Column(
            "form_signed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "UTC timestamp when the customer submitted the signed SaralForm "
                "via POST /forms/submit.  NULL = form not yet signed."
            ),
        ),
    )


def downgrade() -> None:
    """Remove form_signed_at from sessions — fully reversible."""
    op.drop_column("sessions", "form_signed_at")
