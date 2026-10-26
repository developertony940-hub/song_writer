"""Cooperative, lease-based GPU model scheduler.

A lease is held from ``acquire`` until matching ``release``.  This serializes
this application's jobs per GPU; it cannot reserve a GPU against external
processes.  In particular LM Studio's OpenAI-compatible server has no
verified unload endpoint, so GPU0 residency is explicitly advisory.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Awaitable, Callable


class Slot(IntEnum):
    GPU0 = 0
    GPU1 = 1


class CapacityHeld(RuntimeError):
    """A GPU slot is held pending documented recovery of uncertain capacity.

    Raised by :meth:`ModelRouter.acquire` so that *any* competing acquisition
    (another project, another product) is refused while an unconfirmed job may
    still occupy the slot.
    """

    def __init__(self, slot: "Slot", hold: dict[str, Any]) -> None:
        self.slot = slot
        self.hold = hold
        detail = hold.get("reason") or "uncertain occupied capacity"
        holder = hold.get("holder")
        suffix = f" (held by {holder})" if holder else ""
        super().__init__(f"GPU {int(slot)} capacity is held pending documented recovery: {detail}{suffix}")


class ModelSwitchRequired(RuntimeError):
    """A requested model would require an unapproved ComfyUI model switch."""

    def __init__(self, slot: "Slot", prior_model: str, requested_model: str) -> None:
        self.slot = slot
        self.prior_model = prior_model
        self.requested_model = requested_model
        super().__init__(
            f"GPU {int(slot)} currently holds {prior_model}; requesting {requested_model} "
            "would require an unload/model switch")


class HoldPersistenceError(RuntimeError):
    """A capacity hold could not be persisted to (or cleared from) disk.

    The in-memory hold remains active so this process stays blocked; the error
    signals that other processes/restarts may not observe the hold.
    """


@dataclass(frozen=True)
class ModelProfile:
    size_mb: int
    slot: Slot
    category: str


PROFILES = {
    "qwen3-coder-30b-a3b-instruct": ModelProfile(19000, Slot.GPU0, "llm"),
    "gemma-3-27b-it": ModelProfile(18000, Slot.GPU0, "llm"),
    "flux_dev_fp8": ModelProfile(18000, Slot.GPU1, "image"),
    "wan21_i2v_14b_fp8": ModelProfile(21000, Slot.GPU1, "video"),
    "wan21_t2v_14b_fp8": ModelProfile(21000, Slot.GPU1, "video"),
    "ace-step-1.5": ModelProfile(16000, Slot.GPU1, "music"),
    "kokoro-82m": ModelProfile(2000, Slot.GPU0, "tts"),
}


class ModelRouter:
    def __init__(self, holds_path: Path | None = None) -> None:
        self._slots: dict[Slot, dict[str, Any]] = {
            slot: {"current_model": None, "locked_by": None, "last_unload": None,
                   "residency_guarantee": "advisory" if slot is Slot.GPU0 else "cooperative"}
            for slot in Slot
        }
        self._leases = {slot: asyncio.Lock() for slot in Slot}
        self._state_lock = asyncio.Lock()
        self._unloaders: dict[Slot, Callable[[], Awaitable[Any]]] = {}
        # Process-wide, disk-persisted capacity holds. Recorded when a job's
        # cancellation cannot be confirmed stopped, so the uncertain capacity
        # blocks competing acquisition across projects and survives restart.
        self._holds_path = holds_path if holds_path is not None else _default_holds_path()
        self._holds_unreadable = False
        self._holds: dict[str, dict[str, Any]] = self._load_holds()

    def register_unloader(self, slot: int, callback: Callable[[], Awaitable[Any]]) -> None:
        self._unloaders[Slot(slot)] = callback

    # --- persistent capacity holds ----------------------------------
    @staticmethod
    def _slot_key(slot: Slot) -> str:
        return str(int(slot))

    def _load_holds(self) -> dict[str, dict[str, Any]]:
        path = self._holds_path
        if path is None or not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Fail closed: an unreadable hold file may hide a real hold, so
            # acquisitions are refused until the file is repaired.
            self._holds_unreadable = True
            return {}
        if not isinstance(payload, dict):
            self._holds_unreadable = True
            return {}
        holds: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if isinstance(value, dict) and str(key) in {self._slot_key(Slot.GPU0), self._slot_key(Slot.GPU1)}:
                holds[str(key)] = value
        return holds

    def _save_holds(self) -> None:
        if self._holds_path is None:
            return
        try:
            self._holds_path.parent.mkdir(parents=True, exist_ok=True)
            self._holds_path.write_text(json.dumps(self._holds, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise HoldPersistenceError(
                f"could not persist capacity holds to {self._holds_path}: {exc}") from exc

    def holds_unreadable(self) -> bool:
        """True when the persisted hold file existed but could not be read."""
        return self._holds_unreadable

    def held(self, model: str) -> dict[str, Any] | None:
        """Return the active hold record for ``model``'s slot, if any."""
        return self._holds.get(self._slot_key(self._profile(model).slot))

    def holds(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._holds.items()}

    def hold(self, model: str, job_id: str, *, reason: str, holder: str | None = None) -> dict[str, Any]:
        """Record a persistent hold on ``model``'s slot (idempotent per slot)."""
        slot = self._profile(model).slot
        record = {
            "slot": int(slot),
            "model": model,
            "job_id": job_id,
            "holder": holder,
            "reason": reason,
            "since": datetime.now(timezone.utc).isoformat(),
        }
        self._holds[self._slot_key(slot)] = record
        self._save_holds()  # raises HoldPersistenceError on failure; in-memory hold remains
        self._holds_unreadable = False
        return record

    def clear_hold(self, model: str, *, holder: str | None = None) -> bool:
        """Remove a hold. When ``holder`` is given, only that owner may clear it."""
        slot = self._slot_key(self._profile(model).slot)
        existing = self._holds.get(slot)
        if existing is None:
            return False
        if holder is not None and existing.get("holder") not in (None, holder):
            raise RuntimeError(f"GPU capacity hold is owned by {existing.get('holder')}, not {holder}")
        self._holds.pop(slot, None)
        try:
            self._save_holds()
        except HoldPersistenceError:
            # Keep this process blocked; the on-disk hold still governs restart.
            self._holds[slot] = existing
            raise
        self._holds_unreadable = False
        return True

    @staticmethod
    def _profile(model: str) -> ModelProfile:
        return PROFILES.get(model, ModelProfile(0, Slot.GPU0, "unknown"))

    def _raise_if_held(self, profile: ModelProfile) -> None:
        if self._holds_unreadable:
            # Fail closed: a hold may exist but cannot be verified.
            raise CapacityHeld(profile.slot, {
                "reason": "capacity-hold file is unreadable; refusing acquisition (fail closed)",
            })
        hold = self._holds.get(self._slot_key(profile.slot))
        if hold is not None:
            raise CapacityHeld(profile.slot, hold)

    async def acquire(self, model: str, job_id: str, *, allow_unload: bool = True) -> ModelProfile:
        profile = self._profile(model)
        lease = self._leases[profile.slot]
        await lease.acquire()  # intentionally held for the whole model invocation
        try:
            self._raise_if_held(profile)
            async with self._state_lock:
                state = self._slots[profile.slot]
                if state["locked_by"] is not None:
                    raise RuntimeError(f"GPU {int(profile.slot)} lease state is inconsistent")
                prior = state["current_model"]
            if prior not in (None, model) and profile.slot in self._unloaders:
                if not allow_unload:
                    raise ModelSwitchRequired(profile.slot, str(prior), model)
                outcome = await self._unloaders[profile.slot]()
                verified = bool(outcome is True or (isinstance(outcome, dict) and outcome.get("verified")))
                async with self._state_lock:
                    self._slots[profile.slot]["last_unload"] = {
                        "prior_model": prior, "verified": verified,
                        "detail": outcome if isinstance(outcome, dict) else None,
                    }
            async with self._state_lock:
                self._slots[profile.slot].update(current_model=model, locked_by=job_id)
            return profile
        except BaseException:
            lease.release()
            raise

    async def release(self, model: str, job_id: str, unload: bool = False) -> None:
        profile = self._profile(model)
        lease = self._leases[profile.slot]
        if not lease.locked():
            raise RuntimeError(f"GPU {int(profile.slot)} has no active lease")
        async with self._state_lock:
            state = self._slots[profile.slot]
            if state["locked_by"] != job_id:
                raise RuntimeError(f"GPU {int(profile.slot)} is leased by another job")
        try:
            if unload and profile.slot in self._unloaders:
                outcome = await self._unloaders[profile.slot]()
                verified = bool(outcome is True or (isinstance(outcome, dict) and outcome.get("verified")))
                async with self._state_lock:
                    state = self._slots[profile.slot]
                    state["last_unload"] = {"prior_model": model, "verified": verified,
                                            "detail": outcome if isinstance(outcome, dict) else None}
                    # Do not falsely claim non-residency when runtime cannot verify it.
                    if verified:
                        state["current_model"] = None
            elif unload:
                async with self._state_lock:
                    self._slots[profile.slot]["last_unload"] = {"prior_model": model, "verified": False,
                                                                  "detail": {"reason": "no unloader registered"}}
        finally:
            async with self._state_lock:
                self._slots[profile.slot]["locked_by"] = None
            lease.release()

    def status(self) -> dict[str, Any]:
        return {"semantics": "leases serialize song_writer jobs only; external GPU use is not controlled",
                "slots": {str(int(slot)): dict(state) for slot, state in self._slots.items()},
                "holds": self.holds()}


def _default_holds_path() -> Path | None:
    """Resolve the process-wide capacity-hold file beside the project root."""
    try:
        from app.config import get_settings
        return get_settings().projects_root_path / ".router_holds.json"
    except Exception:
        return None


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def reset_router() -> None:
    global _router
    _router = None
