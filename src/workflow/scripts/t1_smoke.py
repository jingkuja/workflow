from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


def request(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    value = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(10):
        try:
            with urllib.request.urlopen(value, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
        except urllib.error.URLError:
            if attempt == 9:
                raise
            time.sleep(1)
    raise RuntimeError("unreachable")


def main() -> None:
    boss_token = os.environ["MCP_BOSS_TOKEN"]
    employees = json.loads(os.environ["MCP_EMPLOYEES_JSON"])
    employee_token = employees[0]["token"]
    key = f"t1-smoke-{uuid.uuid4()}"

    boss_identity = request("GET", "/internal/t1/identity", boss_token)
    employee_identity = request("GET", "/internal/t1/identity", employee_token)
    invalid_identity = request("GET", "/internal/t1/identity", "invalid-token-value")
    employee_forbidden = request("POST", "/internal/t1/jobs/noop", employee_token)
    first = request(
        "POST",
        "/internal/t1/idempotency-probe",
        boss_token,
        {"idempotency_key": key, "value": "same"},
    )
    replay = request(
        "POST",
        "/internal/t1/idempotency-probe",
        boss_token,
        {"idempotency_key": key, "value": "same"},
    )
    conflict = request(
        "POST",
        "/internal/t1/idempotency-probe",
        boss_token,
        {"idempotency_key": key, "value": "different"},
    )
    created_job = request("POST", "/internal/t1/jobs/noop", boss_token)
    time.sleep(2)
    status = request("GET", "/internal/t1/status", boss_token)

    assert boss_identity[0] == 200
    assert employee_identity[0] == 200
    assert invalid_identity[0] == 401
    assert employee_forbidden[0] == 403
    assert first[0] == 200 and first[1]["deduplicated"] is False
    assert replay[0] == 200 and replay[1]["deduplicated"] is True
    assert conflict[0] == 409
    assert created_job[0] == 200
    assert status[0] == 200
    print(
        json.dumps(
            {
                "success": True,
                "identity_matrix": [200, 200, 401, 403],
                "idempotency_matrix": [200, 200, 409],
                "counts": status[1]["data"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
