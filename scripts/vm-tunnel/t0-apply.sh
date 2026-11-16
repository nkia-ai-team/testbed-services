#!/usr/bin/env bash
# Puts up, checks and takes down the 119 to 66 WireGuard tunnel from the control host (testbed v4
# T0). Procedure, timing rules and what to look at: docs/runbook-testbed-tunnel.md.
#
#   t0-apply.sh probe [port...]                which UDP ports from 66 reach 119 (nothing written)
#   t0-apply.sh apply --yes [--port P] [--no-rollback]
#   t0-apply.sh verify [--target IP[:PORT]]... both ends; targets are pinged from 119 and its pod,
#                                              and with a PORT also reached over TCP from the pod
#   t0-apply.sh add-net <cidr>                 one more /24 through the tunnel (S at ST2)
#   t0-apply.sh rollback --yes [--purge]       66 first, then 119
#
# apply, add-net and rollback must run under the testbed lock: TB_LOCK_FILE names it and the
# caller holds it (flock "$TB_LOCK_FILE" t0-apply.sh ...).
#
# Connections: TB_SSH_119 and TB_SSH_66 are the command words that open a root shell on each host.
# Without them the password files TB_PASS_119 and TB_PASS_66 (default ~/.config/testbed/ssh-<ip>)
# are given to sshpass. Keys never pass through this host's output: the public keys travel in
# shell variables from one host's stdout to the other host's stdin, the private keys never leave
# their host. Never put "sshpass -p <password>" in TB_SSH_*: the value would sit in the process
# list and the shell history; use -f with a file, or keys.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOL=$HERE/tb-wg.sh
ENV_SRC=$HERE/tunnel.env
# shellcheck disable=SC1090
source "$ENV_SRC"
BIN=${TB_WG_REMOTE_BIN:-/usr/local/sbin/tb-wg}
REMOTE_ENV=${TB_WG_REMOTE_ENV:-/etc/wireguard/tb-wg.env}
RUN_ROOT=${TB_WG_RUN_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tb-wg}
HANDSHAKE_WAIT=${TB_WG_HANDSHAKE_WAIT:-90}
PROBE_SECS=${TB_WG_PROBE_SECS:-20}

die() { printf 't0-apply: %s\n' "$*" >&2; exit 1; }
kst() { TZ=Asia/Seoul date '+%F %T KST'; }
log() {
  if [[ -n "${RUN:-}" ]]; then printf '%s %s\n' "$(kst)" "$*" | tee -a "$RUN/apply.log"; else printf '%s %s\n' "$(kst)" "$*"; fi
}

ssh_words() {  # ssh_words 119|66 -> array SSH
  local var="TB_SSH_$1" host pass
  if [[ -n "${!var:-}" ]]; then read -r -a SSH <<<"${!var}"; return 0; fi
  if [[ "$1" == 119 ]]; then host=$WG_HOST_119; else host=$WG_HOST_66; fi
  pass="TB_PASS_$1"; pass=${!pass:-$HOME/.config/testbed/ssh-$host}
  [[ -r "$pass" ]] || die "no password file $pass for $host (or set TB_SSH_$1)"
  command -v sshpass >/dev/null || die "sshpass is needed (or set TB_SSH_$1)"
  SSH=(sshpass -f "$pass" ssh -o PubkeyAuthentication=no -o ConnectTimeout=10 "root@$host")
}
on() {  # on 119|66 <remote command words...>
  local SSH
  ssh_words "$1"; shift
  "${SSH[@]}" "$*"
}
# The tool without installing it: probe and snapshot write nothing on the hosts.
on_stdin_tool() {
  local h=$1; shift
  on "$h" "env WG_HOST_119=$WG_HOST_119 WG_HOST_66=$WG_HOST_66 bash -s -- $*" <"$TOOL"
}

push_tool() {
  local h=$1
  on "$h" "mkdir -p ${BIN%/*} ${REMOTE_ENV%/*} && chmod 700 ${REMOTE_ENV%/*} && cat >$BIN.new && chmod 755 $BIN.new && mv $BIN.new $BIN" <"$TOOL"
  on "$h" "cat >$REMOTE_ENV.new && chmod 600 $REMOTE_ENV.new && mv $REMOTE_ENV.new $REMOTE_ENV" <"$ENV_SRC"
}

new_run() {
  RUN=$RUN_ROOT/$(TZ=Asia/Seoul date +%Y%m%d-%H%M%S)-$1
  mkdir -p "$RUN"
  ln -sfn "$RUN" "$RUN_ROOT/latest-$1"
}

probe() {  # prints the ports that arrived, in the order given
  local ports=("$@") seen
  (( ${#ports[@]} )) || read -r -a ports <<<"$WG_PORT_CANDIDATES"
  local out
  out=$(mktemp)
  on_stdin_tool 119 "udp-listen $PROBE_SECS ${ports[*]}" >"$out" &
  local lp=$!
  sleep "${TB_WG_PROBE_LEAD:-4}"
  on_stdin_tool 66 "udp-send ${ports[*]}" >/dev/null
  wait "$lp" || true
  seen=$(sed -n 's/^seen //p' "$out")
  rm -f "$out"
  local p
  for p in "${ports[@]}"; do grep -qx "$p" <<<"$seen" && printf '%s\n' "$p"; done
  return 0
}

snap() { on_stdin_tool "$1" snapshot | LC_ALL=C sort -u; }

# Only lines about wg0 or our chains may appear, and nothing may disappear.
compare() {
  local h=$1 before=$2 after=$3 removed added bad
  removed=$(LC_ALL=C comm -23 "$before" "$after")
  added=$(LC_ALL=C comm -13 "$before" "$after")
  bad=$(grep -v -E "wg0|TBWG-" <<<"$added" | grep -v '^$' || true)
  if [[ -n "$removed" ]]; then log "[FAIL] $h: lines gone from routes and top level chains:"; printf '%s\n' "$removed" | tee -a "$RUN/apply.log"; return 1; fi
  if [[ -n "$bad" ]]; then log "[FAIL] $h: lines added that are not about wg0 or TBWG chains:"; printf '%s\n' "$bad" | tee -a "$RUN/apply.log"; return 1; fi
  log "[PASS] $h: only wg0 and TBWG lines added ($(grep -c . <<<"$added" || true)), nothing removed"
}

wait_handshake() {
  local i age
  for ((i = 0; i < HANDSHAKE_WAIT; i += 5)); do
    age=$(on 66 "$BIN handshake-age" 2>/dev/null || echo never)
    [[ "$age" =~ ^[0-9]+$ ]] && { log "handshake ${age}s ago"; return 0; }
    sleep 5
  done
  log "[FAIL] no handshake within ${HANDSHAKE_WAIT}s"
  return 1
}

# 66 (the side that dials) first, so 119 stops seeing handshakes before it goes down. Each host
# is tried even when the other cannot be reached.
do_rollback() {
  local purge=${1:-} rc=0 h
  for h in 66 119; do
    if on "$h" "test ! -x $BIN || $BIN remove $purge" 2>&1 | tee -a "$RUN/apply.log"; then :
    else log "[FAIL] could not take $h down (ssh or remove failed): run '$BIN remove' there by hand"; rc=1; fi
  done
  return "$rc"
}

# The testbed lock must be held by the caller (flock "$TB_LOCK_FILE" t0-apply.sh ...): a second
# open of the file then cannot take it.
need_lock() {
  [[ -n "${TB_LOCK_FILE:-}" ]] || die "set TB_LOCK_FILE to the testbed lock and run under it: flock \"\$TB_LOCK_FILE\" $0 $*"
  [[ -e "$TB_LOCK_FILE" ]] || die "no lock file $TB_LOCK_FILE"
  if flock -n "$TB_LOCK_FILE" true; then die "$TB_LOCK_FILE is not held: run under flock"; fi
}

# Once the hosts are touched, any way out of apply other than success rolls back: a failed check,
# a failed comparison, and also an ssh or snapshot that dies under set -e.
APPLY_STATE=idle
ROLLBACK_ON_FAIL=1
apply_exit() {
  local rc=$?
  [[ "$APPLY_STATE" == touched ]] || return 0
  trap - EXIT
  set +e
  # An exit from inside "cmd >file" would leave our output in that file: back to the terminal.
  exec 1>&7 2>&8
  if (( ROLLBACK_ON_FAIL )); then
    log "[FAIL] rolling back (files kept, the package stays)"
    do_rollback
    snap 119 >"$RUN/rolledback-119.txt"
    snap 66 >"$RUN/rolledback-66.txt"
    if cmp -s "$RUN/before-119.txt" "$RUN/rolledback-119.txt" && cmp -s "$RUN/before-66.txt" "$RUN/rolledback-66.txt"; then
      log "routes and top level chains are back to what they were"
    else
      log "[FAIL] after rollback the routes or chains differ from before: compare $RUN/before-*.txt and rolledback-*.txt"
    fi
  else
    log "[FAIL] left as it is (--no-rollback)"
  fi
  log "last change to 119 and 66: $(kst)"
  (( rc != 0 )) || rc=1
  exit "$rc"
}

apply() {
  local yes=0 port=""
  while (( $# )); do
    case "$1" in
      --yes) yes=1; shift ;;
      --port) port=${2:-}; shift 2 ;;
      --no-rollback) ROLLBACK_ON_FAIL=0; shift ;;
      *) die "apply: unknown argument $1" ;;
    esac
  done
  (( yes )) || die "apply changes 119 and 66: pass --yes after reading docs/runbook-testbed-tunnel.md"
  need_lock apply
  new_run apply
  log "run directory $RUN"
  snap 119 >"$RUN/before-119.txt"
  snap 66 >"$RUN/before-66.txt"
  on_stdin_tool 119 pod-check | tee -a "$RUN/apply.log" \
    || die "check could not find the collector pod on 119 (crictl config); nothing changed yet"

  if [[ -z "$port" ]]; then
    local passing
    passing=$(probe)
    printf '%s\n' "$passing" >"$RUN/probe.txt"
    port=$(head -1 <<<"$passing")
    [[ -n "$port" ]] || die "no candidate UDP port ($WG_PORT_CANDIDATES) reached 119 from 66: decision 8 says fall back to the product remote collection worker (spec §3-4), stop here"
    log "UDP ports that reached 119: $(paste -sd' ' <<<"$passing"); using $port"
  fi
  [[ "$port" =~ ^[0-9]+$ ]] || die "apply: bad port $port"
  printf '%s\n' "$port" >"$RUN/port"

  exec 7>&1 8>&2
  APPLY_STATE=touched
  trap apply_exit EXIT
  push_tool 119
  push_tool 66
  on 119 "$BIN install-tools 119" | tee -a "$RUN/apply.log"
  on 66 "$BIN install-tools 66" | tee -a "$RUN/apply.log"
  on 66 "$BIN pin-uplink" | tee -a "$RUN/apply.log"
  on 119 "$BIN keygen" >/dev/null
  on 66 "$BIN keygen" >/dev/null
  local pub119 pub66
  pub119=$(on 119 "$BIN pubkey")
  pub66=$(on 66 "$BIN pubkey")
  on 119 "$BIN render 119 --port $port" <<<"$pub66" | tee -a "$RUN/apply.log"
  on 66 "$BIN render 66 --port $port" <<<"$pub119" | tee -a "$RUN/apply.log"
  unset pub119 pub66

  log "starting wg-quick@${WG_IF} on 119, then on 66"
  on 119 "systemctl enable --now wg-quick@$WG_IF" 2>&1 | tee -a "$RUN/apply.log"
  on 66 "systemctl enable --now wg-quick@$WG_IF" 2>&1 | tee -a "$RUN/apply.log"
  wait_handshake || return 1
  local ok=1
  on 119 "$BIN check" | tee "$RUN/check-119.txt" | tee -a "$RUN/apply.log" || ok=0
  on 66 "$BIN check" | tee "$RUN/check-66.txt" | tee -a "$RUN/apply.log" || ok=0
  snap 119 >"$RUN/after-119.txt"
  snap 66 >"$RUN/after-66.txt"
  compare 119 "$RUN/before-119.txt" "$RUN/after-119.txt" || ok=0
  compare 66 "$RUN/before-66.txt" "$RUN/after-66.txt" || ok=0
  (( ok )) || return 1
  APPLY_STATE=done
  log "[DONE] tunnel up on port $port. Last change to 119 and 66: $(kst). Recording may start after $(TZ=Asia/Seoul date -d '+150 min' '+%F %T KST')."
}

verify() {
  local args=()
  while (( $# )); do
    case "$1" in
      --target) [[ "${2:-}" =~ ^[0-9.]+(:[0-9]+)?$ ]] || die "verify: --target needs IP or IP:TCPPORT"; args+=(--target "$2"); shift 2 ;;
      *) die "verify: unknown argument $1" ;;
    esac
  done
  new_run verify
  local ok=0
  on 119 "$BIN check ${args[*]}" | tee "$RUN/check-119.txt" || ok=1
  on 66 "$BIN check" | tee "$RUN/check-66.txt" || ok=1
  return "$ok"
}

add_net() {
  local net=${1:-}
  [[ -n "$net" ]] || die "add-net <cidr>"
  need_lock add-net
  new_run add-net
  on 119 "$BIN add-net $net" | tee -a "$RUN/apply.log"
  on 66 "$BIN add-net $net" | tee -a "$RUN/apply.log"
  log "added $net on both ends. Last change to 119 and 66: $(kst)"
}

rollback() {
  local yes=0 purge=""
  while (( $# )); do
    case "$1" in
      --yes) yes=1; shift ;;
      --purge) purge=--purge; shift ;;
      *) die "rollback: unknown argument $1" ;;
    esac
  done
  (( yes )) || die "rollback changes 119 and 66: pass --yes"
  need_lock rollback
  new_run rollback
  local ok=0 h
  do_rollback "$purge" || ok=1
  for h in 119 66; do snap "$h" >"$RUN/after-$h.txt" || { log "[FAIL] could not read $h after rollback"; ok=1; }; done
  for h in 119 66; do
    if grep -qE "wg0|TBWG-" "$RUN/after-$h.txt"; then log "[FAIL] $h still has wg0 or TBWG lines"; ok=1
    else log "[PASS] $h has no wg0 route or TBWG rule left"; fi
  done
  local before=$RUN_ROOT/latest-apply
  if [[ -r "$before/before-119.txt" ]]; then
    for h in 119 66; do
      if cmp -s "$before/before-$h.txt" "$RUN/after-$h.txt"; then log "[PASS] $h matches the snapshot taken before apply"
      else log "[NOTE] $h differs from the snapshot before apply (other changes since then?):"; diff "$before/before-$h.txt" "$RUN/after-$h.txt" || true; fi
    done
  fi
  log "last change to 119 and 66: $(kst)"
  return "$ok"
}

cmd=${1:-}; shift || true
case "$cmd" in
  probe) mkdir -p "$RUN_ROOT"; on_stdin_tool 119 pod-check >&2 || true; probe "$@" ;;
  apply) apply "$@" ;;
  verify) verify "$@" ;;
  add-net) add_net "$@" ;;
  rollback) rollback "$@" ;;
  *) sed -n '5,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
