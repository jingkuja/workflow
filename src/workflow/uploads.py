from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from workflow.config import Settings
from workflow.db.models import ActorProfile, TemporaryFileUpload
from workflow.errors import FileProcessingFailed, FileTooLarge, InvalidArgument, ResourceNotFound
from workflow.storage import LocalStorage, StoredFile
from workflow.t2.files import validate_filename, validate_signature


class TemporaryUploadService:
    """Store temporary uploads and resolve actor-bound opaque file handles."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = LocalStorage(settings.file_data_dir, settings.disk_reject_percent)

    @property
    def max_bytes(self) -> int:
        return max(
            self.settings.max_topic_document_bytes,
            self.settings.max_script_document_bytes,
            self.settings.max_video_bytes,
        )

    def upload_stream(
        self,
        session: Session,
        *,
        stream: BinaryIO,
    ) -> dict[str, object]:
        """Store an unbound raw binary stream received by the upload API."""
        upload_id = str(uuid.uuid4())
        stored = self.storage.put(
            stream,
            company_id="unbound",
            purpose="api-upload",
            attachment_id=upload_id,
        )
        if stored.size_bytes == 0:
            raise InvalidArgument("文件内容不能为空。")
        if stored.size_bytes > self.max_bytes:
            raise FileTooLarge(f"文件超过技术上限 {self.max_bytes} 字节。")
        return self._record_upload(
            session,
            stored=stored,
            upload_id=upload_id,
        )

    def _record_upload(
        self,
        session: Session,
        *,
        stored: StoredFile,
        upload_id: str,
    ) -> dict[str, object]:
        expires_at = datetime.now(UTC) + timedelta(hours=self.settings.file_upload_ttl_hours)
        upload = TemporaryFileUpload(
            id=upload_id,
            company_id=None,
            uploaded_by=None,
            file_key=stored.opaque_file_id,
            storage_provider=stored.storage_provider,
            storage_key=stored.storage_key,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            expires_at=expires_at,
        )
        session.add(upload)
        session.flush()
        return {
            "success": True,
            "data": {
                "file_key": upload.file_key,
                "sha256": upload.sha256,
                "size_bytes": upload.size_bytes,
                "expires_at": expires_at.isoformat(),
            },
            "user_message": "文件已上传但尚未绑定人员。请把 file_key 传给后续业务工具。",
            "next_actions": [
                {
                    "action": "use_file_key",
                    "description": "调用导入或提交工具，并传入本次返回的 file_key。",
                }
            ],
        }

    def read_document(
        self,
        session: Session,
        *,
        actor: ActorProfile,
        file_key: str,
        filename: str,
        allowed: set[str],
        max_bytes: int,
    ) -> bytes:
        path = self.document_path(
            session,
            actor=actor,
            file_key=file_key,
            filename=filename,
            allowed=allowed,
            max_bytes=max_bytes,
        )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FileProcessingFailed("预上传文件内容已不可用，请重新上传。") from exc

    def document_path(
        self,
        session: Session,
        *,
        actor: ActorProfile,
        file_key: str,
        filename: str,
        allowed: set[str],
        max_bytes: int,
    ) -> Path:
        extension = validate_filename(filename, allowed)
        upload = session.scalar(
            select(TemporaryFileUpload).where(TemporaryFileUpload.file_key == file_key)
        )
        if upload is None:
            raise ResourceNotFound("file_key 不存在。")
        expires_at = upload.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise ResourceNotFound("file_key 已过期，请通过文件上传 API 重新上传。")
        self._claim_or_verify(session, upload=upload, actor=actor)
        if upload.size_bytes > max_bytes:
            raise FileTooLarge(f"文件超过该业务工具的技术上限 {max_bytes} 字节。")
        try:
            path = self.storage.path_for(upload.storage_key)
            digest = hashlib.sha256()
            size = 0
            signature = b""
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    if not signature:
                        signature = chunk[:4096]
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise FileProcessingFailed("预上传文件内容已不可用，请重新上传。") from exc
        if size != upload.size_bytes or digest.hexdigest() != upload.sha256:
            raise FileProcessingFailed("预上传文件完整性校验失败，请重新上传。")
        validate_signature(signature, extension)
        return path

    @staticmethod
    def _claim_or_verify(
        session: Session,
        *,
        upload: TemporaryFileUpload,
        actor: ActorProfile,
    ) -> None:
        if (upload.company_id is None) != (upload.uploaded_by is None):
            raise FileProcessingFailed("file_key 归属状态异常。")
        if upload.company_id is None:
            claimed = session.execute(
                update(TemporaryFileUpload)
                .where(
                    TemporaryFileUpload.id == upload.id,
                    TemporaryFileUpload.company_id.is_(None),
                    TemporaryFileUpload.uploaded_by.is_(None),
                )
                .values(company_id=actor.company_id, uploaded_by=actor.id)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount == 1:
                upload.company_id = actor.company_id
                upload.uploaded_by = actor.id
            else:
                session.expire(upload)
                session.refresh(upload)
        if upload.company_id != actor.company_id or upload.uploaded_by != actor.id:
            raise ResourceNotFound("file_key 已由其他调用人绑定。")
