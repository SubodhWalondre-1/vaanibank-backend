"""Add collected_data JSONB column to sessions table

Revision ID: 003_collected_data
Revises: e4ef62d0f1cb
Create Date: 2026-05-15

Stores accumulated AI-extracted customer information (collected_info)
across all exchanges in a session, solving the "repeated questions" bug.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "003_collected_data"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("collected_data", JSONB, nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("sessions", "collected_data")
