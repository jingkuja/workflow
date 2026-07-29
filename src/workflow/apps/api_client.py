from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger("workflow.mcp")


class WorkflowApiClient:
    """MCP 入口到内部工作流 API 的适配客户端（规格 §3.1）。

    只做凭据透传、参数转发和返回结构适配；不包含任何业务规则，
    也不直接访问数据库。错误响应（统一错误结构）原样透传给 WorkBuddy。
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def call(
        self,
        path: str,
        *,
        token: str,
        tool: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = f"req_{uuid.uuid4().hex}"
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.post(
                    path,
                    json=payload or {},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Request-ID": request_id,
                    },
                )
        except httpx.HTTPError:
            logger.exception(
                "api_call_failed",
                extra={"tool_name": tool, "result_code": "EXTERNAL_DEPENDENCY_FAILED"},
            )
            return {
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "EXTERNAL_DEPENDENCY_FAILED",
                    "message": "内部工作流服务暂时不可用，请稍后使用相同幂等键重试。",
                    "remediation": "确认 workflow-api 健康后，使用相同幂等键重试。",
                },
            }
        try:
            body = response.json()
        except ValueError:
            logger.error(
                "api_call_bad_response",
                extra={"tool_name": tool, "result_code": response.status_code},
            )
            return {
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "系统处理失败，请稍后使用相同幂等键重试。",
                    "remediation": "保留 request_id 并联系管理员检查 workflow-api 日志。",
                },
            }
        if response.status_code >= 400:
            logger.info(
                "api_call_rejected",
                extra={
                    "tool_name": tool,
                    "result_code": body.get("error", {}).get("code", response.status_code),
                },
            )
        return body
