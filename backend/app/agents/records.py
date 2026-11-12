"""Append-only ledger of agent runs: what was called, what it cost, what happened.

One JSON row per run, written with the same append-and-fsync discipline as the
music attempt journal but in its own file. ``attempts.jsonl`` is never extended
with new row kinds because its parser treats an unknown kind as corruption, so
this evidence lives beside it, not inside it.

The ledger is the input the retro agent needs: token cost, latency, defect
codes, and the outcome of each run, which today is recorded nowhere.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_NAME = "agent_runs.jsonl"
MAX_LEDGER_ROWS = 500


def ledger_path(state_dir: Path) -> Path:
    """The ledger file for a music state directory."""
    return state_dir / LEDGER_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_run_record(state_dir: Path, row: dict[str, Any]) -> dict[str, Any]:
    """Append one run record and return it with the fields the ledger owns."""
    record = {
        "kind": "agent_run",
        "recorded_at": _now(),
        **{key: value for key, value in row.items() if key not in {"kind", "recorded_at"}},
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_path(state_dir)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def read_run_records(state_dir: Path) -> list[dict[str, Any]]:
    """Read every run record. A malformed line is reported, never repaired."""
    path = ledger_path(state_dir)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            rows.append({"kind": "ledger_error", "line": number})
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows[-MAX_LEDGER_ROWS:]


def summarise_run_records(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the ledger into the numbers the metrics panel needs."""
    usable = [row for row in rows if row.get("kind") == "agent_run"]
    tokens_in = sum(int(row.get("tokens_in") or 0) for row in usable)
    tokens_out = sum(int(row.get("tokens_out") or 0) for row in usable)
    elapsed = sum(int(row.get("elapsed_ms") or 0) for row in usable)
    outcomes: dict[str, int] = {}
    defects: dict[str, int] = {}
    for row in usable:
        outcome = str(row.get("outcome") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        for code in row.get("defects") or []:
            key = str(code)
            defects[key] = defects.get(key, 0) + 1
    return {
        "runs": len(usable),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total_tokens": tokens_in + tokens_out,
        "elapsed_ms": elapsed,
        "outcomes": outcomes,
        "defects": defects,
    }
