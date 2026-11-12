"""Deterministically recover the operator's own lyrics from a pasted brief.

recommendations_validation.html V1: the draft path asks a 70B model to copy
operator lyrics verbatim, and that promise has never been tested against a
real model. Asking a model to be careful is not a guarantee, so this module
recovers the lyrics with rules instead, and the draft path injects them.

The extractor is deliberately conservative. It only claims text that sits
inside a recognised section and is shaped like a sung line. When the
operator's paste ran together with no line breaks, it reports that it could
not find a clean structure rather than guessing, because a wrong split
corrupts a song.

A pasted brief is often a conversation, not a brief: it can carry several
drafts of the same song, band notes between them, and section headings that
carry an annotation. The extractor therefore keeps several rules that matter
for that material:

* a section heading may carry an annotation - ``[Verse 1 - Johnny's Party]``
  is the verse, and the annotation is direction, not a sung line;
* a markdown bullet is a list item, never a sung line;
* an instrumental section carries direction, not words;
* a chord symbol in front of a line is a chord, and the words behind it are
  still the operator's words;
* when the same song appears more than once in the paste, the extractor
  reports the single most complete copy rather than merging every copy into
  one three-times-too-long song.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.music_input_review import canonical_section_name, section_family

# A label line such as "Bassline:", "Musical Vibe:" or "Chord Progression:".
_LABEL_LINE = re.compile(r"^[A-Z][A-Za-z /&'’\-]{0,40}:\s")
# A markdown bullet such as "- [Verse 1] ..." or "- Drums: ...".
_BULLET = re.compile(r"^\s*[-*+]\s+")
_BRACKETED = re.compile(r"^\[(.*)\]\s*(.*)$")
_SENTENCE_END = (",", ".", "!", "?", ";", "—", "–")
# A chord symbol in brackets, as used in a chord chart: [G], [D7], [Am], [G/B].
_CHORD_SYMBOL = re.compile(
    r"^[A-G](?:#|b)?(?:m|maj|min|dim|aug|sus|add)?\d*(?:/[A-G](?:#|b)?)?$")
# The annotation that follows a section heading: "Verse 1 - Johnny's Party".
_ANNOTATION = re.compile(r"\s*[-–—:]\s+")
# A numbered heading in the surrounding document: "4. Arrangement by instrument".
_NUMBERED_HEADING = re.compile(r"^\d{1,2}\.\s+\S")
# Sections whose text is direction rather than something to sing.
_INSTRUMENTAL_SECTIONS = {"instrumental", "inst", "interlude"}

# A line that starts with an instrument is a playing instruction.
_INSTRUMENT_STARTS = {
    "fiddle", "banjo", "guitar", "bass", "drums", "piano", "snare", "kick",
    "harmonica", "accordion", "mandolin", "organ", "strings", "horns", "brass",
    "percussion", "cymbals", "violin", "cello", "clarinet", "trumpet",
}
# A line that starts with an instruction verb is a playing instruction.
_INSTRUCTION_STARTS = {
    "use", "add", "drop", "pull", "push", "keep", "layer", "feature", "trade",
    "establish", "hit", "strip", "track", "record", "mix", "pan", "automate",
    "carve", "punch", "lock", "print", "sit", "cut", "drives", "building",
    "capturing", "balancing", "adding", "before", "return", "start", "end",
}
# Words that mark a line as studio language rather than something to sing.
_STUDIO_WORDS = {"bars", "chord", "chords", "bpm", "lyrics", "instrument",
                 "instrumental", "mix", "master", "reverb", "delay", "eq"}

# Prose tells: these lines are descriptions, not sung text.
_PROSE_TELLS = (
    "reminiscent", "layered", "utilizing", "transitions", "provides", "featuring",
    "throughout", "creating", "designed", "including", "paired with", "suggested",
    "transitioning", "similarly", "classic", "modern", "traditional", "produces",
    "matches", "captures", "evokes", "anchors", "reinforces",
)

MAX_LYRIC_WORDS = 14
MAX_LYRIC_CHARS = 100
MAX_SKIPPED_BLOCKS = 40


@dataclass
class OperatorLyrics:
    """What the extractor could prove about the operator's lyrics."""

    lyrics: str = ""
    tags: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    section_order: list[tuple[str, list[str]]] = field(default_factory=list)
    found: bool = False
    confidence: str = "none"          # none | partial | clean
    skipped_blocks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.lines)


def _known_sections() -> tuple[str, ...]:
    try:
        from app.config import get_settings

        return tuple(get_settings().music.known_sections)
    except Exception:  # noqa: BLE001 - a missing config must not break extraction
        return ("intro", "verse", "pre-chorus", "chorus", "post-chorus", "bridge",
                "outro", "inst", "instrumental", "hook", "breakdown", "interlude")


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


def _canonical_section(name: str, known: tuple[str, ...]) -> str | None:
    """Map a heading onto the app's section vocabulary, or give up.

    ``[Verse 1 - Johnny's Party]`` is the verse; the annotation is direction.
    ``[INSTRUMENTAL BREAK 1 - 8 BARS]`` is an instrumental break.
    """
    return canonical_section_name(name, known)


def _is_label(line: str) -> bool:
    return bool(_LABEL_LINE.match(line.strip()))


def _is_prose(line: str) -> bool:
    lowered = line.casefold()
    return any(tell in lowered for tell in _PROSE_TELLS)


def _looks_like_lyric(line: str) -> bool:
    """True when a line reads like something to sing rather than a note to read.

    A real lyric sheet rarely punctuates its lines, so punctuation is not
    required. What separates a sung line from a playing instruction is the
    vocabulary: instructions name instruments, start with an instruction
    verb, or use studio words.
    """
    stripped = line.strip()
    if len(stripped) < 4 or len(stripped) > MAX_LYRIC_CHARS:
        return False
    if stripped.startswith(("(", "-", "*", ">", "#", "_")):
        return False
    if _is_label(stripped):
        return False
    words = stripped.split()
    if len(words) > MAX_LYRIC_WORDS:
        return False
    if _is_prose(stripped):
        return False
    if not re.search(r"[A-Za-z]", stripped):
        return False
    if "**" in stripped or "__" in stripped:
        return False
    first = words[0].strip(".,!?:;\"'").casefold()
    if first in _INSTRUMENT_STARTS or first in _INSTRUCTION_STARTS:
        return False
    lowered = {word.strip(".,!?:;\"'()[]").casefold() for word in words}
    if lowered & _STUDIO_WORDS:
        return False
    # A short all-caps line is a heading; a long one is a shout.
    if stripped.isupper() and len(words) <= 3:
        return False
    # A short line with no punctuation is a heading in the surrounding
    # document ("What I've improved"), not something to sing.
    if len(words) <= 4 and not stripped.endswith(_SENTENCE_END):
        return False
    return True


def _is_instrumental(section: str) -> bool:
    return section.casefold() in _INSTRUMENTAL_SECTIONS


class _Document:
    """One copy of one song found inside the paste."""

    def __init__(self) -> None:
        self.occurrences: list[list] = []   # ordered [tag, lines] per occurrence
        self.tags: list[str] = []
        self.lines_seen = 0
        self._open: list | None = None

    def open_section(self, tag: str) -> None:
        """Start a section. A tag that appears again is the song repeating."""
        self._open = [tag, []]
        self.occurrences.append(self._open)
        if tag not in self.tags:
            self.tags.append(tag)

    def add_line(self, line: str) -> None:
        if self._open is not None:
            self._open[1].append(line)

    @property
    def lines(self) -> list[str]:
        return [line for _, lines in self.occurrences for line in lines]

    @property
    def sections_with_lines(self) -> int:
        return len({tag for tag, lines in self.occurrences if lines})


def extract_operator_lyrics(source: str) -> OperatorLyrics:
    """Recover operator lyrics from a pasted brief.

    Only complete, line-separated lyric lines inside a recognised section are
    claimed. Run-together pastes are reported as unstructured rather than
    guessed at. When the paste carries more than one copy of the song, the
    most complete copy is the one that is claimed.
    """
    result = OperatorLyrics()
    if not isinstance(source, str) or not source.strip():
        return result
    known = _known_sections()
    text = source.replace("\r\n", "\n").replace("\r", "\n")

    documents: list[_Document] = []
    current_doc = _Document()
    current: str | None = None
    first_tag: str | None = None

    def close_document() -> None:
        nonlocal current_doc, current, first_tag
        if current_doc.occurrences:
            documents.append(current_doc)
        current_doc = _Document()
        current = None
        first_tag = None

    def skip(line: str) -> None:
        if len(result.skipped_blocks) < MAX_SKIPPED_BLOCKS:
            result.skipped_blocks.append(line[:80])

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _BULLET.match(line):
            # A list item is never a sung line.
            skip(line)
            continue
        if _NUMBERED_HEADING.match(line):
            # A heading in the surrounding document ends the song region.
            close_document()
            skip(line)
            continue
        bracket = _BRACKETED.match(line)
        if bracket:
            name = bracket.group(1).strip()
            remainder = bracket.group(2).strip()
            canonical = _canonical_section(name, known)
            if canonical is not None:
                if first_tag is None:
                    first_tag = canonical
                elif canonical == first_tag and len(current_doc.occurrences) >= 3:
                    # A whole song has passed and the first section starts
                    # again: this is another copy of the song, not a repeat
                    # inside it. A song that opens with a section it repeats
                    # later keeps that repeat, because three sections have
                    # not passed yet.
                    close_document()
                    first_tag = canonical
                current = canonical
                current_doc.open_section(canonical)
                if remainder:
                    skip(line)
                continue
            if remainder and _CHORD_SYMBOL.match(name) and current is not None \
                    and not _is_instrumental(current) and _looks_like_lyric(remainder):
                current_doc.add_line(remainder)
                current_doc.lines_seen += 1
                continue
            skip(line)
            continue
        if _is_label(line):
            skip(line)
            continue
        if current is None:
            continue
        current_doc.lines_seen += 1
        if _is_instrumental(current):
            skip(line)
            continue
        if _looks_like_lyric(line):
            current_doc.add_line(line)
        else:
            skip(line)

    close_document()
    if not documents:
        result.notes.append(
            "no clean line-separated lyric lines were found inside a recognised "
            "section; the source may be run together or contain no lyrics")
        return result

    best = max(documents, key=lambda doc: (
        doc.sections_with_lines,
        len(doc.lines),
        -doc.lines_seen,
    ))
    collected: list[str] = []
    for tag, lines in best.occurrences:
        lines = [line for line in lines if line]
        if not lines:
            continue
        if tag not in result.tags:
            result.tags.append(tag)
        result.sections.setdefault(tag, []).extend(lines)
        result.section_order.append((tag, lines))
        collected.extend(lines)

    if not collected:
        result.notes.append(
            "no clean line-separated lyric lines were found inside a recognised "
            "section; the source may be run together or contain no lyrics")
        return result

    if len(documents) > 1:
        result.notes.append(
            f"the paste carried {len(documents)} copies of sectioned material; the "
            "most complete copy was claimed and the others were left alone")
    result.lines = collected
    result.lyrics = "\n".join(collected)
    result.found = True
    # "clean" means the structure is confidently identified: more than one
    # section carried lines and there is enough text to work with.
    if len(result.sections) >= 2 and len(collected) >= 4:
        result.confidence = "clean"
    else:
        result.confidence = "partial"
        result.notes.append(
            f"only {len(collected)} lyric line(s) across {len(result.sections)} "
            "section(s) were recovered; treat as partial")
    return result


def operator_lyrics_survived(extracted: OperatorLyrics, final_lyrics: str) -> list[str]:
    """Return the operator lines that are missing from the produced lyrics."""
    if not extracted.found:
        return []
    produced = (final_lyrics or "").replace("\r\n", "\n")
    return [line for line in extracted.lines if line not in produced]


def parse_lyric_sections(lyrics: str) -> list[tuple[str, list[str]]]:
    """Split a lyric block into ordered (tag, lines) pairs."""
    sections: list[tuple[str, list[str]]] = []
    current: str | None = None
    known = _known_sections()
    for raw in (lyrics or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        bracket = _BRACKETED.match(line)
        if bracket:
            canonical = _canonical_section(bracket.group(1).strip(), known)
            if canonical is not None:
                current = canonical
                sections.append((current, []))
                continue
        if current is None:
            continue
        sections[-1][1].append(line)
    return sections


def _section_key(tag: str) -> str:
    """The identity of a section, ignoring a trailing number.

    "Chorus 1" and "Chorus" are the same section; "Final Chorus" is its own
    section and must not be confused with it.
    """
    lowered = (tag or "").strip().casefold()
    return re.sub(r"\s+[1-9][0-9]*$", "", lowered)


def merge_operator_lyrics(
    extracted: OperatorLyrics, model_lyrics: str,
) -> tuple[str, list[str], list[str]]:
    """Assemble the final lyrics with the operator's own words taking priority.

    The operator's sections replace the model's version of the same section;
    model sections the operator did not write are kept; operator sections the
    model omitted are added back. The result is deterministic, so the model
    cannot silently rewrite or drop the operator's words.

    Sections are matched by identity, not by exact name, so a model that
    numbers its sections (``Chorus 1``, ``Chorus 2``) does not end up beside
    the operator's own repeated ``Chorus`` and double the song. The operator's
    order is kept, repeats included, so a chorus that comes back after a verse
    still comes back after that verse.

    Returns ``(merged_lyrics, replaced_sections, added_sections)``.
    """
    if not extracted.found:
        return model_lyrics, [], []
    model_sections = parse_lyric_sections(model_lyrics)
    operator_order = extracted.section_order or [
        (tag, list(lines)) for tag, lines in extracted.sections.items()]
    operator_keys = {_section_key(tag) for tag, _ in operator_order}

    merged: list[tuple[str, list[str]]] = []
    replaced: list[str] = []
    added: list[str] = []
    consumed: set[int] = set()
    pointer = 0
    for model_tag, model_lines in model_sections:
        key = _section_key(model_tag)
        match: int | None = None
        for index in range(pointer, len(operator_order)):
            if index in consumed:
                continue
            if _section_key(operator_order[index][0]) == key:
                match = index
                break
        if match is None:
            if key not in operator_keys:
                # The operator never wrote this section; the model's stands.
                merged.append((model_tag, list(model_lines)))
            continue
        consumed.add(match)
        pointer = match + 1
        op_tag, op_lines = operator_order[match]
        merged.append((op_tag, list(op_lines)))
        replaced.append(op_tag)
    for index, (op_tag, op_lines) in enumerate(operator_order):
        if index in consumed:
            continue
        merged.append((op_tag, list(op_lines)))
        added.append(op_tag)
    blocks = []
    for tag, lines in merged:
        body = "\n".join(lines)
        blocks.append(f"[{tag}]\n{body}" if body else f"[{tag}]")
    return "\n\n".join(blocks).strip(), replaced, added
