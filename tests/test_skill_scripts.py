from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def load_boss_import_script():
    scripts_dir = Path("skill/boss/scripts").resolve()
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "boss_import_topics",
        scripts_dir / "import_topics.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_boss_import_script_uploads_then_imports(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = load_boss_import_script()
    document = tmp_path / "选题.docx"
    document.write_bytes(b"word")
    topics_json = tmp_path / "topics.json"
    topics_json.write_text(
        json.dumps(
            {
                "topics": [
                    {
                        "title": "测试选题",
                        "source_text": "测试原文",
                        "script": None,
                        "confidence": 0.9,
                        "evidence": ["测试原文"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: dict[str, Any] = {}

    def fake_upload(file_path, *, endpoint, token, timeout):
        calls["upload"] = {
            "file_path": file_path,
            "endpoint": endpoint,
            "token": token,
            "timeout": timeout,
        }
        return {"file_key": "file-key-123"}

    def fake_post(endpoint, *, token, payload, timeout):
        calls["import"] = {
            "endpoint": endpoint,
            "token": token,
            "payload": payload,
            "timeout": timeout,
        }
        return {
            "success": True,
            "data": {
                "created_count": 1,
                "deduplicated": False,
                "tasks": [{"task_no": "WJ-1", "title": "测试选题"}],
            },
        }

    monkeypatch.setattr(module, "upload_file", fake_upload)
    monkeypatch.setattr(module, "post_json", fake_post)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import_topics.py",
            str(document),
            str(topics_json),
            "--idempotency-key",
            "boss-import-test-0001",
        ],
    )

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["created_count"] == 1
    assert calls["upload"]["endpoint"].endswith("/api/files/upload")
    assert calls["import"]["endpoint"].endswith("/api/topics/import-structured")
    assert calls["import"]["payload"]["file_key"] == "file-key-123"
    assert calls["import"]["payload"]["idempotency_key"] == "boss-import-test-0001"
    assert calls["import"]["payload"]["topics"][0]["title"] == "测试选题"
