from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StructuredTopicInput(BaseModel):
    """WorkBuddy 从任意排版 Word 中抽取出的单条选题。"""

    model_config = ConfigDict(extra="forbid")

    source_index: str | None = Field(default=None, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    source_text: str = Field(min_length=1, max_length=100_000)
    script: str | None = Field(default=None, max_length=100_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("source_index", "script")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("title", "source_text")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("evidence")
    @classmethod
    def strip_evidence(cls, values: list[str]) -> list[str]:
        stripped = [value.strip() for value in values]
        if any(not value for value in stripped):
            raise ValueError("evidence 不能包含空字符串")
        if any(len(value) > 2_000 for value in stripped):
            raise ValueError("单条 evidence 不能超过 2000 字符")
        return stripped


class StructuredTopicImportBody(BaseModel):
    """WorkBuddy 调用结构化导入 API/MCP 时使用的稳定契约。"""

    model_config = ConfigDict(extra="forbid")

    original_filename: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    topics: list[StructuredTopicInput] = Field(min_length=1, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    content_base64: str | None = None
    file_url: str | None = None

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, values: list[str]) -> list[str]:
        stripped = [value.strip() for value in values if value.strip()]
        if any(len(value) > 2_000 for value in stripped):
            raise ValueError("单条 warning 不能超过 2000 字符")
        return stripped
