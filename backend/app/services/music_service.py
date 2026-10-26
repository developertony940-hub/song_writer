"""Bounded ACE-Step music workflow service.

Music is kept separate from narration and social-video generation. This module
owns the versioned lyrics/settings draft, explicit human approval, durable
attempt journal, server-side live-generation hold, ComfyUI submission, and
byte-bound audio approval.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import hashlib
import hmac
import json
import httpx
import logging
import os
import platform
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from array import array
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from app.config import get_settings
from app.models.music import (
    SECTION_TAG_RE,
    MusicApprovalRecord,
    MusicAttemptRecord,
    MusicAudioApprovalRecord,
    MusicCritiqueRecord,
    MusicCritiqueRequest,
    MusicDraftRecord,
    MusicLyricVariant,
    MusicPreferredTakeRecord,
    MusicRepaintCandidateRecord,
    MusicRepaintProposalRecord,
    MusicRevisionRecord,
    MusicSectionBoundaryRecord,
    MusicSettings,
    MusicState,
    MusicVariantPlanItem,
    MusicVariantRequest,
    parse_section_tags,
)
from app.models.schemas import AssetKind, AssetRef
from app.services.comfyui_client import ComfyUICancelled, ComfyUIClient, ComfyUIError, get_comfyui
from app.services.model_router import CapacityHeld, ModelSwitchRequired, get_router
from app.services.music_input_review import review_music_inputs
from app.services.project_store import ProjectStore, get_store
from app.services.provenance import WORKFLOWS_DIR, asset_provenance, workflow_record
from app.services.repaint_provider import (
    NATIVE_PROVIDER,
    NATIVE_REQUEST_SCHEMA,
    AceRepaintClient,
    DeterministicRepaintProvider,
    NativeExecution,
    NativePreparedRequest,
    NativeRepaintFailed,
    NativeRepaintTimeout,
    RepaintOutput,
    RepaintProvider,
    RepaintRequest,
    build_native_multipart,
)

log = logging.getLogger(__name__)

MODEL = "ace-step-1.5"
TERMINAL_KINDS = {"attempt_completed", "attempt_failed", "attempt_cancelled", "attempt_uncertain"}


class MusicError(RuntimeError):
    pass


class MusicNotAuthorized(MusicError):
    pass


class MusicApprovalRequired(MusicError):
    pass


class MusicCapReached(MusicError):
    pass


class MusicRecoveryRequired(MusicError):
    pass


class MusicConflict(MusicError):
    pass


@contextlib.contextmanager
def _file_lock(path: Path, timeout_s: float = 30.0):
    """Cross-process exclusive lock using a durable lock file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise MusicConflict(
                        f"shared accounting lock is busy: {path.name}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


class MusicRuntimeUnavailable(MusicError):
    def __init__(self, message: str, runtime: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.runtime = runtime or {}


class MusicGenerationFailed(MusicError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _normalize_lyrics(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return text + "\n" if text else ""


def _settings_hash(settings: MusicSettings) -> str:
    payload = settings.model_dump(mode="json")
    payload.pop("lyrics", None)
    if not payload.get("arrangement"):
        payload.pop("arrangement", None)
    for knob in ("temperature", "top_p", "cfg_scale"):
        if payload.get(knob) is None:
            payload.pop(knob, None)
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _draft_hashes(settings: MusicSettings) -> tuple[str, str]:
    return _sha256_text(_normalize_lyrics(settings.lyrics)), _settings_hash(settings)


def _effective_tags(settings: MusicSettings) -> str:
    direction = settings.musical_direction.strip()
    if not settings.arrangement:
        return direction
    directives = "; ".join(
        f"{section}: {directive.strip()}" for section, directive in settings.arrangement.items())
    return f"{direction}; {directives}"


def _plan_overrides(item: MusicVariantPlanItem) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for field_name in ("seed", "key_scale", "bpm", "temperature", "top_p", "cfg_scale"):
        value = getattr(item, field_name)
        if value is not None:
            overrides[field_name] = value
    return overrides


class MusicService:
    def __init__(self, *, store: ProjectStore | None = None, comfy=None, router=None) -> None:
        self.store = store or get_store()
        self.comfy = comfy or get_comfyui()
        self.router = router or get_router()
        self._lock = asyncio.Lock()
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._thread_locks: dict[str, threading.Lock] = {}
        self._thread_lock_guard = threading.Lock()

    @contextlib.contextmanager
    def _project_lock(self, pid: str):
        with self._thread_lock_guard:
            thread_lock = self._thread_locks.get(pid)
            if thread_lock is None:
                thread_lock = threading.Lock()
                self._thread_locks[pid] = thread_lock
        with thread_lock:
            with _file_lock(self._state_dir(pid) / "accounting.lock"):
                yield

    # --- filesystem state -------------------------------------------
    def _music_dir(self, pid: str) -> Path:
        return self.store.asset_dir(pid, AssetKind.music)

    def _state_dir(self, pid: str) -> Path:
        directory = self._music_dir(pid) / ".state"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _state_path(self, pid: str) -> Path:
        return self._state_dir(pid) / "state.json"

    def _journal_path(self, pid: str) -> Path:
        return self._state_dir(pid) / "attempts.jsonl"

    def _load_state(self, pid: str) -> MusicState:
        path = self._state_path(pid)
        if not path.is_file():
            return MusicState()
        try:
            state = MusicState.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise MusicConflict(f"music state is unreadable: {exc}") from exc
        if state.schema_version < 5:
            state.schema_version = 5
        return state

    def _save_state(self, pid: str, state: MusicState) -> None:
        state.updated_at = _utcnow()
        path = self._state_path(pid)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _append_journal(self, pid: str, row: dict[str, Any]) -> None:
        path = self._journal_path(pid)
        line = json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_journal(self, pid: str) -> list[dict[str, Any]]:
        path = self._journal_path(pid)
        if not path.is_file():
            return []
        try:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception as exc:
            raise MusicConflict(f"music attempt journal is unreadable: {exc}") from exc

    def _journal_state(self, pid: str) -> dict[str, Any]:
        rows = self._read_journal(pid)
        started: dict[str, dict[str, Any]] = {}
        terminal: set[str] = set()
        uncertain: set[str] = set()
        recovered: set[str] = set()
        failures: list[str] = []
        invalid = False
        for row in rows:
            if not isinstance(row, dict):
                invalid = True
                continue
            kind = row.get("kind")
            attempt_id = row.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                invalid = True
                continue
            if kind == "attempt_started":
                if attempt_id in started:
                    invalid = True
                    continue
                started[attempt_id] = row
            elif kind in TERMINAL_KINDS:
                if attempt_id not in started or attempt_id in terminal:
                    invalid = True
                    continue
                terminal.add(attempt_id)
                if kind == "attempt_uncertain":
                    uncertain.add(attempt_id)
                elif kind == "attempt_failed":
                    failures.append(attempt_id)
            elif kind == "recovery_confirmed":
                if attempt_id not in started:
                    invalid = True
                    continue
                recovered.add(attempt_id)
                terminal.add(attempt_id)
            elif kind == "cancel_requested":
                if attempt_id not in started:
                    invalid = True
                continue
            else:
                invalid = True
        if invalid:
            raise MusicConflict("music attempt journal contains inconsistent rows")
        orphans = sorted(attempt_id for attempt_id in started if attempt_id not in terminal)
        open_uncertain = sorted(attempt_id for attempt_id in uncertain if attempt_id not in recovered)
        return {
            "rows": rows,
            "submissions": len(started),
            "completed": sum(1 for row in rows if row.get("kind") == "attempt_completed"),
            "failed": len(failures),
            "uncertain": len(open_uncertain),
            "orphans": orphans,
            "open_uncertain": open_uncertain,
            "attempt_ids": sorted(started),
            "plan_labels": sorted(
                {row["plan_label"] for row in started.values() if row.get("plan_label")}),
        }

    def _effective_cap(self, state: MusicState) -> int | None:
        if state.approval is None:
            return state.lifetime_cap
        if state.lifetime_cap is None:
            return state.approval.submission_cap
        return min(state.lifetime_cap, state.approval.submission_cap)

    def _approval_mismatch(self, state: MusicState) -> str | None:
        if state.draft is None or state.approval is None:
            return None
        lyrics_sha, settings_sha = _draft_hashes(state.draft.settings)
        lyrics_changed = lyrics_sha != state.approval.lyrics_sha256
        settings_changed = settings_sha != state.approval.settings_sha256
        if lyrics_changed and settings_changed:
            return "lyrics and settings changed after approval"
        if lyrics_changed:
            return "lyrics changed after approval"
        if settings_changed:
            return "settings changed after approval"
        return None

    def _approval_matches(self, state: MusicState) -> bool:
        if state.draft is None or state.approval is None:
            return False
        return self._approval_mismatch(state) is None

    def _ensure_ready(self, pid: str) -> tuple[MusicState, dict[str, Any], int]:
        settings = get_settings()
        if not settings.music.live_generation_authorized:
            raise MusicNotAuthorized(
                "live music generation is not authorized for this process")
        state = self._load_state(pid)
        if state.draft is None:
            raise MusicApprovalRequired("save lyrics and musical direction first")
        if state.approval is None:
            raise MusicApprovalRequired("lyrics/settings approval is required")
        mismatch = self._approval_mismatch(state)
        if mismatch is not None:
            raise MusicApprovalRequired(f"{mismatch}; reapprove them")
        journal = self._journal_state(pid)
        if journal["orphans"] or journal["open_uncertain"]:
            reason = "unresolved prior attempt: " + ", ".join(
                sorted(set(journal["orphans"]) | set(journal["open_uncertain"])))
            if not state.recovery_required or state.recovery_reason != reason:
                state.recovery_required = True
                state.recovery_reason = reason
                self._save_state(pid, state)
            raise MusicRecoveryRequired(reason)
        if state.recovery_required:
            raise MusicRecoveryRequired(state.recovery_reason or "recovery is required")
        cap = self._effective_cap(state)
        if cap is None:
            raise MusicApprovalRequired("no lifetime submission cap is recorded")
        if journal["submissions"] >= cap:
            raise MusicCapReached(
                f"lifetime submission cap reached ({journal['submissions']}/{cap})")
        if state.current_attempt is not None and state.current_attempt.status == "running":
            raise MusicConflict("a music attempt is already running")
        return state, journal, cap

    # --- draft and approval -----------------------------------------
    def _preflight(self, settings: MusicSettings) -> list[dict[str, Any]]:
        cfg = get_settings().music
        return review_music_inputs(
            lyrics=_normalize_lyrics(settings.lyrics),
            musical_direction=settings.musical_direction,
            duration_s=settings.duration_s,
            arrangement=settings.arrangement,
            known_sections=cfg.known_sections,
            seconds_per_line=cfg.preflight_seconds_per_line,
        )

    def save_draft(
        self,
        pid: str,
        settings: MusicSettings,
        revision_name: str | None = None,
        *,
        source: str = "draft",
    ) -> dict[str, Any]:
        normalized = settings.model_copy(update={"lyrics": _normalize_lyrics(settings.lyrics)})
        if len(normalized.lyrics.strip()) < 8:
            raise MusicConflict("lyrics must contain at least 8 visible characters")
        lyrics_sha, settings_sha = _draft_hashes(normalized)
        state = self._load_state(pid)
        same = (
            state.draft is not None
            and state.draft.lyrics_sha256 == lyrics_sha
            and state.draft.settings_sha256 == settings_sha
        )
        version = state.draft.version if same and state.draft else ((state.draft.version + 1) if state.draft else 1)
        revision_created = False
        if same:
            draft_name = state.draft.revision_name if state.draft else ""
        else:
            max_revisions = get_settings().music.max_revisions
            if len(state.revisions) + 1 > max_revisions:
                would_drop = [record.version for record in state.revisions[:1]]
                references = self._revision_references(pid, state)
                held_desc = "; ".join(
                    f"v{v} ({', '.join(sorted(references[v]))})"
                    for v in would_drop if v in references)
                raise MusicConflict(
                    f"revision retention limit ({max_revisions}) reached"
                    f"{': ' + held_desc if held_desc else ''}; history is never "
                    "deleted automatically - increase music.max_revisions in "
                    "settings to record more revisions")
            name = (revision_name or "").strip()
            state.revisions.append(MusicRevisionRecord(
                version=version,
                name=name,
                parent_version=state.draft.version if state.draft else None,
                source=source,
                settings=normalized,
                lyrics_sha256=lyrics_sha,
                settings_sha256=settings_sha,
            ))
            draft_name = name
            revision_created = True
        state.draft = MusicDraftRecord(
            version=version,
            settings=normalized,
            lyrics_sha256=lyrics_sha,
            settings_sha256=settings_sha,
            revision_name=draft_name,
        )
        if not same:
            state.approval = None
            state.audio_approval = None
        self._save_state(pid, state)
        result = state.model_dump(mode="json")
        result["preflight"] = self._preflight(normalized)
        result["revision_created"] = revision_created
        return result

    def approve(
        self,
        pid: str,
        note: str,
        submission_cap: int,
        variant_plan: list[MusicVariantPlanItem] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if not note.strip():
            raise MusicConflict("approval note is required")
        if submission_cap > settings.music.max_submission_cap:
            raise MusicConflict(
                f"submission cap exceeds configured maximum ({settings.music.max_submission_cap})")
        plan = list(variant_plan or [])
        labels = [item.label for item in plan]
        if len(set(labels)) != len(labels):
            raise MusicConflict("variant plan labels must be unique")
        if len(plan) > submission_cap:
            raise MusicConflict(
                f"variant plan has {len(plan)} entries but the submission cap is {submission_cap}")
        state = self._load_state(pid)
        if state.draft is None:
            raise MusicApprovalRequired("save lyrics and musical direction first")
        lyrics_sha, settings_sha = _draft_hashes(state.draft.settings)
        if state.lifetime_cap is not None and submission_cap > state.lifetime_cap:
            raise MusicConflict(
                f"lifetime cap cannot be increased beyond {state.lifetime_cap}")
        state.lifetime_cap = (
            submission_cap if state.lifetime_cap is None
            else min(state.lifetime_cap, submission_cap)
        )
        state.approval = MusicApprovalRecord(
            draft_version=state.draft.version,
            lyrics_sha256=lyrics_sha,
            settings_sha256=settings_sha,
            submission_cap=submission_cap,
            note=note.strip(),
            variant_plan=plan,
        )
        state.audio_approval = None
        self._save_state(pid, state)
        return state.model_dump(mode="json")

    # --- critique registry ------------------------------------------
    def save_critique(self, pid: str, req: MusicCritiqueRequest) -> dict[str, Any]:
        cfg = get_settings().music
        state = self._load_state(pid)
        if state.draft is None:
            raise MusicApprovalRequired("save lyrics and musical direction first")
        summary = req.summary.strip()
        if not summary:
            raise MusicConflict("critique summary is required")
        record = MusicCritiqueRecord(
            critique_id=uuid.uuid4().hex[:10],
            draft_version=state.draft.version,
            lyrics_sha256=state.draft.lyrics_sha256,
            settings_sha256=state.draft.settings_sha256,
            summary=summary,
            suggestions=req.suggestions,
            key_candidates=list(req.key_candidates),
            refined_direction=req.refined_direction,
        )
        state.critiques.append(record)
        overflow = len(state.critiques) - cfg.max_critiques
        if overflow > 0:
            state.critiques = state.critiques[overflow:]
        self._save_state(pid, state)
        return record.model_dump(mode="json")

    def get_critique(self, pid: str, critique_id: str) -> dict[str, Any]:
        state = self._load_state(pid)
        for record in state.critiques:
            if record.critique_id == critique_id:
                return record.model_dump(mode="json")
        raise KeyError(critique_id)

    # --- lyric variant registry ---------------------------------------
    def register_variant(self, pid: str, req: MusicVariantRequest) -> dict[str, Any]:
        cfg = get_settings().music
        state = self._load_state(pid)
        if state.draft is None:
            raise MusicApprovalRequired("save lyrics and musical direction first")
        if any(variant.name == req.name for variant in state.variants):
            raise MusicConflict(f"a lyric variant named {req.name!r} already exists")
        if len(state.variants) >= cfg.max_variants:
            raise MusicConflict(
                f"the lyric variant registry is full ({cfg.max_variants} entries)")
        text = req.text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not text.strip():
            raise MusicConflict("variant text is empty")
        if parse_section_tags(text):
            raise MusicConflict("variant text must not contain [section] tags")
        lyrics = _normalize_lyrics(state.draft.settings.lyrics)
        tags = parse_section_tags(lyrics)
        count = sum(1 for name, _, _ in tags if name == req.section)
        if count == 0:
            raise MusicConflict(f"the current draft has no [{req.section}] section")
        if req.occurrence > count:
            raise MusicConflict(
                f"the current draft has {count} [{req.section}] section(s); "
                f"occurrence {req.occurrence} does not exist")
        variant = MusicLyricVariant(
            name=req.name,
            section=req.section,
            occurrence=req.occurrence,
            text=text,
            lyrics_sha256=state.draft.lyrics_sha256,
            note=req.note.strip(),
        )
        state.variants.append(variant)
        self._save_state(pid, state)
        return variant.model_dump(mode="json")

    def get_variant(self, pid: str, name: str) -> dict[str, Any]:
        state = self._load_state(pid)
        for variant in state.variants:
            if variant.name == name:
                result = variant.model_dump(mode="json")
                result["stale"] = bool(
                    state.draft is not None
                    and variant.lyrics_sha256 != state.draft.lyrics_sha256
                )
                return result
        raise KeyError(name)

    def promote_variant(self, pid: str, name: str) -> dict[str, Any]:
        state = self._load_state(pid)
        if state.draft is None:
            raise MusicApprovalRequired("save lyrics and musical direction first")
        variant = next((item for item in state.variants if item.name == name), None)
        if variant is None:
            raise KeyError(name)
        lyrics = _normalize_lyrics(state.draft.settings.lyrics)
        tags = parse_section_tags(lyrics)
        matches = [tag for tag in tags if tag[0] == variant.section]
        if not matches:
            raise MusicConflict(
                f"the current draft has no [{variant.section}] section")
        if variant.occurrence > len(matches):
            raise MusicConflict(
                f"the current draft has {len(matches)} [{variant.section}] section(s); "
                f"occurrence {variant.occurrence} does not exist")
        _, _, end = matches[variant.occurrence - 1]
        following = [tag for tag in tags if tag[1] > end]
        block_end = following[0][1] if following else len(lyrics)
        replacement = "\n" + variant.text + ("\n" if not following else "\n\n")
        new_lyrics = lyrics[:end] + replacement + lyrics[block_end:]
        new_settings = state.draft.settings.model_copy(update={"lyrics": new_lyrics})
        result = self.save_draft(
            pid,
            new_settings,
            revision_name=f"variant {variant.name}",
            source="promote",
        )
        result["promoted_variant"] = {
            "name": variant.name,
            "section": variant.section,
            "occurrence": variant.occurrence,
        }
        return result

    # --- runtime readiness ------------------------------------------
    def _validate_workflow(self, workflow: dict[str, Any], object_info: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for node_id, node in workflow.items():
            if node_id.startswith("_"):
                continue
            if not isinstance(node, dict):
                errors.append(f"{node_id}: node is not an object")
                continue
            class_type = node.get("class_type")
            if not class_type:
                errors.append(f"{node_id}: missing class_type")
                continue
            schema = object_info.get(class_type)
            if not schema:
                errors.append(f"{node_id}: unsupported node class {class_type}")
                continue
            inputs = node.get("inputs", {})
            required = schema.get("input", {}).get("required", {})
            for input_name, spec in required.items():
                if input_name not in inputs:
                    errors.append(f"{node_id}.{input_name}: required input missing")
                    continue
                value = inputs[input_name]
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    source_id, source_slot = value
                    source = workflow.get(source_id)
                    if not isinstance(source, dict):
                        errors.append(f"{node_id}.{input_name}: unknown source {source_id}")
                        continue
                    source_class = source.get("class_type")
                    source_schema = object_info.get(source_class, {})
                    outputs = source_schema.get("output", [])
                    if not isinstance(source_slot, int) or source_slot >= len(outputs):
                        errors.append(f"{node_id}.{input_name}: invalid source slot {source_slot}")
                    continue
                if isinstance(spec, list) and spec and isinstance(spec[0], list):
                    options = spec[0]
                    if options and isinstance(options[0], str) and value not in options:
                        errors.append(
                            f"{node_id}.{input_name}: value {value!r} is not in installed options")
        return errors

    async def runtime(self) -> dict[str, Any]:
        cfg = get_settings().music
        result: dict[str, Any] = {
            "engine": cfg.engine,
            "checkpoint": cfg.checkpoint,
            "checkpoint_sha256": cfg.checkpoint_sha256,
            "workflow": cfg.workflow,
            "live_generation_authorized": cfg.live_generation_authorized,
            "capacity_verified": cfg.capacity_verified,
            "capacity_override_authorized": cfg.capacity_override_authorized,
            "capacity_ready": bool(cfg.capacity_verified or cfg.capacity_override_authorized),
            "capacity_label": (
                "verified"
                if cfg.capacity_verified
                else "experimental override (not verified)"
                if cfg.capacity_override_authorized
                else "unverified"
            ),
            "comfyui_online": False,
            "checkpoint_available": False,
            "workflow_valid": False,
            "offline_ready": False,
            "live_ready": False,
            "errors": [],
            "ready": False,
        }
        try:
            status = await self.comfy.status()
        except Exception as exc:
            status = {"online": False, "error": str(exc)}
        result["comfyui_online"] = bool(status.get("online"))
        if not result["comfyui_online"]:
            result["errors"].append(status.get("error") or "ComfyUI is offline")
            return result
        result["comfyui"] = {
            "base_url": status.get("base_url"),
            "gpus": status.get("gpus", []),
        }
        try:
            object_info = await self.comfy.object_info()
        except Exception as exc:
            result["errors"].append(f"cannot read ComfyUI node schemas: {exc}")
            return result
        checkpoint_spec = (
            object_info.get("CheckpointLoaderSimple", {})
            .get("input", {})
            .get("required", {})
            .get("ckpt_name", [])
        )
        options = checkpoint_spec[0] if isinstance(checkpoint_spec, list) and checkpoint_spec else []
        result["checkpoint_available"] = isinstance(options, list) and cfg.checkpoint in options
        if not result["checkpoint_available"]:
            result["errors"].append(f"checkpoint is not visible to ComfyUI: {cfg.checkpoint}")
        workflow_path = WORKFLOWS_DIR / f"{cfg.workflow}.json"
        if not workflow_path.is_file():
            result["errors"].append(f"workflow file missing: {workflow_path}")
            return result
        try:
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        except Exception as exc:
            result["errors"].append(f"workflow is unreadable: {exc}")
            return result
        workflow_errors = self._validate_workflow(workflow, object_info)
        result["workflow_errors"] = workflow_errors
        result["workflow_valid"] = not workflow_errors
        result["errors"].extend(workflow_errors)
        result["ready"] = bool(
            result["comfyui_online"] and result["checkpoint_available"] and result["workflow_valid"])
        result["offline_ready"] = result["ready"]
        result["capacity_ready"] = bool(
            result["capacity_verified"] or result["capacity_override_authorized"])
        result["live_ready"] = bool(
            result["ready"] and result["capacity_ready"] and result["live_generation_authorized"])
        return result

    # --- generation --------------------------------------------------
    def _patches(self, pid: str, attempt_id: str, settings: MusicSettings) -> dict[str, dict[str, Any]]:
        lyrics = _normalize_lyrics(settings.lyrics)
        node3: dict[str, Any] = {
            "tags": _effective_tags(settings),
            "lyrics": lyrics,
            "seed": settings.seed,
            "bpm": settings.bpm,
            "duration": settings.duration_s,
            "timesignature": settings.time_signature,
            "language": settings.language,
            "keyscale": settings.key_scale,
            "generate_audio_codes": settings.generate_audio_codes,
        }
        if settings.temperature is not None:
            node3["temperature"] = settings.temperature
        if settings.top_p is not None:
            node3["top_p"] = settings.top_p
        if settings.cfg_scale is not None:
            node3["cfg_scale"] = settings.cfg_scale
        return {
            "3": node3,
            "5": {"seconds": settings.duration_s},
            "6": {"seed": settings.seed},
            "8": {"filename_prefix": f"music/{pid}-{attempt_id}"},
        }

    def _execution_dir(self, pid: str) -> Path:
        directory = self._state_dir(pid) / "requests"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_exclusive_bytes(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise MusicConflict(f"execution record already exists: {path.name}")
        tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            with tmp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError as exc:
                raise MusicConflict(
                    f"execution record already exists: {path.name}") from exc
        finally:
            tmp.unlink(missing_ok=True)

    def _write_exclusive_json(self, path: Path, payload: Any) -> None:
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._write_exclusive_bytes(path, encoded)

    def _copy_exclusive(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise MusicConflict(
                f"execution record already exists: {destination.name}")
        tmp = destination.with_name(f"{destination.name}.tmp-{uuid.uuid4().hex}")
        try:
            with source.open("rb") as src, tmp.open("xb") as out:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(tmp, destination)
            except FileExistsError as exc:
                raise MusicConflict(
                    f"execution record already exists: {destination.name}") from exc
        finally:
            tmp.unlink(missing_ok=True)

    def _execution_file(self, pid: str, attempt_id: str, suffix: str) -> Path:
        return self._execution_dir(pid) / f"{attempt_id}{suffix}"

    def _append_execution_event(self, pid: str, attempt_id: str, kind: str,
                                detail: Any = None) -> None:
        path = self._execution_file(pid, attempt_id, ".events.jsonl")
        row = {
            "at": _utcnow().isoformat(),
            "kind": kind,
            "detail": detail,
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _probe_output_properties(self, data: bytes, suffix: str) -> dict[str, Any]:
        ffprobe = get_settings().ffmpeg.ffprobe_binary
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / f"output{suffix or '.bin'}"
                path.write_bytes(data)
                proc = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries",
                     "format=duration,format_name:stream=codec_type,sample_rate,channels",
                     "-of", "json", str(path)],
                    capture_output=True,
                )
            if proc.returncode != 0:
                return {"status": "unavailable",
                        "error": proc.stderr.decode("utf-8", "replace")[:500]}
            payload = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
            streams = payload.get("streams") or []
            audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
            fmt = payload.get("format") or {}
            duration = fmt.get("duration")
            return {
                "status": "measured",
                "duration_s": float(duration) if duration not in (None, "") else None,
                "format_name": fmt.get("format_name"),
                "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
                "channels": int(audio["channels"]) if audio.get("channels") else None,
                "codec_type": audio.get("codec_type"),
            }
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)[:500]}

    async def _capture_environment(self, workflow_name: str,
                                   settings: MusicSettings) -> dict[str, Any]:
        stats = None
        if hasattr(self.comfy, "system_stats"):
            try:
                stats = await self.comfy.system_stats()
            except Exception as exc:
                stats = {"error": str(exc)}
        status = None
        if stats is None:
            try:
                status = await self.comfy.status()
            except Exception as exc:
                status = {"online": False, "error": str(exc)}
        system = (stats or {}).get("system") or {}
        devices = (stats or {}).get("devices") or (status or {}).get("gpus") or []
        workflow = workflow_record(workflow_name)
        cfg = get_settings().music
        return {
            "captured_at": _utcnow().isoformat(),
            "comfyui": {
                "reachable": bool(system) or bool((status or {}).get("online")),
                "version": system.get("comfyui_version"),
                "version_basis": "freshly_verified_from_system_stats" if system.get("comfyui_version") else "unknown",
                "packages": system.get("comfy_package_versions") or [],
                "python_version": system.get("python_version"),
                "pytorch_version": system.get("pytorch_version"),
                "argv": system.get("argv"),
                "devices": devices,
                "system_stats_error": (stats or {}).get("error"),
            },
            "commit": {
                "value": None,
                "status": "unknown",
                "reason": "not exposed by /system_stats; no read-only install root was configured",
            },
            "dirty_status": {
                "value": None,
                "status": "unknown",
                "reason": "no read-only install root was configured",
            },
            "custom_nodes": {
                "status": "unknown",
                "items": [],
                "reason": "no read-only install root was configured",
            },
            "workflow": {
                "name": workflow_name,
                "path": workflow.get("path"),
                "version": workflow.get("version"),
                "sha256": workflow.get("sha256"),
                "basis": "freshly_verified_from_repository_template",
            },
            "model": {
                "checkpoint": cfg.checkpoint,
                "expected_sha256": cfg.checkpoint_sha256,
                "sha256": cfg.checkpoint_sha256,
                "basis": "previously_recorded_configuration_hash",
                "status": "previously_recorded",
                "verification_time": None,
                "freshly_verified": False,
                "loaded_bytes_verified": False,
                "filename_is_not_proof_of_loaded_bytes": True,
            },
            "evidence_status": {
                "comfyui_version": "freshly_verified" if system.get("comfyui_version") else "unknown",
                "comfyui_packages": "freshly_verified" if system.get("comfy_package_versions") else "unknown",
                "comfyui_commit": "unknown",
                "comfyui_dirty_status": "unknown",
                "custom_nodes": "unknown",
                "workflow_template": "freshly_verified",
                "model_hash": "previously_recorded",
                "devices": "freshly_verified" if devices else "unknown",
            },
            "runtime": {
                "base_url": get_settings().comfyui.base_url,
                "devices": devices,
                "source": "comfyui_system_stats" if stats else "comfyui_status_fallback",
            },
        }

    def _persist_request_environment(self, pid: str, attempt_id: str,
                                     environment: dict[str, Any]) -> dict[str, Any]:
        path = self._execution_file(pid, attempt_id, ".environment.json")
        self._write_exclusive_json(path, environment)
        return {
            "environment_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "environment_sha256": _sha256_bytes(path.read_bytes()),
        }

    def _persist_response(self, pid: str, attempt_id: str,
                          response: dict[str, Any]) -> dict[str, Any]:
        meta_path = self._execution_file(pid, attempt_id, ".response.json")
        if meta_path.exists():
            return {
                "response_path": str(meta_path.relative_to(self.store.root / pid)).replace("\\", "/"),
                "response_sha256": _sha256_bytes(meta_path.read_bytes()),
            }
        body = bytes(response.get("body") or b"")
        body_path = self._execution_file(pid, attempt_id, ".response.body")
        self._write_exclusive_bytes(body_path, body)
        headers = {
            key: value for key, value in (response.get("headers") or {}).items()
            if key.lower() not in {"set-cookie", "authorization"}
        }
        meta = {
            "status_code": response.get("status_code"),
            "headers": headers,
            "server_date": headers.get("date"),
            "client_observed_at": response.get("observed_at"),
            "prompt_id": response.get("prompt_id"),
            "client_id": response.get("client_id"),
            "body_sha256": _sha256_bytes(body),
            "body_path": str(body_path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "body_bytes": len(body),
            "json": response.get("json"),
        }
        self._write_exclusive_json(meta_path, meta)
        return {
            "response_path": str(meta_path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "response_sha256": _sha256_bytes(meta_path.read_bytes()),
            "response_body_path": meta["body_path"],
            "response_body_sha256": meta["body_sha256"],
        }

    def _persist_history(self, pid: str, attempt_id: str, result: Any,
                         *, complete: bool, source: str,
                         error: str | None = None) -> dict[str, Any]:
        path = self._execution_file(pid, attempt_id, ".history.json")
        if path.exists():
            return {
                "history_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
                "history_sha256": _sha256_bytes(path.read_bytes()),
                "history_complete": True,
            }
        payload = {
            "complete": complete,
            "source": source,
            "client_observed_at": _utcnow().isoformat(),
            "error": error,
            "result": result,
        }
        self._write_exclusive_json(path, payload)
        return {
            "history_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "history_sha256": _sha256_bytes(path.read_bytes()),
            "history_complete": complete,
        }

    def _persist_output(self, pid: str, attempt_id: str, asset: AssetRef,
                        data: bytes, filename: str, prompt_id: str) -> dict[str, Any]:
        path = self._execution_file(pid, attempt_id, ".output.json")
        if path.exists():
            return {
                "output_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
                "output_sha256": _sha256_bytes(path.read_bytes()),
            }
        suffix = Path(filename).suffix.lower()
        payload = {
            "asset_id": asset.id,
            "asset_relative_path": asset.relative_path,
            "filename": filename,
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
            "prompt_id": prompt_id,
            "client_observed_at": _utcnow().isoformat(),
            "measured_properties": self._probe_output_properties(data, suffix),
        }
        self._write_exclusive_json(path, payload)
        return {
            "output_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "output_sha256": _sha256_bytes(path.read_bytes()),
        }

    def _execution_links(self, pid: str, attempt_id: str) -> dict[str, Any]:
        links: dict[str, Any] = {}
        for key, suffix in [
            ("api_request_path", ".json"),
            ("api_request_body_path", ".body"),
            ("environment_path", ".environment.json"),
            ("response_path", ".response.json"),
            ("response_body_path", ".response.body"),
            ("history_path", ".history.json"),
            ("output_path", ".output.json"),
            ("events_path", ".events.jsonl"),
            ("outcome_path", ".outcome.json"),
        ]:
            path = self._execution_file(pid, attempt_id, suffix)
            if path.is_file():
                links[key] = str(path.relative_to(self.store.root / pid)).replace("\\", "/")
                links[key.replace("_path", "_sha256")] = _sha256_bytes(path.read_bytes())
        links["api_request_retained"] = bool(links.get("api_request_path"))
        for key, suffix in [
            ("native_request_path", ".native.request.json"),
            ("native_request_body_path", ".native.body"),
            ("native_environment_path", ".native.environment.json"),
            ("native_response_path", ".native.response.json"),
            ("native_response_body_path", ".native.response.body"),
            ("native_status_path", ".native.status.jsonl"),
            ("native_output_path", ".native.output.json"),
            ("native_output_body_path", ".native.output.body"),
            ("native_outcome_path", ".native.outcome.json"),
            ("native_events_path", ".native.events.jsonl"),
        ]:
            path = self._execution_file(pid, attempt_id, suffix)
            if path.is_file():
                links[key] = str(path.relative_to(self.store.root / pid)).replace("\\", "/")
                links[key.replace("_path", "_sha256")] = _sha256_bytes(path.read_bytes())
        conditioning = sorted(
            self._execution_dir(pid).glob(f"{attempt_id}.native.conditioning.*"))
        if conditioning:
            path = conditioning[0]
            links["native_conditioning_path"] = str(
                path.relative_to(self.store.root / pid)).replace("\\", "/")
            links["native_conditioning_sha256"] = _sha256_bytes(path.read_bytes())
        links["native_request_retained"] = bool(links.get("native_request_path"))
        return links

    def _native_file(self, pid: str, attempt_id: str, suffix: str) -> Path:
        return self._execution_file(pid, attempt_id, suffix)

    def _append_native_event(self, pid: str, attempt_id: str, kind: str,
                             detail: Any = None) -> None:
        path = self._native_file(pid, attempt_id, ".native.events.jsonl")
        row = {
            "at": _utcnow().isoformat(),
            "kind": kind,
            "detail": detail,
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _persist_native_conditioning(
        self, pid: str, attempt_id: str, source_path: Path, expected_sha256: str,
    ) -> dict[str, Any]:
        suffix = source_path.suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac", ".ogg", ".m4a"}:
            suffix = ".bin"
        final = self._native_file(
            pid, attempt_id, f".native.conditioning{suffix}")
        if final.exists():
            raise MusicConflict(
                f"native conditioning record already exists: {final.name}")
        tmp = final.with_name(f"{final.name}.tmp-{uuid.uuid4().hex}")
        digest = hashlib.sha256()
        total = 0
        try:
            with source_path.open("rb") as src, tmp.open("xb") as out:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
                    total += len(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise MusicConflict(
                    "conditioning source changed before capture: "
                    f"expected {expected_sha256}, got {actual}")
            try:
                os.link(tmp, final)
            except FileExistsError as exc:
                raise MusicConflict(
                    f"native conditioning record already exists: {final.name}") from exc
        finally:
            tmp.unlink(missing_ok=True)
        rate, channels, frames = _audio_info(final)
        return {
            "filename": source_path.name,
            "relative_path": str(final.relative_to(self.store.root / pid)).replace(
                "\\", "/"),
            "sha256": expected_sha256,
            "bytes": total,
            "duration_s": round(frames / rate, 6),
            "sample_rate": rate,
            "channels": channels,
            "content_type": (
                "audio/wav" if suffix == ".wav" else "audio/mpeg"),
            "exact": True,
        }

    def _native_environment(self, proposal: MusicRepaintProposalRecord) -> dict[str, Any]:
        repo = Path(__file__).resolve().parents[3]
        verification_path = repo / "ops" / "ace-repaint" / "model-verification.json"
        manifest_path = repo / "ops" / "ace-repaint" / "model-manifest.json"
        model_name = proposal.model or "acestep-v15-turbo"
        model_sha = None
        model_bytes = None
        verification_time = None
        evidence = []
        try:
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
            target = f"{model_name}/model.safetensors"
            row = next(
                (item for item in verification.get("files", [])
                 if item.get("path") == target), None)
            if row:
                model_sha = row.get("sha256")
                model_bytes = row.get("bytes")
                evidence.append(str(verification_path.relative_to(repo)).replace(
                    "\\", "/"))
        except Exception:
            verification = None
        commit = None
        fetched_at = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            commit = manifest.get("code_commit")
            fetched_at = manifest.get("fetched_at")
            evidence.append(str(manifest_path.relative_to(repo)).replace("\\", "/"))
        except Exception:
            manifest = None
        status = "previously_recorded" if model_sha else "unknown"
        return {
            "captured_at": _utcnow().isoformat(),
            "provider": {
                "name": NATIVE_PROVIDER,
                "kind": "native_ace_step_repaint",
                "service_url": get_settings().music.repaint_service_url,
                "contacted_by_capture": False,
                "reachability": "unknown",
            },
            "model": {
                "name": model_name,
                "configured": True,
                "path": f"S:/AI/ace-step-models/{model_name}/model.safetensors",
                "sha256": model_sha,
                "bytes": model_bytes,
                "basis": "previously_recorded_model_verification"
                    if model_sha else "unknown",
                "status": status,
                "verification_time": fetched_at,
                "freshly_verified": False,
                "loaded_bytes_verified": False,
                "filename_or_manifest_is_not_proof_of_loaded_bytes": True,
            },
            "code": {
                "repository": "S:/AI/ACE-Step-1.5",
                "commit": commit,
                "status": "previously_recorded" if commit else "unknown",
                "basis": "ops/ace-repaint/model-manifest.json" if commit else "unknown",
                "freshly_verified": False,
            },
            "runtime": {
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "process": "song_writer",
                "native_python_version": None,
                "torch_version": None,
                "cuda_version": None,
                "basis": "not contacted; native process runtime unknown",
            },
            "workflow": {
                "name": proposal.workflow,
                "output_format": proposal.output_format,
            },
            "evidence": evidence,
            "evidence_status": {
                "model": status,
                "code_commit": "previously_recorded" if commit else "unknown",
                "runtime": "unknown",
                "service_reachability": "unknown",
                "loaded_model_bytes": "unverified",
            },
        }

    def _persist_native_request(
        self, pid: str, attempt_id: str, prepared: NativePreparedRequest,
        *, request: RepaintRequest, proposal: MusicRepaintProposalRecord,
        environment: dict[str, Any], accounting: dict[str, Any],
        extended_proposal: dict[str, Any] | None = None,
    ) -> tuple[NativePreparedRequest, dict[str, Any], dict[str, Any]]:
        body_final = self._native_file(pid, attempt_id, ".native.body")
        if body_final.exists():
            raise MusicConflict(
                f"native request body already exists: {body_final.name}")
        try:
            os.link(prepared.body_path, body_final)
        except FileExistsError as exc:
            raise MusicConflict(
                f"native request body already exists: {body_final.name}") from exc
        finally:
            prepared.body_path.unlink(missing_ok=True)
        prepared = replace(prepared, body_path=body_final)
        source_path = request.source_path
        rate, channels, frames = _audio_info(source_path)
        conditioning = self._persist_native_conditioning(
            pid, attempt_id, source_path, prepared.source_sha256)
        env_path = self._native_file(pid, attempt_id, ".native.environment.json")
        self._write_exclusive_json(env_path, environment)
        meta_path = self._native_file(pid, attempt_id, ".native.request.json")
        record = {
            "schema": NATIVE_REQUEST_SCHEMA,
            "provider": NATIVE_PROVIDER,
            "provider_kind": "native_ace_step_repaint",
            "explicitly_not_comfyui": True,
            "request_id": attempt_id,
            "project_id": pid,
            "attempt_id": attempt_id,
            "source_revision": proposal.source_revision,
            "created_at": _utcnow().isoformat(),
            "endpoint": f"{get_settings().music.repaint_service_url.rstrip('/')}/release_task",
            "method": "POST",
            "poll_endpoint": f"{get_settings().music.repaint_service_url.rstrip('/')}/query_result",
            "cancellation": {
                "supported": False,
                "status": "unavailable",
                "reason": (
                    "The authoritative native ACE-Step API exposes no task "
                    "cancellation endpoint; timeout/transport loss remains uncertain."),
            },
            "content_type": prepared.content_type,
            "serialization": {
                "format": "multipart/form-data",
                "boundary": prepared.boundary,
                "line_ending": "CRLF",
                "field_order": list(prepared.field_order),
                "file_part_first": True,
                "frozen_before_dispatch": True,
                "rebuilt_for_dispatch": False,
            },
            "fields": prepared.form,
            "field_order": list(prepared.field_order),
            "source": {
                "project_id": pid,
                "asset_id": proposal.source_asset_id,
                "attempt_id": proposal.source_attempt_id,
                "revision": proposal.source_revision,
                "sha256": proposal.source_sha256,
                "filename": source_path.name,
                "relative_path": str(source_path.relative_to(
                    self.store.root / pid)).replace("\\", "/"),
                "duration_s": round(frames / rate, 6),
                "sample_rate": rate,
                "channels": channels,
            },
            "proposal": {
                "proposal_id": proposal.proposal_id,
                "fingerprint_sha256": self._repaint_fingerprint(proposal),
                "authorization_note": proposal.authorization_note,
                "start_s": proposal.start_s,
                "end_s": proposal.end_s,
                "start_sample": proposal.start_sample,
                "end_sample": proposal.end_sample,
                "model": proposal.model,
                "workflow": proposal.workflow,
                "output_format": proposal.output_format,
                "extended_bridge": extended_proposal,
            },
            "conditioning": {
                **conditioning,
                "file_field": prepared.file_field,
                "repaint_start_s": proposal.start_s,
                "repaint_end_s": proposal.end_s,
                "repaint_mode": prepared.form.get("repaint_mode"),
                "audio_cover_strength": prepared.form.get("audio_cover_strength"),
                "chunk_mask_mode": prepared.form.get("chunk_mask_mode"),
                "output_format": prepared.form.get("audio_format"),
                "source_mapping": {
                    "clip_start_s": 0.0,
                    "clip_end_s": round(frames / rate, 6),
                    "mask_start_s": proposal.start_s,
                    "mask_end_s": proposal.end_s,
                    "clip_offset_s": 0.0,
                    "conditioning_is_full_source": True,
                },
                "settings": proposal.settings.model_dump(mode="json"),
                "lyrics": prepared.form.get("lyrics"),
                "lyrics_sha256": _sha256_bytes(
                    str(prepared.form.get("lyrics") or "").encode("utf-8")),
            },
            "input_files": [{
                "field": prepared.file_field,
                "filename": prepared.filename,
                "content_type": prepared.file_content_type,
                "sha256": prepared.file_sha256,
                "bytes": prepared.file_bytes,
                "relative_path": conditioning["relative_path"],
            }],
            "missing_input_files": [],
            "request_body_path": str(body_final.relative_to(
                self.store.root / pid)).replace("\\", "/"),
            "request_body_sha256": prepared.body_sha256,
            "request_body_bytes": prepared.body_bytes,
            "environment": environment,
            "environment_path": str(env_path.relative_to(
                self.store.root / pid)).replace("\\", "/"),
            "accounting": accounting,
            "capture_status": "persisted_before_dispatch",
        }
        self._write_exclusive_json(meta_path, record)
        self._append_native_event(pid, attempt_id, "request_persisted", {
            "request_body_sha256": prepared.body_sha256,
            "conditioning_sha256": prepared.source_sha256,
        })
        return prepared, record, {
            "native_request_path": str(meta_path.relative_to(
                self.store.root / pid)).replace("\\", "/"),
            "native_request_sha256": _sha256_bytes(meta_path.read_bytes()),
            "native_request_body_path": record["request_body_path"],
            "native_request_body_sha256": prepared.body_sha256,
            "native_environment_path": record["environment_path"],
            "native_conditioning_path": conditioning["relative_path"],
            "native_conditioning_sha256": prepared.source_sha256,
            "native_request_retained": True,
        }

    def _persist_native_submission(
        self, pid: str, attempt_id: str, response: dict[str, Any],
        task_id: str | None,
    ) -> None:
        meta_path = self._native_file(pid, attempt_id, ".native.response.json")
        if meta_path.exists():
            return
        body = bytes(response.get("body") or b"")
        body_path = self._native_file(pid, attempt_id, ".native.response.body")
        self._write_exclusive_bytes(body_path, body)
        meta = {
            "observed_at": response.get("observed_at"),
            "url": response.get("url"),
            "status_code": response.get("status_code"),
            "headers": response.get("headers") or {},
            "body_sha256": _sha256_bytes(body),
            "body_path": str(body_path.relative_to(
                self.store.root / pid)).replace("\\", "/"),
            "body_bytes": len(body),
            "json": response.get("json"),
            "task_id": task_id,
        }
        self._write_exclusive_json(meta_path, meta)
        self._append_native_event(pid, attempt_id, "submission_response", {
            "status_code": meta["status_code"],
            "task_id": task_id,
            "body_sha256": meta["body_sha256"],
        })

    def _append_native_status(
        self, pid: str, attempt_id: str, response: dict[str, Any], row: Any,
    ) -> None:
        body = bytes(response.get("body") or b"")
        line = {
            "observed_at": response.get("observed_at"),
            "url": response.get("url"),
            "status_code": response.get("status_code"),
            "headers": response.get("headers") or {},
            "body_sha256": _sha256_bytes(body),
            "body_base64": base64.b64encode(body).decode("ascii"),
            "json": response.get("json"),
            "row": row,
        }
        path = self._native_file(pid, attempt_id, ".native.status.jsonl")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _persist_native_output(
        self, pid: str, attempt_id: str, response: dict[str, Any], url: str,
    ) -> None:
        meta_path = self._native_file(pid, attempt_id, ".native.output.json")
        if meta_path.exists():
            return
        body = bytes(response.get("body") or b"")
        body_path = self._native_file(pid, attempt_id, ".native.output.body")
        self._write_exclusive_bytes(body_path, body)
        suffix = Path(urlparse(url).path).suffix.lower()
        meta = {
            "url": url,
            "filename": Path(urlparse(url).path).name or None,
            "content_type": (response.get("headers") or {}).get("content-type"),
            "status_code": response.get("status_code"),
            "observed_at": response.get("observed_at"),
            "sha256": _sha256_bytes(body),
            "bytes": len(body),
            "body_path": str(body_path.relative_to(
                self.store.root / pid)).replace("\\", "/"),
            "measured_properties": self._probe_output_properties(
                body, suffix or ".bin"),
            "exact": True,
        }
        self._write_exclusive_json(meta_path, meta)
        self._append_native_event(pid, attempt_id, "output_downloaded", {
            "sha256": meta["sha256"],
            "bytes": meta["bytes"],
            "content_type": meta["content_type"],
        })

    def _persist_native_outcome(self, pid: str, attempt_id: str, status: str,
                                error: str | None = None) -> dict[str, Any]:
        path = self._native_file(pid, attempt_id, ".native.outcome.json")
        if path.exists():
            return {
                "native_outcome_path": str(path.relative_to(
                    self.store.root / pid)).replace("\\", "/"),
                "native_outcome_sha256": _sha256_bytes(path.read_bytes()),
            }
        payload = {
            "status": status,
            "observed_at": _utcnow().isoformat(),
            "error": error,
            "links": self._execution_links(pid, attempt_id),
        }
        self._write_exclusive_json(path, payload)
        return {
            "native_outcome_path": str(path.relative_to(
                self.store.root / pid)).replace("\\", "/"),
            "native_outcome_sha256": _sha256_bytes(path.read_bytes()),
        }

    def _mark_native_uncertain(self, pid: str, attempt_id: str, reason: str) -> None:
        state = self._load_state(pid)
        self._append_native_event(pid, attempt_id, "uncertain", {
            "reason": reason[:1000]})
        try:
            self._persist_native_outcome(pid, attempt_id, "uncertain", reason[:1000])
        except Exception:
            log.exception("failed to persist native uncertain outcome")
        self._append_journal(pid, {
            "kind": "attempt_uncertain",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "provider": NATIVE_PROVIDER,
            "reason": reason[:1000],
            **self._request_link(pid, attempt_id),
        })
        state.recovery_required = True
        state.recovery_reason = reason[:1000]
        self._save_state(pid, state)

    def _mark_native_failed(self, pid: str, attempt_id: str, error: str) -> None:
        self._append_native_event(pid, attempt_id, "failed", {
            "error": error[:1000]})
        try:
            self._persist_native_outcome(pid, attempt_id, "failed", error[:1000])
        except Exception:
            log.exception("failed to persist native failed outcome")
        self._append_journal(pid, {
            "kind": "attempt_failed",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "provider": NATIVE_PROVIDER,
            "error": error[:1000],
            **self._request_link(pid, attempt_id),
        })

    def _persist_outcome(self, pid: str, attempt_id: str, status: str,
                         error: str | None = None) -> dict[str, Any]:
        path = self._execution_file(pid, attempt_id, ".outcome.json")
        if path.exists():
            return {
                "outcome_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
                "outcome_sha256": _sha256_bytes(path.read_bytes()),
            }
        payload = {
            "status": status,
            "client_observed_at": _utcnow().isoformat(),
            "error": error,
            "links": self._execution_links(pid, attempt_id),
        }
        self._write_exclusive_json(path, payload)
        return {
            "outcome_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "outcome_sha256": _sha256_bytes(path.read_bytes()),
        }

    def _persist_api_request(self, pid: str, attempt_id: str, workflow_name: str,
                             patches: dict[str, dict[str, Any]],
                             settings: MusicSettings,
                             environment: dict[str, Any] | None = None,
                             source_revision: int | None = None) -> tuple[dict[str, Any], bytes]:
        """Persist the exact final ComfyUI /prompt request before dispatch."""
        template_path = WORKFLOWS_DIR / f"{workflow_name}.json"
        if not template_path.is_file():
            raise MusicConflict(f"workflow template is missing: {template_path}")
        template = json.loads(template_path.read_text(encoding="utf-8"))
        prompt = ComfyUIClient._patch_workflow(template, patches)
        client_id = str(uuid.uuid4())
        payload = {"prompt": prompt, "client_id": client_id}
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        prompt_sha = _sha256_bytes(
            json.dumps(prompt, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        patches_sha = _sha256_bytes(
            json.dumps(patches, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        workflow = workflow_record(workflow_name)
        generation_inputs = {
            "tags": prompt.get("3", {}).get("inputs", {}).get("tags"),
            "lyrics": settings.lyrics,
            "normalized_lyrics": _normalize_lyrics(settings.lyrics),
            "seed": settings.seed,
            "bpm": settings.bpm,
            "time_signature": settings.time_signature,
            "key_scale": settings.key_scale,
            "language": settings.language,
            "duration_s": settings.duration_s,
            "generate_audio_codes": settings.generate_audio_codes,
            "temperature": settings.temperature,
            "top_p": settings.top_p,
            "cfg_scale": settings.cfg_scale,
            "resolved_node3_inputs": prompt.get("3", {}).get("inputs"),
        }
        environment = environment or {}
        body_path = self._execution_file(pid, attempt_id, ".body")
        meta_path = self._execution_file(pid, attempt_id, ".json")
        if body_path.exists() or meta_path.exists():
            raise MusicConflict(
                f"API request {attempt_id} already exists; request records are immutable")
        self._write_exclusive_bytes(body_path, body)
        environment_link = {}
        if environment:
            environment_link = self._persist_request_environment(pid, attempt_id, environment)
        record = {
            "schema": "song_writer.comfyui_api_request.v2",
            "request_id": attempt_id,
            "project_id": pid,
            "attempt_id": attempt_id,
            "source_revision": source_revision,
            "created_at": _utcnow().isoformat(),
            "endpoint": "/prompt",
            "method": "POST",
            "serialization": {
                "format": "json",
                "ensure_ascii": False,
                "separators": [",", ":"],
                "allow_nan": False,
                "sort_keys": False,
                "order_preserved": True,
            },
            "workflow": {
                "name": workflow_name,
                "version": workflow.get("version"),
                "path": workflow.get("path"),
                "sha256": workflow.get("sha256"),
            },
            "applied_patches": patches,
            "applied_patches_sha256": patches_sha,
            "settings_sha256": _sha256_bytes(
                json.dumps(settings.model_dump(mode="json"), sort_keys=True).encode("utf-8")),
            "lyrics_sha256": _sha256_bytes(settings.lyrics.encode("utf-8")),
            "payload": payload,
            "request_body_path": str(body_path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "request_body_sha256": _sha256_bytes(body),
            "payload_sha256": _sha256_bytes(body),
            "prompt_sha256": prompt_sha,
            "generation_inputs": generation_inputs,
            "input_files": [],
            "missing_input_files": [],
            "environment": environment,
            "environment_path": environment_link.get("environment_path"),
            "captured_at": _utcnow().isoformat(),
        }
        self._write_exclusive_json(meta_path, record)
        self._append_execution_event(pid, attempt_id, "request_persisted", {
            "request_body_sha256": record["request_body_sha256"],
            "prompt_sha256": prompt_sha,
        })
        return {
            "api_request_id": attempt_id,
            "api_request_path": str(meta_path.relative_to(self.store.root / pid)).replace("\\", "/"),
            "api_request_sha256": _sha256_bytes(meta_path.read_bytes()),
            "api_request_prompt_sha256": prompt_sha,
            "api_request_body_sha256": record["request_body_sha256"],
            "api_request_body_path": record["request_body_path"],
            "api_request_client_id": client_id,
            "workflow_template_sha256": workflow.get("sha256"),
            "workflow_template_version": workflow.get("version"),
            "api_request_retained": True,
            **environment_link,
        }, body

    def _request_link(self, pid: str, attempt_id: str) -> dict[str, Any]:
        try:
            links = self._execution_links(pid, attempt_id)
        except Exception:
            return {}
        if links.get("api_request_path"):
            links["api_request_id"] = attempt_id
        return links

    def _mark_uncertain(self, pid: str, attempt_id: str, reason: str) -> None:
        state = self._load_state(pid)
        self._append_execution_event(pid, attempt_id, "uncertain", {"reason": reason[:1000]})
        try:
            self._persist_outcome(pid, attempt_id, "uncertain", reason[:1000])
        except Exception:
            log.exception("failed to persist uncertain outcome")
        self._append_journal(pid, {
            "kind": "attempt_uncertain",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "reason": reason[:1000],
            **self._request_link(pid, attempt_id),
        })
        if state.current_attempt is not None and state.current_attempt.id == attempt_id:
            state.current_attempt.status = "uncertain"
            state.current_attempt.completed_at = _utcnow()
            state.current_attempt.error = reason[:1000]
        state.recovery_required = True
        state.recovery_reason = reason[:1000]
        self._save_state(pid, state)
        try:
            # A timed-out/uncertain ComfyUI prompt may still occupy GPU1. Block
            # every app job (not just music) until an operator confirms recovery.
            self.router.hold(
                MODEL,
                attempt_id,
                reason=f"music attempt uncertain: {reason[:400]}",
                holder=f"music:{pid}",
            )
        except Exception:
            log.exception("failed to persist music capacity hold")

    def _mark_failed(self, pid: str, attempt_id: str, error: str) -> None:
        state = self._load_state(pid)
        self._append_execution_event(pid, attempt_id, "failed", {"error": error[:1000]})
        try:
            self._persist_outcome(pid, attempt_id, "failed", error[:1000])
        except Exception:
            log.exception("failed to persist failed outcome")
        self._append_journal(pid, {
            "kind": "attempt_failed",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "error": error[:1000],
            **self._request_link(pid, attempt_id),
        })
        if state.current_attempt is not None and state.current_attempt.id == attempt_id:
            state.current_attempt.status = "failed"
            state.current_attempt.completed_at = _utcnow()
            state.current_attempt.error = error[:1000]
        self._save_state(pid, state)

    async def generate(self, pid: str, plan_label: str | None = None) -> dict[str, Any]:
        settings = get_settings()
        state, journal, cap = self._ensure_ready(pid)
        runtime = await self.runtime()
        capacity_ready = bool(
            runtime.get("capacity_ready", runtime.get("capacity_verified")))
        if not runtime["ready"] or not capacity_ready:
            raise MusicRuntimeUnavailable(
                "music runtime is not ready, or live capacity is not verified and no "
                "experimental override is authorized: "
                + "; ".join(runtime.get("errors", [])), runtime)
        job_id = uuid.uuid4().hex[:10]
        try:
            # Music must never silently evict another shared-ComfyUI model.
            await self.router.acquire(MODEL, job_id, allow_unload=False)
        except CapacityHeld as exc:
            raise MusicConflict(
                "GPU1 capacity is held pending confirmed recovery. Verify the prior "
                "ComfyUI job stopped before using Recover. " + str(exc)) from exc
        except ModelSwitchRequired as exc:
            raise MusicConflict(
                f"GPU1 is currently assigned to {exc.prior_model}; Song Writer will not "
                "unload or switch it automatically. Finish or clear that model, or use a "
                "separately authorized capacity procedure.") from exc

        attempt_id = uuid.uuid4().hex[:10]
        event = asyncio.Event()
        self._cancel_events[attempt_id] = event
        started = False
        submitted = False
        terminal_result = False
        try:
            async with self._lock:
                state, journal, cap = self._ensure_ready(pid)
                draft = state.draft
                if draft is None:
                    raise MusicApprovalRequired("save lyrics and musical direction first")
                plan = list(state.approval.variant_plan) if state.approval else []
                executed = set(journal["plan_labels"])
                if plan_label is not None:
                    item = next((entry for entry in plan if entry.label == plan_label), None)
                    if item is None:
                        raise MusicConflict(
                            f"plan label {plan_label!r} is not part of the approved variant plan")
                    if plan_label in executed:
                        raise MusicConflict(
                            f"plan entry {plan_label!r} was already executed; "
                            "each plan entry runs once")
                    overrides = _plan_overrides(item)
                else:
                    remaining = [entry.label for entry in plan if entry.label not in executed]
                    if remaining:
                        raise MusicConflict(
                            "an approved variant plan is pending; pass plan_label for one of: "
                            + ", ".join(remaining))
                    overrides = {}
                effective = draft.settings.model_copy(update=overrides)
                patches = self._patches(pid, attempt_id, effective)
                environment = await self._capture_environment(
                    settings.music.workflow, effective)
                api_request, request_body = self._persist_api_request(
                    pid, attempt_id, settings.music.workflow, patches, effective,
                    environment=environment, source_revision=draft.version)
                request_payload = json.loads(request_body.decode("utf-8"))
                attempt = MusicAttemptRecord(
                    id=attempt_id,
                    status="running",
                    workflow=settings.music.workflow,
                    checkpoint=settings.music.checkpoint,
                    lyrics_sha256=draft.lyrics_sha256,
                    settings_sha256=draft.settings_sha256,
                    plan_label=plan_label,
                    revision=draft.version,
                )
                self._append_journal(pid, {
                    "kind": "attempt_started",
                    "attempt_id": attempt_id,
                    "at": _utcnow().isoformat(),
                    "lyrics_sha256": draft.lyrics_sha256,
                    "settings_sha256": draft.settings_sha256,
                    "submission_cap": cap,
                    "engine": settings.music.engine,
                    "checkpoint": settings.music.checkpoint,
                    "workflow": settings.music.workflow,
                    "plan_label": plan_label,
                    "revision": draft.version,
                    "revision_name": draft.revision_name,
                    "overrides": overrides,
                    "effective": {
                        "seed": effective.seed,
                        "bpm": effective.bpm,
                        "key_scale": effective.key_scale,
                        "temperature": effective.temperature,
                        "top_p": effective.top_p,
                        "cfg_scale": effective.cfg_scale,
                    },
                    "effective_settings": effective.model_dump(mode="json"),
                    **api_request,
                })
                started = True
                state.current_attempt = attempt
                state.audio_approval = None
                state.recovery_required = False
                state.recovery_reason = None
                self._save_state(pid, state)

            response_links = {}
            if hasattr(self.comfy, "submit_prompt_body"):
                submitted = True
                response = await self.comfy.submit_prompt_body(
                    request_body, client_id=request_payload["client_id"])
                response_links = self._persist_response(pid, attempt_id, response)
                api_request.update(response_links)
                self._append_execution_event(pid, attempt_id, "response_received", {
                    "status_code": response.get("status_code"),
                    "prompt_id": response.get("prompt_id"),
                })
                prompt_id = response.get("prompt_id")
                if response.get("status_code", 0) >= 400:
                    terminal_result = True
                    raise ComfyUIError(
                        f"ComfyUI rejected exact request with HTTP {response.get('status_code')}")
                if not prompt_id:
                    raise ComfyUIError("ComfyUI response did not include prompt_id")
            elif hasattr(self.comfy, "submit_music_graph"):
                prompt_id = await self.comfy.submit_music_graph(
                    request_payload["payload"]["prompt"],
                    client_id=request_payload["payload"]["client_id"])
                submitted = True
            else:
                prompt_id = await self.comfy.submit_music(
                    settings.music.workflow, patches)
                submitted = True
            async with self._lock:
                state = self._load_state(pid)
                if state.current_attempt is None or state.current_attempt.id != attempt_id:
                    raise MusicConflict("attempt state changed before ComfyUI submission")
                state.current_attempt.prompt_id = prompt_id
                self._save_state(pid, state)

            if event.is_set():
                await self.comfy.cancel_prompt(prompt_id)
                async with self._lock:
                    self._mark_uncertain(pid, attempt_id, "cancel requested before completion")
                raise MusicRecoveryRequired("music generation was cancelled; recovery is required")

            try:
                result = await self.comfy.wait_music(prompt_id, should_cancel=event.is_set)
            except Exception as exc:
                history = None
                if hasattr(self.comfy, "history"):
                    try:
                        history = await self.comfy.history(prompt_id)
                    except Exception:
                        history = None
                self._append_execution_event(pid, attempt_id, "wait_error", {
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                    "history_available": history is not None,
                })
                try:
                    self._persist_history(
                        pid, attempt_id, history, complete=history is not None,
                        source="readback_after_wait_error" if history is not None else "unavailable",
                        error=str(exc)[:1000])
                except Exception:
                    log.exception("failed to persist music readback history")
                raise
            terminal_result = True
            history_links = {}
            try:
                history_links = self._persist_history(
                    pid, attempt_id, result, complete=True, source="wait_music")
            except Exception as exc:
                log.exception("failed to persist music history")
                self._append_execution_event(pid, attempt_id, "history_persist_error", {
                    "error": str(exc)[:1000],
                })
            if event.is_set():
                async with self._lock:
                    self._mark_uncertain(pid, attempt_id, "cancel requested before result recording")
                raise MusicRecoveryRequired("music generation was cancelled; recovery is required")

            audio = await self.comfy.fetch_music(result, prompt_id=prompt_id)
            data = audio.get("bytes")
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise ComfyUIError("ComfyUI returned no audio bytes")

            suffix = Path(str(audio.get("filename", ""))).suffix.lower()
            suffix = suffix if suffix in {".mp3", ".wav", ".flac"} else ".mp3"
            filename = f"ace-step-{attempt_id}{suffix}"
            provenance = asset_provenance(
                prompt_id="music.generate",
                rendered_prompt=(
                    f"{_effective_tags(effective)}\n\n"
                    f"{_normalize_lyrics(effective.lyrics)}"
                ),
                models={
                    "checkpoint": settings.music.checkpoint,
                    "engine": settings.music.engine,
                },
                workflows=[settings.music.workflow],
            )
            data_bytes = bytes(data)
            audio_sha = _sha256_bytes(data_bytes)
            meta = {
                "provenance": provenance,
                "music": {
                    "engine": settings.music.engine,
                    "checkpoint": settings.music.checkpoint,
                    "checkpoint_sha256": settings.music.checkpoint_sha256,
                    "workflow": settings.music.workflow,
                    "attempt_id": attempt_id,
                    "api_request_path": api_request["api_request_path"],
                    "api_request_sha256": api_request["api_request_sha256"],
                    "api_request_prompt_sha256": api_request["api_request_prompt_sha256"],
                    "api_request_body_path": api_request.get("api_request_body_path"),
                    "api_request_body_sha256": api_request.get("api_request_body_sha256"),
                    "environment_path": api_request.get("environment_path"),
                    "response_path": response_links.get("response_path"),
                    "history_path": history_links.get("history_path"),
                    "workflow_template_sha256": api_request["workflow_template_sha256"],
                    "prompt_id": prompt_id,
                    "lyrics_sha256": draft.lyrics_sha256,
                    "settings_sha256": draft.settings_sha256,
                    "duration_s": effective.duration_s,
                    "seed": effective.seed,
                    "bpm": effective.bpm,
                    "time_signature": effective.time_signature,
                    "key_scale": effective.key_scale,
                    "language": effective.language,
                    "temperature": effective.temperature,
                    "top_p": effective.top_p,
                    "cfg_scale": effective.cfg_scale,
                    "plan_label": plan_label,
                    "revision": draft.version,
                    "revision_name": draft.revision_name,
                    "overrides": overrides,
                    "effective_settings": effective.model_dump(mode="json"),
                    "audio_sha256": audio_sha,
                    "approval": state.approval.model_dump(mode="json") if state.approval else None,
                },
            }
            async with self._lock:
                state = self._load_state(pid)
                current = state.current_attempt
                if current is None or current.id != attempt_id:
                    raise MusicConflict("attempt state changed before asset registration")
                if current.cancel_requested or event.is_set():
                    self._mark_uncertain(pid, attempt_id, "cancel requested during asset registration")
                    raise MusicRecoveryRequired("music generation was cancelled; recovery is required")
                asset = self.store.save_asset(
                    pid, AssetKind.music, filename, data_bytes, meta=meta)
                output_links = {}
                try:
                    output_links = self._persist_output(
                        pid, attempt_id, asset, data_bytes, filename, prompt_id)
                except Exception as exc:
                    log.exception("failed to persist music output record")
                    self._append_execution_event(pid, attempt_id, "output_record_error", {
                        "error": str(exc)[:1000],
                    })
                outcome_links = {}
                try:
                    outcome_links = self._persist_outcome(pid, attempt_id, "completed")
                except Exception as exc:
                    log.exception("failed to persist music outcome")
                    self._append_execution_event(pid, attempt_id, "outcome_record_error", {
                        "error": str(exc)[:1000],
                    })
                self._append_execution_event(pid, attempt_id, "completed", {
                    "asset_id": asset.id,
                    "audio_sha256": audio_sha,
                })
                execution_links = self._execution_links(pid, attempt_id)
                self._append_journal(pid, {
                    "kind": "attempt_completed",
                    "attempt_id": attempt_id,
                    "at": _utcnow().isoformat(),
                    "asset_id": asset.id,
                    "sha256": audio_sha,
                    "prompt_id": prompt_id,
                    **execution_links,
                })
                current.status = "completed"
                current.completed_at = _utcnow()
                current.asset_id = asset.id
                current.audio_sha256 = audio_sha
                self._save_state(pid, state)
            return await self.status(pid)
        except MusicRecoveryRequired:
            raise
        except asyncio.CancelledError:
            if started:
                async with self._lock:
                    self._mark_uncertain(pid, attempt_id, "music request task was cancelled")
            raise
        except Exception as exc:
            if started:
                async with self._lock:
                    current = self._load_state(pid).current_attempt
                    uncertain = bool(
                        current is not None
                        and current.id == attempt_id
                        and (current.cancel_requested or (submitted and not terminal_result))
                    )
                    if uncertain:
                        self._mark_uncertain(pid, attempt_id, f"{type(exc).__name__}: {exc}")
                        raise MusicRecoveryRequired(
                            "music generation timed out, was cancelled, or its terminal "
                            "state is uncertain") from exc
                    self._mark_failed(pid, attempt_id, f"{type(exc).__name__}: {exc}")
            if isinstance(exc, MusicError):
                raise
            raise MusicGenerationFailed(str(exc)) from exc
        finally:
            self._cancel_events.pop(attempt_id, None)
            try:
                await self.router.release(MODEL, job_id, unload=False)
            except Exception:
                log.exception("failed to release music capacity lease")

    async def cancel(self, pid: str) -> dict[str, Any]:
        async with self._lock:
            state = self._load_state(pid)
            current = state.current_attempt
            allowed = bool(
                current is not None
                and (
                    current.status == "running"
                    or (current.prompt_id and current.status in {"failed", "uncertain"})
                )
            )
            if not allowed:
                raise MusicConflict("no active or uncertain ComfyUI prompt to cancel")
            prior_status = current.status
            current.cancel_requested = True
            self._append_journal(pid, {
                "kind": "cancel_requested",
                "attempt_id": current.id,
                "at": _utcnow().isoformat(),
                "prior_status": prior_status,
            })
            self._save_state(pid, state)
            event = self._cancel_events.get(current.id)
            if event is not None:
                event.set()
            prompt_id = current.prompt_id
        cancel_result = await self.comfy.cancel_prompt(prompt_id) if prompt_id else None
        return {
            "attempt_id": current.id,
            "prompt_id": prompt_id,
            "prior_status": prior_status,
            "cancel_requested": True,
            "cancel_result": cancel_result,
        }

    async def recover(self, pid: str, confirm_stopped: bool) -> dict[str, Any]:
        if not confirm_stopped:
            raise MusicConflict("recovery requires confirm_stopped=true")
        async with self._lock:
            state = self._load_state(pid)
            journal = self._journal_state(pid)
            ids = sorted(set(journal["orphans"]) | set(journal["open_uncertain"]))
            if not ids and not state.recovery_required:
                raise MusicConflict("no uncertain music attempt requires recovery")
            self.router.clear_hold(MODEL, holder=f"music:{pid}")
            for attempt_id in ids:
                self._append_journal(pid, {
                    "kind": "recovery_confirmed",
                    "attempt_id": attempt_id,
                    "at": _utcnow().isoformat(),
                    "note": "operator confirmed the prior attempt is no longer running",
                })
            state.recovery_required = False
            state.recovery_reason = None
            if state.current_attempt is not None and state.current_attempt.id in ids:
                state.current_attempt.status = "recovered"
                state.current_attempt.completed_at = _utcnow()
            self._save_state(pid, state)
        return await self.status(pid)

    async def approve_audio(self, pid: str, asset_id: str, note: str) -> dict[str, Any]:
        if not note.strip():
            raise MusicConflict("audio approval note is required")
        async with self._lock:
            state = self._load_state(pid)
            current = state.current_attempt
            if current is None or current.status != "completed" or current.asset_id != asset_id:
                raise MusicConflict("only the latest completed music asset can be approved")
            if not self._approval_matches(state):
                raise MusicApprovalRequired("the lyrics/settings approval is stale")
            if current.lyrics_sha256 != state.approval.lyrics_sha256 or current.settings_sha256 != state.approval.settings_sha256:
                raise MusicApprovalRequired("the audio belongs to an older lyrics/settings version")
            try:
                ref = self.store.get_asset(pid, asset_id)
                path = self.store.asset_path(pid, asset_id)
            except KeyError as exc:
                raise MusicConflict("music asset not found") from exc
            if ref.kind != AssetKind.music or not path.is_file():
                raise MusicConflict("music asset file is missing")
            audio_sha = _sha256_bytes(path.read_bytes())
            if audio_sha != current.audio_sha256:
                raise MusicConflict("music asset bytes changed after generation")
            state.audio_approval = MusicAudioApprovalRecord(
                asset_id=asset_id,
                audio_sha256=audio_sha,
                note=note.strip(),
            )
            self._save_state(pid, state)
            self.store.update_asset_meta(
                pid, asset_id, music_audio_approval=state.audio_approval.model_dump(mode="json"))
        return await self.status(pid)

    # --- revisions, takes, and preferred selection ------------------
    def _started_rows(self, journal: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            row["attempt_id"]: row
            for row in journal["rows"]
            if row.get("kind") == "attempt_started"
        }

    def _revision_references(self, pid: str, state: MusicState) -> dict[int, set[str]]:
        reasons: dict[int, set[str]] = {}

        def add(version: int | None, why: str) -> None:
            if version is None:
                return
            reasons.setdefault(int(version), set()).add(why)

        journal = self._journal_state(pid)
        started = self._started_rows(journal)
        take_counts: dict[int, int] = {}
        for row in journal["rows"]:
            if row.get("kind") != "attempt_completed":
                continue
            revision = (started.get(row.get("attempt_id")) or {}).get("revision")
            if revision is not None:
                take_counts[revision] = take_counts.get(revision, 0) + 1
        for revision, count in take_counts.items():
            add(revision, f"{count} take{'s' if count > 1 else ''}")
        if state.preferred_take is not None:
            add(state.preferred_take.revision, "preferred take")
        if state.approval is not None:
            add(state.approval.draft_version, "approved draft")
        for record in state.revisions:
            add(record.parent_version, f"parent of v{record.version}")
        return reasons

    def revisions(self, pid: str) -> dict[str, Any]:
        state = self._load_state(pid)
        journal = self._journal_state(pid)
        started = self._started_rows(journal)
        take_counts: dict[int, int] = {}
        for row in journal["rows"]:
            if row.get("kind") != "attempt_completed":
                continue
            revision = (started.get(row.get("attempt_id")) or {}).get("revision")
            if revision is not None:
                take_counts[revision] = take_counts.get(revision, 0) + 1
        critique_counts: dict[int, int] = {}
        for record in state.critiques:
            critique_counts[record.draft_version] = critique_counts.get(record.draft_version, 0) + 1
        return {
            "current_version": state.draft.version if state.draft else None,
            "revisions": [
                {
                    "version": record.version,
                    "name": record.name,
                    "parent_version": record.parent_version,
                    "source": record.source,
                    "lyrics_sha256": record.lyrics_sha256,
                    "settings_sha256": record.settings_sha256,
                    "take_count": take_counts.get(record.version, 0),
                    "critique_count": critique_counts.get(record.version, 0),
                    "created_at": record.created_at.isoformat(),
                }
                for record in state.revisions
            ],
        }

    def revision_detail(self, pid: str, version: int) -> dict[str, Any]:
        state = self._load_state(pid)
        for record in state.revisions:
            if record.version == version:
                return record.model_dump(mode="json")
        raise KeyError(f"revision {version} not found")

    def restore_revision(self, pid: str, version: int) -> dict[str, Any]:
        state = self._load_state(pid)
        record = next(
            (item for item in state.revisions if item.version == version), None)
        if record is None:
            raise KeyError(f"revision {version} not found")
        result = self.save_draft(
            pid,
            record.settings,
            revision_name=f"restored from v{version}",
            source="restore",
        )
        result["restored_from"] = version
        return result

    def revision_compare(self, pid: str, from_version: int, to_version: int) -> dict[str, Any]:
        state = self._load_state(pid)
        records = {record.version: record for record in state.revisions}
        if from_version not in records:
            raise KeyError(f"revision {from_version} not found")
        if to_version not in records:
            raise KeyError(f"revision {to_version} not found")
        a, b = records[from_version], records[to_version]
        from_dump = a.settings.model_dump(mode="json")
        to_dump = b.settings.model_dump(mode="json")
        changes = []
        for field_name in MusicSettings.model_fields:
            if field_name == "lyrics":
                continue
            if from_dump[field_name] != to_dump[field_name]:
                changes.append({
                    "field": field_name,
                    "from": from_dump[field_name],
                    "to": to_dump[field_name],
                })
        from_lines = from_dump["lyrics"].splitlines()
        to_lines = to_dump["lyrics"].splitlines()
        lyrics_diff = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                None, from_lines, to_lines).get_opcodes():
            if tag == "equal":
                lyrics_diff.append({"op": "equal", "count": i2 - i1})
            else:
                lyrics_diff.append({
                    "op": tag,
                    "from_lines": from_lines[i1:i2],
                    "to_lines": to_lines[j1:j2],
                })
        return {
            "from": {
                "version": a.version,
                "name": a.name,
                "source": a.source,
                "created_at": a.created_at.isoformat(),
            },
            "to": {
                "version": b.version,
                "name": b.name,
                "source": b.source,
                "created_at": b.created_at.isoformat(),
            },
            "settings_changes": changes,
            "lyrics_diff": lyrics_diff,
        }

    def takes(self, pid: str) -> dict[str, Any]:
        state = self._load_state(pid)
        journal = self._journal_state(pid)
        started = self._started_rows(journal)
        critique_summaries: dict[int, list[dict[str, Any]]] = {}
        for record in state.critiques:
            critique_summaries.setdefault(record.draft_version, []).append({
                "critique_id": record.critique_id,
                "summary": record.summary,
            })
        rows = []
        for row in journal["rows"]:
            if row.get("kind") != "attempt_completed":
                continue
            st = started.get(row.get("attempt_id")) or {}
            asset_id = row.get("asset_id")
            revision = st.get("revision")
            legacy = "revision" not in st
            provenance = (
                {"basis": "journal snapshot (v3 schema)", "uncertain": False}
                if not legacy else
                {
                    "basis": "durable journal completion row (pre-v3 schema)",
                    "uncertain": True,
                    "detail": (
                        "this take predates revision linkage: no revision pointer or "
                        "effective-settings snapshot was recorded when it was generated, "
                        "and none is reconstructed here"),
                }
            )
            audio_approval = None
            if asset_id:
                try:
                    ref = self.store.get_asset(pid, asset_id)
                except KeyError:
                    ref = None
                if ref is not None:
                    audio_approval = ref.meta.get("music_audio_approval")
            rows.append({
                "attempt_id": row.get("attempt_id"),
                "asset_id": asset_id,
                "audio_sha256": row.get("sha256"),
                "prompt_id": row.get("prompt_id"),
                "created_at": row.get("at"),
                "legacy": legacy,
                "take_provenance": provenance,
                "revision": revision,
                "revision_name": st.get("revision_name") or "",
                "plan_label": st.get("plan_label"),
                "overrides": st.get("overrides") or {},
                "effective_settings": st.get("effective_settings"),
                "api_request_path": st.get("api_request_path"),
                "api_request_sha256": st.get("api_request_sha256"),
                "api_request_prompt_sha256": st.get("api_request_prompt_sha256"),
                "api_request_body_path": st.get("api_request_body_path"),
                "api_request_body_sha256": st.get("api_request_body_sha256"),
                "environment_path": st.get("environment_path"),
                "environment_sha256": st.get("environment_sha256"),
                "response_path": row.get("response_path"),
                "response_sha256": row.get("response_sha256"),
                "history_path": row.get("history_path"),
                "history_sha256": row.get("history_sha256"),
                "output_path": row.get("output_path"),
                "output_sha256": row.get("output_sha256"),
                "outcome_path": row.get("outcome_path"),
                "outcome_sha256": row.get("outcome_sha256"),
                "events_path": row.get("events_path"),
                "events_sha256": row.get("events_sha256"),
                "api_request_retained": bool(st.get("api_request_path")),
                "audio_approval": audio_approval,
                "preferred": bool(
                    state.preferred_take and state.preferred_take.asset_id == asset_id),
                "critiques": critique_summaries.get(revision, []),
            })
        return {
            "takes": rows,
            "preferred_take": (
                state.preferred_take.model_dump(mode="json")
                if state.preferred_take else None
            ),
        }

    def prefer_take(self, pid: str, asset_id: str, note: str) -> dict[str, Any]:
        note = note.strip()
        if not note:
            raise MusicConflict("preferred-take note is required")
        state = self._load_state(pid)
        journal = self._journal_state(pid)
        started = self._started_rows(journal)
        completed = {
            row.get("asset_id"): (row.get("attempt_id"), row.get("sha256"))
            for row in journal["rows"]
            if row.get("kind") == "attempt_completed" and row.get("asset_id")
        }
        if asset_id not in completed:
            raise MusicConflict("only a completed music take can be preferred")
        attempt_id, recorded_sha = completed[asset_id]
        try:
            ref = self.store.get_asset(pid, asset_id)
            path = self.store.asset_path(pid, asset_id)
        except KeyError as exc:
            raise MusicConflict("music asset not found") from exc
        if ref.kind != AssetKind.music or not path.is_file():
            raise MusicConflict("music asset file is missing")
        audio_sha = _sha256_bytes(path.read_bytes())
        if recorded_sha and audio_sha != recorded_sha:
            raise MusicConflict("music asset bytes changed after generation")
        record = MusicPreferredTakeRecord(
            asset_id=asset_id,
            attempt_id=attempt_id,
            audio_sha256=audio_sha,
            revision=(started.get(attempt_id) or {}).get("revision"),
            note=note,
        )
        state.preferred_take = record
        self._save_state(pid, state)
        self.store.update_asset_meta(
            pid, asset_id, music_preferred_take=record.model_dump(mode="json"))
        return record.model_dump(mode="json")

    def _model_switch_model(self) -> str | None:
        try:
            status = self.router.status()
        except Exception:
            return None
        slots = status.get("slots") or {}
        slot = slots.get("1") or {}
        current = slot.get("current_model")
        if current and current != MODEL:
            return str(current)
        return None

    async def status(self, pid: str) -> dict[str, Any]:
        state = self._load_state(pid)
        journal = self._journal_state(pid)
        cap = self._effective_cap(state)
        remaining = None if cap is None else max(0, cap - journal["submissions"])
        cfg = get_settings().music
        runtime = await self.runtime()
        capacity_ready = bool(
            runtime.get("capacity_ready", runtime.get("capacity_verified")))
        approved = self._approval_matches(state)
        prior_model = self._model_switch_model()
        capacity_hold = self.router.held(MODEL)
        blockers: list[dict[str, str]] = []
        if not cfg.live_generation_authorized:
            blockers.append({
                "code": "live_disabled",
                "message": "Live music generation is disabled in this process. Start the explicit music-enabled mode for an authorized run.",
            })
        if not runtime["ready"]:
            detail = "; ".join(runtime.get("errors", [])) or "ACE-Step is not ready"
            blockers.append({
                "code": "runtime_unavailable",
                "message": f"ACE-Step runtime is not ready: {detail}",
            })
        if not capacity_ready:
            blockers.append({
                "code": "capacity_unverified",
                "message": "Live capacity is not verified. Use an explicit experimental override or complete a capacity check before generating.",
            })
        if not runtime.get("live_ready") and not any(
            item["code"] in {"live_disabled", "runtime_unavailable", "capacity_unverified"}
            for item in blockers
        ):
            blockers.append({
                "code": "live_not_ready",
                "message": "Live generation is not ready for this process.",
            })
        if prior_model:
            blockers.append({
                "code": "model_switch_required",
                "message": (
                    f"GPU1 is currently assigned to {prior_model}. Song Writer will not "
                    "unload or switch it automatically. Finish or clear that model, or use a "
                    "separately authorized capacity procedure."
                ),
            })
        if state.draft is None:
            blockers.append({
                "code": "draft_missing",
                "message": "Save lyrics and musical settings first.",
            })
        elif state.approval is None or not approved:
            mismatch = self._approval_mismatch(state)
            blockers.append({
                "code": "approval_required",
                "message": (
                    f"{mismatch}; reapprove the exact saved lyrics/settings with a "
                    "finite submission cap."
                    if mismatch
                    else "Approve the exact saved lyrics/settings with a finite submission cap; "
                    "the current draft is missing approval or has changed since approval."
                ),
            })
        if journal["orphans"] or journal["open_uncertain"] or state.recovery_required or capacity_hold:
            recovery_reason = (
                state.recovery_reason
                or (capacity_hold or {}).get("reason")
                or "unresolved prior attempt"
            )
            blockers.append({
                "code": "recovery_required",
                "message": (
                    f"A prior attempt needs confirmed recovery: {recovery_reason}. "
                    "Verify the ComfyUI job stopped, then use Recover."
                ),
            })
        if cap is None and state.approval is not None:
            blockers.append({
                "code": "cap_missing",
                "message": "No lifetime submission cap is recorded; approve the exact draft with a finite cap.",
            })
        elif cap is not None and journal["submissions"] >= cap:
            blockers.append({
                "code": "cap_exhausted",
                "message": (
                    f"Lifetime submission cap reached ({journal['submissions']}/{cap}). "
                    "This project cannot submit again."
                ),
            })
        if state.current_attempt and state.current_attempt.status == "running":
            blockers.append({
                "code": "attempt_running",
                "message": "A music attempt is already running.",
            })
        can_generate = not blockers
        reason = blockers[0]["message"] if blockers else None
        return {
            "project_id": pid,
            "draft": state.draft.model_dump(mode="json") if state.draft else None,
            "approval": state.approval.model_dump(mode="json") if state.approval else None,
            "lifetime_cap": state.lifetime_cap,
            "effective_cap": cap,
            "cap_remaining": remaining,
            "current_attempt": state.current_attempt.model_dump(mode="json") if state.current_attempt else None,
            "audio_approval": state.audio_approval.model_dump(mode="json") if state.audio_approval else None,
            "preferred_take": (
                state.preferred_take.model_dump(mode="json")
                if state.preferred_take else None
            ),
            "recovery_required": state.recovery_required,
            "recovery_reason": state.recovery_reason,
            "capacity_hold": capacity_hold,
            "preflight": self._preflight(state.draft.settings) if state.draft else [],
            "critiques": [
                {
                    "critique_id": record.critique_id,
                    "draft_version": record.draft_version,
                    "lyrics_sha256": record.lyrics_sha256,
                    "settings_sha256": record.settings_sha256,
                    "summary": record.summary,
                    "key_candidates": list(record.key_candidates),
                    "refined_direction": record.refined_direction,
                    "suggestion_count": len(record.suggestions),
                    "created_at": record.created_at.isoformat(),
                }
                for record in state.critiques
            ],
            "variants": [
                {
                    "name": variant.name,
                    "section": variant.section,
                    "occurrence": variant.occurrence,
                    "note": variant.note,
                    "lyrics_sha256": variant.lyrics_sha256,
                    "stale": bool(
                        state.draft is not None
                        and variant.lyrics_sha256 != state.draft.lyrics_sha256
                    ),
                    "created_at": variant.created_at.isoformat(),
                }
                for variant in state.variants
            ],
            "executed_plan_labels": journal["plan_labels"],
            "journal": {
                "submissions": journal["submissions"],
                "completed": journal["completed"],
                "failed": journal["failed"],
                "uncertain": journal["uncertain"],
                "orphans": journal["orphans"],
            },
            "runtime": runtime,
            "assets": [
                asset.model_dump(mode="json")
                for asset in self.store.list_assets(pid, AssetKind.music)
            ],
            "blockers": blockers,
            "can_generate": can_generate,
            "blocked_reason": reason,
            "repaint": {
                "enabled": get_settings().music.repaint_enabled,
                "crossfade_ms": get_settings().music.repaint_crossfade_ms,
                "recovery_required": state.recovery_required,
                "recovery_reason": state.recovery_reason,
                "capacity_gate": self.repaint_capacity_gate(pid),
            },
        }

    # --- section-level repaint (offline integration) -----------------
    def _repaint_gate_path(self, pid: str) -> Path:
        return self._state_dir(pid) / "repaint-capacity-gate.json"

    def repaint_capacity_gate(self, pid: str) -> dict[str, Any] | None:
        path = self._repaint_gate_path(pid)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MusicConflict(f"repaint capacity gate is unreadable: {exc}") from exc

    def _write_repaint_gate(self, pid: str, record: dict[str, Any]) -> dict[str, Any]:
        path = self._repaint_gate_path(pid)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return record

    def propose_repaint_capacity_gate(
        self, pid: str, *, measured_free_vram_mib: int, gpu_index: int,
        competing_processes: list[str], note: str,
    ) -> dict[str, Any]:
        if measured_free_vram_mib <= 0:
            raise MusicConflict("capacity gate needs a measured free-VRAM value")
        if not note.strip():
            raise MusicConflict("capacity gate proposal requires an operator note")
        record = {
            "approved": False,
            "gpu_index": int(gpu_index),
            "measured_free_vram_mib": int(measured_free_vram_mib),
            "competing_processes": list(competing_processes),
            "note": note.strip(),
            "proposed_at": _utcnow().isoformat(),
        }
        return self._write_repaint_gate(pid, record)

    def approve_repaint_capacity_gate(
        self, pid: str, *, confirm: bool, note: str,
        measured_free_vram_mib: int | None = None,
    ) -> dict[str, Any]:
        if confirm is not True:
            raise MusicConflict("capacity gate approval requires explicit confirmation")
        record = self.repaint_capacity_gate(pid)
        if record is None:
            raise MusicConflict("propose the capacity gate before approving it")
        if record.get("approved"):
            raise MusicConflict("capacity gate is already approved")
        if not note.strip():
            raise MusicConflict("capacity gate approval requires an operator note")
        if measured_free_vram_mib is not None:
            record["measured_free_vram_mib"] = int(measured_free_vram_mib)
        record["approved"] = True
        record["approval_note"] = note.strip()
        record["approved_at"] = _utcnow().isoformat()
        return self._write_repaint_gate(pid, record)

    def _require_repaint_capacity(self, pid: str) -> dict[str, Any]:
        settings = get_settings()
        gate = self.repaint_capacity_gate(pid)
        if gate is None:
            raise MusicConflict("repaint capacity gate is not proposed; model load is not authorized")
        if not gate.get("approved"):
            raise MusicConflict("repaint capacity gate is closed; the first model load is not authorized")
        measured = int(gate.get("measured_free_vram_mib") or 0)
        if measured < settings.music.repaint_min_free_vram_mib:
            raise MusicConflict(
                f"measured free VRAM {measured} MiB is below the required "
                f"{settings.music.repaint_min_free_vram_mib} MiB")
        return gate

    def _completed_source_take(
        self, pid: str, asset_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        journal = self._journal_state(pid)
        started = self._started_rows(journal)
        row = next(
            (item for item in journal["rows"]
             if item.get("kind") == "attempt_completed" and item.get("asset_id") == asset_id),
            None,
        )
        if row is None:
            raise MusicConflict("repaint requires a completed source take")
        return row, started.get(row.get("attempt_id")) or {}

    def create_repaint_proposal(
        self, pid: str, *, source_asset_id: str, start_s: float, end_s: float,
        prompt: str, context_s: float = 3.0,
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise MusicConflict("repaint guidance is required")
        if start_s < 0 or end_s <= start_s:
            raise MusicConflict("edit interval must satisfy 0 <= start < end")
        row, started = self._completed_source_take(pid, source_asset_id)
        state = self._load_state(pid)
        revision = started.get("revision")
        if revision is None:
            raise MusicConflict("source take has no revision linkage; its settings are unknown")
        record = next((item for item in state.revisions if item.version == revision), None)
        if record is None:
            raise MusicConflict("source revision settings are unavailable (unknown)")
        path = self.store.asset_path(pid, source_asset_id)
        actual_sha = _sha256_bytes(path.read_bytes())
        if actual_sha != row.get("sha256"):
            raise MusicConflict("source take bytes do not match the recorded hash")
        duration = float(record.settings.duration_s)
        if end_s > duration:
            raise MusicConflict(f"edit interval exceeds the {duration:.3f}s source duration")
        sample_rate, channels, frames = _audio_info(path)
        start_sample = int(round(start_s * sample_rate))
        end_sample = int(round(end_s * sample_rate))
        if start_sample < 0 or end_sample <= start_sample or end_sample > frames:
            raise MusicConflict("edit interval is outside the decoded source")
        crossfade = int(round(get_settings().music.repaint_crossfade_ms * sample_rate / 1000))
        proposal = MusicRepaintProposalRecord(
            proposal_id=uuid.uuid4().hex[:12],
            source_asset_id=source_asset_id,
            source_attempt_id=str(row.get("attempt_id")),
            source_revision=int(revision),
            source_sha256=actual_sha,
            source_duration_s=duration,
            start_s=float(start_s),
            end_s=float(end_s),
            start_sample=start_sample,
            end_sample=end_sample,
            context_start_s=max(0.0, start_s - context_s),
            context_end_s=min(duration, end_s + context_s),
            prompt=prompt,
            settings=record.settings,
            lyrics_sha256=record.lyrics_sha256,
            settings_sha256=record.settings_sha256,
            provider="ace-step-1.5-repaint",
            model="acestep-v15-turbo",
            workflow=f"repaint:explicit:{get_settings().music.repaint_output_format}",
            output_format=get_settings().music.repaint_output_format,
        )
        state.repaint_proposals.append(proposal)
        self._save_state(pid, state)
        return {"proposal": proposal.model_dump(mode="json"), "crossfade_samples": crossfade}

    def repaint_proposals(self, pid: str) -> dict[str, Any]:
        state = self._load_state(pid)
        return {"proposals": [item.model_dump(mode="json") for item in state.repaint_proposals]}

    def _repaint_proposal(
        self, state: MusicState, proposal_id: str,
    ) -> MusicRepaintProposalRecord:
        proposal = next(
            (item for item in state.repaint_proposals if item.proposal_id == proposal_id),
            None,
        )
        if proposal is None:
            raise KeyError(f"repaint proposal {proposal_id} not found")
        return proposal

    def repaint_proposal(self, pid: str, proposal_id: str) -> dict[str, Any]:
        return self._repaint_proposal(self._load_state(pid), proposal_id).model_dump(mode="json")

    def _repaint_fingerprint(self, proposal: MusicRepaintProposalRecord) -> str:
        payload = {
            "source_asset_id": proposal.source_asset_id,
            "source_sha256": proposal.source_sha256,
            "source_revision": proposal.source_revision,
            "start_sample": proposal.start_sample,
            "end_sample": proposal.end_sample,
            "prompt": proposal.prompt,
            "lyrics_sha256": proposal.lyrics_sha256,
            "settings_sha256": proposal.settings_sha256,
            "provider": proposal.provider,
            "model": proposal.model,
            "workflow": proposal.workflow,
            "output_format": proposal.output_format,
        }
        return _sha256_bytes(
            json.dumps(payload, sort_keys=True).encode("utf-8"))

    def authorize_repaint(self, pid: str, proposal_id: str, note: str) -> dict[str, Any]:
        if not note.strip():
            raise MusicConflict("repaint authorization requires a note")
        state = self._load_state(pid)
        proposal = self._repaint_proposal(state, proposal_id)
        if proposal.status != "proposed":
            raise MusicConflict(
                f"repaint proposal is {proposal.status}; only a proposal can be authorized")
        proposal.status = "authorized"
        proposal.authorization_note = note.strip()
        proposal.authorized_fingerprint = self._repaint_fingerprint(proposal)
        proposal.authorized_at = _utcnow()
        self._save_state(pid, state)
        return proposal.model_dump(mode="json")

    def revise_repaint(
        self, pid: str, proposal_id: str, *, start_s: float, end_s: float,
        prompt: str, context_s: float = 3.0,
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise MusicConflict("repaint guidance is required")
        if start_s < 0 or end_s <= start_s:
            raise MusicConflict("edit interval must satisfy 0 <= start < end")
        state = self._load_state(pid)
        proposal = self._repaint_proposal(state, proposal_id)
        if proposal.status not in {"proposed", "authorized"}:
            raise MusicConflict(
                f"repaint proposal is {proposal.status}; it cannot be revised")
        path = self.store.asset_path(pid, proposal.source_asset_id)
        sample_rate, channels, frames = _audio_info(path)
        start_sample = int(round(start_s * sample_rate))
        end_sample = int(round(end_s * sample_rate))
        if end_sample > frames or start_sample < 0 or end_sample <= start_sample:
            raise MusicConflict("edit interval is outside the decoded source")
        proposal.start_s = float(start_s)
        proposal.end_s = float(end_s)
        proposal.start_sample = start_sample
        proposal.end_sample = end_sample
        proposal.context_start_s = max(0.0, start_s - context_s)
        proposal.context_end_s = min(proposal.source_duration_s, end_s + context_s)
        proposal.prompt = prompt
        proposal.status = "proposed"
        proposal.authorization_note = ""
        proposal.authorized_fingerprint = ""
        proposal.authorized_at = None
        proposal.candidate = None
        proposal.error = None
        self._save_state(pid, state)
        return proposal.model_dump(mode="json")

    def reject_repaint(self, pid: str, proposal_id: str, reason: str = "") -> dict[str, Any]:
        state = self._load_state(pid)
        proposal = self._repaint_proposal(state, proposal_id)
        if proposal.status in {"applied", "rejected"}:
            raise MusicConflict(f"repaint proposal is already {proposal.status}")
        proposal.status = "rejected"
        proposal.error = reason.strip() or None
        self._save_state(pid, state)
        return proposal.model_dump(mode="json")

    def _load_extended_bridge_plan(self, pid: str, plan_dir: Path) -> dict[str, Any]:
        from app.services.replacement_export import ExtendedBridgeExportService

        plan_dir = Path(plan_dir)
        plan_path = plan_dir / "plan.json"
        request_path = plan_dir / "native.request.json"
        body_path = plan_dir / "native.body"
        conditioning_path = plan_dir / "native.conditioning.wav"
        environment_path = plan_dir / "native.environment.json"
        for path in (plan_path, request_path, body_path,
                     conditioning_path, environment_path):
            if not path.is_file():
                raise MusicConflict(f"extended-Bridge plan record is missing: {path.name}")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
        if plan.get("status") != "planned_not_dispatched":
            raise MusicConflict("extended-Bridge plan is not a dry-run plan")
        if plan.get("project_id") != pid:
            raise MusicConflict("extended-Bridge plan project does not match")
        if plan.get("provider_contacted") is not False:
            raise MusicConflict("extended-Bridge plan contacted a provider")
        validator = ExtendedBridgeExportService(music=self)
        source = validator._validate_source(pid)
        proposal = validator._proposal()
        plan_source_sha = (
            plan["source"].get("source_sha256") or plan["source"].get("sha256"))
        if plan_source_sha != source["source_sha256"]:
            raise MusicConflict("extended-Bridge plan source hash is stale")
        if plan["source"]["attempt"] != "519bd4ccf1":
            raise MusicConflict("extended-Bridge plan source attempt is wrong")
        if plan["source"]["revision"] != 3:
            raise MusicConflict("extended-Bridge plan source revision is wrong")
        if plan["proposal"]["sha256"] != _sha256_bytes(
                validator.proposal_path.read_bytes()):
            raise MusicConflict("extended-Bridge proposal changed after planning")
        body_sha = _sha256_bytes(body_path.read_bytes())
        cond_sha = _sha256_bytes(conditioning_path.read_bytes())
        if body_sha != plan["native_request"]["body_sha256"]:
            raise MusicConflict("extended-Bridge body hash changed after planning")
        if body_sha != request["request_body_sha256"]:
            raise MusicConflict("extended-Bridge request metadata body hash mismatch")
        if cond_sha != plan["conditioning"]["sha256"]:
            raise MusicConflict("extended-Bridge conditioning hash changed after planning")
        if body_path.stat().st_size != plan["native_request"]["body_bytes"]:
            raise MusicConflict("extended-Bridge body size changed after planning")
        if request["provider_kind"] != "native_ace_step_repaint":
            raise MusicConflict("extended-Bridge request provider is wrong")
        if request["fields"]["lyrics"] != "\n".join(plan["lyrics"]):
            raise MusicConflict("extended-Bridge lyrics changed after planning")
        if len(plan["lyrics"]) != 8:
            raise MusicConflict("extended-Bridge plan must contain eight Bridge lines")
        if request["fields"]["prompt"] != plan["performance_guidance"]:
            raise MusicConflict("extended-Bridge guidance changed after planning")
        if request["conditioning"]["source_mapping"] != {
                "clip_start_s": 182.0, "clip_end_s": 236.0,
                "mask_start_s": 185.0, "mask_end_s": 233.0,
                "clip_offset_s": 182.0, "conditioning_is_full_source": False}:
            raise MusicConflict("extended-Bridge conditioning mapping is wrong")
        if request["conditioning"]["duration_s"] != 54.0:
            raise MusicConflict("extended-Bridge conditioning is not 54s")
        model = environment.get("model") or {}
        if model.get("sha256") != proposal["native_model_identity"]["native"]["sha256"]:
            raise MusicConflict("extended-Bridge native model identity is wrong")
        return {
            "plan": plan,
            "request": request,
            "environment": environment,
            "source": source,
            "proposal": proposal,
            "plan_dir": plan_dir,
            "plan_sha256": _sha256_bytes(plan_path.read_bytes()),
            "body_path": body_path,
            "body_sha256": body_sha,
            "conditioning_path": conditioning_path,
            "conditioning_sha256": cond_sha,
            "environment_path": environment_path,
        }

    def _extended_auth_payload(self, authorization: dict[str, Any]) -> bytes:
        payload = {key: value for key, value in authorization.items()
                   if key != "signature"}
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def _validate_extended_authorization(
        self, authorization: dict[str, Any], *, plan: dict[str, Any],
        plan_sha256: str, body_sha256: str, conditioning_sha256: str,
        source_sha256: str, journal: dict[str, Any], cap: int | None,
    ) -> None:
        secret = get_settings().acceptance.signing_secret
        if not secret:
            raise MusicNotAuthorized(
                "no live extended-Bridge signing secret is configured")
        if not isinstance(authorization, dict):
            raise MusicNotAuthorized("live authorization is missing")
        signature = authorization.get("signature")
        expected = hmac.new(
            secret.encode("utf-8"),
            self._extended_auth_payload(authorization),
            hashlib.sha256,
        ).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(
                signature, expected):
            raise MusicNotAuthorized("live authorization signature is invalid")
        if authorization.get("schema") != (
                "song_writer.extended_bridge_live_authorization.v1"):
            raise MusicNotAuthorized("live authorization schema is wrong")
        if authorization.get("one_shot") is not True:
            raise MusicNotAuthorized("live authorization must be one-shot")
        if authorization.get("no_apply") is not True:
            raise MusicNotAuthorized("live authorization must forbid apply")
        if authorization.get("project_id") != plan["project_id"]:
            raise MusicNotAuthorized("live authorization project is wrong")
        if authorization.get("plan_sha256") != plan_sha256:
            raise MusicNotAuthorized("live authorization plan hash is wrong")
        if authorization.get("request_body_sha256") != body_sha256:
            raise MusicNotAuthorized("live authorization body hash is wrong")
        if authorization.get("conditioning_sha256") != conditioning_sha256:
            raise MusicNotAuthorized("live authorization conditioning is wrong")
        if authorization.get("source_sha256") != source_sha256:
            raise MusicNotAuthorized("live authorization source is wrong")
        if authorization.get("source_asset_id") != "0f26194c08":
            raise MusicNotAuthorized("live authorization source asset is wrong")
        if authorization.get("source_attempt_id") != "519bd4ccf1":
            raise MusicNotAuthorized("live authorization source attempt is wrong")
        if authorization.get("source_revision") != 3:
            raise MusicNotAuthorized("live authorization source revision is wrong")
        if authorization.get("lyrics_sha256") != _sha256_bytes(
                "\n".join(plan["lyrics"]).encode("utf-8")):
            raise MusicNotAuthorized("live authorization lyrics binding is wrong")
        if authorization.get("guidance_sha256") != _sha256_bytes(
                plan["performance_guidance"].encode("utf-8")):
            raise MusicNotAuthorized("live authorization guidance binding is wrong")
        model_sha = (
            plan["native_request"].get("environment", {}).get("model", {}).get("sha256")
            if isinstance(plan["native_request"].get("environment"), dict) else None
        )
        if model_sha is None:
            model_sha = get_settings().music.checkpoint_sha256
        if authorization.get("model_sha256") != (
                "3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7"):
            raise MusicNotAuthorized("live authorization native model is wrong")
        limits = authorization.get("limits") or {}
        if limits != {"startups": 1, "model_loads": 1,
                      "submissions": 1, "retries": 0}:
            raise MusicNotAuthorized("live authorization limits are not exactly one-shot")
        if cap is None:
            raise MusicNotAuthorized("no existing lifetime allowance is available")
        allowance = authorization.get("allowance") or {}
        if allowance != {
            "source": "project_lifetime_cap",
            "cap": cap,
            "prior_submissions": journal["submissions"],
            "submission_number": journal["submissions"] + 1,
        }:
            raise MusicNotAuthorized(
                "live authorization allowance does not match the shared journal")
        try:
            issued = datetime.fromisoformat(
                str(authorization["issued_at"]).replace("Z", "+00:00"))
            expires = datetime.fromisoformat(
                str(authorization["expires_at"]).replace("Z", "+00:00"))
        except Exception as exc:
            raise MusicNotAuthorized("live authorization timestamps are invalid") from exc
        now = datetime.now(timezone.utc)
        if issued > now or expires <= now:
            raise MusicNotAuthorized("live authorization is not currently valid")
        if not authorization.get("authorization_id"):
            raise MusicNotAuthorized("live authorization ID is missing")
        if not authorization.get("launch_id"):
            raise MusicNotAuthorized("live authorization launch ID is missing")

    def claim_extended_bridge_launch(
        self, pid: str, plan_dir: Path, authorization: dict[str, Any],
    ) -> dict[str, Any]:
        with self._project_lock(pid):
            state = self._load_state(pid)
            if state.recovery_required:
                raise MusicRecoveryRequired("recovery is required before launch")
            if not get_settings().music.repaint_enabled:
                raise MusicNotAuthorized("repaint live gate is disabled")
            self._require_repaint_capacity(pid)
            journal = self._journal_state(pid)
            if journal["orphans"] or journal["open_uncertain"]:
                raise MusicRecoveryRequired("an unresolved prior attempt blocks launch")
            cap = self._effective_cap(state)
            loaded = self._load_extended_bridge_plan(pid, plan_dir)
            self._validate_extended_authorization(
                authorization, plan=loaded["plan"], plan_sha256=loaded["plan_sha256"],
                body_sha256=loaded["body_sha256"],
                conditioning_sha256=loaded["conditioning_sha256"],
                source_sha256=loaded["source"]["source_sha256"],
                journal=journal, cap=cap)
            launch_id = authorization["launch_id"]
            path = self._state_dir(pid) / "extended-bridge" / "launches" / f"{launch_id}.json"
            if path.exists():
                raise MusicConflict("launch authorization was already claimed")
            self._write_exclusive_json(path, {
                "schema": "song_writer.extended_bridge_launch_claim.v1",
                "launch_id": launch_id,
                "authorization_id": authorization["authorization_id"],
                "project_id": pid,
                "plan_sha256": loaded["plan_sha256"],
                "request_body_sha256": loaded["body_sha256"],
                "claimed_at": _utcnow().isoformat(),
                "startups": 1,
                "model_loads": 1,
                "submissions": 1,
                "retries": 0,
                "music_submission_consumed": False,
            })
            return {
                "launch_id": launch_id,
                "authorization_id": authorization["authorization_id"],
                "claim_path": str(path.relative_to(self.store.root / pid)).replace("\\", "/"),
                "plan_sha256": loaded["plan_sha256"],
            }

    def begin_extended_bridge_live(
        self, pid: str, plan_dir: Path, authorization: dict[str, Any],
    ) -> dict[str, Any]:
        with self._project_lock(pid):
            state = self._load_state(pid)
            if state.recovery_required:
                raise MusicRecoveryRequired("recovery is required before submission")
            if not get_settings().music.repaint_enabled:
                raise MusicNotAuthorized("repaint live gate is disabled")
            self._require_repaint_capacity(pid)
            journal = self._journal_state(pid)
            if journal["orphans"] or journal["open_uncertain"]:
                raise MusicRecoveryRequired("an unresolved prior attempt blocks submission")
            cap = self._effective_cap(state)
            loaded = self._load_extended_bridge_plan(pid, plan_dir)
            self._validate_extended_authorization(
                authorization, plan=loaded["plan"], plan_sha256=loaded["plan_sha256"],
                body_sha256=loaded["body_sha256"],
                conditioning_sha256=loaded["conditioning_sha256"],
                source_sha256=loaded["source"]["source_sha256"],
                journal=journal, cap=cap)
            launch_id = authorization["launch_id"]
            launch_path = self._state_dir(pid) / "extended-bridge" / "launches" / f"{launch_id}.json"
            if not launch_path.is_file():
                raise MusicNotAuthorized("launch authorization was not claimed")
            launch_claim = json.loads(launch_path.read_text(encoding="utf-8"))
            if launch_claim.get("plan_sha256") != loaded["plan_sha256"]:
                raise MusicNotAuthorized("launch claim is bound to another plan")
            if launch_claim.get("authorization_id") != authorization["authorization_id"]:
                raise MusicNotAuthorized("launch claim is bound to another authorization")
            dispatch_path = (
                self._state_dir(pid) / "extended-bridge" / "dispatches"
                / f"{authorization['authorization_id']}.json")
            if dispatch_path.exists():
                raise MusicNotAuthorized("live authorization was already dispatched")
            attempt_id = uuid.uuid4().hex[:12]
            body_final = self._native_file(pid, attempt_id, ".native.body")
            cond_final = self._native_file(
                pid, attempt_id, ".native.conditioning.wav")
            env_final = self._native_file(
                pid, attempt_id, ".native.environment.json")
            try:
                self._copy_exclusive(loaded["body_path"], body_final)
                self._copy_exclusive(loaded["conditioning_path"], cond_final)
                self._write_exclusive_bytes(
                    env_final, json.dumps(
                        loaded["environment"], indent=2, sort_keys=True).encode("utf-8"))
                record = json.loads(json.dumps(loaded["request"]))
                record["request_id"] = attempt_id
                record["attempt_id"] = attempt_id
                record["capture_status"] = "persisted_before_dispatch"
                record["plan_sha256"] = loaded["plan_sha256"]
                record["authorization"] = {
                    "authorization_id": authorization["authorization_id"],
                    "launch_id": launch_id,
                    "signature_verified": True,
                    "one_shot": True,
                }
                record["accounting"] = {
                    "source": "project_lifetime_cap",
                    "cap_redacted": True,
                    "prior_submissions": journal["submissions"],
                    "submission_number": journal["submissions"] + 1,
                    "consumed_on_attempt_started": True,
                }
                record["request_body_path"] = str(
                    body_final.relative_to(self.store.root / pid)).replace("\\", "/")
                record["environment_path"] = str(
                    env_final.relative_to(self.store.root / pid)).replace("\\", "/")
                record["conditioning"]["relative_path"] = str(
                    cond_final.relative_to(self.store.root / pid)).replace("\\", "/")
                record["input_files"][0]["relative_path"] = record["conditioning"]["relative_path"]
                meta_path = self._native_file(pid, attempt_id, ".native.request.json")
                self._write_exclusive_json(meta_path, record)
                self._write_exclusive_json(dispatch_path, {
                    "schema": "song_writer.extended_bridge_dispatch_claim.v1",
                    "authorization_id": authorization["authorization_id"],
                    "launch_id": launch_id,
                    "attempt_id": attempt_id,
                    "plan_sha256": loaded["plan_sha256"],
                    "claimed_at": _utcnow().isoformat(),
                    "one_shot": True,
                })
            except Exception as exc:
                raise MusicConflict(
                    f"extended-Bridge record persistence failed; dispatch blocked: {exc}") from exc
            self._append_native_event(pid, attempt_id, "request_persisted", {
                "request_body_sha256": loaded["body_sha256"],
                "conditioning_sha256": loaded["conditioning_sha256"],
                "plan_sha256": loaded["plan_sha256"],
            })
            links = self._request_link(pid, attempt_id)
            self._append_journal(pid, {
                "kind": "attempt_started",
                "attempt_id": attempt_id,
                "at": _utcnow().isoformat(),
                "provider": NATIVE_PROVIDER,
                "engine": "ace-step-1.5",
                "task": "extended_bridge",
                "plan_id": loaded["plan"]["plan_id"],
                "plan_sha256": loaded["plan_sha256"],
                "authorization_id": authorization["authorization_id"],
                "launch_id": launch_id,
                "source_asset_id": "0f26194c08",
                "source_attempt_id": "519bd4ccf1",
                "source_sha256": loaded["source"]["source_sha256"],
                "source_revision": 3,
                "conditioning_sha256": loaded["conditioning_sha256"],
                "request_body_sha256": loaded["body_sha256"],
                "submission_cap": cap,
                "prior_submissions": journal["submissions"],
                "submission_number": journal["submissions"] + 1,
                "effective_settings": loaded["request"]["conditioning"].get("settings"),
                **links,
            })
            prepared = NativePreparedRequest(
                body_path=body_final,
                content_type=loaded["request"]["content_type"],
                boundary=loaded["request"]["serialization"]["boundary"],
                filename=loaded["request"]["input_files"][0]["filename"],
                file_field=loaded["request"]["input_files"][0]["field"],
                file_content_type=loaded["request"]["input_files"][0]["content_type"],
                file_sha256=loaded["request"]["input_files"][0]["sha256"],
                file_bytes=loaded["request"]["input_files"][0]["bytes"],
                form=loaded["request"]["fields"],
                field_order=tuple(loaded["request"]["field_order"]),
                body_sha256=loaded["body_sha256"],
                body_bytes=body_final.stat().st_size,
                source_sha256=loaded["conditioning_sha256"],
                source_bytes=cond_final.stat().st_size,
            )
            return {
                "pid": pid,
                "attempt_id": attempt_id,
                "plan_dir": str(plan_dir),
                "plan_sha256": loaded["plan_sha256"],
                "prepared": prepared,
                "links": self._request_link(pid, attempt_id),
                "authorization_id": authorization["authorization_id"],
                "launch_id": launch_id,
            }

    def submit_repaint(
        self, pid: str, proposal_id: str, provider: RepaintProvider | None = None,
    ) -> dict[str, Any]:
        with self._project_lock(pid):
            return self._submit_repaint_locked(pid, proposal_id, provider)

    def _submit_repaint_locked(
        self, pid: str, proposal_id: str, provider: RepaintProvider | None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if not settings.music.repaint_enabled:
            raise MusicNotAuthorized("repaint submissions are disabled in this process")
        state = self._load_state(pid)
        proposal = self._repaint_proposal(state, proposal_id)
        if state.recovery_required:
            raise MusicConflict(
                "repaint recovery is required; confirm the provider stopped before "
                "further submissions")
        if proposal.status != "authorized":
            raise MusicConflict(
                f"repaint proposal is {proposal.status}; explicit authorization is required")
        if proposal.authorized_fingerprint != self._repaint_fingerprint(proposal):
            raise MusicConflict(
                "repaint authorization is invalid: a bound input changed after authorization")
        self._require_repaint_capacity(pid)
        journal = self._journal_state(pid)
        cap = self._effective_cap(state)
        if cap is not None and journal["submissions"] >= cap:
            raise MusicConflict(
                f"lifetime submission cap reached ({journal['submissions']}/{cap})")
        source_path = self.store.asset_path(pid, proposal.source_asset_id)
        actual_source_sha = _sha256_bytes(source_path.read_bytes())
        if actual_source_sha != proposal.source_sha256:
            raise MusicConflict(
                "repaint source bytes changed after authorization: "
                f"expected {proposal.source_sha256}, got {actual_source_sha}")
        request = RepaintRequest(
            source_path=source_path,
            source_sha256=proposal.source_sha256,
            start_s=proposal.start_s,
            end_s=proposal.end_s,
            prompt=proposal.prompt,
            settings=proposal.settings.model_dump(mode="json"),
            output_format=proposal.output_format,
            lyrics=proposal.settings.lyrics,
            seed=proposal.settings.seed,
            bpm=proposal.settings.bpm,
            key_scale=proposal.settings.key_scale,
            time_signature=proposal.settings.time_signature,
            language=proposal.settings.language,
        )
        attempt_id = uuid.uuid4().hex[:12]
        active = provider or (
            DeterministicRepaintProvider()
            if settings.music.repaint_fake_provider
            else AceRepaintClient(
                settings.music.repaint_service_url,
                timeout_s=settings.ffmpeg.process_timeout_s,
            )
        )
        if isinstance(active, AceRepaintClient):
            return self._submit_native_repaint(
                pid, proposal_id, proposal, state, journal, cap, attempt_id,
                request, active)
        return self._submit_legacy_repaint(
            pid, proposal_id, proposal, state, cap, attempt_id, request, active)

    def _submit_native_repaint(
        self, pid: str, proposal_id: str, proposal: MusicRepaintProposalRecord,
        state: MusicState, journal: dict[str, Any], cap: int | None,
        attempt_id: str, request: RepaintRequest, active: AceRepaintClient,
    ) -> dict[str, Any]:
        temporary_body = self._execution_file(
            pid, attempt_id, f".native.body.tmp-{uuid.uuid4().hex}")
        try:
            prepared = build_native_multipart(request, temporary_body)
        except Exception as exc:
            temporary_body.unlink(missing_ok=True)
            raise MusicConflict(
                f"native request could not be frozen before dispatch: {exc}") from exc
        environment = self._native_environment(proposal)
        accounting = {
            "attempt_id": attempt_id,
            "prior_submissions": journal["submissions"],
            "attempt_started_before_dispatch": True,
            "allowance_source": "existing_music_journal",
            "allowance_may_be_consumed_after_attempt_started": True,
        }
        try:
            prepared, request_record, links = self._persist_native_request(
                pid, attempt_id, prepared, request=request, proposal=proposal,
                environment=environment, accounting=accounting)
        except Exception as exc:
            prepared.body_path.unlink(missing_ok=True)
            raise MusicConflict(
                f"native request persistence failed; dispatch blocked: {exc}") from exc
        self._append_journal(pid, {
            "kind": "attempt_started",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "provider": NATIVE_PROVIDER,
            "engine": "ace-step-1.5",
            "checkpoint": proposal.model,
            "workflow": proposal.workflow,
            "task": "repaint",
            "revision": proposal.source_revision,
            "revision_name": f"repaint {proposal.proposal_id[:6]}",
            "prompt": proposal.prompt,
            "source_sha256": proposal.source_sha256,
            "start_sample": proposal.start_sample,
            "end_sample": proposal.end_sample,
            "submission_cap": cap,
            "effective_settings": proposal.settings.model_dump(mode="json"),
            **links,
        })
        proposal.status = "submitted"
        proposal.attempt_id = attempt_id
        self._save_state(pid, state)
        recorder = _NativeCaptureRecorder(self, pid, attempt_id)
        try:
            execution = active.execute_prepared(prepared, recorder=recorder)
        except (NativeRepaintTimeout, httpx.TimeoutException,
                httpx.TransportError) as exc:
            reason = (
                "native repaint timeout/transport loss; provider stop is not "
                f"proven: {exc}")
            self._mark_native_uncertain(pid, attempt_id, reason)
            state = self._load_state(pid)
            held = self._repaint_proposal(state, proposal_id)
            held.status = "uncertain"
            held.error = reason[:1000]
            self._save_state(pid, state)
            raise MusicConflict(
                "native repaint did not return a confirmed result; an uncertainty "
                "hold blocks further submissions until recovery is confirmed") from exc
        except httpx.HTTPStatusError as exc:
            reason = f"native submission failed with HTTP {exc.response.status_code}"
            self._mark_native_failed(pid, attempt_id, reason)
            state = self._load_state(pid)
            failed = self._repaint_proposal(state, proposal_id)
            failed.status = "failed"
            failed.error = reason
            self._save_state(pid, state)
            raise MusicConflict(reason) from exc
        except NativeRepaintFailed as exc:
            self._mark_native_failed(pid, attempt_id, str(exc))
            state = self._load_state(pid)
            failed = self._repaint_proposal(state, proposal_id)
            failed.status = "failed"
            failed.error = str(exc)[:1000]
            self._save_state(pid, state)
            raise MusicConflict(f"native repaint submission failed: {exc}") from exc
        except Exception as exc:
            reason = f"native post-dispatch recording failure: {exc}"
            if recorder.dispatched:
                self._mark_native_uncertain(pid, attempt_id, reason)
                state = self._load_state(pid)
                held = self._repaint_proposal(state, proposal_id)
                held.status = "uncertain"
                held.error = reason[:1000]
                self._save_state(pid, state)
                raise MusicConflict(
                    "native repaint was dispatched but its evidence could not be "
                    "fully recorded; an uncertainty hold blocks further submissions") from exc
            self._mark_native_failed(pid, attempt_id, reason)
            state = self._load_state(pid)
            failed = self._repaint_proposal(state, proposal_id)
            failed.status = "failed"
            failed.error = reason[:1000]
            self._save_state(pid, state)
            raise MusicConflict(reason) from exc
        try:
            candidate = self._assemble_repaint(
                pid, proposal, attempt_id, execution.output)
        except Exception as exc:
            reason = f"native provider output rejected: {exc}"
            self._mark_native_failed(pid, attempt_id, reason)
            state = self._load_state(pid)
            failed = self._repaint_proposal(state, proposal_id)
            failed.status = "failed"
            failed.error = reason[:1000]
            self._save_state(pid, state)
            raise MusicConflict(reason) from exc
        candidate.assembly["provider_task_id"] = execution.task_id
        candidate.assembly["provider_output_sha256"] = _sha256_bytes(execution.raw_bytes)
        try:
            self._persist_native_outcome(pid, attempt_id, "completed")
        except Exception as exc:
            reason = f"native post-dispatch outcome recording failure: {exc}"
            self._mark_native_uncertain(pid, attempt_id, reason)
            state = self._load_state(pid)
            held = self._repaint_proposal(state, proposal_id)
            held.status = "uncertain"
            held.error = reason[:1000]
            self._save_state(pid, state)
            raise MusicConflict(
                "native repaint completed remotely but its outcome record could not "
                "be persisted; an uncertainty hold blocks further submissions") from exc
        journal_links = self._request_link(pid, attempt_id)
        self._append_journal(pid, {
            "kind": "attempt_completed",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "provider": NATIVE_PROVIDER,
            "asset_id": candidate.master_asset_id,
            "sha256": candidate.master_sha256,
            "task_id": execution.task_id,
            "provider_output_sha256": _sha256_bytes(execution.raw_bytes),
            **journal_links,
        })
        state = self._load_state(pid)
        done = self._repaint_proposal(state, proposal_id)
        done.status = "candidate"
        done.candidate = candidate
        self._save_state(pid, state)
        return done.model_dump(mode="json")

    def _submit_legacy_repaint(
        self, pid: str, proposal_id: str, proposal: MusicRepaintProposalRecord,
        state: MusicState, cap: int | None, attempt_id: str,
        request: RepaintRequest, active: RepaintProvider,
    ) -> dict[str, Any]:
        self._append_journal(pid, {
            "kind": "attempt_started",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "provider": "ace-step-1.5-repaint",
            "task": "repaint",
            "revision": proposal.source_revision,
            "revision_name": f"repaint {proposal.proposal_id[:6]}",
            "prompt": proposal.prompt,
            "source_sha256": proposal.source_sha256,
            "start_sample": proposal.start_sample,
            "end_sample": proposal.end_sample,
            "submission_cap": cap,
            "effective_settings": proposal.settings.model_dump(mode="json"),
        })
        proposal.status = "submitted"
        proposal.attempt_id = attempt_id
        self._save_state(pid, state)
        try:
            output = active.generate(request)
            candidate = self._assemble_repaint(pid, proposal, attempt_id, output)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._append_journal(pid, {
                "kind": "attempt_uncertain",
                "attempt_id": attempt_id,
                "at": _utcnow().isoformat(),
                "error": str(exc),
            })
            state = self._load_state(pid)
            held = self._repaint_proposal(state, proposal_id)
            held.status = "uncertain"
            held.error = str(exc)
            state.recovery_required = True
            state.recovery_reason = (
                "repaint provider timeout/disconnect; confirm the provider stopped "
                "before further submissions")
            self._save_state(pid, state)
            raise MusicConflict(
                "repaint provider did not return a confirmed result; an uncertainty "
                "hold blocks further submissions until recovery is confirmed") from exc
        except Exception as exc:
            self._append_journal(pid, {
                "kind": "attempt_failed",
                "attempt_id": attempt_id,
                "at": _utcnow().isoformat(),
                "error": str(exc),
            })
            state = self._load_state(pid)
            failed = self._repaint_proposal(state, proposal_id)
            failed.status = "failed"
            failed.error = str(exc)
            self._save_state(pid, state)
            raise MusicConflict(f"repaint submission failed: {exc}") from exc
        self._append_journal(pid, {
            "kind": "attempt_completed",
            "attempt_id": attempt_id,
            "at": _utcnow().isoformat(),
            "asset_id": candidate.master_asset_id,
            "sha256": candidate.master_sha256,
        })
        state = self._load_state(pid)
        done = self._repaint_proposal(state, proposal_id)
        done.status = "candidate"
        done.candidate = candidate
        self._save_state(pid, state)
        return done.model_dump(mode="json")

    def recover_extended_bridge(
        self, pid: str, attempt_id: str, confirm_stopped: bool,
    ) -> dict[str, Any]:
        if confirm_stopped is not True:
            raise MusicConflict("extended-Bridge recovery requires confirmed stop")
        with self._project_lock(pid):
            state = self._load_state(pid)
            if not state.recovery_required:
                raise MusicConflict("no extended-Bridge recovery hold is active")
            journal = self._journal_state(pid)
            uncertain = next(
                (row for row in journal["rows"]
                 if row.get("kind") == "attempt_uncertain"
                 and row.get("attempt_id") == attempt_id),
                None,
            )
            if uncertain is None:
                raise MusicConflict("extended-Bridge uncertain attempt was not found")
            self._append_native_event(pid, attempt_id, "recovery_confirmed", {
                "provider_stop_confirmed": True,
                "allowance_consumed": True,
            })
            self._append_journal(pid, {
                "kind": "recovery_confirmed",
                "attempt_id": attempt_id,
                "at": _utcnow().isoformat(),
                "provider_stop_confirmed": True,
                "allowance_consumed": True,
                "note": "owned service shutdown/operator confirmation; no retry and no refund",
                **self._request_link(pid, attempt_id),
            })
            state = self._load_state(pid)
            state.recovery_required = False
            state.recovery_reason = None
            self._save_state(pid, state)
            return {
                "recovery_required": False,
                "attempt_id": attempt_id,
                "allowance_consumed": True,
                "no_retry": True,
            }

    def recover_repaint(
        self, pid: str, proposal_id: str, confirm_stopped: bool,
    ) -> dict[str, Any]:
        if confirm_stopped is not True:
            raise MusicConflict("recovery requires confirmed provider stop")
        state = self._load_state(pid)
        proposal = self._repaint_proposal(state, proposal_id)
        if not state.recovery_required:
            raise MusicConflict("no repaint recovery hold is active")
        state.recovery_required = False
        state.recovery_reason = None
        recovered_ids: list[str] = []
        for held in state.repaint_proposals:
            if held.status == "uncertain":
                held.status = "failed"
                held.error = "provider stop confirmed; submission allowance consumed"
                if held.attempt_id:
                    recovered_ids.append(held.attempt_id)
        self._save_state(pid, state)
        for attempt_id in recovered_ids:
            try:
                self._append_native_event(pid, attempt_id, "recovery_confirmed", {
                    "provider_stop_confirmed": True,
                    "allowance_consumed": True,
                })
            except Exception:
                log.exception("failed to append native recovery event")
            try:
                if self._native_file(
                        pid, attempt_id, ".native.request.json").is_file():
                    self._append_journal(pid, {
                        "kind": "recovery_confirmed",
                        "attempt_id": attempt_id,
                        "at": _utcnow().isoformat(),
                        "provider_stop_confirmed": True,
                        **self._request_link(pid, attempt_id),
                    })
            except Exception:
                log.exception("failed to append native recovery journal row")
        return {
            "recovered": True,
            "recovery_required": False,
            "proposal": proposal.model_dump(mode="json"),
        }

    def _assemble_repaint(
        self, pid: str, proposal: MusicRepaintProposalRecord,
        attempt_id: str, output: RepaintOutput,
    ) -> MusicRepaintCandidateRecord:
        source_path = self.store.asset_path(pid, proposal.source_asset_id)
        source, sample_rate, channels = _decode_pcm(source_path)
        if output.sample_rate != sample_rate or output.channels != channels:
            raise MusicConflict("repaint output sample rate/channels differ from the source")
        if len(output.samples) != len(source):
            raise MusicConflict("repaint output length differs from the source; refusing to stretch")
        fade = int(round(get_settings().music.repaint_crossfade_ms * sample_rate / 1000))
        total_frames = len(source) // channels
        start = proposal.start_sample
        end = proposal.end_sample
        if end > total_frames:
            raise MusicConflict("edit interval is outside the source")
        master = array("f", source)
        fade_start = max(0, start - fade)
        for frame in range(fade_start, start):
            weight = (frame - fade_start) / fade
            base = frame * channels
            for channel in range(channels):
                master[base + channel] = (
                    weight * output.samples[base + channel]
                    + (1.0 - weight) * source[base + channel])
        for index in range(start * channels, end * channels):
            master[index] = output.samples[index]
        fade_end = min(total_frames, end + fade)
        for frame in range(end, fade_end):
            weight = 1.0 - (frame - end) / fade
            base = frame * channels
            for channel in range(channels):
                master[base + channel] = (
                    weight * output.samples[base + channel]
                    + (1.0 - weight) * source[base + channel])
        outside_start = fade_start * channels
        outside_end = fade_end * channels
        preserved = (master[:outside_start] == source[:outside_start]
                     and master[outside_end:] == source[outside_end:])
        if not preserved:
            raise MusicConflict("PCM outside the declared edit/transition region changed")
        master_bytes = _wav_f32_bytes(master, sample_rate, channels)
        raw_bytes = _wav_f32_bytes(output.samples, sample_rate, channels)
        meta = {
            "provider": proposal.provider,
            "task": "repaint",
            "proposal_id": proposal.proposal_id,
            "source_asset_id": proposal.source_asset_id,
            "source_sha256": proposal.source_sha256,
            "source_revision": proposal.source_revision,
            "start_sample": start,
            "end_sample": end,
            "transition_start_sample": fade_start,
            "transition_end_sample": fade_end,
            "prompt": proposal.prompt,
            "effective_settings": proposal.settings.model_dump(mode="json"),
        }
        raw_asset = self.store.save_asset(
            pid, AssetKind.music, f"repaint-raw-{attempt_id}.wav", raw_bytes, meta=meta)
        master_asset = self.store.save_asset(
            pid, AssetKind.music, f"repaint-master-{attempt_id}.wav", master_bytes, meta=meta)
        return MusicRepaintCandidateRecord(
            raw_asset_id=raw_asset.id,
            master_asset_id=master_asset.id,
            raw_sha256=_sha256_bytes(raw_bytes),
            master_sha256=_sha256_bytes(master_bytes),
            sample_rate=sample_rate,
            channels=channels,
            duration_samples=total_frames,
            start_sample=start,
            end_sample=end,
            transition_start_sample=fade_start,
            transition_end_sample=fade_end,
            source_sha256=proposal.source_sha256,
            assembly={
                "method": "source-pcm-splice",
                "crossfade_samples": fade,
                "crossfade_ms": get_settings().music.repaint_crossfade_ms,
                "output_format": "wav-float32",
            },
            verification={
                "pcm_equal_outside_transition": True,
                "checked_before_sample": outside_start // channels,
                "checked_after_sample": outside_end // channels,
                "duration_samples": total_frames,
            },
        )

    def apply_repaint(self, pid: str, proposal_id: str) -> dict[str, Any]:
        state = self._load_state(pid)
        proposal = self._repaint_proposal(state, proposal_id)
        if proposal.status != "candidate" or proposal.candidate is None:
            raise MusicConflict(
                f"repaint proposal is {proposal.status}; a candidate is required")
        if state.draft is None:
            raise MusicConflict("a saved draft is required before applying a repaint")
        max_revisions = get_settings().music.max_revisions
        if len(state.revisions) + 1 > max_revisions:
            raise MusicConflict(
                f"revision retention limit ({max_revisions}) reached; history is never "
                "deleted automatically - increase music.max_revisions")
        version = state.draft.version + 1
        name = f"repaint {proposal.proposal_id[:6]}"
        state.revisions.append(MusicRevisionRecord(
            version=version,
            name=name,
            parent_version=state.draft.version,
            source="repaint",
            settings=state.draft.settings,
            lyrics_sha256=state.draft.lyrics_sha256,
            settings_sha256=state.draft.settings_sha256,
        ))
        state.draft = MusicDraftRecord(
            version=version,
            settings=state.draft.settings,
            lyrics_sha256=state.draft.lyrics_sha256,
            settings_sha256=state.draft.settings_sha256,
            revision_name=name,
        )
        proposal.status = "applied"
        self._save_state(pid, state)
        self.store.update_asset_meta(
            pid, proposal.candidate.master_asset_id, applied_revision=version)
        return {
            "proposal": proposal.model_dump(mode="json"),
            "draft_version": version,
            "revision": state.revisions[-1].model_dump(mode="json"),
        }

    def compare_repaint(self, pid: str, proposal_id: str) -> dict[str, Any]:
        proposal = self._repaint_proposal(self._load_state(pid), proposal_id)
        return {
            "proposal": proposal.model_dump(mode="json"),
            "source": {
                "asset_id": proposal.source_asset_id,
                "sha256": proposal.source_sha256,
                "revision": proposal.source_revision,
                "duration_s": proposal.source_duration_s,
            },
            "interval": {
                "start_s": proposal.start_s,
                "end_s": proposal.end_s,
                "start_sample": proposal.start_sample,
                "end_sample": proposal.end_sample,
                "context_start_s": proposal.context_start_s,
                "context_end_s": proposal.context_end_s,
            },
            "candidate": (
                proposal.candidate.model_dump(mode="json")
                if proposal.candidate else None),
        }

    def _section_source_identity(
        self, pid: str, asset_id: str,
    ) -> tuple[int, str, int, int]:
        row, started = self._completed_source_take(pid, asset_id)
        revision = started.get("revision")
        if revision is None:
            raise MusicConflict("source take has no revision linkage; its settings are unknown")
        state = self._load_state(pid)
        record = next(
            (item for item in state.revisions if item.version == revision), None)
        if record is None:
            raise MusicConflict("source revision settings are unavailable (unknown)")
        path = self.store.asset_path(pid, asset_id)
        digest = _sha256_bytes(path.read_bytes())
        if digest != row.get("sha256"):
            raise MusicConflict("source take bytes do not match the recorded hash")
        sample_rate, channels, frames = _audio_info(path)
        return int(revision), digest, sample_rate, frames

    def save_section_boundary(
        self, pid: str, *,
        asset_id: str, section: str, occurrence: int, arrangement_index: int,
        start_s: float, end_s: float,
        suggested_start_s: float | None = None,
        suggested_end_s: float | None = None,
        shared_start_from: str | None = None,
    ) -> dict[str, Any]:
        section = section.strip()
        if not section:
            raise MusicConflict("section name is required")
        if start_s < 0 or end_s <= start_s:
            raise MusicConflict("boundaries must satisfy 0 <= start < end")
        revision, digest, sample_rate, frames = self._section_source_identity(
            pid, asset_id)
        duration = frames / sample_rate
        if end_s > duration + 0.001:
            raise MusicConflict(
                f"end exceeds the actual source duration ({duration:.3f}s)")
        start_sample = int(round(start_s * sample_rate))
        end_sample = int(round(end_s * sample_rate))
        if start_sample < 0 or end_sample <= start_sample or end_sample > frames:
            raise MusicConflict("boundaries are outside the decoded source")
        state = self._load_state(pid)
        record = MusicSectionBoundaryRecord(
            source_asset_id=asset_id,
            source_sha256=digest,
            source_revision=revision,
            section=section,
            occurrence=occurrence,
            arrangement_index=arrangement_index,
            suggested_start_s=suggested_start_s,
            suggested_end_s=suggested_end_s,
            start_s=float(start_s),
            end_s=float(end_s),
            start_sample=start_sample,
            end_sample=end_sample,
            source_duration_s=duration,
            shared_start_from=shared_start_from,
            confirmed_at=_utcnow(),
        )
        state.section_boundaries = [
            item for item in state.section_boundaries
            if not (item.source_asset_id == asset_id
                    and item.section == section
                    and item.occurrence == occurrence)
        ]
        state.section_boundaries.append(record)
        self._save_state(pid, state)
        return {
            "boundary": record.model_dump(mode="json"),
            "review_flags": self._section_boundary_flags(state, asset_id),
        }

    def section_boundaries(
        self, pid: str, asset_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._load_state(pid)
        records = [
            item for item in state.section_boundaries
            if asset_id is None or item.source_asset_id == asset_id
        ]
        records = sorted(
            records, key=lambda item: (item.arrangement_index, item.occurrence))
        return {
            "boundaries": [item.model_dump(mode="json") for item in records],
            "review_flags": self._section_boundary_flags(state, asset_id),
        }

    def _section_boundary_flags(
        self, state: MusicState, asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        groups: dict[str, list[Any]] = {}
        for item in state.section_boundaries:
            if asset_id is not None and item.source_asset_id != asset_id:
                continue
            groups.setdefault(item.source_asset_id, []).append(item)
        flags: list[dict[str, Any]] = []
        for source_id, records in groups.items():
            records = sorted(
                records, key=lambda item: (item.arrangement_index, item.occurrence))
            for previous, current in zip(records, records[1:]):
                delta = current.start_s - previous.end_s
                if delta > 0.0005:
                    flags.append({
                        "code": "gap",
                        "source_asset_id": source_id,
                        "previous": f"{previous.section} #{previous.occurrence}",
                        "next": f"{current.section} #{current.occurrence}",
                        "seconds": round(delta, 3),
                        "message": (
                            f"gap of {delta:.3f}s between {previous.section} "
                            f"#{previous.occurrence} end and {current.section} "
                            f"#{current.occurrence} start"),
                    })
                elif delta < -0.0005:
                    flags.append({
                        "code": "overlap",
                        "source_asset_id": source_id,
                        "previous": f"{previous.section} #{previous.occurrence}",
                        "next": f"{current.section} #{current.occurrence}",
                        "seconds": round(-delta, 3),
                        "message": (
                            f"overlap of {-delta:.3f}s between {previous.section} "
                            f"#{previous.occurrence} end and {current.section} "
                            f"#{current.occurrence} start"),
                    })
        return flags


def _read_wav_f32(path: Path) -> tuple[array, int, int]:
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MusicConflict("source is not a RIFF/WAVE file")
    fmt = None
    payload = None
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        body = data[offset + 8:offset + 8 + size]
        offset += 8 + size + (size & 1)
        if chunk_id == b"fmt ":
            fmt = body
        elif chunk_id == b"data":
            payload = body
    if fmt is None or payload is None:
        raise MusicConflict("WAVE file is missing fmt/data chunks")
    tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
    samples = array("f")
    if tag == 3 and bits == 32:
        samples.frombytes(payload)
    elif tag == 1 and bits == 16:
        ints = array("h")
        ints.frombytes(payload)
        samples = array("f", (value / 32768.0 for value in ints))
    else:
        raise MusicConflict(f"unsupported WAVE encoding tag={tag} bits={bits}")
    return samples, rate, int(channels)


def _wav_f32_bytes(samples: array, sample_rate: int, channels: int) -> bytes:
    payload = samples.tobytes()
    block = channels * 4
    fmt = struct.pack("<HHIIHH", 3, channels, sample_rate, sample_rate * block, block, 32)
    chunks = (
        b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(payload)) + payload)
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def _decode_pcm(path: Path) -> tuple[array, int, int]:
    if path.suffix.lower() == ".wav":
        return _read_wav_f32(path)
    binary = get_settings().ffmpeg.binary
    proc = subprocess.run(
        [binary, "-v", "error", "-i", str(path), "-f", "f32le",
         "-ac", "2", "-ar", "48000", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise MusicConflict("source audio could not be decoded")
    samples = array("f")
    samples.frombytes(proc.stdout)
    return samples, 48000, 2


def _audio_info(path: Path) -> tuple[int, int, int]:
    if path.suffix.lower() == ".wav":
        samples, rate, channels = _read_wav_f32(path)
        return rate, channels, len(samples) // channels
    probe = get_settings().ffmpeg.ffprobe_binary
    proc = subprocess.run(
        [probe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels:format=duration",
         "-of", "json", str(path)],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise MusicConflict("source audio metadata could not be read")
    info = json.loads(proc.stdout or b"{}")
    streams = info.get("streams") or []
    if not streams:
        raise MusicConflict("source audio has no audio stream")
    rate = int(streams[0].get("sample_rate") or 0)
    channels = int(streams[0].get("channels") or 0)
    duration = float((info.get("format") or {}).get("duration") or 0.0)
    if rate <= 0 or channels <= 0 or duration <= 0:
        raise MusicConflict("source audio metadata is incomplete")
    return rate, channels, int(round(duration * rate))


class _NativeCaptureRecorder:
    def __init__(self, service: MusicService, pid: str, attempt_id: str) -> None:
        self.service = service
        self.pid = pid
        self.attempt_id = attempt_id
        self.dispatched = False

    def mark_dispatched(self) -> None:
        self.dispatched = True

    def submission(self, response: dict[str, Any], task_id: str | None) -> None:
        self.service._persist_native_submission(
            self.pid, self.attempt_id, response, task_id)

    def status(self, response: dict[str, Any], row: Any) -> None:
        self.service._append_native_status(
            self.pid, self.attempt_id, response, row)

    def output_download(self, response: dict[str, Any], url: str) -> None:
        self.service._persist_native_output(
            self.pid, self.attempt_id, response, url)

    def event(self, kind: str, detail: Any = None) -> None:
        self.service._append_native_event(
            self.pid, self.attempt_id, kind, detail)


_service: MusicService | None = None


def get_music_service() -> MusicService:
    global _service
    if _service is None:
        _service = MusicService()
    return _service
