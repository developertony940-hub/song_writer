"""ComfyUI client (image + video generation).

ComfyUI exposes a queue/poll API at http://127.0.0.1:8188. We submit a
workflow JSON (prompt graph) to /prompt, then poll /history/{id} until the
output artifacts appear, then fetch each via /view.

This module ships with the **workflow contract** for three job types but the
actual workflow JSON files live under comfyui/workflows/ and are loaded at
runtime. When ComfyUI is not installed, endpoints degrade gracefully and
return a structured "offline" status so the rest of the pipeline can run
(LLM copy + TTS still work without ComfyUI).
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / "comfyui" / "workflows"


class ComfyUIError(RuntimeError):
    pass


class ComfyUICancelled(ComfyUIError):
    """A bounded music submission was interrupted; terminal state is uncertain."""

    def __init__(self, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


class ComfyUIClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        cfg = get_settings().comfyui
        self.base_url = cfg.base_url.rstrip("/")
        self.poll_interval = cfg.poll_interval_s
        self.timeout = cfg.request_timeout_s
        self.workflow_image = cfg.workflow_image
        self.workflow_image_img2img = cfg.workflow_image_img2img
        self.workflow_image_pulid = cfg.workflow_image_pulid
        self.workflow_i2v = cfg.workflow_i2v
        self.workflow_t2v = cfg.workflow_t2v
        self.workflow_upscale = cfg.workflow_upscale
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- status ------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        try:
            r = await self._client.get("/system_stats")
            r.raise_for_status()
            data = r.json()
            return {
                "online": True,
                "base_url": self.base_url,
                "gpus": data.get("devices", []),
                "workflows": {
                    "image": self.workflow_image,
                    "i2v": self.workflow_i2v,
                    "t2v": self.workflow_t2v,
                },
            }
        except Exception as e:
            return {"online": False, "base_url": self.base_url, "error": str(e)}

    # --- workflow loading and contract inspection -------------------
    def configured_workflows(self) -> dict[str, str]:
        """Configured, enabled workflow roles. Empty names are disabled features."""
        candidates = {
            "image": self.workflow_image,
            "image_img2img": self.workflow_image_img2img,
            "image_pulid": self.workflow_image_pulid,
            "i2v": self.workflow_i2v,
            "t2v": self.workflow_t2v,
            "upscale": self.workflow_upscale,
        }
        return {role: name for role, name in candidates.items() if name}

    def workflow_contract(self, name: str) -> dict[str, Any]:
        """Extract native node classes and model filenames from an API graph."""
        workflow = self._load_workflow(name)
        nodes = [node for key, node in workflow.items() if not key.startswith("_") and isinstance(node, dict)]
        classes = sorted({str(node.get("class_type", "")) for node in nodes if node.get("class_type")})
        model_input_keys = {"unet_name", "clip_name", "clip_name1", "clip_name2", "vae_name", "ckpt_name"}
        models: list[dict[str, str]] = []
        for node in nodes:
            class_type = str(node.get("class_type", ""))
            for key, value in node.get("inputs", {}).items():
                if key in model_input_keys and isinstance(value, str):
                    models.append({"class_type": class_type, "input": key, "filename": value})
        return {"name": name, "node_classes": classes, "models": models}

    async def object_info(self) -> dict[str, Any]:
        response = await self._client.get("/object_info")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ComfyUIError("ComfyUI /object_info returned a non-object response")
        return payload

    # --- workflow loading -------------------------------------------
    def _load_workflow(self, name: str) -> dict[str, Any]:
        path = WORKFLOWS_DIR / f"{name}.json"
        if not path.exists():
            raise ComfyUIError(
                f"Workflow '{name}' not found at {path}. Create it in ComfyUI's UI "
                f"and export as API JSON, then save to comfyui/workflows/{name}.json."
            )
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _patch_workflow(workflow: dict[str, Any], patches: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Mutate node inputs. `patches` maps node_id -> {input_name: value}.
        Also strips non-node keys (like `_comment`) and ensures each node
        has a `_meta` dict — ComfyUI v0.33+ crashes on missing `_meta`."""
        out = json.loads(json.dumps(workflow))  # deep copy
        for key in list(out.keys()):
            if key.startswith("_"):
                del out[key]
        for node_id, node in out.items():
            if isinstance(node, dict) and "_meta" not in node:
                node["_meta"] = {}
        for node_id, inputs in patches.items():
            node = out.get(str(node_id))
            if node is None:
                continue
            node.setdefault("inputs", {}).update(inputs)
        return out

    # --- generation --------------------------------------------------
    async def _upload_image(self, image_bytes: bytes, filename: str | None = None) -> str:
        """Upload an image to ComfyUI's input folder and return the filename."""
        fname = filename or f"upload_{uuid.uuid4().hex[:8]}.png"
        files = {"image": (fname, image_bytes, "image/png")}
        r = await self._client.post("/upload/image", files=files)
        if r.status_code >= 400:
            raise ComfyUIError(f"Image upload failed: {r.text}")
        return r.json().get("name", fname)

    async def _queue_prompt(self, workflow: dict[str, Any],
                            client_id: str | None = None) -> str:
        payload = {"prompt": workflow, "client_id": client_id or str(uuid.uuid4())}
        r = await self._client.post("/prompt", json=payload)
        if r.status_code >= 400:
            raise ComfyUIError(f"ComfyUI rejected workflow: {r.text}")
        return r.json()["prompt_id"]

    async def _wait(self, prompt_id: str, timeout_s: int | None = None) -> dict[str, Any]:
        deadline = time.time() + (timeout_s or self.timeout)
        while time.time() < deadline:
            r = await self._client.get(f"/history/{prompt_id}")
            r.raise_for_status()
            hist = r.json()
            if prompt_id in hist:
                return hist[prompt_id]
            await asyncio.sleep(self.poll_interval)
        raise ComfyUIError(f"Timed out waiting for ComfyUI prompt {prompt_id}")

    async def _fetch_output(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        r = await self._client.get("/view", params=params)
        r.raise_for_status()
        return r.content

    # --- music generation (ACE-Step) --------------------------------
    async def submit_music(self, workflow_name: str, patches: dict[str, dict[str, Any]]) -> str:
        workflow = self._patch_workflow(self._load_workflow(workflow_name), patches)
        return await self._queue_prompt(workflow)

    async def submit_music_graph(self, workflow: dict[str, Any],
                                 client_id: str | None = None) -> str:
        """Submit the already-resolved graph so the archived request is exact."""
        return await self._queue_prompt(workflow, client_id=client_id)

    async def system_stats(self) -> dict[str, Any] | None:
        """Read ComfyUI's system statistics without loading or changing anything."""
        try:
            response = await self._client.get("/system_stats")
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    async def submit_prompt_body(self, body: bytes, *,
                                 client_id: str | None = None) -> dict[str, Any]:
        """POST exact serialized request bytes and return the raw response record."""
        response = await self._client.post(
            "/prompt", content=body,
            headers={"Content-Type": "application/json"})
        observed_at = datetime.now(timezone.utc).isoformat()
        response_json = None
        try:
            response_json = response.json()
        except Exception:
            response_json = None
        prompt_id = None
        if isinstance(response_json, dict):
            prompt_id = response_json.get("prompt_id")
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.content,
            "json": response_json,
            "prompt_id": prompt_id,
            "observed_at": observed_at,
            "client_id": client_id,
            "request_url": "/prompt",
        }

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        """Read one prompt's ComfyUI history once; returns None if unavailable."""
        try:
            response = await self._client.get(f"/history/{prompt_id}")
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data.get(prompt_id)
        except Exception:
            return None
        return None

    async def wait_music(self, prompt_id: str, *, should_cancel=None,
                         cancel_timeout_s: float = 10.0) -> dict[str, Any]:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if should_cancel is not None and should_cancel():
                await self.cancel_prompt(prompt_id)
                grace_deadline = time.time() + cancel_timeout_s
                while time.time() < grace_deadline:
                    r = await self._client.get(f"/history/{prompt_id}")
                    r.raise_for_status()
                    hist = r.json()
                    if prompt_id in hist:
                        raise ComfyUICancelled(
                            "cancel requested; prompt already has terminal history",
                            result=hist[prompt_id],
                        )
                    await asyncio.sleep(min(self.poll_interval, 1))
                raise ComfyUICancelled("cancel requested; terminal history not confirmed")
            r = await self._client.get(f"/history/{prompt_id}")
            r.raise_for_status()
            hist = r.json()
            if prompt_id in hist:
                return hist[prompt_id]
            await asyncio.sleep(self.poll_interval)
        raise ComfyUIError(f"Timed out waiting for ComfyUI prompt {prompt_id}")

    async def fetch_music(self, result: dict[str, Any], *, prompt_id: str = "") -> dict[str, Any]:
        outputs = result.get("outputs", {})
        for node_out in outputs.values():
            for key in ("audio", "mp3", "wav", "flac"):
                items = node_out.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    data = await self._fetch_output(
                        item["filename"], item.get("subfolder", ""), item.get("type", "output")
                    )
                    return {
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "bytes": data,
                        "prompt_id": prompt_id,
                    }
        raise ComfyUIError("No audio output found in ComfyUI result")

    async def cancel_prompt(self, prompt_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            r = await self._client.get("/queue")
            r.raise_for_status()
            queue = r.json()
        except Exception as exc:
            result["queue_inspect_error"] = str(exc)
            queue = {}
        running = [
            item[1] for item in queue.get("queue_running", [])
            if isinstance(item, list) and len(item) > 1
        ]
        pending = [
            item[1] for item in queue.get("queue_pending", [])
            if isinstance(item, list) and len(item) > 1
        ]
        result["observed_running"] = prompt_id in running
        result["observed_pending"] = prompt_id in pending
        if prompt_id in running:
            try:
                r = await self._client.post("/interrupt")
                result["interrupt_status"] = r.status_code
                r.raise_for_status()
            except Exception as exc:
                result["interrupt_error"] = str(exc)
        if prompt_id in pending:
            try:
                r = await self._client.post("/queue", json={"delete": [prompt_id]})
                result["queue_status"] = r.status_code
                r.raise_for_status()
            except Exception as exc:
                result["queue_error"] = str(exc)
        return result

    async def generate_image(
        self,
        prompt: str,
        *,
        negative_prompt: str = "",
        width: int = 1080,
        height: int = 1920,
        steps: int = 20,
        seed: int | None = None,
        reference_image_b64: str | None = None,
        denoise: float | None = None,
        face_reference_b64: str | None = None,
    ) -> dict[str, Any]:
        """Submit a FLUX workflow. Returns {"filename":..., "bytes":...}.

        If `face_reference_b64` is provided, the PuLID workflow is used:
        PuLID locks the face from the reference image into the generation,
        ensuring character consistency across scenes.

        If `reference_image_b64` is provided (and denoise < 1.0), the img2img
        workflow is used: the reference image is VAE-encoded and the KSampler
        re-diffuses from it at `denoise` strength, preserving the reference
        identity (face/character) while the prompt drives the new scene.

        txt2img workflow node ids: 6=positive, 7=negative, 10=EmptyLatentImage,
        25=KSampler.
        img2img workflow node ids: 6=positive, 7=negative, 1=LoadImage,
        61=VAEEncode, 25=KSampler (denoise).
        PuLID workflow node ids: 53=LoadImage (face ref), 54=ApplyPulid,
        25=KSampler (model from 54).
        """
        s = seed if seed is not None else int(time.time()) % (2**31)
        if face_reference_b64 is not None:
            if not self.workflow_image_pulid:
                raise ComfyUIError("PuLID generation is disabled: configure and preflight an exported native workflow first")
            # PuLID path: upload face reference, patch LoadImage + prompts + KSampler
            wf = self._load_workflow(self.workflow_image_pulid)
            face_filename = await self._upload_image(base64.b64decode(face_reference_b64))
            patches: dict[str, dict[str, Any]] = {
                "6": {"text": prompt},
                "7": {"text": negative_prompt},
                "10": {"width": width, "height": height},
                "25": {"seed": s, "steps": steps, "denoise": 1.0},
                "53": {"image": face_filename},
            }
        elif reference_image_b64 is not None:
            if not self.workflow_image_img2img:
                raise ComfyUIError("Image-to-image generation is disabled: configure and preflight an exported native workflow first")
            # img2img path: upload the reference, patch LoadImage + VAEEncode + denoise
            wf = self._load_workflow(self.workflow_image_img2img)
            ref_filename = await self._upload_image(base64.b64decode(reference_image_b64))
            patches: dict[str, dict[str, Any]] = {
                "6": {"text": prompt},
                "7": {"text": negative_prompt},
                "1": {"image": ref_filename},
                "25": {"seed": s, "steps": steps, "denoise": denoise if denoise is not None else 0.5},
            }
        else:
            wf = self._load_workflow(self.workflow_image)
            patches: dict[str, dict[str, Any]] = {
                "6": {"text": prompt},
                "7": {"text": negative_prompt},
                "10": {"width": width, "height": height},
                "25": {"seed": s, "steps": steps, "denoise": 1.0},
            }
        wf = self._patch_workflow(wf, patches)
        pid = await self._queue_prompt(wf)
        result = await self._wait(pid)
        # Find the SaveImage/PreviewImage output node
        outputs = result.get("outputs", {})
        for _node_id, node_out in outputs.items():
            imgs = node_out.get("images") or node_out.get("gifs") or []
            if imgs:
                f = imgs[0]
                data = await self._fetch_output(f["filename"], f.get("subfolder", ""), f.get("type", "output"))
                return {"filename": f["filename"], "bytes": data, "prompt_id": pid, "workflow": (self.workflow_image_pulid if face_reference_b64 is not None else self.workflow_image_img2img if reference_image_b64 is not None else self.workflow_image)}
        raise ComfyUIError("No image output found in ComfyUI result")

    async def generate_video(
        self,
        prompt: str,
        *,
        mode: str = "image_to_video",
        source_image_b64: str | None = None,
        width: int = 720,
        height: int = 1280,
        duration_s: float = 5.0,
        fps: int = 16,
        seed: int | None = None,
    ) -> dict[str, Any]:
        wf_name = self.workflow_i2v if mode == "image_to_video" else self.workflow_t2v
        wf = self._load_workflow(wf_name)
        s = seed if seed is not None else int(time.time()) % (2**31)
        # Wan temporal VAE accepts lengths 4n+1. Round up so a normal 5 s / 16
        # fps request patches the workflow's valid 81-frame default, not 80.
        requested_frames = max(1, round(duration_s * fps))
        frames = requested_frames + ((1 - requested_frames) % 4)
        patches: dict[str, dict[str, Any]] = {
            "6": {"text": prompt},
            "10": {"width": width, "height": height, "length": frames},
            "25": {"seed": s},
            "28": {"frame_rate": fps},
        }
        if mode == "image_to_video":
            if source_image_b64 is None:
                raise ComfyUIError("image_to_video requires source_image_b64")
            import base64 as _b64
            img_bytes = _b64.b64decode(source_image_b64)
            uploaded_name = await self._upload_image(img_bytes)
            patches["40"] = {"image": uploaded_name}  # LoadImage node filename
        wf = self._patch_workflow(wf, patches)
        pid = await self._queue_prompt(wf)
        result = await self._wait(pid)
        outputs = result.get("outputs", {})
        for _node_id, node_out in outputs.items():
            gifs = node_out.get("gifs", [])
            if gifs:
                g = gifs[0]
                data = await self._fetch_output(g["filename"], g.get("subfolder", ""), g.get("type", "output"))
                return {"filename": g["filename"], "bytes": data, "prompt_id": pid,
                        "frames": frames, "duration_s": frames / fps, "workflow": wf_name}
        raise ComfyUIError("No video output found in ComfyUI result")

    async def upscale_video(
        self,
        video_bytes: bytes,
        *,
        fps: int = 16,
        model_name: str = "RealESRGAN_x2plus.pth",
        target_size: int = 1920,
    ) -> dict[str, Any]:
        """Upscale a video using Real-ESRGAN via ComfyUI.

        Loads the input video, upscales each frame with the specified ESRGAN
        model, scales to target max dimension (default 1920 = 1080x1920 for 9:16),
        and re-encodes to H264 mp4.

        Returns {"filename":..., "bytes":..., "prompt_id":...}.
        """
        if not self.workflow_upscale:
            raise ComfyUIError("Video upscale is disabled: configure and preflight an exported native workflow first")
        wf = self._load_workflow(self.workflow_upscale)
        # Upload the video to ComfyUI's input folder (endpoint is /upload/image
        # even for videos; field name must be "image").
        fname = f"upscale_input_{uuid.uuid4().hex[:8]}.mp4"
        files = {"image": (fname, video_bytes, "video/mp4")}
        r = await self._client.post("/upload/image", files=files)
        if r.status_code >= 400:
            raise ComfyUIError(f"Video upload failed: {r.text}")
        uploaded_name = r.json().get("name", fname)

        patches: dict[str, dict[str, Any]] = {
            "1": {"video": uploaded_name},
            "2": {"model_name": model_name},
            "4": {"largest_size": target_size},
            "5": {"frame_rate": fps},
        }
        wf = self._patch_workflow(wf, patches)
        pid = await self._queue_prompt(wf)
        result = await self._wait(pid)
        outputs = result.get("outputs", {})
        for _node_id, node_out in outputs.items():
            gifs = node_out.get("gifs", [])
            if gifs:
                g = gifs[0]
                data = await self._fetch_output(g["filename"], g.get("subfolder", ""), g.get("type", "output"))
                return {"filename": g["filename"], "bytes": data, "prompt_id": pid, "workflow": self.workflow_upscale}
        raise ComfyUIError("No upscaled video output found in ComfyUI result")

    # --- model lifecycle (for ModelRouter unloader) -----------------
    async def unload(self) -> dict[str, Any]:
        """Request ComfyUI to release cached models.

        ``/free`` acknowledges a request but does not return a VRAM measurement,
        so this method deliberately does not represent it as verified release.
        """
        try:
            r = await self._client.post("/free", json={"unload_models": True, "free_memory": True})
            r.raise_for_status()
            return {"attempted": True, "verified": False, "reason": "ComfyUI /free accepted; VRAM not measured"}
        except Exception as e:
            log.warning("ComfyUI.unload: /free failed: %s", e)
            return {"attempted": True, "verified": False, "reason": str(e)}


_client: ComfyUIClient | None = None


def get_comfyui() -> ComfyUIClient:
    global _client
    if _client is None:
        _client = ComfyUIClient()
    return _client


def encode_file_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")