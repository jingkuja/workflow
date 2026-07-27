"""Complete T1 domain foundation tables.

Revision ID: 20260727_02
Revises: 20260727_01
"""

from __future__ import annotations

from alembic import op

from workflow.db.models import Base

revision = "20260727_02"
down_revision = "20260727_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in ("blockers", "publish_records", "reviews", "submissions", "import_batches"):
        table = Base.metadata.tables[name]
        table.drop(bind=op.get_bind(), checkfirst=True)
