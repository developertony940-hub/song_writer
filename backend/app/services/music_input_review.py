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


def canonical_section_name(name: str, known_sections: Iterable[str]) -> str | None:
    """Map a heading onto the app's section vocabulary, or give up.

    A band chart heading carries an annotation - ``[Verse 1 - Johnny's
    Party]``, ``[CHORUS - 8 BARS]`` - and the annotation is direction, not
    part of the section's name. The returned name is the house spelling.
    """
    known = tuple(known_sections)
    head = (name or "").strip().strip("[]").strip()
    if not head:
        return None
    if section_family(head, known) is not None:
        return _title_section(head)
    parts = re.split(r"\s*[-–—:]\s+", head, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and section_family(parts[0], known) is not None:
        return _title_section(parts[0].strip())
    for word in re.split(r"[\s/]+", head):
        word = word.strip(".,")
        if word and section_family(word, known) is not None:
            return _title_section(word)
    return None


def _title_section(name: str) -> str:
    """The house spelling of a section: Verse 1, Pre-Chorus, Final Chorus."""
    parts: list[str] = []
    for part in name.split("-"):
        if not part.strip():
            continue
        words: list[str] = []
        for index, word in enumerate(part.split(" ")):
            if not word:
                continue
            if index == 0 or word[0].isdigit():
                words.append(word[:1].upper() + word[1:].lower())
            else:
                words.append(word.lower())
        parts.append(" ".join(words))
    return "-".join(parts)


def _instrumental_only(name: str, arrangement: Mapping[str, str]) -> bool:
    # Intro/outro may contain vocals. Only exempt those when their exact
    # directive explicitly says instrumental-only, rather than guessing from
    # instrument names ("steel guitar" alone does not establish no vocals).
    if name.casefold() in {"inst", "instrumental"}:
        return True
    directive = arrangement.get(name, "").strip()
    if not directive:
        return False
    # "Instrumental punctuation" or "instrumental fills" describe what happens
    # inside a sung section; they do not make the section wordless. Only a
    # directive that calls the passage itself instrumental counts.
    says_instrumental = bool(
        re.match(r"^\s*instrumental\b", directive, re.IGNORECASE)
        or re.search(
            r"\binstrumental[ -](?:only|breaks?|sections?|solos?|passages?|"
            r"breakdowns?|figure|figures?)\b", directive, re.IGNORECASE)
        or re.search(r"\bno (?:vocals|lyrics)\b|\bwithout vocals\b",
                     directive, re.IGNORECASE))
    if not says_instrumental:
        return False
    return not bool(re.search(
        r"\b(?:not instrumental|non[ -]instrumental|vocals? enter|sung)\b",
        directive, re.IGNORECASE,
    ))


def _structure_warnings(
    lyrics: str, tags: list[tuple[str, int, int]], tag_names: list[str],
    arrangement: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Deterministic structure checks that no model call is needed for.

    These cover the defect classes observed in real draft runs: a shared
    section name whose directive reads like a single occurrence, a section
    described as instrumental while it carries words, and a structure with
    tags but nothing to sing.
    """
    warnings: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for name in tag_names:
        counts[name] = counts.get(name, 0) + 1
    for name, count in counts.items():
        if count > 1:
            directive = arrangement.get(name, "")
            if _OCCURRENCE_WORDS.search(directive):
                warnings.append({
                    "code": "shared_occurrence_wording", "section": name,
                    "message": (
                        f"[{name}] appears {count} times but its single directive reads "
                        "like a single occurrence; give the occurrences distinct names "
                        "if they need different directions"),
                })
    for index, (name, _, end) in enumerate(tags):
        block_end = tags[index + 1][1] if index + 1 < len(tags) else len(lyrics)
        body = lyrics[end:block_end].strip()
        if not body:
            continue
        directive = arrangement.get(name, "")
        if _INSTRUMENTAL_CLAIM.search(directive) and not _INSTRUMENTAL_ONLY.search(directive):
            warnings.append({
                "code": "instrumental_lyric_conflict", "section": name,
                "message": (
                    f"[{name}] is described as instrumental but carries lyric lines; "
                    "a section with words cannot also be an instrumental passage"),
            })
    if tag_names and not _any_sung_line(lyrics, tags):
        warnings.append({
            "code": "no_sung_lines",
            "message": (
                "the lyrics contain section tags but no singable lines; every "
                "non-instrumental section needs lyric text"),
        })
    return warnings


_OCCURRENCE_WORDS = re.compile(
    r"\b(?:final|last|first|second|third|opening|closing)\s+"
    r"(?:chorus|verses?|intros?|outros?|bridges?|solos?|pre-?chorus|breaks?|"
    r"instruments?|parts?|hooks?)\b"
    r"|\b(?:the\s+)?(?:first|last)\s+time\b"
    r"|\brepeat\s+(?:the\s+|this\s+|that\s+)?"
    r"(?:chorus|verses?|intros?|outros?|bridges?|hooks?|parts?)\b",
    re.IGNORECASE)
_INSTRUMENTAL_CLAIM = re.compile(
    r"\b(?:instrumental(?:[ -]only)?|solo|no vocals|without vocals)\b", re.IGNORECASE)
_INSTRUMENTAL_ONLY = re.compile(
    r"\b(?:not instrumental|non[ -]instrumental|vocals? enter|sung)\b", re.IGNORECASE)


def _any_sung_line(lyrics: str, tags: list[tuple[str, int, int]]) -> bool:
    """True when at least one non-instrumental section carries a sung line."""
    for index, (_, _, end) in enumerate(tags):
        block_end = tags[index + 1][1] if index + 1 < len(tags) else len(lyrics)
        for line in lyrics[end:block_end].splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("("):
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                continue
            return True
    return False


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
    warnings.extend(_structure_warnings(lyrics, tags, tag_names, arrangement))
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
