from __future__ import annotations

import base64
from io import BytesIO

import pytest
from docx import Document
from sqlalchemy import func, select
from test_t2_service_flow import make_service

from workflow.db.models import ContentProject, ImportBatch
from workflow.errors import InvalidArgument
from workflow.t2.contracts import StructuredTopicInput


def _arbitrary_document() -> bytes:
    document = Document()
    document.add_paragraph("本周 AI 市场观察")
    document.add_paragraph("人工智能代理正在进入企业流程，这是第一条候选选题的对应原文。")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "补充观察"
    table.cell(0, 1).text = "算力成本下降正在改变中小企业采用 AI 的节奏。"
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_structured_import_accepts_arbitrary_word_layout_and_deduplicates(tmp_path) -> None:
    service = make_service(tmp_path)
    content = _arbitrary_document()
    encoded = base64.b64encode(content).decode()
    topics = [
        StructuredTopicInput(
            source_index="段落 2",
            title="AI 代理进入企业工作流",
            source_text="人工智能代理正在进入企业流程，这是第一条候选选题的对应原文。",
            script=None,
            confidence=0.92,
            evidence=["人工智能代理正在进入企业流程"],
        ),
        StructuredTopicInput(
            source_index="表格 1",
            title="算力降价推动中小企业采用 AI",
            source_text="算力成本下降正在改变中小企业采用 AI 的节奏。",
            confidence=0.65,
            evidence=["算力成本下降"],
        ),
    ]

    imported = service.import_structured_topics(
        actor_name="老板测试",
        original_filename="ChatGPT随手生成的选题.docx",
        idempotency_key="structured-import-0001",
        topics=topics,
        warnings=["第二条需要确认数据时效。"],
        schema_version="1.0",
        content_base64=encoded,
        file_url=None,
    )

    data = imported["data"]
    assert data["created_count"] == 2
    assert data["parse_status"] == "COMPLETED"
    assert data["import_mode"] == "WORKBUDDY_STRUCTURED"
    assert data["schema_version"] == "1.0"
    assert len(data["warnings"]) == 2

    with service.sessions() as session:
        projects = session.scalars(
            select(ContentProject).order_by(ContentProject.source_sequence)
        ).all()
        assert projects[0].source_content["script"] is None
        assert projects[0].source_content["evidence_verified"] is True
        assert projects[1].source_content["confidence"] == 0.65

    duplicate = service.import_structured_topics(
        actor_name="老板测试",
        original_filename="换个名字也还是同一内容.docx",
        idempotency_key="structured-import-0002",
        topics=topics,
        warnings=[],
        schema_version="1.0",
        content_base64=encoded,
        file_url=None,
    )
    assert duplicate["data"]["deduplicated"] is True
    assert duplicate["data"]["created_count"] == 0


def test_structured_import_rejects_hallucinated_source_without_writes(tmp_path) -> None:
    service = make_service(tmp_path)
    encoded = base64.b64encode(_arbitrary_document()).decode()

    with pytest.raises(InvalidArgument, match="无法在 Word 原文中定位"):
        service.import_structured_topics(
            actor_name="老板测试",
            original_filename="任意格式.docx",
            idempotency_key="structured-invalid-0001",
            topics=[
                StructuredTopicInput(
                    title="模型幻觉",
                    source_text="这段内容从未出现在原始 Word 文档中。",
                    confidence=0.99,
                    evidence=[],
                )
            ],
            warnings=[],
            schema_version="1.0",
            content_base64=encoded,
            file_url=None,
        )

    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ImportBatch)) == 0
        assert session.scalar(select(func.count()).select_from(ContentProject)) == 0
