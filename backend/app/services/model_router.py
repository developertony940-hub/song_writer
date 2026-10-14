"""Cooperative, lease-based GPU model scheduler.

A lease is held from ``acquire`` until matching ``release``.  This serializes
this application's jobs per GPU; it cannot reserve a GPU against external
processes.  In particular LM Studio's OpenAI-compatible server has no
verified unload endpoint, so GPU0 residency is explicitly advisory.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Awaitable, Callable


class Slot(IntEnum):
    GPU0 = 0
    GPU1 = 1


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
    "kokoro-82m": ModelProfile(2000, Slot.GPU0, "tts"),
}


class ModelRouter:
    def __init__(self) -> None:
        self._slots: dict[Slot, dict[str, Any]] = {
            slot: {"current_model": None, "locked_by": None, "last_unload": None,
                   "residency_guarantee": "advisory" if slot is Slot.GPU0 else "cooperative"}
            for slot in Slot
        }
        self._leases = {slot: asyncio.Lock() for slot in Slot}
        self._state_lock = asyncio.Lock()
        self._unloaders: dict[Slot, Callable[[], Awaitable[Any]]] = {}

    def register_unloader(self, slot: int, callback: Callable[[], Awaitable[Any]]) -> None:
        self._unloaders[Slot(slot)] = callback

    @staticmethod
    def _profile(model: str) -> ModelProfile:
        return PROFILES.get(model, ModelProfile(0, Slot.GPU0, "unknown"))

    async def acquire(self, model: str, job_id: str) -> ModelProfile:
        profile = self._profile(model)
        lease = self._leases[profile.slot]
        await lease.acquire()  # intentionally held for the whole model invocation
        try:
            async with self._state_lock:
                state = self._slots[profile.slot]
                if state["locked_by"] is not None:
                    raise RuntimeError(f"GPU {int(profile.slot)} lease state is inconsistent")
                prior = state["current_model"]
            if prior not in (None, model) and profile.slot in self._unloaders:
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
        return {"semantics": "leases serialize LocalAIContent jobs only; external GPU use is not controlled",
                "slots": {str(int(slot)): dict(state) for slot, state in self._slots.items()}}


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def reset_router() -> None:
    global _router
    _router = None
