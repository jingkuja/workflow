from __future__ import annotations

import base64

import pytest

from workflow.errors import (
    FileTooLarge,
    InvalidArgument,
    UnsupportedFileType,
)
from workflow.t2.files import decode_document_base64, receive_document


def test_base64_document_rejects_path_and_forged_signature() -> None:
    with pytest.raises(InvalidArgument, match="不能包含路径"):
        decode_document_base64(
            base64.b64encode(b"content").decode(),
            filename="../script.txt",
            allowed={".txt"},
            max_bytes=100,
        )
    with pytest.raises(InvalidArgument, match="文件头无效"):
        decode_document_base64(
            base64.b64encode(b"not a PDF").decode(),
            filename="script.pdf",
            allowed={".pdf"},
            max_bytes=100,
        )


def test_unsupported_extension_and_oversize_use_spec_error_codes() -> None:
    with pytest.raises(UnsupportedFileType):
        decode_document_base64(
            base64.b64encode(b"content").decode(),
            filename="script.exe",
            allowed={".txt"},
            max_bytes=100,
        )
    with pytest.raises(FileTooLarge):
        decode_document_base64(
            base64.b64encode(b"x" * 200).decode(),
            filename="script.txt",
            allowed={".txt"},
            max_bytes=100,
        )


def test_document_source_must_be_exclusive() -> None:
    with pytest.raises(InvalidArgument, match="必须二选一"):
        receive_document(
            filename="script.txt",
            allowed={".txt"},
            max_bytes=100,
            content_base64=None,
            file_url=None,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/script.txt",
        "https://127.0.0.1/script.txt",
        "https://user:password@example.com/script.txt",
        "https://[::1]/script.txt",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_url_document_rejects_insecure_or_private_targets(url: str) -> None:
    with pytest.raises(InvalidArgument):
        receive_document(
            filename="script.txt",
            allowed={".txt"},
            max_bytes=100,
            content_base64=None,
            file_url=url,
        )
