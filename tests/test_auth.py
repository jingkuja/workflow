import json
from typing import Any

import pytest

from workflow.auth import StaticBearerAuthMiddleware
from workflow.config import Actor


async def downstream_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    del receive
    body = json.dumps(scope.get("state", {})).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def invoke(app: Any, authorization: str | None) -> tuple[int, bytes]:
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    scope = {"type": "http", "path": "/mcp", "headers": headers}
    events: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(event: dict[str, Any]) -> None:
        events.append(event)

    await app(scope, receive, send)
    status = next(event["status"] for event in events if event["type"] == "http.response.start")
    body = b"".join(event.get("body", b"") for event in events)
    return status, body


@pytest.mark.asyncio
async def test_auth_accepts_matching_role() -> None:
    registry = {
        "boss-token-at-least-16": Actor(
            name="老板",
            role="BOSS",
            token="boss-token-at-least-16",
        )
    }
    app = StaticBearerAuthMiddleware(downstream_app, registry, "BOSS")

    status, body = await invoke(app, "Bearer boss-token-at-least-16")

    assert status == 200
    assert json.loads(body) == {"actor_name": "老板", "actor_role": "BOSS"}


@pytest.mark.asyncio
async def test_auth_rejects_missing_token() -> None:
    app = StaticBearerAuthMiddleware(downstream_app, {}, "BOSS")

    status, body = await invoke(app, None)

    assert status == 401
    assert json.loads(body)["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_auth_rejects_wrong_role() -> None:
    registry = {
        "employee-token-at-least-16": Actor(
            name="员工",
            role="EMPLOYEE",
            token="employee-token-at-least-16",
        )
    }
    app = StaticBearerAuthMiddleware(downstream_app, registry, "BOSS")

    status, body = await invoke(app, "Bearer employee-token-at-least-16")

    assert status == 403
    assert json.loads(body)["error"]["code"] == "FORBIDDEN"
