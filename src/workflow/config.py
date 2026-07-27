from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["BOSS", "EMPLOYEE"]


@dataclass(frozen=True, slots=True)
class Actor:
    name: str
    role: Role
    token: str
    wecom_userid: str | None = None
    active: bool = True


class Settings(BaseSettings):
    """T0 配置。

    Token 只存在于进程内存，不写入探针元数据或日志。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_timezone: str = "Asia/Shanghai"
    public_base_url: str = "http://localhost:8080"
    probe_data_dir: Path = Path("/data/probes")
    file_data_dir: Path = Path("/data/files")
    database_url: str = "postgresql+psycopg://workflow:workflow@postgres:5432/workflow"
    company_id: str = "default"
    disk_warn_percent: int = Field(default=20, ge=1, le=99)
    disk_reject_percent: int = Field(default=10, ge=1, le=99)
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: int = Field(default=60, ge=5)
    worker_max_attempts: int = Field(default=5, ge=1)
    notification_send_enabled: bool = False
    mcp_allowed_hosts: str = "localhost,localhost:*,127.0.0.1,127.0.0.1:*"
    mcp_allowed_origins: str = "http://localhost:*,http://127.0.0.1:*"

    mcp_boss_name: str = "老板测试"
    mcp_boss_token: SecretStr = SecretStr("dev-boss-token-change-me")
    mcp_boss_wecom_userid: str = ""
    mcp_employees_json: str = (
        '[{"name":"员工测试","token":"dev-employee-token-change-me",'
        '"wecom_userid":"","active":true}]'
    )

    max_topic_document_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_script_document_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_video_bytes: int = Field(default=100 * 1024 * 1024, gt=0)

    t0_allow_wecom_send: bool = False
    wecom_group_webhook_url: SecretStr | None = None

    @field_validator("public_base_url")
    @classmethod
    def normalize_public_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("PUBLIC_BASE_URL 必须以 http:// 或 https:// 开头")
        return value

    def actors(self) -> tuple[Actor, ...]:
        try:
            raw_employees = json.loads(self.mcp_employees_json)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP_EMPLOYEES_JSON 不是合法 JSON") from exc

        if not isinstance(raw_employees, list):
            raise ValueError("MCP_EMPLOYEES_JSON 必须是数组")

        actors: list[Actor] = [
            Actor(
                name=self.mcp_boss_name.strip(),
                role="BOSS",
                token=self.mcp_boss_token.get_secret_value(),
                wecom_userid=self.mcp_boss_wecom_userid.strip() or None,
            )
        ]
        for index, item in enumerate(raw_employees):
            if not isinstance(item, dict):
                raise ValueError(f"MCP_EMPLOYEES_JSON[{index}] 必须是对象")
            name = str(item.get("name", "")).strip()
            token = str(item.get("token", "")).strip()
            if not name or not token:
                raise ValueError(f"MCP_EMPLOYEES_JSON[{index}] 缺少 name 或 token")
            actors.append(
                Actor(
                    name=name,
                    role="EMPLOYEE",
                    token=token,
                    wecom_userid=str(item.get("wecom_userid", "")).strip() or None,
                    active=bool(item.get("active", True)),
                )
            )

        tokens = [actor.token for actor in actors]
        if len(tokens) != len(set(tokens)):
            raise ValueError("每位人员必须使用不同 Token")
        if any(len(token) < 16 for token in tokens):
            raise ValueError("T0 Token 至少需要 16 个字符")
        return tuple(actors)

    def token_registry(self) -> dict[str, Actor]:
        return {actor.token: actor for actor in self.actors() if actor.active}

    def known_wecom_userids(self) -> set[str]:
        return {
            actor.wecom_userid
            for actor in self.actors()
            if actor.active and actor.wecom_userid is not None
        }

    def allowed_hosts(self) -> list[str]:
        values = [item.strip() for item in self.mcp_allowed_hosts.split(",") if item.strip()]
        public_url = urlsplit(self.public_base_url)
        if public_url.hostname:
            values.extend((public_url.hostname, f"{public_url.hostname}:*"))
        return list(dict.fromkeys(values))

    def allowed_origins(self) -> list[str]:
        values = [item.strip() for item in self.mcp_allowed_origins.split(",") if item.strip()]
        public_url = urlsplit(self.public_base_url)
        if public_url.scheme and public_url.hostname:
            values.extend(
                (
                    f"{public_url.scheme}://{public_url.hostname}",
                    f"{public_url.scheme}://{public_url.hostname}:*",
                )
            )
        return list(dict.fromkeys(values))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.actors()
    return settings
