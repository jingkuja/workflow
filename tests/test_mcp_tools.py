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


def test_boss_and_employee_tool_whitelists_are_isolated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    boss_tools = tool_names(create_mcp_server(settings, "BOSS"))
    employee_tools = tool_names(create_mcp_server(settings, "EMPLOYEE"))

    assert "t0_boss_capability" in boss_tools
    assert "t0_employee_capability" not in boss_tools
    assert "t0_employee_capability" in employee_tools
    assert "t0_boss_capability" not in employee_tools
    assert "t0_probe_wecom_mention" in boss_tools
    assert "t0_probe_wecom_mention" not in employee_tools
    assert "import_topic_document" in boss_tools
    assert "review_script_submission" in boss_tools
    assert "submit_script_file" not in boss_tools
    assert "submit_script_file" in employee_tools
    assert "import_topic_document" not in employee_tools
