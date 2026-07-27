"""T1 foundation schema.

Revision ID: 20260727_01
Revises:
"""

from __future__ import annotations

from alembic import op

from workflow.db.models import Base

revision = "20260727_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
