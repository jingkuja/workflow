from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from workflow.config import Role, Settings
from workflow.probes.service import ProbeService
from workflow.probes.wecom import send_wecom_probe


def create_mcp_server(settings: Settings, role: Role) -> FastMCP:
    service = ProbeService(settings, role)
    server_name = "boss-workflow-mcp-t0" if role == "BOSS" else "employee-workflow-mcp-t0"
    mcp = FastMCP(
        server_name,
        instructions=(
            "T0 技术验证服务。仅用于验证 WorkBuddy 的 MCP、Token、文件、"
            "Base64、下载链接和企业微信链路。"
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts(),
            allowed_origins=settings.allowed_origins(),
        ),
    )

    @mcp.tool(
        name="t0_ping",
        description="验证当前 MCP 入口、角色和 Streamable HTTP 响应。",
    )
    def t0_ping() -> dict[str, Any]:
        return service.ping().model_dump()

    @mcp.tool(
        name="t0_probe_document_input",
        description=(
            "验证 WorkBuddy 传入的 .docx/.pdf/.md/.txt 文件字段。"
            "content_base64 与 file_url 必须二选一。"
        ),
    )
    def t0_probe_document_input(
        original_filename: str,
        idempotency_key: str,
        content_base64: str | None = None,
        file_url: str | None = None,
    ) -> dict[str, Any]:
        return service.probe_document(
            original_filename=original_filename,
            content_base64=content_base64,
            file_url=file_url,
            idempotency_key=idempotency_key,
        ).model_dump()

    @mcp.tool(
        name="t0_probe_video_base64",
        description=("验证最大约 100 MB 视频的纯 Base64 字段、分块解码、文件头校验和落盘。"),
    )
    def t0_probe_video_base64(
        original_filename: str,
        video_base64: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return service.probe_video(
            original_filename=original_filename,
            video_base64=video_base64,
            idempotency_key=idempotency_key,
        ).model_dump()

    @mcp.tool(
        name="t0_get_probe_file",
        description="查询 T0 探针文件元数据和稳定下载地址。",
    )
    def t0_get_probe_file(opaque_file_id: str) -> dict[str, Any]:
        return service.get_probe_file(opaque_file_id).model_dump()

    if role == "BOSS":

        @mcp.tool(
            name="t0_boss_capability",
            description="老板入口专有工具，用于验证工具白名单隔离。",
        )
        def t0_boss_capability() -> dict[str, object]:
            return {
                "success": True,
                "data": {"role": "BOSS", "capability": "boss-only"},
                "user_message": "老板专有工具可用。",
            }

        @mcp.tool(
            name="t0_probe_wecom_mention",
            description=(
                "向企业微信群发送 T0 文本并按 .env 中登记的 userid @成员。"
                "只有 T0_ALLOW_WECOM_SEND=true 时才真实发送。"
            ),
        )
        async def t0_probe_wecom_mention(
            message: str,
            mentioned_userids: list[str],
        ) -> dict[str, object]:
            result = await send_wecom_probe(settings, message, mentioned_userids)
            return {
                "success": True,
                "data": result,
                "user_message": "企业微信 T0 探针执行完成。",
            }

    else:

        @mcp.tool(
            name="t0_employee_capability",
            description="员工入口专有工具，用于验证工具白名单隔离。",
        )
        def t0_employee_capability() -> dict[str, object]:
            return {
                "success": True,
                "data": {"role": "EMPLOYEE", "capability": "employee-only"},
                "user_message": "员工专有工具可用。",
            }

    return mcp
