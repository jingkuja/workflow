#!/usr/bin/env python3
"""Upload a source document and import confirmed structured topics over REST."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from upload_file import upload_file

DEFAULT_BASE_URL = "https://aiflow.todoucloud.com"
DEFAULT_UPLOAD_TOKEN = "dev-file-upload-token-change-me"
DEFAULT_BOSS_TOKEN = "dev-boss-token-change-me"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="上传源 Word，并直接调用结构化选题 REST API 创建任务。"
    )
    parser.add_argument("document", type=Path, help="原始选题文档路径")
    parser.add_argument("topics_json", type=Path, help="老板确认后的结构化选题 JSON")
    parser.add_argument(
        "--idempotency-key",
        required=True,
        help="本次导入的稳定幂等键；失败重试时必须复用",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="工作流服务地址")
    parser.add_argument(
        "--upload-token",
        default=os.getenv("FILE_UPLOAD_TOKEN", DEFAULT_UPLOAD_TOKEN),
        help="文件上传 Token",
    )
    parser.add_argument(
        "--boss-token",
        default=os.getenv("WORKFLOW_BOSS_TOKEN", DEFAULT_BOSS_TOKEN),
        help="老板导入 Token",
    )
    parser.add_argument("--timeout", type=float, default=300, help="请求超时秒数")
    return parser.parse_args()


def load_topics(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"选题 JSON 不存在或不是普通文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取合法的 UTF-8 JSON：{path}") from exc

    if isinstance(raw, list):
        payload: dict[str, Any] = {
            "topics": raw,
            "warnings": [],
            "schema_version": "1.0",
        }
    elif isinstance(raw, dict):
        payload = {
            "topics": raw.get("topics"),
            "warnings": raw.get("warnings", []),
            "schema_version": raw.get("schema_version", "1.0"),
        }
    else:
        raise ValueError("选题 JSON 顶层必须是数组或对象。")

    if not isinstance(payload["topics"], list) or not payload["topics"]:
        raise ValueError("topics 必须是非空数组。")
    if not isinstance(payload["warnings"], list):
        raise ValueError("warnings 必须是数组。")
    if payload["schema_version"] != "1.0":
        raise ValueError("schema_version 只支持 1.0。")
    return payload


def post_json(
    endpoint: str,
    *,
    token: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    if not token:
        raise ValueError("老板导入 Token 不能为空。")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw_body = response.read()
    except HTTPError as exc:
        status = exc.code
        raw_body = exc.read()
    except URLError as exc:
        raise RuntimeError(f"无法连接结构化导入接口：{exc.reason}") from exc

    try:
        result = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"结构化导入接口返回了无效 JSON（HTTP {status}）。") from exc
    if status < 200 or status >= 300 or result.get("success") is False:
        message = result.get("error", {}).get("message") or result
        raise RuntimeError(f"结构化导入失败（HTTP {status}）：{message}")
    return result


def main() -> int:
    args = parse_args()
    try:
        document = args.document.expanduser()
        topics_payload = load_topics(args.topics_json.expanduser())
        base_url = args.base_url.rstrip("/") + "/"
        uploaded = upload_file(
            document,
            endpoint=urljoin(base_url, "api/files/upload"),
            token=args.upload_token,
            timeout=args.timeout,
        )
        request_payload = {
            "original_filename": document.name,
            "idempotency_key": args.idempotency_key,
            "file_key": uploaded["file_key"],
            **topics_payload,
        }
        result = post_json(
            urljoin(base_url, "api/topics/import-structured"),
            token=args.boss_token,
            payload=request_payload,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
