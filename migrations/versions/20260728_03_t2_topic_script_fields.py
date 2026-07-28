"""T2 topic and script workflow fields.

Revision ID: 20260728_03
Revises: 20260727_02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_03"
down_revision = "20260727_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    project_columns = {column["name"] for column in inspector.get_columns("content_projects")}
    if "import_batch_id" not in project_columns:
        op.add_column(
            "content_projects",
            sa.Column("import_batch_id", sa.String(length=36), nullable=True),
        )
        op.create_foreign_key(
            "fk_project_import_batch",
            "content_projects",
            "import_batches",
            ["import_batch_id"],
            ["id"],
        )
    if "source_sequence" not in project_columns:
        op.add_column("content_projects", sa.Column("source_sequence", sa.Integer()))
    if "source_content" not in project_columns:
        op.add_column(
            "content_projects",
            sa.Column("source_content", sa.JSON(), nullable=False, server_default="{}"),
        )

    assignment_columns = {column["name"] for column in inspector.get_columns("task_assignments")}
    if "workload_delta" not in assignment_columns:
        op.add_column(
            "task_assignments",
            sa.Column("workload_delta", sa.Integer(), nullable=False, server_default="1"),
        )
    if "work_week_start" not in assignment_columns:
        op.add_column(
            "task_assignments",
            sa.Column("work_week_start", sa.Date(), nullable=True),
        )
        op.execute(
            """
            UPDATE task_assignments
            SET work_week_start =
              (assigned_at AT TIME ZONE 'Asia/Shanghai')::date
              - EXTRACT(ISODOW FROM assigned_at AT TIME ZONE 'Asia/Shanghai')::int + 1
            """
        )
        op.alter_column("task_assignments", "work_week_start", nullable=False)
    if "task_number_counters" not in inspector.get_table_names():
        op.create_table(
            "task_number_counters",
            sa.Column("counter_date", sa.Date(), nullable=False),
            sa.Column("last_value", sa.Integer(), nullable=False, server_default="0"),
            sa.PrimaryKeyConstraint("counter_date"),
        )


def downgrade() -> None:
    op.drop_table("task_number_counters")
    op.drop_column("task_assignments", "work_week_start")
    op.drop_column("task_assignments", "workload_delta")
    op.drop_constraint("fk_project_import_batch", "content_projects", type_="foreignkey")
    op.drop_column("content_projects", "source_content")
    op.drop_column("content_projects", "source_sequence")
    op.drop_column("content_projects", "import_batch_id")
