"""The agent pipeline: deterministic orchestration, bounded, single-writer.

recommendations_validation.html V2 showed that always decomposing would be
three times slower than the single call it replaces, so this pipeline picks
its mode from the brief:

* **fast** - the brief already carries the operator's lyrics and a chord map.
  One assistant call produces the arrangement, and the deterministic critics
  check it. This is the common case for this operator and it keeps the
  current latency.
* **decomposed** - something is genuinely missing. Narrow producers fill one
  slot each, and only the missing ones run.

Two invariants hold in both modes:

* The only write to project state is ``MusicService.save_draft``. Approval,
  the submission cap and the audio approval are unreachable from here.
* The operator's brief is untrusted data. It is sanitised, sent in its own
  labelled message, and never allowed to influence budgets, paths, model
  choice or authority fields.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from app.agents.contracts import (
    Critique, Defect, DirectionSpec, MixPlan, Patch, RunResult,
)
from app.agents.runtime import (
    AgentBudgetExhausted, AgentRuntime, AgentStepFailed, RunBudget, StepGuard,
    hash_text, known_usage,
)
from app.models.music import KEY_SCALES, MusicSettings
from app.services.brief_safety import ALLOWED_BRIEF_FIELDS, brief_is_complete, sanitize_brief
from app.services.music_input_review import review_music_inputs
from app.services.operator_lyrics import (
    extract_operator_lyrics, merge_operator_lyrics,
)

log = logging.getLogger(__name__)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FENCE = re.compile(r"```[a-zA-Z]*")


class DraftSaveInvalid(RuntimeError):
    """Raised when ``save_draft`` returned something the contract forbids."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classify_defects(issues: list[dict[str, Any]]) -> list[Defect]:
    """Turn preflight advisories into typed defects."""
    defects: list[Defect] = []
    for issue in issues:
        code = str(issue.get("code") or "unknown")
        severity = "error" if code in {
            "empty_section", "no_sung_lines", "instrumental_lyric_conflict",
        } else "warn"
        defects.append(Defect(
            code=code, section=str(issue.get("section") or ""),
            message=str(issue.get("message") or ""), severity=severity))
    return defects


class AgentPipeline:
    """Runs the bounded graph and writes at most one draft."""

    def __init__(self, *, service=None, lms=None, router=None, store=None,
                 budget: RunBudget | None = None) -> None:
        from app.services.lmstudio_client import get_lmstudio
        from app.services.model_router import get_router
        from app.services.music_service import get_music_service
        from app.services.project_store import get_store

        self.service = service or get_music_service()
        self.lms = lms or get_lmstudio()
        self.router = router or get_router()
        self.store = store or get_store()
        self.budget = budget or RunBudget()

    # -- assistant calls ------------------------------------------------
    async def _ask(self, runtime: AgentRuntime, *, system: str, instructions: str,
                   data: str, guard: StepGuard, model: str,
                   extra_body: dict[str, Any] | None = None) -> Any:
        """One bounded assistant call with the brief as labelled data.

        The completion object is returned so the runtime can charge its
        measured usage against the run budget.
        """
        job_id = uuid.uuid4().hex[:10]
        await self.router.acquire(model, job_id)
        try:
            return await self.lms.chat_result(
                instructions, model=model, system_prompt=system,
                temperature=0.4, max_tokens=guard.tokens,
                untrusted_blocks=[data] if data else None,
                extra_body=extra_body)
        finally:
            await self.router.release(model, job_id, unload=False)

    async def _ask_resilient(self, runtime: AgentRuntime, *, system: str,
                             instructions: str, data: str, guard: StepGuard,
                             model: str, filled: list[str]) -> Any:
        """Ask a step, and recover once from a truncated answer.

        A reasoning model can spend the whole token allowance on hidden
        thinking and answer with nothing, or with half an object. Both stop
        because the cap was hit, and ``finish_reason`` says so. The step is
        retried once with server-side thinking turned off and enough room to
        finish the answer.
        """
        completion = await runtime.run_step(
            guard.name, guard,
            lambda: self._ask(
                runtime, system=system, instructions=instructions,
                data=data, guard=guard, model=model))
        finish_reason = str(getattr(completion, "finish_reason", "") or "")
        if finish_reason != "length" or known_usage(getattr(completion, "usage", None)) is None:
            # An unknown or invalid provider outcome is terminal: its
            # reservation was charged to the unknown bucket and it must not be
            # retried. This uses the same predicate as settlement.
            return completion
        room = max(guard.tokens * 4, 8192)
        filled.append(
            f"the {guard.name} step hit the {guard.tokens} token cap before it "
            f"could finish its answer; it was retried with thinking off and "
            f"{room} tokens")
        retry_guard = StepGuard(name=f"{guard.name}_retry", wall_s=guard.wall_s,
                                tokens=room, model_calls=guard.model_calls)
        return await runtime.run_step(
            retry_guard.name, retry_guard,
            lambda: self._ask(
                runtime, system=system, instructions=instructions,
                data=data, guard=retry_guard, model=model,
                extra_body={"think": False}))

    # -- critics --------------------------------------------------------
    @staticmethod
    def _structure_critique(settings: MusicSettings, known: tuple[str, ...]) -> Critique:
        issues = review_music_inputs(
            lyrics=settings.lyrics,
            musical_direction=settings.musical_direction,
            duration_s=settings.duration_s,
            arrangement=settings.arrangement,
            known_sections=known,
            bpm=settings.bpm,
        )
        defects = classify_defects(issues)
        errors = [d for d in defects if d.severity == "error"]
        return Critique(
            critic="structure",
            verdict="revise" if errors else ("pass" if not defects else "pass"),
            defects=defects,
            score=max(0.0, 1.0 - 0.2 * len(defects)),
            evidence=[d.code for d in defects],
        )

    @staticmethod
    def _production_critique(settings: MusicSettings, brief_text: str) -> Critique:
        """Checks that need no model: leakage, coverage, and contradictions."""
        defects: list[Defect] = []
        direction = settings.musical_direction or ""
        named = re.findall(r"\b(?:Michael Jackson|Rockwell|Cab Calloway|John Carpenter|"
                           r"Panic! At The Disco|Brian Setzer|Vincent Price|"
                           r"Caravan Palace)\b", direction, re.IGNORECASE)
        if named:
            defects.append(Defect(
                code="named_artist_leak", section="",
                message=f"named references reached the generator tags: {sorted(set(named))}",
                severity="warn"))
        for section, directive in settings.arrangement.items():
            if re.search(r"\binstrumental\b", directive, re.IGNORECASE):
                body = settings.lyrics.split(f"[{section}]", 1)[-1]
                if re.search(r"[A-Za-z]{3,}", body.split("[", 1)[0]):
                    defects.append(Defect(
                        code="instrumental_lyric_conflict", section=section,
                        message=f"[{section}] is called instrumental but carries words",
                        severity="error"))
        if brief_text and not direction:
            defects.append(Defect(code="empty_direction", section="",
                                  message="the musical direction is empty", severity="error"))
        return Critique(
            critic="production",
            verdict="revise" if any(d.severity == "error" for d in defects) else "pass",
            defects=defects,
            score=max(0.0, 1.0 - 0.2 * len(defects)),
            evidence=[d.code for d in defects],
        )

    # -- the graph ------------------------------------------------------
    async def run(self, pid: str, source: str, *, model: str | None = None) -> RunResult:
        """Produce one draft. Never approves, never generates audio."""
        from app.config import get_settings

        raw = (source or "").strip()
        if len(raw) < 8:
            raise ValueError("the prompt is too short to work with")
        data = sanitize_brief(raw)
        cfg = get_settings()
        known = tuple(cfg.music.known_sections)
        target = model or self.lms.default_model
        runtime = AgentRuntime(self.budget.derive())
        mode = "fast" if brief_is_complete(data) else "decomposed"
        result = RunResult(run_id=runtime.run_id, mode=mode, model=target)
        result.budget = runtime.budget.as_dict()

        extracted = extract_operator_lyrics(data)
        try:
            settings, filled, defects = await self._produce(
                runtime, data=data, extracted=extracted, known=known,
                model=target, mode=mode)
        except AgentBudgetExhausted as exc:
            result.status = "failed"
            result.notes.append(f"budget exhausted: {exc}")
            self._finalise(runtime, result)
            self._record(pid, result, raw, mode)
            return result
        except AgentStepFailed as exc:
            result.status = "failed"
            result.notes.append(f"a step failed: {exc}")
            self._finalise(runtime, result)
            self._record(pid, result, raw, mode)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            result.status = "failed"
            result.notes.append(f"the run failed: {exc}")
            self._finalise(runtime, result)
            self._record(pid, result, raw, mode)
            raise
        result.filled = filled
        result.defects = defects

        try:
            settings = MusicSettings.model_validate(settings)
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed run
            result.status = "failed"
            result.notes.append(f"the assembled draft failed validation: {exc}")
            self._finalise(runtime, result)
            self._record(pid, result, raw, mode)
            return result

        title = self._title(pid, settings)
        try:
            saved = self.service.save_draft(
                pid, settings, revision_name=f"Agent draft: {title}"[:60],
                source="prompt_draft")
            draft = self._validated_saved_draft(saved)
        except DraftSaveInvalid as exc:
            # A malformed response is not a success. A write may already have
            # completed, so we neither retry the save nor claim the state was
            # unchanged; the failed/uncertain record is the honest output.
            result.status = "failed"
            result.notes.append(
                f"the draft save response was malformed ({exc.reason}); a write may "
                "have completed and the state was not verified")
            self._finalise(runtime, result)
            self._record(pid, result, raw, mode)
            return result
        except Exception as exc:  # noqa: BLE001 - a failed save is still auditable
            result.status = "failed"
            result.notes.append(f"the draft could not be saved: {exc}")
            self._finalise(runtime, result)
            self._record(pid, result, raw, mode)
            raise
        result.status = "saved"
        result.draft_version = draft["version"]
        result.lyrics_sha256 = str(draft.get("lyrics_sha256") or "")
        result.settings_sha256 = str(draft.get("settings_sha256") or "")
        result.title = title
        self._finalise(runtime, result)
        if not self._record(pid, result, raw, mode):
            # The draft was saved but the audit row could not be written; do
            # not report a fully durable result.
            result.status = "partial"
        return result

    @staticmethod
    def _validated_saved_draft(saved: Any) -> dict[str, Any]:
        """Enforce the ``save_draft`` return contract.

        The contract (``MusicService.save_draft``) returns a mapping whose
        ``draft`` entry is a mapping with an integer ``version`` of at least 1.
        Anything else is malformed.
        """
        if not isinstance(saved, Mapping):
            raise DraftSaveInvalid(
                f"save returned {type(saved).__name__}, not a mapping")
        draft = saved.get("draft")
        if not isinstance(draft, Mapping):
            raise DraftSaveInvalid("save returned no draft mapping")
        version = draft.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise DraftSaveInvalid(f"save returned an invalid draft version {version!r}")
        return dict(draft)

    @staticmethod
    def _usage_snapshot(runtime: AgentRuntime) -> dict[str, int]:
        """The run's measured, unknown, requested and committed usage."""
        budget = runtime.budget
        return {
            "calls": budget.calls,
            "calls_actual": budget.calls_actual,
            "calls_unknown": budget.calls_unknown,
            "tokens_in": budget.tokens_in,
            "tokens_out": budget.tokens_out,
            "tokens_unknown": budget.tokens_unknown,
            "total_tokens": budget.tokens,
            "tokens_requested": budget.tokens_requested,
            "tokens_committed": budget.committed_tokens,
            "elapsed_ms": int(budget.elapsed_s * 1000),
        }

    def _finalise(self, runtime: AgentRuntime, result: RunResult) -> None:
        """Attach the usage, budget and step evidence every terminal path owns."""
        result.usage = self._usage_snapshot(runtime)
        result.budget = runtime.budget.as_dict()
        result.steps = runtime.steps

    async def _produce(
        self, runtime: AgentRuntime, *, data: str, extracted, known: tuple[str, ...],
        model: str, mode: str,
    ) -> tuple[dict[str, Any], list[str], list[Defect]]:
        """Run the mode's steps and return a candidate settings dict."""
        from app.services.music_draft_service import build_draft_settings

        filled: list[str] = []
        if mode == "fast":
            # One assistant call, then deterministic critics.
            guard = StepGuard(name="fast_assistant", wall_s=180, tokens=8192)
            completion = await self._ask_resilient(
                runtime, system=_FAST_SYSTEM, instructions=_FAST_INSTRUCTIONS,
                data=data, guard=guard, model=model, filled=filled)
            payload = _extract_json(completion.text)
            candidate, built, problems = build_draft_settings(payload, data, known)
            filled.extend(built)
            if problems:
                raise ValueError("; ".join(problems))
            candidate = self._apply_operator_lyrics(candidate, extracted, filled)
        else:
            candidate, filled = await self._run_decomposed(
                runtime, data=data, extracted=extracted, known=known, model=model)

        settings = MusicSettings.model_validate(candidate)
        critique = self._structure_critique(settings, known)
        production = self._production_critique(settings, data)
        defects = critique.defects + production.defects
        for critic_name, critique in (("structure", critique), ("production", production)):
            for defect in critique.defects:
                filled.append(f"critic {critic_name}[{defect.code}]: {defect.message}")
        return candidate, filled, defects

    async def _run_decomposed(
        self, runtime: AgentRuntime, *, data: str, extracted, known: tuple[str, ...],
        model: str,
    ) -> tuple[dict[str, Any], list[str]]:
        """Narrow producers, each filling one slot."""
        filled: list[str] = []
        # 1. composition
        guard = StepGuard(name="composition", wall_s=180, tokens=2048)
        completion = await self._ask_resilient(
            runtime, system=_COMPOSITION_SYSTEM,
            instructions=_COMPOSITION_INSTRUCTIONS + _vocabulary_instructions(known),
            data=data, guard=guard, model=model, filled=filled)
        composition = _extract_json(completion.text)
        plan, plan_notes = _structure_plan(composition, known)
        filled.extend(plan_notes)
        if plan:
            filled.append("the structure plan set the sections: "
                          + ", ".join(name for name, _ in plan))
        # 2. lyrics (skipped when the operator supplied their own)
        lyrics_payload: dict[str, Any] = {}
        if not extracted.found:
            guard = StepGuard(name="lyricist", wall_s=180, tokens=4096)
            completion = await self._ask_resilient(
                runtime, system=_LYRICIST_SYSTEM,
                instructions=_LYRICIST_INSTRUCTIONS + _structure_instructions(plan, "lyricist"),
                data=data, guard=guard, model=model, filled=filled)
            lyrics_payload = _extract_json(completion.text)
            filled.append("the lyricist wrote the missing lyric lines")
        else:
            filled.append(
                f"the operator's own lyrics were kept ({extracted.line_count} line(s))")
        # 3. arrangement and sound design
        guard = StepGuard(name="arrangement", wall_s=180, tokens=4096)
        completion = await self._ask_resilient(
            runtime, system=_ARRANGER_SYSTEM,
            instructions=_ARRANGER_INSTRUCTIONS + _structure_instructions(plan, "arranger"),
            data=data, guard=guard, model=model, filled=filled)
        arrangement_payload = _extract_json(completion.text)
        guard = StepGuard(name="sound_design", wall_s=180, tokens=2048)
        completion = await self._ask_resilient(
            runtime, system=_DIRECTION_SYSTEM, instructions=_DIRECTION_INSTRUCTIONS,
            data=data, guard=guard, model=model, filled=filled)
        direction_payload = _extract_json(completion.text)
        merged = {
            "musical_direction": direction_payload.get("musical_direction", ""),
            "lyrics": lyrics_payload.get("lyrics", ""),
            "duration_s": composition.get("duration_s"),
            "bpm": composition.get("bpm"),
            "time_signature": composition.get("time_signature"),
            "key_scale": composition.get("key_scale"),
            "language": composition.get("language", "en"),
            "arrangement": arrangement_payload.get("arrangement", {}),
        }
        from app.services.music_draft_service import build_draft_settings

        candidate, filled2, problems = build_draft_settings(merged, data, known)
        filled.extend(filled2)
        if problems:
            raise ValueError("; ".join(problems))
        candidate = self._apply_operator_lyrics(candidate, extracted, filled)
        return candidate, filled

    @staticmethod
    def _apply_operator_lyrics(candidate: dict[str, Any], extracted, filled: list[str]) -> dict[str, Any]:
        """Guarantee the operator's words survive whatever the assistant wrote."""
        if not extracted.found:
            return candidate
        merged, replaced, added = merge_operator_lyrics(extracted, candidate.get("lyrics", ""))
        candidate["lyrics"] = merged
        filled.append(
            f"operator lyrics took priority ({extracted.line_count} line(s), "
            f"{len(replaced)} section(s) replaced, {len(added)} added)")
        return candidate

    def _title(self, pid: str, settings: MusicSettings) -> str:
        try:
            name = (self.store.get(pid).name or "").strip()
        except KeyError:
            name = ""
        return name or "Untitled"

    def _record(self, pid: str, result: RunResult, source: str, mode: str) -> bool:
        """Append one ledger row. Returns False when durable audit failed."""
        from app.agents.records import append_run_record

        try:
            state_dir = self.service._state_dir(pid)
        except KeyError:
            result.notes.append(
                "no audit ledger row was written: the project state directory is unknown")
            return False
        try:
            append_run_record(state_dir, {
            "project_id": pid,
            "run_id": result.run_id,
            "mode": mode,
            "model": result.model,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "outcome": result.status,
            "notes": result.notes,
            "defects": [defect.code for defect in result.defects],
            "filled": result.filled,
            "calls": result.usage.get("calls", 0),
            "calls_actual": result.usage.get("calls_actual", 0),
            "calls_unknown": result.usage.get("calls_unknown", 0),
            "tokens_in": result.usage.get("tokens_in", 0),
            "tokens_out": result.usage.get("tokens_out", 0),
            "tokens_unknown": result.usage.get("tokens_unknown", 0),
            "tokens_requested": result.usage.get("tokens_requested", 0),
            "tokens_committed": result.usage.get("tokens_committed", 0),
            "total_tokens": result.usage.get("total_tokens", 0),
            "elapsed_ms": result.usage.get("elapsed_ms", 0),
            "draft_version": result.draft_version,
            "lyrics_sha256": result.lyrics_sha256,
            "settings_sha256": result.settings_sha256,
            "steps": result.steps,
            "budget": result.budget,
            })
        except Exception as exc:  # noqa: BLE001 - audited honestly, then reported
            result.notes.append(f"the audit ledger could not be written: {exc}")
            return False
        return True


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------
_FAST_SYSTEM = (
    "You are a senior record producer. You reply with one JSON object and never "
    "rewrite lyrics the operator supplied."
)
_FAST_INSTRUCTIONS = (
    "Build the music draft for the operator's source below. Reply with one JSON "
    "object with these keys: title, musical_direction, lyrics, duration_s, bpm, "
    "time_signature, key_scale, language, arrangement, mix_chain, references.\n"
    "Keep every lyric line the operator supplied, verbatim and in order. Put each "
    "section tag on its own line. Give every section tag in the lyrics exactly one "
    "arrangement directive of at most 240 characters, keyed by the exact tag text. "
    "Never name an artist, band, song or film in musical_direction; put those in "
    "references. At most 600 characters for musical_direction."
)
_COMPOSITION_SYSTEM = "You are a songwriting producer who plans structure. You reply with one JSON object."
_COMPOSITION_INSTRUCTIONS = (
    "Plan the song structure from the operator's source. Reply with one JSON object "
    "with keys: key_scale, bpm, time_signature, duration_s, sections, rationale. "
    "Use the operator's tempo, key and length when stated. sections is a list of "
    "objects with name, chords and energy. Every section the operator asked for "
    "must appear, including a bridge or solo section. Set vocal to false for a "
    "section the operator described as instrumental, and true otherwise. Keep the "
    "duration long enough for the lyric count: at least 3.5 seconds per sung line."
)
_LYRICIST_SYSTEM = "You are a lyricist. You reply with one JSON object and never rewrite supplied lyrics."
_LYRICIST_INSTRUCTIONS = (
    "Write the missing lyrics for the operator's source. Reply with one JSON object "
    "with keys: lyrics and written_sections. Use [Section] tags on their own lines. "
    "At least four lines per verse, pre-chorus and chorus, with a consistent rhyme "
    "scheme. If the source already contains lyrics, reproduce them verbatim and "
    "write only the sections it is missing."
)
_ARRANGER_SYSTEM = "You are an arranger. You reply with one JSON object."
_ARRANGER_INSTRUCTIONS = (
    "Write the arrangement plan for the operator's source. Reply with one JSON "
    "object with keys: arrangement and unresolved. arrangement maps each section "
    "tag to a directive of at most 240 characters covering instrumentation, energy, "
    "chords and sound effects. If a tag appears more than once, the directive must "
    "read correctly for every occurrence. Never call a section instrumental when it "
    "carries words."
)


def _vocabulary_instructions(known: Iterable[str]) -> str:
    """The section names the draft may use."""
    names = [str(item).strip() for item in known if str(item).strip()]
    if not names:
        return ""
    return (" Name each section with exactly one of these names: "
            + ", ".join(_display_section(item) for item in names) + ". Do not combine "
            "names and do not add any other.")


def _display_section(name: str) -> str:
    """The house spelling of a section name: Pre-Chorus, not pre-chorus."""
    return "-".join(part[:1].upper() + part[1:] for part in name.split("-") if part)


def _canonical_section(name: str, known: Iterable[str]) -> str | None:
    """Map a planned section name onto the app's section vocabulary.

    The operator's brief names sections in prose ("Bridge / Instrumental
    Solo"). The draft has to use a name the app recognises, so the planned
    name is mapped rather than invented or discarded.
    """
    vocabulary = {item.casefold(): item for item in known}
    lowered = name.strip().casefold()
    if not lowered:
        return None
    if lowered in vocabulary:
        return _display_section(vocabulary[lowered])
    numbered = re.fullmatch(r"(.+?) [1-9][0-9]*", lowered)
    if numbered and numbered.group(1) in vocabulary:
        return _display_section(vocabulary[numbered.group(1)])
    parts = [part.strip().casefold()
             for part in re.split(r"[/,;&|]|\s+-\s+", name) if part.strip()]
    for part in parts:
        if part in vocabulary:
            return _display_section(vocabulary[part])
    for part in parts:
        numbered = re.fullmatch(r"(.+?) [1-9][0-9]*", part)
        if numbered and numbered.group(1) in vocabulary:
            return _display_section(vocabulary[numbered.group(1)])
    return None


def _structure_plan(composition: dict[str, Any],
                    known: Iterable[str]) -> tuple[list[tuple[str, bool]], list[str]]:
    """The section names the composition step planned, with their vocal flag."""
    plan: list[tuple[str, bool]] = []
    notes: list[str] = []
    sections = composition.get("sections")
    if not isinstance(sections, list):
        return plan, notes
    for item in sections:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            vocal = item.get("vocal")
        else:
            name = str(item or "").strip()
            vocal = None
        if not name:
            continue
        canonical = _canonical_section(name, known)
        if canonical is None:
            notes.append(f"the planned section '{name}' is not a recognised "
                         "section name and was not planned")
            continue
        if any(existing.casefold() == canonical.casefold() for existing, _ in plan):
            continue
        plan.append((canonical, True if vocal is None else bool(vocal)))
    return plan, notes


def _structure_instructions(plan: list[tuple[str, bool]], role: str,
                            known: Iterable[str] = ()) -> str:
    """Tell the next producer which sections the song actually has."""
    if not plan:
        return ""
    listed = ", ".join(name for name, _ in plan)
    if role == "lyricist":
        instrumental = [name for name, vocal in plan if not vocal]
        extra = ""
        if instrumental:
            extra = (" The sections " + ", ".join(instrumental) + " are instrumental: "
                     "give each one its tag on its own line and no lyric lines under it.")
        return (" The planned sections of this song are: " + listed + ". Write a "
                "[Section] tag for every one of them, in that order, and no other "
                "tag." + extra)
    return (" The planned sections of this song are: " + listed + ". Write exactly "
            "one arrangement directive for each of those sections, and none for any "
            "other section."
            + _instrumental_directive_instruction(plan))


def _instrumental_directive_instruction(plan: list[tuple[str, bool]]) -> str:
    """An empty section is only honest when the directive says it is instrumental."""
    instrumental = [name for name, vocal in plan if not vocal]
    if not instrumental:
        return ""
    return (" The sections " + ", ".join(instrumental) + " carry no sung words: say "
            "so in their directive, for example 'instrumental, no vocals'.")
_DIRECTION_SYSTEM = "You are a sound designer. You reply with one JSON object."
_DIRECTION_INSTRUCTIONS = (
    "Describe the sound for the operator's source. Reply with one JSON object with "
    "keys: musical_direction and references. musical_direction is at most 600 "
    "characters of plain text: genre, mood, tempo feel, instrumentation, vocal "
    "character and mix flavour. Never name an artist, band, song or film there; "
    "put those in references instead."
)


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the assistant's JSON, tolerating fences and control characters."""
    from app.services.music_draft_service import extract_json_object

    return extract_json_object(text)


_pipeline: AgentPipeline | None = None


def get_agent_pipeline() -> AgentPipeline:
    """The process-wide pipeline. One instance keeps one budget policy."""
    global _pipeline
    if _pipeline is None:
        _pipeline = AgentPipeline()
    return _pipeline
