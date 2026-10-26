"""Local, append-only audio edit decisions and CPU-only master exports.

The core intentionally uses only the standard library. No music/generation
service, arrangement data, model, network client, or mutable project manifest
is involved. See docs/MASTER-AUDIO.md for the persistence and threat model.
"""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import re
import stat
import subprocess
import threading
import time
import uuid
import wave
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_BYTES = 64 * 1024 * 1024
MAX_DURATION = 1800
MAX_TOTAL_SOURCE_DURATION = 7200
MAX_SOURCES = 32
MAX_SECTIONS = 64
MAX_REVISIONS = 500
MAX_ANALYSES = 32
MAX_MAPS = 100
MAX_SECTION_EXPORTS = 128
MAX_SECTION_EXPORT_DURATION = 7200
SECTION_EXPORT_TIMEOUT_S = 30
ANALYSIS_TIMEOUT_S = 20
ANALYSIS_WINDOW_FRAMES = 4800  # 100 ms; at most 18,000 feature windows.
RATE = 48000
CHANNELS = 2
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
FORMATS = {".wav": "wav", ".mp3": "mp3", ".flac": "flac", ".ogg": "ogg", ".m4a": "mov", ".aac": "aac"}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class MasterAudioError(Exception):
    def __init__(self, message: str, code: str = "validation", status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def fail(message, code="validation", status=422):
    raise MasterAudioError(message, code, status)


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def wav_quality(path):
    """Measure actual interleaved master PCM, not recipe gain or source peaks.

    A rail-valued sample is evidence of zero headroom, not proof of clipping.
    Null represents negative-infinity dBFS for digital silence (JSON has no inf).
    """
    peak, full_scale, total = 0, 0, 0
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (CHANNELS, 2, RATE):
            fail("quality measurement PCM format mismatch", "processing_failed", 502)
        expected_samples = audio.getnframes() * CHANNELS
        while block := audio.readframes(65536):
            samples = array("h", block)
            if os.sys.byteorder != "little":
                samples.byteswap()
            low, high = min(samples), max(samples)
            peak = max(peak, abs(low), abs(high))
            full_scale += samples.count(-32768) + samples.count(32767)
            total += len(samples)
    if total != expected_samples or not total:
        fail("quality measurement PCM truncated or empty", "processing_failed", 502)
    peak_dbfs = 20 * math.log10(peak / 32768) if peak else None
    warnings = []
    if full_scale:
        warnings.append("Digital full-scale samples are present: this indicates no sample headroom and possible clipping risk, not proof of clipping. Listen and review gain before release.")
    if peak_dbfs is not None and peak_dbfs > -1:
        warnings.append("Sample peak exceeds -1 dBFS; MP3 encoding or playback reconstruction may create overs. This is not a true-peak measurement.")
    return {"basis": "master_wav_pcm_s16le", "sample_peak_dbfs": peak_dbfs,
            "full_scale_samples": full_scale, "total_samples": total, "warnings": warnings}


def is_link(path):
    # Windows junctions are reparse points but not necessarily symlinks.
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def confined(root: Path, relative: str, *, exists=True):
    """Reject every symlink component, Windows path syntax, and traversal."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        fail("invalid relative path")
    parts = relative.split("/")
    if any(p in {"", ".", ".."} for p in parts) or PurePosixPath(relative).is_absolute():
        fail("path must be project-relative without traversal")
    path = root
    for part in parts:
        path = path / part
        if is_link(path):
            fail("symlinks are not allowed", "unsafe_path", 409)
        if path.exists() and not (path.is_dir() or path.is_file()):
            fail("special files are not allowed", "unsafe_path", 409)
    if not path.resolve().is_relative_to(root.resolve()):
        fail("path escapes project", "unsafe_path", 409)
    if exists and not path.is_file():
        fail("file not found", "not_found", 404)
    return path


def read_json(path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            fail("oversized evidence record", "evidence_invalid", 409)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"unreadable evidence record {path.name}: {exc}", "evidence_invalid", 409)


def write_new(path, value):
    """O_EXCL records never replace evidence; partial writes fail closed."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        fail("evidence already exists; no overwrite/retry allowed", "conflict", 409)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        fail(f"{name} must be a finite number from {low} to {high}")
    return float(value)


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        fail("object has missing or unsupported fields")


def section_label(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 120
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        fail("section label must be nonempty text up to 120 characters without controls")
    return value.strip()


def analyze_pcm(path, frames, labels):
    """Bounded streaming acoustic heuristics, never semantic/lyric recognition.

    100 ms windows, sampling both channels at 2.4 kHz for cheap RMS and
    rough waveform-change features. This is deliberately not a music model.
    Only feature windows (not the recording) are retained in memory.
    """
    deadline = time.monotonic() + ANALYSIS_TIMEOUT_S
    energy, texture = [], []
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) != (CHANNELS, 2, RATE, frames):
                fail("analysis PCM format or frame count mismatch", "stale_source", 409)
            consumed = 0
            while consumed < frames:
                if time.monotonic() > deadline:
                    fail("local analysis time limit reached; no suggestion saved", "analysis_timeout", 503)
                count = min(ANALYSIS_WINDOW_FRAMES, frames - consumed)
                block = audio.readframes(count)
                if len(block) != count * CHANNELS * 2:
                    fail("analysis PCM truncated", "stale_source", 409)
                samples = array("h", block)
                if os.sys.byteorder != "little":
                    samples.byteswap()
                # No channel cancellation; both stereo channels contribute.
                left, right = samples[::40], samples[1::40]
                power = sum(x * x for x in left) + sum(x * x for x in right)
                rms = math.sqrt(power / (len(left) + len(right))) / 32768
                energy.append(20 * math.log10(max(rms, 1e-6)))
                change = sum(abs(a - b) for channel in (left, right) for a, b in zip(channel, channel[1:]))
                texture.append(change / max(1, len(left) + len(right) - 2) / max(rms * 32768, 1))
                consumed += count
    except (wave.Error, OSError, EOFError):
        fail("cannot read source PCM for local analysis", "processing_failed", 502)

    def mean(values, a, b):
        return sum(values[a:b]) / (b - a)

    candidates = []
    # Compare one-second neighborhoods, never infer semantic verse/chorus.
    for i in range(10, len(energy) - 9):
        before, after = mean(energy, i - 10, i), mean(energy, i, i + 10)
        delta = abs(after - before)
        silence = (before < -48) != (after < -48)
        timbre = abs(mean(texture, i - 10, i) - mean(texture, i, i + 10))
        if silence or delta >= 7 or (timbre >= 0.45 and min(before, after) > -48):
            score = (20 if silence else 0) + delta + min(timbre * 8, 12)
            reason = "silence_transition" if silence else "energy_change" if delta >= 7 else "waveform_change"
            candidates.append((score, i * ANALYSIS_WINDOW_FRAMES, reason))
    # Suppress neighboring candidates, keep strongest evidence, and reserve
    # at least two seconds per suggested region. Hints never set the count.
    selected = []
    minimum = 2 * RATE
    for score, frame, reason in sorted(candidates, reverse=True):
        if (minimum <= frame <= frames - minimum
                and all(abs(frame - old[0]) >= minimum for old in selected)):
            selected.append((frame, reason))
            if len(selected) == MAX_SECTIONS - 1:
                break
    boundaries = [(0, "source_start")] + sorted(selected) + [(frames, "source_end")]
    sections = []
    for i, ((start, reason), (end, _)) in enumerate(zip(boundaries, boundaries[1:])):
        sections.append({"label": labels[i] if i < len(labels) else f"Section {i + 1}",
                         "start_s": start / RATE, "end_s": end / RATE, "confirmed": False,
                         "confidence": "medium" if reason == "silence_transition" else "low",
                         "boundary_reason": reason})
    return sections, len(energy)


def _analysis_worker(connection, path, frames, labels):
    """Separate CPU worker gives the caller a real wall-clock stop/kill bound."""
    try:
        connection.send({"result": analyze_pcm(path, frames, labels)})
    except MasterAudioError as exc:
        connection.send({"error": [str(exc), exc.code, exc.status]})
    except Exception:
        connection.send({"error": ["local PCM analysis failed", "processing_failed", 502]})
    finally:
        connection.close()


def bounded_analysis(path, frames, labels):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_analysis_worker, args=(sender, str(path), frames, labels), daemon=True)
    try:
        worker.start()
        sender.close()
        if not receiver.poll(ANALYSIS_TIMEOUT_S):
            fail("local analysis time limit reached; no suggestion saved", "analysis_timeout", 503)
        try:
            response = receiver.recv()
        except EOFError:
            fail("local PCM analysis worker stopped", "processing_failed", 502)
        if "error" in response:
            raise MasterAudioError(*response["error"])
        return response["result"]
    finally:
        receiver.close()
        sender.close()
        if worker.pid is not None:
            worker.join(timeout=0.1)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=0.5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=0.5)
            worker.close()


class MasterAudioService:
    def __init__(self, store=None, settings=None):
        if store is None:
            from app.services.project_store import get_store
            store = get_store()
        if settings is None:
            from app.config import get_settings
            settings = get_settings()
        self.store = store
        self.ffmpeg = settings.ffmpeg.binary
        self.timeout = min(7200, max(1, settings.ffmpeg.process_timeout_s))

    def _project(self, pid):
        if not isinstance(pid, str) or not re.fullmatch(r"[a-f0-9]{12}", pid):
            fail("invalid project id", "not_found", 404)
        root = Path(self.store.root)
        if is_link(root) or is_link(root / pid):
            fail("symlink project root forbidden", "unsafe_path", 409)
        # Current export exposes _dir, not project_dir. Support the public
        # method if a future store adds it; do not invent a new store API.
        try:
            method = getattr(self.store, "project_dir", None) or self.store._dir
            project = Path(method(pid))
        except KeyError:
            fail("project not found", "not_found", 404)
        if project.resolve() != (root.resolve() / pid) or not project.is_dir():
            fail("project escapes store root", "unsafe_path", 409)
        # Resolve trusted root ancestors only after rejecting project symlinks.
        return project.resolve()

    def _root(self, pid):
        project = self._project(pid)
        root = confined(project, "master-audio", exists=False)
        root.mkdir(exist_ok=True)
        for name in ("sources", "revisions", "renders"):
            confined(root, name, exists=False).mkdir(exist_ok=True)
        return root

    @contextmanager
    def _locked(self, pid):
        root = self._root(pid)
        with _locks_guard:
            mutex = _locks.setdefault(str(root), threading.Lock())
        if not mutex.acquire(blocking=False):
            fail("another master-audio operation is active", "busy", 409)
        stream = None
        try:
            path = confined(root, ".lock", exists=False)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                fail("invalid lock file", "unsafe_path", 409)
            stream = os.fdopen(fd, "r+b")
            if os.fstat(fd).st_size == 0:
                stream.write(b"0")
                stream.flush()
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fail("another process holds master-audio lock", "busy", 409)
            yield root
        finally:
            if stream is not None:
                stream.close()  # OS releases lock, even after process death.
            mutex.release()

    def _run(self, argv, directory, evidence):
        """CPU-only argv; bounded stderr goes directly to an evidence file."""
        log = confined(directory, evidence, exists=False)
        write_new(directory / (evidence + ".argv.json"), argv)
        try:
            with log.open("xb") as stderr:
                subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=stderr, check=True, timeout=self.timeout, shell=False)
        except FileNotFoundError:
            fail("configured ffmpeg binary unavailable", "tool_unavailable", 503)
        except (subprocess.SubprocessError, OSError) as exc:
            fail(f"local audio processing failed; evidence preserved ({type(exc).__name__})", "processing_failed", 502)

    def _base(self):
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1"]

    def _records(self, root, kind):
        directory = confined(root, kind, exists=False)
        rows = []
        for item in sorted(directory.iterdir()):
            if kind == "revisions":
                if not re.fullmatch(r"\d{6}\.json", item.name):
                    fail("unexpected revision evidence", "evidence_invalid", 409)
                rows.append(read_json(confined(root, f"{kind}/{item.name}")))
            else:
                if is_link(item) or not item.is_dir():
                    fail("unexpected source/render evidence", "evidence_invalid", 409)
                name = "source.json" if kind == "sources" else "claim.json"
                path = confined(root, f"{kind}/{item.name}/{name}", exists=False)
                if not path.is_file():
                    # Interrupted imports remain visible, never reused silently.
                    rows.append({"status": "uncertain", "source_id" if kind == "sources" else "render_id": item.name})
                else:
                    row = read_json(path)
                    if kind == "renders":
                        result = confined(root, f"{kind}/{item.name}/result.json", exists=False)
                        row = {**row, **read_json(result)} if result.is_file() else {**row, "status": "uncertain"}
                    rows.append(row)
        return rows

    def _revisions(self, root):
        rows = self._records(root, "revisions")
        parent = None
        for i, row in enumerate(rows, 1):
            if row.get("revision") != i or row.get("parent_revision") != i - 1 or row.get("parent_recipe_sha256") != parent:
                fail("revision chain is invalid", "evidence_invalid", 409)
            recipe = {"sections": row.get("sections"), "note": row.get("note")}
            if row.get("recipe_sha256") != digest(recipe):
                fail("revision recipe hash is invalid", "evidence_invalid", 409)
            parent = row["recipe_sha256"]
        return rows

    def _expected(self, root, expected):
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            fail("expected_revision must be a nonnegative integer")
        rows = self._revisions(root)
        if len(rows) != expected:
            fail(f"revision conflict: current revision is {len(rows)}", "revision_conflict", 409)
        return rows

    def _url(self, pid, path):
        return f"/api/projects/{pid}/master-audio/files/{path}"

    def _source(self, pid, root, source_id, verify=True):
        if not isinstance(source_id, str) or not SHA_RE.fullmatch(source_id):
            fail("invalid source id")
        record = read_json(confined(root, f"sources/{source_id}/source.json"))
        if record.get("source_id") != source_id or record.get("sha256") != source_id:
            fail("invalid source binding", "evidence_invalid", 409)
        if verify:
            for key, expected in (("raw_path", source_id), ("pcm_path", record["pcm_sha256"])):
                path = confined(root, record[key])
                if sha_file(path) != expected:
                    fail("immutable source bytes changed", "stale_source", 409)
            original = record.get("original_relative_path")
            if original is not None:
                path = confined(self._project(pid), original)
                if path.stat().st_size > MAX_BYTES or sha_file(path) != source_id:
                    fail("original asset changed; register its new bytes and reconfirm clips", "stale_source", 409)
        return record

    def status(self, pid):
        with self._locked(pid) as root:
            revisions = self._revisions(root)
            sources = self._records(root, "sources")
            for source in sources:
                if source.get("status") == "uncertain":
                    continue
                try:
                    self._source(pid, root, source["source_id"])
                    source["status"] = "ready"
                except MasterAudioError as exc:
                    source["status"], source["error"] = "stale", str(exc)
            return {"schema_version": 1, "revision": len(revisions), "sources": sources,
                    "revisions": revisions, "renders": self._records(root, "renders"),
                    "limits": {"max_bytes": MAX_BYTES, "max_duration_s": MAX_DURATION,
                               "max_sections": MAX_SECTIONS, "max_sources": MAX_SOURCES,
                               "max_revisions": MAX_REVISIONS},
                    "generation": False, "listening_confirmation_is_user_attestation": True}

    def register_source(self, pid, request):
        keys(request, {"relative_path"}, {"relative_path"})
        relative = request["relative_path"]
        with self._locked(pid) as root:
            path = confined(self._project(pid), relative)
            if relative.split("/")[0] not in {"audio", "music"}:
                fail("source must be an existing project audio/ or music/ asset")
            return self._import(pid, root, path, relative)

    def _section_source(self, pid, root, source_id):
        """Cap evidence sizes before the existing immutable-source verifier."""
        source = self._source(pid, root, source_id, verify=False)
        frames = source.get("frames")
        if (isinstance(frames, bool) or not isinstance(frames, int) or not 0 < frames <= MAX_DURATION * RATE
                or source.get("duration_s") != frames / RATE
                or source.get("sample_rate") != RATE or source.get("channels") != CHANNELS
                or not isinstance(source.get("pcm_sha256"), str) or not SHA_RE.fullmatch(source["pcm_sha256"])):
            fail("invalid source PCM metadata", "evidence_invalid", 409)
        for field, limit in (("raw_path", MAX_BYTES), ("pcm_path", frames * CHANNELS * 2 + 1024 * 1024)):
            path = confined(root, source.get(field))
            if not 0 < path.stat().st_size <= limit:
                fail("source evidence exceeds bounded analysis limits", "stale_source", 409)
        return self._source(pid, root, source_id)

    def _section_history(self, root, source, kind):
        directory = confined(root, f"sources/{source['source_id']}/{kind}", exists=False)
        directory.mkdir(exist_ok=True)
        limit = MAX_ANALYSES if kind == "analyses" else MAX_MAPS
        paths = []
        for path in directory.iterdir():
            paths.append(path)
            if len(paths) > limit:
                fail("section history exceeds record limit", "evidence_invalid", 409)
        rows, parent = [], None
        for index, path in enumerate(sorted(paths), 1):
            if path.name != f"{index:06d}.json":
                fail("unexpected section history evidence", "evidence_invalid", 409)
            path = confined(root, f"sources/{source['source_id']}/{kind}/{path.name}")
            if path.stat().st_size > 128 * 1024:
                fail("oversized section record", "evidence_invalid", 409)
            row = read_json(path)
            if not isinstance(row, dict):
                fail("invalid section record", "evidence_invalid", 409)
            checksum = row.get("record_sha256")
            content = {k: v for k, v in row.items() if k != "record_sha256"}
            try:
                valid_hash = checksum == digest(content)
            except (TypeError, ValueError):
                valid_hash = False
            revision_key = "analysis_revision" if kind == "analyses" else "map_revision"
            if (not valid_hash or row.get("source_id") != source["source_id"]
                    or row.get("source_sha256") != source["sha256"] or row.get("pcm_sha256") != source["pcm_sha256"]
                    or row.get(revision_key) != index or row.get("parent_record_sha256") != parent):
                fail("section record hash/source/revision binding invalid", "evidence_invalid", 409)
            rows.append(row)
            parent = checksum
        return rows

    def _save_section_record(self, root, source, kind, rows, record):
        record.update({"schema_version": 1, "source_id": source["source_id"], "source_sha256": source["sha256"],
                       "pcm_sha256": source["pcm_sha256"], "duration_s": source["duration_s"],
                       "created_at": now(), "generation": False,
                       "parent_record_sha256": rows[-1]["record_sha256"] if rows else None})
        record["record_sha256"] = digest(record)
        path = confined(root, f"sources/{source['source_id']}/{kind}/{len(rows) + 1:06d}.json", exists=False)
        write_new(path, record)
        return record

    def create_analysis(self, pid, source_id, request):
        keys(request, {"section_labels"})
        labels = request.get("section_labels", [])
        if not isinstance(labels, list) or len(labels) > MAX_SECTIONS:
            fail(f"section_labels must be a list of at most {MAX_SECTIONS} ordered hints")
        labels = [section_label(label) for label in labels]
        with self._locked(pid) as root:
            source = self._section_source(pid, root, source_id)
            rows = self._section_history(root, source, "analyses")
            if len(rows) >= MAX_ANALYSES:
                fail("analysis record limit reached", "size_limit", 413)
            sections, windows = bounded_analysis(confined(root, source["pcm_path"]), source["frames"], labels)
            self._section_source(pid, root, source_id)  # Detect changes during analysis.
            record = {"analysis_id": uuid.uuid4().hex, "analysis_revision": len(rows) + 1,
                      "kind": "suggestion", "method": "local_pcm_energy_silence_waveform_v1",
                      "section_labels": labels, "sections": sections, "confidence": "low",
                      "feature_windows": windows, "window_s": ANALYSIS_WINDOW_FRAMES / RATE,
                      "limits": {"max_duration_s": MAX_DURATION, "processing_timeout_s": ANALYSIS_TIMEOUT_S,
                                 "max_sections": MAX_SECTIONS, "max_analyses": MAX_ANALYSES},
                      "warnings": ["Acoustic suggestions only; not semantic section or lyrics recognition. Listen and adjust every boundary.",
                                   "Labels are tentative ordered hints, not detected identities. Hint count does not determine section count.",
                                   "Repetition is not identified; quiet or subtle changes may be missed. No listening confirmation is automatic."]}
            return self._save_section_record(root, source, "analyses", rows, record)

    def analysis_history(self, pid, source_id):
        with self._locked(pid) as root:
            source = self._section_source(pid, root, source_id)
            return {"source_id": source_id, "analyses": self._section_history(root, source, "analyses")}

    def _map_sections(self, source, sections):
        if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
            fail(f"sections must contain 1 to {MAX_SECTIONS} contiguous ranges")
        result, previous = [], 0.0
        for section in sections:
            keys(section, {"label", "start_s", "end_s", "confirmed"}, {"label", "start_s", "end_s", "confirmed"})
            label = section_label(section["label"])
            start = number(section["start_s"], "start_s", 0, source["duration_s"])
            end = number(section["end_s"], "end_s", 0, source["duration_s"])
            if type(section["confirmed"]) is not bool:
                fail("confirmed must be an explicit boolean")
            a, b = math.floor(start * RATE + 0.5), math.floor(end * RATE + 0.5)
            if start != previous or end <= start or b <= a or b > source["frames"]:
                fail("map must cover the source contiguously in order, without gaps, overlaps or empty sections")
            result.append({"label": label, "start_s": start, "end_s": end, "confirmed": section["confirmed"]})
            previous = end
        if previous != source["duration_s"]:
            fail("map must end at the complete source duration")
        return result

    def create_map(self, pid, source_id, request):
        keys(request, {"expected_map_revision", "analysis_id", "sections"}, {"expected_map_revision", "sections"})
        expected = request["expected_map_revision"]
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            fail("expected_map_revision must be a nonnegative integer")
        analysis_id = request.get("analysis_id")
        if analysis_id is not None and (not isinstance(analysis_id, str) or not re.fullmatch(r"[a-f0-9]{32}", analysis_id)):
            fail("analysis_id must be a saved analysis id or null")
        with self._locked(pid) as root:
            source = self._section_source(pid, root, source_id)
            rows = self._section_history(root, source, "maps")
            if len(rows) != expected:
                fail(f"map revision conflict: current map revision is {len(rows)}", "map_revision_conflict", 409)
            if len(rows) >= MAX_MAPS:
                fail("map record limit reached", "size_limit", 413)
            if analysis_id is not None:
                analyses = self._section_history(root, source, "analyses")
                if not any(row["analysis_id"] == analysis_id for row in analyses):
                    fail("analysis not found for this source", "analysis_not_found", 404)
            sections = self._map_sections(source, request["sections"])
            self._section_source(pid, root, source_id)
            return self._save_section_record(root, source, "maps", rows,
                {"kind": "reviewed", "map_revision": len(rows) + 1, "parent_map_revision": len(rows),
                 "analysis_id": analysis_id, "sections": sections,
                 "all_confirmed": all(section["confirmed"] for section in sections),
                 "confirmation_basis": "user_attestation_only", "edit_recipe_created": False})

    def map_history(self, pid, source_id):
        with self._locked(pid) as root:
            source = self._section_source(pid, root, source_id)
            rows = self._section_history(root, source, "maps")
            return {"source_id": source_id, "map_revision": len(rows), "maps": rows}

    def _export_limits(self):
        return {"max_exports": MAX_SECTION_EXPORTS, "max_duration_s": MAX_DURATION,
                "max_total_duration_s": MAX_SECTION_EXPORT_DURATION,
                "copy_timeout_s": SECTION_EXPORT_TIMEOUT_S}

    def _export_hash(self, path, deadline):
        """Bounded streaming hash; deadline covers copying and output checking."""
        h = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                if time.monotonic() > deadline:
                    fail("section export time limit reached", "export_timeout", 503)
                h.update(block)
        return h.hexdigest()

    def _export_claim(self, root, export_id):
        path = confined(root, f"section-exports/{export_id}/claim.json")
        if path.stat().st_size > 16 * 1024:
            fail("oversized section export claim", "evidence_invalid", 409)
        claim = read_json(path)
        if (not isinstance(claim, dict) or claim.get("export_id") != export_id
                or not isinstance(claim.get("source_id"), str) or not SHA_RE.fullmatch(claim["source_id"])
                or claim.get("source_sha256") != claim["source_id"]
                or not isinstance(claim.get("pcm_sha256"), str) or not SHA_RE.fullmatch(claim["pcm_sha256"])
                or type(claim.get("start_frame")) is not int or type(claim.get("end_frame")) is not int
                or not 0 <= claim["start_frame"] < claim["end_frame"] <= MAX_DURATION * RATE
                or claim.get("duration_s") != (claim["end_frame"] - claim["start_frame"]) / RATE):
            fail("invalid section export claim", "evidence_invalid", 409)
        return claim

    def _export_manifest(self, root, export_id, claim, verify_audio=True):
        relative = f"section-exports/{export_id}"
        # A failed publication (including an fsync error after writing bytes)
        # must never expose an otherwise valid-looking success manifest.
        if confined(root, relative + "/failure.json", exists=False).exists():
            fail("section export failed; evidence is not a deliverable", "not_found", 404)
        manifest = confined(root, relative + "/manifest.json")
        if manifest.stat().st_size > 16 * 1024:
            fail("oversized section export manifest", "evidence_invalid", 409)
        record = read_json(manifest)
        if not isinstance(record, dict):
            fail("invalid section export manifest", "evidence_invalid", 409)
        content = {k: v for k, v in record.items() if k != "record_sha256"}
        try:
            valid = record.get("record_sha256") == digest(content)
        except (ValueError, TypeError):
            valid = False
        if (not valid or record.get("status") != "completed"
                or any(record.get(key) != claim.get(key) for key in claim)
                or record.get("claim_sha256") != digest(claim)):
            fail("section export manifest binding invalid", "evidence_invalid", 409)
        expected_path = relative + "/section.wav"
        output = record.get("files", {}).get("wav", {})
        if (output.get("relative_path") != expected_path
                or record.get("frames") != claim["end_frame"] - claim["start_frame"]):
            fail("section export file binding invalid", "evidence_invalid", 409)
        if verify_audio:
            path = confined(root, expected_path)
            if (path.stat().st_size != output.get("byte_size")
                    or path.stat().st_size != record["frames"] * CHANNELS * 2 + 44
                    or self._export_hash(path, time.monotonic() + SECTION_EXPORT_TIMEOUT_S) != output.get("sha256")):
                fail("immutable exported WAV changed", "stale_export", 409)
        return record

    def _export_history(self, root):
        directory = confined(root, "section-exports", exists=False)
        directory.mkdir(exist_ok=True)
        paths = []
        for path in directory.iterdir():
            paths.append(path)
            if len(paths) > MAX_SECTION_EXPORTS:
                fail("section export history exceeds limit", "evidence_invalid", 409)
        rows = []
        for path in paths:
            eid = path.name
            if not re.fullmatch(r"[a-f0-9]{32}", eid) or is_link(path) or not path.is_dir():
                fail("unexpected section export evidence", "evidence_invalid", 409)
            claim_path = confined(root, f"section-exports/{eid}/claim.json", exists=False)
            if not claim_path.is_file():
                rows.append({"export_id": eid, "status": "uncertain", "duration_s": MAX_DURATION})
                continue
            claim = self._export_claim(root, eid)
            manifest = confined(root, f"section-exports/{eid}/manifest.json", exists=False)
            failure = confined(root, f"section-exports/{eid}/failure.json", exists=False)
            if failure.is_file():
                rows.append({**claim, "status": "failed", "evidence_preserved": True})
            elif manifest.is_file():
                rows.append(self._export_manifest(root, eid, claim, verify_audio=False))
            else:
                rows.append({**claim, "status": "uncertain", "evidence_preserved": True})
        return sorted(rows, key=lambda row: (row.get("created_at", ""), row["export_id"]))

    def section_exports(self, pid, source_id):
        with self._locked(pid) as root:
            self._section_source(pid, root, source_id)
            rows = self._export_history(root)
            return {"source_id": source_id, "exports": [r for r in rows if r.get("source_id") == source_id],
                    "limits": self._export_limits()}

    def create_section_export(self, pid, source_id, request):
        keys(request, {"source_sha256", "start_s", "end_s", "label"},
             {"source_sha256", "start_s", "end_s", "label"})
        if (not isinstance(request["source_sha256"], str)
                or not SHA_RE.fullmatch(request["source_sha256"])):
            fail("source_sha256 must be the registered source SHA-256")
        label = section_label(request["label"])
        with self._locked(pid) as root:
            source = self._section_source(pid, root, source_id)
            if request["source_sha256"] != source["sha256"]:
                fail("export source hash is stale", "stale_source", 409)
            start = number(request["start_s"], "start_s", 0, source["duration_s"])
            end = number(request["end_s"], "end_s", 0, source["duration_s"])
            a, b = math.floor(start * RATE + 0.5), math.floor(end * RATE + 0.5)
            if end <= start or b <= a or b > source["frames"]:
                fail("export range must contain at least one source PCM frame")
            duration = (b - a) / RATE
            rows = self._export_history(root)
            if len(rows) >= MAX_SECTION_EXPORTS or sum(r["duration_s"] for r in rows) + duration > MAX_SECTION_EXPORT_DURATION:
                fail("section export storage/duration limit reached", "size_limit", 413)
            eid = uuid.uuid4().hex
            relative = f"section-exports/{eid}"
            directory = confined(root, relative, exists=False)
            directory.mkdir()
            base_url = f"/api/projects/{pid}/master-audio/section-exports/{eid}"
            claim = {"schema_version": 1, "export_id": eid, "source_id": source_id,
                     "source_sha256": source["sha256"], "pcm_sha256": source["pcm_sha256"],
                     "source_frames": source["frames"], "label": label, "created_at": now(),
                     "requested_start_s": start, "requested_end_s": end,
                     "start_frame": a, "end_frame": b, "start_s": a / RATE, "end_s": b / RATE,
                     "duration_s": duration, "generation": False}
            write_new(directory / "claim.json", claim)
            try:
                deadline = time.monotonic() + SECTION_EXPORT_TIMEOUT_S
                sample_hash = hashlib.sha256()
                output = directory / "section.wav"
                with wave.open(str(confined(root, source["pcm_path"])), "rb") as audio:
                    if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) != (CHANNELS, 2, RATE, source["frames"]):
                        fail("source PCM format/frame mismatch", "stale_source", 409)
                    audio.setpos(a)
                    with output.open("xb") as stream:
                        with wave.open(stream, "wb") as clip:
                            clip.setparams((CHANNELS, 2, RATE, 0, "NONE", "not compressed"))
                            remaining = b - a
                            while remaining:
                                if time.monotonic() > deadline:
                                    fail("section export time limit reached", "export_timeout", 503)
                                take = min(65536, remaining)
                                block = audio.readframes(take)
                                if len(block) != take * CHANNELS * 2:
                                    fail("source PCM truncated", "stale_source", 409)
                                sample_hash.update(block)
                                clip.writeframesraw(block)
                                remaining -= take
                        stream.flush()
                        os.fsync(stream.fileno())
                wav_sha = self._export_hash(output, deadline)
                if output.stat().st_size != (b - a) * CHANNELS * 2 + 44:
                    fail("exported WAV frame count mismatch", "processing_failed", 502)
                # Recheck immutable inputs after copying before publishing success.
                self._section_source(pid, root, source_id)
                if time.monotonic() > deadline:
                    fail("section export time limit reached", "export_timeout", 503)
                record = {**claim, "status": "completed", "completed_at": now(),
                          "claim_sha256": digest(claim), "frames": b - a, "sample_rate": RATE,
                          "channels": CHANNELS, "sample_width_bytes": 2, "encoding": "pcm_s16le",
                          "frame_range": "start_inclusive_end_exclusive", "rounding": "nearest_frame_half_up",
                          "method": "exact_decoded_pcm_frame_copy_v1", "audio_scope": "full_mix_not_isolated_vocals",
                          "sample_data_sha256": sample_hash.hexdigest(), "manifest_url": base_url + "/manifest",
                          "files": {"wav": {"relative_path": relative + "/section.wav", "url": base_url + "/wav",
                                            "sha256": wav_sha, "byte_size": output.stat().st_size}},
                          "processing": {"gain_db": 0, "fades": False, "source_separation": False},
                          "listening_confirmed": False, "edit_recipe_created": False}
                record["record_sha256"] = digest(record)
                write_new(directory / "manifest.json", record)
                return record
            except Exception as exc:
                write_new(directory / "failure.json", {"export_id": eid, "status": "failed", "failed_at": now(),
                          "error": str(exc), "evidence_preserved": True})
                if isinstance(exc, MasterAudioError):
                    raise
                fail("section export failed; evidence retained", "processing_failed", 502)

    def section_export_file(self, pid, export_id, kind):
        if not isinstance(export_id, str) or not re.fullmatch(r"[a-f0-9]{32}", export_id) or kind not in {"wav", "manifest"}:
            fail("section export not found", "not_found", 404)
        with self._locked(pid) as root:
            claim = self._export_claim(root, export_id)
            source = self._section_source(pid, root, claim.get("source_id"))
            if claim.get("source_sha256") != source["sha256"] or claim.get("pcm_sha256") != source["pcm_sha256"]:
                fail("section export source binding invalid", "evidence_invalid", 409)
            self._export_manifest(root, export_id, claim)
            return confined(root, f"section-exports/{export_id}/" + ("section.wav" if kind == "wav" else "manifest.json"))

    def upload_source(self, pid, filename, payload):
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255
                or filename in {".", ".."} or any(c in filename for c in ("/", "\\", ":", "\x00"))
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
            fail("filename must be an audio basename without separators, drive syntax, or control characters")
        extension = Path(filename).suffix.lower()
        if extension not in FORMATS:
            fail("unsupported uploaded audio extension")
        if not payload or len(payload) > MAX_BYTES:
            fail("empty or oversized upload", "size_limit", 413)
        with self._locked(pid) as root:
            # Upload staging is private; never trust a caller filesystem path.
            staging = confined(root, f"upload-{uuid.uuid4().hex}{extension}", exists=False)
            try:
                with staging.open("xb") as stream:
                    stream.write(payload)
                return self._import(pid, root, staging, None, filename)
            finally:
                staging.unlink(missing_ok=True)

    def _import(self, pid, root, original, relative, original_filename=None):
        extension = original.suffix.lower()
        if extension not in FORMATS:
            fail("supported audio extensions: " + ", ".join(FORMATS))
        size = original.stat().st_size
        if not 0 < size <= MAX_BYTES:
            fail("empty or oversized audio asset", "size_limit", 413)
        source_id = sha_file(original)
        directory = confined(root, f"sources/{source_id}", exists=False)
        if directory.exists():
            if not confined(root, f"sources/{source_id}/source.json", exists=False).is_file():
                fail("source import is incomplete/uncertain; preserve evidence for manual review", "source_uncertain", 409)
            return self._source(pid, root, source_id)
        sources = self._records(root, "sources")
        if len(sources) >= MAX_SOURCES:
            fail("source count limit reached")
        if sum(s.get("duration_s", 0) for s in sources) >= MAX_TOTAL_SOURCE_DURATION:
            fail("total source duration limit reached")
        directory.mkdir()
        raw = directory / ("raw" + extension)
        # Copy through no-follow fd; stat before/after + SHA detect mutable
        # generation output changing while being registered.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(original, flags), "rb") as src, raw.open("xb") as dst:
            before = os.fstat(src.fileno())
            if not stat.S_ISREG(before.st_mode):
                fail("source must be a regular file")
            total = 0
            while block := src.read(1024 * 1024):
                total += len(block)
                if total > MAX_BYTES:
                    fail("source grew beyond byte limit", "size_limit", 413)
                dst.write(block)
            after = os.fstat(src.fileno())
            dst.flush()
            os.fsync(dst.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or sha_file(raw) != source_id or sha_file(original) != source_id:
            fail("source changed during import; evidence retained", "stale_source", 409)
        pcm = directory / "pcm.wav"
        argv = self._base() + ["-protocol_whitelist", "file,pipe", "-f", FORMATS[extension], "-i", str(raw),
                "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-t", str(MAX_DURATION + 0.01),
                "-ar", str(RATE), "-ac", str(CHANNELS), "-c:a", "pcm_s16le", str(pcm)]
        self._run(argv, directory, "decode.stderr")
        try:
            with wave.open(str(pcm), "rb") as audio:
                frames = audio.getnframes()
        except (wave.Error, OSError):
            fail("decoded audio is not valid PCM", "processing_failed", 502)
        duration = frames / RATE
        if not 0 < duration <= MAX_DURATION or duration + sum(s.get("duration_s", 0) for s in sources) > MAX_TOTAL_SOURCE_DURATION:
            fail("decoded source exceeds duration limits; evidence retained")
        record = {"source_id": source_id, "sha256": source_id, "created_at": now(),
                  "original_relative_path": relative, "original_filename": original_filename or original.name,
                  "byte_size": size, "duration_s": duration,
                  "sample_rate": RATE, "channels": CHANNELS, "frames": frames,
                  "raw_path": f"sources/{source_id}/{raw.name}", "pcm_path": f"sources/{source_id}/pcm.wav",
                  "pcm_sha256": sha_file(pcm), "duration_basis": "decoded_pcm_frames", "generation": False}
        record["file_url"] = self._url(pid, record["raw_path"])
        record["pcm_url"] = self._url(pid, record["pcm_path"])
        write_new(directory / "source.json", record)
        return record

    def _sections(self, pid, root, sections):
        if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
            fail(f"sections must contain 1 to {MAX_SECTIONS} clips")
        result, seen, sources, total = [], set(), {}, 0
        for clip in sections:
            keys(clip, {"clip_id", "source_id", "start_s", "end_s", "gain_db", "fade_in_s", "fade_out_s", "label", "confirmation"},
                 {"clip_id", "source_id", "start_s", "end_s", "confirmation"})
            clip_id = clip["clip_id"]
            if not isinstance(clip_id, str) or not ID_RE.fullmatch(clip_id) or clip_id in seen:
                fail("clip_id must be safe and unique within recipe")
            seen.add(clip_id)
            sid = clip["source_id"]
            if not isinstance(sid, str):
                fail("invalid source id")
            if sid not in sources:
                sources[sid] = self._source(pid, root, sid)
            source = sources[sid]
            start = number(clip["start_s"], "start_s", 0, source["duration_s"])
            end = number(clip["end_s"], "end_s", 0, source["duration_s"])
            a, b = math.floor(start * RATE + 0.5), math.floor(end * RATE + 0.5)
            if b <= a or b > source["frames"]:
                fail("clip range must contain at least one source PCM frame")
            length = (b - a) / RATE
            gain = number(clip.get("gain_db", 0), "gain_db", -60, 12)
            fade_in = number(clip.get("fade_in_s", 0), "fade_in_s", 0, length)
            fade_out = number(clip.get("fade_out_s", 0), "fade_out_s", 0, length)
            if fade_in + fade_out > length:
                fail("fades may not overlap within a clip")
            label = clip.get("label", "")
            if not isinstance(label, str) or len(label) > 120:
                fail("label must be text up to 120 characters")
            confirmation = clip["confirmation"]
            keys(confirmation, {"confirmed", "source_sha256", "start_s", "end_s"}, {"confirmed", "source_sha256", "start_s", "end_s"})
            if type(confirmation["confirmed"]) is not bool or confirmation["source_sha256"] != sid:
                fail("boolean review state with exact source SHA is required", "confirmation_required", 409)
            if number(confirmation["start_s"], "confirmation.start_s", 0, source["duration_s"]) != start or number(confirmation["end_s"], "confirmation.end_s", 0, source["duration_s"]) != end:
                fail("listening confirmation range is stale", "confirmation_required", 409)
            normalized = {"clip_id": clip_id, "source_id": sid, "source_sha256": sid,
                          "start_s": start, "end_s": end, "start_frame": a, "end_frame": b,
                          "gain_db": gain, "fade_in_s": fade_in, "fade_out_s": fade_out, "label": label}
            normalized["confirmation"] = {**confirmation, "confirmed_at": now() if confirmation["confirmed"] else None,
                                          "clip_sha256": digest(normalized) if confirmation["confirmed"] else None}
            result.append(normalized)
            total += length
        if total > MAX_DURATION:
            fail("master duration limit exceeded")
        return result, sources, total

    def create_revision(self, pid, request):
        keys(request, {"expected_revision", "sections", "note"}, {"expected_revision", "sections"})
        with self._locked(pid) as root:
            rows = self._expected(root, request["expected_revision"])
            if len(rows) >= MAX_REVISIONS:
                fail("revision limit reached")
            sections, sources, duration = self._sections(pid, root, request["sections"])
            note = request.get("note", "")
            if not isinstance(note, str) or len(note) > 2000:
                fail("note must be text up to 2000 characters")
            record = {"revision": len(rows) + 1, "parent_revision": len(rows),
                      "parent_recipe_sha256": rows[-1]["recipe_sha256"] if rows else None,
                      "created_at": now(), "sections": sections, "note": note, "duration_s": duration,
                      "source_ids": sorted(sources), "generation": False}
            record["recipe_sha256"] = digest({"sections": sections, "note": note})
            write_new(root / "revisions" / f"{record['revision']:06d}.json", record)
            return record

    def render(self, pid, request):
        keys(request, {"expected_revision", "render_id"}, {"expected_revision", "render_id"})
        rid = request["render_id"]
        if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
            fail("render_id must be a safe unique identifier")
        with self._locked(pid) as root:
            rows = self._expected(root, request["expected_revision"])
            if not rows:
                fail("save a confirmed recipe first", "confirmation_required", 409)
            revision = rows[-1]
            prior = self._records(root, "renders")
            if any(r.get("revision") == revision["revision"] or r.get("render_id") == rid for r in prior):
                fail("this revision or render id already has a claim; retries are forbidden", "render_claimed", 409)
            # An interrupted mkdir before claim must also block all rendering:
            # its intended revision cannot safely be reconstructed.
            if any(r.get("revision") is None for r in prior):
                fail("uncertain render claim requires manual evidence review", "render_claimed", 409)
            sources = {sid: self._source(pid, root, sid) for sid in revision["source_ids"]}
            for clip in revision["sections"]:
                without = {k: v for k, v in clip.items() if k != "confirmation"}
                if clip["confirmation"].get("confirmed") is not True or clip["confirmation"].get("clip_sha256") != digest(without):
                    fail("all section boundaries must be listening-confirmed before building a master; save draft progress freely", "confirmation_required", 409)
            directory = confined(root, f"renders/{rid}", exists=False)
            directory.mkdir()
            claim = {"schema_version": 1, "render_id": rid, "revision": revision["revision"],
                     "recipe_sha256": revision["recipe_sha256"], "parent_revision": revision["parent_revision"],
                     "source_ids": revision["source_ids"], "source_hashes": {sid: sources[sid]["sha256"] for sid in sources},
                     "claimed_at": now(), "status": "claimed", "generation": False}
            write_new(directory / "claim.json", claim)
            write_new(directory / "recipe.json", revision)
            try:
                clips = []
                for index, clip in enumerate(revision["sections"]):
                    source = sources[clip["source_id"]]
                    output = directory / f"clip-{index:03d}.wav"
                    length = (clip["end_frame"] - clip["start_frame"]) / RATE
                    filters = [f"atrim=start_sample={clip['start_frame']}:end_sample={clip['end_frame']}",
                               "asetpts=PTS-STARTPTS", f"volume={clip['gain_db']:.9f}dB"]
                    if clip["fade_in_s"]:
                        filters.append(f"afade=t=in:st=0:d={clip['fade_in_s']:.9f}")
                    if clip["fade_out_s"]:
                        filters.append(f"afade=t=out:st={length - clip['fade_out_s']:.9f}:d={clip['fade_out_s']:.9f}")
                    argv = self._base() + ["-protocol_whitelist", "file,pipe", "-f", "wav", "-i", str(confined(root, source["pcm_path"])),
                        "-map", "0:a:0", "-af", ",".join(filters), "-map_metadata", "-1", "-c:a", "pcm_s16le", str(output)]
                    self._run(argv, directory, f"clip-{index:03d}.stderr")
                    clips.append(output)
                wav_path = directory / "master.wav"
                with wav_path.open("xb") as stream:
                    with wave.open(stream, "wb") as master:
                        master.setnchannels(CHANNELS)
                        master.setsampwidth(2)
                        master.setframerate(RATE)
                        for path in clips:
                            with wave.open(str(path), "rb") as audio:
                                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (CHANNELS, 2, RATE):
                                    fail("rendered clip format mismatch", "processing_failed", 502)
                                while block := audio.readframes(65536):
                                    master.writeframesraw(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                with wave.open(str(wav_path), "rb") as audio:
                    frames = audio.getnframes()
                if frames != sum(c["end_frame"] - c["start_frame"] for c in revision["sections"]):
                    fail("master frame count mismatch", "processing_failed", 502)
                quality = wav_quality(wav_path)
                quality_path = directory / "quality.json"
                write_new(quality_path, quality)
                mp3_path = directory / "master.mp3"
                self._run(self._base() + ["-protocol_whitelist", "file,pipe", "-f", "wav", "-i", str(wav_path),
                    "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", "320k", str(mp3_path)], directory, "mp3.stderr")
                # A present/nonempty MP3 is not proof of a playable master.
                # Decode independently with a bound slightly above the expected
                # duration; permit at most two MP3 frames of encoder padding.
                verified_pcm = directory / "mp3-verified.wav"
                self._run(self._base() + ["-protocol_whitelist", "file,pipe", "-f", "mp3", "-i", str(mp3_path),
                    "-map", "0:a:0", "-map_metadata", "-1", "-t", f"{frames / RATE + 0.25:.9f}",
                    "-ar", str(RATE), "-ac", str(CHANNELS), "-c:a", "pcm_s16le", str(verified_pcm)], directory, "mp3-verify.stderr")
                with wave.open(str(verified_pcm), "rb") as decoded:
                    decoded_frames = decoded.getnframes()
                    if (decoded.getnchannels(), decoded.getsampwidth(), decoded.getframerate()) != (CHANNELS, 2, RATE):
                        fail("MP3 verification format mismatch", "processing_failed", 502)
                if decoded_frames <= 0 or abs(decoded_frames - frames) > 2304:
                    fail("MP3 verification duration mismatch", "processing_failed", 502)
                # Detect external source changes during rendering before any
                # output is declared complete. Failed artifacts remain evidence.
                for sid in sources:
                    self._source(pid, root, sid)
                files = {}
                for kind, path in (("wav", wav_path), ("mp3", mp3_path), ("quality", quality_path)):
                    relative = f"renders/{rid}/{path.name}"
                    files[kind] = {"relative_path": relative, "url": self._url(pid, relative),
                                   "sha256": sha_file(path), "byte_size": path.stat().st_size}
                result = {**claim, "status": "completed", "completed_at": now(), "files": files,
                          "duration_s": frames / RATE, "frames": frames, "sample_rate": RATE, "channels": CHANNELS,
                          "mp3_encoded_from": "master.wav", "mp3_input_sha256": files["wav"]["sha256"],
                          "mp3_bitrate_kbps": 320, "mp3_verified": True, "mp3_decoded_frames": decoded_frames,
                          "recipe_file_sha256": sha_file(directory / "recipe.json"), "quality": quality}
                write_new(directory / "result.json", result)
                return result
            except Exception as exc:
                write_new(directory / "result.json", {**claim, "status": "failed", "failed_at": now(),
                          "error": str(exc), "evidence_preserved": True, "retry_allowed": False})
                if isinstance(exc, MasterAudioError):
                    raise
                fail("master render failed; claim and evidence retained", "processing_failed", 502)

    def peaks(self, pid, source_id, bins=1000):
        if isinstance(bins, bool) or not isinstance(bins, int) or not 16 <= bins <= 4096:
            fail("bins must be an integer from 16 to 4096")
        with self._locked(pid) as root:
            source = self._source(pid, root, source_id)
            bins = min(bins, source["frames"])
            peaks = []
            with wave.open(str(confined(root, source["pcm_path"])), "rb") as audio:
                for i in range(bins):
                    count = (i + 1) * source["frames"] // bins - i * source["frames"] // bins
                    low, high = 0, 0
                    while count:
                        take = min(count, 65536)
                        samples = array("h", audio.readframes(take))
                        if os.sys.byteorder != "little":
                            samples.byteswap()
                        if not samples:
                            fail("PCM unexpectedly truncated", "stale_source", 409)
                        low, high = min(low, min(samples)), max(high, max(samples))
                        count -= take
                    peaks.append([round(low / 32768, 6), round(high / 32768, 6)])
            return {"source_id": source_id, "sha256": source_id, "duration_s": source["duration_s"], "bins": bins,
                    "peaks": peaks, "basis": "decoded_pcm_all_channels_min_max"}

    def file(self, pid, relative):
        """Return only hash-verified source audio or completed master outputs."""
        with self._locked(pid) as root:
            path = confined(root, relative)
            parts = relative.split("/")
            if len(parts) == 3 and parts[0] == "sources":
                record = self._source(pid, root, parts[1])
                if relative in {record["raw_path"], record["pcm_path"]}:
                    return path
            if len(parts) == 3 and parts[0] == "renders" and ID_RE.fullmatch(parts[1]):
                result = read_json(confined(root, f"renders/{parts[1]}/result.json"))
                if result.get("status") == "completed":
                    for output in result["files"].values():
                        if output["relative_path"] == relative and sha_file(path) == output["sha256"]:
                            return path
            fail("file is not a registered deliverable", "not_found", 404)


def get_master_audio_service():
    return MasterAudioService()
