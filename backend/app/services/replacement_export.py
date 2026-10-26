"""Export-only replacement-section workflow.

The service validates an exact source take and listening-confirmed boundary,
extracts a reviewed context window, records the exact proposed provider
request, and can package a supplied fixed-duration candidate. It never
submits to a provider and never mutates song state, approvals, or caps.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import platform
import re
import subprocess
import tempfile
import uuid
import zipfile
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.models.replacement import (
    ReplacementExportManifest,
    ReplacementExportOutput,
    ReplacementExportProviderCapability,
    ReplacementExportRequest,
)
from app.services.music_service import (
    _NativeCaptureRecorder,
    _audio_info,
    _decode_pcm,
    _sha256_bytes,
    _wav_f32_bytes,
    get_music_service,
)
from app.services.repaint_provider import (
    NATIVE_PROVIDER,
    NATIVE_REQUEST_SCHEMA,
    AceRepaintClient,
    NativePreparedRequest,
    NativeRepaintFailed,
    NativeRepaintTimeout,
    RepaintOutput,
    RepaintRequest,
    _repaint_form,
    build_native_multipart,
)

EXPORT_ID_RE = re.compile(r"^[a-f0-9]{12}$")
SECTION_HEADERS = {
    "intro", "verse", "verse 1", "verse 2", "pre-chorus", "prechorus",
    "chorus", "chorus 2", "post-chorus", "postchorus", "bridge",
    "final chorus", "instrumental", "inst", "outro", "hook", "breakdown",
    "interlude",
}
CONTINUITY_LABEL = "singer/melody continuity unverified"


class ReplacementExportError(Exception):
    pass


def _label(line: str) -> str:
    return line.strip().strip("[]").strip().lower()


def extract_section_lines(lyrics: str, section: str) -> list[str]:
    """Return non-empty text lines for one plain or bracketed section."""
    wanted = section.strip().lower()
    lines = lyrics.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _label(line) == wanted:
            start = index + 1
            break
    if start is None:
        raise ReplacementExportError(f"section {section!r} was not found in the draft lyrics")
    collected: list[str] = []
    for line in lines[start:]:
        if _label(line) in SECTION_HEADERS:
            break
        if line.strip():
            collected.append(line.strip())
    if not collected:
        raise ReplacementExportError(f"section {section!r} has no lyric lines")
    return collected


def _supplied_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and _label(lines[0]) == "bridge":
        lines = lines[1:]
    return lines


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReplacementExportService:
    def __init__(self, music=None) -> None:
        self.music = music or get_music_service()

    # --- capability ---------------------------------------------------
    def capability(self) -> ReplacementExportProviderCapability:
        settings = get_settings().music
        native = Path(r"S:\AI\ACE-Step-1.5")
        return ReplacementExportProviderCapability(
            provider="ACE-Step 1.5 native repaint",
            native_install_present=native.is_dir(),
            source_conditioning=True,
            duration_mode="fixed_to_conditioning_source_duration",
            supports_longer_replacement=False,
            supports_variable_duration=False,
            comfyui_workflow_supports_repaint=False,
            native_lyrics_conditioning_supported=True,
            export_client_can_send_lyrics_and_settings=True,
            documented_manual_multipass_extension=True,
            replacement_duration_rule=(
                "The replacement interval must equal the confirmed section "
                "duration. The provider cannot lengthen a section in one call."
            ),
            provider_output_duration_rule=(
                "Native repaint locks output duration to the uploaded source "
                "duration; non-repaint regions are preserved and spliced back."
            ),
            notes=[
                "The ComfyUI ACE-Step 1.5 workflow exposes text2music/empty-latent "
                "nodes only; it has no source-audio or repaint node.",
                "Native ACE-Step repaint accepts source audio and explicit "
                "repainting_start/repainting_end, but the output length equals "
                "the conditioning source length.",
                "The upstream tutorial documents repeated manual repaint passes "
                "for extension; that is not a single-call longer replacement and "
                "is not used here.",
                "Repaint skips the LM; lyrics and source audio still condition "
                "the native request.",
                "The export request now records lyrics and conditioning settings; "
                "the current AceRepaintClient sends them only when supplied.",
            ],
            evidence=[
                "S:/AI/ACE-Step-1.5/docs/en/API.md:243-254 (src_audio, task_type=repaint, repainting_start/end, explicit mask)",
                "S:/AI/ACE-Step-1.5/acestep/core/generation/handler/generate_music.py:363-368 (duration locked to processed source audio)",
                "S:/AI/ACE-Step-1.5/acestep/core/generation/handler/repaint_waveform_splice.py:66-90 (same-shape splice; original outside the mask)",
                "S:/AI/ACE-Step-1.5/docs/en/API.md:169 (LM is skipped for repaint)",
                "S:/AI/ACE-Step-1.5/docs/en/Tutorial.md:802-805 (manual multi-pass extension only)",
                "S:/ComfyUI/ComfyUI/comfy_extras/nodes_ace.py:35-130 (text encoder, empty latent, reference node; no repaint/source node)",
                "comfyui/workflows/ace_step_1_5_aio.json (text2music graph, no repaint interval)",
                f"backend/app/services/repaint_provider.py (_repaint_form; checkpoint {settings.checkpoint})",
            ],
        )

    def _export_dir(self, pid: str, export_id: str) -> Path:
        if not EXPORT_ID_RE.fullmatch(export_id):
            raise KeyError(f"replacement export {export_id} not found")
        root = self.music.store.root / pid / "music" / "replacement-exports"
        return root / export_id

    def _manifest_path(self, pid: str, export_id: str) -> Path:
        return self._export_dir(pid, export_id) / "manifest.json"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                       encoding="utf-8")
        tmp.replace(path)

    def _source(self, pid: str, asset_id: str) -> tuple[dict[str, Any], Path, array, int, int]:
        takes = self.music.takes(pid)["takes"]
        take = next((item for item in takes if item["asset_id"] == asset_id), None)
        if take is None:
            raise ReplacementExportError("a completed source take is required")
        if not take.get("effective_settings"):
            raise ReplacementExportError(
                "source take has no effective-settings snapshot; refusing to invent settings")
        try:
            path = self.music.store.asset_path(pid, asset_id)
        except KeyError as exc:
            raise ReplacementExportError("source audio asset not found") from exc
        if not path.is_file():
            raise ReplacementExportError("source audio file is missing")
        actual = _sha256_bytes(path.read_bytes())
        if actual != take.get("audio_sha256"):
            raise ReplacementExportError("source audio bytes do not match the recorded take SHA-256")
        samples, sample_rate, channels = _decode_pcm(path)
        return take, path, samples, sample_rate, channels

    def _boundary(self, pid: str, asset_id: str, take: dict[str, Any],
                  section: str, occurrence: int) -> dict[str, Any]:
        state = self.music._load_state(pid)
        matches = [
            item for item in state.section_boundaries
            if item.source_asset_id == asset_id
            and item.section == section
            and item.occurrence == occurrence
        ]
        if not matches:
            raise ReplacementExportError(
                f"no listening-confirmed boundary for {section} #{occurrence}")
        record = matches[0]
        if record.source_sha256 != take.get("audio_sha256"):
            raise ReplacementExportError("boundary source SHA-256 is stale")
        if record.source_revision != take.get("revision"):
            raise ReplacementExportError("boundary source revision is stale")
        return record.model_dump(mode="json")

    def _capacity_plan(self, clip_duration_s: float) -> dict[str, Any]:
        settings = get_settings().music
        return {
            "target_gpu_index": 1,
            "admission_floor_mib": 20480,
            "runtime_floor_mib": 1536,
            "reference_peak_mib": 7788,
            "reference_peak_is_not_capacity_proof": True,
            "fresh_measurement_required": True,
            "no_competing_compute_required": True,
            "one_service_start": True,
            "one_model_load": True,
            "one_submission": True,
            "timeout_s": 7200,
            "no_retry": True,
            "service_url": settings.repaint_service_url,
            "model": settings.checkpoint,
            "model_sha256": settings.checkpoint_sha256,
            "workflow": "native-repaint-explicit-mask",
            "lm": "skipped for repaint",
            "conditioning_source_duration_s": round(clip_duration_s, 3),
        }

    def prepare(self, pid: str, req: ReplacementExportRequest) -> dict[str, Any]:
        if req.section.strip().lower() != "bridge":
            raise ReplacementExportError("this export version supports the Bridge only")
        if not req.target_duration_reviewed:
            raise ReplacementExportError("target duration must be explicitly reviewed")
        if not req.context_reviewed:
            raise ReplacementExportError("surrounding context interval must be explicitly reviewed")
        take, source_path, samples, sample_rate, channels = self._source(pid, req.asset_id)
        boundary = self._boundary(pid, req.asset_id, take, req.section, req.occurrence)
        frames = len(samples) // channels
        source_duration = frames / sample_rate
        start = float(boundary["start_s"])
        end = float(boundary["end_s"])
        section_duration = end - start
        if abs(req.target_duration_s - section_duration) > 0.05:
            if req.target_duration_s > section_duration:
                raise ReplacementExportError(
                    "longer replacement is unsupported: native repaint output duration "
                    "is locked to the conditioning source; no whole-song text2music "
                    "substitution will be used")
            raise ReplacementExportError(
                "shorter replacement is unsupported: it would shorten the song or "
                "require marker shifts; target must equal the confirmed interval")
        if not (0.0 <= req.context_start_s < start and end < req.context_end_s <= source_duration):
            raise ReplacementExportError(
                "context interval must strictly contain the confirmed section and remain in the source")
        lead_in = start - req.context_start_s
        lead_out = req.context_end_s - end
        if lead_in < 0.25 or lead_out < 0.25:
            raise ReplacementExportError("lead-in and lead-out handles must each be at least 0.25s")

        expected_lines = extract_section_lines(
            self.music._load_state(pid).draft.settings.lyrics, req.section)
        supplied_lines = _supplied_lines(req.bridge_lyrics)
        if supplied_lines != expected_lines:
            raise ReplacementExportError(
                "supplied Bridge lyrics do not match the complete draft stanza; "
                "provide all eight lines including the two reported omitted sentences")
        if len(expected_lines) < 8:
            raise ReplacementExportError(
                "the intended Bridge stanza must contain all eight lines, including "
                "the two reported omitted sentences")

        clip_start = int(round(req.context_start_s * sample_rate))
        clip_end = int(round(req.context_end_s * sample_rate))
        start_sample = int(round(start * sample_rate))
        end_sample = int(round(end * sample_rate))
        if clip_end > frames or start_sample <= clip_start or end_sample <= start_sample:
            raise ReplacementExportError("context/section samples are outside the decoded source")
        clip = array("f", samples[clip_start * channels:clip_end * channels])
        clip_bytes = _wav_f32_bytes(clip, sample_rate, channels)
        clip_sha = _sha256_bytes(clip_bytes)
        clip_duration = len(clip) // channels / sample_rate
        relative_start = (start_sample - clip_start) / sample_rate
        relative_end = (end_sample - clip_start) / sample_rate

        effective = take["effective_settings"]
        journal = self.music._journal_state(pid)
        started = self.music._started_rows(journal).get(take.get("attempt_id"), {})
        try:
            asset_meta = self.music.store.get_asset(pid, req.asset_id).meta.get("music", {}) or {}
        except KeyError:
            asset_meta = {}
        export_id = uuid.uuid4().hex[:12]
        directory = self._export_dir(pid, export_id)
        directory.mkdir(parents=True, exist_ok=False)
        clip_path = directory / "conditioning_source.wav"
        clip_path.write_bytes(clip_bytes)

        settings = get_settings().music
        guidance = req.performance_guidance.strip()
        provider_request = RepaintRequest(
            source_path=clip_path,
            source_sha256=clip_sha,
            start_s=relative_start,
            end_s=relative_end,
            prompt=guidance,
            settings=effective,
            output_format=settings.repaint_output_format,
            lyrics="\n".join(expected_lines),
            seed=effective.get("seed"),
            bpm=effective.get("bpm"),
            key_scale=effective.get("key_scale"),
            time_signature=effective.get("time_signature"),
            language=effective.get("language"),
        )
        form = _repaint_form(provider_request)
        request_payload = {
            "endpoint": f"{settings.repaint_service_url.rstrip('/')}/release_task",
            "method": "POST multipart/form-data",
            "source_upload_filename": clip_path.name,
            "source_upload_sha256": clip_sha,
            "source_upload_path": str(clip_path.relative_to(
                self.music.store.root / pid)).replace("\\", "/"),
            "form": form,
            "duration_locked_by_provider": True,
            "provider_output_duration_s": round(clip_duration, 3),
        }
        form_sha = hashlib.sha256(
            json.dumps(form, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        capability = self.capability()
        capacity = self._capacity_plan(clip_duration)
        boundary_duration = round(section_duration, 3)
        manifest = ReplacementExportManifest(
            export_id=export_id,
            created_at=_utcnow(),
            project_id=pid,
            section=req.section,
            occurrence=req.occurrence,
            source={
                "project_id": pid,
                "asset_id": req.asset_id,
                "attempt_id": take.get("attempt_id"),
                "revision": take.get("revision"),
                "audio_sha256": take.get("audio_sha256"),
                "duration_s": round(source_duration, 3),
                "relative_path": str(source_path.relative_to(
                    self.music.store.root / pid)).replace("\\", "/"),
                "sample_rate": sample_rate,
                "channels": channels,
                "engine": started.get("engine"),
                "checkpoint": started.get("checkpoint") or asset_meta.get("checkpoint"),
                "checkpoint_sha256": asset_meta.get("checkpoint_sha256"),
                "workflow": started.get("workflow"),
            },
            boundary=boundary,
            context={
                "start_s": req.context_start_s,
                "end_s": req.context_end_s,
                "reviewed": True,
                "note": req.context_note,
                "conditioning_source": "conditioning_source.wav",
                "conditioning_source_sha256": clip_sha,
                "conditioning_source_duration_s": round(clip_duration, 3),
                "lead_in_s": round(lead_in, 3),
                "lead_out_s": round(lead_out, 3),
            },
            handles={
                "source_start_s": start,
                "source_end_s": end,
                "export_start_s": req.context_start_s,
                "export_end_s": req.context_end_s,
                "export_duration_s": round(clip_duration, 3),
                "replacement_offset_in_export_s": round(relative_start, 3),
                "replacement_end_in_export_s": round(relative_end, 3),
                "lead_in_s": round(lead_in, 3),
                "lead_out_s": round(lead_out, 3),
            },
            lyrics={
                "text": "\n".join(expected_lines),
                "lines": expected_lines,
                "line_count": len(expected_lines),
                "sha256": _sha256_bytes("\n".join(expected_lines).encode("utf-8")),
                "source": "current draft Bridge stanza",
                "includes_full_bridge_stanza": True,
            },
            performance_guidance=guidance,
            effective_settings=effective,
            conditioning={
                "method": "native source-audio repaint with explicit mask",
                "source_conditioned": True,
                "lyrics_conditioned": True,
                "settings_conditioned": [
                    "seed", "bpm", "key_scale", "time_signature", "language"
                ],
                "duration_locked_to_conditioning_source": True,
                "conditioning_source": "conditioning_source.wav",
                "conditioning_source_sha256": clip_sha,
                "repaint_start_s": round(relative_start, 3),
                "repaint_end_s": round(relative_end, 3),
                "repaint_mode": "conservative",
                "chunk_mask_mode": "explicit",
                "output_format": settings.repaint_output_format,
            },
            provider_capability=capability,
            provider_request=request_payload,
            capacity_plan=capacity,
            authorization_request={
                "requested_actions": [
                    "start one isolated ACE-Step 1.5 repaint service if not already running",
                    "load one checkpoint once capacity evidence is approved",
                    "submit exactly one fixed-duration repaint request",
                    "package the returned candidate as an export bundle",
                    "stop/unload the service after capture",
                ],
                "limits": {
                    "attempts": 1,
                    "retries": 0,
                    "apply_to_song": False,
                    "marker_shift": False,
                    "approval_change": False,
                    "cap_change": False,
                    "other_projects": False,
                    "comfyui_start_stop": False,
                    "downloads": False,
                    "installs": False,
                },
                "provider_request_sha256": form_sha,
                "capacity_gate_condition": (
                    "fresh GPU1 measurement must satisfy the existing 20,480 MiB "
                    "admission floor and show no competing/uncertain compute process"),
                "output_sha256": None,
                "approval_required": True,
            },
            output=ReplacementExportOutput(status="pending"),
            continuity_label=CONTINUITY_LABEL,
            apply_allowed=False,
            warnings=[
                "fixed-duration replacement only; longer replacement is unsupported",
                "export only; the song, markers, approvals, and caps are unchanged",
                CONTINUITY_LABEL,
                "lyrics completion is declared by review; the candidate has not been auditioned",
                "target duration and context interval are user-reviewed flags, not audio analysis",
            ],
        )
        self._write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        (directory / "splice-plan.txt").write_text(
            "Replacement Section Export (export only)\n"
            f"Project: {pid}\nSection: {req.section} #{req.occurrence}\n"
            f"Source asset: {req.asset_id} ({take.get('audio_sha256')})\n"
            f"Source interval: {start:.3f}-{end:.3f}s ({section_duration:.3f}s)\n"
            f"Export interval: {req.context_start_s:.3f}-{req.context_end_s:.3f}s "
            f"({clip_duration:.3f}s)\n"
            f"Replacement within export: {relative_start:.3f}-{relative_end:.3f}s\n"
            f"Lead-in: {lead_in:.3f}s; lead-out: {lead_out:.3f}s\n\n"
            "Manual splice only. Do not shift later markers. Import the candidate, "
            "align its start to the export start, replace only the source interval, "
            "and preserve all audio outside it. No apply operation exists.\n"
            "Singer/melody continuity is unverified.\n",
            encoding="utf-8",
        )
        self._write_json(directory / "output.json", {"status": "pending"})
        return manifest.model_dump(mode="json")

    def list_exports(self, pid: str) -> dict[str, Any]:
        self.music.store.get(pid)
        root = self.music.store.root / pid / "music" / "replacement-exports"
        records = []
        if root.is_dir():
            for child in sorted(root.iterdir(), reverse=True):
                if not child.is_dir() or not EXPORT_ID_RE.fullmatch(child.name):
                    continue
                path = child / "manifest.json"
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append({
                    "export_id": data.get("export_id"),
                    "status": data.get("status"),
                    "section": data.get("section"),
                    "occurrence": data.get("occurrence"),
                    "created_at": data.get("created_at"),
                    "target_duration_s": data.get("handles", {}).get("export_duration_s"),
                    "output_sha256": data.get("output", {}).get("export_sha256"),
                })
        return {"exports": records}

    def get(self, pid: str, export_id: str) -> dict[str, Any]:
        self.music.store.get(pid)
        path = self._manifest_path(pid, export_id)
        if not path.is_file():
            raise KeyError(f"replacement export {export_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def package_candidate(
        self, pid: str, export_id: str, output: RepaintOutput,
    ) -> dict[str, Any]:
        manifest = self.get(pid, export_id)
        if manifest["status"] == "candidate":
            raise ReplacementExportError("candidate is already packaged; exports are immutable")
        directory = self._export_dir(pid, export_id)
        clip_path = directory / manifest["context"]["conditioning_source"]
        clip_bytes = clip_path.read_bytes()
        if _sha256_bytes(clip_bytes) != manifest["context"]["conditioning_source_sha256"]:
            raise ReplacementExportError("conditioning source changed after export preparation")
        source = manifest.get("source") or {}
        source_path = self.music.store.root / pid / source.get("relative_path", "")
        if not source_path.is_file() or _sha256_bytes(source_path.read_bytes()) != source.get("audio_sha256"):
            raise ReplacementExportError("original source audio is missing or changed")
        clip, sample_rate, channels = _decode_pcm(clip_path)
        if output.sample_rate != sample_rate or output.channels != channels:
            raise ReplacementExportError("candidate sample rate/channels differ from the export source")
        if len(output.samples) != len(clip):
            raise ReplacementExportError(
                "candidate length differs from the fixed-duration condition source; "
                "refusing to stretch")
        start = int(round(manifest["handles"]["replacement_offset_in_export_s"] * sample_rate))
        end = int(round(manifest["handles"]["replacement_end_in_export_s"] * sample_rate))
        if start <= 0 or end <= start or end >= len(clip) // channels:
            raise ReplacementExportError("candidate replacement interval is outside the source")
        guard = int(round(0.05 * sample_rate))
        left = max(0, start - guard)
        right = min(len(clip) // channels, end + guard)
        preserved_before = output.samples[:left * channels] == clip[:left * channels]
        preserved_after = output.samples[right * channels:] == clip[right * channels:]
        if not (preserved_before and preserved_after):
            raise ReplacementExportError(
                "candidate changed PCM outside the replacement region; refusing manual splice")

        export_bytes = _wav_f32_bytes(output.samples, sample_rate, channels)
        replacement = array("f", output.samples[start * channels:end * channels])
        replacement_bytes = _wav_f32_bytes(replacement, sample_rate, channels)
        lead_in_bytes = _wav_f32_bytes(
            array("f", clip[:start * channels]), sample_rate, channels)
        lead_out_bytes = _wav_f32_bytes(
            array("f", clip[end * channels:]), sample_rate, channels)
        files = {
            "export_with_handles.wav": export_bytes,
            "replacement.wav": replacement_bytes,
            "lead_in_reference.wav": lead_in_bytes,
            "lead_out_reference.wav": lead_out_bytes,
        }
        for name, payload in files.items():
            (directory / name).write_bytes(payload)
        hashes = {
            "provider_output_sha256": _sha256_bytes(export_bytes),
            "export_sha256": _sha256_bytes(export_bytes),
            "replacement_sha256": _sha256_bytes(replacement_bytes),
            "lead_in_sha256": _sha256_bytes(lead_in_bytes),
            "lead_out_sha256": _sha256_bytes(lead_out_bytes),
        }
        manifest["status"] = "candidate"
        manifest["output"] = {
            "status": "candidate",
            **hashes,
            "provider_output_duration_s": round(len(output.samples) // channels / sample_rate, 3),
            "export_duration_s": round(len(output.samples) // channels / sample_rate, 3),
            "replacement_duration_s": round(len(replacement) // channels / sample_rate, 3),
            "sample_rate": sample_rate,
            "channels": channels,
            "files": {
                "export_with_handles.wav": "export_with_handles.wav",
                "replacement.wav": "replacement.wav",
                "lead_in_reference.wav": "lead_in_reference.wav",
                "lead_out_reference.wav": "lead_out_reference.wav",
                "conditioning_source.wav": "conditioning_source.wav",
            },
            "verification": {
                "pcm_preserved_before_replacement": preserved_before,
                "pcm_preserved_after_replacement": preserved_after,
                "guard_samples": guard,
                "source_interval": [manifest["handles"]["source_start_s"],
                                    manifest["handles"]["source_end_s"]],
                "export_interval": [manifest["handles"]["export_start_s"],
                                    manifest["handles"]["export_end_s"]],
            },
            "candidate_at": _utcnow().isoformat(),
        }
        manifest["authorization_request"]["output_sha256"] = hashes["export_sha256"]
        manifest["warnings"] = [
            warning for warning in manifest["warnings"]
            if warning != "lyrics completion is declared by review; the candidate has not been auditioned"
        ] + ["candidate packaged offline; no live submission or apply occurred"]
        self._write_json(directory / "manifest.json", manifest)
        (directory / "checksums.sha256").write_text(
            "".join(f"{hashes[key]}  {name}\n" for key, name in [
                ("export_sha256", "export_with_handles.wav"),
                ("replacement_sha256", "replacement.wav"),
                ("lead_in_sha256", "lead_in_reference.wav"),
                ("lead_out_sha256", "lead_out_reference.wav"),
            ]) + f"{manifest['context']['conditioning_source_sha256']}  conditioning_source.wav\n",
            encoding="utf-8",
        )
        return manifest

class ExtendedBridgeBlocked(Exception):
    pass


def _encode_mp3(master_wav: bytes, destination: Path, *,
                bitrate: str = "320k") -> dict[str, Any]:
    """Encode a lossy listening derivative without changing the lossless master."""
    ffmpeg = get_settings().ffmpeg.binary
    destination = Path(destination)
    if destination.exists():
        raise ExtendedBridgeBlocked("MP3 derivative already exists")
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "candidate_master_309s.wav"
        source.write_bytes(master_wav)
        proc = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(source), "-ar", "48000", "-ac", "2",
             "-codec:a", "libmp3lame", "-b:a", bitrate,
             str(destination)],
            capture_output=True,
        )
    if proc.returncode != 0 or not destination.is_file():
        raise ExtendedBridgeBlocked(
            "MP3 encoding failed: "
            + proc.stderr.decode("utf-8", "replace")[:500])
    payload = destination.read_bytes()
    return {
        "path": destination.name,
        "filename": destination.name,
        "codec": "libmp3lame",
        "bitrate": bitrate,
        "sample_rate": 48000,
        "channels": 2,
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
        "lossy": True,
        "pcm_equality_claims_apply": False,
        "note": "Lossy listening derivative; not a lossless PCM preservation claim.",
    }


def _extended_frame_index(seconds: float, sample_rate: int = 48000) -> int:
    return int(round(seconds * sample_rate))


def _extended_sample_index(seconds: float, sample_rate: int = 48000,
                            channels: int = 2) -> int:
    return _extended_frame_index(seconds, sample_rate) * channels


def assemble_extended_bridge(
    original: array, provider: array, *, sample_rate: int = 48000,
    channels: int = 2,
) -> array:
    """Assemble the documented 309s contract from original and 54s provider PCM."""
    source_frames = 300 * sample_rate
    provider_frames = 54 * sample_rate
    if len(original) != source_frames * channels:
        raise ReplacementExportError("original PCM is not 300s")
    if len(provider) != provider_frames * channels:
        raise ReplacementExportError("provider PCM is not 54s")
    out = array("f", original[:_extended_sample_index(185.0, sample_rate, channels)])
    start_n = (
        _extended_frame_index(185.05, sample_rate)
        - _extended_frame_index(185.0, sample_rate)
    )
    for n in range(start_n):
        alpha = n / max(start_n - 1, 1)
        oi = (_extended_frame_index(185.0, sample_rate) + n) * channels
        pi = (_extended_frame_index(3.0, sample_rate) + n) * channels
        for channel in range(channels):
            out.append(
                original[oi + channel] * (1.0 - alpha)
                + provider[pi + channel] * alpha)
    out.extend(provider[
        _extended_sample_index(3.05, sample_rate, channels):
        _extended_sample_index(50.95, sample_rate, channels)
    ])
    end_n = (
        _extended_frame_index(233.0, sample_rate)
        - _extended_frame_index(232.95, sample_rate)
    )
    for n in range(end_n):
        alpha = n / max(end_n - 1, 1)
        pi = (_extended_frame_index(50.95, sample_rate) + n) * channels
        oi = (_extended_frame_index(223.95, sample_rate) + n) * channels
        for channel in range(channels):
            out.append(
                provider[pi + channel] * (1.0 - alpha)
                + original[oi + channel] * alpha)
    out.extend(original[_extended_sample_index(224.0, sample_rate, channels):])
    if len(out) != 309 * sample_rate * channels:
        raise ReplacementExportError("assembled master is not 309s")
    return out


class ExtendedBridgeExportService:
    """Offline-only extended-Bridge plan, assembly, and package runner.

    This is not a web submission/apply path. The future live path remains
    fail-closed until a separate authorization/accounting source is approved.
    """

    def __init__(self, music=None, *, proposal_path: Path | None = None,
                 source_path: Path | None = None) -> None:
        self.music = music or get_music_service()
        root = Path(__file__).resolve().parents[3]
        self.proposal_path = proposal_path or (
            root / "docs" / "extended-bridge-audio-v2-proposal.json")
        self.source_path = source_path

    def live_blockers(self) -> list[str]:
        return [
            "No approved extended-Bridge live authorization/accounting source exists.",
            "Live gates remain false and no capacity/ownership gate has been exercised.",
            "Native model parity with the original ComfyUI AIO take is unverified.",
            "The real 54s provider output has not been auditioned or live-validated.",
            "No web submit or apply endpoint is enabled.",
        ]

    def execute_live(self, plan_dir: Path, *, authorization: Any = None,
                     client: AceRepaintClient | None = None) -> dict[str, Any]:
        plan_dir = Path(plan_dir)
        plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        pid = plan["project_id"]
        started = self.music.begin_extended_bridge_live(
            pid, plan_dir, authorization)
        attempt_id = started["attempt_id"]
        recorder = _NativeCaptureRecorder(self.music, pid, attempt_id)
        active = client or AceRepaintClient(
            get_settings().music.repaint_service_url,
            timeout_s=get_settings().ffmpeg.process_timeout_s)
        try:
            execution = active.execute_prepared(
                started["prepared"], recorder=recorder)
        except (NativeRepaintTimeout, httpx.TimeoutException,
                httpx.TransportError) as exc:
            reason = (
                "native extended-Bridge timeout/transport loss; provider stop is "
                f"not proven: {exc}")
            self.music._mark_native_uncertain(pid, attempt_id, reason)
            raise ExtendedBridgeBlocked(reason) from exc
        except httpx.HTTPStatusError as exc:
            reason = f"native extended-Bridge HTTP failure {exc.response.status_code}"
            self.music._mark_native_failed(pid, attempt_id, reason)
            raise ExtendedBridgeBlocked(reason) from exc
        except NativeRepaintFailed as exc:
            self.music._mark_native_failed(pid, attempt_id, str(exc))
            raise ExtendedBridgeBlocked(str(exc)) from exc
        except Exception as exc:
            reason = f"native extended-Bridge post-dispatch failure: {exc}"
            if recorder.dispatched:
                self.music._mark_native_uncertain(pid, attempt_id, reason)
            else:
                self.music._mark_native_failed(pid, attempt_id, reason)
            raise ExtendedBridgeBlocked(reason) from exc
        raw_path = plan_dir / "provider_output.raw"
        try:
            if raw_path.exists():
                if _sha256_bytes(raw_path.read_bytes()) != _sha256_bytes(
                        execution.raw_bytes):
                    raise ExtendedBridgeBlocked(
                        "existing raw provider output has a different identity")
            else:
                self.music._write_exclusive_bytes(raw_path, execution.raw_bytes)
            self.music._persist_native_outcome(pid, attempt_id, "completed")
            self.music._append_journal(pid, {
                "kind": "attempt_completed",
                "attempt_id": attempt_id,
                "at": _utcnow().isoformat(),
                "provider": NATIVE_PROVIDER,
                "task": "extended_bridge",
                "task_id": execution.task_id,
                "sha256": _sha256_bytes(execution.raw_bytes),
                "provider_output_sha256": _sha256_bytes(execution.raw_bytes),
                **self.music._request_link(pid, attempt_id),
            })
        except Exception as exc:
            reason = f"extended-Bridge post-dispatch recording failure: {exc}"
            self.music._mark_native_uncertain(pid, attempt_id, reason)
            raise ExtendedBridgeBlocked(reason) from exc
        execution_files = {
            path.name: path
            for path in self.music._execution_dir(pid).glob(f"{attempt_id}.*")
            if path.is_file()
        }
        try:
            package = self.package_candidate(
                plan_dir,
                execution.output,
                synthetic=False,
                raw_provider_bytes=execution.raw_bytes,
                label="live native ACE-Step provider output",
                live_execution_records=True,
                execution_attempt_id=attempt_id,
                execution_files=execution_files,
            )
        except Exception as exc:
            self.music._append_native_event(
                pid, attempt_id, "packaging_failed", {"error": str(exc)[:1000]})
            return {
                "execution_status": "completed",
                "packaging_status": "failed",
                "attempt_id": attempt_id,
                "task_id": execution.task_id,
                "raw_provider_output": str(raw_path),
                "raw_provider_output_sha256": _sha256_bytes(execution.raw_bytes),
                "execution_links": self.music._request_link(pid, attempt_id),
                "packaging_error": str(exc)[:1000],
            }
        return {
            "execution_status": "completed",
            "packaging_status": "completed",
            "attempt_id": attempt_id,
            "task_id": execution.task_id,
            "package_path": package["package_path"],
            "package_sha256": package["package_sha256"],
            "candidate_master_sha256": package["hashes"]["candidate_master_309s.wav"],
            "review_sha256": package["hashes"]["review_54s.wav"],
            "mp3_sha256": package["hashes"].get("candidate_master_309s.mp3"),
            "raw_provider_output": str(raw_path),
            "execution_links": self.music._request_link(pid, attempt_id),
        }

    def _validate_source(self, pid: str) -> dict[str, Any]:
        state = self.music._load_state(pid)
        journal = self.music._journal_state(pid)
        started = self.music._started_rows(journal)
        completed = next(
            (row for row in journal["rows"]
             if row.get("kind") == "attempt_completed"
             and row.get("asset_id") == "0f26194c08"),
            None,
        )
        if completed is None:
            raise ExtendedBridgeBlocked("source take 0f26194c08 is not completed")
        attempt = completed.get("attempt_id")
        start_row = started.get(attempt) or {}
        if attempt != "519bd4ccf1":
            raise ExtendedBridgeBlocked("source attempt is not 519bd4ccf1")
        if start_row.get("revision") != 3:
            raise ExtendedBridgeBlocked("source draft revision is not 3")
        source = self.source_path or self.music.store.asset_path(pid, "0f26194c08")
        if not source.is_file():
            raise ExtendedBridgeBlocked("source audio is missing")
        actual = _sha256_bytes(source.read_bytes())
        expected = "d26450aae581bfc2a585dcd9811e2b9af8dbee70ecf19027b56a6ec4920bdb63"
        if actual != expected:
            raise ExtendedBridgeBlocked("source audio SHA-256 is stale")
        boundaries = self.music.section_boundaries(pid, "0f26194c08")["boundaries"]
        live = {
            item["section"]: [item["start_s"], item["end_s"]]
            for item in boundaries
            if item["section"] in {"Bridge", "Final Chorus", "Outro"}
        }
        expected_live = {
            "Bridge": [185.0, 224.0],
            "Final Chorus": [224.0, 285.0],
            "Outro": [285.0, 300.0],
        }
        if live != expected_live:
            raise ExtendedBridgeBlocked(
                f"live source markers differ from the authoritative map: {live}")
        return {
            "source_path": str(source),
            "source_sha256": actual,
            "live_markers": expected_live,
            "attempt": attempt,
            "revision": 3,
        }

    def _proposal(self) -> dict[str, Any]:
        if not self.proposal_path.is_file():
            raise ExtendedBridgeBlocked("authoritative proposal is missing")
        proposal = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        if proposal.get("project_id") != "ac668b7c7faa":
            raise ExtendedBridgeBlocked("proposal project identity is wrong")
        if proposal.get("audio_version") != 2:
            raise ExtendedBridgeBlocked("proposal audio version is not 2")
        if proposal["source_audio_version"].get("source_draft_revision") != 3:
            raise ExtendedBridgeBlocked("proposal source draft revision is not 3")
        if proposal["source_audio_version"].get("sha256") != (
                "d26450aae581bfc2a585dcd9811e2b9af8dbee70ecf19027b56a6ec4920bdb63"):
            raise ExtendedBridgeBlocked("proposal source SHA-256 is wrong")
        corrected = {
            item["section"]: [item["start_s"], item["end_s"]]
            for item in proposal["marker_history"]["corrected_source_map"]
            if item["section"] in {"Bridge", "Final Chorus", "Outro"}
        }
        active = {
            "Bridge": [185.0, 224.0],
            "Final Chorus": [224.0, 285.0],
            "Outro": [285.0, 300.0],
        }
        if corrected != active:
            raise ExtendedBridgeBlocked("active corrected marker map is inconsistent")
        original_timeline = {
            item["section"]: [item["start_s"], item["end_s"]]
            for item in proposal["audio_timelines"]["original"]["markers"]
            if item["section"] in {"Bridge", "Final Chorus", "Outro"}
        }
        if original_timeline != active:
            raise ExtendedBridgeBlocked(
                "audio_timelines.original.markers is not the active corrected map")
        planned = {
            item["section"]: [item["start_s"], item["end_s"]]
            for item in proposal["audio_timelines"]["planned_v2"]["markers"]
            if item["section"] in {"Bridge", "Final Chorus", "Outro"}
        }
        expected_planned = {
            "Bridge": [185.0, 233.0],
            "Final Chorus": [233.0, 294.0],
            "Outro": [294.0, 309.0],
        }
        if planned != expected_planned:
            raise ExtendedBridgeBlocked("planned candidate marker map is inconsistent")
        if proposal.get("verification", {}).get("status") != "verified_offline":
            raise ExtendedBridgeBlocked("proposal is not offline verified")
        if proposal.get("verification", {}).get("failed_checks"):
            raise ExtendedBridgeBlocked("proposal verification has failures")
        lyrics = proposal["planned_v2"].get("lyrics") or []
        if len(lyrics) != 8:
            raise ExtendedBridgeBlocked("the proposal must contain all eight Bridge lines")
        guidance = proposal["conditioning_inputs"].get("performance_guidance")
        if not isinstance(guidance, str) or not guidance.strip():
            raise ExtendedBridgeBlocked("performance guidance is missing")
        clip = proposal["splice_contract"]["conditioning_source"]
        mask = proposal["splice_contract"]["provider_mask"]
        if [clip["start_s"], clip["end_s"], clip["duration_s"]] != [182.0, 236.0, 54.0]:
            raise ExtendedBridgeBlocked("conditioning clip contract is wrong")
        if mask["relative_s"] != [3.0, 51.0] or mask["full_source_s"] != [185.0, 233.0]:
            raise ExtendedBridgeBlocked("provider mask contract is wrong")
        return proposal

    def _environment(self, proposal: dict[str, Any]) -> dict[str, Any]:
        model = proposal["native_model_identity"]["native"]
        ffmpeg = get_settings().ffmpeg.binary
        try:
            version = subprocess.run(
                [ffmpeg, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
        except Exception as exc:
            version = f"unavailable: {exc}"
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
                **model,
                "basis": "previously_recorded_proposal_identity",
                "status": "previously_recorded",
                "freshly_verified": False,
                "loaded_bytes_verified": False,
                "filename_or_manifest_is_not_proof_of_loaded_bytes": True,
            },
            "original_comfyui_take": proposal["native_model_identity"]["original_take"],
            "model_parity": proposal["native_model_identity"]["parity"],
            "code": {
                "commit": model.get("upstream_commit"),
                "status": "previously_recorded",
                "basis": "authoritative proposal",
                "freshly_verified": False,
            },
            "decode": {
                "tool": ffmpeg,
                "tool_version": version,
                "input": "original MP3",
                "output": "48kHz stereo f32le",
                "sample_rate": 48000,
                "channels": 2,
            },
            "runtime": {
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "native_python_version": None,
                "torch_version": None,
                "cuda_version": None,
                "basis": "not contacted; native process runtime unknown",
            },
            "evidence_status": {
                "model": "previously_recorded",
                "code_commit": "previously_recorded",
                "loaded_model_bytes": "unverified",
                "service_reachability": "unknown",
                "decode_tool": "freshly_observed",
            },
        }

    def plan(self, pid: str, *, output_root: Path) -> dict[str, Any]:
        source = self._validate_source(pid)
        proposal = self._proposal()
        project_root = (self.music.store.root / pid).resolve()
        out_root = Path(output_root).resolve()
        if out_root == project_root or out_root.is_relative_to(project_root):
            raise ExtendedBridgeBlocked(
                "extended-Bridge plan output must be outside the protected project")
        samples, sample_rate, channels = _decode_pcm(Path(source["source_path"]))
        if (sample_rate, channels) != (48000, 2) or len(samples) != 300 * sample_rate * channels:
            raise ExtendedBridgeBlocked("source did not decode to 300s 48kHz stereo")
        working = array("f", samples[:_extended_sample_index(224.0, sample_rate, channels)])
        working.extend(array("f", [0.0]) * (9 * sample_rate * channels))
        working.extend(samples[_extended_sample_index(224.0, sample_rate, channels):])
        clip = array("f", working[
            _extended_sample_index(182.0, sample_rate, channels):
            _extended_sample_index(236.0, sample_rate, channels)
        ])
        working_sha = _sha256_bytes(_wav_f32_bytes(working, sample_rate, channels))
        clip_sha = _sha256_bytes(_wav_f32_bytes(clip, sample_rate, channels))
        expected_working = proposal["hashes"]["working_source_sha256"]
        expected_clip = proposal["hashes"]["conditioning_clip_sha256"]
        if working_sha != expected_working:
            raise ExtendedBridgeBlocked(
                "reconstructed working source SHA-256 differs from the proposal")
        if clip_sha != expected_clip:
            raise ExtendedBridgeBlocked(
                "reconstructed conditioning SHA-256 differs from the proposal")
        plan_id = uuid.uuid4().hex[:12]
        plan_dir = out_root / f"extended-bridge-{proposal['proposal_id']}-{plan_id}"
        plan_dir.mkdir(parents=True, exist_ok=False)
        conditioning_path = plan_dir / "native.conditioning.wav"
        conditioning_path.write_bytes(_wav_f32_bytes(clip, sample_rate, channels))
        lyrics = "\n".join(proposal["planned_v2"]["lyrics"])
        guidance = proposal["conditioning_inputs"]["performance_guidance"]
        settings = {
            "lyrics": lyrics,
            "seed": proposal["conditioning_inputs"]["seed"],
            "bpm": proposal["conditioning_inputs"]["bpm"],
            "key_scale": proposal["conditioning_inputs"]["key_scale"],
            "time_signature": proposal["conditioning_inputs"]["time_signature"],
            "language": proposal["conditioning_inputs"]["language"],
            "duration_s": 54.0,
        }
        request = RepaintRequest(
            source_path=conditioning_path,
            source_sha256=clip_sha,
            start_s=3.0,
            end_s=51.0,
            prompt=guidance,
            settings=settings,
            output_format="wav32",
            lyrics=lyrics,
            seed=settings["seed"],
            bpm=settings["bpm"],
            key_scale=settings["key_scale"],
            time_signature=settings["time_signature"],
            language=settings["language"],
        )
        prepared = build_native_multipart(request, plan_dir / "native.body")
        environment = self._environment(proposal)
        env_path = plan_dir / "native.environment.json"
        env_path.write_text(json.dumps(environment, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8", newline="")
        request_record = {
            "schema": NATIVE_REQUEST_SCHEMA,
            "provider": NATIVE_PROVIDER,
            "provider_kind": "native_ace_step_repaint",
            "explicitly_not_comfyui": True,
            "request_id": plan_id,
            "attempt_id": None,
            "project_id": pid,
            "source_revision": 3,
            "created_at": _utcnow().isoformat(),
            "endpoint": f"{get_settings().music.repaint_service_url.rstrip('/')}/release_task",
            "method": "POST",
            "poll_endpoint": f"{get_settings().music.repaint_service_url.rstrip('/')}/query_result",
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
                "asset_id": "0f26194c08",
                "attempt_id": "519bd4ccf1",
                "revision": 3,
                "sha256": source["source_sha256"],
            },
            "conditioning": {
                "filename": conditioning_path.name,
                "relative_path": "native.conditioning.wav",
                "sha256": clip_sha,
                "bytes": conditioning_path.stat().st_size,
                "duration_s": 54.0,
                "sample_rate": sample_rate,
                "channels": channels,
                "content_type": "audio/wav",
                "file_field": "src_audio",
                "repaint_start_s": 3.0,
                "repaint_end_s": 51.0,
                "mask_full_source_s": [185.0, 233.0],
                "source_mapping": {
                    "clip_start_s": 182.0,
                    "clip_end_s": 236.0,
                    "mask_start_s": 185.0,
                    "mask_end_s": 233.0,
                    "clip_offset_s": 182.0,
                    "conditioning_is_full_source": False,
                },
                "working_source_sha256": working_sha,
                "settings": settings,
                "lyrics": lyrics,
                "lyrics_sha256": _sha256_bytes(lyrics.encode("utf-8")),
                "repaint_mode": prepared.form.get("repaint_mode"),
                "audio_cover_strength": prepared.form.get("audio_cover_strength"),
                "chunk_mask_mode": prepared.form.get("chunk_mask_mode"),
                "output_format": prepared.form.get("audio_format"),
            },
            "input_files": [{
                "field": "src_audio",
                "filename": conditioning_path.name,
                "content_type": "audio/wav",
                "sha256": clip_sha,
                "bytes": conditioning_path.stat().st_size,
                "relative_path": "native.conditioning.wav",
            }],
            "missing_input_files": [],
            "request_body_path": "native.body",
            "request_body_sha256": prepared.body_sha256,
            "request_body_bytes": prepared.body_bytes,
            "environment": environment,
            "environment_path": "native.environment.json",
            "accounting": {
                "status": "dry_run_not_started",
                "allowance_consumed": False,
                "attempt_started": False,
            },
            "cancellation": {
                "supported": False,
                "status": "unavailable",
                "reason": "The authoritative native API exposes no task cancellation endpoint.",
            },
            "capture_status": "dry_run_not_dispatched",
        }
        request_path = plan_dir / "native.request.json"
        request_path.write_text(json.dumps(request_record, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8", newline="")
        assembly = {
            "candidate_duration_s": 309.0,
            "review_window_s": [182.0, 236.0],
            "start_transition_s": [185.0, 185.05],
            "pure_replacement_s": [185.05, 232.95],
            "end_transition_s": [232.95, 233.0],
            "original_preserved_s": [[0.0, 185.0], [224.0, 300.0]],
            "candidate_shift_s": 9.0,
            "provider_context_handles_used_wholesale": False,
        }
        plan = {
            "schema": "song_writer.extended_bridge_plan.v1",
            "plan_id": plan_id,
            "created_at": _utcnow().isoformat(),
            "status": "planned_not_dispatched",
            "project_id": pid,
            "source": source,
            "proposal": {
                "path": str(self.proposal_path),
                "sha256": _sha256_bytes(self.proposal_path.read_bytes()),
                "proposal_id": proposal["proposal_id"],
            },
            "timeline": {
                "source_markers": proposal["marker_history"]["corrected_source_map"],
                "planned_markers": proposal["audio_timelines"]["planned_v2"]["markers"],
                "audio_version": 2,
                "source_draft_revision": 3,
            },
            "conditioning": request_record["conditioning"],
            "native_request": {
                "path": "native.request.json",
                "body_path": "native.body",
                "body_sha256": prepared.body_sha256,
                "body_bytes": prepared.body_bytes,
                "environment_path": "native.environment.json",
            },
            "assembly": assembly,
            "lyrics": proposal["planned_v2"]["lyrics"],
            "performance_guidance": guidance,
            "provider_contacted": False,
            "allowance_consumed": False,
            "project_mutated": False,
            "live_blockers": self.live_blockers(),
            "actual_candidate": "not_recorded",
        }
        plan_path = plan_dir / "plan.json"
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8", newline="")
        return {
            "plan": plan,
            "plan_dir": str(plan_dir),
            "plan_sha256": _sha256_bytes(plan_path.read_bytes()),
            "conditioning_path": str(conditioning_path),
            "conditioning_sha256": clip_sha,
            "request_body_sha256": prepared.body_sha256,
            "request_body_bytes": prepared.body_bytes,
        }

    def package_candidate(
        self, plan_dir: Path, output: RepaintOutput, *,
        synthetic: bool, raw_provider_bytes: bytes | None = None,
        label: str = "", include_mp3: bool = True,
        mp3_bitrate: str = "320k",
        live_execution_records: bool = False,
        execution_attempt_id: str | None = None,
        execution_files: dict[str, Path] | None = None,
    ) -> dict[str, Any]:
        if synthetic and live_execution_records:
            raise ExtendedBridgeBlocked(
                "synthetic packaging cannot claim live execution records")
        if live_execution_records and not execution_files:
            raise ExtendedBridgeBlocked(
                "live packaging requires actual execution record files")
        plan_dir = Path(plan_dir)
        plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        if plan.get("status") != "planned_not_dispatched":
            raise ExtendedBridgeBlocked("plan is not a dry-run plan")
        source_path = Path(plan["source"]["source_path"])
        if _sha256_bytes(source_path.read_bytes()) != plan["source"]["source_sha256"]:
            raise ExtendedBridgeBlocked("plan source is stale")
        clip_path = plan_dir / "native.conditioning.wav"
        clip_sha = _sha256_bytes(clip_path.read_bytes())
        if clip_sha != plan["conditioning"]["sha256"]:
            raise ExtendedBridgeBlocked("conditioning bytes changed after planning")
        body_path = plan_dir / "native.body"
        if _sha256_bytes(body_path.read_bytes()) != plan["native_request"]["body_sha256"]:
            raise ExtendedBridgeBlocked("request body changed after planning")
        original, sample_rate, channels = _decode_pcm(source_path)
        if (sample_rate, channels) != (48000, 2):
            raise ExtendedBridgeBlocked("source format is not 48kHz stereo")
        if output.sample_rate != sample_rate or output.channels != channels:
            raise ExtendedBridgeBlocked("provider output rate/channels are incompatible")
        if len(output.samples) != 54 * sample_rate * channels:
            raise ExtendedBridgeBlocked("provider output is not exactly 54s")
        if not all(math.isfinite(float(value)) for value in output.samples):
            raise ExtendedBridgeBlocked("provider output contains non-finite samples")
        provider = array("f", output.samples)
        master = assemble_extended_bridge(
            original, provider, sample_rate=sample_rate, channels=channels)
        if master[:185 * sample_rate * channels] != original[:185 * sample_rate * channels]:
            raise ExtendedBridgeBlocked("candidate changed the pre-Bridge region")
        if master[233 * sample_rate * channels:] != original[224 * sample_rate * channels:]:
            raise ExtendedBridgeBlocked("candidate did not preserve shifted Final Chorus/Outro")
        review = array("f", master[
            _extended_sample_index(182.0, sample_rate, channels):
            _extended_sample_index(236.0, sample_rate, channels)
        ])
        raw = raw_provider_bytes if raw_provider_bytes is not None else _wav_f32_bytes(
            provider, sample_rate, channels)
        master_wav = _wav_f32_bytes(master, sample_rate, channels)
        review_wav = _wav_f32_bytes(review, sample_rate, channels)
        files: dict[str, bytes] = {
            "plan.json": (plan_dir / "plan.json").read_bytes(),
            "native.request.json": (plan_dir / "native.request.json").read_bytes(),
            "native.body": body_path.read_bytes(),
            "native.conditioning.wav": clip_path.read_bytes(),
            "native.environment.json": (plan_dir / "native.environment.json").read_bytes(),
            "provider_output.raw": raw,
            "review_54s.wav": review_wav,
            "candidate_master_309s.wav": master_wav,
        }
        mp3_meta = None
        if include_mp3:
            mp3_meta = _encode_mp3(
                master_wav, plan_dir / "candidate_master_309s.mp3",
                bitrate=mp3_bitrate)
            files["candidate_master_309s.mp3"] = (
                plan_dir / "candidate_master_309s.mp3").read_bytes()
        execution_names: list[str] = []
        if live_execution_records:
            for name, path in sorted((execution_files or {}).items()):
                path = Path(path)
                if not path.is_file():
                    raise ExtendedBridgeBlocked(
                        f"execution record is missing: {name}")
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)
                arcname = f"execution-records/{safe}"
                files[arcname] = path.read_bytes()
                execution_names.append(arcname)
        mapping = {
            "source": {
                "asset_id": "0f26194c08",
                "attempt_id": "519bd4ccf1",
                "revision": 3,
                "sha256": plan["source"]["source_sha256"],
            },
            "conditioning": plan["conditioning"],
            "candidate": {
                "duration_s": 309.0,
                "sample_rate": sample_rate,
                "channels": channels,
                "frames": len(master) // channels,
                "sha256": _sha256_bytes(_wav_f32_bytes(master, sample_rate, channels)),
            },
            "review": {
                "start_s": 182.0,
                "end_s": 236.0,
                "duration_s": 54.0,
                "sha256": _sha256_bytes(_wav_f32_bytes(review, sample_rate, channels)),
            },
            "assembly": plan["assembly"],
            "pcm_checks": {
                "pre_bridge_original_preserved": True,
                "final_chorus_224_285_maps_233_294": True,
                "outro_285_300_maps_294_309": True,
                "provider_context_handles_used_wholesale": False,
            },
        }
        files["mapping.json"] = json.dumps(
            mapping, indent=2, sort_keys=True).encode("utf-8")
        files["provider_output.meta.json"] = json.dumps({
            "synthetic_fixture": synthetic,
            "label": label or ("synthetic contract fixture" if synthetic else "provider output"),
            "duration_s": 54.0,
            "sample_rate": sample_rate,
            "channels": channels,
            "sha256": _sha256_bytes(raw),
            "bytes": len(raw),
            "decoded_sha256": _sha256_bytes(_wav_f32_bytes(provider, sample_rate, channels)),
            "actual_model_output": not synthetic,
        }, indent=2, sort_keys=True).encode("utf-8")
        files["assembly-recipe.txt"] = (
            "Extended-Bridge 309s assembly\n"
            "0-185s: original PCM\n"
            "185.000-185.050s: linear crossfade original -> provider\n"
            "185.050-232.950s: provider replacement\n"
            "232.950-233.000s: linear crossfade provider -> original shifted\n"
            "233-309s: original 224-300s shifted +9s\n"
            "Provider context handles are not used wholesale.\n"
            "PCM equality applies to lossless decoded assembly only.\n"
        ).encode("utf-8")
        file_hashes = {name: _sha256_bytes(payload) for name, payload in files.items()}
        manifest = {
            "schema": "song_writer.extended_bridge_package.v1",
            "plan_id": plan["plan_id"],
            "created_at": _utcnow().isoformat(),
            "status": "candidate_packaged",
            "provider_kind": "native_ace_step_repaint",
            "explicitly_not_comfyui": True,
            "synthetic_fixture": synthetic,
            "provider_output_label": label or (
                "synthetic contract fixture" if synthetic else "provider output"),
            "actual_provider_output_claimed": not synthetic,
            "live_execution_records": live_execution_records,
            "execution_attempt_id": execution_attempt_id,
            "execution_records": execution_names,
            "linked_plan_records": [
                "plan.json", "native.request.json", "native.body",
                "native.conditioning.wav", "native.environment.json",
            ],
            "mp3_derivative": mp3_meta,
            "package_files": file_hashes,
            "excluded": [
                "approval", "audio_approval", "submission_cap", "credentials",
                "other_projects", "other_assets", "preferred_selection",
                "authorization_signature", "launch_claims",
            ],
            "apply_allowed": False,
            "project_mutated": False,
            "model_parity": "unverified",
            "provider_context_handles_used_wholesale": False,
            "notes": [
                "No provider was contacted and no model was loaded.",
                "This is an offline candidate package, not a live generation record.",
                "Lossless PCM checks do not apply to any re-encoded lossy derivative.",
            ],
        }
        files["manifest.json"] = json.dumps(
            manifest, indent=2, sort_keys=True).encode("utf-8")
        all_hashes = {name: _sha256_bytes(payload) for name, payload in files.items()}
        files["hashes.sha256"] = ("".join(
            f"{digest}  {name}\n" for name, digest in sorted(all_hashes.items())
        )).encode("utf-8")
        package_path = plan_dir / "extended-bridge-package.zip"
        if package_path.exists():
            raise ExtendedBridgeBlocked("package already exists; refusing to overwrite")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(files.items()):
                archive.writestr(name, payload)
        package_path.write_bytes(buffer.getvalue())
        result = dict(manifest)
        result["package_path"] = str(package_path)
        result["package_sha256"] = _sha256_bytes(buffer.getvalue())
        result["package_bytes"] = len(buffer.getvalue())
        result["hashes"] = all_hashes
        (plan_dir / "package-manifest.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="")
        return result


_service: ReplacementExportService | None = None


def get_replacement_export_service() -> ReplacementExportService:
    global _service
    if _service is None:
        _service = ReplacementExportService()
    return _service


def reset_replacement_export_service() -> None:
    global _service
    _service = None
