from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

# logging.LogRecord 的标准属性，extra 注入的字段才是业务字段。
_STANDARD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__)

# 不允许出现在日志里的敏感字段名（规格 §4.9：Token、Webhook、密码不进日志）。
_SENSITIVE_KEYS = frozenset({"token", "authorization", "webhook", "password", "secret"})


class JsonFormatter(logging.Formatter):
    """JSON 结构化日志，字段口径见实施方案 §4.11。"""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if key.lower() in _SENSITIVE_KEYS:
                payload[key] = "***"
            else:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service: str, level: int = logging.INFO) -> None:
    """把 root 与 uvicorn 日志统一为单行 JSON。

    在应用入口（workflow_api / worker / 两个 MCP）模块加载早期调用一次。
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        child = logging.getLogger(name)
        child.handlers[:] = []
        child.propagate = True
