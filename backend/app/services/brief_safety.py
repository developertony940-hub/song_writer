"""Safety helpers for operator-supplied briefs.

recommendations_validation.html F8 and R9: the operator's brief is usually
pasted from the web, a video description or a chat log, so it is untrusted
text. It may suggest a tempo or a key; it may not carry instructions, code
fences or control bytes that could reshape the prompt, and it may never
influence budgets, paths, model choice or authority fields.

This module is deliberately free of agent imports so both the services and
the agent pipeline can use it without a dependency cycle.
"""
from __future__ import annotations

import re

from app.services.operator_lyrics import extract_operator_lyrics

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FENCE = re.compile(r"```[a-zA-Z]*")

# The brief may contribute values as data. Nothing else is read from it.
ALLOWED_BRIEF_FIELDS = ("bpm", "key_scale", "time_signature", "duration_s")


def sanitize_brief(source: str) -> str:
    """Strip control characters and code fences from untrusted operator text."""
    text = _CONTROL_CHARS.sub(" ", source or "")
    text = _FENCE.sub("", text)
    text = text.replace("\u2028", " ").replace("\u2029", " ")
    return text.strip()


def brief_is_complete(source: str) -> bool:
    """True when the brief already carries lyrics and a chord map.

    This is the gate for the fast path: if the operator supplied both, the
    pipeline does not need to write lyrics or plan harmony, and decomposing
    would only add latency.
    """
    lyrics = extract_operator_lyrics(source)
    chords = bool(
        re.search(r"\b(?:progression|chords?)\b", source or "", re.IGNORECASE)
        or re.search(r"[A-G][#b]?\s*(?:->|→)\s*[A-G][#b]?", source or ""))
    return lyrics.found and chords
