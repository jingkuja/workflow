from __future__ import annotations

import json

import httpx

from workflow.apps.api_client import WorkflowApiClient


def test_client_forwards_token_and_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": {"ok": True}})

    client = WorkflowApiClient(
        "http://workflow-api:8000", transport=httpx.MockTransport(handler)
    )
    result = client.call(
        "/internal/tools/list-employees",
        token="boss-token-at-least-16",
        tool="list_employees",
        payload={"ping": 1},
    )

    assert captured["authorization"] == "Bearer boss-token-at-least-16"
    assert captured["body"] == {"ping": 1}
    assert result == {"success": True, "data": {"ok": True}}


def test_client_passes_through_unified_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "success": False,
                "error": {"code": "INVALID_STATE_TRANSITION", "message": "状态不允许。"},
            },
        )

    client = WorkflowApiClient(
        "http://workflow-api:8000", transport=httpx.MockTransport(handler)
    )
    result = client.call(
        "/internal/tools/review-script-submission",
        token="boss-token-at-least-16",
        tool="review_script_submission",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_client_maps_network_failure_to_external_dependency() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = WorkflowApiClient(
        "http://workflow-api:8000", transport=httpx.MockTransport(handler)
    )
    result = client.call(
        "/internal/tools/list-employees",
        token="boss-token-at-least-16",
        tool="list_employees",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "EXTERNAL_DEPENDENCY_FAILED"
