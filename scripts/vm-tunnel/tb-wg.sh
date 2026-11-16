#!/usr/bin/env bash
# One end of the WireGuard tunnel between the observation server 119 and the hypervisor 66
# (testbed v4 T0, docs/spec-testbed-vm-3tier.md §3-4, docs/spec-testbed-sms-only.md §3-5).
#
# Why: 119 must reach the new testbed networks (SNMP polls, pings) and their agents must reach 119
# from their own addresses, without asking for office network addresses. 66 is the only tunnel
# end on the testbed side and no injection ever touches it.
#
# Installed by t0-apply.sh as /usr/local/sbin/tb-wg next to /etc/wireguard/tb-wg.env. Runs as root.
#   tb-wg install-tools <119|66>       wireguard-tools (119 only installs it, 66 already has it)
#   tb-wg keygen                       private key in /etc/wireguard/wg0.key (0600), never printed
#   tb-wg pubkey                       the public key, for the orchestrator to pass to the peer
#   tb-wg render <119|66> --port P     reads the peer public key on stdin, writes wg0.conf (0600)
#   tb-wg hook-up | hook-down          firewall rules of this end (wg-quick PostUp, PreDown)
#   tb-wg add-net <cidr> | del-net <cidr>
#                                      route one more /24 through the tunnel (S at ST2), live
#   tb-wg check [--target IP[:PORT]]...
#                                      handshake, ping, routes, guards, conntrack; rc 1 on FAIL
#   tb-wg handshake-age                seconds since the latest handshake, or "never"
#   tb-wg pod-check                    (119, read only) can check enter the collector pod
#   tb-wg snapshot                     top level routes and rules, to compare before and after
#   tb-wg remove [--purge]             stop and disable; --purge also deletes files and the
#                                      package if this tool installed it
#   tb-wg udp-listen <secs> <ports..>  (119) which probe ports from 66 arrive
#   tb-wg udp-send <ports..>           (66) small probe datagrams to 119
#
# The private key is set by "wg set ... private-key <file>" in PostUp, so wg0.conf holds no
# key. Rules live in chains of their own in the mangle table and the jumps match wg0 only: nothing
# else on the host changes (k3s chains on 119, the tb-nat rules on 66, default policies on both).
set -euo pipefail

ROOT=${TB_WG_ROOT:-}
WG_DIR=$ROOT/etc/wireguard
ENV_FILE=${TB_WG_ENV:-$WG_DIR/tb-wg.env}
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE"
: "${WG_IF:=wg0}" "${WG_MTU:=1420}" "${WG_PREFIX:=30}" "${WG_ADDR_119:=10.250.0.1}"
: "${WG_ADDR_66:=10.250.0.2}" "${WG_HOST_119:=192.168.230.119}" "${WG_HOST_66:=192.168.200.66}"
: "${WG_KEEPALIVE:=25}" "${WG_NETS_DEFAULT:=}" "${WG_DENY_NETS:=}"
: "${WG_IN_TCP_119:=18090 514}" "${WG_IN_UDP_119:=514 162}" "${WG_HANDSHAKE_MAX:=150}"
: "${WG_RTT_EXTRA_MS:=2}" "${WG_RTT_MAX_MS:=20}" "${WG_CONNTRACK_MAX_RATIO:=80}"
: "${WG_POD_NAME:=collector-nms}" "${WG_POD_TCP_PORT:=9}"

SELF=$ROOT/usr/local/sbin/tb-wg
CONF=$WG_DIR/$WG_IF.conf
KEY=$WG_DIR/$WG_IF.key
PUB=$WG_DIR/$WG_IF.pub
PEER=$WG_DIR/$WG_IF.peer
NETS=$WG_DIR/$WG_IF.nets
ROLE_FILE=$WG_DIR/tb-wg.role
STATE_DIR=$ROOT/var/lib/tb-wg
PKG_MARK=$STATE_DIR/installed-wireguard-tools
UNIT=wg-quick@$WG_IF
CHAIN_IN=TBWG-IN      # 119, mangle: what the tunnel may open on 119
CHAIN_FWD=TBWG-FWD    # 66, mangle: which networks may cross between wg0 and the testbed
CONNTRACK_DIR=${TB_WG_CONNTRACK_DIR:-/proc/sys/net/netfilter}

die() { printf 'tb-wg: %s\n' "$*" >&2; exit 1; }
say() { printf 'tb-wg: %s\n' "$*"; }
pass() { printf '[PASS] %s\n' "$*"; }
FAILS=0
flunk() { printf '[FAIL] %s\n' "$*"; FAILS=$((FAILS + 1)); }

role_arg() {
  case "${1:-}" in 119|66) printf '%s' "$1" ;; *) die "role must be 119 or 66, got '${1:-}'" ;; esac
}
role_saved() {
  [[ -r "$ROLE_FILE" ]] || die "no $ROLE_FILE: run render first"
  role_arg "$(<"$ROLE_FILE")"
}
my_addr() { if [[ "$1" == 119 ]]; then printf '%s' "$WG_ADDR_119"; else printf '%s' "$WG_ADDR_66"; fi; }
peer_addr() { if [[ "$1" == 119 ]]; then printf '%s' "$WG_ADDR_66"; else printf '%s' "$WG_ADDR_119"; fi; }
peer_host() { if [[ "$1" == 119 ]]; then printf '%s' "$WG_HOST_66"; else printf '%s' "$WG_HOST_119"; fi; }

# A tunnel network is one /24 inside 192.168.0.0/16 that is not on the deny list. Bundled ranges
# are refused on purpose: 123~126 do not sit on a /22 boundary, and a /22 would take in 122.
valid_net() {
  local net=$1 o
  [[ "$net" =~ ^192\.168\.([0-9]{1,3})\.0/24$ ]] || return 1
  o=${BASH_REMATCH[1]}
  [[ "$o" == 0 || "$o" != 0* ]] || return 1
  (( o <= 255 )) || return 1
  local d
  for d in $WG_DENY_NETS; do [[ "$net" == "$d" ]] && return 1; done
  return 0
}

nets() {
  if [[ -s "$NETS" ]]; then grep -v '^[[:space:]]*$' "$NETS"; else printf '%s\n' $WG_NETS_DEFAULT; fi
}
nets_init() {
  [[ -s "$NETS" ]] && return 0
  local n
  for n in $WG_NETS_DEFAULT; do valid_net "$n" || die "default network $n is not allowed"; done
  printf '%s\n' $WG_NETS_DEFAULT >"$NETS.tmp"
  mv "$NETS.tmp" "$NETS"
}
allowed_ips() {  # what this end accepts from the peer
  if [[ "$1" == 119 ]]; then
    { printf '%s/32\n' "$WG_ADDR_66"; nets; } | paste -sd, - | sed 's/,/, /g'
  else
    printf '%s/32' "$WG_ADDR_119"
  fi
}

ipt() { iptables -w "$@"; }
jump_add() {
  local table=$1 chain=$2; shift 2
  ipt -t "$table" -C "$chain" "$@" 2>/dev/null || ipt -t "$table" -I "$chain" 1 "$@"
}
jump_del() {
  local table=$1 chain=$2; shift 2
  while ipt -t "$table" -C "$chain" "$@" 2>/dev/null; do ipt -t "$table" -D "$chain" "$@"; done
}
chain_drop() {
  if ipt -t "$1" -S "$2" >/dev/null 2>&1; then ipt -t "$1" -F "$2"; ipt -t "$1" -X "$2"; fi
}
MSS=(-o "$WG_IF" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu)

# The guards sit in the mangle table, which k3s, kube-proxy, kube-router, flannel and docker leave
# alone on both hosts. In the filter table kube-proxy prepends its own jumps whenever it starts
# after wg-quick (every boot), and KUBE-FORWARD accepts NodePort traffic before a guard placed
# there is reached. A DROP in mangle is final. 119: PREROUTING sees every packet from wg0 before
# DNAT, so the ports are the ones dialled on 10.250.0.1. 66: FORWARD both ways.
GUARD_TABLE=mangle
guard_chain() { if [[ "$1" == 119 ]]; then printf '%s' "$CHAIN_IN"; else printf '%s' "$CHAIN_FWD"; fi; }
guard_hooks() {  # one "<hook chain> <match words>" per line
  if [[ "$1" == 119 ]]; then printf 'PREROUTING -i %s\n' "$WG_IF"
  else printf 'FORWARD -i %s\nFORWARD -o %s\n' "$WG_IF" "$WG_IF"; fi
}
guard_fill() {  # guard_fill <role> <chain>
  local role=$1 c=$2 p n
  if [[ "$role" == 119 ]]; then
    ipt -t "$GUARD_TABLE" -A "$c" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    ipt -t "$GUARD_TABLE" -A "$c" -p icmp --icmp-type echo-request -d "$WG_ADDR_119" -j RETURN
    for p in $WG_IN_TCP_119; do ipt -t "$GUARD_TABLE" -A "$c" -p tcp -d "$WG_ADDR_119" --dport "$p" -j RETURN; done
    for p in $WG_IN_UDP_119; do ipt -t "$GUARD_TABLE" -A "$c" -p udp -d "$WG_ADDR_119" --dport "$p" -j RETURN; done
  else
    while read -r n; do
      valid_net "$n" || die "network $n in $NETS is not allowed"
      ipt -t "$GUARD_TABLE" -A "$c" -i "$WG_IF" -s "$WG_ADDR_119/32" -d "$n" -j RETURN
      ipt -t "$GUARD_TABLE" -A "$c" -o "$WG_IF" -s "$n" -d "$WG_ADDR_119/32" -j RETURN
    done < <(nets)
  fi
  ipt -t "$GUARD_TABLE" -A "$c" -j DROP
}
# A new chain is filled first, its jumps go in above the old ones, then the old chain goes and the
# new one takes its name (jumps follow a renamed chain). No packet ever sees a half filled guard.
guard_install() {
  local role=$1 chain new hook m
  chain=$(guard_chain "$role"); new=$chain-NEW
  while read -r hook m; do jump_del "$GUARD_TABLE" "$hook" $m -j "$new"; done < <(guard_hooks "$role")
  chain_drop "$GUARD_TABLE" "$new"
  ipt -t "$GUARD_TABLE" -N "$new"
  guard_fill "$role" "$new"
  while read -r hook m; do jump_add "$GUARD_TABLE" "$hook" $m -j "$new"; done < <(guard_hooks "$role")
  while read -r hook m; do jump_del "$GUARD_TABLE" "$hook" $m -j "$chain"; done < <(guard_hooks "$role")
  chain_drop "$GUARD_TABLE" "$chain"
  ipt -t "$GUARD_TABLE" -E "$new" "$chain"
}
guard_remove() {
  local role=$1 chain c hook m
  chain=$(guard_chain "$role")
  for c in "$chain" "$chain-NEW"; do
    while read -r hook m; do jump_del "$GUARD_TABLE" "$hook" $m -j "$c"; done < <(guard_hooks "$role")
    chain_drop "$GUARD_TABLE" "$c"
  done
}

hook_up() {
  local role
  role=$(role_saved)
  guard_install "$role"
  ipt -t mangle -C FORWARD "${MSS[@]}" 2>/dev/null || ipt -t mangle -A FORWARD "${MSS[@]}"
}

hook_down() {
  local role
  role=$(role_saved)
  guard_remove "$role"
  while ipt -t mangle -C FORWARD "${MSS[@]}" 2>/dev/null; do ipt -t mangle -D FORWARD "${MSS[@]}"; done
}

pkg_installed() {
  [[ "$(dpkg-query -W -f='${Status}' wireguard-tools 2>/dev/null || true)" == "install ok installed" ]]
}
install_tools() {
  local role
  role=$(role_arg "${1:-}")
  modinfo -F filename wireguard >/dev/null 2>&1 || die "kernel has no wireguard module"
  if pkg_installed; then say "wireguard-tools already installed"; return 0; fi
  [[ "$role" == 119 ]] || die "wireguard-tools missing on 66; it was there on 11-09, look before installing"
  # 119 has services and a kernel waiting for a restart (needrestart): the apt hook must not
  # restart them (docker would, for one, re-order the filter table).
  NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends wireguard-tools
  pkg_installed || die "wireguard-tools did not install"
  mkdir -p "$STATE_DIR"
  : >"$PKG_MARK"
  say "installed wireguard-tools (marked for removal on purge)"
}

keygen() {
  mkdir -p "$WG_DIR"
  chmod 700 "$WG_DIR"
  if [[ ! -s "$KEY" ]]; then
    (umask 077 && wg genkey >"$KEY.tmp" && mv "$KEY.tmp" "$KEY")
  fi
  chmod 600 "$KEY"
  wg pubkey <"$KEY" >"$PUB.tmp"
  chmod 644 "$PUB.tmp"
  mv "$PUB.tmp" "$PUB"
  say "key ready"
}

valid_key() { [[ "$1" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$ ]]; }

conf_write() {
  local role=$1 port=$2 peer
  peer=$(<"$PEER")
  {
    printf '# Written by tb-wg render (testbed v4 T0). No private key here: PostUp sets it from %s.\n' "$KEY"
    printf '[Interface]\n'
    printf 'Address = %s/%s\n' "$(my_addr "$role")" "$WG_PREFIX"
    [[ "$role" == 119 ]] && printf 'ListenPort = %s\n' "$port"
    printf 'MTU = %s\n' "$WG_MTU"
    printf 'PostUp = wg set %%i private-key %s\n' "$KEY"
    printf 'PostUp = %s hook-up\n' "$SELF"
    printf 'PreDown = %s hook-down\n' "$SELF"
    printf '\n[Peer]\n'
    printf 'PublicKey = %s\n' "$peer"
    if [[ "$role" == 66 ]]; then
      printf 'Endpoint = %s:%s\n' "$WG_HOST_119" "$port"
      printf 'PersistentKeepalive = %s\n' "$WG_KEEPALIVE"
    fi
    printf 'AllowedIPs = %s\n' "$(allowed_ips "$role")"
  } >"$CONF.tmp"
  chmod 600 "$CONF.tmp"
  mv "$CONF.tmp" "$CONF"
}
conf_port() {
  local p
  if [[ -r "$CONF" ]]; then
    p=$(sed -n -e 's/^ListenPort = \([0-9]*\)$/\1/p' -e 's/^Endpoint = .*:\([0-9]*\)$/\1/p' "$CONF" | head -1)
    [[ -n "$p" ]] && { printf '%s' "$p"; return 0; }
  fi
  return 1
}

render() {
  local role port=""
  role=$(role_arg "${1:-}"); shift || true
  while (( $# )); do
    case "$1" in
      --port) port=${2:-}; shift 2 ;;
      *) die "render: unknown argument $1" ;;
    esac
  done
  [[ "$port" =~ ^[0-9]+$ ]] && (( port > 0 && port < 65536 )) || die "render: --port must be a UDP port"
  local peer
  IFS= read -r peer || true
  valid_key "$peer" || die "render: the peer public key on stdin is not a WireGuard key"
  [[ -s "$KEY" ]] || die "render: run keygen first"
  mkdir -p "$WG_DIR"
  chmod 700 "$WG_DIR"
  (umask 077 && printf '%s\n' "$peer" >"$PEER")
  printf '%s\n' "$role" >"$ROLE_FILE"
  nets_init
  conf_write "$role" "$port"
  say "wrote $CONF for $role, port $port"
}

link_up() { ip link show "$WG_IF" >/dev/null 2>&1; }

nets_apply_live() {
  local role=$1
  link_up || return 0
  if [[ "$role" == 119 ]]; then
    wg set "$WG_IF" peer "$(<"$PEER")" allowed-ips "$(allowed_ips 119 | sed 's/, /,/g')"
  else
    hook_up
  fi
}

add_net() {
  local net=${1:-} role port
  valid_net "$net" || die "add-net: $net is not a /24 that may ride the tunnel"
  role=$(role_saved)
  port=$(conf_port) || die "add-net: no port in $CONF"
  nets_init
  if grep -qxF "$net" "$NETS"; then say "$net already routed"; return 0; fi
  printf '%s\n' "$net" >>"$NETS"
  conf_write "$role" "$port"
  nets_apply_live "$role"
  if [[ "$role" == 119 ]] && link_up; then ip route replace "$net" dev "$WG_IF"; fi
  say "added $net"
}

del_net() {
  local net=${1:-} role port
  [[ -n "$net" ]] || die "del-net: which network"
  role=$(role_saved)
  port=$(conf_port) || die "del-net: no port in $CONF"
  nets_init
  grep -qxF "$net" "$NETS" || { say "$net is not routed"; return 0; }
  grep -vxF "$net" "$NETS" >"$NETS.tmp" || true
  [[ -s "$NETS.tmp" ]] || { rm -f "$NETS.tmp"; die "del-net: refusing to leave the tunnel with no network"; }
  mv "$NETS.tmp" "$NETS"
  conf_write "$role" "$port"
  nets_apply_live "$role"
  if [[ "$role" == 119 ]] && link_up; then ip route del "$net" dev "$WG_IF" 2>/dev/null || true; fi
  say "removed $net"
}

# ping summary: prints "<loss%> <avg ms> <max ms>", or "100 - -" when nothing came back.
ping_stats() {
  local out loss avg max
  out=$("$@" 2>/dev/null || true)
  loss=$(sed -n 's/.* \([0-9.]*\)% packet loss.*/\1/p' <<<"$out" | head -1)
  read -r avg max < <(sed -n 's#.* = [0-9.]*/\([0-9.]*\)/\([0-9.]*\)/.*#\1 \2#p' <<<"$out" | head -1) || true
  printf '%s %s %s\n' "${loss:-100}" "${avg:--}" "${max:--}"
}
le() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 <= b + 0) }'; }
plus() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.3f", a + b }'; }

# crictl needs the k3s config file; k3s writes it under its data dir, which 119 moved with
# --data-dir. Order: WG_CRICTL_CONFIG, the --data-dir of the running k3s unit, data-dir in
# /etc/rancher/k3s/config.yaml, the k3s default.
crictl_config() {
  if [[ -n "${WG_CRICTL_CONFIG:-}" ]]; then printf '%s' "$WG_CRICTL_CONFIG"; return 0; fi
  local dd
  dd=$(systemctl show -p ExecStart --value k3s 2>/dev/null | tr ' ;' '\n\n' \
    | awk 'p {print; exit} /^--data-dir=/ {sub(/^--data-dir=/, ""); print; exit} $0 == "--data-dir" {p = 1}' || true)
  if [[ -z "$dd" && -r "$ROOT/etc/rancher/k3s/config.yaml" ]]; then
    dd=$(sed -n "s/^data-dir:[[:space:]]*[\"']\{0,1\}\([^\"' ]*\).*/\1/p" "$ROOT/etc/rancher/k3s/config.yaml" | head -1)
  fi
  printf '%s/agent/etc/crictl.yaml' "${dd:-$ROOT/var/lib/rancher/k3s}"
}
# Pid of a container of the named pod, to enter its network namespace (119 only).
pod_pid() {
  local cfg id
  cfg=$(crictl_config)
  id=$(crictl --config "$cfg" ps --name "$WG_POD_NAME" --state running -q 2>/dev/null | head -1 || true)
  [[ -n "$id" ]] || return 1
  crictl --config "$cfg" inspect --output go-template --template '{{.info.pid}}' "$id" 2>/dev/null
}
# Read only, for probe: can check find the pod before anything is changed?
pod_check() {
  local pid cfg
  cfg=$(crictl_config)
  if pid=$(pod_pid) && [[ "$pid" =~ ^[0-9]+$ ]]; then pass "found the $WG_POD_NAME container with $cfg"; return 0; fi
  flunk "no running $WG_POD_NAME container through $cfg"
  return 1
}
# TCP reach from inside a namespace: connected or refused both prove the path; a timeout does not.
tcp_reach() {
  local pid=$1 ip=$2 port=$3 out rc
  out=$(nsenter -t "$pid" -n timeout 3 bash -c "exec 3<>/dev/tcp/$ip/$port" 2>&1) && rc=0 || rc=$?
  if (( rc == 0 )); then printf 'connected'; return 0; fi
  if [[ "$out" == *"refused"* ]]; then printf 'refused'; return 0; fi
  printf 'no answer (rc %s)' "$rc"; return 1
}

# The guard works only where nothing above it can accept the packet: its jumps must be the first
# rules of their hook chains, and the chain must end in DROP.
guard_check() {
  local role=$1 chain hook m want got first
  chain=$(guard_chain "$role")
  local ok=1 hooks=""
  while read -r hook m; do
    hooks+="${hooks:+, }$hook $m"
    want="-A $hook $m -j $chain"
    first=$(ipt -t "$GUARD_TABLE" -S "$hook" 2>/dev/null | grep '^-A ' | head -"$(guard_hooks "$role" | grep -c "^$hook ")" || true)
    grep -qxF -- "$want" <<<"$first" || { ok=0; flunk "$chain: '$want' is not at the top of $GUARD_TABLE $hook (got: $(paste -sd'|' <<<"$first"))"; }
  done < <(guard_hooks "$role")
  got=$(ipt -t "$GUARD_TABLE" -S "$chain" 2>/dev/null | tail -1 || true)
  [[ "$got" == "-A $chain -j DROP" ]] || { ok=0; flunk "$chain does not end in DROP (last: ${got:-no chain})"; }
  (( ok )) && pass "$chain first in $GUARD_TABLE ($hooks), last rule DROP"
  return 0
}

handshake_age() {
  local hs
  hs=$(wg show "$WG_IF" latest-handshakes 2>/dev/null | awk 'NR == 1 {print $2}' || true)
  if [[ "${hs:-0}" =~ ^[0-9]+$ ]] && (( ${hs:-0} > 0 )); then echo $(( $(date +%s) - hs )); else echo never; fi
}

check() {
  local targets=() role
  while (( $# )); do
    case "$1" in
      --target)
        [[ "${2:-}" =~ ^[0-9.]+(:[0-9]+)?$ ]] || die "check: --target needs IP or IP:TCPPORT"
        targets+=("$2"); shift 2 ;;
      *) die "check: unknown argument $1" ;;
    esac
  done
  role=$(role_saved)
  local peer lan
  peer=$(peer_addr "$role"); lan=$(peer_host "$role")

  if link_up; then pass "$WG_IF exists"; else flunk "$WG_IF missing"; fi
  if [[ "$(systemctl is-enabled "$UNIT" 2>/dev/null || true)" == enabled ]]; then pass "$UNIT enabled (comes back after reboot)"
  else flunk "$UNIT not enabled"; fi
  if systemctl is-active --quiet "$UNIT"; then pass "$UNIT active"; else flunk "$UNIT not active"; fi

  local age
  age=$(handshake_age)
  if [[ "$age" =~ ^[0-9]+$ ]]; then
    if (( age <= WG_HANDSHAKE_MAX )); then pass "latest handshake ${age}s ago (limit ${WG_HANDSHAKE_MAX}s)"
    else flunk "latest handshake ${age}s ago (limit ${WG_HANDSHAKE_MAX}s)"; fi
  else
    flunk "no handshake yet"
  fi

  # Three rounds, office path and tunnel back to back: a one-off wobble must not fail the check
  # (apply rolls back on a failed check). Loss in any round fails.
  local base_loss base_avg base_max loss avg max round over=0 lost=0 detail=""
  for round in 1 2 3; do
    read -r base_loss base_avg base_max < <(ping_stats ping -n -q -c 10 -i 0.2 -W 2 "$lan")
    read -r loss avg max < <(ping_stats ping -n -q -c 10 -i 0.2 -W 2 "$peer")
    if [[ "$loss" != 0 || "$avg" == - ]]; then lost=1; detail="loss ${loss}%"; break; fi
    detail="avg ${avg}ms, max ${max}ms against office path avg ${base_avg}ms + ${WG_RTT_EXTRA_MS}ms, max ${WG_RTT_MAX_MS}ms"
    if [[ "$base_avg" == - ]] || ! le "$avg" "$(plus "$base_avg" "$WG_RTT_EXTRA_MS")" || ! le "$max" "$WG_RTT_MAX_MS"; then
      over=$((over + 1))
    else
      break
    fi
  done
  if (( lost )); then flunk "ping $peer through $WG_IF: $detail"
  elif (( over == 3 )); then flunk "ping $peer through $WG_IF: $detail, 3 rounds out of 3"
  else pass "ping $peer through $WG_IF: loss 0%, $detail (over in $over of the rounds taken)"; fi

  local n
  if [[ "$role" == 119 ]]; then
    local routes
    routes=$(ip -4 route show dev "$WG_IF" 2>/dev/null || true)
    while read -r n; do
      if grep -qE "^${n//./\\.}( |\$)" <<<"$routes"; then pass "route $n dev $WG_IF"; else flunk "route $n dev $WG_IF missing"; fi
    done < <(nets)
  else
    while read -r n; do
      if ipt -t "$GUARD_TABLE" -C "$CHAIN_FWD" -i "$WG_IF" -s "$WG_ADDR_119/32" -d "$n" -j RETURN 2>/dev/null \
        && ipt -t "$GUARD_TABLE" -C "$CHAIN_FWD" -o "$WG_IF" -s "$n" -d "$WG_ADDR_119/32" -j RETURN 2>/dev/null; then
        pass "$CHAIN_FWD lets $n and $WG_ADDR_119 through"
      else
        flunk "$CHAIN_FWD has no rule pair for $n"
      fi
    done < <(nets)
  fi
  guard_check "$role"
  if ipt -t mangle -C FORWARD "${MSS[@]}" 2>/dev/null; then pass "TCP MSS clamped on $WG_IF"; else flunk "TCP MSS clamp missing"; fi

  local count max_ct
  count=$(cat "$CONNTRACK_DIR/nf_conntrack_count" 2>/dev/null || echo -)
  max_ct=$(cat "$CONNTRACK_DIR/nf_conntrack_max" 2>/dev/null || echo -)
  if [[ "$count" =~ ^[0-9]+$ && "$max_ct" =~ ^[0-9]+$ ]] && (( count * 100 <= max_ct * WG_CONNTRACK_MAX_RATIO )); then
    pass "conntrack $count of $max_ct (limit ${WG_CONNTRACK_MAX_RATIO}%)"
  else
    flunk "conntrack $count of $max_ct (limit ${WG_CONNTRACK_MAX_RATIO}%)"
  fi

  # Targets are IP or IP:PORT. The TCP probe needs a port the target answers (a refusal counts),
  # so it runs only when one is given; the tunnel peer itself is probed on WG_POD_TCP_PORT.
  local t ip port
  for t in "${targets[@]}"; do
    ip=${t%%:*}
    read -r loss avg max < <(ping_stats ping -n -q -c 5 -i 0.2 -W 2 "$ip")
    if [[ "$loss" == 0 ]]; then pass "ping $ip from this host: loss 0%, avg ${avg}ms"; else flunk "ping $ip from this host: loss ${loss}%"; fi
  done

  if [[ "$role" == 119 ]]; then
    local pid r
    if pid=$(pod_pid) && [[ "$pid" =~ ^[0-9]+$ ]]; then
      for t in "$peer:$WG_POD_TCP_PORT" "${targets[@]}"; do
        ip=${t%%:*}; port=""; [[ "$t" == *:* ]] && port=${t##*:}
        read -r loss avg max < <(ping_stats nsenter -t "$pid" -n ping -n -q -c 5 -i 0.2 -W 2 "$ip")
        if [[ "$loss" == 0 ]]; then pass "ping $ip from the $WG_POD_NAME pod: loss 0%, avg ${avg}ms"
        else flunk "ping $ip from the $WG_POD_NAME pod: loss ${loss}%"; fi
        [[ -n "$port" ]] || continue
        if r=$(tcp_reach "$pid" "$ip" "$port"); then pass "tcp $ip:$port from the $WG_POD_NAME pod: $r"
        else flunk "tcp $ip:$port from the $WG_POD_NAME pod: $r"; fi
      done
    else
      flunk "no running $WG_POD_NAME container through $(crictl_config), so the collector side was not tested"
    fi
  fi
  (( FAILS == 0 )) || { say "$FAILS check(s) failed"; return 1; }
  say "all checks passed"
}

# Top level routes and rules only: the chains k3s rewrites all the time are left out, our own
# chains are listed in full. t0-apply.sh compares the before and after lists.
snapshot() {
  ip -4 rule show | sed 's/^/rule /'
  ip -4 route show table main | sed 's/^/route /'
  local c
  for c in INPUT FORWARD; do ipt -S "$c" | sed 's/^/filter /'; done
  for c in PREROUTING POSTROUTING; do ipt -t nat -S "$c" | sed 's/^/nat /'; done
  for c in PREROUTING FORWARD; do ipt -t mangle -S "$c" | sed 's/^/mangle /'; done
  for c in "$CHAIN_IN" "$CHAIN_FWD" "$CHAIN_IN-NEW" "$CHAIN_FWD-NEW"; do
    ipt -t "$GUARD_TABLE" -S "$c" 2>/dev/null | sed "s/^/$GUARD_TABLE /" || true
  done
}

remove() {
  local purge=0
  [[ "${1:-}" == --purge ]] && purge=1
  if systemctl is-active --quiet "$UNIT" || systemctl is-enabled --quiet "$UNIT" 2>/dev/null; then
    systemctl disable --now "$UNIT"
  fi
  if link_up; then ip link del "$WG_IF"; fi
  [[ -r "$ROLE_FILE" ]] && hook_down
  if (( purge )); then
    rm -f "$CONF" "$KEY" "$PUB" "$PEER" "$NETS" "$ROLE_FILE"
    if [[ -e "$PKG_MARK" ]]; then
      NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get remove -y wireguard-tools
      rm -f "$PKG_MARK"
    fi
    rmdir "$STATE_DIR" 2>/dev/null || true
    rm -f "$WG_DIR/tb-wg.env" "$SELF"
    say "removed the tunnel, its files and the tool"
  else
    say "tunnel stopped and disabled; files kept for a later start"
  fi
}

udp_listen() {
  local secs=${1:-}; shift || true
  [[ "$secs" =~ ^[0-9]+$ ]] && (( $# )) || die "udp-listen <seconds> <port>..."
  local dev filter="" p
  dev=$(ip -4 route get "$WG_HOST_66" | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
  [[ -n "$dev" ]] || die "udp-listen: no route to $WG_HOST_66"
  for p in "$@"; do
    [[ "$p" =~ ^[0-9]+$ ]] || die "udp-listen: bad port $p"
    filter+="${filter:+ or }dst port $p"
  done
  # -p: no promiscuous mode, so the kernel writes no "entered promiscuous mode" line.
  { timeout "$secs" tcpdump -p -n -l -i "$dev" "udp and src host $WG_HOST_66 and ($filter)" 2>/dev/null || true; } \
    | sed -n 's/.* > [0-9.]*\.\([0-9]*\): .*/\1/p' | sort -un | sed 's/^/seen /'
}

udp_send() {
  (( $# )) || die "udp-send <port>..."
  local p i
  for p in "$@"; do
    [[ "$p" =~ ^[0-9]+$ ]] || die "udp-send: bad port $p"
    for i in 1 2 3; do printf 'tb-wg-probe' >"/dev/udp/$WG_HOST_119/$p" || true; sleep 0.2; done
  done
  say "sent 3 datagrams to each of: $*"
}

cmd=${1:-}; shift || true
case "$cmd" in
  install-tools) install_tools "$@" ;;
  keygen) keygen ;;
  pubkey) [[ -s "$PUB" ]] || die "no public key: run keygen"; cat "$PUB" ;;
  render) render "$@" ;;
  hook-up) hook_up ;;
  hook-down) hook_down ;;
  add-net) add_net "$@" ;;
  del-net) del_net "$@" ;;
  check) check "$@" ;;
  handshake-age) handshake_age ;;
  pod-check) pod_check ;;
  snapshot) snapshot ;;
  remove) remove "$@" ;;
  udp-listen) udp_listen "$@" ;;
  udp-send) udp_send "$@" ;;
  *) sed -n '9,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
