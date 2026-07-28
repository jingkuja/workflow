from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from workflow.config import Role, Settings
from workflow.probes.service import ProbeService
from workflow.probes.wecom import send_wecom_probe
from workflow.t2.service import T2Service, safe_call


def _actor_name(context: Context) -> str:
    request = context.request_context.request
    state = request.scope.get("state", {}) if request is not None else {}
    actor_name = state.get("actor_name")
    if not actor_name:
        raise RuntimeError("请求身份上下文缺失")
    return str(actor_name)


def create_mcp_server(settings: Settings, role: Role) -> FastMCP:
    service = ProbeService(settings, role)
    t2 = T2Service(settings)
    server_name = "boss-workflow-mcp" if role == "BOSS" else "employee-workflow-mcp"
    mcp = FastMCP(
        server_name,
        instructions=(
            "新媒体内容制作工作流服务。T2 支持选题 Word 导入、任务分配、员工演播稿提交和老板审核。"
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

        @mcp.tool(name="import_topic_document", description="导入 .docx 选题并自动均衡分配。")
        def import_topic_document(
            original_filename: str,
            idempotency_key: str,
            context: Context,
            content_base64: str | None = None,
            file_url: str | None = None,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.import_topics(
                    actor_name=_actor_name(context),
                    original_filename=original_filename,
                    idempotency_key=idempotency_key,
                    content_base64=content_base64,
                    file_url=file_url,
                )
            )

        @mcp.tool(name="list_import_batch_tasks", description="查询导入批次的最新任务列表。")
        def list_import_batch_tasks(import_batch_id: str, context: Context) -> dict[str, Any]:
            return safe_call(
                lambda: t2.list_batch(
                    actor_name=_actor_name(context),
                    import_batch_id=import_batch_id,
                )
            )

        @mcp.tool(name="delete_imported_task", description="软删除尚未提交的导入任务。")
        def delete_imported_task(
            task_no: str,
            idempotency_key: str,
            context: Context,
            reason: str | None = None,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.cancel_imported_task(
                    actor_name=_actor_name(context),
                    task_no=task_no,
                    reason=reason,
                    idempotency_key=idempotency_key,
                )
            )

        @mcp.tool(name="change_task_assignee", description="把任务改派给其他在岗员工。")
        def change_task_assignee(
            task_no: str,
            new_employee_id: str,
            idempotency_key: str,
            context: Context,
            reason: str | None = None,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.reassign(
                    actor_name=_actor_name(context),
                    task_no=task_no,
                    new_employee_id=new_employee_id,
                    reason=reason,
                    idempotency_key=idempotency_key,
                )
            )

        @mcp.tool(name="set_task_priority", description="设置或取消任务优先处理标记。")
        def set_task_priority(
            task_no: str,
            priority: bool,
            idempotency_key: str,
            context: Context,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.set_priority(
                    actor_name=_actor_name(context),
                    task_no=task_no,
                    priority=priority,
                    idempotency_key=idempotency_key,
                )
            )

        @mcp.tool(name="list_employees", description="查询在岗员工及本周有效任务数。")
        def list_employees(context: Context) -> dict[str, Any]:
            return safe_call(lambda: t2.list_employees(actor_name=_actor_name(context)))

        @mcp.tool(name="list_content_projects", description="查询内容项目及当前阶段。")
        def list_content_projects(
            context: Context,
            status: str | None = None,
            limit: int = 50,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.list_projects(
                    actor_name=_actor_name(context), status=status, limit=limit
                )
            )

        @mcp.tool(name="get_content_project", description="查询项目、源文案和版本历史。")
        def get_content_project(task_no: str, context: Context) -> dict[str, Any]:
            return safe_call(
                lambda: t2.get_project(actor_name=_actor_name(context), task_no=task_no)
            )

        @mcp.tool(name="list_pending_reviews", description="查询待老板审核的演播稿。")
        def list_pending_reviews(context: Context) -> dict[str, Any]:
            return safe_call(lambda: t2.pending_reviews(actor_name=_actor_name(context)))

        @mcp.tool(name="review_script_submission", description="通过或驳回最新演播稿版本。")
        def review_script_submission(
            task_no: str,
            decision: str,
            idempotency_key: str,
            context: Context,
            comment: str | None = None,
            reason_category: str | None = None,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.review_script(
                    actor_name=_actor_name(context),
                    task_no=task_no,
                    decision=decision,
                    comment=comment,
                    reason_category=reason_category,
                    idempotency_key=idempotency_key,
                )
            )

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

        @mcp.tool(name="list_my_tasks", description="查询当前 Token 对应员工的本人任务。")
        def list_my_tasks(
            context: Context,
            status: str | None = None,
            limit: int = 50,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.list_my_tasks(
                    actor_name=_actor_name(context), status=status, limit=limit
                )
            )

        @mcp.tool(name="get_my_task", description="查询本人任务、源文案和审核历史。")
        def get_my_task(task_no: str, context: Context) -> dict[str, Any]:
            return safe_call(
                lambda: t2.get_my_task(actor_name=_actor_name(context), task_no=task_no)
            )

        @mcp.tool(name="submit_script_file", description="提交本人演播稿文件进入老板审核。")
        def submit_script_file(
            task_no: str,
            original_filename: str,
            idempotency_key: str,
            context: Context,
            content_base64: str | None = None,
            file_url: str | None = None,
            note: str | None = None,
        ) -> dict[str, Any]:
            return safe_call(
                lambda: t2.submit_script(
                    actor_name=_actor_name(context),
                    task_no=task_no,
                    original_filename=original_filename,
                    idempotency_key=idempotency_key,
                    content_base64=content_base64,
                    file_url=file_url,
                    note=note,
                )
            )

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
