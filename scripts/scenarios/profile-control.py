#!/usr/bin/env python3
"""Fenced, idempotent per-profile boundary used by AdaptiveRuntime."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

ROOT = Path(__file__).resolve().parent
COORDINATOR_STATE = Path("/app/state/coordinator.json")
CONTROL_STATE = Path("/app/state/profile-control.json")
SPEC = importlib.util.spec_from_file_location("profile_control_compiler", ROOT / "compile-plan.py")
assert SPEC and SPEC.loader
compiler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compiler)


class ControlError(RuntimeError):
    pass


class Result(NamedTuple):
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _run(argv: Sequence[str]) -> Result:
    completed = subprocess.run(
        list(argv), check=False, capture_output=True, text=True, timeout=1200
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProfileController:
    def __init__(
        self,
        *,
        root: Path = ROOT,
        coordinator_state: Path = COORDINATOR_STATE,
        control_state: Path = CONTROL_STATE,
        compile_plan=compiler.compile_plan,
        runner: Callable[[Sequence[str]], Result] = _run,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.root = root.resolve()
        self.coordinator_state = coordinator_state
        self.coordinator_lock = coordinator_state.with_suffix(coordinator_state.suffix + ".lock")
        self.control_state = control_state
        self.control_lock = control_state.with_suffix(control_state.suffix + ".lock")
        self.compile_plan = compile_plan
        self.runner = runner
        self.clock = clock

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlError(f"trusted state unreadable: {path.name}") from exc
        if not isinstance(value, dict):
            raise ControlError(f"trusted state invalid: {path.name}")
        return value

    def _write_control(self, value: dict[str, Any]) -> None:
        self.control_state.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        fd, raw = tempfile.mkstemp(prefix="profile-control.", dir=self.control_state.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(raw, 0o640)
            os.replace(raw, self.control_state)
        finally:
            if os.path.exists(raw):
                os.unlink(raw)

    def _plan(self, slug: str, digest: str, confirmation: str) -> dict[str, Any]:
        try:
            plan = self.compile_plan(slug)
        except Exception as exc:
            raise ControlError(f"trusted plan compilation failed: {exc}") from exc
        if not plan.get("live_allowed") or plan.get("plan_digest") != digest:
            raise ControlError("current compiled plan does not authorize this live action")
        if confirmation != f"LIVE:{plan['scenario']['id']}:{digest}":
            raise ControlError("live confirmation mismatch")
        return plan

    def _instance(self, plan: dict[str, Any], profile_id: str) -> tuple[dict[str, Any], Path]:
        matches = [row for row in plan["profile_instances"] if row["profile_id"] == profile_id]
        if len(matches) != 1:
            raise ControlError("profile is not uniquely bound in the compiled plan")
        instance = matches[0]
        executor = (self.root / instance["executor"]).resolve()
        try:
            executor.relative_to(self.root)
        except ValueError as exc:
            raise ControlError("executor escapes trusted root") from exc
        if not executor.is_file() or not os.access(executor, os.X_OK):
            raise ControlError("trusted profile executor is unavailable")
        if hashlib.sha256(executor.read_bytes()).hexdigest() != instance["executor_sha256"]:
            raise ControlError("trusted profile executor hash drift")
        return instance, executor

    def _verify_lease(
        self, *, run_id: str, token: int, scenario_id: str, allow_dirty: bool
    ) -> None:
        state = self._load(self.coordinator_state, {})
        active = state.get("active_lease")
        candidates = [active, state.get("dirty_run") if allow_dirty else None]
        lease = next(
            (row for row in candidates if isinstance(row, dict)
             and row.get("run_id") == run_id
             and row.get("fencing_token") == token
             and row.get("scenario_id") == scenario_id),
            None,
        )
        if lease is None:
            raise ControlError("operation rejected by runner lease/fencing proof")
        claim = state.get("cleanup_claim")
        matching_claim = (
            isinstance(claim, dict)
            and claim.get("run_id") == run_id
            and claim.get("fencing_token") == token
        )
        if not allow_dirty and claim is not None:
            raise ControlError("cleanup claim is a terminal fence for profile mutation")
        if lease is active and lease.get("expires_at"):
            expires = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
            if expires <= self.clock() and not (allow_dirty and matching_claim):
                raise ControlError("runner lease is expired")
        if allow_dirty and lease is active and claim is not None and not matching_claim:
            raise ControlError("cleanup claim does not match runner lease/fencing proof")

    def _parameters(
        self, instance: dict[str, Any], level_index: int | None,
        level_id: str | None, raw: str | None,
    ) -> tuple[int | None, dict[str, Any]]:
        try:
            supplied = json.loads(raw) if raw is not None else None
        except json.JSONDecodeError as exc:
            raise ControlError("--parameters-json is invalid") from exc
        if raw is not None and raw != self._canonical(supplied):
            raise ControlError("--parameters-json must use canonical JSON encoding")
        levels = instance.get("approved_levels", [])
        if levels:
            if level_index is None or not 0 <= level_index < len(levels):
                raise ControlError("level index is outside the predeclared adaptive ladder")
            level = levels[level_index]
            if level_id is not None and level_id != level.get("level_id"):
                raise ControlError("level id does not match the predeclared adaptive ladder")
            expected = level.get("parameters", level) if isinstance(level, dict) else level
            if supplied != expected:
                raise ControlError("parameters do not exactly match the predeclared level")
            return level_index, expected
        if level_index not in {None, 0}:
            raise ControlError("fixed profile does not accept another level index")
        if level_id is not None:
            raise ControlError("fixed profile does not accept a level id")
        expected = instance.get("parameters", {})
        if supplied is not None and supplied != expected:
            raise ControlError("parameters do not exactly match the compiled fixed profile")
        return level_index, expected

    def execute(
        self, *, slug: str, profile_id: str, action: str, run_id: str,
        fencing_token: int, idempotency_key: str, digest: str,
        confirmation: str, level_index: int | None = None,
        level_id: str | None = None, parameters_json: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"apply", "cleanup"}:
            raise ControlError("action must be apply or cleanup")
        if action == "cleanup" and (
            level_index is not None or level_id is not None or parameters_json is not None
        ):
            raise ControlError("cleanup does not accept level parameters")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", idempotency_key):
            raise ControlError("invalid idempotency key")
        self.coordinator_lock.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.control_lock.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with self.coordinator_lock.open("a+") as lease_lock, self.control_lock.open("a+") as state_lock:
            fcntl.flock(lease_lock.fileno(), fcntl.LOCK_EX)
            fcntl.flock(state_lock.fileno(), fcntl.LOCK_EX)
            plan = self._plan(slug, digest, confirmation)
            instance, executor = self._instance(plan, profile_id)
            self._verify_lease(
                run_id=run_id, token=fencing_token,
                scenario_id=plan["scenario"]["id"], allow_dirty=action == "cleanup",
            )
            state = self._load(self.control_state, {"schema_version": "1.0", "results": {}})
            request = {
                "run_id": run_id, "fencing_token": fencing_token, "scenario": slug,
                "profile": profile_id, "action": action, "plan_digest": digest,
                "level_index": level_index, "level_id": level_id,
                "parameters_json": parameters_json,
            }
            existing = state.setdefault("results", {}).get(idempotency_key)
            if existing is not None:
                if existing.get("request") != request:
                    raise ControlError("idempotency key was already used for another request")
                return existing["response"]

            selected, parameters = self._parameters(
                instance, level_index, level_id, parameters_json
            ) if action == "apply" else (None, instance.get("parameters", {}))
            common = [
                "--scenario", slug, "--plan-digest", digest,
                "--confirm", confirmation, "--live",
            ]
            if action == "apply" and instance.get("approved_levels"):
                common.extend([
                    "--level-index", str(selected),
                    "--parameters-json", self._canonical(parameters),
                ])
            # Do not hold the coordinator lock while a remote preflight or
            # cleanup runs. The runner heartbeat owns that lock and must be
            # able to renew the lease throughout long recovery operations.
            fcntl.flock(lease_lock.fileno(), fcntl.LOCK_UN)
            preflight = self.runner([str(executor), "preflight", *common])
            if action == "apply" and preflight.returncode:
                detail = preflight.stderr.strip() or preflight.stdout.strip() or "no detail"
                raise ControlError(f"profile preflight failed: {detail}")
            # Re-acquire for the final fenced proof. Apply keeps this lock for
            # the complete mutation invocation, serializing watchdog claims.
            fcntl.flock(lease_lock.fileno(), fcntl.LOCK_EX)
            refreshed = self._plan(slug, digest, confirmation)
            _, executor = self._instance(refreshed, profile_id)
            self._verify_lease(
                run_id=run_id, token=fencing_token,
                scenario_id=refreshed["scenario"]["id"], allow_dirty=action == "cleanup",
            )
            verb = "run" if action == "apply" else "cleanup"
            effect_started = self.clock() if action == "apply" else None
            if action != "apply":
                fcntl.flock(lease_lock.fileno(), fcntl.LOCK_UN)
            completed = self.runner([str(executor), verb, *common])
            if action == "apply":
                fcntl.flock(lease_lock.fileno(), fcntl.LOCK_UN)
            now = self.clock()
            if completed.returncode:
                detail = completed.stderr.strip() or completed.stdout.strip() or "no detail"
                if action == "apply":
                    raise ControlError(f"profile apply failed: {detail}")
                response = {"succeeded": False, "effect_ended_at": None, "reason": detail}
            elif action == "apply":
                assert effect_started is not None
                response = {"applied_at": self._timestamp(effect_started)}
            else:
                fcntl.flock(lease_lock.fileno(), fcntl.LOCK_EX)
                refreshed = self._plan(slug, digest, confirmation)
                _, executor = self._instance(refreshed, profile_id)
                self._verify_lease(
                    run_id=run_id, token=fencing_token,
                    scenario_id=refreshed["scenario"]["id"], allow_dirty=True,
                )
                fcntl.flock(lease_lock.fileno(), fcntl.LOCK_UN)
                recovery = self.runner([
                    str(executor), "recovery", "--scenario", slug,
                    "--plan-digest", digest, "--confirm", confirmation, "--live",
                ])
                succeeded = recovery.returncode == 0
                response = {
                    "succeeded": succeeded,
                    "effect_ended_at": self._timestamp(now) if succeeded else None,
                    "reason": None if succeeded else (recovery.stderr.strip() or "recovery failed"),
                }
            state["results"][idempotency_key] = {"request": request, "response": response}
            self._write_control(state)
            return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--action", required=True, choices=["apply", "cleanup"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fencing-token", required=True, type=int)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--level-index", type=int)
    parser.add_argument("--level-id")
    parser.add_argument("--parameters-json")
    args = parser.parse_args()
    print(json.dumps(ProfileController().execute(
        slug=args.scenario, profile_id=args.profile, action=args.action,
        run_id=args.run_id, fencing_token=args.fencing_token,
        idempotency_key=args.idempotency_key, digest=args.plan_digest,
        confirmation=args.confirm, level_index=args.level_index,
        level_id=args.level_id, parameters_json=args.parameters_json,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ControlError as exc:
        print(f"profile control refused: {exc}", file=sys.stderr)
        raise SystemExit(3)
