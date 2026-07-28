from __future__ import annotations

import base64

import pytest

from workflow.errors import ValidationFailed
from workflow.t2.files import decode_document_base64, receive_document


def test_base64_document_rejects_path_and_forged_signature() -> None:
    with pytest.raises(ValidationFailed, match="不能包含路径"):
        decode_document_base64(
            base64.b64encode(b"content").decode(),
            filename="../script.txt",
            allowed={".txt"},
            max_bytes=100,
        )
    with pytest.raises(ValidationFailed, match="文件头无效"):
        decode_document_base64(
            base64.b64encode(b"not a PDF").decode(),
            filename="script.pdf",
            allowed={".pdf"},
            max_bytes=100,
        )


def test_document_source_must_be_exclusive() -> None:
    with pytest.raises(ValidationFailed, match="必须二选一"):
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
    ],
)
def test_url_document_rejects_insecure_or_private_targets(url: str) -> None:
    with pytest.raises(ValidationFailed):
        receive_document(
            filename="script.txt",
            allowed={".txt"},
            max_bytes=100,
            content_base64=None,
            file_url=url,
        )
