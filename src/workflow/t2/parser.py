from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from docx import Document

_TOPIC_HEADING = re.compile(r"^(?P<sequence>\d{2})[｜|]\s*(?P<title>.+)$")


class TopicParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedTopic:
    source_sequence: int
    heading_title: str
    title: str
    source_content: dict[str, object]


def parse_topic_document(content: bytes) -> list[ParsedTopic]:
    try:
        document = Document(BytesIO(content))
    except Exception as exc:
        raise TopicParseError("Word 文件无法解析或不是有效的 .docx。") from exc

    blocks: list[tuple[int, str, list[str]]] = []
    current: tuple[int, str, list[str]] | None = None
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        match = _TOPIC_HEADING.fullmatch(text)
        is_heading_one = paragraph.style.name.lower().startswith("heading 1")
        if is_heading_one and match:
            if current is not None:
                blocks.append(current)
            current = (int(match.group("sequence")), match.group("title").strip(), [])
        elif is_heading_one and current is not None:
            blocks.append(current)
            current = None
        elif current is not None:
            current[2].append(text)
    if current is not None:
        blocks.append(current)

    topics: list[ParsedTopic] = []
    for sequence, heading_title, paragraphs in blocks:
        fields: dict[str, str] = {}
        for text in paragraphs:
            key, separator, value = text.partition("：")
            if separator and key.strip() and value.strip():
                fields[key.strip()] = value.strip()
        title = fields.get("主爆款标题", heading_title).strip()
        script = next(
            (value for key, value in fields.items() if key.startswith("提词器口播正文")),
            "",
        )
        if not title or not script:
            continue
        topics.append(
            ParsedTopic(
                source_sequence=sequence,
                heading_title=heading_title,
                title=title,
                source_content={
                    "fields": fields,
                    "paragraphs": paragraphs,
                },
            )
        )
    if not topics:
        raise TopicParseError("未识别到包含标题和提词器口播正文的有效选题。")
    return topics
