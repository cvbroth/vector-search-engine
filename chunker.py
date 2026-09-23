"""Split by sections, paragraphs and sentences, with bounded overlap."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS
from parsers import ParsedBlock


@dataclass(frozen=True, slots=True)
class Chunk:
    document_id: str
    source_path: str
    filename: str
    page: int | None
    chunk_index: int
    text: str


def _split_long(text: str, limit: int) -> list[str]:
    """Prefer sentence, then word boundaries; hard-split only an unbroken unit."""
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[。！？.!?；;])\s*", text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(sentence) > limit:
            if current:
                pieces.append(current.strip())
                current = ""
            words = re.split(r"(\s+)", sentence)
            for word in words:
                if len(current) + len(word) > limit and current.strip():
                    pieces.append(current.strip())
                    current = ""
                while len(word) > limit:
                    pieces.append(word[:limit])
                    word = word[limit:]
                current += word
        elif len(current) + len(sentence) > limit and current.strip():
            pieces.append(current.strip())
            current = sentence
        else:
            current += sentence
    if current.strip():
        pieces.append(current.strip())
    return pieces


def chunk_blocks(
    blocks: list[ParsedBlock],
    document_id: str,
    path: Path,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    if max_chars < 100 or not 0 <= overlap_chars < max_chars // 2:
        raise ValueError("invalid chunk size or overlap")

    chunks: list[Chunk] = []
    parts: list[str] = []
    page: int | None = None
    heading = ""
    previous_tail = ""

    def flush(*, allow_overlap: bool = True) -> None:
        nonlocal parts, previous_tail
        content = "\n\n".join(parts).strip()
        if content:
            chunks.append(
                Chunk(document_id, str(path), path.name, page, len(chunks), content)
            )
        previous_tail = (
            content[-overlap_chars:]
            if allow_overlap and content and overlap_chars > 0
            else ""
        )
        parts = []

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if parts and block.page != page:
            flush(allow_overlap=False)
            heading = ""
        page = block.page
        if block.is_heading:
            flush(allow_overlap=False)
            heading = text[: max_chars // 4]
            continue

        available = max_chars - len(heading) - 4
        for piece in _split_long(text, available):
            if parts and len("\n\n".join(parts)) + len(piece) + 2 > max_chars:
                flush()
            if not parts:
                if heading:
                    parts.append(heading)
                remaining = max_chars - len("\n\n".join(parts)) - len(piece) - 4
                if previous_tail and remaining > 0:
                    parts.append(previous_tail[-min(overlap_chars, remaining) :])
                previous_tail = ""
            parts.append(piece)
    if not parts and heading:
        chunks.append(Chunk(document_id, str(path), path.name, page, len(chunks), heading))
    else:
        flush(allow_overlap=False)
    return chunks
