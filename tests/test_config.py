from pydantic import SecretStr

from workflow.config import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "public_base_url": "http://testserver/",
        "probe_data_dir": "/tmp/workflow-t0-tests",
        "mcp_boss_token": SecretStr("boss-token-at-least-16"),
        "mcp_employees_json": (
            '[{"name":"员工甲","token":"employee-token-at-least-16",'
            '"wecom_userid":"employee-a","active":true}]'
        ),
    }
    values.update(overrides)
    return Settings(**values)


def test_actor_registry_has_distinct_roles() -> None:
    settings = make_settings()

    actors = settings.actors()

    assert [actor.role for actor in actors] == ["BOSS", "EMPLOYEE"]
    assert settings.public_base_url == "http://testserver"
    assert set(settings.token_registry()) == {
        "boss-token-at-least-16",
        "employee-token-at-least-16",
    }
    assert "localhost:*" in settings.allowed_hosts()
    assert "http://localhost:*" in settings.allowed_origins()


def test_known_wecom_userids_ignores_blank_values() -> None:
    settings = make_settings(mcp_boss_wecom_userid="")

    assert settings.known_wecom_userids() == {"employee-a"}


def test_public_base_url_is_automatically_allowed_for_mcp_transport() -> None:
    settings = make_settings(public_base_url="https://feishu.todoucloud.com")

    assert "feishu.todoucloud.com" in settings.allowed_hosts()
    assert "feishu.todoucloud.com:*" in settings.allowed_hosts()
    assert "https://feishu.todoucloud.com" in settings.allowed_origins()
    assert "https://feishu.todoucloud.com:*" in settings.allowed_origins()
