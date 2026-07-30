from pathlib import Path
from typing import Any

from pydantic import SecretStr

from workflow.apps.mcp_factory import create_mcp_server
from workflow.config import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://testserver",
        probe_data_dir=tmp_path,
        file_data_dir=tmp_path / "files",
        mcp_boss_token=SecretStr("boss-token-at-least-16"),
        mcp_employees_json=(
            '[{"name":"员工甲","token":"employee-token-at-least-16","active":true}]'
        ),
    )


def tool_names(server: Any) -> set[str]:
    manager = server._tool_manager
    tools = manager.list_tools()
    return {tool.name for tool in tools}


def tool_parameters(server: Any, name: str) -> dict[str, Any]:
    tool = next(item for item in server._tool_manager.list_tools() if item.name == name)
    return tool.parameters["properties"]


def test_boss_and_employee_tool_whitelists_are_isolated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    boss_server = create_mcp_server(settings, "BOSS")
    employee_server = create_mcp_server(settings, "EMPLOYEE")
    boss_tools = tool_names(boss_server)
    employee_tools = tool_names(employee_server)

    assert "upload_file" in boss_tools
    assert "upload_file" in employee_tools
    assert "t0_boss_capability" in boss_tools
    assert "t0_employee_capability" not in boss_tools
    assert "t0_employee_capability" in employee_tools
    assert "t0_boss_capability" not in employee_tools
    assert "t0_probe_wecom_mention" in boss_tools
    assert "t0_probe_wecom_mention" not in employee_tools
    assert "import_topic_document" in boss_tools
    assert "import_structured_topics" in boss_tools
    assert "change_task_assignee" in boss_tools
    assert "review_script_submission" in boss_tools
    assert "get_workflow_dashboard" in boss_tools
    assert "list_operational_issues" in boss_tools
    assert "submit_script_file" not in boss_tools
    assert "submit_script_file" in employee_tools
    assert "report_task_blocker" in employee_tools
    assert "get_workflow_dashboard" not in employee_tools
    assert "import_topic_document" not in employee_tools
    assert "import_structured_topics" not in employee_tools
    assert "change_task_assignee" not in employee_tools

    upload_input = tool_parameters(boss_server, "upload_file")["file_base64"]
    assert upload_input["contentEncoding"] == "base64"
    assert upload_input["contentMediaType"] == "application/octet-stream"
    assert upload_input["format"] == "byte"

    for server, tool_name in (
        (boss_server, "import_topic_document"),
        (boss_server, "import_structured_topics"),
        (employee_server, "submit_script_file"),
    ):
        parameters = tool_parameters(server, tool_name)
        assert "file_key" in parameters
        assert all("base64" not in name for name in parameters)
