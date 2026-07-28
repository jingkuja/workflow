from __future__ import annotations

import base64
import binascii
import ipaddress
import socket
from pathlib import Path, PurePath
from urllib.parse import urlsplit

import httpx

from workflow.errors import ValidationFailed

DOCUMENT_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


def validate_filename(filename: str, allowed: set[str]) -> str:
    if not filename or PurePath(filename).name != filename:
        raise ValidationFailed("文件名不能为空且不能包含路径。")
    extension = Path(filename).suffix.lower()
    if extension not in allowed:
        raise ValidationFailed(f"不支持文件扩展名 {extension or '(无扩展名)'}。")
    return extension


def decode_document_base64(
    encoded: str, *, filename: str, allowed: set[str], max_bytes: int
) -> bytes:
    extension = validate_filename(filename, allowed)
    if encoded.startswith("data:"):
        raise ValidationFailed("请传入纯 Base64，不要使用 data URI。")
    estimated = len(encoded) * 3 // 4
    if estimated > max_bytes:
        raise ValidationFailed(f"文件超过技术上限 {max_bytes} 字节。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValidationFailed("Base64 内容格式错误。") from exc
    if len(content) > max_bytes:
        raise ValidationFailed(f"文件超过技术上限 {max_bytes} 字节。")
    validate_signature(content, extension)
    return content


def validate_signature(content: bytes, extension: str) -> None:
    if extension == ".docx" and not content.startswith(b"PK"):
        raise ValidationFailed(".docx 文件头无效。")
    if extension == ".pdf" and not content.startswith(b"%PDF"):
        raise ValidationFailed(".pdf 文件头无效。")
    if extension in {".md", ".txt"} and b"\x00" in content[:4096]:
        raise ValidationFailed("文本文件包含 NUL 字节。")


def download_document(url: str, *, filename: str, allowed: set[str], max_bytes: int) -> bytes:
    extension = validate_filename(filename, allowed)
    current = url
    for _ in range(4):
        _validate_public_https_url(current)
        with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(20, connect=5)) as client:
            response = client.get(current, headers={"User-Agent": "workflow-document-fetch/1"})
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise ValidationFailed("文件 URL 重定向缺少 Location。")
            current = str(httpx.URL(current).join(location))
            continue
        response.raise_for_status()
        content = response.content
        if len(content) > max_bytes:
            raise ValidationFailed(f"下载文件超过技术上限 {max_bytes} 字节。")
        validate_signature(content, extension)
        return content
    raise ValidationFailed("文件 URL 重定向次数超过限制。")


def receive_document(
    *,
    filename: str,
    allowed: set[str],
    max_bytes: int,
    content_base64: str | None,
    file_url: str | None,
) -> bytes:
    if (content_base64 is None) == (file_url is None):
        raise ValidationFailed("content_base64 与 file_url 必须二选一。")
    if content_base64 is not None:
        return decode_document_base64(
            content_base64, filename=filename, allowed=allowed, max_bytes=max_bytes
        )
    return download_document(
        file_url or "", filename=filename, allowed=allowed, max_bytes=max_bytes
    )


def _validate_public_https_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationFailed("文件 URL 必须是无用户信息的 HTTPS 地址。")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise ValidationFailed("文件 URL 域名无法解析。") from exc
    if not addresses:
        raise ValidationFailed("文件 URL 域名没有可用地址。")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise ValidationFailed("文件 URL 不能指向内网、环回或保留地址。")
