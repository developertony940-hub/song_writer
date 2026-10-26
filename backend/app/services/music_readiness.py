"""Conservative, read-only music readiness observations.

Pure functions have no third-party dependencies. The application adapter is
lazy-imported and deliberately avoids MusicService.status/_load_state because
those convenience readers create state directories.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from app.services.music_input_review import estimate_lyric_duration, review_music_inputs


def device_mapping(stats: Any, *, configured_index: int, policy_slot: int) -> dict[str, Any]:
    devices = stats.get("devices", []) if isinstance(stats, dict) else []
    if not isinstance(devices, list):
        devices = []
    mismatch = configured_index != policy_slot
    reasons = []
    if mismatch:
        reasons.append("Configured ComfyUI device index differs from the scheduler policy slot.")
    cuda_devices = [d for d in devices if isinstance(d, dict) and str(d.get("type", "")).lower() == "cuda"]
    indexes = [d["index"] for d in cuda_devices if type(d.get("index")) is int]
    if indexes and configured_index not in indexes:
        mismatch = True
        reasons.append("Endpoint-reported CUDA indexes do not include the configured device index (possible local-index remapping).")
    if devices and not cuda_devices:
        mismatch = True
        reasons.append("Endpoint reports no CUDA device for the CUDA scheduler policy.")
    if len(cuda_devices) > 1:
        reasons.append("Multiple CUDA devices reported; execution device selection is not established.")
    return {
        "status": "mismatch" if mismatch else "unknown",
        "reason": " ".join(reasons) or "Endpoint-local index/name is not a verified host physical GPU identity; CUDA visibility remapping and process ownership are unknown.",
        "devices": devices,
    }


def summarize_queue(payload: Any) -> dict[str, Any]:
    known = (isinstance(payload, dict)
             and isinstance(payload.get("queue_running"), list)
             and isinstance(payload.get("queue_pending"), list))
    # Counts only: do not echo queued workflows, user prompts or client secrets.
    return {"known": known,
            "running": len(payload["queue_running"]) if known else None,
            "pending": len(payload["queue_pending"]) if known else None}


def build_music_readiness(
    *, project_id: str, state: Mapping[str, Any], journal: Mapping[str, Any],
    hashes_match: bool, policy: Mapping[str, Any], router: Mapping[str, Any],
    endpoint: str, system_stats: Any = None, queue: Any = None,
    known_sections: tuple[str, ...] = (), observation_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Build an advisory snapshot; never turn configuration or free VRAM into proof."""
    blockers: list[dict[str, str]] = []

    def block(code: str, message: str) -> None:
        blockers.append({"code": code, "message": message})

    draft, approval = state.get("draft"), state.get("approval")
    if not draft:
        block("draft_missing", "Save lyrics and musical settings before approval.")
    if not approval or not hashes_match:
        block("approval_required", "Exact saved lyrics/settings hashes need matching human approval.")
    if not policy.get("live_generation_authorized"):
        block("live_disabled", "The process live-generation authorization hold is closed.")
    recorded_caps = (state.get("lifetime_cap"), (approval or {}).get("submission_cap"))
    if any(n is not None and (type(n) is not int or n <= 0) for n in recorded_caps):
        block("cap_invalid", "A recorded submission cap is not a positive finite integer; accounting needs review.")
    caps = [n for n in recorded_caps if type(n) is int]
    cap = min(caps) if caps else None
    used = journal.get("submissions", 0)
    if cap is None:
        block("cap_missing", "No finite lifetime submission cap is recorded.")
    elif used >= cap:
        block("cap_exhausted", "The recorded lifetime submission cap is exhausted.")
    attempt = state.get("current_attempt")
    if attempt and attempt.get("status") == "running":
        block("attempt_running", "A recorded music attempt is running.")
    if state.get("recovery_required") or journal.get("orphans") or journal.get("open_uncertain"):
        block("recovery_required", "A prior attempt requires separately confirmed recovery.")
    slot_key = str(policy["slot"])
    if router.get("holds_unreadable") or router.get("holds", {}).get(slot_key):
        block("capacity_held", "Scheduler capacity is held or its persisted hold cannot be read.")
    slot = router.get("slots", {}).get(slot_key)
    if slot is None:
        block("router_unknown", "Scheduler slot state is unavailable.")
    elif slot.get("locked_by"):
        block("slot_busy", "Another cooperative lease owns the scheduler slot.")
    if slot and slot.get("current_model") not in (None, policy["model"]):
        block("model_switch_required", "The scheduler records another model; this GET will not unload or switch it.")
    online = isinstance(system_stats, dict) and isinstance(system_stats.get("devices"), list)
    if not online:
        block("runtime_unavailable", "Endpoint /system_stats is unavailable or malformed.")
    observed_queue = summarize_queue(queue)
    if not observed_queue["known"]:
        block("queue_unknown", "Endpoint queue could not be inspected.")
    elif observed_queue["running"] or observed_queue["pending"]:
        block("queue_busy", "The endpoint has running or pending work; ownership is not established.")
    mapping = device_mapping(system_stats, configured_index=policy["configured_device_index"], policy_slot=policy["slot"])
    block("device_mapping_" + mapping["status"], mapping["reason"])
    block("ownership_unknown", "No verified endpoint process-to-physical-GPU ownership evidence is available.")
    block("exclusive_capacity_unknown", "An empty queue and free memory do not verify exclusive capacity against external processes.")
    block("workflow_unverified", "This snapshot does not verify installed node schemas, checkpoint bytes or model residency; the existing generation route retains its own guards.")
    if not policy.get("capacity_verified") and not policy.get("capacity_override_authorized"):
        block("capacity_unverified", "No prior capacity verification or explicit experimental override is recorded.")
    settings = (draft or {}).get("settings", {})
    duration = None
    issues = []
    if settings:
        args = dict(lyrics=settings["lyrics"], arrangement=settings.get("arrangement", {}),
                    musical_direction=settings["musical_direction"], bpm=settings.get("bpm"))
        duration = estimate_lyric_duration(**args)
        issues = review_music_inputs(**args, duration_s=settings["duration_s"], known_sections=known_sections)
    captures = []
    for row in journal.get("rows", []):
        artifacts = {k: v for k, v in row.items()
                     if isinstance(k, str) and k.endswith(("_path", "_sha256")) and v is not None}
        if artifacts:
            captures.append({"attempt_id": row.get("attempt_id"), "kind": row.get("kind"), "artifacts": artifacts})
    return {
        "schema_version": 1, "project_id": project_id, "read_only": True,
        "ready": False, "can_generate": False,
        "status": "blocked" if any(b["code"] not in {
            "device_mapping_unknown", "ownership_unknown", "exclusive_capacity_unknown", "workflow_unverified"
        } for b in blockers) else "unknown",
        "blockers": blockers,
        "approvals": {"draft_version": (draft or {}).get("version"),
                      "lyrics_sha256": (draft or {}).get("lyrics_sha256"),
                      "settings_sha256": (draft or {}).get("settings_sha256"),
                      "record": approval, "hashes_match": hashes_match,
                      "audio_approval": state.get("audio_approval")},
        "submission": {"lifetime_cap": state.get("lifetime_cap"), "effective_cap": cap,
                       "used": used, "remaining": None if cap is None else max(0, cap - used),
                       "current_attempt": attempt, "recovery_required": bool(state.get("recovery_required")),
                       "orphans": journal.get("orphans", []), "open_uncertain": journal.get("open_uncertain", [])},
        "runtime": {"endpoint": endpoint, "online": online, "system_stats": system_stats,
                    "queue": observed_queue, "policy": dict(policy), "device_mapping": mapping,
                    "ownership": {"status": "unknown", "reason": "No physical process ownership attestation."},
                    "exclusive_capacity": {"status": "unknown", "reason": "Cooperative scheduler only; external use is not controlled."},
                    "router": dict(router), "observation_errors": observation_errors or []},
        "input_review": {"issues": issues, "duration": duration}, "captures": captures,
        "actions": {"generation": {"method": "POST", "path": f"/api/music/{project_id}/generate",
                                   "enabled": False, "reason": "Readiness is not verified; no dispatch is provided by this endpoint."},
                    "dispatch_added": False},
        "limitations": ["Point-in-time non-atomic observation, not a reservation or generation authorization.",
                        "Capture references are existing journal metadata, not fresh artifact/hash verification.",
                        "Configured capacity flags are historical/operator claims, not current measured exclusivity.",
                        "Readiness and tests are not listening acceptance, lyric coverage, or audio quality evidence."],
    }


async def observe_endpoint(client: Any) -> tuple[Any, Any, list[str]]:
    """The injected async HTTP client needs only get(); only two GETs are made."""
    results = []
    errors = []
    for path in ("/system_stats", "/queue"):
        try:
            response = await client.get(path)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            results.append(data)
        except Exception as exc:
            results.append(None)
            errors.append(f"{path}: {type(exc).__name__}")
    return results[0], results[1], errors


def read_persisted_snapshot(root: Path, pid: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read existing files only, with no ProjectStore mkdir/migration helpers."""
    if not re.fullmatch(r"[0-9a-f]{12}", pid):
        raise KeyError(pid)
    root = Path(root).resolve()
    project = (root / pid).resolve()
    if project.parent != root or not (project / "manifest.json").is_file():
        raise KeyError(pid)
    directory = project / "music" / ".state"
    # Refuse external symlink targets even though nothing here writes.
    if not directory.resolve().is_relative_to(project):
        raise ValueError("music state escapes project directory")
    state_path, journal_path = directory / "state.json", directory / "attempts.jsonl"
    for path in (state_path, journal_path):
        if not path.resolve().is_relative_to(project):
            raise ValueError("music state file escapes project directory")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if not isinstance(state, dict):
        raise ValueError("music state must be an object")
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()] if journal_path.is_file() else []
    return state, rows


async def music_readiness(pid: str, *, client: Any = None, settings: Any = None,
                          model_router: Any = None) -> dict[str, Any]:
    """Application adapter; client injection supports httpx.MockTransport clients."""
    from app.config import get_settings
    from app.models.music import MusicState
    from app.services.model_router import PROFILES, get_router
    from app.services.music_service import MusicService, _draft_hashes

    cfg = settings if settings is not None else get_settings()
    raw, rows = read_persisted_snapshot(cfg.projects_root_path, pid)
    state = MusicState.model_validate(raw)
    journal = MusicService._journal_state(SimpleNamespace(_read_journal=lambda _pid: rows), pid)
    match = False
    if state.draft and state.approval:
        lyrics_sha, settings_sha = _draft_hashes(state.draft.settings)
        match = lyrics_sha == state.approval.lyrics_sha256 and settings_sha == state.approval.settings_sha256
    scheduler = model_router if model_router is not None else get_router()
    router_snapshot = scheduler.status()
    router_snapshot["holds_unreadable"] = scheduler.holds_unreadable()
    if client is None:
        import httpx
        async with httpx.AsyncClient(base_url=cfg.comfyui.base_url.rstrip("/"), timeout=5.0,
                                     follow_redirects=False) as http:
            stats, queue, errors = await observe_endpoint(http)
    else:
        stats, queue, errors = await observe_endpoint(client)
    profile = PROFILES["ace-step-1.5"]
    policy = {"model": "ace-step-1.5", "slot": int(profile.slot),
              "configured_device_index": cfg.comfyui.gpu,
              "capacity_verified": cfg.music.capacity_verified,
              "capacity_override_authorized": cfg.music.capacity_override_authorized,
              "live_generation_authorized": cfg.music.live_generation_authorized}
    return build_music_readiness(project_id=pid, state=state.model_dump(mode="json"), journal=journal,
                                hashes_match=match, policy=policy, router=router_snapshot,
                                endpoint=cfg.comfyui.base_url, system_stats=stats, queue=queue,
                                known_sections=tuple(cfg.music.known_sections), observation_errors=errors)
