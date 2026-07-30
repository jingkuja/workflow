from pathlib import Path


def test_nginx_uses_dynamic_docker_dns_for_recreated_upstreams() -> None:
    config = Path("deploy/nginx/nginx.conf").read_text()

    assert "resolver 127.0.0.11" in config
    assert "server boss-mcp:8000 resolve;" in config
    assert "server employee-mcp:8000 resolve;" in config
    assert "server workflow-api:8000 resolve;" in config
    assert "location = /api/files/upload" in config
    assert "location = /file-upload" in config
    assert "proxy_request_buffering off;" in config
