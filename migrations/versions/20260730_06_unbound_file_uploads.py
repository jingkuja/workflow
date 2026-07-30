"""Allow temporary uploads to be claimed on first business use.

Revision ID: 20260730_06
Revises: 20260730_05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260730_06"
down_revision = "20260730_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "mcp_file_uploads",
        "company_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.alter_column(
        "mcp_file_uploads",
        "uploaded_by",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM mcp_file_uploads "
            "WHERE company_id IS NULL OR uploaded_by IS NULL"
        )
    )
    op.alter_column(
        "mcp_file_uploads",
        "uploaded_by",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "mcp_file_uploads",
        "company_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
