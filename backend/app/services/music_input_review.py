"""Read-only songwriting input checks; no model, storage, or approval effects.

Recognition here is an application convention, not evidence that a model will
obey a section label or deliver every word. Exact names remain distinct.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


SECTION_TAG_RE = re.compile(r"^\[([a-zA-Z0-9 _-]+)\][ \t]*\r?$", re.MULTILINE)


def parse_section_tags(lyrics: str) -> list[tuple[str, int, int]]:
    """Return exact names and original-string spans, including CRLF support."""
    return [(m.group(1), m.start(), m.end()) for m in SECTION_TAG_RE.finditer(lyrics)]


def section_family(name: str, known_sections: Iterable[str]) -> str | None:
    """Classify for diagnostics ONLY; do not merge arrangement keys or tags."""
    known = {item.casefold() for item in known_sections}
    lowered = name.casefold()
    if lowered in known:
        return lowered
    numbered = re.fullmatch(r"(.+?) [1-9][0-9]*", lowered)
    if numbered and numbered.group(1) in known:
        return numbered.group(1)
    if lowered == "final chorus" and "chorus" in known:
        return "chorus"
    return None


def _instrumental_only(name: str, arrangement: Mapping[str, str]) -> bool:
    # Intro/outro may contain vocals. Only exempt those when their exact
    # directive explicitly says instrumental-only, rather than guessing from
    # instrument names ("steel guitar" alone does not establish no vocals).
    if name.casefold() in {"inst", "instrumental"}:
        return True
    directive = arrangement.get(name, "")
    return bool(re.search(
        r"\b(?:instrumental(?:[ -]only)?|no (?:vocals|lyrics)|without vocals)\b",
        directive, re.IGNORECASE,
    )) and not bool(re.search(
        r"\b(?:not instrumental|non[ -]instrumental|vocals? enter|sung)\b",
        directive, re.IGNORECASE,
    ))


def review_music_inputs(
    *, lyrics: str, musical_direction: str, duration_s: float,
    arrangement: Mapping[str, str], known_sections: Iterable[str],
    seconds_per_line: float,
) -> list[dict[str, Any]]:
    """Advisory diagnostics only. Never rewrite inputs or establish readiness."""
    known_sections = tuple(known_sections)
    warnings: list[dict[str, Any]] = []
    tags = parse_section_tags(lyrics)
    tag_names = [name for name, _, _ in tags]
    for name in tag_names:
        if section_family(name, known_sections) is None:
            warnings.append({
                "code": "unknown_section", "section": name,
                "message": f"[{name}] is not a recognized section name",
            })
    for index, (name, _, end) in enumerate(tags):
        block_end = tags[index + 1][1] if index + 1 < len(tags) else len(lyrics)
        if not lyrics[end:block_end].strip() and not _instrumental_only(name, arrangement):
            warnings.append({
                "code": "empty_section", "section": name,
                "message": f"[{name}] has no lyric lines; mark instrumental intent explicitly if intended",
            })
    for section in arrangement:
        if section not in tag_names:
            warnings.append({
                "code": "arrangement_orphan", "section": section,
                "message": f"arrangement directive for [{section}] has no matching section tag",
            })
    for line_number, line in enumerate(lyrics.splitlines(), 1):
        # Bare headers are ambiguous; warn, never silently transform lyrics.
        if line.strip() and section_family(line.strip(), known_sections) is not None:
            warnings.append({
                "code": "unbracketed_section", "section": line.strip(),
                "message": f"Line {line_number}: {line.strip()!r} may be a section header; use [brackets] if so",
            })
    content_lines = [
        line for line in lyrics.splitlines()
        if line.strip() and not SECTION_TAG_RE.fullmatch(line)
    ]
    estimated = len(content_lines) * seconds_per_line
    if estimated > duration_s:
        warnings.append({
            "code": "lyric_density_high",
            "message": (
                f"{len(content_lines)} lyric lines may not fit {duration_s:g}s "
                f"at about {seconds_per_line:g}s per line; this heuristic does not "
                "measure syllables, vocal phrasing, or instrumental space"
            ),
        })
    elif estimated * 2 < duration_s:
        warnings.append({
            "code": "lyric_density_low",
            "message": (
                f"{len(content_lines)} lyric lines may leave {duration_s:g}s "
                f"mostly instrumental at about {seconds_per_line:g}s per line"
            ),
        })
    # Deliberately narrow: explicit seconds/minutes duration statements, not
    # arbitrary numbers such as BPM. This detects the observed stale 180s
    # instruction without pretending to fully parse natural-language timing.
    for match in re.finditer(
        r"\bduration\s*(?:target\s*)?(?:[:=]|is|of)?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*(seconds?|secs?|s|minutes?|mins?)\b",
        musical_direction, re.IGNORECASE,
    ):
        value = float(match.group(1))
        if match.group(2).casefold().startswith("m"):
            value *= 60
        if abs(value - duration_s) > 0.001:
            warnings.append({
                "code": "duration_conflict",
                "message": (
                    f"Direction says {value:g}s but the duration target is {duration_s:g}s; "
                    "reconcile the draft before approval (neither is a guarantee)"
                ),
            })
    return warnings
