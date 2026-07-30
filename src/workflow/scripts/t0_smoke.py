from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T0 MCP 端到端冒烟与文件链路验证")
    parser.add_argument(
        "--base-url",
        default=os.getenv("T0_BASE_URL", "http://localhost:8080"),
    )
    parser.add_argument("--boss-url", default=os.getenv("T0_BOSS_URL"))
    parser.add_argument("--employee-url", default=os.getenv("T0_EMPLOYEE_URL"))
    parser.add_argument("--document", type=Path)
    parser.add_argument("--video", type=Path)
    return parser.parse_args()


async def call_server(
    url: str,
    token: str,
    expected_role_tool: str,
    forbidden_role_tool: str,
    document: Path | None = None,
    video: Path | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    started = time.perf_counter()
    async with (
        httpx.AsyncClient(headers=headers, timeout=300.0, trust_env=False) as http_client,
        streamable_http_client(url, http_client=http_client) as streams,
    ):
        read_stream, write_stream, _ = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool_names = sorted(tool.name for tool in listed.tools)
            if expected_role_tool not in tool_names:
                raise RuntimeError(f"{url} 缺少 {expected_role_tool}")
            if forbidden_role_tool in tool_names:
                raise RuntimeError(f"{url} 错误暴露 {forbidden_role_tool}")

            ping = await session.call_tool("t0_ping")
            result: dict[str, Any] = {
                "url": url,
                "tools": tool_names,
                "ping_error": ping.isError,
            }

            if document is not None:
                encoded = base64.b64encode(document.read_bytes()).decode("ascii")
                document_result = await session.call_tool(
                    "upload_file",
                    arguments={"file_base64": encoded},
                )
                result["document_error"] = document_result.isError
                result["document_upload"] = document_result.structuredContent

            if video is not None:
                video_started = time.perf_counter()
                encoded = base64.b64encode(video.read_bytes()).decode("ascii")
                result["video_encoded_chars"] = len(encoded)
                video_result = await session.call_tool(
                    "upload_file",
                    arguments={"file_base64": encoded},
                )
                result["video_error"] = video_result.isError
                result["video_upload"] = video_result.structuredContent
                result["video_elapsed_seconds"] = round(
                    time.perf_counter() - video_started,
                    3,
                )

    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    raw_max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["client_max_rss_bytes"] = raw_max_rss if sys.platform == "darwin" else raw_max_rss * 1024
    return result


async def async_main(args: argparse.Namespace) -> int:
    boss_token = os.getenv("MCP_BOSS_TOKEN")
    employees_json = os.getenv("MCP_EMPLOYEES_JSON", "")
    if not boss_token or not employees_json:
        raise SystemExit("请先加载 .env：MCP_BOSS_TOKEN 和 MCP_EMPLOYEES_JSON 必填")

    employees = json.loads(employees_json)
    employee_token = employees[0]["token"]
    base_url = args.base_url.rstrip("/")
    boss_url = args.boss_url or f"{base_url}/mcp/boss"
    employee_url = args.employee_url or f"{base_url}/mcp/employee"
    boss = await call_server(
        boss_url,
        boss_token,
        "t0_boss_capability",
        "t0_employee_capability",
        args.document,
        args.video,
    )
    employee = await call_server(
        employee_url,
        employee_token,
        "t0_employee_capability",
        "t0_boss_capability",
    )
    print(json.dumps({"boss": boss, "employee": employee}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
