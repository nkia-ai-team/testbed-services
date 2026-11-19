#!/usr/bin/env python3
"""vmhost.job: an operator's heavy job on one server VM (docs/spec-testbed-vm-3tier.md §6-1).

The VM counterpart of host.job: a named transient unit (the contract's unit name and description, an
operator's plain words such as a nightly archive) compresses a directory into an archive file with
tar and gzip at a nice level, bounded by a run time. Its start and stop lines and its processes reach
119 as a real job's would; the executor's own connection, sudo and guard lines do not (§6-2).

Self-recovery (U8): armed before the unit starts, with an undo that stops exactly this unit and
removes its archive; the archive is listed for removal at the next boot.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "vmhost.job"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "unit", "description", "source_dir", "output_dir", "level", "nice", "runtime_seconds", "min_free_mib"}


def _path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or ".." in value or any(c.isspace() for c in value):
        raise ExecutorError(f"{field} must be an absolute path without spaces")
    return value


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    vm.vm_named(p, ("web", "was", "db"))
    unit = vm.neutral_name(p["unit"], "unit")
    description = vm.description(p["description"])
    return [unit, description, _path(p["source_dir"], "source_dir"), _path(p["output_dir"], "output_dir"),
            str(vm.integer(p["level"], "level", 1, 9)), str(vm.integer(p["nice"], "nice", 0, 19)),
            str(vm.integer(p["runtime_seconds"], "runtime_seconds", 60, 1800)),
            str(vm.integer(p["min_free_mib"], "min_free_mib", 1024, 1_048_576))]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "server job")
    check_parameters(params)


# set -- <act> <unit> <description> <source dir> <output dir> <level> <nice> <runtime> <min free MiB>
NODE_BODY = vm.UNIT_FUNCTIONS + br'''act="$1"; unit="$2"; description="$3"; src="$4"; out="$5"; level="$6"; nice="$7"; runtime="$8"; min_free="$9"
job="$unit"
archive="$out/$unit.tar.gz"
boot_files=("$archive")
state() {
  if unit_active || [[ -e "$archive" ]]; then echo ours; return 0; fi
  echo default
}
check() {
  command -v tar >/dev/null; command -v gzip >/dev/null
  [[ -d "$src" && ! -L "$src" && -d "$out" && ! -L "$out" ]] || { echo "source or output directory missing" >&2; exit 1; }
  (( $(df -Pk "$out" | awk 'NR==2{print int($4/1024)}') > min_free )) || { echo "less than $min_free MiB free under $out" >&2; exit 1; }
  if unit_active; then echo "$unit is already running" >&2; exit 1; fi
}
undo_body() { unit_undo; printf 'rm -f %q\n' "$archive"; }
apply() {
  unit_start --property=Nice="$nice" --property=IOSchedulingClass=best-effort \
    /usr/bin/timeout "$runtime" /bin/sh -c 'tar -C "$1" -cf - . | gzip -"$2" >"$3"' archive "$src" "$level" "$archive"
  # A small source can be archived before the first look; the archive then stands for the job.
  local _
  for _ in $(seq 1 25); do if unit_active || [[ -s "$archive" ]]; then return 0; fi; sleep 0.2; done
  unit_active || [[ -s "$archive" ]]
}
remove() { unit_stop; rm -f -- "$archive"; }
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    return vm.vm_invocation(plan, action, p, NODE_BODY, args)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
