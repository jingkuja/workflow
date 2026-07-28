from __future__ import annotations

import hashlib
import io
import logging
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from workflow.audit import append_audit
from workflow.config import Settings
from workflow.db.models import (
    ActorProfile,
    Attachment,
    AttachmentStatus,
    AuditEvent,
    ContentProject,
    ImportBatch,
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
from workflow.storage import LocalStorage
from workflow.t2.allocation import (
    advisory_lock,
    choose_employee,
    employee_loads,
    next_task_number,
)
from workflow.t2.calendar import effective_started_at, week_start_for
from workflow.t2.files import DOCUMENT_MIME_TYPES, receive_document
from workflow.t2.parser import TopicParseError, parse_topic_document

TOPIC_EXTENSIONS = {".docx"}
SCRIPT_EXTENSIONS = {".docx", ".pdf", ".md", ".txt"}

logger = logging.getLogger("workflow.t2")


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
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            replay = self._replay(
                session,
                actor,
                "import_topic_document",
                idempotency_key,
                {"filename": original_filename, "sha256": sha256},
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
                    "import_topic_document",
                    idempotency_key,
                    {"filename": original_filename, "sha256": sha256},
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
                parse_report={"failures": failure_report},
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
                "import_topic_document",
                idempotency_key,
                {"filename": original_filename, "sha256": sha256},
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
            loads = employee_loads(session, actor.company_id, now, self.settings.app_timezone)
            return {
                "success": True,
                "data": [
                    {
                        "employee_id": employee.id,
                        "display_name": employee.display_name,
                        "weekly_task_count": count,
                    }
                    for employee, count in loads
                ],
                "user_message": f"共 {len(loads)} 名在岗员工。",
            }

    def list_projects(
        self, *, actor_name: str, status: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            query = select(ContentProject).where(ContentProject.company_id == actor.company_id)
            if status:
                query = query.where(ContentProject.status == status)
            projects = session.scalars(
                query.order_by(ContentProject.created_at.desc()).limit(min(limit, 100))
            ).all()
            return {
                "success": True,
                "data": [self._project_dict(session, project) for project in projects],
                "user_message": f"查询到 {len(projects)} 个内容项目。",
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
        limit: int = 50,
    ) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.EMPLOYEE)
            query = select(StageTask).where(
                StageTask.company_id == actor.company_id,
                StageTask.assignee_id == actor.id,
                StageTask.status != "CANCELLED",
            )
            if status:
                query = query.where(StageTask.status == status)
            tasks = session.scalars(
                query.order_by(
                    StageTask.priority.desc(),
                    StageTask.effective_started_at,
                    StageTask.created_at,
                    StageTask.task_no,
                ).limit(min(limit, 100))
            ).all()
            return {
                "success": True,
                "data": [self._task_dict(session, task) for task in tasks],
                "user_message": f"你有 {len(tasks)} 条匹配任务。",
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

    def pending_reviews(self, *, actor_name: str) -> dict[str, Any]:
        with session_scope(self.sessions) as session:
            actor = self._actor(session, actor_name, Role.BOSS)
            tasks = session.scalars(
                select(StageTask)
                .where(
                    StageTask.company_id == actor.company_id,
                    StageTask.task_type == "SCRIPT",
                    StageTask.status == "SUBMITTED",
                )
                .order_by(StageTask.created_at)
            ).all()
            return {
                "success": True,
                "data": [self._task_dict(session, task) for task in tasks],
                "user_message": f"有 {len(tasks)} 条演播稿待审核。",
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
        }

    def _project_dict(self, session: Session, project: ContentProject) -> dict[str, Any]:
        task = session.scalar(select(StageTask).where(StageTask.project_id == project.id))
        return {
            "project_id": project.id,
            "title": project.title,
            "status": project.status,
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
        for submission in submissions:
            attachment = session.get(Attachment, submission.attachment_id)
            review = session.scalar(select(Review).where(Review.submission_id == submission.id))
            history.append(
                {
                    "version_no": submission.version_no,
                    "note": submission.note,
                    "download_url": self._download_url(attachment) if attachment else None,
                    "review": {
                        "decision": review.decision,
                        "reason_category": review.reason_category,
                        "comment": review.comment,
                    }
                    if review
                    else None,
                }
            )
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
        return save_response(
            session,
            company_id=actor.company_id,
            actor_id=actor.id,
            tool=tool,
            key=key,
            payload=payload,
            response=response,
        )

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
