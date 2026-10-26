"""ACE-Step 1.5 repaint provider boundary.

The real client talks to the isolated local service. Tests inject a fake
provider. No code path in this module loads a model.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.config import get_settings

NATIVE_PROVIDER = "ace-step-native-repaint"
NATIVE_REQUEST_SCHEMA = "song_writer.native_repaint_request.v1"


@dataclass(frozen=True)
class RepaintRequest:
    source_path: Path
    source_sha256: str
    start_s: float
    end_s: float
    prompt: str
    settings: dict
    output_format: str
    lyrics: str | None = None
    seed: int | None = None
    bpm: int | None = None
    key_scale: str | None = None
    time_signature: str | None = None
    language: str | None = None


def _repaint_form(request: RepaintRequest) -> dict[str, str]:
    """Build the native ACE-Step repaint form without submitting it."""
    form = {
        "task_type": "repaint",
        "prompt": request.prompt,
        "repainting_start": str(request.start_s),
        "repainting_end": str(request.end_s),
        "chunk_mask_mode": "explicit",
        "repaint_mode": "conservative",
        "audio_cover_strength": "1.0",
        "audio_format": request.output_format,
    }
    if request.lyrics is not None:
        form["lyrics"] = request.lyrics
    if request.seed is not None:
        form["seed"] = str(request.seed)
        form["use_random_seed"] = "false"
    if request.bpm is not None:
        form["bpm"] = str(request.bpm)
    if request.key_scale:
        form["key_scale"] = request.key_scale
    if request.time_signature:
        form["time_signature"] = request.time_signature
    if request.language:
        form["vocal_language"] = request.language
    return form


@dataclass(frozen=True)
class NativePreparedRequest:
    """Exact multipart request frozen before native dispatch."""
    body_path: Path
    content_type: str
    boundary: str
    filename: str
    file_field: str
    file_content_type: str
    file_sha256: str
    file_bytes: int
    form: dict[str, str]
    field_order: tuple[str, ...]
    body_sha256: str
    body_bytes: int
    source_sha256: str
    source_bytes: int


@dataclass(frozen=True)
class NativeExecution:
    """Provider result plus the exact native evidence observed in transit."""
    output: RepaintOutput
    task_id: str
    raw_bytes: bytes
    output_url: str
    output_content_type: str | None
    output_filename: str | None
    submission: dict[str, Any]
    statuses: list[dict[str, Any]]
    client_observed_at: str


@dataclass(frozen=True)
class RepaintOutput:
    samples: array
    sample_rate: int
    channels: int


class NativeRepaintTimeout(RuntimeError):
    pass


class NativeRepaintFailed(RuntimeError):
    pass


class NativeExecutionRecorder(Protocol):
    dispatched: bool

    def mark_dispatched(self) -> None: ...
    def submission(self, response: dict[str, Any], task_id: str | None) -> None: ...
    def status(self, response: dict[str, Any], row: Any) -> None: ...
    def output_download(self, response: dict[str, Any], url: str) -> None: ...
    def event(self, kind: str, detail: Any = None) -> None: ...


class RepaintProvider(Protocol):
    def generate(self, request: RepaintRequest) -> RepaintOutput: ...


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _multipart_quote(value: str) -> str:
    return value.replace("\r", "").replace("\n", "").replace('"', "'")


def build_native_multipart(
    request: RepaintRequest,
    body_path: Path,
    *,
    boundary: str | None = None,
) -> NativePreparedRequest:
    """Write the exact multipart body once, before dispatch.

    The audio part is streamed from disk. The returned body path is the exact
    file that must be posted; callers must not rebuild the request.
    """
    source = request.source_path
    if not source.is_file():
        raise FileNotFoundError(f"repaint source is missing: {source}")
    boundary = boundary or f"----songwriter{uuid.uuid4().hex}"
    form = _repaint_form(request)
    field_order = tuple(form.keys())
    filename = _multipart_quote(source.name)
    suffix = source.suffix.lower()
    file_content_type = "audio/wav" if suffix == ".wav" else "audio/mpeg"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    file_bytes = 0

    def write(handle, payload: bytes) -> None:
        handle.write(payload)

    with body_path.open("xb") as out, source.open("rb") as src:
        write(out, f"--{boundary}\r\n".encode("ascii"))
        write(out, (
            f'Content-Disposition: form-data; name="src_audio"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8"))
        write(out, f"Content-Type: {file_content_type}\r\n\r\n".encode("ascii"))
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
            file_bytes += len(chunk)
            write(out, chunk)
        write(out, b"\r\n")
        for name, value in form.items():
            write(out, f"--{boundary}\r\n".encode("ascii"))
            write(out, (
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            ).encode("utf-8"))
            write(out, str(value).encode("utf-8"))
            write(out, b"\r\n")
        write(out, f"--{boundary}--\r\n".encode("ascii"))

    body_sha256, body_bytes = _sha256_file(body_path)
    source_sha256 = digest.hexdigest()
    if request.source_sha256 and source_sha256 != request.source_sha256:
        raise ValueError(
            f"source changed while freezing request: expected {request.source_sha256}, "
            f"got {source_sha256}")
    return NativePreparedRequest(
        body_path=body_path,
        content_type=f"multipart/form-data; boundary={boundary}",
        boundary=boundary,
        filename=source.name,
        file_field="src_audio",
        file_content_type=file_content_type,
        file_sha256=source_sha256,
        file_bytes=file_bytes,
        form=form,
        field_order=field_order,
        body_sha256=body_sha256,
        body_bytes=body_bytes,
        source_sha256=source_sha256,
        source_bytes=file_bytes,
    )


def _safe_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value for key, value in response.headers.items()
        if key.lower() not in {"set-cookie", "authorization"}
    }


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    body = response.content
    parsed = None
    try:
        parsed = response.json()
    except Exception:
        parsed = None
    return {
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": str(response.url),
        "status_code": response.status_code,
        "headers": _safe_headers(response),
        "body": body,
        "json": parsed,
    }


def decode_audio_bytes(payload: bytes) -> RepaintOutput:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "candidate.bin"
        path.write_bytes(payload)
        binary = get_settings().ffmpeg.binary
        proc = subprocess.run(
            [binary, "-v", "error", "-i", str(path), "-f", "f32le",
             "-ac", "2", "-ar", "48000", "-"],
            capture_output=True,
        )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("repaint output could not be decoded as audio")
    samples = array("f")
    samples.frombytes(proc.stdout)
    return RepaintOutput(samples=samples, sample_rate=48000, channels=2)


class DeterministicRepaintProvider:
    """Isolated deterministic provider for offline browser walkthroughs.

    Prompt markers select behaviours: "[timeout]" raises a transport timeout,
    "[short]" returns a shorter buffer, and anything else scales the edit
    interval by 0.5. It never loads a model or contacts a service.
    """

    def generate(self, request: RepaintRequest) -> RepaintOutput:
        if "[timeout]" in request.prompt:
            raise httpx.TimeoutException("isolated fake provider timeout")
        from app.services.music_service import _decode_pcm

        source, sample_rate, channels = _decode_pcm(request.source_path)
        output = array("f", source)
        start = int(round(request.start_s * sample_rate))
        end = int(round(request.end_s * sample_rate))
        for index in range(start * channels, min(end * channels, len(output))):
            output[index] = source[index] * 0.5
        if "[short]" in request.prompt:
            output = output[:-2]
        return RepaintOutput(
            samples=output, sample_rate=sample_rate, channels=channels)


class AceRepaintClient:
    """Local-only HTTP client for the ACE-Step native repaint API.

    The real request body is frozen with :func:`build_native_multipart` and
    posted from that saved file. Polling/status/output observations are emitted
    to an optional recorder; the client never loads a model.
    """

    def __init__(self, base_url: str, timeout_s: int = 7200, *,
                 transport: httpx.BaseTransport | None = None,
                 poll_interval_s: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport
        self.poll_interval_s = poll_interval_s

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout_s, transport=self.transport)

    def execute_prepared(
        self,
        prepared: NativePreparedRequest,
        *,
        recorder: NativeExecutionRecorder,
    ) -> NativeExecution:
        submission: dict[str, Any] | None = None
        statuses: list[dict[str, Any]] = []
        task_id: str | None = None
        with self._client() as client:
            recorder.mark_dispatched()
            try:
                with prepared.body_path.open("rb") as handle:
                    response = client.post(
                        f"{self.base_url}/release_task",
                        content=handle,
                        headers={
                            "Content-Type": prepared.content_type,
                            "Content-Length": str(prepared.body_bytes),
                        },
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                recorder.event("transport_error", {"error": str(exc)[:1000]})
                raise
            submission = _response_payload(response)
            try:
                payload = response.json()
                task_id = (payload.get("data") or {}).get("task_id")
            except Exception:
                task_id = None
            recorder.submission(submission, task_id)
            response.raise_for_status()
            if not task_id:
                raise NativeRepaintFailed("native submission returned no task_id")
            deadline = time.monotonic() + self.timeout_s
            output_info: dict[str, Any] | None = None
            result_row: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                if self.poll_interval_s:
                    time.sleep(self.poll_interval_s)
                result = client.post(
                    f"{self.base_url}/query_result",
                    json={"task_id_list": [task_id]},
                )
                observed = _response_payload(result)
                rows = (observed.get("json") or {}).get("data") or []
                row = rows[0] if rows else None
                statuses.append({**observed, "row": row})
                recorder.status(observed, row)
                result.raise_for_status()
                if not row:
                    continue
                if row.get("status") == 2:
                    raise NativeRepaintFailed(f"repaint task failed: {row}")
                if row.get("status") == 1:
                    parsed = row.get("result")
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                    try:
                        output_info = parsed[0]
                    except (TypeError, IndexError, KeyError) as exc:
                        raise NativeRepaintFailed(
                            "repaint task succeeded without an output reference") from exc
                    result_row = row
                    break
            if output_info is None:
                recorder.event("timeout", {
                    "task_id": task_id,
                    "statuses_observed": len(statuses),
                })
                raise NativeRepaintTimeout(
                    "native repaint task timed out; execution stop is not proven")
            audio_url = output_info["file"]
            if not audio_url.startswith("http"):
                audio_url = f"{self.base_url}{audio_url}"
            audio = client.get(audio_url)
            download = _response_payload(audio)
            recorder.output_download(download, audio_url)
            audio.raise_for_status()
            raw = audio.content
            output = decode_audio_bytes(raw)
            return NativeExecution(
                output=output,
                task_id=task_id,
                raw_bytes=raw,
                output_url=audio_url,
                output_content_type=audio.headers.get("content-type"),
                output_filename=Path(urlparse(audio_url).path).name or None,
                submission=submission or {},
                statuses=statuses,
                client_observed_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

