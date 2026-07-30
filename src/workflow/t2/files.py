from __future__ import annotations

import base64
import binascii
import http.client
import ipaddress
import socket
import ssl
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

from workflow.errors import (
    ExternalDependencyFailed,
    FileTooLarge,
    InvalidArgument,
    UnsupportedFileType,
)

DOCUMENT_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
}

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 4
_DOWNLOAD_CHUNK = 64 * 1024


def validate_filename(filename: str, allowed: set[str]) -> str:
    if not filename or PurePath(filename).name != filename:
        raise InvalidArgument("文件名不能为空且不能包含路径。")
    extension = Path(filename).suffix.lower()
    if extension not in allowed:
        raise UnsupportedFileType(f"不支持文件扩展名 {extension or '(无扩展名)'}。")
    return extension


def decode_document_base64(
    encoded: str, *, filename: str, allowed: set[str], max_bytes: int
) -> bytes:
    extension = validate_filename(filename, allowed)
    if encoded.startswith("data:"):
        raise InvalidArgument("请传入纯 Base64，不要使用 data URI。")
    estimated = len(encoded) * 3 // 4
    if estimated > max_bytes:
        raise FileTooLarge(f"文件超过技术上限 {max_bytes} 字节。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise InvalidArgument("Base64 内容格式错误。") from exc
    if len(content) > max_bytes:
        raise FileTooLarge(f"文件超过技术上限 {max_bytes} 字节。")
    validate_signature(content, extension)
    return content


def validate_signature(content: bytes, extension: str) -> None:
    if extension == ".docx" and not content.startswith(b"PK"):
        raise InvalidArgument(".docx 文件头无效。")
    if extension == ".pdf" and not content.startswith(b"%PDF"):
        raise InvalidArgument(".pdf 文件头无效。")
    if extension in {".md", ".txt"} and b"\x00" in content[:4096]:
        raise InvalidArgument("文本文件包含 NUL 字节。")


def download_document(url: str, *, filename: str, allowed: set[str], max_bytes: int) -> bytes:
    with open_downloaded_document(
        url,
        filename=filename,
        allowed=allowed,
        max_bytes=max_bytes,
    ) as stream:
        return stream.read()


@contextmanager
def open_downloaded_document(
    url: str,
    *,
    filename: str,
    allowed: set[str],
    max_bytes: int,
) -> Iterator[BinaryIO]:
    extension = validate_filename(filename, allowed)
    current = url
    with tempfile.SpooledTemporaryFile(max_size=_DOWNLOAD_CHUNK) as stream:
        for _ in range(_MAX_REDIRECTS):
            status, location = _fetch_https_pinned_into(
                current,
                target=stream,
                max_bytes=max_bytes,
            )
            if status in _REDIRECT_STATUSES:
                if not location:
                    raise ExternalDependencyFailed("文件 URL 重定向缺少 Location。")
                current = urljoin(current, location)
                continue
            if status != 200:
                raise ExternalDependencyFailed(f"文件 URL 返回 HTTP {status}。")
            stream.seek(0)
            validate_signature(stream.read(4096), extension)
            stream.seek(0)
            yield stream
            return
        raise ExternalDependencyFailed("文件 URL 重定向次数超过限制。")


def receive_document(
    *,
    filename: str,
    allowed: set[str],
    max_bytes: int,
    content_base64: str | None,
    file_url: str | None,
) -> bytes:
    if (content_base64 is None) == (file_url is None):
        raise InvalidArgument("content_base64 与 file_url 必须二选一。")
    if content_base64 is not None:
        return decode_document_base64(
            content_base64, filename=filename, allowed=allowed, max_bytes=max_bytes
        )
    return download_document(
        file_url or "", filename=filename, allowed=allowed, max_bytes=max_bytes
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP 连接固定到已校验的 IP，SNI 与 Host 头仍使用原域名。

    消除"先解析校验、连接时重新解析"之间的 DNS 重绑定（TOCTOU）窗口。
    """

    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _parse_https_url(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InvalidArgument("文件 URL 必须是无用户信息的 HTTPS 地址。")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parsed.hostname, parsed.port or 443, path


def _resolve_public_ips(hostname: str, port: int) -> list[str]:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise InvalidArgument("文件 URL 域名无法解析。") from exc
    if not addresses:
        raise InvalidArgument("文件 URL 域名没有可用地址。")
    for raw in addresses:
        if not ipaddress.ip_address(raw).is_global:
            raise InvalidArgument("文件 URL 不能指向内网、环回或保留地址。")
    return sorted(addresses)


def _fetch_https_pinned(url: str, *, max_bytes: int) -> tuple[int, str | None, bytes]:
    with tempfile.SpooledTemporaryFile(max_size=_DOWNLOAD_CHUNK) as stream:
        status, location = _fetch_https_pinned_into(
            url,
            target=stream,
            max_bytes=max_bytes,
        )
        stream.seek(0)
        return status, location, stream.read()


def _fetch_https_pinned_into(
    url: str,
    *,
    target: BinaryIO,
    max_bytes: int,
) -> tuple[int, str | None]:
    host, port, path = _parse_https_url(url)
    ips = _resolve_public_ips(host, port)
    last_error: Exception | None = None
    for ip in ips:
        target.seek(0)
        target.truncate()
        connection = _PinnedHTTPSConnection(host, ip, port, timeout=20.0)
        try:
            connection.connect()
            connection.putrequest("GET", path, skip_accept_encoding=True)
            connection.putheader("User-Agent", "workflow-document-fetch/1")
            connection.endheaders()
            response = connection.getresponse()
            status = response.status
            if status in _REDIRECT_STATUSES:
                return status, response.getheader("Location")
            if status != 200:
                return status, None
            content_length = response.getheader("Content-Length")
            if (
                content_length is not None
                and content_length.isdigit()
                and int(content_length) > max_bytes
            ):
                raise FileTooLarge(f"下载文件超过技术上限 {max_bytes} 字节。")
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = response.read(min(_DOWNLOAD_CHUNK, remaining))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
            if target.tell() > max_bytes:
                raise FileTooLarge(f"下载文件超过技术上限 {max_bytes} 字节。")
            target.seek(0)
            return status, None
        except (InvalidArgument, FileTooLarge):
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last_error = exc
        finally:
            connection.close()
    raise ExternalDependencyFailed(f"文件 URL 下载失败：{last_error}")
