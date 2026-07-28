"""Complete T1 domain foundation tables.

Revision ID: 20260727_02
Revises: 20260727_01

显式 DDL，反映该 revision 时的历史 schema（T2 字段由 20260728_03 扩展，
parse_report 由 20260728_04 扩展）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260727_02"
down_revision = "20260727_01"
branch_labels = None
depends_on = None

_TIMESTAMPS = (
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "source_attachment_id",
            sa.String(length=36),
            sa.ForeignKey("attachments.id"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        *_TIMESTAMPS,
        sa.UniqueConstraint("company_id", "sha256", name="uq_import_company_sha"),
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "task_id", sa.String(length=36), sa.ForeignKey("stage_tasks.id"), nullable=False
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "submitted_by",
            sa.String(length=36),
            sa.ForeignKey("actor_profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "attachment_id", sa.String(length=36), sa.ForeignKey("attachments.id"), nullable=True
        ),
        sa.Column("note", sa.Text(), nullable=True),
        *_TIMESTAMPS,
        sa.UniqueConstraint("task_id", "version_no", name="uq_submission_task_version"),
    )

    op.create_table(
        "reviews",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "submission_id",
            sa.String(length=36),
            sa.ForeignKey("submissions.id"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_id",
            sa.String(length=36),
            sa.ForeignKey("actor_profiles.id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason_category", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        *_TIMESTAMPS,
        sa.UniqueConstraint("submission_id", name="uq_review_submission"),
    )

    op.create_table(
        "publish_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "task_id",
            sa.String(length=36),
            sa.ForeignKey("stage_tasks.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("completion_note", sa.Text(), nullable=False),
        sa.Column("extension_data", sa.JSON(), nullable=False, server_default="{}"),
        *_TIMESTAMPS,
    )

    op.create_table(
        "blockers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "task_id", sa.String(length=36), sa.ForeignKey("stage_tasks.id"), nullable=False
        ),
        sa.Column("blocker_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "reported_by",
            sa.String(length=36),
            sa.ForeignKey("actor_profiles.id"),
            nullable=False,
        ),
        *_TIMESTAMPS,
    )
    op.create_index("ix_blocker_task_status", "blockers", ["task_id", "status"])


def downgrade() -> None:
    for table in ("blockers", "publish_records", "reviews", "submissions", "import_batches"):
        op.drop_table(table)
