#!/usr/bin/env python3
"""Upload one boss-local file and print the returned file_key as JSON."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = "https://aiflow.todoucloud.com/api/files/upload"
DEFAULT_TOKEN = "dev-file-upload-token-change-me"
CHUNK_SIZE = 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="上传老板本地文件并输出供工作流 REST API 使用的 file_key。"
    )
    parser.add_argument("file", type=Path, help="要上传的本地文件路径")
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"上传接口（默认：{DEFAULT_ENDPOINT}）",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("FILE_UPLOAD_TOKEN", DEFAULT_TOKEN),
        help="固定上传 Token（默认读取 FILE_UPLOAD_TOKEN）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="请求超时秒数（默认：300）",
    )
    return parser.parse_args()


def upload_file(
    file_path: Path, *, endpoint: str, token: str, timeout: float
) -> dict[str, Any]:
    if not file_path.is_file():
        raise ValueError(f"文件不存在或不是普通文件：{file_path}")
    if not token:
        raise ValueError("上传 Token 不能为空。")

    target = urlsplit(endpoint)
    if target.scheme not in {"http", "https"} or not target.hostname:
        raise ValueError("上传接口必须是完整的 http:// 或 https:// URL。")

    connection_class = (
        http.client.HTTPSConnection
        if target.scheme == "https"
        else http.client.HTTPConnection
    )
    connection_kwargs: dict[str, Any] = {"timeout": timeout}
    if target.scheme == "https":
        connection_kwargs["context"] = ssl.create_default_context()

    connection = connection_class(target.hostname, port=target.port, **connection_kwargs)
    request_target = target.path or "/"
    if target.query:
        request_target += f"?{target.query}"

    size = file_path.stat().st_size
    try:
        connection.putrequest("POST", request_target)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(size))
        connection.endheaders()
        with file_path.open("rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                connection.send(chunk)

        response = connection.getresponse()
        raw_body = response.read()
        status = response.status
    finally:
        connection.close()

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"上传接口返回了无效 JSON（HTTP {status}）。") from exc

    if status < 200 or status >= 300 or payload.get("success") is False:
        message = payload.get("error", {}).get("message") or payload
        raise RuntimeError(f"上传失败（HTTP {status}）：{message}")

    data = payload.get("data", payload)
    file_key = data.get("file_key")
    if not file_key:
        raise RuntimeError("上传响应中没有 file_key。")

    return {
        "file_key": file_key,
        "original_filename": file_path.name,
        "size_bytes": data.get("size_bytes", size),
        "sha256": data.get("sha256"),
        "expires_at": data.get("expires_at"),
    }


def main() -> int:
    args = parse_args()
    try:
        result = upload_file(
            args.file.expanduser(),
            endpoint=args.endpoint,
            token=args.token,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({"success": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
