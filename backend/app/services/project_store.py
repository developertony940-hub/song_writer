"""Project store: per-project folder layout + JSON manifest + asset registry.

Layout per project:
  projects/{id}/
    manifest.json          # ProjectMeta + asset index
    copy/                  # *.md, *.txt
    images/                # *.png, *.webp
    video/                 # *.mp4, *.webm
    audio/                 # *.wav, *.mp3
    music/                 # *.wav, *.mp3
    final/                 # assembled outputs
    prompts/               # saved LLM prompts/workflows (audit trail)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import PydanticUndefined

from app.config import get_settings
from app.models.schemas import (
    AssetKind, AssetRef, PlanCandidateKind, Product, ProjectCreate, ProjectMeta,
    StageStatus, VideoPlan, video_plan_fingerprint,
)
from app.services.production_plan import ProductionPlanService

log = logging.getLogger(__name__)

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
PROJECT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_PRODUCT_ADAPTER = TypeAdapter(Product)
# Presence-gated keys are written only when the original manifest already had
# them or the internal migration path set a value; they are never injected as a
# default. Nested-merge keys preserve unknown sub-keys because they are updated
# incrementally; authoritative keys are replaced wholesale by their owning
# operation (documented), which is why unknown sub-keys there are not carried.
_PRESENCE_GATED_KEYS = ("product", "manifest_schema_version")
_NESTED_MERGE_KEYS = ("stages", "artifacts")
_AUTHORITATIVE_REPLACE_KEYS = ("video_plan", "composition", "preview_approval")
ASSET_DIRS: dict[AssetKind, str] = {
    AssetKind.copy: "copy",
    AssetKind.image: "images",
    AssetKind.video: "video",
    AssetKind.audio: "audio",
    AssetKind.music: "music",
    AssetKind.final: "final",
    AssetKind.preview: "preview",
}
KIND_BY_DIR: dict[str, AssetKind] = {v: k for k, v in ASSET_DIRS.items()}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_name(name: str) -> str:
    return SAFE_RE.sub("-", name).strip("-")[:80] or "project"


class ProjectStore:
    def __init__(self) -> None:
        self.root = get_settings().projects_root_path.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # --- project lifecycle ------------------------------------------
    def create(self, req: ProjectCreate) -> ProjectMeta:
        pid = uuid.uuid4().hex[:12]
        pdir = self.root / pid
        pdir.mkdir(parents=True, exist_ok=True)
        for d in ASSET_DIRS.values():
            (pdir / d).mkdir(exist_ok=True)
        (pdir / "prompts").mkdir(exist_ok=True)
        (pdir / "reviews").mkdir(exist_ok=True)
        now = _utcnow()
        meta = ProjectMeta(
            id=pid,
            name=req.name,
            brief=req.brief,
            brand_voice=req.brand_voice,
            target_audience=req.target_audience,
            notes=req.notes,
            created_at=now,
            updated_at=now,
        )
        self._write_manifest(pdir, meta)
        return meta

    def list_projects(self) -> list[ProjectMeta]:
        out: list[ProjectMeta] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            mf = child / "manifest.json"
            if not mf.exists():
                continue
            try:
                out.append(self._read_manifest(child))
            except Exception:
                log.exception("Bad manifest in %s", child)
        return out

    def get(self, pid: str) -> ProjectMeta:
        pdir = self._dir(pid)
        return self._read_manifest(pdir)

    def update(self, pid: str, **fields: Any) -> ProjectMeta:
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        data = meta.model_dump()
        data.update({k: v for k, v in fields.items() if v is not None})
        data["updated_at"] = _utcnow()
        updated = ProjectMeta(**data)
        self._write_manifest(pdir, updated)
        return updated

    def stage_output_summary(self, pid: str, stage: str) -> dict[str, Any]:
        """Return the persisted output evidence needed to approve a pipeline stage.

        Approval is intentionally an explicit human action.  This check prevents a
        stale manifest or a partially failed worker from being marked approved.
        """
        meta = self.get(pid)
        assets = self.list_assets(pid)
        existing = [a for a in assets if (self._dir(pid) / a.relative_path).is_file()]
        if stage == "copy":
            if meta.video_plan:
                # A Phase-2 copy stage is never satisfied by the legacy mutable
                # Markdown ``video_plan`` slot. Require canonical manifest state,
                # its verified fingerprint, and the matching immutable JSON asset.
                try:
                    stored = dict(meta.video_plan)
                    fingerprint = stored.pop("fingerprint")
                    plan = VideoPlan(**stored)
                    artifact_id = meta.artifacts.get("production_plan")
                    artifact = next(a for a in existing if a.id == artifact_id)
                    artifact_payload = json.loads((self._dir(pid) / artifact.relative_path).read_text(encoding="utf-8"))
                    artifact_plan = VideoPlan(**artifact_payload)
                    ok = (
                        artifact.kind == AssetKind.copy
                        and artifact.meta.get("kind") == "video_plan"
                        and fingerprint == video_plan_fingerprint(plan)
                        and artifact.meta.get("fingerprint") == fingerprint
                        and video_plan_fingerprint(artifact_plan) == fingerprint
                    )
                except (KeyError, StopIteration, OSError, json.JSONDecodeError, ValueError):
                    ok = False
                return {"ok": ok, "required": "validated structured VideoPlan state and matching fingerprinted plan artifact", "count": int(ok)}
            asset_id = meta.artifacts.get("copy_plan")
            ok = bool(asset_id and any(a.id == asset_id for a in existing))
            return {"ok": ok, "required": "copy_plan artifact", "count": int(ok)}
        # When a Phase-2 plan exists, generic files cannot falsely advance the
        # stage: every scene must have selected/reviewed plan evidence.
        if meta.video_plan:
            production = ProductionPlanService(self)
            if stage == "images":
                evidence = production.candidate_evidence(pid, PlanCandidateKind.image)
                return {"ok": evidence["ok"], "required": "a selected accepted image candidate for every VideoPlan scene", "count": len(evidence["selected"]), "missing": evidence["missing"]}
            if stage == "video":
                evidence = production.candidate_evidence(pid, PlanCandidateKind.video)
                return {"ok": evidence["ok"], "required": "a selected accepted motion candidate linked to the selected image for every VideoPlan scene", "count": len(evidence["selected"]), "missing": evidence["missing"]}
            if stage == "audio":
                evidence = production.audio_evidence(pid)
                return {"ok": evidence["ok"], "required": "VideoPlan voiceover audio for every scene", "count": len(evidence["assets"]), "missing": evidence["missing"]}
            if stage == "final":
                try:
                    _plan, fingerprint = production.get_plan(pid)
                    # A stale or un-QC'd master never qualifies just because it is an MP4.
                    comp = production.composition(pid)
                    raw = json.dumps(comp.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
                    masters = [
                        a for a in existing
                        if a.kind == AssetKind.final
                        and a.meta.get("plan_fingerprint") == fingerprint
                        and a.meta.get("composition_digest") == digest
                        and isinstance(a.meta.get("qc"), dict)
                        and a.meta["qc"].get("passed") is True
                    ]
                    return {"ok": bool(masters), "required": "QC-passed master rendered from the currently approved VideoPlan proxy", "count": len(masters)}
                except (ValueError, KeyError):
                    return {"ok": False, "required": "current approved VideoPlan proxy and matching QC-passed master", "count": 0}
        expected_kind = {"images": AssetKind.image, "video": AssetKind.video,
                         "audio": AssetKind.audio, "final": AssetKind.final}.get(stage)
        if expected_kind is None:
            raise ValueError(f"Unknown stage: {stage}")
        if stage == "final":
            # A final asset is evidence only when this exact registered file
            # remains present and its persisted QC record explicitly passed.
            approved = [a for a in existing if a.kind == AssetKind.final and isinstance(a.meta.get("qc"), dict)
                        and a.meta["qc"].get("passed") is True]
            return {"ok": bool(approved), "required": "an existing final asset with qc.passed=true", "count": len(approved)}
        count = sum(a.kind == expected_kind for a in existing)
        return {"ok": count > 0, "required": f"at least one {expected_kind.value} asset", "count": count}

    def _set_stage(self, pid: str, stage: str, desired: str, *, public: bool) -> ProjectMeta:
        """Apply the small, explicit stage state machine.

        Worker-only states are never exposed through the review endpoint.  Both
        worker success and approval require persisted files, preventing stale
        manifests from becoming workflow evidence.
        """
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        if stage not in meta.stages:
            raise ValueError(f"Unknown stage: {stage}")
        try:
            desired = StageStatus(desired).value
        except ValueError as exc:
            raise ValueError(f"Invalid stage status '{desired}'") from exc
        if public and desired not in {StageStatus.pending.value, StageStatus.approved.value}:
            raise ValueError("Public stage updates may only set pending or approved")
        current = meta.stages[stage]
        predecessors = ("copy", "images", "video", "audio", "final")
        index = predecessors.index(stage)
        if desired == StageStatus.approved.value:
            if current != StageStatus.succeeded.value:
                raise ValueError(f"Cannot approve '{stage}' from '{current}'; it must be succeeded first")
            if any(meta.stages[p] != StageStatus.approved.value for p in predecessors[:index]):
                raise ValueError(f"Cannot approve '{stage}' before prior stages are approved")
            evidence = self.stage_output_summary(pid, stage)
            if not evidence["ok"]:
                raise ValueError(f"Cannot approve '{stage}': {evidence['required']} is missing")
        elif desired == StageStatus.succeeded.value:
            if current not in {StageStatus.running.value, StageStatus.pending.value, StageStatus.succeeded.value}:
                raise ValueError(f"Cannot mark '{stage}' succeeded from '{current}'")
            evidence = self.stage_output_summary(pid, stage)
            if not evidence["ok"]:
                raise ValueError(f"Cannot mark '{stage}' succeeded: {evidence['required']} is missing")
        elif desired == StageStatus.running.value:
            # A final master may be deliberately re-rendered after a succeeded
            # review. The caller must invalidate its old final evidence first.
            rerenderable = {StageStatus.pending.value, StageStatus.failed.value, StageStatus.running.value}
            if stage == "final":
                rerenderable.add(StageStatus.succeeded.value)
            if current not in rerenderable:
                raise ValueError(f"Cannot run '{stage}' from '{current}'")
        elif desired == StageStatus.failed.value:
            if current not in {StageStatus.pending.value, StageStatus.running.value, StageStatus.failed.value}:
                raise ValueError(f"Cannot fail '{stage}' from '{current}'")
        elif desired == StageStatus.pending.value:
            if current == StageStatus.approved.value:
                raise ValueError(f"Cannot reset approved stage '{stage}' through this API")
        meta.stages[stage] = desired
        meta.updated_at = _utcnow()
        self._write_manifest(pdir, meta)
        return meta

    def update_stage(self, pid: str, stage: str, status: str) -> ProjectMeta:
        """Trusted worker transition. Public routes use ``public_update_stage``."""
        return self._set_stage(pid, stage, status, public=False)

    def public_update_stage(self, pid: str, stage: str, status: str) -> ProjectMeta:
        return self._set_stage(pid, stage, status, public=True)

    def set_mode(self, pid: str, mode: str) -> ProjectMeta:
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        meta.mode = mode
        meta.updated_at = _utcnow()
        self._write_manifest(pdir, meta)
        return meta

    def set_product(self, pid: str, product: Product) -> ProjectMeta:
        """Internal migration path for a project's product identity.

        Not exposed through any public route. Refused once the project already
        carries a different product or any product-specific artifact, so a
        project that has begun producing product output cannot be re-labelled.
        Bumps ``manifest_schema_version`` and records a ``PRODUCT_MIGRATED``
        event. This is the only operation that may introduce the presence-gated
        ``product``/``manifest_schema_version`` keys.
        """
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        requested = Product(product)
        if meta.product is not None and meta.product != requested:
            raise ValueError(f"project is already bound to product '{meta.product.value}'")
        conflicts = [
            asset for asset in self._read_assets(pdir)
            if asset.meta.get("product") not in (None, Product.social_video.value)
        ]
        if conflicts:
            raise ValueError(
                "cannot change product after product-specific artifacts exist: "
                + ", ".join(f"{a.filename} ({a.meta.get('product')})" for a in conflicts[:5])
            )
        data = meta.model_dump()
        data["product"] = requested
        data["manifest_schema_version"] = 1
        updated = ProjectMeta(**data)
        self._write_manifest(pdir, updated)
        self._append_manifest_event(pdir, "PRODUCT_MIGRATED", {
            "product": requested.value, "manifest_schema_version": 1,
        })
        return updated

    @staticmethod
    def _append_manifest_event(pdir: Path, kind: str, detail: dict[str, Any]) -> None:
        events = pdir / "reviews" / "manifest_events.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        record = {"kind": kind, "at": _utcnow().isoformat(), **detail}
        with events.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def set_artifact(self, pid: str, key: str, asset_id: str | None) -> ProjectMeta:
        """Register which asset id is the editable plan artifact for a stage."""
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        allowed = {"copy_plan", "image_plan", "video_plan", "audio_plan", "production_plan"}
        if key not in allowed:
            raise ValueError(f"Unknown artifact key: {key}")
        # Old manifests do not have the new namespace until their first plan save.
        meta.artifacts.setdefault(key, None)
        meta.artifacts[key] = asset_id
        meta.updated_at = _utcnow()
        self._write_manifest(pdir, meta)
        return meta

    def save_video_plan(self, pid: str, plan: dict[str, Any], fingerprint: str) -> ProjectMeta:
        """Persist the canonical plan and invalidate dependent proxy approval.

        A changed plan can never reuse candidate selection or delivery approval
        from an earlier plan fingerprint.
        """
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        validated = VideoPlan(**plan)
        if video_plan_fingerprint(validated) != fingerprint:
            raise ValueError("canonical VideoPlan fingerprint does not match its structured state")
        meta.video_plan = {**validated.model_dump(mode="json"), "fingerprint": fingerprint}
        meta.preview_approval = None
        meta.updated_at = _utcnow()
        self._write_manifest(pdir, meta)
        return meta

    def set_preview_approval(self, pid: str, approval: dict[str, Any] | None) -> ProjectMeta:
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        meta.preview_approval = approval
        meta.updated_at = _utcnow()
        self._write_manifest(pdir, meta)
        return meta

    def _candidates_path(self, pdir: Path) -> Path:
        return pdir / "reviews" / "candidates.json"

    def list_candidates(self, pid: str) -> list[dict[str, Any]]:
        p = self._candidates_path(self._dir(pid))
        if not p.exists(): return []
        try:
            with p.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            log.warning("Ignoring unreadable candidate list for project %s", pid)
            return []
        if not isinstance(payload, list):
            log.warning("Ignoring non-list candidate data for project %s", pid)
            return []
        # Keep valid-shaped rows available; callers validate each row before it
        # can qualify as evidence, while malformed legacy rows cannot break lists.
        return [row for row in payload if isinstance(row, dict)]

    def save_candidates(self, pid: str, candidates: list[dict[str, Any]]) -> None:
        pdir = self._dir(pid); p = self._candidates_path(pdir); p.parent.mkdir(exist_ok=True)
        with p.open("w", encoding="utf-8") as fh: json.dump(candidates, fh, indent=2, ensure_ascii=False)

    def add_candidate(self, pid: str, candidate: dict[str, Any]) -> None:
        candidates = self.list_candidates(pid); candidates.append(candidate); self.save_candidates(pid, candidates)

    def update_candidate(self, pid: str, candidate_id: str, **changes: Any) -> dict[str, Any]:
        candidates = self.list_candidates(pid)
        for candidate in candidates:
            if candidate.get("id") == candidate_id:
                candidate.update(changes); self.save_candidates(pid, candidates); return candidate
        raise KeyError(f"Candidate {candidate_id} not found")

    def update_asset_meta(self, pid: str, aid: str, **changes: Any) -> AssetRef:
        pdir = self._dir(pid); assets = self._read_assets(pdir)
        for asset in assets:
            if asset.id == aid:
                asset.meta.update(changes); self._write_assets(pdir, assets); return asset
        raise KeyError(f"Asset {aid} not found in project {pid}")

    def save_composition(self, pid: str, composition: dict[str, Any]) -> ProjectMeta:
        pdir = self._dir(pid)
        meta = self._read_manifest(pdir)
        meta.composition = composition
        meta.updated_at = _utcnow()
        self._write_manifest(pdir, meta)
        return meta

    def delete(self, pid: str) -> None:
        import shutil

        pdir = self._dir(pid)
        shutil.rmtree(pdir, ignore_errors=True)

    # --- asset management -------------------------------------------
    def asset_dir(self, pid: str, kind: AssetKind) -> Path:
        pdir = self._dir(pid)
        d = pdir / ASSET_DIRS[kind]
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_asset(
        self,
        pid: str,
        kind: AssetKind,
        filename: str,
        data: bytes | str | None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> AssetRef:
        pdir = self._dir(pid)
        adir = self.asset_dir(pid, kind)
        # Asset registration must refer to a real file inside its declared kind
        # directory. Sanitising a path here would turn a traversal into a
        # phantom registry entry, so reject it instead.
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            raise ValueError("asset filename must be a non-empty basename")
        safe = SAFE_RE.sub("-", filename)[:120]
        if safe != filename:
            raise ValueError("asset filename contains unsupported characters")
        path = (adir / safe).resolve()
        if path.parent != adir.resolve():
            raise ValueError("asset path escapes its asset directory")
        if data is not None:
            if isinstance(data, str):
                path.write_text(data, encoding="utf-8")
            else:
                path.write_bytes(data)
        if not path.is_file():
            raise ValueError(f"cannot register missing asset file: {path}")
        ref = AssetRef(
            id=uuid.uuid4().hex[:10],
            project_id=pid,
            kind=kind,
            filename=safe,
            relative_path=str(path.relative_to(pdir)).replace("\\", "/"),
            created_at=_utcnow(),
            meta=meta or {},
        )
        manifest = self._read_manifest(pdir)
        assets = self._read_assets(pdir)
        assets.append(ref)
        self._write_assets(pdir, assets)
        # update counts
        manifest.asset_counts = self._counts(assets)
        manifest.updated_at = _utcnow()
        self._write_manifest(pdir, manifest)
        return ref

    def rescan_assets(self, pid: str) -> dict[str, Any]:
        """Reconcile the assets registry with what's on disk.

        Removes registry entries whose files no longer exist, and registers
        files found on disk that aren't in the registry. Returns a summary.
        """
        pdir = self._dir(pid)
        assets = self._read_assets(pdir)
        removed: list[str] = []
        added: list[AssetRef] = []
        kept: list[AssetRef] = []

        # Map registry entries to actual files
        for ref in assets:
            fpath = pdir / ref.relative_path
            if fpath.exists():
                kept.append(ref)
            else:
                removed.append(ref.filename)

        # Walk every registered asset subfolder, including plan-review proxies.
        seen_paths = {str(pdir / r.relative_path) for r in kept}
        for sub, kind in KIND_BY_DIR.items():
            subdir = pdir / sub
            if not subdir.exists():
                continue
            for f in sorted(subdir.iterdir()):
                if not f.is_file():
                    continue
                if str(f) in seen_paths:
                    continue
                seen_paths.add(str(f))
                rel = str(f.relative_to(pdir)).replace("\\", "/")
                ref = AssetRef(
                    id=uuid.uuid4().hex[:10],
                    project_id=pid,
                    kind=kind,
                    filename=f.name,
                    relative_path=rel,
                    created_at=_utcnow(),
                    meta={},
                )
                added.append(ref)
                kept.append(ref)

        self._write_assets(pdir, kept)
        manifest = self._read_manifest(pdir)
        manifest.asset_counts = self._counts(kept)
        manifest.updated_at = _utcnow()
        self._write_manifest(pdir, manifest)
        return {
            "added": [r.filename for r in added],
            "removed": removed,
            "total": len(kept),
        }

    def list_assets(self, pid: str, kind: AssetKind | None = None) -> list[AssetRef]:
        pdir = self._dir(pid)
        assets = self._read_assets(pdir)
        if kind is None:
            return assets
        return [a for a in assets if a.kind == kind]

    def get_asset(self, pid: str, aid: str) -> AssetRef:
        for a in self.list_assets(pid):
            if a.id == aid:
                return a
        raise KeyError(f"Asset {aid} not found in project {pid}")

    def asset_path(self, pid: str, aid: str) -> Path:
        ref = self.get_asset(pid, aid)
        pdir = self._dir(pid).resolve()
        path = (pdir / ref.relative_path).resolve()
        if path.parent not in {(pdir / name).resolve() for name in ASSET_DIRS.values()}:
            raise ValueError("registered asset path escapes its project asset directory")
        return path

    def unregister_final_output(self, pid: str, filename: str | None = None) -> int:
        """Remove final registry evidence without deleting its delivery bytes.

        ``filename=None`` invalidates *all* prior final evidence before a new
        render begins.  A failed render therefore cannot leave an older master
        approvable merely because it had a different output name.
        """
        pdir = self._dir(pid)
        assets = self._read_assets(pdir)
        kept = [a for a in assets if not (a.kind == AssetKind.final and (filename is None or a.filename == filename))]
        removed = len(assets) - len(kept)
        if removed:
            self._write_assets(pdir, kept)
            manifest = self._read_manifest(pdir)
            manifest.asset_counts = self._counts(kept)
            manifest.updated_at = _utcnow()
            self._write_manifest(pdir, manifest)
        return removed

    def delete_asset(self, pid: str, aid: str) -> None:
        """Remove an asset's file and registry entry."""
        pdir = self._dir(pid)
        ref = self.get_asset(pid, aid)
        fpath = pdir / ref.relative_path
        if fpath.exists():
            fpath.unlink()
        assets = self._read_assets(pdir)
        assets = [a for a in assets if a.id != aid]
        self._write_assets(pdir, assets)
        manifest = self._read_manifest(pdir)
        manifest.asset_counts = self._counts(assets)
        manifest.updated_at = _utcnow()
        self._write_manifest(pdir, manifest)

    # --- internals --------------------------------------------------
    def _dir(self, pid: str) -> Path:
        # Project IDs are generated fixed-width lowercase UUID prefixes.  Reject
        # traversal, separators, and arbitrary folders before touching disk.
        if not isinstance(pid, str) or not PROJECT_ID_RE.fullmatch(pid):
            raise KeyError("invalid project ID")
        pdir = (self.root / pid).resolve()
        if pdir.parent != self.root or not pdir.is_dir():
            raise KeyError(f"Project {pid} not found")
        return pdir

    @staticmethod
    def _manifest_path(pdir: Path) -> Path:
        return pdir / "manifest.json"

    @staticmethod
    def _assets_path(pdir: Path) -> Path:
        return pdir / "assets.json"

    def _read_manifest(self, pdir: Path) -> ProjectMeta:
        with self._manifest_path(pdir).open("r", encoding="utf-8") as fh:
            return ProjectMeta(**json.load(fh))

    @staticmethod
    def _field_default(key: str) -> Any:
        """Return the pristine default for a model field, or ``PydanticUndefined``.

        Used only to decide whether a *previously absent* field is an actual
        mutation. ``model_fields_set`` is deliberately not used here: it tracks
        loaded and assigned fields, not changes made in this cycle.
        """
        field = ProjectMeta.model_fields.get(key)
        if field is None:
            return PydanticUndefined
        return field.get_default(call_default_factory=True)

    def _differs_from_default(self, key: str, value: Any) -> bool:
        default = self._field_default(key)
        if default is PydanticUndefined:
            # Required fields have no default and are always written; they are
            # always present in a valid source manifest anyway.
            return True
        return value != default

    @staticmethod
    def _merge_manifest_value(key: str, original_value: Any, new_value: Any) -> Any:
        if key in _NESTED_MERGE_KEYS and isinstance(original_value, dict) and isinstance(new_value, dict):
            merged = dict(original_value)
            merged.update(new_value)
            return merged
        # Authoritative fields are replaced wholesale by their owning operation;
        # unknown sub-keys are intentionally not carried forward.
        return new_value

    @staticmethod
    def _serialize_presence_gated(key: str, value: Any) -> Any:
        if key == "product":
            member = value if isinstance(value, Product) else Product(value)
            return _PRODUCT_ADAPTER.dump_python(member, mode="json")
        return value

    def _compose_manifest(self, pdir: Path, meta: ProjectMeta) -> dict[str, Any]:
        """Build the on-disk manifest without mutating legacy shape.

        Rules:
          * Unknown root keys are preserved from the existing file.
          * Known keys already present are updated in place (with nested merge
            for incrementally-updated maps).
          * Known keys absent from the original are written only when they differ
            from their pristine default (actual mutation).
          * ``product``/``manifest_schema_version`` are written only when already
            present or explicitly set by the internal migration path; a stored
            value is never downgraded to null.
        Values are produced by Pydantic's JSON serializer so no raw Enum/model
        object reaches ``json.dump``.
        """
        original: dict[str, Any] = {}
        path = self._manifest_path(pdir)
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    original = loaded
            except (OSError, json.JSONDecodeError):
                original = {}
        current = meta.model_dump(mode="json")  # product/version are excluded here
        out: dict[str, Any] = {}
        for key, value in original.items():
            if key in _PRESENCE_GATED_KEYS:
                continue  # handled below, presence-gated
            if key in current:
                out[key] = self._merge_manifest_value(key, value, current[key])
            else:
                out[key] = value  # unknown legacy root key: preserve verbatim
        for key, value in current.items():
            if key in out:
                continue
            if self._differs_from_default(key, value):
                out[key] = value
        for key in _PRESENCE_GATED_KEYS:
            new_value = getattr(meta, key)
            if new_value is None:
                if key in original and original[key] is not None:
                    out[key] = original[key]  # never downgrade a stored value to null
                continue
            out[key] = self._serialize_presence_gated(key, new_value)
        return out

    def _write_manifest(self, pdir: Path, meta: ProjectMeta) -> None:
        payload = self._compose_manifest(pdir, meta)
        with self._manifest_path(pdir).open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)

    def _read_assets(self, pdir: Path) -> list[AssetRef]:
        ap = self._assets_path(pdir)
        if not ap.exists():
            return []
        with ap.open("r", encoding="utf-8") as fh:
            return [AssetRef(**a) for a in json.load(fh)]

    def _write_assets(self, pdir: Path, assets: list[AssetRef]) -> None:
        with self._assets_path(pdir).open("w", encoding="utf-8") as fh:
            json.dump([a.model_dump(mode="json") for a in assets], fh, indent=2, ensure_ascii=False)

    @staticmethod
    def _counts(assets: list[AssetRef]) -> dict[str, int]:
        c: dict[str, int] = {}
        for a in assets:
            c[a.kind.value] = c.get(a.kind.value, 0) + 1
        return c


_store: ProjectStore | None = None


def get_store() -> ProjectStore:
    global _store
    if _store is None:
        _store = ProjectStore()
    return _store