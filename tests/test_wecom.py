from pathlib import Path

import pytest
from pydantic import SecretStr

from workflow.config import Settings
from workflow.probes.wecom import send_wecom_probe


@pytest.mark.asyncio
async def test_wecom_probe_is_safe_by_default(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        probe_data_dir=tmp_path,
        mcp_boss_token=SecretStr("boss-token-at-least-16"),
        mcp_boss_wecom_userid="boss-userid",
        mcp_employees_json=(
            '[{"name":"员工甲","token":"employee-token-at-least-16",'
            '"wecom_userid":"employee-a","active":true}]'
        ),
        t0_allow_wecom_send=False,
    )

    result = await send_wecom_probe(settings, "不应真实发送", ["boss-userid"])

    assert result["sent"] is False
    assert "T0_ALLOW_WECOM_SEND=false" in str(result["reason"])
