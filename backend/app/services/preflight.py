"""Runtime readiness checks.  This is deliberately read-only and safe to call often."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.comfyui_client import ComfyUIClient
from app.services.ffmpeg_runner import FFmpegRunner
from app.services.lmstudio_client import LMStudioClient

# Leave sufficient room for temporary media without rejecting smaller dev disks.
MIN_PROJECT_DISK_FREE_BYTES = 1 * 1024 ** 3


class RuntimePreflight:
    def __init__(self, *, lmstudio: LMStudioClient, comfyui: ComfyUIClient,
                 tts: Any, ffmpeg: FFmpegRunner) -> None:
        self.lmstudio, self.comfyui, self.tts, self.ffmpeg = lmstudio, comfyui, tts, ffmpeg

    @staticmethod
    def _check(ok: bool, **details: Any) -> dict[str, Any]:
        return {"ok": ok, **details}

    def _paths(self) -> dict[str, Any]:
        cfg = get_settings()
        root = cfg.projects_root_path
        errors: list[str] = []
        try:
            root.mkdir(parents=True, exist_ok=True)
            # os.access is advisory on Windows and can report writable for an
            # ACL-denied directory. Exercise an actual create/delete instead.
            probe = root / f".preflight-write-{os.getpid()}"
            probe.write_bytes(b"ok")
            probe.unlink()
            writable = True
            free = shutil.disk_usage(root).free
        except OSError as exc:
            writable, free = False, 0
            errors.append(str(exc))
        if not writable:
            errors.append(f"projects_root is not writable: {root}")
        if free < MIN_PROJECT_DISK_FREE_BYTES:
            errors.append(f"projects_root has less than the required free space ({free} bytes)")
        workflow_paths: dict[str, str] = {}
        for role, name in self.comfyui.configured_workflows().items():
            # Keep the path calculation centralized with the actual workflow loader.
            from app.services.comfyui_client import WORKFLOWS_DIR
            candidate = WORKFLOWS_DIR / f"{name}.json"
            workflow_paths[role] = str(candidate)
            if not candidate.is_file():
                errors.append(f"workflow file missing: {candidate}")
        return self._check(not errors, projects_root=str(root), writable=writable,
                           free_bytes=free, min_free_bytes=MIN_PROJECT_DISK_FREE_BYTES,
                           workflows=workflow_paths, errors=errors)

    async def _lmstudio(self) -> dict[str, Any]:
        status = await self.lmstudio.status()
        models = status.get("loaded_models", [])
        required = [self.lmstudio.default_model, self.lmstudio.vision_model]
        unavailable = [model for model in required if model not in models]
        return self._check(bool(status.get("online")) and not unavailable, status=status,
                           required_models=required, unavailable_models=unavailable)

    async def _comfyui(self) -> dict[str, Any]:
        status = await self.comfyui.status()
        local: dict[str, Any] = {}
        errors: list[str] = []
        for role, name in self.comfyui.configured_workflows().items():
            try:
                local[role] = self.comfyui.workflow_contract(name)
            except Exception as exc:
                errors.append(f"{role}: {exc}")
        if not status.get("online"):
            return self._check(False, status=status, workflows=local,
                               errors=errors + ["ComfyUI is unreachable; node/model inventory was not checked"])
        try:
            object_info = await self.comfyui.object_info()
        except Exception as exc:
            return self._check(False, status=status, workflows=local,
                               errors=errors + [f"could not fetch /object_info: {exc}"])
        for role, contract in local.items():
            missing_classes = [name for name in contract["node_classes"] if name not in object_info]
            if missing_classes:
                errors.append(f"{role}: missing node classes: {', '.join(missing_classes)}")
            for requirement in contract["models"]:
                cls = object_info.get(requirement["class_type"], {})
                inputs = cls.get("input", {}).get("required", {})
                spec = inputs.get(requirement["input"])
                choices = spec[0] if isinstance(spec, list) and spec and isinstance(spec[0], list) else None
                if choices is None:
                    errors.append(f"{role}: cannot verify {requirement['input']} on {requirement['class_type']}")
                elif requirement["filename"] not in choices:
                    errors.append(f"{role}: missing model {requirement['filename']} for {requirement['class_type']}.{requirement['input']}")
        return self._check(not errors, status=status, workflows=local, errors=errors)

    async def run(self) -> dict[str, Any]:
        ffmpeg_status = await self.ffmpeg.status()
        # Current FFmpegRunner always exposes feature inventory.  Preserve
        # compatibility with narrow third-party/test runner implementations
        # that predate it; production runner output is never treated as absent.
        features = ffmpeg_status.get("features")
        required = ("libx264", "aac", "ass", "loudnorm")
        missing = [name for name in required if not features.get(name)] if isinstance(features, dict) else []
        ready_ffmpeg = bool(ffmpeg_status.get("online")) and bool(ffmpeg_status.get("ffprobe_available")) and not missing
        errors = [] if ready_ffmpeg else ["ffmpeg and ffprobe must both be executable"]
        if missing: errors.append("required FFmpeg encoder/filter unavailable: " + ", ".join(missing))
        ffmpeg = self._check(ready_ffmpeg, status=ffmpeg_status, errors=errors)
        paths = self._paths()
        lmstudio = await self._lmstudio()
        tts_status = await self.tts.status()
        tts = self._check(bool(tts_status.get("online")), status=tts_status,
                          errors=[] if tts_status.get("online") else ["TTS service is unreachable"])
        comfyui = await self._comfyui()
        checks = {"paths": paths, "ffmpeg": ffmpeg, "lmstudio": lmstudio, "tts": tts, "comfyui": comfyui}
        return {"ready": all(item["ok"] for item in checks.values()), "checks": checks}
