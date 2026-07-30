from __future__ import annotations

import hashlib
import os
import secrets
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from workflow.errors import InsufficientStorage


@dataclass(frozen=True, slots=True)
class StoredFile:
    opaque_file_id: str
    storage_provider: str
    storage_key: str
    sha256: str
    size_bytes: int


class LocalStorage:
    def __init__(self, root: Path, reject_percent: int = 10) -> None:
        self.root = root
        self.reject_percent = reject_percent
        self.tmp = root / "tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def free_percent(self) -> float:
        usage = shutil.disk_usage(self.root)
        return usage.free / usage.total * 100

    def ensure_capacity(self) -> None:
        if self.free_percent() < self.reject_percent:
            raise InsufficientStorage("磁盘剩余空间低于安全阈值，暂不接收本地文件。")

    def put(
        self, stream: BinaryIO, *, company_id: str, purpose: str, attachment_id: str
    ) -> StoredFile:
        return self.put_chunks(
            iter(lambda: stream.read(1024 * 1024), b""),
            company_id=company_id,
            purpose=purpose,
            attachment_id=attachment_id,
        )

    def put_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        company_id: str,
        purpose: str,
        attachment_id: str,
    ) -> StoredFile:
        self.ensure_capacity()
        opaque_id = secrets.token_urlsafe(24)
        storage_key = f"{purpose}/{company_id}/{attachment_id}/content"
        destination = self._resolve(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = self.tmp / f"{attachment_id}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp.open("wb") as target:
                for chunk in chunks:
                    if not chunk:
                        continue
                    digest.update(chunk)
                    size += len(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return StoredFile(opaque_id, "LOCAL", storage_key, digest.hexdigest(), size)

    def path_for(self, storage_key: str) -> Path:
        path = self._resolve(storage_key)
        if not path.is_file():
            raise FileNotFoundError(storage_key)
        return path

    def exists(self, storage_key: str) -> bool:
        return self._resolve(storage_key).is_file()

    def _resolve(self, storage_key: str) -> Path:
        path = (self.root / storage_key).resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise ValueError("非法 storage_key")
        return path
