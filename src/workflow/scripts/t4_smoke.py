from __future__ import annotations

import argparse
import asyncio
import json
import os

from workflow.scripts.t2_smoke import call_tool


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T4 MCP 看板、分页与运维查询冒烟")
    parser.add_argument("--base-url", default=os.getenv("T4_BASE_URL", "http://localhost:8080"))
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    boss_token = os.environ["MCP_BOSS_TOKEN"]
    boss_url = f"{args.base_url.rstrip('/')}/mcp/boss"
    dashboard = await call_tool(
        boss_url, boss_token, "get_workflow_dashboard", {}
    )
    projects = await call_tool(
        boss_url,
        boss_token,
        "list_content_projects",
        {"page": 1, "limit": 3},
    )
    pending = await call_tool(
        boss_url,
        boss_token,
        "list_pending_reviews",
        {"page": 1, "page_size": 2},
    )
    issues = await call_tool(
        boss_url,
        boss_token,
        "list_operational_issues",
        {"page": 1, "page_size": 5},
    )
    for payload in (dashboard, projects, pending, issues):
        assert payload["request_id"].startswith("req_")
    assert dashboard["data"]["normal_terminal_status"] == "WAITING_FOR_FILMING"
    assert projects["pagination"]["page_size"] == 3
    print(
        json.dumps(
            {
                "success": True,
                "normal_terminal_status": dashboard["data"]["normal_terminal_status"],
                "project_total": projects["pagination"]["total_items"],
                "pending_review_total": pending["pagination"]["total_items"],
                "operational_issue_total": issues["pagination"]["total_items"],
                "request_contract": "request_id present",
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    return asyncio.run(async_main(arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
