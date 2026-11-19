#!/usr/bin/env python3
"""vm.service: an operator stops or restarts a service, edits its configuration, or swaps the WAR
(docs/spec-testbed-vm-3tier.md §6-1; also the U7 decoy deployment's path).

Every change goes the operator's way (design §6-1 "변경 원인 주입 위치"): over the executor's open
connection root becomes the operator account (setpriv, no PAM session, no log) and the operator types
`sudo systemctl stop|start|restart <unit>`, `sudo cp -p <file> <file>.bak`, `sudo cp <edited copy> <file>`,
`sudo cp <release> <webapps>/petition.war`. Those sudo lines, the unit's journal lines and the
application's own log lines are the change's trace and reach 119; the executor's own lines do not (§6-2).
No unnamed systemd-run is used (its run-r line is diverted by the first rsyslog rule, which must not
swallow a cause trace of the same form).

- stop: the unit stops; cleanup starts it again.
- restart: the unit restarts once. Nothing stays changed, so nothing is armed.
- config: one exact line of one allow-listed file becomes another line; the file is backed up next to
  itself as <file>.bak, the unit restarts. Cleanup copies the backup back and restarts.
- war: the deployed WAR is backed up to /var/lib/petition-deploy/petition.war.bak and replaced by a
  release file already on the VM whose sha256 the contract fixes. Tomcat redeploys it by itself.

Self-recovery (U8): stop, config and war arm the guard before the change; its undo (root) starts the
unit, copies the backup back or the previous WAR back, exactly when the backup is still there.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

import re
from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "vm.service"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "unit", "action", "file", "match", "replace", "release", "release_sha256"}
UNITS = {"web": {"apache2": ("/etc/apache2/",)},
         "was": {"tomcat10": ("/etc/tomcat10/", "/etc/default/tomcat10", "/var/lib/tomcat10/conf/")},
         "db": {"postgresql@16-main": ("/etc/postgresql/16/main/",)}}
WAR = "/var/lib/tomcat10/webapps/petition.war"
RELEASES = "/var/lib/petition-deploy/releases/"
LINE = re.compile(r"^[^\n\r'\\\\]{1,200}$")
PROTECTED_TEXT = ("10.250.0.", "192.168.127.", "18090", "lucida")


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    node = vm.vm_named(p, ("web", "was", "db"))
    role = vm.VMS[node]["role"]
    unit = p["unit"]
    if unit not in UNITS[role]:
        raise ExecutorError(f"unit must be one of {sorted(UNITS[role])} on a {role} VM")
    action = p["action"]
    blank = {"file": "-", "match": "-", "replace": "-", "release": "-", "release_sha256": "-"}
    if action in ("stop", "restart"):
        if any(p[k] != v for k, v in blank.items()):
            raise ExecutorError(f"{action} takes the unit only")
    elif action == "config":
        f = p["file"]
        if not isinstance(f, str) or ".." in f or not any(f == pre or (pre.endswith("/") and f.startswith(pre)) for pre in UNITS[role][unit]):
            raise ExecutorError("file is not an allow-listed configuration file of the unit")
        for key in ("match", "replace"):
            if not isinstance(p[key], str) or not LINE.fullmatch(p[key]):
                raise ExecutorError(f"{key} must be one line without quotes or backslashes")
        if any(word in p["replace"] for word in PROTECTED_TEXT):
            raise ExecutorError("replace names a monitoring or management address or port")
        if p["match"] == p["replace"] or p["release"] != "-" or p["release_sha256"] != "-":
            raise ExecutorError("config changes one line to a different line and names no release")
    elif action == "war":
        if unit != "tomcat10":
            raise ExecutorError("war swaps the Tomcat application only")
        rel = p["release"]
        if not isinstance(rel, str) or not rel.startswith(RELEASES) or ".." in rel or not rel.endswith(".war"):
            raise ExecutorError(f"release must be a .war under {RELEASES}")
        if not isinstance(p["release_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", p["release_sha256"]):
            raise ExecutorError("release_sha256 must be a sha256")
        if p["file"] != "-" or p["match"] != "-" or p["replace"] != "-":
            raise ExecutorError("war names the release only")
    else:
        raise ExecutorError("action must be stop, restart, config or war")
    return [unit, action, p["file"], p["match"], p["replace"], p["release"], p["release_sha256"]]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "service change")
    check_parameters(params)


# set -- <act> <unit> <action> <file> <match> <replace> <release> <release sha256>
NODE_BODY = vm.OPERATOR_FUNCTIONS + br'''act="$1"; svc="$2"; action="$3"; file="$4"; match="$5"; replace="$6"; release="$7"; release_sum="$8"
job="svc-${svc%%@*}"
ctl="${UNIT_SYSTEMCTL:-systemctl}"
war="${PETITION_WAR:-/var/lib/tomcat10/webapps/petition.war}"
war_bak="${PETITION_WAR_BAK:-/var/lib/petition-deploy/petition.war.bak}"
state_root="${OPSJOB_STATE_ROOT:-/var/lib/opsjobs}"
mark="$state_root/$job.stopped"
edited="$op_home/$(basename "$file").new"
svc_active() { "$ctl" is-active --quiet "$svc" </dev/null 2>/dev/null; }
state() {
  case "$action" in
    stop) if [[ -e "$mark" ]]; then echo ours; else echo default; fi ;;
    restart) echo default ;;
    config) if [[ -e "$file.bak" ]]; then echo ours; else echo default; fi ;;
    war) if [[ -e "$war_bak" ]]; then echo ours; else echo default; fi ;;
  esac
}
check() {
  [[ -n "$op_home" && -d "$op_home" ]] || { echo "no home for $operator" >&2; exit 1; }
  svc_active || { echo "$svc is not active" >&2; exit 1; }
  case "$action" in
    config) [[ -f "$file" && ! -L "$file" ]] || { echo "no $file" >&2; exit 1; }
      [[ "$(grep -cxF -- "$match" "$file")" == 1 ]] || { echo "the line to change is not in $file exactly once" >&2; exit 1; } ;;
    war) [[ -f "$release" && -f "$war" ]] || { echo "release or deployed WAR missing" >&2; exit 1; }
      [[ "$(sha256sum "$release" | cut -d' ' -f1)" == "$release_sum" ]] || { echo "release checksum differs" >&2; exit 1; } ;;
  esac
}
prepare() {
  mkdir -p "$state_root"; chmod 700 "$state_root"
  case "$action" in
    stop) boot_files=("$mark") ;;
    config) boot_files=("$edited") ;;
  esac
}
undo_body() {
  case "$action" in
    stop) printf 'if [[ -e %q ]]; then %q start %q; rm -f %q; fi\n' "$mark" "$ctl" "$svc" "$mark" ;;
    config) printf 'if [[ -e %q ]]; then cp %q %q; rm -f %q %q; %q restart %q; fi\n' "$file.bak" "$file.bak" "$file" "$file.bak" "$edited" "$ctl" "$svc" ;;
    war) printf 'if [[ -e %q ]]; then cp %q %q; rm -f %q; fi\n' "$war_bak" "$war_bak" "$war" "$war_bak" ;;
  esac
}
apply() {
  case "$action" in
    stop) : >"$mark"; as_operator "$ctl" stop "$svc" ;;
    config)
      # The operator's own edited copy in the operator's home, then copied over the file with sudo.
      awk -v m="$match" -v r="$replace" '$0 == m { print r; next } { print }' "$file" | as_operator_user tee "$edited" >/dev/null
      as_operator cp -p "$file" "$file.bak"
      as_operator cp "$edited" "$file"
      as_operator "$ctl" restart "$svc" ;;
    war)
      as_operator cp -p "$war" "$war_bak"
      as_operator cp "$release" "$war" ;;
  esac
}
remove() {
  case "$action" in
    stop) if [[ -e "$mark" ]]; then as_operator "$ctl" start "$svc"; rm -f "$mark"; fi ;;
    config) if [[ -e "$file.bak" ]]; then as_operator cp "$file.bak" "$file"; as_operator rm -f "$file.bak"; as_operator "$ctl" restart "$svc"; fi
      rm -f "$edited" ;;
    war) if [[ -e "$war_bak" ]]; then as_operator cp "$war_bak" "$war"; as_operator rm -f "$war_bak"; fi ;;
  esac
}
if [[ "$action" == restart ]]; then
  # One restart is the whole change; nothing stays changed, so nothing is armed.
  case "$act" in
    check) check ;;
    state) echo default ;;
    run) check; as_operator "$ctl" restart "$svc" ;;
    cleanup) : ;;
    *) exit 2 ;;
  esac
  exit 0
fi
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
