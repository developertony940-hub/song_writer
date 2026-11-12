"""Versioned agent policies with human-gated adoption.

recommendations_validation.html V6: every other state transition in this
repository needs an explicit human authorisation, so a policy change does
too. A proposal can be generated and scored, but only an explicit approval
activates it, and the previous version stays one command away.

Nothing here can touch a gate, a cap, a hold or a hash. The policy holds
prompt text, a model role, a temperature, exemplars and a step budget.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.agents.contracts import PolicyVersion

log = logging.getLogger(__name__)

MAX_VERSIONS = 5
MAX_EXEMPLARS = 3
TEMPERATURE_RANGE = (0.2, 0.8)
BUDGET_FLOOR = {"wall_s": 15, "tokens": 512}

# Fields a policy may never carry, whatever a proposal asks for.
FORBIDDEN_FIELDS = (
    "approval", "submission_cap", "lifetime_cap", "live_generation_authorized",
    "capacity_verified", "capacity_override_authorized", "audio_approval",
    "lyrics_sha256", "settings_sha256", "prompt_template_version",
)


class PolicyError(RuntimeError):
    """Raised when a policy change is not allowed."""


def policy_dir(root: Path) -> Path:
    return root / "policies"


def _agent_dir(root: Path, agent: str) -> Path:
    safe = "".join(char for char in agent if char.isalnum() or char in "_-")
    return policy_dir(root) / safe


def template_hash(template: str) -> str:
    return hashlib.sha256((template or "").encode("utf-8")).hexdigest()


def validate_policy(policy: PolicyVersion) -> PolicyVersion:
    """Reject anything outside the narrow envelope a policy may occupy."""
    if not policy.agent.strip():
        raise PolicyError("a policy needs an agent name")
    if len(policy.exemplars) > MAX_EXEMPLARS:
        raise PolicyError(f"a policy may carry at most {MAX_EXEMPLARS} exemplars")
    low, high = TEMPERATURE_RANGE
    if not low <= policy.temperature <= high:
        raise PolicyError(f"temperature must be between {low} and {high}")
    for key, floor in BUDGET_FLOOR.items():
        if key in policy.budget and int(policy.budget[key]) < floor:
            raise PolicyError(f"budget {key} may not go below {floor}")
    for field in FORBIDDEN_FIELDS:
        if field in policy.budget or field in policy.scorecard:
            raise PolicyError(f"a policy may not carry {field}")
    policy.template_sha256 = template_hash(policy.template)
    return policy


def list_policies(root: Path, agent: str) -> list[PolicyVersion]:
    """Every stored version of one agent, newest first."""
    directory = _agent_dir(root, agent)
    if not directory.is_dir():
        return []
    policies: list[PolicyVersion] = []
    for path in sorted(directory.glob("v*.json")):
        try:
            policies.append(PolicyVersion.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - a damaged file is reported
            log.warning("policy %s could not be read: %s", path, exc)
    policies.sort(key=lambda item: item.policy_version, reverse=True)
    return policies


def save_policy(root: Path, policy: PolicyVersion) -> PolicyVersion:
    """Store a version. The first version of an agent becomes active."""
    validate_policy(policy)
    directory = _agent_dir(root, policy.agent)
    directory.mkdir(parents=True, exist_ok=True)
    existing = list_policies(root, policy.agent)
    if existing and policy.policy_version <= existing[0].policy_version:
        raise PolicyError("a new policy must carry a higher version number")
    if len(existing) >= MAX_VERSIONS:
        oldest = existing[-1]
        oldest_path = directory / f"v{oldest.policy_version}.json"
        oldest_path.unlink(missing_ok=True)
        oldest.status = "superseded"
    if not existing:
        policy.status = "active"
    path = directory / f"v{policy.policy_version}.json"
    path.write_text(json.dumps(policy.model_dump(mode="json"), indent=2,
                               ensure_ascii=False) + "\n", encoding="utf-8")
    return policy


def active_policy(root: Path, agent: str) -> PolicyVersion | None:
    for policy in list_policies(root, agent):
        if policy.status == "active":
            return policy
    return None


def approve_policy(root: Path, agent: str, version: int, *, approved_by: str,
                   note: str = "") -> PolicyVersion:
    """Activate a stored version. This is the only way a policy changes."""
    if not approved_by.strip():
        raise PolicyError("an approval needs a name")
    policies = list_policies(root, agent)
    target = next((item for item in policies if item.policy_version == version), None)
    if target is None:
        raise PolicyError(f"policy v{version} does not exist for {agent}")
    if not target.scorecard:
        raise PolicyError("a policy needs a golden scorecard before it can be approved")
    for policy in policies:
        if policy.status == "active":
            policy.status = "superseded"
            _write(root, policy)
    target.status = "active"
    target.approved_by = approved_by.strip()
    target.approved_at = _now()
    target.notes = note
    _write(root, target)
    return target


def rollback_policy(root: Path, agent: str, *, approved_by: str) -> PolicyVersion:
    """Return to the previous version."""
    policies = list_policies(root, agent)
    if len(policies) < 2:
        raise PolicyError("there is no earlier version to roll back to")
    current = next((item for item in policies if item.status == "active"), None)
    previous = next((item for item in policies if item.status != "active"), None)
    if current is None or previous is None:
        raise PolicyError("there is no earlier version to roll back to")
    current.status = "superseded"
    _write(root, current)
    previous.status = "active"
    previous.approved_by = approved_by.strip()
    previous.approved_at = _now()
    _write(root, previous)
    return previous


def _write(root: Path, policy: PolicyVersion) -> None:
    directory = _agent_dir(root, policy.agent)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"v{policy.policy_version}.json").write_text(
        json.dumps(policy.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
