from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

_TOPIC_HEADING = re.compile(r"^(?P<sequence>\d{2})[｜|]\s*(?P<title>.+)$")
_SCRIPT_SECTION = "提词器口播正文"
_TITLE_FIELD = "主爆款标题"


class TopicParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedTopic:
    source_sequence: int
    heading_title: str
    title: str
    script: str
    source_content: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TopicFailure:
    source_sequence: int | None
    heading_title: str
    reason: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    topics: list[ParsedTopic] = field(default_factory=list)
    failures: list[TopicFailure] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Line:
    """文档顺序中的一个内容行。

    style 为 "H1"/"H2"/"P" 之一；表格行被展开为普通内容行。
    """

    style: str
    text: str


def _heading_level(style_name: str | None, style_id: str | None) -> int | None:
    name = (style_name or "").strip().lower()
    sid = (style_id or "").strip().lower().replace(" ", "")
    for level in (1, 2):
        if name in {f"heading {level}", f"标题 {level}"} or sid == f"heading{level}":
            return level
    return None


def _iter_lines(document: Any) -> list[_Line]:
    """按文档相对顺序展开段落和表格（规格 §4.2 规则 4）。"""
    lines: list[_Line] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            level = _heading_level(
                paragraph.style.name if paragraph.style else None,
                paragraph.style.style_id if paragraph.style else None,
            )
            lines.append(_Line(style=f"H{level}" if level else "P", text=text))
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if not cells:
                    continue
                if len(cells) >= 2:
                    lines.append(_Line(style="P", text=f"{cells[0]}：{' '.join(cells[1:])}"))
                else:
                    lines.append(_Line(style="P", text=cells[0]))
    return lines


def _split_blocks(lines: list[_Line]) -> list[tuple[int, str, list[_Line]]]:
    """以带序号一级标题为起点切块；无序号一级标题结束当前块。"""
    blocks: list[tuple[int, str, list[_Line]]] = []
    current: tuple[int, str, list[_Line]] | None = None
    for line in lines:
        match = _TOPIC_HEADING.fullmatch(line.text) if line.style == "H1" else None
        if match:
            if current is not None:
                blocks.append(current)
            current = (int(match.group("sequence")), match.group("title").strip(), [])
        elif line.style == "H1":
            if current is not None:
                blocks.append(current)
                current = None
        elif current is not None:
            current[2].append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def _parse_block(sequence: int, heading_title: str, lines: list[_Line]) -> ParsedTopic:
    fields: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    for line in lines:
        if line.style == "H2":
            current_section = line.text
            sections.setdefault(current_section, [])
            continue
        if current_section is not None:
            sections[current_section].append(line.text)
        key, separator, value = line.text.partition("：")
        if separator and key.strip() and value.strip():
            fields[key.strip()] = value.strip()

    title = fields.get(_TITLE_FIELD, heading_title).strip()
    script_lines = next(
        (value for key, value in sections.items() if key.startswith(_SCRIPT_SECTION)),
        [],
    )
    script = "\n".join(script_lines).strip()
    if not title:
        raise TopicParseError("缺少主爆款标题和一级标题文本。")
    if not script:
        raise TopicParseError("缺少提词器口播正文。")
    return ParsedTopic(
        source_sequence=sequence,
        heading_title=heading_title,
        title=title,
        script=script,
        source_content={
            "fields": fields,
            "sections": sections,
            "script": script,
            "paragraphs": [line.text for line in lines],
        },
    )


def parse_topic_document(content: bytes) -> ParseResult:
    try:
        document = Document(BytesIO(content))
    except Exception as exc:
        raise TopicParseError("Word 文件无法解析或不是有效的 .docx。") from exc

    lines = _iter_lines(document)
    blocks = _split_blocks(lines)
    result = ParseResult()
    for sequence, heading_title, block_lines in blocks:
        try:
            result.topics.append(_parse_block(sequence, heading_title, block_lines))
        except TopicParseError as exc:
            result.failures.append(
                TopicFailure(
                    source_sequence=sequence,
                    heading_title=heading_title,
                    reason=str(exc),
                )
            )
    if not blocks:
        result.failures.append(
            TopicFailure(
                source_sequence=None,
                heading_title="",
                reason="未识别到带序号一级标题的选题区块。",
            )
        )
    return result
