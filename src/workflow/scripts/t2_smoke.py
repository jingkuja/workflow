from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T2 MCP 端到端业务闭环验证")
    parser.add_argument("--base-url", default=os.getenv("T2_BASE_URL", "http://localhost:8080"))
    parser.add_argument(
        "--document",
        type=Path,
        default=Path("docs/AI行业选题文档上传样例.docx"),
    )
    parser.add_argument("--concurrency-check", action="store_true")
    return parser.parse_args()


async def call_tool(url: str, token: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
            trust_env=False,
        ) as client,
        streamable_http_client(url, http_client=client) as streams,
    ):
        read_stream, write_stream, _ = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments=arguments)
            if result.isError:
                raise RuntimeError(f"{tool} MCP error: {result.content}")
            payload = result.structuredContent
            if payload is None:
                raise RuntimeError(f"{tool} 未返回结构化内容")
            if payload.get("success") is False:
                raise RuntimeError(f"{tool} 业务失败: {payload}")
            return payload


async def async_main(args: argparse.Namespace) -> int:
    boss_token = os.environ["MCP_BOSS_TOKEN"]
    employees = json.loads(os.environ["MCP_EMPLOYEES_JSON"])
    employee_token = employees[0]["token"]
    base = args.base_url.rstrip("/")
    boss_url = f"{base}/mcp/boss"
    employee_url = f"{base}/mcp/employee"
    encoded_word = base64.b64encode(args.document.read_bytes()).decode()
    topic_upload = await call_tool(
        boss_url,
        boss_token,
        "upload_file",
        {"file_base64": encoded_word},
    )
    topic_file_key = topic_upload["data"]["file_key"]

    concurrency_result: dict[str, Any] | None = None
    if args.concurrency_check:
        source = BytesIO(args.document.read_bytes())
        output = BytesIO()
        with (
            zipfile.ZipFile(source) as source_zip,
            zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target_zip,
        ):
            for item in source_zip.infolist():
                target_zip.writestr(item, source_zip.read(item.filename))
            target_zip.writestr(f"t2-concurrency-{uuid.uuid4().hex}.txt", "probe")
        concurrent_encoded = base64.b64encode(output.getvalue()).decode()
        concurrent_upload = await call_tool(
            boss_url,
            boss_token,
            "upload_file",
            {"file_base64": concurrent_encoded},
        )
        concurrent_file_key = concurrent_upload["data"]["file_key"]

        async def concurrent_import() -> dict[str, Any]:
            return await call_tool(
                boss_url,
                boss_token,
                "import_topic_document",
                {
                    "original_filename": args.document.name,
                    "file_key": concurrent_file_key,
                    "idempotency_key": f"t2-concurrent-{uuid.uuid4().hex}",
                },
            )

        concurrent = await asyncio.gather(concurrent_import(), concurrent_import())
        created_counts = sorted(item["data"]["created_count"] for item in concurrent)
        assert created_counts == [0, 10]
        assert sum(bool(item["data"]["deduplicated"]) for item in concurrent) == 1
        concurrency_result = {"created_counts": created_counts}

    imported = await call_tool(
        boss_url,
        boss_token,
        "import_topic_document",
        {
            "original_filename": args.document.name,
            "file_key": topic_file_key,
            "idempotency_key": f"t2-import-{uuid.uuid4().hex}",
        },
    )
    tasks = imported["data"]["tasks"]
    assert len(tasks) == 10
    duplicate = await call_tool(
        boss_url,
        boss_token,
        "import_topic_document",
        {
            "original_filename": args.document.name,
            "file_key": topic_file_key,
            "idempotency_key": f"t2-import-duplicate-{uuid.uuid4().hex}",
        },
    )
    assert duplicate["data"]["deduplicated"] is True
    target = next(task for task in tasks if task["status"] in {"IN_PROGRESS", "REJECTED"})
    task_no = target["task_no"]

    mine = await call_tool(
        employee_url,
        employee_token,
        "get_my_task",
        {"task_no": task_no},
    )
    assert mine["data"]["task"]["task_no"] == task_no
    first_content = base64.b64encode("第一版演播稿".encode()).decode()
    first_upload = await call_tool(
        employee_url,
        employee_token,
        "upload_file",
        {"file_base64": first_content},
    )
    first_file_key = first_upload["data"]["file_key"]
    first_key = f"t2-submit-{uuid.uuid4().hex}"
    first = await call_tool(
        employee_url,
        employee_token,
        "submit_script_file",
        {
            "task_no": task_no,
            "original_filename": "演播稿-v1.txt",
            "file_key": first_file_key,
            "note": "T2 冒烟第一版",
            "idempotency_key": first_key,
        },
    )
    replay = await call_tool(
        employee_url,
        employee_token,
        "submit_script_file",
        {
            "task_no": task_no,
            "original_filename": "演播稿-v1.txt",
            "file_key": first_file_key,
            "note": "T2 冒烟第一版",
            "idempotency_key": first_key,
        },
    )
    assert first["data"]["version_no"] == replay["data"]["version_no"] == 1

    rejected = await call_tool(
        boss_url,
        boss_token,
        "review_script_submission",
        {
            "task_no": task_no,
            "decision": "REJECTED",
            "comment": "请补充更明确的开场钩子。",
            "reason_category": "OPENING_HOOK",
            "idempotency_key": f"t2-review-{uuid.uuid4().hex}",
        },
    )
    assert rejected["data"]["task_status"] == "REJECTED"

    second_content = base64.b64encode("第二版演播稿，已补充开场钩子。".encode()).decode()
    second_upload = await call_tool(
        employee_url,
        employee_token,
        "upload_file",
        {"file_base64": second_content},
    )
    second = await call_tool(
        employee_url,
        employee_token,
        "submit_script_file",
        {
            "task_no": task_no,
            "original_filename": "演播稿-v2.md",
            "file_key": second_upload["data"]["file_key"],
            "note": "按驳回意见修改",
            "idempotency_key": f"t2-submit-{uuid.uuid4().hex}",
        },
    )
    assert second["data"]["version_no"] == 2
    approved = await call_tool(
        boss_url,
        boss_token,
        "review_script_submission",
        {
            "task_no": task_no,
            "decision": "APPROVED",
            "comment": "通过",
            "idempotency_key": f"t2-review-{uuid.uuid4().hex}",
        },
    )
    assert approved["data"]["project_status"] == "WAITING_FOR_FILMING"
    detail = await call_tool(
        boss_url,
        boss_token,
        "get_content_project",
        {"task_no": task_no},
    )
    assert len(detail["data"]["submission_history"]) == 2
    print(
        json.dumps(
            {
                "success": True,
                "import_batch_id": imported["data"]["import_batch_id"],
                "task_count": len(tasks),
                "deduplicated": duplicate["data"]["deduplicated"],
                "completed_task": task_no,
                "versions": 2,
                "final_status": approved["data"]["project_status"],
                "concurrency": concurrency_result,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    return asyncio.run(async_main(arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
