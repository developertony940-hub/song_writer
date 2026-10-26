"""Read-only songwriting input checks; no model, storage, or approval effects.

Recognition here is an application convention, not evidence that a model will
obey a section label or deliver every word. Exact names remain distinct.
"""
from __future__ import annotations

import re
import math
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
    seconds_per_line: float = 6.0, bpm: float | None = None,
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
    estimate = estimate_lyric_duration(
        lyrics=lyrics, arrangement=arrangement, bpm=bpm,
        musical_direction=musical_direction,
    )
    if estimate["total_seconds_range"][0] > duration_s:
        warnings.append({
            "code": "lyric_density_high",
            "message": (
                f"{estimate['lyric_lines']} lyric lines ({estimate['words']} words; "
                f"about {estimate['syllables_estimate']} syllables) may not fit {duration_s:g}s; "
                f"estimated range {estimate['total_seconds_range']}s including instrumental allowance. "
                "Tempo/phrasing estimates are uncertain, not an input rewrite or quality verdict."
            ),
            "estimate": estimate,
        })
    elif estimate["tempo_source"] != "unspecified_range" and estimate["total_seconds_range"][1] * 2 < duration_s:
        warnings.append({
            "code": "lyric_density_low",
            "message": (
                f"{estimate['words']} words may leave substantial space within {duration_s:g}s; "
                f"estimated range {estimate['total_seconds_range']}s. Sustained notes, repeats "
                "and instrumental passages can fill it; this is only an advisory."
            ),
            "estimate": estimate,
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


def estimate_lyric_duration(
    *, lyrics: str, arrangement: Mapping[str, str], bpm: float | None = None,
    musical_direction: str = "",
) -> dict[str, Any]:
    """Transparent English-oriented heuristic, never a vocal-coverage guarantee.

    Count vowel groups as approximate syllables; allow 0.5–2 syllables/beat
    and 1–4 words/second. Explicit instrumental sections receive 8–16 beats
    each. Unknown tempo spans 60–140 BPM. No inference changes stored text.
    """
    source = "settings" if bpm is not None else "unspecified_range"
    if bpm is None:
        match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*BPM\b", musical_direction, re.I)
        if match:
            bpm, source = float(match.group(1)), "direction"
    if bpm is not None and (not math.isfinite(bpm) or bpm <= 0):
        bpm, source = None, "unspecified_range"
    tempos = [float(bpm), float(bpm)] if bpm is not None else [60.0, 140.0]
    lines = [line for line in lyrics.splitlines() if line.strip() and not SECTION_TAG_RE.fullmatch(line)]
    words = re.findall(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", "\n".join(lines), re.UNICODE)
    syllables = 0
    for word in words:
        lowered = word.casefold()
        groups = len(re.findall(r"[aeiouy]+", lowered))
        if lowered.endswith("e") and not lowered.endswith(("le", "ye")) and groups > 1:
            groups -= 1
        syllables += max(1, groups)
    instrumental = sum(_instrumental_only(name, arrangement) for name, _, _ in parse_section_tags(lyrics))
    vocal = [max(len(words) / 4, syllables * 60 / (tempos[1] * 2)),
             max(len(words), syllables * 60 / (tempos[0] * .5))]
    allowance = [instrumental * 8 * 60 / tempos[1], instrumental * 16 * 60 / tempos[0]]
    return {
        "method": "word_syllable_tempo_range_v1", "lyric_lines": len(lines),
        "words": len(words), "syllables_estimate": syllables,
        "tempo_bpm_range": tempos, "tempo_source": source,
        "syllables_per_beat_range": [.5, 2.0], "words_per_second_range": [1.0, 4.0],
        "instrumental_sections": instrumental, "instrumental_beats_per_section_range": [8, 16],
        "vocal_seconds_range": [round(n, 2) for n in vocal],
        "instrumental_seconds_range": [round(n, 2) for n in allowance],
        "total_seconds_range": [round(vocal[i] + allowance[i], 2) for i in (0, 1)],
        "uncertainty": "English-oriented vowel-group estimate; language, melisma, rests, repeats, phrasing and model omissions are not predicted. Instrumental allowance is a planning assumption, not parsed timing.",
        "legacy_seconds_per_line_used": False,
    }
