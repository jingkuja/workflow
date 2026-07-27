import base64
from pathlib import Path

import pytest
from pydantic import SecretStr

from workflow.config import Settings
from workflow.probes.service import ProbeService
from workflow.probes.storage import ProbeValidationError, estimated_decoded_size


def make_service(tmp_path: Path, role: str = "BOSS") -> ProbeService:
    settings = Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://testserver",
        probe_data_dir=tmp_path,
        mcp_boss_token=SecretStr("boss-token-at-least-16"),
        mcp_employees_json=(
            '[{"name":"员工甲","token":"employee-token-at-least-16","active":true}]'
        ),
        max_topic_document_bytes=1024 * 1024,
        max_script_document_bytes=1024 * 1024,
        max_video_bytes=1024 * 1024,
    )
    return ProbeService(settings, role)  # type: ignore[arg-type]


def test_document_probe_saves_and_reuses_idempotency_key(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    encoded = base64.b64encode(b"PK\x03\x04minimal-ooxml-probe").decode()

    first = service.probe_document(
        original_filename="sample.docx",
        content_base64=encoded,
        file_url=None,
        idempotency_key="document-probe-001",
    )
    second = service.probe_document(
        original_filename="sample.docx",
        content_base64=encoded,
        file_url=None,
        idempotency_key="document-probe-001",
    )

    first_file = first.data["file"]
    second_file = second.data["file"]
    assert first_file["opaque_file_id"] == second_file["opaque_file_id"]
    assert (
        first_file["mime_type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert first.data["deduplicated"] is False
    assert second.data["deduplicated"] is True
    assert Path(tmp_path / "files" / first_file["opaque_file_id"]).read_bytes().startswith(b"PK")


def test_video_probe_validates_mp4_header(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    mp4 = b"\x00\x00\x00\x18ftypisom" + (b"x" * 32)

    result = service.probe_video(
        original_filename="sample.mp4",
        video_base64=base64.b64encode(mp4).decode(),
        idempotency_key="video-probe-001",
    )

    assert result.data["file"]["size_bytes"] == len(mp4)
    assert result.data["received_shape"] == "base64"


def test_video_probe_rejects_wrong_signature(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ProbeValidationError, match="视频文件头"):
        service.probe_video(
            original_filename="sample.mp4",
            video_base64=base64.b64encode(b"not-an-mp4").decode(),
            idempotency_key="video-probe-002",
        )


def test_document_url_must_be_https(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ProbeValidationError, match="HTTPS"):
        service.probe_document(
            original_filename="sample.docx",
            content_base64=None,
            file_url="http://example.com/sample.docx",
            idempotency_key="document-url-001",
        )


def test_decoded_size_and_data_uri_rejection() -> None:
    encoded = base64.b64encode(b"123456789").decode()

    assert estimated_decoded_size(encoded) == 9
    with pytest.raises(ProbeValidationError, match="data: URI"):
        estimated_decoded_size(f"data:video/mp4;base64,{encoded}")
