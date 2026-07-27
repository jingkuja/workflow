from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProbeFileMetadata(BaseModel):
    opaque_file_id: str
    original_filename: str
    probe_kind: Literal["document", "video"]
    mime_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: str
    download_url: str
    idempotency_key_digest: str


class ProbeResult(BaseModel):
    success: bool = True
    request_id: str
    data: dict[str, Any]
    user_message: str
    next_actions: list[str] = Field(default_factory=list)
