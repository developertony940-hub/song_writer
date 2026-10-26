"""Artist profile capture, versioning, preview, and confirmed loading."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.models.music import MusicSettings
from app.models.profiles import (
    PROFILE_LABEL,
    ArtistProfileCreateRequest,
    ArtistProfileLoadRequest,
    ArtistProfileMeta,
    ArtistProfilePreviewRequest,
    ArtistProfileSource,
    ArtistProfileUpdateRequest,
    ArtistProfileVersion,
)
from app.services.music_service import get_music_service
from app.services.profile_store import ProfileStore, get_profile_store

COPY_FIELDS = [
    "musical_direction", "duration_s", "language", "bpm", "time_signature",
    "key_scale", "generate_audio_codes", "seed", "arrangement",
    "temperature", "top_p", "cfg_scale",
]
EXCLUSIONS = {
    "bpm": "Tempo (BPM)",
    "key_scale": "Key",
    "time_signature": "Time signature",
    "seed": "Seed",
    "duration_s": "Duration",
    "arrangement": "Arrangement",
}
DEFAULT_FIELDS = {
    "musical_direction", "language", "generate_audio_codes",
    "temperature", "top_p", "cfg_scale",
}
PRESERVED = [
    "lyrics from the current project",
    "audio approvals",
    "submission allowances",
    "capacity permissions",
    "live authorization",
]


class ProfileError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProfileService:
    def __init__(self, store: ProfileStore | None = None, music=None) -> None:
        self.store = store or get_profile_store()
        self.music = music or get_music_service()

    # --- capture -----------------------------------------------------
    def _take_source(self, project_id: str, asset_id: str) -> tuple[
        ArtistProfileSource, MusicSettings | None, list[str],
    ]:
        state = self.music._load_state(project_id)
        journal = self.music._journal_state(project_id)
        started = self.music._started_rows(journal)
        completed = next(
            (row for row in journal["rows"]
             if row.get("kind") == "attempt_completed"
             and row.get("asset_id") == asset_id),
            None,
        )
        if completed is None:
            raise ProfileError("a completed source take is required")
        st = started.get(completed.get("attempt_id")) or {}
        gaps: list[str] = []
        try:
            asset_meta = self.music.store.get_asset(
                project_id, asset_id).meta.get("music", {}) or {}
        except KeyError:
            asset_meta = {}
        effective = st.get("effective_settings")
        settings_source = "journal" if isinstance(effective, dict) else None
        if not isinstance(effective, dict):
            effective = asset_meta.get("effective_settings")
            if isinstance(effective, dict):
                settings_source = "asset_meta"
        recorded: MusicSettings | None = None
        if isinstance(effective, dict):
            try:
                recorded = MusicSettings.model_validate(effective)
            except ValidationError:
                gaps.append(
                    "recorded effective settings failed schema validation; "
                    "not reconstructed")
        else:
            gaps.append(
                "no effective settings were recorded for this take "
                "(legacy or missing journal snapshot); not reconstructed")
        revision = st.get("revision")
        if revision is None:
            gaps.append("take has no revision linkage")
        engine = st.get("engine")
        checkpoint = st.get("checkpoint")
        workflow = st.get("workflow")
        missing_identity = [
            name for name, value in (
                ("engine", engine), ("checkpoint", checkpoint),
                ("workflow", workflow),
            ) if not value
        ]
        if missing_identity:
            gaps.append(
                "model/workflow identity incomplete: " + ", ".join(missing_identity))
        audio_sha = completed.get("sha256") or ""
        if not audio_sha:
            gaps.append("take has no recorded audio SHA-256")
        checkpoint_sha256 = asset_meta.get("checkpoint_sha256")
        if not checkpoint_sha256:
            gaps.append("take has no recorded checkpoint SHA-256")
        try:
            path = self.music.store.asset_path(project_id, asset_id)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if audio_sha and actual != audio_sha:
                gaps.append("asset bytes do not match the recorded audio SHA-256")
        except KeyError:
            gaps.append("source audio asset is not present")
        source = ArtistProfileSource(
            project_id=project_id,
            asset_id=asset_id,
            attempt_id=str(completed.get("attempt_id") or ""),
            revision=int(revision) if revision is not None else None,
            audio_sha256=audio_sha,
            take_created_at=completed.get("at"),
            engine=engine,
            checkpoint=checkpoint,
            checkpoint_sha256=checkpoint_sha256,
            workflow=workflow,
            settings_source=settings_source,
        )
        return source, recorded, gaps

    def create_from_take(self, req: ArtistProfileCreateRequest) -> dict[str, Any]:
        name = req.name.strip()
        if not name:
            raise ProfileError("profile name is required")
        source, recorded, gaps = self._take_source(req.project_id, req.asset_id)
        profile_id = ProfileStore.new_id()
        version = ArtistProfileVersion(
            profile_id=profile_id,
            version=1,
            name=name,
            created_at=_utcnow(),
            source=source,
            recorded_settings=recorded,
            provenance_gaps=gaps,
            artist_description=req.artist_description,
            vocal_description=req.vocal_description,
            production_defaults=req.production_defaults,
            label=PROFILE_LABEL,
        )
        meta = self.store.create_profile(version)
        return {
            "profile": meta.model_dump(mode="json"),
            "version": version.model_dump(mode="json"),
        }

    # --- listing / versions -----------------------------------------
    def list_profiles(self) -> dict[str, Any]:
        return {
            "profiles": [
                item.model_dump(mode="json") for item in self.store.list_profiles()
            ],
            "label": PROFILE_LABEL,
            "reference_conditioning": "disabled",
        }

    def _compatibility(self, source: ArtistProfileSource) -> dict[str, Any]:
        current = get_settings().music
        profile = {
            "engine": source.engine,
            "checkpoint": source.checkpoint,
            "workflow": source.workflow,
        }
        active = {
            "engine": current.engine,
            "checkpoint": current.checkpoint,
            "workflow": current.workflow,
        }
        if not source.engine or not source.workflow or not source.checkpoint:
            return {
                "status": "unverified",
                "issues": ["profile source lacks a complete engine/workflow/checkpoint identity"],
                "profile": profile,
                "current": active,
            }
        if not source.checkpoint_sha256:
            return {
                "status": "unverified",
                "issues": ["profile source lacks a recorded checkpoint SHA-256"],
                "profile": profile,
                "current": active,
            }
        issues = []
        if source.engine != current.engine:
            issues.append(
                f"engine {source.engine!r} != current {current.engine!r}")
        if source.workflow != current.workflow:
            issues.append(
                f"workflow {source.workflow!r} != current {current.workflow!r}")
        if source.checkpoint != current.checkpoint:
            issues.append(
                f"checkpoint {source.checkpoint!r} != current {current.checkpoint!r}")
        if source.checkpoint_sha256 != current.checkpoint_sha256:
            issues.append("checkpoint SHA-256 differs from the active checkpoint")
        return {
            "status": "unsupported" if issues else "supported",
            "issues": issues,
            "profile": profile,
            "current": active,
        }

    def get_profile(self, profile_id: str, version: int | None = None) -> dict[str, Any]:
        meta = self.store.get_meta(profile_id)
        selected = (
            self.store.get_version(profile_id, version)
            if version is not None else self.store.get_latest(profile_id)
        )
        return {
            "profile": meta.model_dump(mode="json"),
            "version": selected.model_dump(mode="json"),
            "versions": self.store.list_versions(profile_id),
            "compatibility": self._compatibility(selected.source),
        }

    def update_profile(
        self, profile_id: str, req: ArtistProfileUpdateRequest,
    ) -> dict[str, Any]:
        meta = self.store.get_meta(profile_id)
        latest = self.store.get_latest(profile_id)
        name = (req.name if req.name is not None else latest.name).strip()
        if not name:
            raise ProfileError("profile name is required")
        version = ArtistProfileVersion(
            profile_id=profile_id,
            version=latest.version + 1,
            name=name,
            created_at=_utcnow(),
            source=latest.source,
            recorded_settings=latest.recorded_settings,
            provenance_gaps=list(latest.provenance_gaps),
            artist_description=req.artist_description,
            vocal_description=req.vocal_description,
            production_defaults=req.production_defaults,
            label=PROFILE_LABEL,
        )
        updated = self.store.add_version(version)
        return {
            "profile": updated.model_dump(mode="json"),
            "version": version.model_dump(mode="json"),
        }

    # --- preview / load ---------------------------------------------
    def _validate_exclusions(self, exclude: list[str]) -> list[str]:
        unknown = [item for item in exclude if item not in EXCLUSIONS]
        if unknown:
            raise ProfileError(
                "unsupported exclusion fields: " + ", ".join(sorted(set(unknown))))
        return sorted(set(exclude))

    def _build_preview(
        self,
        project_id: str,
        meta: ArtistProfileMeta,
        version: ArtistProfileVersion,
        current: MusicSettings,
        exclude: list[str],
        apply_defaults: bool,
    ) -> dict[str, Any]:
        if version.recorded_settings is None:
            raise ProfileError(
                "profile has no recorded take settings (legacy or unknown); "
                "nothing can be loaded")
        excluded = self._validate_exclusions(exclude)
        proposed = current.model_copy(deep=True)
        for field in COPY_FIELDS:
            if field in excluded:
                continue
            setattr(proposed, field, getattr(version.recorded_settings, field))
        proposed.lyrics = current.lyrics
        unsupported_fields: list[str] = []
        if apply_defaults:
            for key, value in version.production_defaults.items():
                if key not in DEFAULT_FIELDS:
                    unsupported_fields.append(key)
                    continue
                setattr(proposed, key, value)
        try:
            validated = MusicSettings.model_validate(proposed.model_dump(mode="json"))
        except ValidationError as exc:
            detail = exc.errors()[0].get("msg", "invalid value")
            raise ProfileError(f"profile settings are invalid: {detail}") from exc
        current_data = current.model_dump(mode="json")
        proposed_data = validated.model_dump(mode="json")
        changes = []
        for field in COPY_FIELDS:
            if current_data.get(field) == proposed_data.get(field):
                continue
            changes.append({
                "field": field,
                "from": current_data.get(field),
                "to": proposed_data.get(field),
            })
        token_payload = {
            "project_id": project_id,
            "profile_id": meta.id,
            "version": version.version,
            "exclude": excluded,
            "apply_defaults": apply_defaults,
            "current": current_data,
            "proposed": proposed_data,
        }
        token = hashlib.sha256(
            json.dumps(token_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return {
            "profile": meta.model_dump(mode="json"),
            "version": version.model_dump(mode="json"),
            "compatibility": self._compatibility(version.source),
            "unsupported_fields": unsupported_fields,
            "changes": changes,
            "excluded": [
                {"field": field, "label": EXCLUSIONS[field]} for field in excluded
            ],
            "preserved": PRESERVED,
            "preview_token": token,
            "proposed_settings": proposed_data,
        }

    def preview(
        self, profile_id: str, version: int, req: ArtistProfilePreviewRequest,
    ) -> dict[str, Any]:
        meta = self.store.get_meta(profile_id)
        record = self.store.get_version(profile_id, version)
        state = self.music._load_state(req.project_id)
        if state.draft is None:
            raise ProfileError("project has no saved music draft to load into")
        return self._build_preview(
            req.project_id, meta, record, state.draft.settings,
            req.exclude, req.apply_defaults,
        )

    def load(
        self, profile_id: str, version: int, req: ArtistProfileLoadRequest,
    ) -> dict[str, Any]:
        if not req.confirm:
            raise ProfileError("explicit confirmation is required to load a profile")
        preview = self.preview(profile_id, version, req)
        if preview["unsupported_fields"]:
            raise ProfileError(
                "unsupported profile fields: "
                + ", ".join(preview["unsupported_fields"]))
        compatibility = preview["compatibility"]
        if compatibility["status"] == "unsupported":
            raise ProfileError(
                "provider compatibility failed: " + "; ".join(compatibility["issues"]))
        if compatibility["status"] == "unverified" and not req.acknowledge_unverified:
            raise ProfileError(
                "provider compatibility is unverified; acknowledge it to proceed")
        if preview["preview_token"] != req.preview_token:
            raise ProfileError("preview is stale; preview the load again")
        proposed = MusicSettings.model_validate(preview["proposed_settings"])
        draft_result = self.music.save_draft(
            req.project_id,
            proposed,
            revision_name=f"profile {preview['version']['name']} v{version}",
        )
        return {"preview": preview, "draft": draft_result}


_service: ProfileService | None = None


def get_profile_service() -> ProfileService:
    global _service
    if _service is None:
        _service = ProfileService()
    return _service


def reset_profile_service() -> None:
    global _service
    _service = None
