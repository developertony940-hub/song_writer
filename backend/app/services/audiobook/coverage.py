"""Deterministic coverage validation and chunk planning for the audiobook slice.

All offsets are Unicode code-point indices into the stored (LF-normalized)
manuscript. Coverage is the union of speaker spans and explicit exclusions,
which must tile ``[0, char_count)`` exactly once, in order.
"""
from __future__ import annotations

import hashlib

from app.models.audiobook import AnnotationSet, Chunk

DEFAULT_CHUNK_MAX_CHARS = 280
DEFAULT_PAUSE_MS = 300


def coverage_errors(text_len: int, annotations: AnnotationSet) -> list[str]:
    """Return a list of human-readable coverage violations (empty means valid)."""
    errors: list[str] = []
    spans, exclusions = annotations.spans, annotations.exclusions

    for previous, current in zip(spans, spans[1:]):
        if current.start_char <= previous.start_char:
            errors.append(
                f"speaker spans must be stored in strictly increasing source order: "
                f"{previous.id} then {current.id}"
            )
    for previous, current in zip(exclusions, exclusions[1:]):
        if current.start_char <= previous.start_char:
            errors.append("exclusions must be stored in strictly increasing source order")

    for span in spans:
        if span.end_char <= span.start_char or span.start_char < 0 or span.end_char > text_len:
            errors.append(f"span {span.id} is empty or outside the manuscript")
    for exclusion in exclusions:
        if exclusion.end_char <= exclusion.start_char or exclusion.start_char < 0 or exclusion.end_char > text_len:
            errors.append(f"exclusion at {exclusion.start_char} is empty or outside the manuscript")

    intervals: list[tuple[int, int, str]] = (
        [(s.start_char, s.end_char, f"span {s.id}") for s in spans]
        + [(e.start_char, e.end_char, f"exclusion ({e.reason})") for e in exclusions]
    )
    intervals.sort(key=lambda item: (item[0], item[1]))
    cursor = 0
    for start, end, label in intervals:
        if end <= start:
            continue
        if start > cursor:
            errors.append(f"gap in coverage at [{cursor}, {start}) before {label}")
        elif start < cursor:
            errors.append(f"overlap in coverage at [{start}, {min(end, cursor)}) at {label}")
        cursor = max(cursor, end)
    if cursor != text_len:
        errors.append(f"coverage ends at {cursor} but the manuscript is {text_len} characters long")
    return errors


def unresolved_span_ids(annotations: AnnotationSet) -> list[str]:
    return [span.id for span in annotations.spans if span.needs_review]


def chunk_id(span_id: str, start_char: int, end_char: int, speaker: str, text: str) -> str:
    raw = f"{span_id}|{start_char}|{end_char}|{speaker}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _split_exact(segment: str, max_chars: int) -> list[tuple[int, int, str]]:
    """Split ``segment`` into pieces no longer than ``max_chars``.

    Boundaries prefer the last whitespace inside the window; the concatenation
    of the returned pieces is exactly the input, so no character is dropped,
    duplicated, or reordered.
    """
    pieces: list[tuple[int, int, str]] = []
    index, length = 0, len(segment)
    while index < length:
        end = min(index + max_chars, length)
        if end < length:
            cut = segment.rfind(" ", index, end)
            if cut > index:
                end = cut + 1
        pieces.append((index, end, segment[index:end]))
        index = end
    return pieces


def plan_chunks(text: str, annotations: AnnotationSet, *,
                max_chars: int = DEFAULT_CHUNK_MAX_CHARS) -> list[Chunk]:
    """Deterministically derive ordered chunks from the annotated spans."""
    chunks: list[Chunk] = []
    for span in sorted(annotations.spans, key=lambda s: s.start_char):
        segment = text[span.start_char:span.end_char]
        for offset_start, offset_end, piece in _split_exact(segment, max_chars):
            start_char = span.start_char + offset_start
            end_char = span.start_char + offset_end
            chunks.append(Chunk(
                id=chunk_id(span.id, start_char, end_char, span.speaker, piece),
                span_id=span.id, speaker=span.speaker, text=piece,
                start_char=start_char, end_char=end_char,
            ))
    return chunks
