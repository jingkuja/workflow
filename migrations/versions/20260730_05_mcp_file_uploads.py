"""Add temporary MCP file uploads.

Revision ID: 20260730_05
Revises: 20260728_04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260730_05"
down_revision = "20260728_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_file_uploads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_by",
            sa.String(length=36),
            sa.ForeignKey("actor_profiles.id"),
            nullable=False,
        ),
        sa.Column("file_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "storage_provider",
            sa.String(length=32),
            nullable=False,
            server_default="LOCAL",
        ),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_mcp_upload_owner_expiry",
        "mcp_file_uploads",
        ["company_id", "uploaded_by", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_upload_owner_expiry", table_name="mcp_file_uploads")
    op.drop_table("mcp_file_uploads")
