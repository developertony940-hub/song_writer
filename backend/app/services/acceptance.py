"""Phase 4 production acceptance and operational-readiness workflow.

Acceptance state is deliberately persisted separately from ordinary project state.
A real run can only be resumed from the exact pending real run it created; a
project, proxy, or offline fixture can never be re-labelled as real acceptance.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.config import get_settings
from app.models.schemas import (
    AssetKind, CandidateRecord, GenerateCopyRequest, GeneratePlanCandidatesRequest,
    PlanCandidateKind, ProjectCreate, ReviewCandidateRequest, VideoPlan,
)
from app.services.preflight import RuntimePreflight
from app.services.provenance import asset_provenance

AcceptanceMode = Literal["offline", "real"]


class AcceptanceError(RuntimeError):
    """An operational acceptance prerequisite or assertion failed."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _plan() -> VideoPlan:
    return VideoPlan.model_validate({
        "platform": "tiktok", "target_duration_s": 6,
        "hook": "Pause for a better local workflow.",
        "cta": "Try the local workflow today.",
        "scenes": [
            {"id": "hook", "start_s": 0, "end_s": 3,
             "voiceover": "Pause for a better local workflow.",
             "caption": "Pause for a better local workflow.",
             "visual_prompt": "Vertical clean product workspace with generous caption-safe space and soft daylight.",
             "motion_prompt": "Slow controlled push in with subtle natural screen glow.",
             "image_candidates": 1, "video_candidates": 1},
            {"id": "cta", "start_s": 3, "end_s": 6,
             "voiceover": "Try the local workflow today.",
             "caption": "Try the local workflow today.",
             "visual_prompt": "Vertical confident call to action on a clean calm workspace background.",
             "motion_prompt": "Gentle camera settle with restrained optimistic movement.",
             "image_candidates": 1, "video_candidates": 1},
        ],
    })


class AcceptanceService:
    """Create and audit a representative acceptance project with durable state."""

    # ``original_mode`` is distinct from the historical ``mode`` field so the
    # initial real-run intent remains explicit in the signed payload.
    _IMMUTABLE_STATE = ("schema_version", "run_id", "mode", "original_mode", "project_id", "created_at")
    _REAL_SCHEMA_VERSION = 4
    _EVIDENCE_SNAPSHOT_VERSION = 1
    _INTEGRITY_ALGORITHM = "HMAC-SHA256"

    def __init__(self, orchestrator: Any | None = None) -> None:
        if orchestrator is None:
            from app.services.orchestrator import get_orchestrator
            orchestrator = get_orchestrator()
        self.orch = orchestrator
        self.store = orchestrator.store
        self.ffmpeg = orchestrator.ffmpeg

    def _state_path(self, project_id: str) -> Path:
        return self.store._dir(project_id) / "reviews" / "acceptance-run.json"

    @staticmethod
    def _canonical_json(value: Any) -> bytes:
        """Stable bytes for HMAC input; project state is JSON-only by contract."""
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _signing_secret(self) -> bytes:
        # Do not cache or write this material. A changed key must make a pending
        # real run fail closed rather than silently start a new chain.
        # Consult the explicit environment alias at call time so changing it
        # between invocations is detected even in a long-lived process.
        secret = os.environ.get("LOCALAI_ACCEPTANCE_SIGNING_SECRET", get_settings().acceptance.signing_secret)
        if not isinstance(secret, str) or not secret.strip():
            raise AcceptanceError(
                "real acceptance requires LOCALAI_ACCEPTANCE_SIGNING_SECRET (or acceptance.signing_secret in a private config); "
                "set one stable private value and resume with the same value"
            )
        return secret.encode("utf-8")

    def _integrity_payload(self, state: dict[str, Any], prior_integrity: str | None) -> bytes:
        # Sign the whole state except its self-referential envelope. This covers
        # immutable identity, original mode, every transition/history entry,
        # proxy binding, operator approval, and the prior chain value.
        unsigned = {key: value for key, value in state.items() if key != "integrity"}
        return self._canonical_json({
            "version": 1,
            "prior_integrity": prior_integrity,
            "state": unsigned,
        })

    def _seal_real_state(self, state: dict[str, Any], prior_integrity: str | None) -> None:
        value = hmac.new(self._signing_secret(), self._integrity_payload(state, prior_integrity), hashlib.sha256).hexdigest()
        state["integrity"] = {"version": 1, "algorithm": self._INTEGRITY_ALGORITHM,
                              "prior_integrity": prior_integrity, "value": value}

    def _verify_real_state(self, state: dict[str, Any], project_id: str) -> None:
        if not isinstance(state, dict) or state.get("schema_version") != self._REAL_SCHEMA_VERSION:
            raise AcceptanceError("real acceptance state has no supported signed schema; it cannot be resumed or certified")
        if (state.get("project_id") != project_id or state.get("mode") != "real" or
                state.get("original_mode") != "real" or not isinstance(state.get("run_id"), str) or
                not state["run_id"] or not isinstance(state.get("history"), list) or not state["history"] or
                not isinstance(state.get("evidence_snapshots"), list)):
            raise AcceptanceError("real acceptance state identity/mode/original mode is invalid; it cannot be resumed or certified")
        integrity = state.get("integrity")
        if not isinstance(integrity, dict) or integrity.get("version") != 1 or integrity.get("algorithm") != self._INTEGRITY_ALGORITHM:
            raise AcceptanceError("real acceptance state lacks a valid origin signature; offline or unsigned records cannot be certified as real")
        prior, actual = integrity.get("prior_integrity"), integrity.get("value")
        if prior is not None and (not isinstance(prior, str) or len(prior) != 64):
            raise AcceptanceError("real acceptance state has an invalid integrity chain")
        if not isinstance(actual, str) or len(actual) != 64:
            raise AcceptanceError("real acceptance state has an invalid integrity signature")
        expected = hmac.new(self._signing_secret(), self._integrity_payload(state, prior), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise AcceptanceError(
                "real acceptance state signature is invalid: the signing secret changed or project state was modified; "
                "restore the original LOCALAI_ACCEPTANCE_SIGNING_SECRET and untampered record"
            )

    def _create_state(self, project_id: str, mode: AcceptanceMode) -> dict[str, Any]:
        path = self._state_path(project_id)
        if path.exists():
            raise AcceptanceError("project already has an acceptance-run record and cannot start a second acceptance")
        state = {"schema_version": self._REAL_SCHEMA_VERSION, "run_id": uuid.uuid4().hex, "mode": mode,
                 "original_mode": mode, "project_id": project_id, "created_at": _now(), "state": "created",
                 "history": [{"at": _now(), "state": "created"}], "evidence_snapshots": []}
        if mode == "real":
            self._seal_real_state(state, prior_integrity=None)
        self._write_state(state)
        return state

    def _load_state(self, project_id: str, mode: AcceptanceMode) -> dict[str, Any]:
        path = self._state_path(project_id)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AcceptanceError("project is not an acceptance run; arbitrary projects cannot be resumed or certified") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise AcceptanceError(f"acceptance-run state is unreadable: {exc}") from exc
        if not isinstance(state, dict):
            raise AcceptanceError("acceptance-run state is not a JSON object")
        if mode == "real":
            # Verify before any state-dependent resume action. A forged offline
            # ``mode=real`` record has no real-origin HMAC and fails here.
            self._verify_real_state(state, project_id)
        elif state.get("schema_version") != self._REAL_SCHEMA_VERSION:
            raise AcceptanceError("acceptance-run state has an unsupported schema")
        if state.get("project_id") != project_id or state.get("mode") != mode or not isinstance(state.get("run_id"), str):
            raise AcceptanceError("acceptance-run identity/mode does not match this invocation")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        for key in self._IMMUTABLE_STATE:
            if key not in state:
                raise AcceptanceError(f"acceptance-run state is missing immutable field {key}")
        path = self._state_path(str(state["project_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)

    def _transition(self, state: dict[str, Any], next_state: str, **changes: Any) -> dict[str, Any]:
        immutable = {key: state[key] for key in self._IMMUTABLE_STATE}
        prior_integrity: str | None = None
        if state.get("mode") == "real":
            self._verify_real_state(state, str(state["project_id"]))
            prior_integrity = state["integrity"]["value"]
        state.update(changes)
        if any(state.get(key) != value for key, value in immutable.items()):
            raise AcceptanceError("attempted to alter immutable acceptance-run identity")
        state["state"] = next_state
        state.setdefault("history", []).append({"at": _now(), "state": next_state})
        if state.get("mode") == "real":
            self._seal_real_state(state, prior_integrity)
        self._write_state(state)
        return state

    def _project_evidence(self, project_id: str, *, include_composition: bool) -> dict[str, Any]:
        """Canonical live evidence used by signed real-run checkpoints.

        The registry metadata is intentionally retained in full: provenance and
        source markers are evidence, not advisory display fields.  Asset bytes
        are addressed by both their registered path and a fresh SHA-256.
        """
        project = self.store.get(project_id)
        assets: list[dict[str, Any]] = []
        for asset in sorted(self.store.list_assets(project_id), key=lambda item: item.id):
            try:
                path = self.store.asset_path(project_id, asset.id)
            except Exception as exc:
                raise AcceptanceError(f"asset {asset.id} has an invalid registered path: {exc}") from exc
            if not path.is_file():
                raise AcceptanceError(f"asset {asset.id} is missing from its registered path")
            meta = json.loads(self._canonical_json(asset.meta).decode("utf-8"))
            assets.append({
                "id": asset.id, "kind": asset.kind.value, "relative_path": asset.relative_path,
                "sha256": _digest(path), "meta": meta,
                "provenance": meta.get("provenance"), "source": meta.get("source"),
                "acceptance_fixture": bool(meta.get("acceptance_fixture")),
            })
        candidates: list[dict[str, Any]] = []
        for raw in self.store.list_candidates(project_id):
            # Preserve every registry row, including malformed rows.  Otherwise
            # a later corrupt/delete edit could disappear from the binding.
            candidates.append(json.loads(self._canonical_json(raw).decode("utf-8")))
        candidates.sort(key=lambda item: self._canonical_json(item))
        plan = project.video_plan
        if not isinstance(plan, dict):
            raise AcceptanceError("real acceptance evidence requires a canonical VideoPlan")
        linkage: dict[str, Any] = {
            "video_plan": json.loads(self._canonical_json(plan).decode("utf-8")),
            "production_plan_asset_id": project.artifacts.get("production_plan"),
            "composition": None,
            "composition_digest": None,
        }
        if include_composition:
            try:
                composition = self.orch.production.composition(project_id)
                linkage["composition"] = composition.model_dump(mode="json")
                linkage["composition_digest"] = self.orch._composition_digest(composition)
            except Exception as exc:
                raise AcceptanceError(f"could not bind current production composition: {exc}") from exc
        return {"assets": assets, "candidates": candidates, "linkage": linkage}

    def _capture_evidence_snapshot(self, project_id: str, label: str, *, include_composition: bool = False) -> dict[str, Any]:
        """Capture trusted in-process evidence immediately before signing a transition."""
        self._assert_no_synthetic_fixture_origin(project_id)
        return {"version": self._EVIDENCE_SNAPSHOT_VERSION, "label": label, "recorded_at": _now(),
                "evidence": self._project_evidence(project_id, include_composition=include_composition)}

    def _snapshot(self, state: dict[str, Any], label: str) -> dict[str, Any]:
        snapshots = state.get("evidence_snapshots")
        if not isinstance(snapshots, list):
            raise AcceptanceError("real acceptance state has no evidence-snapshot chain")
        for snapshot in reversed(snapshots):
            if isinstance(snapshot, dict) and snapshot.get("label") == label:
                return snapshot
        raise AcceptanceError(f"real acceptance state is missing required signed evidence snapshot {label!r}")

    def _verify_evidence_snapshot(self, project_id: str, state: dict[str, Any], label: str, *, candidates: bool = True,
                                  allow_additions: bool = False) -> None:
        """Fail closed when signed evidence no longer describes the live project.

        Candidate review fields legitimately change between a candidate-generation
        checkpoint and its later human-review checkpoint.  At that one boundary
        callers set ``candidates=False``; all assets, registry metadata, paths,
        plan linkage, and bytes remain strictly bound.  Before the next worker
        action the reviewed candidate evidence is captured in a new signed step.
        """
        snapshot = self._snapshot(state, label)
        if snapshot.get("version") != self._EVIDENCE_SNAPSHOT_VERSION or not isinstance(snapshot.get("evidence"), dict):
            raise AcceptanceError(f"signed evidence snapshot {label!r} has an unsupported format")
        expected = snapshot["evidence"]
        try:
            # Newly registered fixture assets must also fail; they are not made
            # harmless merely because a predecessor checkpoint predates them.
            self._assert_no_synthetic_fixture_origin(project_id)
            include_composition = expected.get("linkage", {}).get("composition") is not None
            actual = self._project_evidence(project_id, include_composition=include_composition)
        except AcceptanceError:
            raise
        except Exception as exc:
            raise AcceptanceError(f"could not verify signed evidence snapshot {label!r}: {exc}") from exc
        if allow_additions:
            # A trusted subsequent worker can append output after this snapshot
            # (e.g. video candidates after accepted image evidence). Every
            # pre-existing bound row must still match exactly; only additions
            # are permitted before the next immediate signed checkpoint.
            actual_assets = {item["id"]: item for item in actual["assets"]}
            try:
                actual["assets"] = [actual_assets[item["id"]] for item in expected["assets"]]
            except KeyError:
                actual["assets"] = []  # force a clear mismatch below
            if candidates:
                remaining = list(actual["candidates"])
                matched: list[dict[str, Any]] = []
                for item in expected["candidates"]:
                    try:
                        index = next(i for i, current in enumerate(remaining)
                                     if self._canonical_json(current) == self._canonical_json(item))
                    except StopIteration:
                        matched = []  # force a clear mismatch below
                        break
                    matched.append(remaining.pop(index))
                actual["candidates"] = matched
        if not candidates:
            expected = {key: value for key, value in expected.items() if key != "candidates"}
            actual = {key: value for key, value in actual.items() if key != "candidates"}
        if not hmac.compare_digest(self._canonical_json(expected), self._canonical_json(actual)):
            raise AcceptanceError(
                f"signed evidence snapshot {label!r} no longer matches project registry, metadata, candidate evidence, paths, or bytes"
            )
        # The signed historical metadata is decisive even if an attacker later
        # removes the marker from assets.json before this comparison.
        fixtures = [asset["id"] for asset in snapshot["evidence"].get("assets", [])
                    if asset.get("acceptance_fixture") or asset.get("source") == "phase4_deterministic_synthetic"]
        if fixtures:
            raise AcceptanceError("real certification rejects deterministic acceptance-fixture provenance: " + ", ".join(fixtures))

    def _transition_with_snapshot(self, state: dict[str, Any], next_state: str, project_id: str, label: str, *,
                                  include_composition: bool = False, **changes: Any) -> dict[str, Any]:
        snapshot = self._capture_evidence_snapshot(project_id, label, include_composition=include_composition)
        snapshots = [*state.get("evidence_snapshots", []), snapshot]
        return self._transition(state, next_state, evidence_snapshots=snapshots, **changes)

    async def _offline_preflight(self) -> dict[str, Any]:
        try:
            status = await self.ffmpeg.status()
            root = self.store.root
            root.mkdir(parents=True, exist_ok=True)
            probe = root / f".acceptance-write-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
            feature = status.get("features", {})
            ffmpeg_ok = bool(status.get("online")) and bool(status.get("ffprobe_available")) and all(
                feature.get(k, False) for k in ("libx264", "aac", "ass", "loudnorm"))
            checks = {
                "paths": {"ok": True, "projects_root": str(root)},
                "ffmpeg": {"ok": ffmpeg_ok, "status": status,
                           "errors": [] if ffmpeg_ok else ["ffmpeg/ffprobe with libx264, aac, ass, and loudnorm are required"]},
                "lmstudio": {"ok": None, "not_contacted": "offline mode deliberately does not use AI services"},
                "comfyui": {"ok": None, "not_contacted": "offline mode deliberately does not use AI services"},
                "tts": {"ok": None, "not_contacted": "offline mode deliberately does not use AI services"},
            }
            return {"ready": checks["paths"]["ok"] and checks["ffmpeg"]["ok"], "mode": "offline", "checks": checks}
        except Exception as exc:
            return {"ready": False, "mode": "offline", "checks": {"paths": {"ok": False, "error": str(exc)}}}

    async def _preflight(self, mode: AcceptanceMode) -> dict[str, Any]:
        try:
            return await (self._offline_preflight() if mode == "offline" else RuntimePreflight(
                lmstudio=self.orch.lms, comfyui=self.orch.comfy, tts=self.orch.tts, ffmpeg=self.ffmpeg).run())
        except Exception as exc:
            return {"ready": False, "checks": {"preflight": {"ok": False, "errors": [str(exc)]}}}

    async def _synthetic_media(self, project_id: str, scene_id: str, color: str, tone: int) -> tuple[Any, Any]:
        video_path = self.store.asset_dir(project_id, AssetKind.video) / f"acceptance_{scene_id}.mp4"
        audio_path = self.store.asset_dir(project_id, AssetKind.audio) / f"acceptance_{scene_id}.wav"
        rc, err = await self.ffmpeg._run(["-f", "lavfi", "-i", f"color=c={color}:s=320x568:r=30:d=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", str(video_path)])
        if rc != 0: raise AcceptanceError(f"could not create deterministic synthetic video: {err}")
        rc, err = await self.ffmpeg._run(["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration=3",
            "-af", "volume=0.12", "-ac", "2", "-ar", "48000", "-y", str(audio_path)])
        if rc != 0: raise AcceptanceError(f"could not create deterministic synthetic audio: {err}")
        provenance = asset_provenance(prompt_id="asset.user_supplied", rendered_prompt="Phase 4 deterministic FFmpeg fixture")
        video = self.store.save_asset(project_id, AssetKind.video, video_path.name, None, meta={"source": "phase4_deterministic_synthetic", "scene_id": scene_id, "provenance": provenance})
        audio = self.store.save_asset(project_id, AssetKind.audio, audio_path.name, None, meta={"source": "phase4_deterministic_synthetic", "scene_id": scene_id, "provenance": provenance})
        return video, audio

    def _add_fixture_candidate(self, project_id: str, plan_fp: str, scene_id: str, kind: PlanCandidateKind,
                               asset_id: str, reference_asset_id: str | None = None) -> CandidateRecord:
        record = CandidateRecord(id=uuid.uuid4().hex[:12], project_id=project_id, plan_fingerprint=plan_fp,
            scene_id=scene_id, kind=kind, asset_id=asset_id, seed=0, reference_asset_id=reference_asset_id)
        self.store.add_candidate(project_id, record.model_dump(mode="json"))
        self.store.update_asset_meta(project_id, asset_id, plan_fingerprint=plan_fp, scene_id=scene_id,
            candidate_id=record.id, candidate_kind=kind.value, acceptance_fixture=True)
        return record

    async def _run_offline(self, project_id: str, plan: VideoPlan) -> Any:
        result = self.orch.save_video_plan(project_id, plan, provenance=asset_provenance(
            prompt_id="asset.user_supplied", rendered_prompt="Phase 4 deterministic acceptance plan"))
        fp = result["fingerprint"]
        videos: dict[str, Any] = {}; images: dict[str, Any] = {}
        for scene, color, tone in zip(plan.scenes, ("0x264653", "0xe76f51"), (440, 554), strict=True):
            video, audio = await self._synthetic_media(project_id, scene.id, color, tone)
            videos[scene.id] = video
            self.store.update_asset_meta(project_id, audio.id, plan_fingerprint=fp, scene_id=scene.id,
                source="VideoPlan.voiceover", timing={"start_s": scene.start_s, "end_s": scene.end_s}, acceptance_fixture=True)
            image_path = self.store.asset_dir(project_id, AssetKind.image) / f"acceptance_{scene.id}.png"
            rc, err = await self.ffmpeg._run(["-f", "lavfi", "-i", f"color=c={color}:s=320x568", "-frames:v", "1", "-y", str(image_path)])
            if rc != 0: raise AcceptanceError(f"could not create deterministic synthetic image: {err}")
            images[scene.id] = self.store.save_asset(project_id, AssetKind.image, image_path.name, None,
                meta={"source": "phase4_deterministic_synthetic", "provenance": asset_provenance(prompt_id="asset.user_supplied", rendered_prompt="Phase 4 deterministic image fixture")})
        # Exercise the normal review method (selection exclusivity, timestamps,
        # stage evidence, and preview invalidation), without claiming a human watched fixtures.
        selected_images: dict[str, Any] = {}
        for scene in plan.scenes:
            candidate = self._add_fixture_candidate(project_id, fp, scene.id, PlanCandidateKind.image, images[scene.id].id)
            selected_images[scene.id] = self.orch.review_candidate(project_id, candidate.id, ReviewCandidateRequest(
                status="accepted", select=True, reason="Automated deterministic fixture selection for technical acceptance; not human creative review."))
        for scene in plan.scenes:
            candidate = self._add_fixture_candidate(project_id, fp, scene.id, PlanCandidateKind.video, videos[scene.id].id, selected_images[scene.id].asset_id)
            self.orch.review_candidate(project_id, candidate.id, ReviewCandidateRequest(
                status="accepted", select=True, reason="Automated deterministic fixture selection for technical acceptance; not human creative review."))
        # The normal audio evidence path is used; no public/human stage approvals are fabricated.
        if self.store.get(project_id).stages["audio"] != "approved": self.store.update_stage(project_id, "audio", "succeeded")
        proxy = await self.orch.render_plan_preview(project_id, "phase4_proxy")
        self.orch.approve_plan_preview(project_id, proxy.id)
        return await self.orch.render_plan_master(project_id, "phase4_master")

    def _pending(self, report: dict[str, Any], state: dict[str, Any], status: str, action: str) -> None:
        report["status"], report["operator_action"] = status, action
        report["acceptance_run"] = state

    async def _fresh_real(self, project_id: str, state: dict[str, Any], report: dict[str, Any]) -> None:
        plan = _plan()
        await self.orch.generate_copy(GenerateCopyRequest(project_id=project_id,
            prompt="Write one concise sentence confirming this is a local production acceptance run.", max_tokens=96,
            save_as="phase4_service_copy.md"))
        self.orch.save_video_plan(project_id, plan, provenance=asset_provenance(prompt_id="asset.user_supplied", rendered_prompt="Phase 4 representative acceptance plan"))
        await self.orch.generate_plan_candidates(GeneratePlanCandidatesRequest(project_id=project_id, kind=PlanCandidateKind.image))
        # The generator returned and registered these assets in-process.  Seal
        # their bytes/metadata before giving the UI a chance to review them.
        state = self._transition_with_snapshot(state, "awaiting_image_review", project_id, "image_candidates_generated")
        self._pending(report, state, "pending_operator_candidate_review", "Review and select one image candidate per scene in the UI. Do not approve pipeline stages; resume this exact run with --mode real --project-id after candidate review.")

    async def _resume_real(self, project_id: str, state: dict[str, Any], report: dict[str, Any], *, operator_approved: bool, operator_rejected: bool) -> Any | None:
        # Keep this guard here as well as in _load_state: internal callers and
        # future entry points must not accidentally resume an unsigned record.
        self._verify_real_state(state, project_id)
        if operator_approved and operator_rejected:
            raise AcceptanceError("operator approval and rejection are mutually exclusive")
        self.orch.production.get_plan(project_id)
        current = state.get("state")
        if current in {"completed", "failed", "master_rendered"}:
            raise AcceptanceError("this acceptance run is closed and cannot be resumed")

        if operator_approved:
            if current != "awaiting_proxy_review":
                raise AcceptanceError("--operator-approved is valid only for the exact pending proxy recorded by this acceptance run")
            self._verify_evidence_snapshot(project_id, state, "proxy_ready")
            proxy = self._assert_proxy_binding(project_id, state)
            self.orch.approve_plan_preview(project_id, proxy.id)
            # Persist the attestation before master work.  The proxy ID/hash and
            # source-media digest make this an evidence event, not a bare flag.
            state = self._transition(state, "operator_approved", operator_approval={
                "at": _now(), "project_id": project_id, "run_id": state["run_id"],
                "proxy_asset_id": proxy.id, "proxy_sha256": state["proxy"]["sha256"],
                "source_media_digest": state["proxy"]["source_media_digest"],
                "source_media": state["proxy"]["source_media"],
            })
            current = "operator_approved"
        if operator_rejected:
            if current != "awaiting_proxy_review":
                raise AcceptanceError("--operator-rejected is valid only while this exact proxy is awaiting review")
            self._verify_evidence_snapshot(project_id, state, "proxy_ready")
            self._assert_proxy_binding(project_id, state)
            self.store.set_preview_approval(project_id, None)
            state = self._transition(state, "awaiting_remediation")
            self._pending(report, state, "pending_operator_remediation", "Proxy rejection was recorded. Revise/review candidates in the UI (do not approve stages), then resume this exact run without an operator flag to render a new proxy.")
            return None
        if current == "operator_approved":
            # A crashed render can safely be retried: the signed approval,
            # exact proxy bytes, and exact source-media binding are revalidated.
            self._verify_evidence_snapshot(project_id, state, "proxy_ready")
            self._assert_operator_approval(project_id, state)
            self._assert_no_synthetic_fixture_origin(project_id)
            master = await self.orch.render_plan_master(project_id, "phase4_master")
            self._transition_with_snapshot(state, "master_rendered", project_id, "master_rendered", include_composition=True)
            return master

        if current == "awaiting_image_review":
            # Candidate review is the one intentional external edit since the
            # generation checkpoint; asset bytes/metadata remain immutable.
            self._verify_evidence_snapshot(project_id, state, "image_candidates_generated", candidates=False)
            image = self.orch.production.candidate_evidence(project_id, PlanCandidateKind.image)
            if not image["ok"]:
                self._pending(report, state, "pending_operator_candidate_review", "Review/select valid image candidates for every scene, then resume this exact real run.")
                return None
            # Seal selected/reviewed image evidence before any video worker can
            # yield.  A crash resumes from this exact signed checkpoint.
            state = self._transition_with_snapshot(state, "generating_video", project_id, "image_candidates_accepted")
            current = "generating_video"

        if current == "generating_video":
            labels = {"image_candidates_accepted", "remediation_images_accepted"}
            label = next((item.get("label") for item in reversed(state["evidence_snapshots"])
                          if isinstance(item, dict) and item.get("label") in labels), None)
            if label is None:
                raise AcceptanceError("video generation has no signed accepted-image evidence")
            self._verify_evidence_snapshot(project_id, state, label)
            await self.orch.generate_plan_candidates(GeneratePlanCandidatesRequest(project_id=project_id, kind=PlanCandidateKind.video))
            state = self._transition_with_snapshot(state, "awaiting_video_review", project_id, "video_candidates_generated")
            self._pending(report, state, "pending_operator_candidate_review", "Review and select one motion candidate per scene in the UI, then resume this exact real run. No proxy exists yet.")
            return None

        if current == "awaiting_video_review":
            # Video review is intentionally external, but the selected image
            # checkpoint and all generated video asset bytes remain bound.
            image_labels = {"image_candidates_accepted", "remediation_images_accepted"}
            image_label = next((item.get("label") for item in reversed(state["evidence_snapshots"])
                                if isinstance(item, dict) and item.get("label") in image_labels), None)
            if image_label is None:
                raise AcceptanceError("motion review has no signed accepted-image evidence")
            self._verify_evidence_snapshot(project_id, state, image_label, allow_additions=True)
            self._verify_evidence_snapshot(project_id, state, "video_candidates_generated", candidates=False)
            video = self.orch.production.candidate_evidence(project_id, PlanCandidateKind.video)
            if not video["ok"]:
                self._pending(report, state, "pending_operator_candidate_review", "Review/select valid motion candidates for every scene, then resume this exact real run.")
                return None
            state = self._transition_with_snapshot(state, "generating_proxy", project_id, "video_candidates_accepted")
            current = "generating_proxy"

        if current == "awaiting_remediation":
            # A rejected proxy deliberately opens a new review boundary.  Its
            # old snapshot was checked at rejection; newly selected evidence is
            # sealed before any subsequent worker call.
            image = self.orch.production.candidate_evidence(project_id, PlanCandidateKind.image)
            if not image["ok"]:
                self._pending(report, state, "pending_operator_candidate_review", "Review/select valid image candidates for every scene, then resume this exact real run.")
                return None
            video = self.orch.production.candidate_evidence(project_id, PlanCandidateKind.video)
            if not video["ok"]:
                state = self._transition_with_snapshot(state, "generating_video", project_id, "remediation_images_accepted")
                current = "generating_video"
            else:
                state = self._transition_with_snapshot(state, "generating_proxy", project_id, "remediation_inputs_accepted")
                current = "generating_proxy"

        if current == "generating_proxy":
            # Choose the precise signed predecessor for an initial or remediated
            # proxy.  Both capture candidate review/selection evidence in full.
            labels = {"video_candidates_accepted", "remediation_inputs_accepted"}
            label = next((item.get("label") for item in reversed(state["evidence_snapshots"])
                          if isinstance(item, dict) and item.get("label") in labels), None)
            if label is None:
                raise AcceptanceError("proxy generation has no signed accepted-candidate evidence")
            self._verify_evidence_snapshot(project_id, state, label)
            # Audio is generated only after the operator selected real motion.
            # Seal its returned registry/file evidence before the next await
            # (the proxy renderer), rather than relying on mutable assets.json.
            if not self.orch.production.audio_evidence(project_id)["ok"]:
                await self.orch.generate_plan_audio(project_id)
                state = self._transition_with_snapshot(state, "rendering_proxy", project_id, "audio_generated")
                current = "rendering_proxy"
            else:
                current = "rendering_proxy"

        if current == "rendering_proxy":
            # Newly generated audio has its own immediate checkpoint; reused
            # remediation audio was already part of the accepted-input snapshot.
            latest_snapshot = state["evidence_snapshots"][-1] if state["evidence_snapshots"] else None
            if isinstance(latest_snapshot, dict) and latest_snapshot.get("label") == "audio_generated":
                self._verify_evidence_snapshot(project_id, state, "audio_generated")
            counter = int(state.get("proxy_generation", 0)) + 1
            proxy = await self.orch.render_plan_preview(project_id, f"phase4_proxy_{state['run_id'][:12]}_{counter}")
            binding = self._proxy_binding(project_id, proxy)
            state = self._transition_with_snapshot(state, "awaiting_proxy_review", project_id, "proxy_ready",
                                                   include_composition=True, proxy_generation=counter, proxy=binding)
            self._pending(report, state, "pending_operator_proxy_review", f"Open and review proxy asset {proxy.id}. Use --operator-approved only after viewing these exact bytes; use --operator-rejected to record a rejection and remediate.")
            return None

        raise AcceptanceError(f"acceptance run is in unexpected state {current!r}")

    def _proxy_binding(self, project_id: str, proxy: Any) -> dict[str, Any]:
        path = self.store.asset_path(project_id, proxy.id)
        if not path.is_file(): raise AcceptanceError("rendered proxy disappeared before it could be recorded")
        return {"asset_id": proxy.id, "sha256": _digest(path), "source_media_digest": proxy.meta.get("source_media_digest"),
                "source_media": proxy.meta.get("source_media"), "recorded_at": _now()}

    def _assert_proxy_binding(self, project_id: str, state: dict[str, Any]) -> Any:
        binding = state.get("proxy")
        if not isinstance(binding, dict): raise AcceptanceError("acceptance run has no recorded pending proxy identity")
        proxy = self.store.get_asset(project_id, str(binding.get("asset_id", "")))
        path = self.store.asset_path(project_id, proxy.id)
        if proxy.kind != AssetKind.preview or not path.is_file() or _digest(path) != binding.get("sha256"):
            raise AcceptanceError("the pending proxy bytes changed or are missing; render and review a new proxy")
        evidence = self.orch.composition_media_evidence(project_id)
        if (binding.get("source_media_digest") != evidence["digest"] or proxy.meta.get("source_media_digest") != evidence["digest"] or
                binding.get("source_media") != evidence["media"] or proxy.meta.get("source_media") != evidence["media"]):
            raise AcceptanceError("reviewed proxy source media changed; render and review a new proxy")
        if proxy.meta.get("proxy_sha256") != binding.get("sha256"):
            raise AcceptanceError("proxy registry hash does not bind the reviewed proxy bytes")
        return proxy

    def _assert_operator_approval(self, project_id: str, state: dict[str, Any]) -> Any:
        """Require the signed, complete attestation before any master work."""
        proxy = self._assert_proxy_binding(project_id, state)
        binding, approval = state.get("proxy"), state.get("operator_approval")
        if not isinstance(binding, dict) or not isinstance(approval, dict):
            raise AcceptanceError("operator-approved real acceptance state lacks a complete signed approval record")
        required = {"at", "project_id", "run_id", "proxy_asset_id", "proxy_sha256", "source_media_digest", "source_media"}
        if not required.issubset(approval) or not isinstance(approval.get("at"), str) or not approval["at"]:
            raise AcceptanceError("operator-approved real acceptance state has an incomplete approval record")
        expected = {
            "project_id": project_id, "run_id": state.get("run_id"), "proxy_asset_id": binding.get("asset_id"),
            "proxy_sha256": binding.get("sha256"), "source_media_digest": binding.get("source_media_digest"),
            "source_media": binding.get("source_media"),
        }
        if any(approval.get(key) != value for key, value in expected.items()) or approval["proxy_asset_id"] != proxy.id:
            raise AcceptanceError("operator approval does not bind the exact recorded proxy and source media")
        return proxy

    @staticmethod
    def _is_synthetic_fixture(asset: Any) -> bool:
        meta = getattr(asset, "meta", {}) or {}
        return bool(meta.get("acceptance_fixture")) or meta.get("source") == "phase4_deterministic_synthetic"

    def _assert_no_synthetic_fixture_origin(self, project_id: str) -> None:
        # This intentionally checks the complete project registry rather than
        # only the current EDL, so a fixture cannot be swapped into a real run
        # during candidate remediation and later hidden by a changed EDL.
        fixtures = [asset.id for asset in self.store.list_assets(project_id) if self._is_synthetic_fixture(asset)]
        if fixtures:
            raise AcceptanceError("real certification rejects deterministic acceptance-fixture provenance: " + ", ".join(fixtures))

    async def _audit_master_async(self, project_id: str, master: Any, plan: VideoPlan, *, real_certification: bool = False) -> dict[str, Any]:
        composition = self.orch.production.composition(project_id)
        digest = self.orch._composition_digest(composition)
        path = self.store.asset_path(project_id, master.id)
        errors: list[str] = []
        if real_certification:
            try:
                self._assert_no_synthetic_fixture_origin(project_id)
            except AcceptanceError as exc:
                errors.append(str(exc))
        exists = path.is_file() and path.stat().st_size > 0
        current_hash = _digest(path) if exists else None
        if not exists: errors.append("registered master file is missing or empty")
        independent_qc: dict[str, Any] | None = None
        if exists:
            try:
                independent_qc = await self.ffmpeg.quality_gate(path, plan.target_duration_s)
                independent_qc["media_sha256"] = current_hash
            except Exception as exc:
                errors.append(f"independent ffprobe/loudness QC failed: {exc}")
        qc = master.meta.get("qc")
        if not isinstance(qc, dict) or not qc.get("passed"): errors.append("master lacks passed FFmpeg QC metadata")
        if qc and qc.get("media_sha256") != current_hash: errors.append("persisted master QC is not bound to current master bytes")
        if master.meta.get("master_sha256") != current_hash: errors.append("master metadata is not bound to current master bytes")
        if master.meta.get("plan_fingerprint") != self.store.get(project_id).video_plan.get("fingerprint"): errors.append("master plan fingerprint is stale or mismatched")
        if master.meta.get("composition_digest") != digest: errors.append("master composition digest is stale or mismatched")
        source = self.orch.composition_media_evidence(project_id)
        if master.meta.get("source_media_digest") != source["digest"] or master.meta.get("source_media") != source["media"]:
            errors.append("master inputs do not match current byte-level source media hashes")
        approval = self.store.get(project_id).preview_approval or {}
        if approval.get("proxy_sha256") != master.meta.get("approved_preview_sha256"):
            errors.append("master is not bound to the approved proxy bytes")
        if approval.get("source_media_digest") != source["digest"]:
            errors.append("approved proxy is not bound to current source media")
        required_assets = [master]
        for item in source["media"]:
            try: required_assets.append(self.store.get_asset(project_id, item["asset_id"]))
            except KeyError: errors.append("a source media asset disappeared")
        artifact_id = self.store.get(project_id).artifacts.get("production_plan")
        if artifact_id:
            try: required_assets.append(self.store.get_asset(project_id, artifact_id))
            except KeyError: errors.append("canonical plan artifact is missing")
        else: errors.append("canonical plan artifact is missing")
        try: required_assets.append(self.store.get_asset(project_id, approval.get("preview_asset_id", "")))
        except KeyError: errors.append("approved proxy registry record is missing")
        for asset in required_assets:
            prov = asset.meta.get("provenance")
            if not isinstance(prov, dict) or not isinstance(prov.get("prompt"), dict) or not prov["prompt"].get("id") or not prov["prompt"].get("template_sha256"):
                errors.append(f"asset {asset.id} has incomplete provenance")
        if not isinstance(master.meta.get("provenance", {}).get("render_profile"), dict): errors.append("master render-profile provenance is missing")
        expected = [{"start_s": s.start_s, "end_s": s.end_s, "text": s.caption} for s in plan.scenes]
        actual = [{"start_s": s.start_s, "end_s": s.end_s, "text": s.text} for s in composition.subtitles]
        if actual != expected: errors.append("rendered caption timeline does not match the canonical VideoPlan")
        stage = self.store.stage_output_summary(project_id, "final")
        if not stage.get("ok"): errors.append("final stage evidence gate rejected the master")
        return {"passed": not errors, "errors": errors, "master": {"asset_id": master.id, "path": str(path), "sha256": current_hash, "bytes": path.stat().st_size if exists else 0, "meta": master.meta}, "qc": independent_qc, "persisted_qc": qc, "caption_timeline": actual, "captions": {"expected": expected, "actual": actual, "matches_plan": actual == expected}, "stage_evidence": stage}

    def _audit_master(self, project_id: str, master: Any, plan: VideoPlan) -> dict[str, Any]:
        """Synchronous compatibility entry point for operational/test callers.

        The acceptance runner uses ``_audit_master_async`` so its independent
        ffprobe/loudness pass never nests an event loop.
        """
        import asyncio
        return asyncio.run(self._audit_master_async(project_id, master, plan))

    def _write_report(self, project_id: str, report: dict[str, Any]) -> dict[str, str]:
        d = self.store._dir(project_id) / "reviews"; d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = report.get("acceptance_run", {}).get("run_id", uuid.uuid4().hex)[:12]
        json_path, md_path = d / f"phase4-acceptance-{stamp}-{suffix}.json", d / f"phase4-acceptance-{stamp}-{suffix}.md"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        audit = report.get("master_audit") or {}
        lines = ["# Phase 4 Production Acceptance", "", f"- **Status:** {report['status']}", f"- **Mode:** {report['mode']}", f"- **Project:** `{project_id}`", f"- **Run ID:** `{report.get('acceptance_run', {}).get('run_id', 'unavailable')}`", "", "## Service preflight", ""]
        for name, check in report.get("preflight", {}).get("checks", {}).items():
            state = "not contacted" if check.get("ok") is None else ("PASS" if check.get("ok") else "FAIL")
            lines.append(f"- **{name}:** {state}")
            for error in check.get("errors", []): lines.append(f"  - {error}")
        lines += ["", "## Master and caption evidence", ""]
        if audit: lines += [f"- **Technical master audit (independent):** {'PASS' if audit['passed'] else 'FAIL'}", f"- **Master SHA-256:** `{audit['master']['sha256']}`", f"- **Caption segments checked:** {len(audit['caption_timeline'])}"]
        else: lines.append("- No master was rendered.")
        lines += ["", "## Operator action", "", report["operator_action"], "", "Offline results prove deterministic plumbing only. Real mode never fabricates candidate or proxy approval: select candidates in the UI, review the recorded exact proxy bytes, and explicitly approve or reject that proxy. A technical pass is not an external delivery approval.", ""]
        md_path.write_text("\n".join(lines), encoding="utf-8")
        return {"json": str(json_path), "markdown": str(md_path)}

    async def run(self, mode: AcceptanceMode = "offline", *, operator_approved: bool = False,
                  operator_rejected: bool = False, project_id: str | None = None) -> dict[str, Any]:
        started = _now(); report: dict[str, Any] = {"schema_version": 2, "phase": 4, "mode": mode, "project_id": project_id, "started_at": started, "preflight": {}, "status": "failed", "failures": [], "operator_action": "Inspect failures and correct the named prerequisite."}
        project = None; state: dict[str, Any] | None = None
        try:
            if mode == "real":
                # Fail before preflight/project creation when real-run signing
                # cannot be performed, while offline acceptance remains keyless.
                self._signing_secret()
            if project_id:
                # _dir validates format and containment before filesystem access.
                project = self.store.get(project_id)
                if mode != "real": raise AcceptanceError("only a pending real acceptance run may be resumed")
                state = self._load_state(project.id, mode)
            elif operator_approved or operator_rejected:
                raise AcceptanceError("operator approval/rejection requires a pending real acceptance project ID")
            report["preflight"] = await self._preflight(mode)
            if not report["preflight"].get("ready"):
                raise AcceptanceError("preflight is not ready; inspect the named checks and repair the local workstation configuration")
            if project is None:
                project = self.store.create(ProjectCreate(name=f"Phase 4 acceptance ({mode})", brief="Representative Phase 4 acceptance project.", notes="Generated by the acceptance workflow; retain reports with the workstation acceptance record."))
                report["project_id"] = project.id
                state = self._create_state(project.id, mode)
            assert state is not None
            report["acceptance_run"] = state
            if mode == "offline":
                if project_id: raise AcceptanceError("offline acceptance runs are never resumable")
                master = await self._run_offline(project.id, _plan())
                audit = await self._audit_master_async(project.id, master, _plan())
                report["master_audit"] = audit
                if not audit["passed"]: raise AcceptanceError("master audit rejected missing, stale, unprovenanced, or non-QC evidence")
                state = self._transition(state, "completed")
                report.update(status="passed_technical", acceptance_run=state, operator_action="Offline technical acceptance completed. It contains no AI creative or human-review claim.")
            elif project_id is None:
                await self._fresh_real(project.id, state, report)
            else:
                master = await self._resume_real(project.id, state, report, operator_approved=operator_approved, operator_rejected=operator_rejected)
                if master is not None:
                    # Reload the signed master-rendered transition persisted by
                    # _resume_real before certification. Never audit against the
                    # caller's stale pre-render state.
                    state = self._load_state(project.id, mode)
                    # Recheck the signed state and approval after rendering too:
                    # certification must bind the same attestation that allowed work.
                    self._verify_real_state(state, project.id)
                    self._verify_evidence_snapshot(project.id, state, "master_rendered")
                    self._assert_operator_approval(project.id, state)
                    self._assert_no_synthetic_fixture_origin(project.id)
                    audit = await self._audit_master_async(project.id, master, self.orch.production.get_plan(project.id)[0], real_certification=True)
                    report["master_audit"] = audit
                    if not audit["passed"]: raise AcceptanceError("master audit rejected missing, stale, fixture-origin, unprovenanced, or non-QC evidence")
                    self._verify_real_state(state, project.id)
                    self._verify_evidence_snapshot(project.id, state, "master_rendered")
                    self._assert_operator_approval(project.id, state)
                    state = self._transition(state, "completed", certified_master={"asset_id": master.id, "sha256": audit["master"]["sha256"]})
                    report.update(status="passed_technical_operator_attested", acceptance_run=state, operator_action="Operator attested review of the exact bound proxy. Independently review the delivery master before external release.")
        except Exception as exc:
            report["failures"].append({"type": type(exc).__name__, "message": str(exc)})
            if state is not None and state.get("state") not in {"completed", "failed"}:
                # Preserve pending states for operator remediation; only unexpected fresh failures are terminal.
                report["acceptance_run"] = state
        report["finished_at"] = _now()
        if project is not None:
            try: report["reports"] = self._write_report(project.id, report)
            except Exception as exc: report["failures"].append({"type": type(exc).__name__, "message": f"could not write acceptance report: {exc}"}); report["reports"] = {}
        else:
            report["reports"] = {}
        return report
