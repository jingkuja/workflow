from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from workflow.config import Role, Settings
from workflow.probes.models import ProbeResult
from workflow.probes.storage import ProbeStorage, ProbeValidationError

_ALLOWED_DOCUMENT_EXTENSIONS = {".docx", ".pdf", ".md", ".txt"}


class ProbeService:
    def __init__(self, settings: Settings, role: Role) -> None:
        self.settings = settings
        self.role = role
        self.storage = ProbeStorage(settings.probe_data_dir, settings.public_base_url)

    def ping(self) -> ProbeResult:
        return ProbeResult(
            request_id=self._request_id(),
            data={
                "phase": "T0",
                "role": self.role,
                "transport": "streamable-http",
                "stateless_http": True,
                "json_response": True,
            },
            user_message=f"{self.role} MCP T0 探针可用。",
            next_actions=["查看工具列表", "验证文件字段"],
        )

    def probe_document(
        self,
        *,
        original_filename: str,
        content_base64: str | None,
        file_url: str | None,
        idempotency_key: str,
    ) -> ProbeResult:
        if bool(content_base64) == bool(file_url):
            raise ProbeValidationError("content_base64 与 file_url 必须且只能提供一个。")

        extension = Path(original_filename).suffix.lower()
        if extension not in _ALLOWED_DOCUMENT_EXTENSIONS:
            raise ProbeValidationError("文档仅支持 .docx、.pdf、.md、.txt。")
        max_bytes = (
            self.settings.max_topic_document_bytes
            if extension == ".docx"
            else self.settings.max_script_document_bytes
        )

        if file_url:
            self._validate_https_url(file_url)
            return ProbeResult(
                request_id=self._request_id(),
                data={
                    "received_shape": "url",
                    "original_filename": original_filename,
                    "file_url": file_url,
                    "downloaded": False,
                    "note": "T0 仅确认字段形态；正式安全下载在 T1 实现。",
                },
                user_message="已收到并校验 HTTPS 文档 URL 字段。",
                next_actions=["记录 WorkBuddy 实际请求样例"],
            )

        metadata, deduplicated = self.storage.save_base64(
            probe_kind="document",
            original_filename=original_filename,
            encoded=content_base64 or "",
            max_bytes=max_bytes,
            idempotency_key=idempotency_key,
            namespace=f"{self.role}:document",
        )
        return ProbeResult(
            request_id=self._request_id(),
            data={
                "received_shape": "base64",
                "deduplicated": deduplicated,
                "file": metadata.model_dump(),
            },
            user_message="文档字段接收、校验和落盘成功。",
            next_actions=["打开下载链接", "登记字段形态和耗时"],
        )

    def probe_video(
        self,
        *,
        original_filename: str,
        video_base64: str,
        idempotency_key: str,
    ) -> ProbeResult:
        metadata, deduplicated = self.storage.save_base64(
            probe_kind="video",
            original_filename=original_filename,
            encoded=video_base64,
            max_bytes=self.settings.max_video_bytes,
            idempotency_key=idempotency_key,
            namespace=f"{self.role}:video",
        )
        return ProbeResult(
            request_id=self._request_id(),
            data={
                "received_shape": "base64",
                "deduplicated": deduplicated,
                "file": metadata.model_dump(),
                "memory_note": (
                    "工具函数内按 1 MiB Base64 字符块解码；SDK/JSON 层是否整体载入"
                    "需在真实 WorkBuddy 链路测量。"
                ),
            },
            user_message="视频 Base64 校验和落盘成功。",
            next_actions=["打开下载链接", "记录耗时与内存峰值"],
        )

    def get_probe_file(self, opaque_file_id: str) -> ProbeResult:
        metadata = self.storage.load_metadata(opaque_file_id)
        exists = self.storage.file_path(opaque_file_id).is_file()
        return ProbeResult(
            request_id=self._request_id(),
            data={"exists": exists, "file": metadata.model_dump()},
            user_message="已获取 T0 探针文件。",
            next_actions=["打开下载链接"],
        )

    @staticmethod
    def _validate_https_url(value: str) -> None:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ProbeValidationError("file_url 必须是带主机名的 HTTPS URL。")
        if parts.username or parts.password:
            raise ProbeValidationError("file_url 不能包含用户名或密码。")
        if re.search(r"[\r\n]", value):
            raise ProbeValidationError("file_url 包含非法换行。")

    @staticmethod
    def _request_id() -> str:
        return f"req_{uuid.uuid4().hex}"
