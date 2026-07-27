from __future__ import annotations

import hmac
import json
from collections.abc import Iterable
from typing import Any

from workflow.config import Actor, Role


class StaticBearerAuthMiddleware:
    """固定 Token 的轻量 T0 ASGI 鉴权。

    采用纯 ASGI 中间件，避免 BaseHTTPMiddleware 改变 Streamable HTTP 的流行为。
    """

    def __init__(
        self,
        app: Any,
        token_registry: dict[str, Actor],
        allowed_role: Role,
        public_paths: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.token_registry = token_registry
        self.allowed_role = allowed_role
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in self.public_paths:
            await self.app(scope, receive, send)
            return

        token = self._extract_bearer_token(scope)
        if token is None:
            await self._send_error(send, 401, "UNAUTHENTICATED", "缺少 Bearer Token。")
            return

        actor = self._find_actor(token)
        if actor is None:
            await self._send_error(send, 401, "UNAUTHENTICATED", "Token 无效。")
            return
        if actor.role != self.allowed_role:
            await self._send_error(send, 403, "FORBIDDEN", "该 Token 无权访问此 MCP 入口。")
            return

        scope.setdefault("state", {})
        scope["state"]["actor_name"] = actor.name
        scope["state"]["actor_role"] = actor.role
        await self.app(scope, receive, send)

    @staticmethod
    def _extract_bearer_token(scope: dict[str, Any]) -> str | None:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() != b"authorization":
                continue
            value = raw_value.decode("latin-1")
            scheme, separator, token = value.partition(" ")
            if separator and scheme.lower() == "bearer" and token.strip():
                return token.strip()
        return None

    def _find_actor(self, supplied_token: str) -> Actor | None:
        for configured_token, actor in self.token_registry.items():
            if hmac.compare_digest(supplied_token, configured_token):
                return actor
        return None

    @staticmethod
    async def _send_error(
        send: Any,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        body = json.dumps(
            {"success": False, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
