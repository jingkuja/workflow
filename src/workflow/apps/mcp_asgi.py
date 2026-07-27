from __future__ import annotations

from typing import Any

from workflow.auth import StaticBearerAuthMiddleware
from workflow.config import Role, Settings


async def health_live(_: Any) -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


def build_authenticated_app(mcp: Any, settings: Settings, role: Role) -> Any:
    base_app = mcp.streamable_http_app()
    base_app.router.add_route("/health/live", health_live, methods=["GET"])
    token_registry = settings.token_registry()
    return StaticBearerAuthMiddleware(
        base_app,
        token_registry=token_registry,
        allowed_role=role,
        public_paths={"/health/live"},
    )
