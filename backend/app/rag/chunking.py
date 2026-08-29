"""Structure-aware chunking with overlap and heading propagation."""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class TextChunk:
    index: int
    content: str
    heading: str | None
    token_count: int
    start_char: int
    end_char: int


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _split_sections(text: str) -> list[tuple[str | None, str, int]]:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [(None, text, 0)]
    sections: list[tuple[str | None, str, int]] = []
    if matches[0].start() > 0:
        sections.append((None, text[: matches[0].start()], 0))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((match.group(2).strip(), text[match.end() : end], match.end()))
    return sections


def chunk_text(
    text: str, *, max_tokens: int = 512, overlap_tokens: int = 64, min_tokens: int = 24
) -> list[TextChunk]:
    """Split text into overlapping chunks that respect headings and sentence bounds."""
    text = (text or "").strip()
    if not text:
        return []

    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4
    chunks: list[TextChunk] = []
    index = 0

    for heading, body, offset in _split_sections(text):
        body = body.strip()
        if not body:
            continue
        if len(body) <= max_chars:
            chunks.append(TextChunk(index, body, heading, estimate_tokens(body), offset, offset + len(body)))
            index += 1
            continue

        sentences = SENTENCE_RE.split(body)
        buffer = ""
        buffer_start = offset
        for sentence in sentences:
            candidate = f"{buffer} {sentence}".strip() if buffer else sentence
            if len(candidate) > max_chars and buffer:
                chunks.append(
                    TextChunk(
                        index,
                        buffer,
                        heading,
                        estimate_tokens(buffer),
                        buffer_start,
                        buffer_start + len(buffer),
                    )
                )
                index += 1
                tail = buffer[-overlap_chars:] if overlap_chars else ""
                buffer_start = buffer_start + len(buffer) - len(tail)
                buffer = f"{tail} {sentence}".strip()
            else:
                buffer = candidate
        if buffer and estimate_tokens(buffer) >= min_tokens:
            chunks.append(
                TextChunk(
                    index, buffer, heading, estimate_tokens(buffer), buffer_start, buffer_start + len(buffer)
                )
            )
            index += 1
        elif buffer and chunks:
            last = chunks[-1]
            merged = f"{last.content}\n{buffer}"
            chunks[-1] = TextChunk(
                last.index,
                merged,
                last.heading,
                estimate_tokens(merged),
                last.start_char,
                last.start_char + len(merged),
            )
    return chunks
