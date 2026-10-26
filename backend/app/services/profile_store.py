"""Durable storage for artist profiles and immutable numbered versions."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from app.config import get_settings
from app.models.profiles import ArtistProfileMeta, ArtistProfileVersion


class ProfileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _profile_dir(self, profile_id: str) -> Path:
        return self.root / profile_id

    def _meta_path(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "profile.json"

    def _versions_dir(self, profile_id: str) -> Path:
        return self._profile_dir(profile_id) / "versions"

    def _version_path(self, profile_id: str, version: int) -> Path:
        return self._versions_dir(profile_id) / f"{version:04d}.json"

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _read_meta(self, profile_id: str) -> ArtistProfileMeta:
        path = self._meta_path(profile_id)
        if not path.is_file():
            raise KeyError(f"artist profile {profile_id} not found")
        return ArtistProfileMeta.model_validate_json(path.read_text(encoding="utf-8"))

    def list_profiles(self) -> list[ArtistProfileMeta]:
        profiles: list[ArtistProfileMeta] = []
        for meta_path in self.root.glob("*/profile.json"):
            try:
                profiles.append(ArtistProfileMeta.model_validate_json(
                    meta_path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return sorted(
            profiles, key=lambda item: (item.updated_at, item.name), reverse=True)

    def create_profile(self, version: ArtistProfileVersion) -> ArtistProfileMeta:
        profile_id = version.profile_id
        directory = self._profile_dir(profile_id)
        if directory.exists():
            raise FileExistsError(f"artist profile {profile_id} already exists")
        meta = ArtistProfileMeta(
            id=profile_id,
            name=version.name,
            created_at=version.created_at,
            updated_at=version.created_at,
            latest_version=version.version,
            version_count=1,
        )
        self._write_json(self._version_path(profile_id, version.version),
                         version.model_dump(mode="json"))
        self._write_json(self._meta_path(profile_id), meta.model_dump(mode="json"))
        return meta

    def add_version(self, version: ArtistProfileVersion) -> ArtistProfileMeta:
        meta = self._read_meta(version.profile_id)
        expected = meta.latest_version + 1
        if version.version != expected:
            raise ValueError(
                f"expected profile version {expected}, got {version.version}")
        path = self._version_path(version.profile_id, version.version)
        if path.exists():
            raise FileExistsError(f"profile version {version.version} already exists")
        self._write_json(path, version.model_dump(mode="json"))
        updated = ArtistProfileMeta(
            id=meta.id,
            name=version.name,
            created_at=meta.created_at,
            updated_at=version.created_at,
            latest_version=version.version,
            version_count=version.version,
        )
        self._write_json(self._meta_path(meta.id), updated.model_dump(mode="json"))
        return updated

    def get_meta(self, profile_id: str) -> ArtistProfileMeta:
        return self._read_meta(profile_id)

    def get_version(self, profile_id: str, version: int) -> ArtistProfileVersion:
        path = self._version_path(profile_id, version)
        if not path.is_file():
            raise KeyError(f"artist profile {profile_id} version {version} not found")
        record = ArtistProfileVersion.model_validate_json(
            path.read_text(encoding="utf-8"))
        if record.profile_id != profile_id or record.version != version:
            raise KeyError(f"artist profile {profile_id} version {version} is inconsistent")
        return record

    def get_latest(self, profile_id: str) -> ArtistProfileVersion:
        meta = self._read_meta(profile_id)
        return self.get_version(profile_id, meta.latest_version)

    def list_versions(self, profile_id: str) -> list[int]:
        directory = self._versions_dir(profile_id)
        versions = []
        for path in directory.glob("*.json"):
            try:
                versions.append(int(path.stem))
            except ValueError:
                continue
        return sorted(versions)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]


_store: ProfileStore | None = None


def get_profile_store() -> ProfileStore:
    global _store
    if _store is None:
        _store = ProfileStore(get_settings().profiles_root)
    return _store


def reset_profile_store() -> None:
    global _store
    _store = None
