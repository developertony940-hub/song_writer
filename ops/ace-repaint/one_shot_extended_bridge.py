"""Standalone one-shot extended-Bridge launcher.

This process does not need a website restart and never submits through the web
app. It starts one held service via first-load.ps1, runs one authorized
extended-Bridge submission, signals the owned service to shut down, and records
the launch outcome. Fixture controllers/transports are used by tests; no real
provider is contacted by the tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.music_service import MusicService  # noqa: E402
from app.services.replacement_export import ExtendedBridgeExportService  # noqa: E402


class LauncherError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ServiceController(Protocol):
    def start(self) -> dict[str, Any]: ...
    def shutdown(self) -> dict[str, Any]: ...
    def provider_client(self) -> Any: ...


class FirstLoadController:
    """Start the existing one-load launcher in held mode; it owns shutdown."""

    def __init__(self, *, ready_path: Path, release_path: Path,
                 timeout_s: int = 900) -> None:
        self.ready_path = Path(ready_path)
        self.release_path = Path(release_path)
        self.timeout_s = timeout_s
        self.process: subprocess.Popen | None = None

    def start(self) -> dict[str, Any]:
        for path in (self.ready_path, self.release_path):
            path.unlink(missing_ok=True)
        command = [
            "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(REPO / "ops" / "ace-repaint" / "first-load.ps1"),
            "-HoldOpen",
            "-HoldReady", str(self.ready_path),
            "-HoldSignal", str(self.release_path),
            "-HoldTimeoutS", "7200",
        ]
        self.process = subprocess.Popen(
            command, cwd=str(REPO), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.ready_path.is_file():
                return json.loads(self.ready_path.read_text(encoding="utf-8"))
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise LauncherError(
                    "startup/load failed before hold: "
                    + (stderr or stdout or "unknown")[-2000:])
            time.sleep(0.5)
        raise LauncherError("startup/load readiness timed out; no retry")

    def provider_client(self) -> Any:
        return None

    def shutdown(self) -> dict[str, Any]:
        self.release_path.parent.mkdir(parents=True, exist_ok=True)
        self.release_path.write_text("release\n", encoding="utf-8", newline="")
        if self.process is not None:
            try:
                self.process.wait(timeout=300)
            except subprocess.TimeoutExpired:
                return {"status": "shutdown-timeout", "process_exited": False}
        result = {"status": "shutdown-requested",
                  "process_exited": self.process is None or self.process.poll() is not None}
        ready = json.loads(self.ready_path.read_text(encoding="utf-8")) if self.ready_path.is_file() else {}
        run_dir = Path(ready.get("run_dir", "")) if ready.get("run_dir") else None
        if run_dir and (run_dir / "outcome.json").is_file():
            result["service_outcome"] = json.loads(
                (run_dir / "outcome.json").read_text(encoding="utf-8"))
        return result


def _durable_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LauncherError(
            "one-shot launch lock already exists; refusing another launch") from exc
    try:
        os.write(fd, (str(os.getpid()) + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


class OneShotLauncher:
    def __init__(self, *, plan_dir: Path, authorization: dict[str, Any],
                 run_root: Path, music: MusicService,
                 controller: ServiceController,
                 runner: ExtendedBridgeExportService | None = None,
                 client: Any = None) -> None:
        self.plan_dir = Path(plan_dir)
        self.authorization = authorization
        self.run_root = Path(run_root)
        self.music = music
        self.controller = controller
        self.runner = runner or ExtendedBridgeExportService(music=music)
        self.client = client

    def run(self) -> dict[str, Any]:
        self.run_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.run_root / "one-shot.lock"
        _durable_lock(lock_path)
        plan_path = self.plan_dir / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        pid = plan["project_id"]
        result: dict[str, Any] = {
            "schema": "song_writer.extended_bridge_launch_outcome.v1",
            "launch_id": self.authorization.get("launch_id"),
            "authorization_id": self.authorization.get("authorization_id"),
            "plan_sha256": _sha(plan_path),
            "project_id": pid,
            "startup": "not_started",
            "model_load": "not_claimed",
            "submission": "not_started",
            "music_submission_consumed": False,
            "no_retry": True,
            "website_restart_required": False,
        }
        try:
            self.music.claim_extended_bridge_launch(
                pid, self.plan_dir, self.authorization)
            result["launch_claim"] = "claimed"
        except Exception as exc:
            result["status"] = "predispatch_rejected"
            result["error"] = str(exc)[:2000]
            self._write(result)
            return result
        try:
            ready = self.controller.start()
            result["startup"] = "started"
            result["service_ready"] = ready
        except Exception as exc:
            result["status"] = "startup_or_load_failed"
            result["startup"] = "failed"
            result["error"] = str(exc)[:2000]
            result["music_submission_consumed"] = False
            result["shutdown"] = self.controller.shutdown()
            self._write(result)
            return result
        before = self.music._journal_state(pid)["submissions"]
        try:
            submission = self.runner.execute_live(
                self.plan_dir, authorization=self.authorization,
                client=self.client)
            result["submission"] = submission
            result["status"] = submission.get("execution_status", "completed")
        except Exception as exc:
            result["submission"] = {"status": "error", "error": str(exc)[:2000]}
            result["status"] = "submission_error"
            result["note"] = (
                "If dispatch occurred, the shared journal is the accounting "
                "authority; uncertainty is not refunded.")
        finally:
            after = self.music._journal_state(pid)["submissions"]
            result["music_submission_consumed"] = after > before
            if result["status"] == "submission_error" and not result["music_submission_consumed"]:
                result["status"] = "predispatch_rejected"
            result["shutdown"] = self.controller.shutdown()
        self._write(result)
        return result

    def _write(self, result: dict[str, Any]) -> None:
        (self.run_root / "launch-outcome.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-dir", required=True)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    plan_dir = Path(args.plan_dir)
    authorization = json.loads(Path(args.authorization).read_text(encoding="utf-8"))
    run_root = Path(args.run_root)
    controller = FirstLoadController(
        ready_path=run_root / "service-ready.json",
        release_path=run_root / "service-release.signal")
    launcher = OneShotLauncher(
        plan_dir=plan_dir, authorization=authorization, run_root=run_root,
        music=MusicService(), controller=controller)
    outcome = launcher.run()
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome.get("status") in {"completed", "failed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
