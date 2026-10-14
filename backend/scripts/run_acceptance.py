#!/usr/bin/env python
"""Run a Phase 4 local production acceptance workflow.

Run from backend (or use ops/run-acceptance.ps1 on Windows).  Reports are
written under the resulting project's reviews directory and a compact JSON
summary is printed for CI artifact collection.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# ``python scripts/run_acceptance.py`` is the documented Windows form; make
# the backend package importable regardless of Python's script-directory path.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.acceptance import AcceptanceService


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalAIContent Phase 4 production acceptance")
    parser.add_argument("--mode", choices=("offline", "real"), default="offline", help="offline uses deterministic FFmpeg fixtures; real calls configured local services")
    parser.add_argument("--operator-approved", action="store_true", help="attest that the exact recorded proxy bytes were watched (real proxy-review resume only)")
    parser.add_argument("--operator-rejected", action="store_true", help="record rejection of the exact recorded proxy and open the remediation path (real proxy-review resume only)")
    parser.add_argument("--project-id", help="resume only the pending real acceptance run identified in its report")
    args = parser.parse_args()
    report = asyncio.run(AcceptanceService().run(args.mode, operator_approved=args.operator_approved, operator_rejected=args.operator_rejected, project_id=args.project_id))
    print(json.dumps({key: report.get(key) for key in ("status", "project_id", "reports", "failures")}, indent=2))
    return 0 if report["status"].startswith("passed_") or report["status"] in {"pending_operator_proxy_review", "pending_operator_candidate_review", "pending_operator_remediation"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
