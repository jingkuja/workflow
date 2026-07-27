from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Literal

from workflow.probes.models import ProbeFileMetadata

ProbeKind = Literal["document", "video"]

_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_DOCUMENT_EXTENSIONS = {".docx", ".pdf", ".md", ".txt"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
_BASE64_CHUNK_CHARS = 1024 * 1024
_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
}


class ProbeValidationError(ValueError):
    pass


def estimated_decoded_size(encoded: str) -> int:
    if not encoded:
        return 0
    if encoded.startswith("data:"):
        raise ProbeValidationError("请传入纯 Base64 字符串，不要使用 data: URI。")
    if any(char.isspace() for char in encoded[: min(len(encoded), 4096)]):
        raise ProbeValidationError("Base64 字符串不能包含空白或换行。")
    padding = len(encoded) - len(encoded.rstrip("="))
    return (len(encoded) * 3) // 4 - padding


class ProbeStorage:
    def __init__(self, root: Path, public_base_url: str) -> None:
        self.root = root
        self.public_base_url = public_base_url.rstrip("/")
        self.files_dir = root / "files"
        self.metadata_dir = root / "metadata"
        self.idempotency_dir = root / "idempotency"
        for directory in (self.files_dir, self.metadata_dir, self.idempotency_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def save_base64(
        self,
        *,
        probe_kind: ProbeKind,
        original_filename: str,
        encoded: str,
        max_bytes: int,
        idempotency_key: str,
        namespace: str,
    ) -> tuple[ProbeFileMetadata, bool]:
        self._validate_filename(original_filename, probe_kind)
        self._validate_idempotency_key(idempotency_key)
        estimate = estimated_decoded_size(encoded)
        if estimate > max_bytes:
            raise ProbeValidationError(
                f"文件预计解码后为 {estimate} 字节，超过上限 {max_bytes} 字节。"
            )

        idempotency_digest = hashlib.sha256(f"{namespace}:{idempotency_key}".encode()).hexdigest()
        idempotency_path = self.idempotency_dir / f"{idempotency_digest}.json"
        if idempotency_path.exists():
            existing_id = json.loads(idempotency_path.read_text(encoding="utf-8"))["opaque_file_id"]
            return self.load_metadata(existing_id), True

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix="probe-",
                dir=self.root,
                delete=False,
            ) as temporary:
                temp_path = Path(temporary.name)
                sha256 = hashlib.sha256()
                actual_size = 0
                head = bytearray()

                for offset in range(0, len(encoded), _BASE64_CHUNK_CHARS):
                    chunk = encoded[offset : offset + _BASE64_CHUNK_CHARS]
                    try:
                        decoded = base64.b64decode(chunk, validate=True)
                    except binascii.Error as exc:
                        raise ProbeValidationError("Base64 内容格式错误。") from exc
                    actual_size += len(decoded)
                    if actual_size > max_bytes:
                        raise ProbeValidationError(f"文件解码后超过上限 {max_bytes} 字节。")
                    if len(head) < 32:
                        head.extend(decoded[: 32 - len(head)])
                    sha256.update(decoded)
                    temporary.write(decoded)

                temporary.flush()
                os.fsync(temporary.fileno())

            self._validate_signature(original_filename, probe_kind, bytes(head))
            opaque_file_id = secrets.token_urlsafe(24)
            final_path = self.files_dir / opaque_file_id
            os.replace(temp_path, final_path)
            temp_path = None

            extension = Path(original_filename).suffix.lower()
            mime_type = (
                _MIME_TYPES.get(extension)
                or mimetypes.guess_type(original_filename)[0]
                or "application/octet-stream"
            )
            metadata = ProbeFileMetadata(
                opaque_file_id=opaque_file_id,
                original_filename=original_filename,
                probe_kind=probe_kind,
                mime_type=mime_type,
                size_bytes=actual_size,
                sha256=sha256.hexdigest(),
                created_at=datetime.now(UTC).isoformat(),
                download_url=f"{self.public_base_url}/files/{opaque_file_id}",
                idempotency_key_digest=idempotency_digest,
            )
            self._write_json_atomic(
                self.metadata_dir / f"{opaque_file_id}.json",
                metadata.model_dump(),
            )
            self._write_json_atomic(
                idempotency_path,
                {"opaque_file_id": opaque_file_id},
            )
            return metadata, False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def load_metadata(self, opaque_file_id: str) -> ProbeFileMetadata:
        if not _OPAQUE_ID_PATTERN.fullmatch(opaque_file_id):
            raise FileNotFoundError(opaque_file_id)
        metadata_path = self.metadata_dir / f"{opaque_file_id}.json"
        if not metadata_path.is_file():
            raise FileNotFoundError(opaque_file_id)
        return ProbeFileMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))

    def file_path(self, opaque_file_id: str) -> Path:
        if not _OPAQUE_ID_PATTERN.fullmatch(opaque_file_id):
            raise FileNotFoundError(opaque_file_id)
        file_path = self.files_dir / opaque_file_id
        if not file_path.is_file():
            raise FileNotFoundError(opaque_file_id)
        return file_path

    @staticmethod
    def _validate_filename(filename: str, probe_kind: ProbeKind) -> None:
        if not filename or PurePath(filename).name != filename:
            raise ProbeValidationError("文件名不能为空，也不能包含路径。")
        if any(ord(char) < 32 for char in filename):
            raise ProbeValidationError("文件名包含控制字符。")
        extension = Path(filename).suffix.lower()
        allowed = _DOCUMENT_EXTENSIONS if probe_kind == "document" else _VIDEO_EXTENSIONS
        if extension not in allowed:
            raise ProbeValidationError(f"{probe_kind} 不支持扩展名 {extension or '(无扩展名)'}。")

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> None:
        if not 8 <= len(idempotency_key) <= 128:
            raise ProbeValidationError("幂等键长度必须为 8—128 个字符。")

    @staticmethod
    def _validate_signature(filename: str, probe_kind: ProbeKind, head: bytes) -> None:
        extension = Path(filename).suffix.lower()
        if probe_kind == "document":
            if extension == ".docx" and not head.startswith(b"PK"):
                raise ProbeValidationError("文件扩展名为 .docx，但内容不是 OOXML ZIP。")
            if extension == ".pdf" and not head.startswith(b"%PDF"):
                raise ProbeValidationError("文件扩展名为 .pdf，但内容不是 PDF。")
            if extension in {".md", ".txt"} and b"\x00" in head:
                raise ProbeValidationError("文本文件头包含 NUL 字节。")
            return

        is_mp4_family = extension in {".mp4", ".mov", ".m4v"} and head[4:8] == b"ftyp"
        is_webm = extension == ".webm" and head.startswith(b"\x1aE\xdf\xa3")
        if not (is_mp4_family or is_webm):
            raise ProbeValidationError("视频文件头与扩展名不匹配。")

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_suffix(f"{path.suffix}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
