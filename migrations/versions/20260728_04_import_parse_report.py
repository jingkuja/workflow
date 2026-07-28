"""Add parse_report column to import_batches.

Revision ID: 20260728_04
Revises: 20260728_03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_04"
down_revision = "20260728_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("import_batches")}
    if "parse_report" not in columns:
        op.add_column(
            "import_batches",
            sa.Column("parse_report", sa.JSON(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    op.drop_column("import_batches", "parse_report")
