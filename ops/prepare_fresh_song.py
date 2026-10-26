"""Create ONE unapproved draft via existing stores. Never call a provider.

Use an inspected input bundle and exact hash. A durable receipt claim blocks
accidental repeat creation; on any failure inspect the receipt, never retry
using a different receipt to conceal an uncertain creation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


class NoProvider:
    def __getattr__(self, name):
        raise RuntimeError(f"provider/runtime access is forbidden during draft preparation: {name}")


def prepare(input_path: Path, expected_sha256: str, receipt_path: Path) -> dict:
    # Imports deferred so --help and syntax checks do not need app dependencies.
    from app.config import get_settings
    from app.models.music import MusicSettings, MusicState
    from app.models.schemas import ProjectCreate
    from app.services.music_service import MusicService
    from app.services.project_store import ProjectStore

    raw = input_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256.lower():
        raise ValueError("input byte hash does not match; no project was created")
    payload = json.loads(raw)
    if set(payload) != {"schema", "name", "lineage", "settings"}:
        raise ValueError("bundle must contain only schema, name, lineage, settings")
    if payload["schema"] != "song_writer_fresh_draft_v1":
        raise ValueError("unsupported input schema")
    settings = MusicSettings.model_validate(payload["settings"])
    cfg = get_settings()
    if cfg.music.live_generation_authorized or cfg.music.repaint_enabled:
        raise RuntimeError("run only with music generation and repaint disabled; no overrides are applied")
    if MusicState().lifetime_cap is not None or MusicState().approval is not None:
        raise RuntimeError("default music state grants authority; preparation refused")
    # A receipt outside projects is evidence, not application state or permission.
    receipt_path = receipt_path.resolve()
    if receipt_path.is_relative_to(cfg.projects_root_path.resolve()):
        raise ValueError("receipt must be outside the projects tree")
    request = ProjectCreate(
        name=payload["name"],
        brief="Provisional fresh rendition; not approved for generation.",
        notes="Provenance only; no allowance or approvals transferred. "
              + json.dumps(payload["lineage"], sort_keys=True),
    )
    claim = {"status": "creation_claimed_do_not_retry", "input_sha256": actual,
             "project_id": None, "generation_authorized": False}
    # Exclusive creation, durable BEFORE any project writes. Existing receipt
    # always blocks, even if the previous attempt failed or was interrupted.
    with receipt_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(claim, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    def persist():
        tmp = receipt_path.with_name(receipt_path.name + ".tmp")
        with tmp.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(claim, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, receipt_path)

    try:
        store = ProjectStore()
        project = store.create(request)
        claim["project_id"] = project.id
        persist()
        service = MusicService(store=store, comfy=NoProvider(), router=NoProvider())
        saved = service.save_draft(project.id, settings, revision_name="Provisional corrected inputs")
        persisted = service._load_state(project.id)
        journal = service._journal_state(project.id)
        if (persisted.lifetime_cap is not None or persisted.approval is not None
                or persisted.audio_approval is not None or persisted.preferred_take is not None
                or journal["submissions"] != 0):
            raise RuntimeError("new project has unexpected authority/accounting; stop for inspection")
        claim.update(status="draft_only_complete", lifetime_cap=None, approval=None,
                     submissions=0, project_path=str(store.root / project.id),
                     draft=saved["draft"], preflight=saved["preflight"])
        persist()
        return claim
    except Exception:
        claim["status"] = "preparation_failed_inspect_before_any_further_action"
        # Best effort only; an unmodified initial claim still forbids retry.
        try:
            persist()
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.input, args.expected_sha256, args.receipt)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
