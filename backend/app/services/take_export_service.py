"""Archival export of one completed take, reusing music journals and profiles."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models.music import MusicSettings
from app.services.comfyui_client import ComfyUIClient
from app.services.music_service import (
    _effective_tags,
    _normalize_lyrics,
    get_music_service,
)
from app.services.profile_store import ProfileStore, get_profile_store
from app.services.provenance import WORKFLOWS_DIR, workflow_record

SECRET_KEYS = {
    "api_key", "apikey", "authorization", "credential", "credentials",
    "password", "secret", "token", "access_token", "refresh_token",
}
EXCLUDED_FIELDS = [
    "approval", "audio_approval", "submission_cap",
    "credentials", "other_attempts", "other_assets", "other_projects",
    "unlinked_artist_profiles",
]


class TakeExportError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS or key in {"submission_cap"}:
                continue
            out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class TakeExportService:
    def __init__(self, music=None, profiles: ProfileStore | None = None) -> None:
        self.music = music or get_music_service()
        self.profiles = profiles or get_profile_store()

    def _linked_profiles(self, pid: str, asset_id: str) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
        summaries: list[dict[str, Any]] = []
        files: dict[str, bytes] = {}
        for meta in self.profiles.list_profiles():
            matching: list[int] = []
            for version in self.profiles.list_versions(meta.id):
                record = self.profiles.get_version(meta.id, version)
                if (record.source.project_id == pid
                        and record.source.asset_id == asset_id):
                    matching.append(version)
                    payload = json.dumps(record.model_dump(mode="json"), indent=2,
                                         sort_keys=True).encode("utf-8")
                    files[f"artist-profiles/{meta.id}/versions/{version:04d}.json"] = payload
            if not matching:
                continue
            payload = json.dumps(meta.model_dump(mode="json"), indent=2,
                                 sort_keys=True).encode("utf-8")
            files[f"artist-profiles/{meta.id}/profile.json"] = payload
            summaries.append({
                "id": meta.id,
                "name": meta.name,
                "latest_version": meta.latest_version,
                "linked_versions": matching,
                "note": "Artist Profile data is archival and is not applied by this export.",
            })
        return summaries, files

    def _execution_files(self, pid: str, attempt_id: str) -> dict[str, Path]:
        root = self.music._state_dir(pid) / "requests"
        suffixes = {
            "request_metadata": ".json",
            "request_body": ".body",
            "environment": ".environment.json",
            "response_metadata": ".response.json",
            "response_body": ".response.body",
            "history": ".history.json",
            "output": ".output.json",
            "outcome": ".outcome.json",
            "events": ".events.jsonl",
            "native_request_metadata": ".native.request.json",
            "native_request_body": ".native.body",
            "native_environment": ".native.environment.json",
            "native_response_metadata": ".native.response.json",
            "native_response_body": ".native.response.body",
            "native_status": ".native.status.jsonl",
            "native_output": ".native.output.json",
            "native_output_body": ".native.output.body",
            "native_outcome": ".native.outcome.json",
            "native_events": ".native.events.jsonl",
        }
        found = {}
        for name, suffix in suffixes.items():
            path = root / f"{attempt_id}{suffix}"
            if path.is_file():
                found[name] = path
        conditioning = sorted(root.glob(f"{attempt_id}.native.conditioning.*"))
        if conditioning:
            found["native_conditioning"] = conditioning[0]
        return found

    def audit(self, pid: str, asset_id: str) -> dict[str, Any]:
        state = self.music._load_state(pid)
        journal = self.music._journal_state(pid)
        started = self.music._started_rows(journal)
        completed = next(
            (row for row in journal["rows"]
             if row.get("kind") == "attempt_completed"
             and row.get("asset_id") == asset_id),
            None,
        )
        if completed is None:
            raise KeyError(f"completed take {asset_id} not found")
        attempt_id = completed.get("attempt_id")
        st = started.get(attempt_id) or {}
        try:
            ref = self.music.store.get_asset(pid, asset_id)
            audio_path = self.music.store.asset_path(pid, asset_id)
        except KeyError as exc:
            raise TakeExportError("source audio asset is not present") from exc
        audio_bytes = audio_path.read_bytes()
        audio_sha = _sha(audio_bytes)
        expected_audio_sha = completed.get("sha256") or ref.meta.get("music", {}).get("audio_sha256")
        if expected_audio_sha and audio_sha != expected_audio_sha:
            raise TakeExportError("source audio bytes do not match the completed take SHA-256")
        meta = ref.meta.get("music", {}) or {}
        provenance = ref.meta.get("provenance", {}) or {}
        recorded_rendered = provenance.get("prompt", {}).get("rendered_sha256")
        candidates: list[tuple[dict[str, Any], str]] = []
        if isinstance(st.get("effective_settings"), dict):
            candidates.append((st["effective_settings"], "attempt_started.effective_settings"))
        if isinstance(meta.get("effective_settings"), dict):
            candidates.append((meta["effective_settings"], "asset_meta.music.effective_settings"))
        if not candidates:
            raise TakeExportError("no effective settings snapshot is retained")
        settings = None
        settings_source = candidates[0][1]
        rendered = ""
        rendered_sha = ""
        for candidate, source in candidates:
            try:
                parsed = MusicSettings.model_validate(candidate)
            except Exception:
                continue
            candidate_text = f"{_effective_tags(parsed)}\n\n{_normalize_lyrics(parsed.lyrics)}"
            candidate_sha = _sha(candidate_text.encode("utf-8"))
            if settings is None:
                settings = parsed
                settings_source = source
                rendered = candidate_text
                rendered_sha = candidate_sha
            if recorded_rendered and candidate_sha == recorded_rendered:
                settings = parsed
                settings_source = source
                rendered = candidate_text
                rendered_sha = candidate_sha
                break
        if settings is None:
            raise TakeExportError("retained effective settings are invalid")
        recorded_workflow = (provenance.get("workflows") or [{}])[0]
        current_workflow = workflow_record(recorded_workflow.get("name") or meta.get("workflow") or "")
        template_path = WORKFLOWS_DIR / f"{current_workflow.get('name')}.json"
        current_template_sha = current_workflow.get("sha256")
        recorded_template_sha = recorded_workflow.get("sha256")

        request_path = st.get("api_request_path") or meta.get("api_request_path")
        request_bytes = None
        request_sha = None
        request_record = None
        if request_path:
            try:
                path = self.music.store.root / pid / request_path
                request_bytes = path.read_bytes()
                request_sha = _sha(request_bytes)
                request_record = json.loads(request_bytes.decode("utf-8"))
            except Exception:
                request_bytes = None
                request_sha = None
                request_record = None
        exact_files = self._execution_files(pid, attempt_id)
        native_record = None
        native_request_path = exact_files.get("native_request_metadata")
        native_request_sha = None
        if native_request_path is not None:
            try:
                native_bytes = native_request_path.read_bytes()
                native_request_sha = _sha(native_bytes)
                native_record = json.loads(native_bytes.decode("utf-8"))
            except Exception as exc:
                native_record = None
                native_request_path = None
        if native_record is None:
            try:
                template = json.loads(template_path.read_text(encoding="utf-8"))
                current_prompt = ComfyUIClient._patch_workflow(
                    template, self.music._patches(pid, attempt_id, settings))
                current_prompt_sha = _sha(json.dumps(
                    current_prompt, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                current_prompt_status = "reconstructed from current template and current _patches; not executed"
            except Exception as exc:
                current_prompt_sha = None
                current_prompt_status = f"unavailable: {exc}"
        else:
            current_prompt_sha = None
            current_prompt_status = "not_applicable: native ACE-Step execution has no ComfyUI prompt graph"

        boundaries = self.music.section_boundaries(pid, asset_id)
        profiles, profile_files = self._linked_profiles(pid, asset_id)
        exact_records = {
            name: {
                "path": str(path.relative_to(self.music.store.root / pid)).replace("\\", "/"),
                "sha256": _sha(path.read_bytes()),
                "bytes": path.stat().st_size,
                "exact": True,
            }
            for name, path in exact_files.items()
        }
        environment = (
            (native_record or {}).get("environment")
            or (request_record or {}).get("environment")
            or {}
        )
        native = native_record is not None
        native_task_id = (
            completed.get("task_id")
            or (json.loads(exact_files["native_response_metadata"].read_text(
                encoding="utf-8")).get("task_id")
                if "native_response_metadata" in exact_files else None)
        )
        if native:
            unavailable = [
                "native_loaded_model_bytes_verified",
                "native_runtime_versions_at_execution",
                "native_commit_freshly_verified",
                "native_cancellation_request_response",
            ]
            if not environment.get("model", {}).get("sha256"):
                unavailable.append("native_model_identity")
            if "native_response_metadata" not in exact_files:
                unavailable.append("native_submission_response")
            if "native_status" not in exact_files:
                unavailable.append("native_polling_status_history")
            if "native_output" not in exact_files:
                unavailable.append("native_output_record")
            if "native_output_body" not in exact_files:
                unavailable.append("native_output_body")
            if "native_outcome" not in exact_files:
                unavailable.append("native_terminal_outcome_record")
            if "native_conditioning" not in exact_files:
                unavailable.append("native_conditioning_bytes")
            request_body_sha = exact_records.get(
                "native_request_body", {}).get("sha256")
            if native_record.get("request_body_sha256"):
                request_body_sha = native_record["request_body_sha256"]
            if request_body_sha != native_record.get("request_body_sha256"):
                unavailable.append("native_request_body_hash_mismatch")
            if "native_request_body" not in exact_records:
                unavailable.append("native_request_body_file_missing")
            if not environment.get("provider", {}).get("contacted_by_capture"):
                unavailable.append("native_service_reachability_pre_dispatch")
        else:
            unavailable = ["comfyui_queue_timestamps"]
            if not environment.get("comfyui", {}).get("version"):
                unavailable.append("comfyui_software_version_at_execution")
            if not environment.get("commit", {}).get("value"):
                unavailable.append("comfyui_commit_at_execution")
            if request_record is None:
                unavailable.extend([
                    "exact_resolved_comfyui_api_request",
                    "comfyui_client_id",
                    "exact_executed_workflow_patch_graph",
                ])
            request_body_sha = exact_records.get("request_body", {}).get("sha256")
            if request_record is not None and request_record.get("request_body_sha256"):
                request_body_sha = request_record["request_body_sha256"]
            if request_record is not None and request_body_sha != request_record.get(
                    "request_body_sha256"):
                unavailable.append("exact_request_body_hash_mismatch")
            if request_record is not None and "request_body" not in exact_records:
                unavailable.append("exact_request_body_file_missing")
            if "history" not in exact_records:
                unavailable.append("comfyui_history_for_prompt")
            if "output" not in exact_records:
                unavailable.append("comfyui_output_record")
            if "outcome" not in exact_records:
                unavailable.append("comfyui_terminal_outcome_record")
        journal_rows = [
            _redact(row) for row in journal["rows"]
            if row.get("attempt_id") == attempt_id or row.get("asset_id") == asset_id
        ]
        retained = {
            "effective_settings": settings.model_dump(mode="json"),
            "effective_settings_source": settings_source,
            "effective_settings_sources_retained": [source for _candidate, source in candidates],
            "rendered_prompt_source": settings_source,
            "lyrics": settings.lyrics,
            "lyrics_sha256": _sha(settings.lyrics.encode("utf-8")),
            "settings_sha256": st.get("settings_sha256") or meta.get("settings_sha256"),
            "revision": st.get("revision") if st.get("revision") is not None else meta.get("revision"),
            "revision_name": st.get("revision_name") or meta.get("revision_name"),
            "engine": st.get("engine") or meta.get("engine"),
            "checkpoint": st.get("checkpoint") or meta.get("checkpoint"),
            "checkpoint_sha256": meta.get("checkpoint_sha256"),
            "workflow_name": recorded_workflow.get("name") or meta.get("workflow"),
            "workflow_template_sha256_recorded": recorded_template_sha,
            "workflow_template_version": recorded_workflow.get("version"),
            "prompt_id": completed.get("prompt_id") or meta.get("prompt_id"),
            "attempt_started_at": st.get("at"),
            "attempt_completed_at": completed.get("at"),
            "audio_sha256": audio_sha,
            "audio_relative_path": ref.relative_path,
            "rendered_prompt_sha256": recorded_rendered,
            "exact_api_request": request_record is not None,
            "exact_native_request": native,
            "provider_kind": "native_ace_step_repaint" if native else "comfyui",
            "native_task_id": native_task_id if native else None,
        }
        if request_record is not None:
            retained["api_request_path"] = request_path
            retained["api_request_sha256"] = request_sha
        if native:
            retained["native_request_path"] = str(
                native_request_path.relative_to(self.music.store.root / pid)
            ).replace("\\", "/")
            retained["native_request_sha256"] = native_request_sha
            retained["native_model"] = environment.get("model")
            retained["native_code"] = environment.get("code")
            retained["native_conditioning"] = native_record.get("conditioning")
            retained["native_request_body_sha256"] = native_record.get(
                "request_body_sha256")
        audit = {
            "schema": "song_writer.take_export_audit.v1",
            "generated_at": _utcnow().isoformat(),
            "project_id": pid,
            "asset_id": asset_id,
            "attempt_id": attempt_id,
            "source_audio": {
                "filename": ref.filename,
                "relative_path": ref.relative_path,
                "sha256": audio_sha,
                "expected_sha256": expected_audio_sha,
                "verified": bool(expected_audio_sha and audio_sha == expected_audio_sha),
                "bytes": len(audio_bytes),
            },
            "execution": {
                "provider_kind": "native_ace_step_repaint" if native else "comfyui",
                "records": journal_rows,
                "prompt_id": completed.get("prompt_id") or meta.get("prompt_id"),
                "native_task_id": native_task_id if native else None,
                "api_request_retained": request_record is not None,
                "api_request": request_record,
                "api_request_sha256": request_sha,
                "native_request_retained": native,
                "native_request": native_record,
                "native_request_sha256": native_request_sha,
                "exact_records": exact_records,
                "exact_request_body_sha256": request_body_sha,
                "journal_is_redacted": True,
                "redacted_fields": ["submission_cap"],
                "exact_request_available": (
                    native and "native_request_body" in exact_records
                ) or (
                    not native and request_record is not None
                    and "request_body" in exact_records
                ),
                "exact_native_request_available": (
                    native and "native_request_body" in exact_records),
            },
            "retained": retained,
            "reconstructed": {
                "rendered_prompt": {
                    "basis": "derived from retained effective settings",
                    "sha256": rendered_sha,
                    "recorded_sha256": recorded_rendered,
                    "matches_recorded": bool(recorded_rendered and rendered_sha == recorded_rendered),
                    "text": rendered,
                },
                "current_code_patched_prompt_sha256": current_prompt_sha,
                "current_code_patched_prompt_status": current_prompt_status,
            },
            "current_only": (
                {
                    "native_service_reachability": environment.get(
                        "provider", {}).get("reachability", "unknown"),
                    "native_loaded_model_bytes_verified": False,
                    "is_execution_record": False,
                }
                if native else {
                    "workflow_template_path": current_workflow.get("path"),
                    "workflow_template_sha256": current_template_sha,
                    "matches_recorded_template": bool(
                        current_template_sha and recorded_template_sha
                        and current_template_sha == recorded_template_sha),
                    "is_execution_record": False,
                }
            ),
            "unavailable": unavailable,
            "source_bound_markers": boundaries,
            "artist_profiles": profiles,
            "excluded_fields": EXCLUDED_FIELDS,
            "notes": [
                "The retained settings, lyrics, model identity, workflow name/template hash, prompt_id, audio, and markers are archival.",
                "Current workflow template and current code-reconstructed prompt are not proof of the executed API graph.",
                "Native ACE-Step request/response/status/output records are explicitly separate from ComfyUI records; model parity is unverified.",
                "Source-bound markers and Artist Profiles may have been created after the take; timestamps show their own confirmation/creation.",
                "Artist Profile loading is separate and is not performed by this export.",
            ],
        }
        return {"audit": audit, "profile_files": profile_files, "settings": settings}

    def package(self, pid: str, asset_id: str) -> tuple[bytes, str, str]:
        audit_result = self.audit(pid, asset_id)
        audit = audit_result["audit"]
        profile_files = audit_result["profile_files"]
        settings = audit_result["settings"]
        ref = self.music.store.get_asset(pid, asset_id)
        audio_path = self.music.store.asset_path(pid, asset_id)
        native = audit["execution"]["provider_kind"] == "native_ace_step_repaint"
        if native:
            workflow_reference = {
                "provider_kind": "native_ace_step_repaint",
                "native_request": audit["execution"].get("native_request"),
                "comfyui_workflow": None,
                "note": "Native records are not ComfyUI execution records.",
            }
        else:
            workflow_reference = {
                "recorded_workflow": audit["retained"]["workflow_name"],
                "recorded_template_sha256": audit["retained"]["workflow_template_sha256_recorded"],
                "current_template_sha256": audit["current_only"]["workflow_template_sha256"],
                "matches_recorded_template": audit["current_only"]["matches_recorded_template"],
                "current_template_is_execution_record": False,
            }
        files: dict[str, bytes] = {
            f"audio/{ref.filename}": audio_path.read_bytes(),
            "settings/effective_settings.json": json.dumps(
                settings.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8"),
            "markers/section_boundaries.json": json.dumps(
                audit["source_bound_markers"], indent=2, sort_keys=True).encode("utf-8"),
            "provenance.json": json.dumps(
                {
                    "recorded": ref.meta.get("provenance", {}),
                    "model": audit["retained"],
                    "current_only": audit["current_only"],
                }, indent=2, sort_keys=True).encode("utf-8"),
            "workflow/reference.json": json.dumps(
                workflow_reference, indent=2, sort_keys=True).encode("utf-8"),
            "execution/journal.jsonl": (
                "\n".join(json.dumps(row, sort_keys=True)
                          for row in audit["execution"]["records"]) + "\n").encode("utf-8"),
            "README.txt": (
                "Song Writer archival take package\n"
                "Provider: " + audit["execution"]["provider_kind"] + "\n"
                "This package is read-only provenance. It does not apply audio, load a preset, "
                "or transfer approvals or submission allowances.\n"
                "Native ACE-Step records are distinct from ComfyUI records and do not imply "
                "model parity with the original ComfyUI take.\n"
                "Unavailable fields: " + ", ".join(audit["unavailable"]) + "\n"
            ).encode("utf-8"),
        }
        if audit["execution"]["api_request"] is not None:
            files["execution/api_request.json"] = json.dumps(
                audit["execution"]["api_request"], indent=2, sort_keys=True).encode("utf-8")
        if audit["execution"].get("native_request") is not None:
            files["execution/native_request.json"] = json.dumps(
                audit["execution"]["native_request"], indent=2,
                sort_keys=True).encode("utf-8")
        execution_files = self._execution_files(pid, audit["attempt_id"])
        for name, path in execution_files.items():
            files[f"execution/records/{name}{path.suffix}"] = path.read_bytes()
        files.update(profile_files)
        file_hashes = {name: _sha(payload) for name, payload in files.items()}
        manifest = dict(audit)
        manifest["package_files"] = file_hashes
        exact_execution_files = [
            f"execution/records/{name}{path.suffix}"
            for name, path in execution_files.items()
        ]
        manifest["exact_execution_files"] = exact_execution_files
        manifest["native_exact_execution_files"] = [
            name for name in exact_execution_files
            if "native_" in name
        ]
        manifest["redacted_files"] = ["execution/journal.jsonl"]
        manifest["reconstruction_files"] = ["provenance.json", "workflow/reference.json"]
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        files["manifest.json"] = manifest_bytes
        hash_lines = [f"{_sha(payload)}  {name}" for name, payload in sorted(files.items())]
        files["hashes.sha256"] = ("\n".join(hash_lines) + "\n").encode("utf-8")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(files.items()):
                archive.writestr(name, payload)
        filename = f"take-{asset_id}-package.zip"
        return buffer.getvalue(), filename, _sha(buffer.getvalue())


_service: TakeExportService | None = None


def get_take_export_service() -> TakeExportService:
    global _service
    if _service is None:
        _service = TakeExportService()
    return _service
