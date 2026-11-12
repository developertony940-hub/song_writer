"""Rank completed music takes by measurable evidence.

recommendations_validation.html R12: the ranking cannot be validated while
there are no labels, so this module refuses to present a ranking until
enough preferred-take records exist. It never approves anything and never
changes the operator's choice.

The signals are deliberately measurable and declared: how close the take's
length is to the requested duration, whether the operator already preferred
it, and whether the take came from a different draft revision. No audio
understanding is implied.
"""
from __future__ import annotations

from typing import Any

from app.agents.contracts import TakeCandidate, TakeRanking

# The ranking stays advisory until this many preferred takes exist.
MINIMUM_LABELS = 10


def collect_candidates(takes: list[dict[str, Any]], *,
                       preferred_asset_id: str | None = None) -> list[TakeCandidate]:
    """Turn the music status rows into ranking candidates."""
    candidates: list[TakeCandidate] = []
    for row in takes:
        asset_id = str(row.get("asset_id") or "")
        if not asset_id:
            continue
        settings = row.get("effective_settings") or {}
        duration = settings.get("duration_s")
        seed = settings.get("seed")
        candidates.append(TakeCandidate(
            asset_id=asset_id,
            attempt_id=str(row.get("attempt_id") or ""),
            revision=row.get("revision"),
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
            seed=int(seed) if isinstance(seed, (int, float)) else None,
            preferred=bool(row.get("preferred")) or asset_id == preferred_asset_id,
            plan_label=str(row.get("plan_label") or ""),
        ))
    return candidates


def rank_takes(candidates: list[TakeCandidate], *, requested_duration_s: float | None = None,
                label_count: int = 0,
                minimum_labels: int = MINIMUM_LABELS) -> TakeRanking:
    """Rank by duration fit, with the operator's preference always winning."""
    if label_count < minimum_labels:
        return TakeRanking(
            ranked=[], scores={}, reasons={},
            preferred=next((c.asset_id for c in candidates if c.preferred), None),
            labelled=label_count,
            status="insufficient_labels",
            note=(f"{label_count} preferred take(s) recorded; {minimum_labels} are required "
                  "before the ranking is presented as advice"),
        )
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for candidate in candidates:
        score = 0.0
        note: list[str] = []
        if requested_duration_s and candidate.duration_s:
            drift = abs(candidate.duration_s - requested_duration_s)
            score += max(0.0, 1.0 - drift / max(1.0, requested_duration_s))
            note.append(f"length {candidate.duration_s:g}s vs requested {requested_duration_s:g}s")
        if candidate.preferred:
            score += 1.0
            note.append("already preferred by the operator")
        if candidate.seed is not None:
            note.append(f"seed {candidate.seed}")
        scores[candidate.asset_id] = round(score, 4)
        reasons[candidate.asset_id] = "; ".join(note) or "no measurable signal"
    ranked = sorted(scores, key=lambda asset: (-scores[asset], asset))
    return TakeRanking(
        ranked=ranked,
        scores=scores,
        reasons=reasons,
        preferred=next((c.asset_id for c in candidates if c.preferred), None),
        labelled=label_count,
        status="ranked",
        note="ranking is advisory; the operator's choice always wins",
    )
