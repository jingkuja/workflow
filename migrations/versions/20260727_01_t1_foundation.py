"""T1 foundation schema.

Revision ID: 20260727_01
Revises:

显式 DDL，反映该 revision 时的历史 schema；不使用 Base.metadata.create_all，
避免后续模型变更回溯性地改变早期迁移的行为。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260727_01"
down_revision = None
branch_labels = None
depends_on = None

_ACTOR_ROLE = sa.Enum("BOSS", "EMPLOYEE", name="actor_role")
_ATTACHMENT_STATUS = sa.Enum(
    "PENDING", "READY", "UNAVAILABLE", "FAILED", name="attachment_status"
)
_JOB_STATUS = sa.Enum(
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "DEAD", name="background_job_status"
)
_NOTIFICATION_STATUS = sa.Enum(
    "PENDING", "SENDING", "SENT", "FAILED", "DEAD", name="notification_status"
)

_TIMESTAMPS = (
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def upgrade() -> None:
    op.create_table(
        "actor_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role", _ACTOR_ROLE, nullable=False),
        sa.Column("position", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("wecom_userid", sa.String(length=128), nullable=True),
        sa.Column("token_sha256", sa.String(length=64), nullable=False, unique=True),
        *_TIMESTAMPS,
        sa.UniqueConstraint("company_id", "role", "display_name", name="uq_actor_identity"),
    )
    op.create_index("ix_actor_company_active", "actor_profiles", ["company_id", "active"])

    op.create_table(
        "content_projects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_TIMESTAMPS,
    )
    op.create_index(
        "ix_project_company_status",
        "content_projects",
        ["company_id", "status", "created_at"],
    )

    op.create_table(
        "stage_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("content_projects.id"),
            nullable=False,
        ),
        sa.Column("task_no", sa.String(length=40), nullable=False, unique=True),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "assignee_id", sa.String(length=36), sa.ForeignKey("actor_profiles.id"), nullable=True
        ),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="NORMAL"),
        sa.Column("effective_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancelled_by", sa.String(length=36), sa.ForeignKey("actor_profiles.id"), nullable=True
        ),
        *_TIMESTAMPS,
    )
    op.create_index(
        "ix_task_assignee_status",
        "stage_tasks",
        ["company_id", "assignee_id", "status", "priority"],
    )

    op.create_table(
        "task_assignments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "task_id", sa.String(length=36), sa.ForeignKey("stage_tasks.id"), nullable=False
        ),
        sa.Column(
            "assignee_id",
            sa.String(length=36),
            sa.ForeignKey("actor_profiles.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_assignment_actor_time", "task_assignments", ["assignee_id", "assigned_at"]
    )

    op.create_table(
        "attachments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("opaque_file_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column(
            "storage_provider", sa.String(length=32), nullable=False, server_default="LOCAL"
        ),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", _ATTACHMENT_STATUS, nullable=False),
        *_TIMESTAMPS,
    )
    op.create_index("ix_attachment_company_sha", "attachments", ["company_id", "sha256"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "actor_id", sa.String(length=36), sa.ForeignKey("actor_profiles.id"), nullable=True
        ),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.String(length=64), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audit_object_time", "audit_events", ["object_type", "object_id", "created_at"]
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column(
            "actor_id", sa.String(length=36), sa.ForeignKey("actor_profiles.id"), nullable=False
        ),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "company_id",
            "actor_id",
            "tool",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
    )

    op.create_table(
        "background_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("job_type", sa.String(length=128), nullable=False),
        sa.Column("object_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", _JOB_STATUS, nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_by", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_TIMESTAMPS,
    )
    op.create_index("ix_job_claim", "background_jobs", ["status", "available_at"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("template", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("mentioned_userids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", _NOTIFICATION_STATUS, nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_summary", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_TIMESTAMPS,
    )
    op.create_index("ix_notification_retry", "notifications", ["status", "next_retry_at"])


def downgrade() -> None:
    for table in (
        "notifications",
        "background_jobs",
        "idempotency_records",
        "audit_events",
        "attachments",
        "task_assignments",
        "stage_tasks",
        "content_projects",
        "actor_profiles",
    ):
        op.drop_table(table)
    for enum_type in (
        "notification_status",
        "background_job_status",
        "attachment_status",
        "actor_role",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")
