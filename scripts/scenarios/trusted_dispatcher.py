#!/usr/bin/env python3
"""Fail-closed live dispatcher for compiled scenario profile plans.

The shell entrypoint supplies only a scenario slug, action, digest, and exact
confirmation.  All executable paths, locations, and parameters are reloaded
from the repository-owned compiler contract before each profile action.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = Path("/var/lib/lucida/scenario-dispatcher")
COMPILER_SPEC = importlib.util.spec_from_file_location(
    "trusted_scenario_compile_plan", ROOT / "compile-plan.py"
)
assert COMPILER_SPEC and COMPILER_SPEC.loader
compiler = importlib.util.module_from_spec(COMPILER_SPEC)
COMPILER_SPEC.loader.exec_module(compiler)


class DispatchError(RuntimeError):
    """A live dispatch was refused or failed safely."""


class InvocationResult(NamedTuple):
    returncode: int
    stdout: str = ""
    stderr: str = ""


Invoker = Callable[[Sequence[str]], InvocationResult]
Compiler = Callable[[str], dict[str, Any]]


def subprocess_invoker(argv: Sequence[str]) -> InvocationResult:
    completed = subprocess.run(
        list(argv), check=False, capture_output=True, text=True, timeout=1200
    )
    return InvocationResult(completed.returncode, completed.stdout, completed.stderr)


class Dispatcher:
    def __init__(
        self,
        *,
        root: Path = ROOT,
        state_dir: Path = DEFAULT_STATE_DIR,
        compile_plan: Compiler = compiler.compile_plan,
        invoker: Invoker = subprocess_invoker,
    ) -> None:
        self.root = root.resolve()
        self.state_dir = state_dir
        self.state_path = state_dir / "state.json"
        self.lock_path = state_dir / "dispatcher.lock"
        self.compile_plan = compile_plan
        self.invoker = invoker

    @staticmethod
    def confirmation(plan: dict[str, Any]) -> str:
        return f"LIVE:{plan['scenario']['id']}:{plan['plan_digest']}"

    @staticmethod
    def _clean_state(fence: int = 0) -> dict[str, Any]:
        return {"schema_version": "1.0", "status": "clean", "fence": fence}

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._clean_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DispatchError("dispatcher state is unreadable; manual recovery required") from exc
        if state.get("status") not in {"clean", "active", "dirty"}:
            raise DispatchError("dispatcher state is invalid; manual recovery required")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        fd, temporary = tempfile.mkstemp(prefix="state.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _acquire_lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise DispatchError("another live scenario dispatch holds the global lease") from exc
        return handle

    def _verified_plan(self, slug: str, digest: str, confirmation: str) -> dict[str, Any]:
        try:
            plan = self.compile_plan(slug)
        except Exception as exc:
            raise DispatchError(f"trusted plan compilation failed: {exc}") from exc
        if not plan.get("live_allowed"):
            raise DispatchError("compiled normalized plan does not allow live execution")
        if digest != plan.get("plan_digest"):
            raise DispatchError("--plan-digest does not match the current compiled plan")
        if confirmation != self.confirmation(plan):
            raise DispatchError("--confirm does not match the exact live confirmation")
        return plan

    def _executor(self, instance: dict[str, Any]) -> Path:
        candidate = (self.root / instance["executor"]).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise DispatchError("profile executor escapes the trusted scenario root") from exc
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise DispatchError(f"profile executor is unavailable: {instance['profile_id']}")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != instance.get("executor_sha256"):
            raise DispatchError(f"profile executor hash drift: {instance['profile_id']}")
        return candidate

    def _invoke(
        self,
        *,
        slug: str,
        profile_id: str,
        action: str,
        digest: str,
        confirmation: str,
        fence: int,
    ) -> InvocationResult:
        # Recompilation revalidates manifest, catalog, registries, locations, and
        # every executor hash immediately before this action.
        plan = self._verified_plan(slug, digest, confirmation)
        matches = [
            item for item in plan["profile_instances"] if item["profile_id"] == profile_id
        ]
        if len(matches) != 1:
            raise DispatchError(f"profile plan binding changed: {profile_id}")
        executor = self._executor(matches[0])
        argv = [
            str(executor),
            action,
            "--scenario",
            slug,
            "--plan-digest",
            digest,
            "--confirm",
            confirmation,
            "--live",
        ]
        try:
            result = self.invoker(argv)
        except Exception as exc:
            raise DispatchError(
                f"profile {profile_id} {action} invocation failed under fence {fence}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no executor detail"
            raise DispatchError(
                f"profile {profile_id} {action} failed under fence {fence}: {detail}"
            )
        return result

    def _preflight_all(
        self,
        plan: dict[str, Any],
        slug: str,
        digest: str,
        confirmation: str,
        fence: int,
        *,
        strict: bool = True,
    ) -> list[str]:
        failures: list[str] = []
        for instance in plan["profile_instances"]:
            try:
                self._invoke(
                    slug=slug,
                    profile_id=instance["profile_id"],
                    action="preflight",
                    digest=digest,
                    confirmation=confirmation,
                    fence=fence,
                )
            except DispatchError as exc:
                failures.append(str(exc))
        if strict and failures:
            raise DispatchError("profile preflight failed: " + " | ".join(failures))
        return failures

    def _cleanup_and_recover(
        self,
        *,
        slug: str,
        profile_ids: list[str],
        digest: str,
        confirmation: str,
        fence: int,
    ) -> None:
        failures: list[str] = []
        for profile_id in reversed(profile_ids):
            try:
                self._invoke(
                    slug=slug,
                    profile_id=profile_id,
                    action="cleanup",
                    digest=digest,
                    confirmation=confirmation,
                    fence=fence,
                )
            except DispatchError as exc:
                failures.append(str(exc))
        if not failures:
            for profile_id in reversed(profile_ids):
                try:
                    self._invoke(
                        slug=slug,
                        profile_id=profile_id,
                        action="recovery",
                        digest=digest,
                        confirmation=confirmation,
                        fence=fence,
                    )
                except DispatchError as exc:
                    failures.append(str(exc))
        if failures:
            raise DispatchError("cleanup/recovery incomplete: " + " | ".join(failures))

    def dispatch(
        self, *, slug: str, action: str, digest: str, confirmation: str
    ) -> dict[str, Any]:
        if action not in {"run", "cleanup"}:
            raise DispatchError("live action must be run or cleanup")
        lock = self._acquire_lock()
        try:
            plan = self._verified_plan(slug, digest, confirmation)
            profile_ids = [item["profile_id"] for item in plan["profile_instances"]]
            state = self._read_state()
            fence = int(state.get("fence", 0)) + 1

            if action == "run":
                if plan["scenario"].get("load_mode") == "adaptive":
                    raise DispatchError(
                        "adaptive scenarios require the fenced profile-control runtime"
                    )
                if state["status"] != "clean":
                    raise DispatchError(
                        f"global dispatcher is {state['status']}; cleanup/recovery is required"
                    )
                active = {
                    "schema_version": "1.0",
                    "status": "active",
                    "fence": fence,
                    "scenario": slug,
                    "scenario_id": plan["scenario"]["id"],
                    "plan_digest": digest,
                    "profiles": profile_ids,
                    "applied": [],
                }
                self._write_state(active)
                applied: list[str] = []
                primary_error: DispatchError | None = None
                try:
                    self._preflight_all(plan, slug, digest, confirmation, fence)
                    for profile_id in profile_ids:
                        self._invoke(
                            slug=slug,
                            profile_id=profile_id,
                            action="run",
                            digest=digest,
                            confirmation=confirmation,
                            fence=fence,
                        )
                        applied.append(profile_id)
                        active["applied"] = list(applied)
                        self._write_state(active)
                except DispatchError as exc:
                    primary_error = exc
                try:
                    self._cleanup_and_recover(
                        slug=slug,
                        profile_ids=applied,
                        digest=digest,
                        confirmation=confirmation,
                        fence=fence,
                    )
                except DispatchError as cleanup_error:
                    dirty = dict(active)
                    dirty["status"] = "dirty"
                    dirty["applied"] = applied
                    dirty["error"] = str(cleanup_error)
                    self._write_state(dirty)
                    if primary_error:
                        raise DispatchError(f"{primary_error}; {cleanup_error}") from cleanup_error
                    raise
                self._write_state(self._clean_state(fence))
                if primary_error:
                    raise primary_error
                return {
                    "status": "complete",
                    "scenario": slug,
                    "scenario_id": plan["scenario"]["id"],
                    "plan_digest": digest,
                    "fence": fence,
                    "profiles": profile_ids,
                    "cleanup": "verified",
                }

            if state["status"] != "dirty":
                raise DispatchError("explicit cleanup requires a DIRTY dispatcher state")
            if state.get("scenario") != slug or state.get("plan_digest") != digest:
                raise DispatchError("cleanup must claim the same dirty scenario and plan digest")
            dirty_profiles = state.get("applied", [])
            if not isinstance(dirty_profiles, list) or not set(dirty_profiles) <= set(profile_ids):
                raise DispatchError("dirty profile state does not match the trusted current plan")
            # A DIRTY target may intentionally fail the normal "nothing is
            # injected" checks.  Run every read-only preflight and retain its
            # warnings, but allow the idempotent cleanup path to make progress.
            preflight_warnings = self._preflight_all(
                plan, slug, digest, confirmation, fence, strict=False
            )
            self._cleanup_and_recover(
                slug=slug,
                profile_ids=dirty_profiles,
                digest=digest,
                confirmation=confirmation,
                fence=fence,
            )
            self._write_state(self._clean_state(fence))
            return {
                "status": "recovered",
                "scenario": slug,
                "scenario_id": plan["scenario"]["id"],
                "plan_digest": digest,
                "fence": fence,
                "profiles": dirty_profiles,
                "cleanup": "verified",
                "preflight_warnings": preflight_warnings,
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--action", required=True, choices=["run", "cleanup"])
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    result = Dispatcher().dispatch(
        slug=args.scenario,
        action=args.action,
        digest=args.plan_digest,
        confirmation=args.confirm,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DispatchError as exc:
        print(f"live refused: {exc}", file=sys.stderr)
        raise SystemExit(3)
