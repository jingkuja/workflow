from __future__ import annotations

import hashlib
import io
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from workflow.audit import append_audit
from workflow.config import Settings
from workflow.db.models import (
    ActorProfile,
    Attachment,
    AttachmentStatus,
    AuditEvent,
    BackgroundJob,
    Blocker,
    ContentProject,
    ImportBatch,
    JobStatus,
    Notification,
    NotificationStatus,
    Review,
    Role,
    StageTask,
    Submission,
    TaskAssignment,
)
from workflow.db.session import create_engine_from_settings, make_session_factory, session_scope
from workflow.errors import (
    Forbidden,
    InvalidArgument,
    InvalidStateTransition,
    ResourceNotFound,
    WorkflowError,
)
from workflow.idempotency import replay_or_none, save_response
from workflow.request_context import current_request_id
from workflow.storage import LocalStorage
from workflow.t2.allocation import (
    advisory_lock,
    choose_employee,
    employee_loads,
    next_task_number,
)
from workflow.t2.calendar import effective_started_at, week_start_for
from workflow.t2.contracts import StructuredTopicInput
from workflow.t2.files import DOCUMENT_MIME_TYPES, receive_document
from workflow.t2.parser import TopicParseError, parse_topic_document

TOPIC_EXTENSIONS = {".docx"}
SCRIPT_EXTENSIONS = {".docx", ".pdf", ".md", ".txt"}

logger = logging.getLogger("workflow.t2")


@dataclass(frozen=True, slots=True)
class _ImportTopic:
    source_sequence: int
    title: str
    source_content: dict[str, Any]


class T2Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine_from_settings(settings)
        self.sessions = make_session_factory(self.engine)
        self.storage = LocalStorage(settings.file_data_dir, settings.disk_reject_percent)

    def import_topics(
        self,
        *,
        actor_name: str,
        original_filename: str,
        idempotency_key: str,
        content_base64: str | None,
        file_url: str | None,
    ) -> dict[str, Any]:
        content = receive_document(
            filename=original_filename,
            allowed=TOPIC_EXTENSIONS,
            max_bytes=self.settings.max_topic_document_bytes,
            content_base64=content_base64,
            file_url=file_url,
        )
        sha256 = hashlib.sha256(content).hexdigest()
        try:
            parse_result = parse_topic_document(content)
        except TopicParseError as exc:
            raise InvalidArgument(str(exc)) from exc
        topics = parse_result.topics
        failure_report = [
            {
                "source_sequence": failure.source_sequence,
                "heading_title": failure.heading_title,
                "reason": failure.reason,
            }
            for failure in parse_result.failures
        ]
        prepared_topics = [
            _ImportTopic(
                source_sequence=topic.source_sequence,
                title=topic.title,
                source_content=topic.source_content,
            )
            for topic in topics
        ]
        return self._persist_topic_import(
            actor_name=actor_name,
            tool_name="import_topic_document",
            original_filename=original_filename,
            idempotency_key=idempotency_key,
            content=content,
            sha256=sha256,
            topics=prepared_topics,
            parse_report={
                "import_mode": "RULE_BASED",
                "schema_version": None,
                "warnings": [],
                "failures": failure_report,
            },
        )

    def import_structured_topics(
        self,
        *,
        actor_name: str,
        original_filename: str,
        idempotency_key: str,
        topics: list[StructuredTopicInput],
        warnings: list[str],
        schema_version: str,
        content_base64: str | None,
        file_url: str | None,
    ) -> dict[str, Any]:
        content = receive_document(
            filename=original_filename,
            allowed=TOPIC_EXTENSIONS,
            max_bytes=self.settings.max_topic_document_bytes,
            content_base64=content_base64,
            file_url=file_url,
        )
        source_sha256 = hashlib.sha256(content).hexdigest()
        prepared_topics = [
            _ImportTopic(
                source_sequence=ordinal,
                title=topic.title,
                source_content={
                    "extraction_mode": "WORKBUDDY_STRUCTURED",
                    "source_index": topic.source_index,
                    "source_text": topic.source_text,
                    "script": topic.script,
                    "confidence": topic.confidence,
                    "evidence": topic.evidence,
                    "source_verification": "SKIPPED",
                },
            )
            for ordinal, topic in enumerate(topics, start=1)
        ]

        request_topics = [topic.model_dump(mode="json") for topic in topics]
        # 结构化导入的任务事实由 MCP 结果决定。同一原文件可能被重新抽取为不同
        # 任务集合，因此批次去重必须同时包含文件内容和结构化任务，而不能只看文件。
        batch_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "source_sha256": source_sha256,
                    "schema_version": schema_version,
                    "topics": request_topics,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return self._persist_topic_import(
            actor_name=actor_name,
            tool_name="import_structured_topics",
            original_filename=original_filename,
            idempotency_key=idempotency_key,
            content=content,
            sha256=batch_sha256,
            topics=prepared_topics,
            parse_report={
                "import_mode": "WORKBUDDY_STRUCTURED",
                "schema_version": schema_version,
                "warnings": warnings,
                "failures": [],
                "source_verification": "SKIPPED",
                "source_sha256": source_sha256,
                "topic_count": len(prepared_topics),
            },
            request_payload={
                "filename": original_filename,
                "source_sha256": source_sha256,
                "batch_sha256": batch_sha256,
                "schema_version": schema_version,
                "topics": request_topics,
                "warnings": warnings,
            },
        )

    def _persist_topic_import(
        self,
        *,
        actor_name: str,
        tool_name: str,
        original_filename: str,
        idempotency_key: str,
        content: bytes,
        sha256: str,
        topics: list[_ImportTopic],
        parse_report: dict[str, Any],
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = request_payload or {"filename": original_filename, "sha256": sha256}
        failure_report = list(parse_report.get("failures", []))
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            replay = self._replay(
                session,
                actor,
                tool_name,
                idempotency_key,
                payload,
            )
            if replay is not None:
                return replay
            advisory_lock(session, f"import:{actor.company_id}:{sha256}")
            existing = session.scalar(
                select(ImportBatch).where(
                    ImportBatch.company_id == actor.company_id,
                    ImportBatch.sha256 == sha256,
                )
            )
            if existing is not None:
                response = self._import_response(session, existing, True, 0)
                replayed = self._save_idempotency(
                    session,
                    actor,
                    tool_name,
                    idempotency_key,
                    payload,
                    response,
                )
                return replayed if replayed is not None else response

            now = datetime.now(UTC)
            attachment = self._store_attachment(
                session,
                company_id=actor.company_id,
                purpose="topic-source",
                original_filename=original_filename,
                content=content,
            )
            batch = ImportBatch(
                company_id=actor.company_id,
                source_attachment_id=attachment.id,
                sha256=sha256,
                parse_status="PROCESSING",
                parse_report=parse_report,
            )
            session.add(batch)
            session.flush()
            effective = effective_started_at(
                now,
                timezone=self.settings.app_timezone,
                start_hour=self.settings.workday_start_hour,
                end_hour=self.settings.workday_end_hour,
            )
            advisory_lock(
                session,
                (
                    f"allocation:{actor.company_id}:"
                    f"{week_start_for(effective, self.settings.app_timezone)}"
                ),
            )
            created = 0
            pending_assignment = 0
            for topic in topics:
                loads = employee_loads(
                    session, actor.company_id, effective, self.settings.app_timezone
                )
                # 规格 §4.3：暂时没有可用员工时任务进入 PENDING_ASSIGNMENT，
                # 不阻断整批导入；老板可稍后通过改派指定负责人。
                assignee = choose_employee(loads) if loads else None
                project = ContentProject(
                    company_id=actor.company_id,
                    import_batch_id=batch.id,
                    source_sequence=topic.source_sequence,
                    source_content=topic.source_content,
                    title=topic.title,
                    status="SCRIPT_IN_PROGRESS",
                )
                session.add(project)
                session.flush()
                task = StageTask(
                    company_id=actor.company_id,
                    project_id=project.id,
                    task_no=next_task_number(session, now, self.settings.app_timezone),
                    task_type="SCRIPT",
                    status="IN_PROGRESS" if assignee else "PENDING_ASSIGNMENT",
                    assignee_id=assignee.id if assignee else None,
                    effective_started_at=effective,
                )
                session.add(task)
                session.flush()
                if assignee is not None:
                    session.add(
                        TaskAssignment(
                            company_id=actor.company_id,
                            task_id=task.id,
                            assignee_id=assignee.id,
                            event_type="AUTO_ASSIGNED",
                            workload_delta=1,
                            work_week_start=week_start_for(
                                effective, self.settings.app_timezone
                            ),
                            reason="选题导入自动分配",
                            assigned_at=now,
                        )
                    )
                    self._notify(
                        session,
                        actor.company_id,
                        "TASK_CREATED",
                        f"新任务 {task.task_no}：{project.title}",
                        [assignee.wecom_userid],
                    )
                else:
                    pending_assignment += 1
                self._audit(
                    session,
                    actor,
                    "SCRIPT_TASK_CREATED",
                    "stage_task",
                    task.id,
                    {
                        "task_no": task.task_no,
                        "assignee_id": assignee.id if assignee else None,
                        "status": task.status,
                    },
                )
                created += 1
            batch.success_count = created
            batch.failure_count = len(failure_report)
            batch.parse_status = (
                "COMPLETED" if not failure_report else "PARTIAL_SUCCESS"
            ) if topics else "FAILED"
            self._audit(
                session,
                actor,
                "TOPIC_BATCH_IMPORTED",
                "import_batch",
                batch.id,
                {
                    "created_count": created,
                    "failure_count": len(failure_report),
                    "pending_assignment_count": pending_assignment,
                    "sha256": sha256,
                },
            )
            response = self._import_response(session, batch, False, created)
            replayed = self._save_idempotency(
                session,
                actor,
                tool_name,
                idempotency_key,
                payload,
                response,
            )
            return replayed if replayed is not None else response

    def list_batch(self, *, actor_name: str, import_batch_id: str) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            batch = self._batch(session, actor, import_batch_id)
            return self._import_response(session, batch, True, 0)

    def cancel_imported_task(
        self,
        *,
        actor_name: str,
        task_no: str,
        reason: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"task_no": task_no, "reason": reason}
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            replay = self._replay(session, actor, "delete_imported_task", idempotency_key, payload)
            if replay is not None:
                return replay
            task, project = self._task_project(session, actor, task_no, lock=True)
            if task.task_type != "SCRIPT" or task.status not in {
                "IN_PROGRESS",
                "REJECTED",
                "PENDING_ASSIGNMENT",
            }:
                raise InvalidStateTransition("该任务已提交或进入后续阶段，不能从导入列表删除。")
            previous_assignee = (
                session.get(ActorProfile, task.assignee_id) if task.assignee_id else None
            )
            now = datetime.now(UTC)
            before = {"status": task.status, "assignee_id": task.assignee_id}
            task.status = "CANCELLED"
            task.cancelled_at = now
            task.cancelled_by = actor.id
            project.status = "CANCELLED"
            if previous_assignee is not None and self._before_effective(task, now):
                session.add(
                    TaskAssignment(
                        company_id=actor.company_id,
                        task_id=task.id,
                        assignee_id=previous_assignee.id,
                        event_type="CANCEL_REVERSED",
                        workload_delta=-1,
                        work_week_start=week_start_for(
                            task.effective_started_at, self.settings.app_timezone
                        ),
                        reason=reason or "生效前取消",
                        assigned_at=now,
                    )
                )
            self._audit(
                session,
                actor,
                "TASK_CANCELLED",
                "stage_task",
                task.id,
                {"status": "CANCELLED", "before": before, "reason": reason},
            )
            self._notify(
                session,
                actor.company_id,
                "TASK_CANCELLED",
                f"任务 {task.task_no} 已取消",
                [previous_assignee.wecom_userid if previous_assignee else None],
            )
            batch = (
                session.get(ImportBatch, project.import_batch_id)
                if project.import_batch_id
                else None
            )
            if batch is not None:
                response: dict[str, Any] = self._import_response(session, batch, True, 0)
            else:
                response = {
                    "success": True,
                    "data": self._task_dict(session, task),
                    "user_message": "任务已取消。",
                }
            replayed = self._save_idempotency(
                session,
                actor,
                "delete_imported_task",
                idempotency_key,
                payload,
                response,
            )
            return replayed if replayed is not None else response

    def reassign(
        self,
        *,
        actor_name: str,
        task_no: str,
        new_employee_id: str,
        reason: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "task_no": task_no,
            "new_employee_id": new_employee_id,
            "reason": reason,
        }
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            replay = self._replay(session, actor, "change_task_assignee", idempotency_key, payload)
            if replay is not None:
                return replay
            task, project = self._task_project(session, actor, task_no, lock=True)
            if task.status in {"APPROVED", "COMPLETED", "CANCELLED"}:
                raise InvalidStateTransition("已结束任务不能改派。")
            employee = session.scalar(
                select(ActorProfile).where(
                    ActorProfile.id == new_employee_id,
                    ActorProfile.company_id == actor.company_id,
                    ActorProfile.role == Role.EMPLOYEE,
                    ActorProfile.active.is_(True),
                )
            )
            if employee is None:
                raise InvalidArgument("新负责人不是有效的在岗员工。")
            if employee.id == task.assignee_id:
                raise InvalidArgument("新负责人与当前负责人相同。")
            previous = session.get(ActorProfile, task.assignee_id) if task.assignee_id else None
            now = datetime.now(UTC)
            if previous is not None and self._before_effective(task, now):
                session.add(
                    TaskAssignment(
                        company_id=actor.company_id,
                        task_id=task.id,
                        assignee_id=previous.id,
                        event_type="REASSIGN_REVERSED",
                        workload_delta=-1,
                        work_week_start=week_start_for(
                            task.effective_started_at, self.settings.app_timezone
                        ),
                        reason=reason or "生效前改派",
                        assigned_at=now,
                    )
                )
            task.assignee_id = employee.id
            session.add(
                TaskAssignment(
                    company_id=actor.company_id,
                    task_id=task.id,
                    assignee_id=employee.id,
                    event_type="MANUAL_REASSIGNED",
                    workload_delta=1,
                    # 与原始分配和抵消事件使用同一口径（effective_started_at 所在周），
                    # 避免跨周改派时新员工的 +1 落入错误的周。
                    work_week_start=week_start_for(
                        task.effective_started_at or now, self.settings.app_timezone
                    ),
                    reason=reason,
                    assigned_at=now,
                )
            )
            self._audit(
                session,
                actor,
                "TASK_REASSIGNED",
                "stage_task",
                task.id,
                {
                    "from": previous.id if previous else None,
                    "to": employee.id,
                    "reason": reason,
                },
            )
            self._notify(
                session,
                actor.company_id,
                "TASK_REASSIGNED",
                f"任务 {task.task_no} 已改派给 {employee.display_name}",
                [
                    previous.wecom_userid if previous else None,
                    employee.wecom_userid,
                ],
            )
            batch = (
                session.get(ImportBatch, project.import_batch_id)
                if project.import_batch_id
                else None
            )
            if batch is not None:
                response = self._import_response(session, batch, True, 0)
            else:
                response = {
                    "success": True,
                    "data": self._task_dict(session, task),
                    "user_message": f"任务已改派给 {employee.display_name}。",
                }
            replayed = self._save_idempotency(
                session,
                actor,
                "change_task_assignee",
                idempotency_key,
                payload,
                response,
            )
            return replayed if replayed is not None else response

    def set_priority(
        self,
        *,
        actor_name: str,
        task_no: str,
        priority: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {"task_no": task_no, "priority": priority}
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            replay = self._replay(session, actor, "set_task_priority", idempotency_key, payload)
            if replay is not None:
                return replay
            task, _ = self._task_project(session, actor, task_no, lock=True)
            if task.status == "CANCELLED":
                raise InvalidStateTransition("已取消任务不能修改优先级。")
            task.priority = "PRIORITY" if priority else "NORMAL"
            self._audit(
                session,
                actor,
                "TASK_PRIORITY_CHANGED",
                "stage_task",
                task.id,
                {"priority": task.priority},
            )
            assignee = (
                session.get(ActorProfile, task.assignee_id) if task.assignee_id else None
            )
            self._notify(
                session,
                actor.company_id,
                "TASK_PRIORITY_CHANGED",
                (
                    f"任务 {task.task_no} 已设为优先处理"
                    if priority
                    else f"任务 {task.task_no} 已取消优先处理"
                ),
                [assignee.wecom_userid if assignee else None],
            )
            response = {
                "success": True,
                "data": self._task_dict(session, task),
                "user_message": "任务优先级已更新。",
            }
            replayed = self._save_idempotency(
                session,
                actor,
                "set_task_priority",
                idempotency_key,
                payload,
                response,
            )
            return replayed if replayed is not None else response

    def list_employees(self, *, actor_name: str) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            now = datetime.now(UTC)
            load_by_id = {
                employee.id: count
                for employee, count in employee_loads(
                    session, actor.company_id, now, self.settings.app_timezone
                )
            }
            employees = session.scalars(
                select(ActorProfile)
                .where(
                    ActorProfile.company_id == actor.company_id,
                    ActorProfile.role == Role.EMPLOYEE,
                )
                .order_by(ActorProfile.active.desc(), ActorProfile.display_name, ActorProfile.id)
            ).all()
            tasks = session.scalars(
                select(StageTask).where(StageTask.company_id == actor.company_id)
            ).all()
            submissions = session.scalars(
                select(Submission).where(Submission.company_id == actor.company_id)
            ).all()
            reviews = session.scalars(
                select(Review).where(Review.company_id == actor.company_id)
            ).all()
            reviews_by_submission = {review.submission_id: review for review in reviews}
            submissions_by_employee: dict[str, list[Submission]] = {}
            for submission in submissions:
                submissions_by_employee.setdefault(submission.submitted_by, []).append(submission)
            rows: list[dict[str, Any]] = []
            terminal = {"APPROVED", "COMPLETED", "CANCELLED"}
            for employee in employees:
                employee_submissions = submissions_by_employee.get(employee.id, [])
                first_versions = [
                    item for item in employee_submissions if item.version_no == 1
                ]
                reviewed_first = [
                    reviews_by_submission[item.id]
                    for item in first_versions
                    if item.id in reviews_by_submission
                ]
                approved = [
                    reviews_by_submission[item.id]
                    for item in employee_submissions
                    if item.id in reviews_by_submission
                    and reviews_by_submission[item.id].decision == "APPROVED"
                ]
                approval_hours = []
                submission_by_id = {item.id: item for item in employee_submissions}
                for review in approved:
                    submitted_at = submission_by_id[review.submission_id].created_at
                    approval_hours.append(
                        max(
                            0.0,
                            (
                                self._aware(review.created_at)
                                - self._aware(submitted_at)
                            ).total_seconds()
                            / 3600,
                        )
                    )
                rows.append(
                    {
                        "employee_id": employee.id,
                        "display_name": employee.display_name,
                        "active": employee.active,
                        "wecom_bound": bool(employee.wecom_userid),
                        "weekly_task_count": load_by_id.get(employee.id, 0),
                        "current_task_count": sum(
                            1
                            for task in tasks
                            if task.assignee_id == employee.id and task.status not in terminal
                        ),
                        "average_review_hours": round(
                            sum(approval_hours) / len(approval_hours), 2
                        )
                        if approval_hours
                        else None,
                        "first_pass_rate": round(
                            sum(
                                1
                                for review in reviewed_first
                                if review.decision == "APPROVED"
                            )
                            / len(reviewed_first),
                            4,
                        )
                        if reviewed_first
                        else None,
                    }
                )
            return {
                "success": True,
                "data": rows,
                "user_message": f"共 {len(rows)} 名员工，其中 "
                f"{sum(1 for item in rows if item['active'])} 名在岗。",
                "next_actions": ["get_workflow_dashboard", "list_content_projects"],
            }

    def list_projects(
        self,
        *,
        actor_name: str,
        status: str | None = None,
        assignee_id: str | None = None,
        priority: bool | None = None,
        import_batch_id: str | None = None,
        keyword: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self._validate_page(page, page_size)
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            query = (
                select(ContentProject)
                .join(StageTask, StageTask.project_id == ContentProject.id)
                .where(ContentProject.company_id == actor.company_id)
            )
            if status:
                query = query.where(ContentProject.status == status)
            if assignee_id:
                query = query.where(StageTask.assignee_id == assignee_id)
            if priority is not None:
                query = query.where(
                    StageTask.priority == ("PRIORITY" if priority else "NORMAL")
                )
            if import_batch_id:
                query = query.where(ContentProject.import_batch_id == import_batch_id)
            if keyword:
                query = query.where(ContentProject.title.ilike(f"%{keyword.strip()}%"))
            if created_from:
                query = query.where(ContentProject.created_at >= created_from)
            if created_to:
                query = query.where(ContentProject.created_at <= created_to)
            total = session.scalar(
                select(func.count()).select_from(query.order_by(None).subquery())
            ) or 0
            projects = session.scalars(
                query.order_by(ContentProject.created_at.desc(), ContentProject.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "success": True,
                "data": [self._project_dict(session, project) for project in projects],
                "pagination": self._pagination(page, page_size, total),
                "user_message": f"查询到 {len(projects)} 个内容项目。",
                "next_actions": self._list_next_actions(page, page_size, total),
            }

    def dashboard(self, *, actor_name: str) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            now = datetime.now(UTC)
            timezone = ZoneInfo(self.settings.app_timezone)
            local_now = now.astimezone(timezone)
            today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            week_start = (today_start - timedelta(days=today_start.weekday())).astimezone(UTC)
            today_start_utc = today_start.astimezone(UTC)

            projects = session.scalars(
                select(ContentProject).where(ContentProject.company_id == actor.company_id)
            ).all()
            tasks = session.scalars(
                select(StageTask).where(StageTask.company_id == actor.company_id)
            ).all()
            submissions = session.scalars(
                select(Submission).where(Submission.company_id == actor.company_id)
            ).all()
            reviews = session.scalars(
                select(Review).where(Review.company_id == actor.company_id)
            ).all()
            open_blockers = session.scalars(
                select(Blocker).where(
                    Blocker.company_id == actor.company_id,
                    Blocker.status == "OPEN",
                )
            ).all()

            stage_counts: dict[str, int] = {}
            for project in projects:
                stage_counts[project.status] = stage_counts.get(project.status, 0) + 1
            backlog: dict[str, int] = {}
            for task in tasks:
                if task.task_type == "SCRIPT":
                    backlog[task.status] = backlog.get(task.status, 0) + 1
            approved_reviews = [
                review for review in reviews if review.decision == "APPROVED"
            ]
            approved_today = sum(
                1
                for review in approved_reviews
                if self._aware(review.created_at) >= today_start_utc
            )
            approved_week = sum(
                1
                for review in approved_reviews
                if self._aware(review.created_at) >= week_start
            )
            reviewed_first = []
            submission_by_id = {item.id: item for item in submissions}
            task_by_id = {item.id: item for item in tasks}
            stage_hours: list[float] = []
            approved_versions: list[int] = []
            for review in approved_reviews:
                submission = submission_by_id.get(review.submission_id)
                task = task_by_id.get(submission.task_id) if submission else None
                if submission is None or task is None:
                    continue
                started_at = task.effective_started_at or task.created_at
                stage_hours.append(
                    max(
                        0.0,
                        (
                            self._aware(review.created_at)
                            - self._aware(started_at)
                        ).total_seconds()
                        / 3600,
                    )
                )
                approved_versions.append(submission.version_no)
            for review in reviews:
                submission = submission_by_id.get(review.submission_id)
                if submission is not None and submission.version_no == 1:
                    reviewed_first.append(review)
            first_pass_rate = (
                round(
                    sum(1 for review in reviewed_first if review.decision == "APPROVED")
                    / len(reviewed_first),
                    4,
                )
                if reviewed_first
                else None
            )
            priority_items = [
                self._task_dict(session, task)
                for task in sorted(
                    (
                        task
                        for task in tasks
                        if task.priority == "PRIORITY"
                        and task.status not in {"APPROVED", "CANCELLED"}
                    ),
                    key=lambda item: (
                        self._aware(item.effective_started_at or item.created_at),
                        item.task_no,
                    ),
                )[:10]
            ]
            rejected = sorted(
                (review for review in reviews if review.decision == "REJECTED"),
                key=lambda item: self._aware(item.created_at),
                reverse=True,
            )[:10]
            recent_rejections = []
            for review in rejected:
                submission = submission_by_id.get(review.submission_id)
                task = session.get(StageTask, submission.task_id) if submission else None
                recent_rejections.append(
                    {
                        "task_no": task.task_no if task else None,
                        "comment": review.comment,
                        "reason_category": review.reason_category,
                        "created_at": self._aware(review.created_at).isoformat(),
                    }
                )
            failed_job_count = session.scalar(
                select(func.count()).select_from(BackgroundJob).where(
                    BackgroundJob.company_id == actor.company_id,
                    BackgroundJob.status.in_([JobStatus.FAILED, JobStatus.DEAD]),
                )
            ) or 0
            failed_notification_count = session.scalar(
                select(func.count()).select_from(Notification).where(
                    Notification.company_id == actor.company_id,
                    Notification.status.in_(
                        [NotificationStatus.FAILED, NotificationStatus.DEAD]
                    ),
                )
            ) or 0
            employee_rows = self._employee_load_summary(session, actor.company_id, now)
            recent_blockers = []
            for blocker in sorted(
                open_blockers,
                key=lambda item: self._aware(item.created_at),
                reverse=True,
            )[:10]:
                task = task_by_id.get(blocker.task_id)
                reporter = session.get(ActorProfile, blocker.reported_by)
                recent_blockers.append(
                    {
                        "blocker_id": blocker.id,
                        "task_no": task.task_no if task else None,
                        "blocker_type": blocker.blocker_type,
                        "description": blocker.description,
                        "reported_by": reporter.display_name if reporter else None,
                        "created_at": self._aware(blocker.created_at).isoformat(),
                    }
                )
            return {
                "success": True,
                "data": {
                    "generated_at": now.isoformat(),
                    "normal_terminal_status": "WAITING_FOR_FILMING",
                    "stage_counts": stage_counts,
                    "script_backlog": backlog,
                    "today": {
                        "created_count": sum(
                            1
                            for project in projects
                            if self._aware(project.created_at) >= today_start_utc
                        ),
                        "approved_count": approved_today,
                    },
                    "this_week": {
                        "created_count": sum(
                            1
                            for project in projects
                            if self._aware(project.created_at) >= week_start
                        ),
                        "approved_count": approved_week,
                        "target_min": self.settings.weekly_target_min,
                        "target_max": self.settings.weekly_target_max,
                        "gap_to_min": max(
                            0, self.settings.weekly_target_min - approved_week
                        ),
                    },
                    "pending_review_count": backlog.get("SUBMITTED", 0),
                    "first_pass_rate": first_pass_rate,
                    "average_script_stage_hours": round(
                        sum(stage_hours) / len(stage_hours), 2
                    )
                    if stage_hours
                    else None,
                    "average_approved_version": round(
                        sum(approved_versions) / len(approved_versions), 2
                    )
                    if approved_versions
                    else None,
                    "priority_tasks": priority_items,
                    "recent_rejections": recent_rejections,
                    "open_blocker_count": len(open_blockers),
                    "recent_blockers": recent_blockers,
                    "failed_background_job_count": failed_job_count,
                    "failed_notification_count": failed_notification_count,
                    "employee_loads": employee_rows,
                },
                "user_message": "已生成 T2 工作流概览。",
                "next_actions": [
                    "list_pending_reviews",
                    "list_content_projects",
                    "list_operational_issues",
                ],
            }

    def get_project(self, *, actor_name: str, task_no: str) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            task, project = self._task_project(session, actor, task_no)
            return {
                "success": True,
                "data": self._project_detail(session, project, task),
                "user_message": f"已获取项目 {task.task_no}。",
            }

    def list_my_tasks(
        self,
        *,
        actor_name: str,
        status: str | None = None,
        priority: bool | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self._validate_page(page, page_size)
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.EMPLOYEE)
            query = select(StageTask).where(
                StageTask.company_id == actor.company_id,
                StageTask.assignee_id == actor.id,
                StageTask.status != "CANCELLED",
            )
            if status:
                query = query.where(StageTask.status == status)
            if priority is not None:
                query = query.where(
                    StageTask.priority == ("PRIORITY" if priority else "NORMAL")
                )
            total = session.scalar(
                select(func.count()).select_from(query.order_by(None).subquery())
            ) or 0
            tasks = session.scalars(
                query.order_by(
                    StageTask.priority.desc(),
                    StageTask.effective_started_at,
                    StageTask.created_at,
                    StageTask.task_no,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "success": True,
                "data": [self._task_dict(session, task) for task in tasks],
                "pagination": self._pagination(page, page_size, total),
                "user_message": f"你有 {total} 条匹配任务，本页 {len(tasks)} 条。",
                "next_actions": self._list_next_actions(page, page_size, total),
            }

    def get_my_task(self, *, actor_name: str, task_no: str) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.EMPLOYEE)
            task = session.scalar(
                select(StageTask).where(
                    StageTask.task_no == task_no,
                    StageTask.company_id == actor.company_id,
                    StageTask.assignee_id == actor.id,
                    StageTask.status != "CANCELLED",
                )
            )
            if task is None:
                raise ResourceNotFound("任务不存在或不属于当前员工。")
            project = session.get(ContentProject, task.project_id)
            return {
                "success": True,
                "data": self._project_detail(session, project, task),
                "user_message": f"已获取任务 {task.task_no}。",
            }

    def submit_script(
        self,
        *,
        actor_name: str,
        task_no: str,
        original_filename: str,
        idempotency_key: str,
        content_base64: str | None,
        file_url: str | None,
        note: str | None,
    ) -> dict[str, Any]:
        content = receive_document(
            filename=original_filename,
            allowed=SCRIPT_EXTENSIONS,
            max_bytes=self.settings.max_script_document_bytes,
            content_base64=content_base64,
            file_url=file_url,
        )
        sha256 = hashlib.sha256(content).hexdigest()
        payload = {
            "task_no": task_no,
            "filename": original_filename,
            "sha256": sha256,
            "note": note,
        }
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.EMPLOYEE)
            replay = self._replay(session, actor, "submit_script_file", idempotency_key, payload)
            if replay is not None:
                return replay
            task = session.scalar(
                select(StageTask)
                .where(
                    StageTask.task_no == task_no,
                    StageTask.company_id == actor.company_id,
                    StageTask.assignee_id == actor.id,
                )
                .with_for_update()
            )
            if task is None:
                raise ResourceNotFound("任务不存在或不属于当前员工。")
            if task.task_type != "SCRIPT" or task.status not in {
                "IN_PROGRESS",
                "REJECTED",
            }:
                raise InvalidStateTransition("当前任务状态不允许提交演播稿。")
            project = session.get(ContentProject, task.project_id)
            attachment = self._store_attachment(
                session,
                company_id=actor.company_id,
                purpose="script",
                original_filename=original_filename,
                content=content,
            )
            version_no = (
                session.scalar(
                    select(func.coalesce(func.max(Submission.version_no), 0)).where(
                        Submission.task_id == task.id
                    )
                )
                or 0
            ) + 1
            submission = Submission(
                company_id=actor.company_id,
                task_id=task.id,
                version_no=version_no,
                submitted_by=actor.id,
                attachment_id=attachment.id,
                note=note,
            )
            session.add(submission)
            task.status = "SUBMITTED"
            project.status = "SCRIPT_REVIEW"
            session.flush()
            self._audit(
                session,
                actor,
                "SCRIPT_SUBMITTED",
                "submission",
                submission.id,
                {"task_no": task.task_no, "version_no": version_no},
            )
            boss = self._boss(session, actor.company_id)
            self._notify(
                session,
                actor.company_id,
                "SCRIPT_SUBMITTED",
                f"演播稿 {task.task_no} 已提交第 {version_no} 版，等待审核",
                [boss.wecom_userid],
            )
            response = {
                "success": True,
                "data": {
                    "task_no": task.task_no,
                    "status": task.status,
                    "version_no": version_no,
                    "download_url": self._download_url(attachment),
                },
                "user_message": "演播稿已提交，等待老板审核。",
            }
            replayed = self._save_idempotency(
                session,
                actor,
                "submit_script_file",
                idempotency_key,
                payload,
                response,
            )
            return replayed if replayed is not None else response

    def pending_reviews(
        self, *, actor_name: str, page: int = 1, page_size: int = 50
    ) -> dict[str, Any]:
        self._validate_page(page, page_size)
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            query = select(StageTask).where(
                StageTask.company_id == actor.company_id,
                StageTask.task_type == "SCRIPT",
                StageTask.status == "SUBMITTED",
            )
            total = session.scalar(
                select(func.count()).select_from(query.order_by(None).subquery())
            ) or 0
            tasks = session.scalars(
                query.order_by(
                    StageTask.priority.desc(),
                    StageTask.created_at,
                    StageTask.task_no,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            rows = []
            for task in tasks:
                item = self._task_dict(session, task)
                latest = session.scalar(
                    select(Submission)
                    .where(Submission.task_id == task.id)
                    .order_by(Submission.version_no.desc())
                    .limit(1)
                )
                attachment = (
                    session.get(Attachment, latest.attachment_id)
                    if latest and latest.attachment_id
                    else None
                )
                item["latest_submission"] = (
                    {
                        "version_no": latest.version_no,
                        "note": latest.note,
                        "submitted_at": self._aware(latest.created_at).isoformat(),
                        "download_url": self._download_url(attachment)
                        if attachment
                        else None,
                    }
                    if latest
                    else None
                )
                rows.append(item)
            return {
                "success": True,
                "data": rows,
                "pagination": self._pagination(page, page_size, total),
                "user_message": f"共有 {total} 条演播稿待审核，本页 {len(rows)} 条。",
                "next_actions": self._list_next_actions(page, page_size, total)
                + (["review_script_submission"] if rows else []),
            }

    def report_blocker(
        self,
        *,
        actor_name: str,
        task_no: str,
        blocker_type: str,
        description: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        blocker_type = blocker_type.strip().upper()
        description = description.strip()
        if not blocker_type or not description:
            raise InvalidArgument("阻塞类型和说明不能为空。")
        payload = {
            "task_no": task_no,
            "blocker_type": blocker_type,
            "description": description,
        }
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.EMPLOYEE)
            replay = self._replay(
                session, actor, "report_task_blocker", idempotency_key, payload
            )
            if replay is not None:
                return replay
            task = session.scalar(
                select(StageTask)
                .where(
                    StageTask.task_no == task_no,
                    StageTask.company_id == actor.company_id,
                    StageTask.assignee_id == actor.id,
                )
                .with_for_update()
            )
            if task is None:
                raise ResourceNotFound("任务不存在或不属于当前员工。")
            if task.status in {"APPROVED", "COMPLETED", "CANCELLED"}:
                raise InvalidStateTransition("已结束任务不能上报新的阻塞事项。")
            blocker = Blocker(
                company_id=actor.company_id,
                task_id=task.id,
                blocker_type=blocker_type,
                description=description,
                status="OPEN",
                reported_by=actor.id,
            )
            session.add(blocker)
            session.flush()
            self._audit(
                session,
                actor,
                "TASK_BLOCKER_REPORTED",
                "blocker",
                blocker.id,
                {
                    "task_no": task.task_no,
                    "blocker_type": blocker_type,
                    "status": "OPEN",
                },
            )
            boss = self._boss(session, actor.company_id)
            self._notify(
                session,
                actor.company_id,
                "TASK_BLOCKER_REPORTED",
                f"任务 {task.task_no} 上报阻塞：{blocker_type}",
                [boss.wecom_userid],
            )
            response = {
                "success": True,
                "data": {
                    "blocker_id": blocker.id,
                    "task_no": task.task_no,
                    "blocker_type": blocker.blocker_type,
                    "description": blocker.description,
                    "status": blocker.status,
                },
                "user_message": "阻塞事项已记录并通知老板。",
                "next_actions": ["get_my_task"],
            }
            replayed = self._save_idempotency(
                session,
                actor,
                "report_task_blocker",
                idempotency_key,
                payload,
                response,
            )
            return replayed if replayed is not None else response

    def operational_issues(
        self,
        *,
        actor_name: str,
        issue_type: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self._validate_page(page, page_size)
        normalized_type = (issue_type or "ALL").upper()
        allowed_types = {"ALL", "BACKGROUND_JOB", "NOTIFICATION", "BLOCKER"}
        if normalized_type not in allowed_types:
            raise InvalidArgument(
                "issue_type 必须是 ALL、BACKGROUND_JOB、NOTIFICATION 或 BLOCKER。"
            )
        normalized_status = status.upper() if status else None
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            items: list[dict[str, Any]] = []
            if normalized_type in {"ALL", "BACKGROUND_JOB"}:
                jobs = session.scalars(
                    select(BackgroundJob).where(
                        BackgroundJob.company_id == actor.company_id
                    )
                ).all()
                for job in jobs:
                    item_status = job.status.value
                    if normalized_status:
                        if item_status != normalized_status:
                            continue
                    elif item_status not in {"FAILED", "DEAD"}:
                        continue
                    items.append(
                        {
                            "issue_type": "BACKGROUND_JOB",
                            "issue_id": job.id,
                            "status": item_status,
                            "job_type": job.job_type,
                            "object_id": job.object_id,
                            "attempts": job.attempts,
                            "max_attempts": job.max_attempts,
                            "last_error": job.last_error,
                            "available_at": self._aware(job.available_at).isoformat(),
                            "created_at": self._aware(job.created_at).isoformat(),
                        }
                    )
            if normalized_type in {"ALL", "NOTIFICATION"}:
                notifications = session.scalars(
                    select(Notification).where(
                        Notification.company_id == actor.company_id
                    )
                ).all()
                for notification in notifications:
                    item_status = notification.status.value
                    if normalized_status:
                        if item_status != normalized_status:
                            continue
                    elif item_status not in {"FAILED", "DEAD"}:
                        continue
                    items.append(
                        {
                            "issue_type": "NOTIFICATION",
                            "issue_id": notification.id,
                            "status": item_status,
                            "event_id": notification.event_id,
                            "template": notification.template,
                            "attempts": notification.attempts,
                            "last_error": notification.response_summary,
                            "next_retry_at": self._aware(
                                notification.next_retry_at
                            ).isoformat(),
                            "created_at": self._aware(
                                notification.created_at
                            ).isoformat(),
                        }
                    )
            if normalized_type in {"ALL", "BLOCKER"}:
                blockers = session.scalars(
                    select(Blocker).where(Blocker.company_id == actor.company_id)
                ).all()
                for blocker in blockers:
                    if normalized_status:
                        if blocker.status.upper() != normalized_status:
                            continue
                    elif blocker.status.upper() != "OPEN":
                        continue
                    task = session.get(StageTask, blocker.task_id)
                    reporter = session.get(ActorProfile, blocker.reported_by)
                    items.append(
                        {
                            "issue_type": "BLOCKER",
                            "issue_id": blocker.id,
                            "status": blocker.status,
                            "task_no": task.task_no if task else None,
                            "blocker_type": blocker.blocker_type,
                            "description": blocker.description,
                            "reported_by": reporter.display_name if reporter else None,
                            "created_at": self._aware(blocker.created_at).isoformat(),
                        }
                    )
            items.sort(key=lambda item: item["created_at"], reverse=True)
            total = len(items)
            start = (page - 1) * page_size
            page_items = items[start : start + page_size]
            return {
                "success": True,
                "data": page_items,
                "pagination": self._pagination(page, page_size, total),
                "user_message": f"共有 {total} 条匹配的运维或阻塞事项。",
                "next_actions": self._list_next_actions(page, page_size, total)
                + ["get_workflow_dashboard"],
            }

    def review_script(
        self,
        *,
        actor_name: str,
        task_no: str,
        decision: str,
        comment: str | None,
        reason_category: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        decision = decision.upper()
        if decision not in {"APPROVED", "REJECTED"}:
            raise InvalidArgument("decision 必须是 APPROVED 或 REJECTED。")
        if decision == "REJECTED" and not (comment or "").strip():
            raise InvalidArgument("驳回时必须填写修改意见。")
        payload = {
            "task_no": task_no,
            "decision": decision,
            "comment": comment,
            "reason_category": reason_category,
        }
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            replay = self._replay(
                session, actor, "review_script_submission", idempotency_key, payload
            )
            if replay is not None:
                return replay
            task, project = self._task_project(session, actor, task_no, lock=True)
            if task.task_type != "SCRIPT" or task.status != "SUBMITTED":
                raise InvalidStateTransition("当前任务没有可审核的演播稿。")
            submission = session.scalar(
                select(Submission)
                .where(Submission.task_id == task.id)
                .order_by(Submission.version_no.desc())
                .limit(1)
            )
            if submission is None:
                raise InvalidStateTransition("任务状态异常：缺少提交版本。")
            review = Review(
                company_id=actor.company_id,
                submission_id=submission.id,
                reviewer_id=actor.id,
                decision=decision,
                reason_category=reason_category,
                comment=comment,
            )
            session.add(review)
            task.status = decision
            project.status = (
                "WAITING_FOR_FILMING" if decision == "APPROVED" else "SCRIPT_IN_PROGRESS"
            )
            session.flush()
            self._audit(
                session,
                actor,
                f"SCRIPT_{decision}",
                "review",
                review.id,
                {
                    "task_no": task.task_no,
                    "submission_version": submission.version_no,
                    "comment": comment,
                },
            )
            assignee = (
                session.get(ActorProfile, task.assignee_id) if task.assignee_id else None
            )
            self._notify(
                session,
                actor.company_id,
                f"SCRIPT_{decision}",
                f"演播稿 {task.task_no} 审核结果：{decision}",
                [assignee.wecom_userid if assignee else None],
            )
            response = {
                "success": True,
                "data": {
                    "task_no": task.task_no,
                    "task_status": task.status,
                    "project_status": project.status,
                    "submission_version": submission.version_no,
                },
                "user_message": "演播稿已通过。"
                if decision == "APPROVED"
                else "演播稿已驳回，原员工可修改后重提。",
            }
            replayed = self._save_idempotency(
                session,
                actor,
                "review_script_submission",
                idempotency_key,
                payload,
                response,
            )
            return replayed if replayed is not None else response

    def _actor(self, session: Session, name: str, role: Role) -> ActorProfile:
        actor = session.scalar(
            select(ActorProfile).where(
                ActorProfile.company_id == self.settings.company_id,
                ActorProfile.display_name == name,
                ActorProfile.role == role,
                ActorProfile.active.is_(True),
            )
        )
        if actor is None:
            raise Forbidden("当前身份未同步或已停用。")
        return actor

    def _boss(self, session: Session, company_id: str) -> ActorProfile:
        boss = session.scalar(
            select(ActorProfile).where(
                ActorProfile.company_id == company_id,
                ActorProfile.role == Role.BOSS,
                ActorProfile.active.is_(True),
            )
        )
        if boss is None:
            raise InvalidStateTransition("未配置有效老板。")
        return boss

    def _batch(self, session: Session, actor: ActorProfile, batch_id: str) -> ImportBatch:
        batch = session.scalar(
            select(ImportBatch).where(
                ImportBatch.id == batch_id,
                ImportBatch.company_id == actor.company_id,
            )
        )
        if batch is None:
            raise ResourceNotFound("导入批次不存在。")
        return batch

    def _task_project(
        self,
        session: Session,
        actor: ActorProfile,
        task_no: str,
        *,
        lock: bool = False,
    ) -> tuple[StageTask, ContentProject]:
        query = select(StageTask).where(
            StageTask.task_no == task_no,
            StageTask.company_id == actor.company_id,
        )
        if lock:
            query = query.with_for_update()
        task = session.scalar(query)
        if task is None:
            raise ResourceNotFound("任务不存在。")
        project = session.get(ContentProject, task.project_id)
        if project is None:
            raise ResourceNotFound("内容项目不存在。")
        return task, project

    def _store_attachment(
        self,
        session: Session,
        *,
        company_id: str,
        purpose: str,
        original_filename: str,
        content: bytes,
    ) -> Attachment:
        attachment_id = str(uuid.uuid4())
        stored = self.storage.put(
            io.BytesIO(content),
            company_id=company_id,
            purpose=purpose,
            attachment_id=attachment_id,
        )
        extension = Path(original_filename).suffix.lower()
        attachment = Attachment(
            id=attachment_id,
            company_id=company_id,
            opaque_file_id=stored.opaque_file_id,
            purpose=purpose,
            storage_provider=stored.storage_provider,
            storage_key=stored.storage_key,
            original_filename=original_filename,
            mime_type=DOCUMENT_MIME_TYPES[extension],
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            status=AttachmentStatus.READY,
        )
        session.add(attachment)
        session.flush()
        return attachment

    def _import_response(
        self,
        session: Session,
        batch: ImportBatch | None,
        deduplicated: bool,
        created_count: int,
    ) -> dict[str, Any]:
        if batch is None:
            raise ResourceNotFound("导入批次不存在。")
        projects = session.scalars(
            select(ContentProject)
            .where(ContentProject.import_batch_id == batch.id)
            .order_by(ContentProject.source_sequence)
        ).all()
        tasks: list[dict[str, Any]] = []
        duplicate_title_warnings: list[str] = []
        pending_assignment_count = 0
        for project in projects:
            task = session.scalar(select(StageTask).where(StageTask.project_id == project.id))
            if task is not None:
                tasks.append(self._task_dict(session, task))
                if task.status == "PENDING_ASSIGNMENT":
                    pending_assignment_count += 1
            duplicate_exists = session.scalar(
                select(ContentProject.id)
                .where(
                    ContentProject.company_id == batch.company_id,
                    ContentProject.title == project.title,
                    ContentProject.id != project.id,
                    ContentProject.import_batch_id != batch.id,
                )
                .limit(1)
            )
            if duplicate_exists is not None:
                duplicate_title_warnings.append(project.title)
        failures = list((batch.parse_report or {}).get("failures", []))
        warnings = list((batch.parse_report or {}).get("warnings", []))
        import_mode = (batch.parse_report or {}).get("import_mode", "RULE_BASED")
        schema_version = (batch.parse_report or {}).get("schema_version")
        if deduplicated:
            user_message = "检测到相同文件，未重复创建任务。"
        elif batch.parse_status == "FAILED":
            user_message = "文档未解析出有效选题，未创建任务，请检查失败明细。"
        else:
            user_message = f"已从 Word 文档创建 {created_count} 个演播稿任务。"
            if pending_assignment_count:
                user_message += f"其中 {pending_assignment_count} 条待分配，请改派负责人。"
            if failures:
                user_message += f"另有 {len(failures)} 条选题解析失败。"
        return {
            "success": True,
            "data": {
                "import_batch_id": batch.id,
                "deduplicated": deduplicated,
                "created_count": created_count,
                "parse_status": batch.parse_status,
                "pending_assignment_count": pending_assignment_count,
                "import_mode": import_mode,
                "schema_version": schema_version,
                "warnings": warnings,
                "failures": failures,
                "tasks": tasks,
                "duplicate_title_warnings": duplicate_title_warnings,
            },
            "user_message": user_message,
            "next_actions": ["浏览全部任务", "删除单个任务", "更换任务员工"],
        }

    def _task_dict(self, session: Session, task: StageTask) -> dict[str, Any]:
        project = session.get(ContentProject, task.project_id)
        assignee = session.get(ActorProfile, task.assignee_id) if task.assignee_id else None
        next_actions: list[str]
        if task.status == "PENDING_ASSIGNMENT":
            next_actions = ["change_task_assignee", "delete_imported_task"]
        elif task.status in {"IN_PROGRESS", "REJECTED"}:
            next_actions = [
                "submit_script_file",
                "change_task_assignee",
                "set_task_priority",
                "report_task_blocker",
            ]
        elif task.status == "SUBMITTED":
            next_actions = ["review_script_submission", "set_task_priority"]
        else:
            # APPROVED/WAITING_FOR_FILMING 是 T4 当前正常终态；不暴露 T3 动作。
            next_actions = []
        return {
            "task_no": task.task_no,
            "title": project.title if project else "",
            "source_sequence": project.source_sequence if project else None,
            "task_type": task.task_type,
            "status": task.status,
            "priority": task.priority,
            "assigned_employee_id": assignee.id if assignee else None,
            "assigned_employee_name": assignee.display_name if assignee else None,
            "effective_started_at": task.effective_started_at.isoformat()
            if task.effective_started_at
            else None,
            "next_actions": next_actions,
        }

    def _project_dict(self, session: Session, project: ContentProject) -> dict[str, Any]:
        task = session.scalar(select(StageTask).where(StageTask.project_id == project.id))
        return {
            "project_id": project.id,
            "title": project.title,
            "status": project.status,
            "created_at": self._aware(project.created_at).isoformat(),
            "task": self._task_dict(session, task) if task else None,
        }

    def _project_detail(
        self, session: Session, project: ContentProject, task: StageTask
    ) -> dict[str, Any]:
        batch = (
            session.get(ImportBatch, project.import_batch_id) if project.import_batch_id else None
        )
        source_attachment = session.get(Attachment, batch.source_attachment_id) if batch else None
        submissions = session.scalars(
            select(Submission).where(Submission.task_id == task.id).order_by(Submission.version_no)
        ).all()
        history = []
        review_ids: list[str] = []
        for submission in submissions:
            attachment = session.get(Attachment, submission.attachment_id)
            review = session.scalar(select(Review).where(Review.submission_id == submission.id))
            if review is not None:
                review_ids.append(review.id)
            history.append(
                {
                    "version_no": submission.version_no,
                    "note": submission.note,
                    "submitted_at": self._aware(submission.created_at).isoformat(),
                    "download_url": self._download_url(attachment) if attachment else None,
                    "review": {
                        "decision": review.decision,
                        "reason_category": review.reason_category,
                        "comment": review.comment,
                        "reviewed_at": self._aware(review.created_at).isoformat(),
                    }
                    if review
                    else None,
                }
            )
        object_ids = [project.id, task.id, *[item.id for item in submissions], *review_ids]
        if batch is not None:
            object_ids.append(batch.id)
        audit_events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.company_id == project.company_id,
                AuditEvent.object_id.in_(object_ids),
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
        ).all()
        timeline = [
            {
                "event_id": event.id,
                "action": event.action,
                "object_type": event.object_type,
                "object_id": event.object_id,
                "actor_id": event.actor_id,
                "before_state": event.before_state,
                "after_state": event.after_state,
                "request_id": event.request_id,
                "created_at": self._aware(event.created_at).isoformat(),
            }
            for event in audit_events
        ]
        return {
            "project_id": project.id,
            "title": project.title,
            "project_status": project.status,
            "source_sequence": project.source_sequence,
            "source_content": project.source_content,
            "source_document_url": self._download_url(source_attachment)
            if source_attachment
            else None,
            "task": self._task_dict(session, task),
            "submission_history": history,
            "timeline": timeline,
        }

    def _download_url(self, attachment: Attachment) -> str:
        return f"{self.settings.public_base_url}/files/{attachment.opaque_file_id}"

    @staticmethod
    def _before_effective(task: StageTask, now: datetime) -> bool:
        effective = task.effective_started_at
        if effective is None:
            return False
        if effective.tzinfo is None:
            effective = effective.replace(tzinfo=UTC)
        return now < effective

    def _replay(
        self,
        session: Session,
        actor: ActorProfile,
        tool: str,
        key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return replay_or_none(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            tool=tool,
            key=key,
            payload=payload,
        )

    def _save_idempotency(
        self,
        session: Session,
        actor: ActorProfile,
        tool: str,
        key: str,
        payload: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        request_id = current_request_id()
        if request_id:
            response.setdefault("request_id", request_id)
        return save_response(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            tool=tool,
            key=key,
            payload=payload,
            response=response,
        )

    def _employee_load_summary(
        self, session: Session, company_id: str, now: datetime
    ) -> list[dict[str, Any]]:
        loads = employee_loads(session, company_id, now, self.settings.app_timezone)
        terminal = {"APPROVED", "COMPLETED", "CANCELLED"}
        tasks = session.scalars(
            select(StageTask).where(StageTask.company_id == company_id)
        ).all()
        return [
            {
                "employee_id": employee.id,
                "display_name": employee.display_name,
                "weekly_task_count": count,
                "current_task_count": sum(
                    1
                    for task in tasks
                    if task.assignee_id == employee.id and task.status not in terminal
                ),
            }
            for employee, count in loads
        ]

    @staticmethod
    def _validate_page(page: int, page_size: int) -> None:
        if page < 1:
            raise InvalidArgument("page 必须大于等于 1。")
        if page_size < 1 or page_size > 100:
            raise InvalidArgument("page_size 必须在 1 到 100 之间。")

    @staticmethod
    def _pagination(page: int, page_size: int, total: int) -> dict[str, Any]:
        total_pages = (total + page_size - 1) // page_size if total else 0
        return {
            "page": page,
            "page_size": page_size,
            "total_items": total,
            "total_pages": total_pages,
            "has_previous": page > 1,
            "has_next": page * page_size < total,
        }

    @staticmethod
    def _list_next_actions(page: int, page_size: int, total: int) -> list[str]:
        actions: list[str] = []
        if page > 1:
            actions.append("previous_page")
        if page * page_size < total:
            actions.append("next_page")
        actions.extend(["filter_results", "view_detail"])
        return actions

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _audit(
        self,
        session: Session,
        actor: ActorProfile,
        action: str,
        object_type: str,
        object_id: str,
        after: dict[str, Any],
    ) -> AuditEvent:
        return append_audit(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            request_id=f"req_{secrets.token_hex(12)}",
            after=after,
        )

    def _notify(
        self,
        session: Session,
        company_id: str,
        template: str,
        content: str,
        mentioned: list[str | None],
    ) -> None:
        session.add(
            Notification(
                company_id=company_id,
                event_id=f"evt_{uuid.uuid4().hex}",
                template=template,
                payload={"content": content},
                mentioned_userids=[value for value in mentioned if value],
                status=NotificationStatus.PENDING,
            )
        )


def safe_call(call: Any, *, tool: str = "") -> dict[str, Any]:
    try:
        return call()
    except WorkflowError as exc:
        logger.info(
            "tool_rejected",
            extra={"tool_name": tool, "result_code": exc.code},
        )
        return {
            "success": False,
            "error": {"code": exc.code, "message": exc.message},
        }
    except Exception:
        logger.exception(
            "tool_failed",
            extra={"tool_name": tool, "result_code": "INTERNAL_ERROR"},
        )
        return {
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "系统处理失败，请稍后使用相同幂等键重试。",
            },
        }
