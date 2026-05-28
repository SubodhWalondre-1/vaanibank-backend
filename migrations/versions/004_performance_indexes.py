"""004 — Performance composite indexes (P0 audit fix)

Adds composite indexes on hot query paths:
  - exchanges(session_id, exchange_number)  — used by pipeline history lookups
  - session_process_tracking(session_id, status) — used by progress counting

Revision ID: 004_perf_indexes
Revises: 003_collected_data
"""

from alembic import op

# revision identifiers
revision = "004_perf_indexes"
down_revision = "003_collected_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index on Exchange for pipeline history fetches:
    #   SELECT ... FROM exchanges WHERE session_id=? ORDER BY exchange_number DESC LIMIT 6
    op.create_index(
        "ix_exchange_session_number",
        "exchanges",
        ["session_id", "exchange_number"],
        unique=False,
        if_not_exists=True,
    )

    # Composite index on SessionProcessTracking for progress counting:
    #   SELECT COUNT(*) FROM session_process_tracking WHERE session_id=? AND status=?
    op.create_index(
        "ix_tracking_session_status",
        "session_process_tracking",
        ["session_id", "status"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_tracking_session_status", table_name="session_process_tracking")
    op.drop_index("ix_exchange_session_number", table_name="exchanges")
