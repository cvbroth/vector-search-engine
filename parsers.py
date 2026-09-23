"""Read supported documents from an in-memory snapshot; never alter sources."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


class ParseError(Exception):
    """A document could not be converted into usable text."""


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    text: str
    page: int | None
    is_heading: bool = False


def _plain_blocks(text: str, *, page: int | None = None) -> list[ParsedBlock]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [
        ParsedBlock(part.strip(), page)
        for part in re.split(r"\n\s*\n", normalized)
        if part.strip()
    ]


def _markdown_blocks(text: str) -> list[ParsedBlock]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[ParsedBlock] = []
    paragraph: list[str] = []
    fenced = False

    def flush() -> None:
        if paragraph:
            blocks.append(ParsedBlock("\n".join(paragraph).strip(), None))
            paragraph.clear()

    for index, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^(`{3,}|~{3,})", stripped):
            fenced = not fenced
            paragraph.append(line)
            continue
        if not fenced:
            heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", stripped)
            if heading:
                flush()
                blocks.append(ParsedBlock(heading.group(1), None, True))
                continue
            if index + 1 < len(lines) and stripped:
                underline = lines[index + 1].strip()
                if re.fullmatch(r"=+|-+", underline):
                    # The next line is consumed by the check below.
                    flush()
                    blocks.append(ParsedBlock(stripped, None, True))
                    continue
            if index > 0 and re.fullmatch(r"=+|-+", stripped):
                continue
            if not stripped:
                flush()
                continue
        paragraph.append(line)
    flush()
    return blocks


def _pdf_blocks(data: bytes) -> list[ParsedBlock]:
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        raise ParseError("encrypted PDF is unsupported")
    blocks: list[ParsedBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        blocks.extend(_plain_blocks(page.extract_text() or "", page=page_number))
    return blocks


def _docx_blocks(data: bytes) -> list[ParsedBlock]:
    document: DocxDocument = Document(io.BytesIO(data))
    blocks: list[ParsedBlock] = []
    for element in document.element.body.iterchildren():
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if text:
                style = (paragraph.style.name or "").lower()
                blocks.append(
                    ParsedBlock(
                        text,
                        None,
                        style.startswith(("heading", "title", "标题")),
                    )
                )
        elif element.tag == qn("w:tbl"):
            table = Table(element, document)
            for row in table.rows:
                text = " | ".join(cell.text.strip() for cell in row.cells).strip(" |")
                if text:
                    blocks.append(ParsedBlock(text, None))
    return blocks


def parse_document(path: Path, data: bytes) -> list[ParsedBlock]:
    """Parse one already-read snapshot; PDF pages use one-based page numbers."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".md":
            blocks = _markdown_blocks(data.decode("utf-8-sig"))
        elif suffix == ".txt":
            blocks = _plain_blocks(data.decode("utf-8-sig"))
        elif suffix == ".pdf":
            blocks = _pdf_blocks(data)
        elif suffix == ".docx":
            blocks = _docx_blocks(data)
        else:
            raise ParseError(f"unsupported file type: {suffix}")
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"cannot parse {path}: {exc}") from exc
    if not blocks:
        raise ParseError(f"no extractable text in {path}; scanned PDFs need OCR")
    return blocks
