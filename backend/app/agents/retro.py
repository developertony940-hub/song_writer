"""Report-only retro analysis over the evidence the app already stores.

recommendations_validation.html R10: the retro agent starts in report mode.
It reads the ledger, the revision history and the stored critiques, and
reports what the numbers say. It does not change any policy; adoption is a
separate, explicitly approved action (R11).

The most useful signal is free: when the operator edits an agent draft and
saves it, the revision pair records the difference. The revision ``source``
field already separates a human save from an agent draft, so attribution
needs no new plumbing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.agents.contracts import PolicyVersion
from app.agents.records import read_run_records, summarise_run_records
from app.agents.policy_store import list_policies, policy_dir

log = logging.getLogger(__name__)

# A human edit is a revision whose source is not an agent-generated one.
AGENT_SOURCES = {"prompt_draft", "restore", "repaint", "promote"}


def human_edit_deltas(revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adjacent revision pairs that a human actually changed.

    Returns one row per pair with the hashes that differ, so the caller can
    show the operator exactly what was edited.
    """
    deltas: list[dict[str, Any]] = []
    for before, after in zip(revisions, revisions[1:]):
        if str(after.get("source")) in AGENT_SOURCES:
            continue
        lyrics_changed = before.get("lyrics_sha256") != after.get("lyrics_sha256")
        settings_changed = before.get("settings_sha256") != after.get("settings_sha256")
        if not (lyrics_changed or settings_changed):
            continue
        deltas.append({
            "from_version": before.get("version"),
            "to_version": after.get("version"),
            "source": after.get("source"),
            "lyrics_changed": lyrics_changed,
            "settings_changed": settings_changed,
            "revision_name": after.get("name", ""),
        })
    return deltas


def build_report(pid: str, state_dir: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Assemble the retro report for one project."""
    ledger = summarise_run_records(read_run_records(state_dir))
    state = _load_state(state_dir)
    revisions = [record.model_dump(mode="json") for record in (state.revisions if state else [])]
    deltas = human_edit_deltas(revisions)
    critiques = [record.model_dump(mode="json") for record in (state.critiques if state else [])]
    agents: dict[str, int] = {}
    for row in read_run_records(state_dir):
        for defect in row.get("defects") or []:
            agents[str(defect)] = agents.get(str(defect), 0) + 1
    policies: dict[str, Any] = {}
    if root is not None and policy_dir(root).is_dir():
        for directory in sorted(policy_dir(root).iterdir()):
            if directory.is_dir():
                versions = list_policies(root, directory.name)
                policies[directory.name] = {
                    "versions": len(versions),
                    "active": next((v.policy_version for v in versions if v.status == "active"), None),
                    "approved": [v.policy_version for v in versions if v.status != "draft"],
                }
    return {
        "project_id": pid,
        "runs": ledger["runs"],
        "tokens_in": ledger["tokens_in"],
        "tokens_out": ledger["tokens_out"],
        "total_tokens": ledger["total_tokens"],
        "elapsed_ms": ledger["elapsed_ms"],
        "outcomes": ledger["outcomes"],
        "defects": ledger["defects"],
        "human_edit_deltas": deltas,
        "human_edit_count": len(deltas),
        "stored_critiques": len(critiques),
        "revision_count": len(revisions),
        "policies": policies,
        "adoption_ready": len(deltas) >= 20,
        "adoption_note": (
            "enough human edits have accumulated for a policy change to be considered"
            if len(deltas) >= 20 else
            f"{len(deltas)} human edit delta(s) recorded; 20 are required before a "
            "policy change is considered"),
    }


def propose_policy_change(report: dict[str, Any], agent: str, *,
                           current: PolicyVersion | None,
                           note: str = "") -> PolicyVersion:
    """Draft a candidate policy for review. It is never active on creation."""
    defects = report.get("defects") or {}
    hottest = sorted(defects.items(), key=lambda item: item[1], reverse=True)
    template = (current.template if current else "")
    if hottest:
        template = template or (
            "You are a careful song producer. Reply with one JSON object. "
            f"Avoid these known defects: {', '.join(code for code, _ in hottest)}.")
    return PolicyVersion(
        agent=agent,
        policy_version=(current.policy_version + 1) if current else 1,
        template=template,
        model_role=current.model_role if current else "strong_prose",
        model_id=current.model_id if current else "",
        temperature=current.temperature if current else 0.4,
        exemplars=list(current.exemplars) if current else [],
        budget=dict(current.budget) if current else {},
        scorecard={
            "golden_pass": report.get("adoption_note", ""),
            "defects_addressed": [code for code, _ in hottest],
            "human_edit_deltas": report.get("human_edit_count", 0),
        },
        status="draft",
        notes=note,
    )


def _load_state(state_dir: Path):
    from app.models.music import MusicState

    path = state_dir / "state.json"
    if not path.is_file():
        return None
    try:
        return MusicState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a damaged state is reported
        log.warning("music state could not be read: %s", exc)
        return None
