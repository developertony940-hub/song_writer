# Budget units and guarantees (successor candidate)

This note states what the agent budget actually measures. It exists so the
successor is not described as enforcing guarantees it does not implement.

## Units

| Field | Unit | Meaning |
|---|---|---|
| `StepGuard.tokens` | output tokens | Sent to the provider as `max_tokens`; the per-call output cap |
| `RunBudget.tokens_requested` | output tokens | Sum of `StepGuard.tokens` for calls started; **not** a total-token reservation |
| `RunBudget.tokens_in` / `tokens_out` | tokens | Measured prompt and completion counts reported by the provider |
| `RunBudget.tokens_unknown` | output tokens | The reserved output allowance charged when usage is absent or invalid |
| `RunBudget.tokens_reserved` | output tokens | Allowance held by in-flight steps |
| `RunBudget.committed_tokens` | mixed (actual + unknown + reserved) | What admission compares against `max_tokens` |
| `RunBudget.max_tokens` | tokens | The explicit existing run cap (128000), unchanged |
| `RunBudget.max_wall_s` | seconds | Run deadline, checked at admission only |
| `StepGuard.wall_s` | seconds | Per-step timeout via `asyncio.timeout` |

## Guarantees that ARE made

1. **In-flight admission.** A step is refused when
   `committed_tokens + guard.tokens > max_tokens`, so two concurrent steps
   cannot reserve the same headroom.
2. **Settle-once.** A reservation is released exactly once; a second settle
   raises. A call is not charged twice.
3. **Unknown outcomes are charged.** Absent, partial, negative, boolean or
   non-integer usage settles as `tokens_unknown` equal to the reserved output
   allowance. It is not recorded as a known zero and is not retried.
4. **Per-step timeout.** `asyncio.timeout(guard.wall_s)` bounds each step.

## Guarantees that are NOT made

1. **No hard total-token upper bound.** Settlement charges prompt plus
   completion usage, while the admission check reserves the *output* cap
   (`guard.tokens`). A provider that reports more than the output cap can push
   one call past `max_tokens`; the overrun is recorded and the next admission
   fails, but the current call is not aborted. Demonstrated by
   `test_prompt_plus_output_can_exceed_the_output_reservation`.
2. **No hard total-wall upper bound.** `max_wall_s` is checked at admission.
   A step admitted with one second left may run for its full `guard.wall_s`.
   Demonstrated by `test_a_step_admitted_before_the_deadline_can_finish_after_it`.

## Deliberate non-goals in this batch

- No tokenizer estimate and no new numeric cap.
- No post-call token enforcement/abort (that would be a separate accounting
  change, out of this bounded scope).
- No change to `max_tokens`, `max_llm_calls`, `max_steps`, `max_wall_s`.

If a hard total-token or total-wall guarantee is required, it needs its own
bounded design and Tony's decision; integration of this candidate should not be
described as providing one.
