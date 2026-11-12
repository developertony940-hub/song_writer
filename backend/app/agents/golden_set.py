"""The golden set: two real briefs, replayed offline against real validators.

recommendations_validation.html V3: the plan assumed eight golden briefs and
only three existed, two of them usable. This ships the two usable ones so the
retro gate has something to replay, and records the expectations that a
policy change must not break.

The briefs are stored in ``golden/`` beside this module rather than in
``projects/`` so the set is stable and self-contained.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).parent / "golden"
INDEX_NAME = "index.json"


def golden_dir() -> Path:
    return GOLDEN_DIR


def load_index() -> list[dict[str, Any]]:
    path = GOLDEN_DIR / INDEX_NAME
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data) if isinstance(data, list) else []


def load_brief(entry: dict[str, Any]) -> str:
    path = GOLDEN_DIR / str(entry.get("file") or "")
    if not path.is_file():
        raise FileNotFoundError(f"golden brief is missing: {path}")
    return path.read_text(encoding="utf-8")


def brief_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_set() -> list[dict[str, Any]]:
    """Check that every stored brief still matches its recorded hash."""
    report: list[dict[str, Any]] = []
    for entry in load_index():
        path = GOLDEN_DIR / str(entry.get("file") or "")
        if not path.is_file():
            report.append({"name": entry.get("name"), "present": False, "matches": False})
            continue
        text = path.read_text(encoding="utf-8")
        matches = brief_hash(text) == entry.get("sha256")
        report.append({
            "name": entry.get("name"),
            "present": True,
            "matches": matches,
            "chars": len(text),
        })
    return report


def replay_one(entry: dict[str, Any], *, known_sections: tuple[str, ...]) -> dict[str, Any]:
    """Replay one golden brief through the deterministic validators only.

    No model is called: this measures what the app can prove about a brief
    without spending tokens, which is the floor a policy change must not
    fall below.
    """
    from app.services.music_draft_service import build_draft_settings
    from app.services.operator_lyrics import extract_operator_lyrics

    text = load_brief(entry)
    extracted = extract_operator_lyrics(text)
    return {
        "name": entry.get("name"),
        "operator_lyrics": extracted.found,
        "operator_lines": extracted.line_count,
        "confidence": extracted.confidence,
        "expected_lyrics": bool(entry.get("expect_operator_lyrics")),
        "expectation_met": extracted.found == bool(entry.get("expect_operator_lyrics")),
    }
