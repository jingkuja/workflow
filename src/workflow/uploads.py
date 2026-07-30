from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow.config import Settings
from workflow.db.models import ActorProfile, McpFileUpload
from workflow.errors import FileProcessingFailed, FileTooLarge, InvalidArgument, ResourceNotFound
from workflow.storage import LocalStorage
from workflow.t2.files import validate_filename, validate_signature

_BASE64_CHUNK_CHARS = 1024 * 1024


def _base64_decoded_size(encoded: str) -> int:
    if len(encoded) % 4:
        raise InvalidArgument("Base64 内容格式错误。")
    padding = 2 if encoded.endswith("==") else 1 if encoded.endswith("=") else 0
    body_end = len(encoded) - padding if padding else len(encoded)
    if padding > 2 or "=" in encoded[:body_end]:
        raise InvalidArgument("Base64 内容格式错误。")
    return len(encoded) // 4 * 3 - padding


def _iter_base64_decoded(encoded: str) -> Iterator[bytes]:
    for offset in range(0, len(encoded), _BASE64_CHUNK_CHARS):
        yield base64.b64decode(
            encoded[offset : offset + _BASE64_CHUNK_CHARS],
            validate=True,
        )


class McpUploadService:
    """Stores Host-provided Base64 separately and resolves opaque file handles."""

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

    def upload_base64(
        self,
        session: Session,
        *,
        actor: ActorProfile,
        file_base64: str,
    ) -> dict[str, object]:
        if file_base64.startswith("data:"):
            raise InvalidArgument("请传入纯 Base64，不要使用 data URI。")
        decoded_size = _base64_decoded_size(file_base64)
        if decoded_size > self.max_bytes:
            raise FileTooLarge(f"文件超过技术上限 {self.max_bytes} 字节。")
        upload_id = str(uuid.uuid4())
        try:
            stored = self.storage.put_chunks(
                _iter_base64_decoded(file_base64),
                company_id=actor.company_id,
                purpose="mcp-upload",
                attachment_id=upload_id,
            )
        except (binascii.Error, ValueError) as exc:
            raise InvalidArgument("Base64 内容格式错误。") from exc
        if stored.size_bytes > self.max_bytes:
            raise FileTooLarge(f"文件超过技术上限 {self.max_bytes} 字节。")

        expires_at = datetime.now(UTC) + timedelta(hours=self.settings.mcp_upload_ttl_hours)
        upload = McpFileUpload(
            id=upload_id,
            company_id=actor.company_id,
            uploaded_by=actor.id,
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
            "user_message": "文件已预上传。请把 file_key 传给后续业务工具。",
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
            select(McpFileUpload).where(
                McpFileUpload.file_key == file_key,
                McpFileUpload.company_id == actor.company_id,
                McpFileUpload.uploaded_by == actor.id,
            )
        )
        if upload is None:
            raise ResourceNotFound("file_key 不存在或不属于当前调用人。")
        expires_at = upload.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise ResourceNotFound("file_key 已过期，请重新调用 upload_file。")
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
