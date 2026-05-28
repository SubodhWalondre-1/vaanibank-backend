"""merge_002_and_004 — Merge the two parallel branches into one linear chain

VaaniBank AI | PSBs Hackathon 2026 | Team Vectora

Context:
  The migration history has two legitimate parallel branches that both
  stem from e4ef62d0f1cb:

    Branch A:  001 → e4ef62d0f1cb → 002                   (audit_logs + indexes)
    Branch B:  001 → e4ef62d0f1cb → 003 → 004_perf_indexes (collected_data + perf)

  Both branches must be applied before we can add new columns (005).
  This merge revision has both branch tips as its down_revision so Alembic
  treats them as a single head going forward.

  Final chain after this merge:
    001 → e4ef62d0f1cb → 003 → 004_perf_indexes ─┐
                       → 002 ──────────────────────┴→ merge_002_004 → 005_add_form_signed_at

Revision ID: merge_002_004
Revises: 002, 004_perf_indexes
Create Date: 2026-05-24
"""

from __future__ import annotations

from alembic import op

# ── Revision identifiers ──────────────────────────────────────────────────────
revision: str = "merge_002_004"
down_revision = ("002", "004_perf_indexes")   # tuple = merge point
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge revisions only — no schema changes needed.
    pass


def downgrade() -> None:
    # Merge revisions only — no schema changes to reverse.
    pass
