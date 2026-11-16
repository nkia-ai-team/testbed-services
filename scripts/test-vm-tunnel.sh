#!/usr/bin/env bash
# Side-effect-free tests for vm-tunnel/tb-wg.sh and vm-tunnel/t0-apply.sh. Every host tool the two
# scripts call (iptables, ip, wg, systemctl, dpkg-query, apt-get, modinfo, ping, crictl, nsenter,
# tcpdump) is a fake on the front of PATH that keeps its state in files under a temporary tree, so
# nothing touches the network stack of the machine running the tests. The fake systemctl plays
# wg-quick: it creates the link, adds the routes of AllowedIPs and runs the PostUp and PreDown
# lines of the written wg0.conf, so the real hook code runs against the fake iptables.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOL="$SCRIPT_DIR/vm-tunnel/tb-wg.sh"
ORCH="$SCRIPT_DIR/vm-tunnel/t0-apply.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }
ok() { printf '[ok] %s\n' "$*"; }
command -v python3 >/dev/null || fail "python3 is needed for the fake key derivation"

SHIM="$TMP_ROOT/bin"
mkdir -p "$SHIM"

cat >"$SHIM/iptables" <<'SH'
#!/usr/bin/env bash
table=filter; args=()
while (( $# )); do case "$1" in -w) shift ;; -t) table=$2; shift 2 ;; *) args+=("$1"); shift ;; esac; done
set -- "${args[@]}"
d=$FAKE/ipt/$table; mkdir -p "$d"
printf 'iptables -t %s %s\n' "$table" "$*" >>"$FAKE/calls"
op=$1; chain=${2:-}; shift 2 2>/dev/null || shift $#
spec="$*"
builtin_chain() { case "$1" in INPUT|FORWARD|OUTPUT|PREROUTING|POSTROUTING) return 0 ;; esac; return 1; }
case "$op" in
  -N) [[ -e "$d/$chain" ]] && exit 1; : >"$d/$chain" ;;
  -F) [[ -e "$d/$chain" ]] || exit 1; : >"$d/$chain" ;;
  -X) [[ -e "$d/$chain" ]] || exit 1; [[ -s "$d/$chain" ]] && exit 1; rm "$d/$chain" ;;
  -C) [[ -e "$d/$chain" ]] || exit 2; grep -qxF -- "$spec" "$d/$chain" ;;
  -A) [[ -e "$d/$chain" ]] || exit 1; printf '%s\n' "$spec" >>"$d/$chain" ;;
  -I) [[ -e "$d/$chain" ]] || exit 1
      pos=1; if [[ "${1:-}" =~ ^[0-9]+$ ]]; then pos=$1; shift; spec="$*"; fi
      awk -v p="$pos" -v s="$spec" 'NR == p {print s} {print} END {if (NR < p) print s}' "$d/$chain" >"$d/$chain.n"
      mv "$d/$chain.n" "$d/$chain" ;;
  -D) [[ -e "$d/$chain" ]] || exit 1; grep -qxF -- "$spec" "$d/$chain" || exit 1
      awk -v s="$spec" '!done && $0 == s {done = 1; next} {print}' "$d/$chain" >"$d/$chain.n"; mv "$d/$chain.n" "$d/$chain" ;;
  -E) [[ -e "$d/$chain" && ! -e "$d/$1" ]] || exit 1
      mv "$d/$chain" "$d/$1"
      for f in "$d"/*; do sed -i "s/ -j $chain\$/ -j $1/" "$f"; done ;;
  -S) [[ -e "$d/$chain" ]] || exit 1
      if builtin_chain "$chain"; then echo "-P $chain ACCEPT"; else echo "-N $chain"; fi
      sed "s/^/-A $chain /" "$d/$chain" ;;
  *) exit 3 ;;
esac
SH

cat >"$SHIM/ip" <<'SH'
#!/usr/bin/env bash
printf 'ip %s\n' "$*" >>"$FAKE/calls"
[[ "${1:-}" == -4 ]] && shift
touch "$FAKE/routes"
case "$1 $2" in
  "link show") [[ -e "$FAKE/link-$3" ]] ;;
  "link del") rm -f "$FAKE/link-$3"; grep -v " dev $3 " "$FAKE/routes" >"$FAKE/routes.n" || true; mv "$FAKE/routes.n" "$FAKE/routes" ;;
  "rule show") printf '0:\tfrom all lookup local\n32766:\tfrom all lookup main\n32767:\tfrom all lookup default\n' ;;
  "route show")
    if [[ "${3:-}" == dev ]]; then awk -v d="$4" '$0 ~ (" dev " d " ") {sub(" dev " d, ""); print}' "$FAKE/routes"
    else cat "$FAKE/routes"; fi ;;
  "route replace") grep -v "^$3 dev $5 " "$FAKE/routes" >"$FAKE/routes.n" || true; mv "$FAKE/routes.n" "$FAKE/routes"; echo "$3 dev $5 scope link " >>"$FAKE/routes" ;;
  "route del") grep -v "^$3 dev $5 " "$FAKE/routes" >"$FAKE/routes.n" || true; mv "$FAKE/routes.n" "$FAKE/routes" ;;
  "route get") [[ -n "${FAKE_NO_ROUTE:-}" ]] && exit 2
               echo "$3 via 192.0.2.1 dev ${FAKE_ROUTE_DEV:-eth9} src 192.0.2.10 uid 0" ;;
  *) exit 3 ;;
esac
SH

# Keys: 32 bytes from a fixed seed per host, base64 like the real tool prints.
cat >"$SHIM/wg" <<'SH'
#!/usr/bin/env bash
printf 'wg %s\n' "$*" >>"$FAKE/calls"
case "$1" in
  genkey) python3 -c 'import base64,hashlib,sys; print(base64.b64encode(hashlib.sha256(sys.argv[1].encode()).digest()).decode())' "priv-$FAKE_TAG" ;;
  pubkey) python3 -c 'import base64,hashlib,sys; print(base64.b64encode(hashlib.sha256(("pub" + sys.stdin.read().strip()).encode()).digest()).decode())' ;;
  show) [[ -e "$FAKE/link-$2" ]] || exit 1
        case "${FAKE_HS:-now}" in now) t=$(date +%s) ;; old) t=$(( $(date +%s) - 1000 )) ;; *) t=0 ;; esac
        printf 'peerkey\t%s\n' "$t" ;;
  set) printf '%s\n' "$*" >>"$FAKE/wg-set" ;;
  *) exit 3 ;;
esac
SH

# wg-quick played by systemctl: link, routes of AllowedIPs, PostUp; PreDown, routes, link.
cat >"$SHIM/systemctl" <<'SH'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$FAKE/calls"
printf '%s %s\n' "$(<"$FAKE/../tag")" "$*" >>"${FAKE_ORDER:-/dev/null}"
quiet=0; args=()
for a in "$@"; do [[ "$a" == --quiet ]] && quiet=1 || args+=("$a"); done
set -- "${args[@]}"
conf() { echo "$TB_WG_ROOT/etc/wireguard/wg0.conf"; }
hooks() { sed -n "s/^$1 = //p" "$(conf)" | sed 's/%i/wg0/g'; }
case "$1 ${2:-}" in
  "enable --now")
    [[ -n "${FAKE_START_FAIL:-}" ]] && exit 1
    while read -r h; do [[ -n "$h" ]] && bash -c "$h"; done < <(hooks PreUp) || exit 1
    echo "systemctl: link created" >>"$FAKE/calls"
    : >"$FAKE/link-wg0"; : >"$FAKE/enabled"
    # Like wg-quick when a step after PreUp fails: its trap deletes the link, PostDown never runs,
    # the unit is enabled but not active.
    if [[ -n "${FAKE_FAIL_AFTER_PREUP:-}" ]]; then
      rm -f "$FAKE/link-wg0"; echo "systemctl: start failed after PreUp" >>"$FAKE/calls"; exit 1
    fi
    : >"$FAKE/active"
    addr=$(sed -n 's/^Address = //p' "$(conf)")
    echo "10.250.0.0/30 dev wg0 proto kernel scope link src ${addr%/*} " >>"$FAKE/routes"
    for n in $(sed -n 's/^AllowedIPs = //p' "$(conf)" | tr -d ,); do echo "$n dev wg0 scope link " >>"$FAKE/routes"; done
    while read -r h; do [[ -n "$h" ]] && bash -c "$h"; done < <(hooks PostUp)
    [[ -n "${FAKE_STRAY:-}" ]] && iptables -A FORWARD -s 198.51.100.0/24 -j ACCEPT
    [[ -n "${FAKE_VANISH:-}" ]] && iptables -D FORWARD -j KUBE-ROUTER-FORWARD
    exit 0 ;;
  "disable --now")
    if [[ -e "$FAKE/active" ]]; then while read -r h; do [[ -n "$h" ]] && bash -c "$h"; done < <(hooks PreDown); fi
    was_active=0; [[ -e "$FAKE/active" ]] && was_active=1
    rm -f "$FAKE/link-wg0" "$FAKE/enabled" "$FAKE/active"
    echo "systemctl: link removed" >>"$FAKE/calls"
    grep -v " dev wg0 " "$FAKE/routes" >"$FAKE/routes.n" || true; mv "$FAKE/routes.n" "$FAKE/routes"
    if (( was_active )); then while read -r h; do [[ -n "$h" ]] && bash -c "$h"; done < <(hooks PostDown); fi
    exit 0 ;;
  "is-enabled "*) if [[ -e "$FAKE/enabled" ]]; then (( quiet )) || echo enabled; exit 0; fi; (( quiet )) || echo disabled; exit 1 ;;
  "is-active "*) [[ -e "$FAKE/active" ]] ;;
  "show -p") [[ -n "${FAKE_K3S_DATADIR:-}" ]] && \
      echo "{ path=/usr/local/bin/k3s ; argv[]=/usr/local/bin/k3s server --cluster-init --data-dir $FAKE_K3S_DATADIR --tls-san 192.0.2.10 ; ignore_errors=no }"
      exit 0 ;;
  *) exit 3 ;;
esac
SH

cat >"$SHIM/dpkg-query" <<'SH'
#!/usr/bin/env bash
[[ -e "$FAKE/pkg" ]] || exit 1
printf 'install ok installed'
SH
cat >"$SHIM/apt-get" <<'SH'
#!/usr/bin/env bash
printf 'apt-get %s NEEDRESTART_SUSPEND=%s\n' "$*" "${NEEDRESTART_SUSPEND:-unset}" >>"$FAKE/calls"
case " $* " in *" install "*) : >"$FAKE/pkg" ;; *" remove "*) rm -f "$FAKE/pkg" ;; esac
SH
cat >"$SHIM/modinfo" <<'SH'
#!/usr/bin/env bash
[[ -n "${FAKE_NO_MODULE:-}" ]] && exit 1
echo /lib/modules/x/kernel/drivers/net/wireguard/wireguard.ko
SH
# ping: the last argument is the target. FAKE_PING_LOSS lists targets that answer nothing,
# FAKE_PING_SLOW lists targets that answer at 40 ms; the rest answer at 3.7 ms (office path) or
# 4.5 ms (anything else).
cat >"$SHIM/ping" <<'SH'
#!/usr/bin/env bash
t=${!#}
printf 'ping %s\n' "$t" >>"$FAKE/calls"
case " ${FAKE_PING_LOSS:-} " in *" $t "*) printf '10 packets transmitted, 0 received, 100%% packet loss, time 1800ms\n'; exit 1 ;; esac
avg=4.5; max=6.1
case " ${FAKE_PING_SLOW:-} " in *" $t "*) avg=40.2; max=44.0 ;; esac
if [[ "$t" == "${FAKE_PING_SLOW_ONCE:-}" && ! -e "$FAKE/slow-once" ]]; then : >"$FAKE/slow-once"; avg=40.2; max=44.0; fi
[[ "$t" == "${FAKE_LAN:-}" ]] && { avg=3.7; max=3.9; }
printf '10 packets transmitted, 10 received, 0%% packet loss, time 1800ms\nrtt min/avg/max/mdev = 3.1/%s/%s/0.2 ms\n' "$avg" "$max"
SH
# crictl, like the k3s one on 119: without --config naming a readable file it cannot find the
# runtime and ends with rc 1.
cat >"$SHIM/crictl" <<'SH'
#!/usr/bin/env bash
[[ "${1:-}" == --config && -r "${2:-}" ]] || { echo "crictl: no config" >&2; exit 1; }
shift 2
[[ -n "${FAKE_NO_POD:-}" ]] && exit 0
case "$1" in ps) echo c0ffee ;; inspect) echo 4242 ;; *) exit 3 ;; esac
SH
# nsenter -t PID -n <cmd>: runs <cmd> here, except the TCP probe, answered from FAKE_TCP.
cat >"$SHIM/nsenter" <<'SH'
#!/usr/bin/env bash
printf 'nsenter %s\n' "$*" >>"$FAKE/calls"
shift 3
case "$*" in
  *"/dev/tcp/"*) case "${FAKE_TCP:-refused}" in
      refused) echo "bash: connect: Connection refused" >&2; exit 1 ;;
      connected) exit 0 ;;
      *) exit 124 ;;
    esac ;;
esac
exec "$@"
SH
cat >"$SHIM/tcpdump" <<'SH'
#!/usr/bin/env bash
printf 'tcpdump %s\n' "$*" >>"$FAKE/calls"
for p in ${FAKE_ARRIVE:-}; do
  printf '11:00:00.000001 IP 192.0.2.66.40001 > 192.0.2.119.%s: UDP, length 11\n' "$p"
  printf '11:00:00.200001 IP 192.0.2.66.40002 > 192.0.2.119.%s: UDP, length 11\n' "$p"
done
SH
chmod +x "$SHIM"/*

# One fake host: a root tree for the tool and a state directory for the fakes.
new_host() {  # new_host <dir> <tag>
  local h=$1
  mkdir -p "$h/root" "$h/fake/ipt/filter" "$h/fake/ipt/nat" "$h/fake/ipt/mangle" "$h/ct"
  printf -- '-j KUBE-ROUTER-INPUT\n' >"$h/fake/ipt/filter/INPUT"
  printf -- '-j KUBE-ROUTER-FORWARD\n-s 10.42.0.0/16 -j ACCEPT\n' >"$h/fake/ipt/filter/FORWARD"
  : >"$h/fake/ipt/filter/OUTPUT"
  : >"$h/fake/ipt/nat/PREROUTING"
  printf -- '-s 192.168.122.0/24 -o vmbr0.200 -j MASQUERADE\n' >"$h/fake/ipt/nat/POSTROUTING"
  : >"$h/fake/ipt/mangle/FORWARD"
  : >"$h/fake/ipt/mangle/PREROUTING"
  mkdir -p "$h/k3sdata/agent/etc"; echo "runtime-endpoint: unix:///fake" >"$h/k3sdata/agent/etc/crictl.yaml"
  printf 'default via 192.0.2.1 dev eth9 proto static \n192.0.2.0/24 dev eth9 proto kernel scope link src 192.0.2.10 \n' >"$h/fake/routes"
  echo 8010 >"$h/ct/nf_conntrack_count"; echo 262144 >"$h/ct/nf_conntrack_max"
  echo "$2" >"$h/tag"
}
# run_on <hostdir> <tool args...>: the tool as it runs on that host.
run_on() {
  local h=$1; shift
  env PATH="$SHIM:$PATH" TB_WG_ROOT="$h/root" FAKE="$h/fake" FAKE_TAG="$(<"$h/tag")" \
    FAKE_K3S_DATADIR="${FAKE_K3S_DATADIR-$h/k3sdata}" \
    TB_WG_CONNTRACK_DIR="$h/ct" TB_WG_ENV="$SCRIPT_DIR/vm-tunnel/tunnel.env" bash "$TOOL" "$@"
}
install_self() {  # what t0-apply.sh push does: the tool where the conf hooks expect it
  mkdir -p "$1/root/usr/local/sbin"
  cp "$TOOL" "$1/root/usr/local/sbin/tb-wg"; chmod 755 "$1/root/usr/local/sbin/tb-wg"
}
ipt_dump() { (cd "$1/fake/ipt" && for f in */*; do printf '%s: ' "$f"; tr '\n' '|' <"$f"; echo; done); }
pub_of() { python3 -c 'import base64,hashlib,sys; p=base64.b64encode(hashlib.sha256(sys.argv[1].encode()).digest()).decode(); print(base64.b64encode(hashlib.sha256(("pub"+p).encode()).digest()).decode())' "priv-$1"; }
priv_of() { python3 -c 'import base64,hashlib,sys; print(base64.b64encode(hashlib.sha256(sys.argv[1].encode()).digest()).decode())' "priv-$1"; }

# Brings a pair up by hand, the way t0-apply.sh does it.
pair_up() {  # pair_up <h119> <h66> <port>
  local a=$1 b=$2 port=$3
  install_self "$a"; install_self "$b"
  run_on "$a" keygen >/dev/null; run_on "$b" keygen >/dev/null
  run_on "$a" render 119 --port "$port" <<<"$(run_on "$b" pubkey)" >/dev/null
  run_on "$b" render 66 --port "$port" <<<"$(run_on "$a" pubkey)" >/dev/null
  sysctl_on "$a" enable --now wg-quick@wg0
  sysctl_on "$b" enable --now wg-quick@wg0
}
# systemctl as wg-quick would see it on that host (the hooks run the installed tool).
sysctl_on() {
  local h=$1; shift
  (export PATH="$SHIM:$PATH" TB_WG_ROOT="$h/root" FAKE="$h/fake" FAKE_TAG="$(<"$h/tag")" \
     TB_WG_ENV="$SCRIPT_DIR/vm-tunnel/tunnel.env" TB_WG_CONNTRACK_DIR="$h/ct"; systemctl "$@")
}

# ---- 1. keygen: private key stays in a 0600 file, nothing printed, second run keeps the key -----
H=$TMP_ROOT/t1; new_host "$H" k1
out=$(run_on "$H" keygen)
[[ "$out" == "tb-wg: key ready" ]] || fail "keygen printed more than its status: $out"
[[ "$(stat -c %a "$H/root/etc/wireguard/wg0.key")" == 600 ]] || fail "private key mode is not 600"
[[ "$(stat -c %a "$H/root/etc/wireguard")" == 700 ]] || fail "/etc/wireguard mode is not 700"
[[ "$(<"$H/root/etc/wireguard/wg0.key")" == "$(priv_of k1)" ]] || fail "key file does not hold the generated key"
echo "keep-me" >"$H/root/etc/wireguard/wg0.key"
run_on "$H" keygen >/dev/null
[[ "$(<"$H/root/etc/wireguard/wg0.key")" == keep-me ]] || fail "keygen replaced an existing key"
ok "keygen"

# ---- 2. render: conf without key material, right shape per role, bad peer key refused ----------
A=$TMP_ROOT/t2a; B=$TMP_ROOT/t2b; new_host "$A" a2; new_host "$B" b2
run_on "$A" keygen >/dev/null; run_on "$B" keygen >/dev/null
if run_on "$A" render 119 --port 4500 <<<"not-a-key" 2>/dev/null; then fail "render accepted a malformed peer key"; fi
if run_on "$A" render 119 <<<"$(run_on "$B" pubkey)" 2>/dev/null; then fail "render ran without --port"; fi
if run_on "$A" render 7 --port 1 <<<"$(run_on "$B" pubkey)" 2>/dev/null; then fail "render accepted role 7"; fi
run_on "$A" render 119 --port 4500 <<<"$(run_on "$B" pubkey)" >/dev/null
run_on "$B" render 66 --port 4500 <<<"$(run_on "$A" pubkey)" >/dev/null
CA=$A/root/etc/wireguard/wg0.conf; CB=$B/root/etc/wireguard/wg0.conf
for c in "$CA" "$CB"; do
  [[ "$(stat -c %a "$c")" == 600 ]] || fail "$c mode is not 600"
  grep -q '^PrivateKey' "$c" && fail "$c has a PrivateKey line"
  grep -qF "$(priv_of a2)" "$c" && fail "$c holds the 119 private key"
  grep -qF "$(priv_of b2)" "$c" && fail "$c holds the 66 private key"
  grep -qx "PostUp = wg set %i private-key $(dirname "$c")/wg0.key" "$c" || fail "$c does not load the key in PostUp"
  grep -qx "PreUp = $(dirname "$(dirname "$(dirname "$c")")")/usr/local/sbin/tb-wg hook-up" "$c" || fail "$c guard is not PreUp"
  grep -qx "PostDown = $(dirname "$(dirname "$(dirname "$c")")")/usr/local/sbin/tb-wg hook-down" "$c" || fail "$c guard is not PostDown"
  grep -qE "^(PostUp|PreDown) = .*hook-" "$c" && fail "$c still hooks the guard to PostUp or PreDown"
  grep -qx "MTU = 1420" "$c" || fail "$c MTU"
done
grep -qx "Address = 10.250.0.1/30" "$CA" || fail "119 address"
grep -qx "ListenPort = 4500" "$CA" || fail "119 listen port"
grep -q '^Endpoint' "$CA" && fail "119 must not dial 66"
grep -qx "AllowedIPs = 10.250.0.2/32, 192.168.123.0/24, 192.168.124.0/24, 192.168.125.0/24, 192.168.126.0/24" "$CA" || fail "119 AllowedIPs: $(grep AllowedIPs "$CA")"
grep -qx "PublicKey = $(pub_of b2)" "$CA" || fail "119 peer key is not the 66 public key"
grep -qx "Address = 10.250.0.2/30" "$CB" || fail "66 address"
grep -q '^ListenPort' "$CB" && fail "66 must not listen on a fixed port"
grep -qx "Endpoint = 192.168.230.119:4500" "$CB" || fail "66 endpoint"
grep -qx "PersistentKeepalive = 25" "$CB" || fail "66 keepalive"
grep -qx "AllowedIPs = 10.250.0.1/32" "$CB" || fail "66 AllowedIPs: $(grep AllowedIPs "$CB")"
grep -qx "PublicKey = $(pub_of a2)" "$CB" || fail "66 peer key is not the 119 public key"
ok "render"

# ---- 3. up and down on 119: guard in mangle PREROUTING, filter untouched, gone after down ------
A=$TMP_ROOT/t3a; B=$TMP_ROOT/t3b; new_host "$A" a3; new_host "$B" b3
before=$(ipt_dump "$A")
filter_before=$(cat "$A/fake/ipt/filter/INPUT" "$A/fake/ipt/filter/FORWARD")
pair_up "$A" "$B" 51820
chain=$(<"$A/fake/ipt/mangle/TBWG-IN")
expected=$'-m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-p icmp --icmp-type echo-request -d 10.250.0.1 -j RETURN
-p tcp -d 10.250.0.1 --dport 18090 -j RETURN
-p tcp -d 10.250.0.1 --dport 514 -j RETURN
-p udp -d 10.250.0.1 --dport 514 -j RETURN
-p udp -d 10.250.0.1 --dport 162 -j RETURN
-j DROP'
[[ "$chain" == "$expected" ]] || fail "TBWG-IN differs:"$'\n'"$chain"
[[ "$(<"$A/fake/ipt/mangle/PREROUTING")" == "-i wg0 -j TBWG-IN" ]] || fail "mangle PREROUTING jump"
[[ -e "$A/fake/ipt/filter/TBWG-IN" ]] && fail "the 119 guard must not live in the filter table (kube-proxy prepends there)"
[[ "$(cat "$A/fake/ipt/filter/INPUT" "$A/fake/ipt/filter/FORWARD")" == "$filter_before" ]] || fail "119 filter INPUT or FORWARD changed"
grep -qxF -- "-o wg0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu" "$A/fake/ipt/mangle/FORWARD" || fail "119 MSS clamp"
up_at=$(grep -n "^systemctl: link created" "$A/fake/calls" | head -1 | cut -d: -f1)
guard_at=$(grep -n -- "-E TBWG-IN-NEW TBWG-IN$" "$A/fake/calls" | head -1 | cut -d: -f1)
[[ -n "$up_at" && -n "$guard_at" ]] && (( guard_at < up_at )) || fail "119 guard was not in place before wg0 came up"
# k3s restarts: kube-proxy and kube-router prepend their jumps to filter; the guard does not move.
(export FAKE="$A/fake" PATH="$SHIM:$PATH"; iptables -I FORWARD 1 -j KUBE-FORWARD; iptables -I INPUT 1 -j KUBE-FIREWALL)
out=$(FAKE_LAN=192.168.200.66 run_on "$A" check) || fail "119 check failed after k3s prepended its jumps:"$'\n'"$out"
grep -qF "[PASS] TBWG-IN first in mangle (PREROUTING -i wg0), last rule DROP" <<<"$out" || fail "guard check line:"$'\n'"$out"
(export FAKE="$A/fake" PATH="$SHIM:$PATH"; iptables -D FORWARD -j KUBE-FORWARD; iptables -D INPUT -j KUBE-FIREWALL)
run_on "$A" hook-up >/dev/null   # a second PostUp (restart) must not stack rules
[[ "$(grep -c 'TBWG-IN' "$A/fake/ipt/mangle/PREROUTING")" == 1 ]] || fail "PREROUTING jump stacked"
[[ "$(grep -c TCPMSS "$A/fake/ipt/mangle/FORWARD")" == 1 ]] || fail "MSS stacked"
[[ "$(<"$A/fake/ipt/mangle/TBWG-IN")" == "$expected" ]] || fail "TBWG-IN changed on a second hook-up"
[[ -e "$A/fake/ipt/mangle/TBWG-IN-NEW" ]] && fail "the build chain was left behind"
for n in 192.168.123.0/24 192.168.124.0/24 192.168.125.0/24 192.168.126.0/24; do
  grep -q "^$n dev wg0 " "$A/fake/routes" || fail "119 route $n"
done
grep -q "^192.168.122.0/24 dev wg0" "$A/fake/routes" && fail "122 routed into the tunnel"
sysctl_on "$A" disable --now wg-quick@wg0
[[ "$(ipt_dump "$A")" == "$before" ]] || fail "119 rules after down differ from before:"$'\n'"$(ipt_dump "$A")"
grep -q " dev wg0 " "$A/fake/routes" && fail "wg0 routes left after down"
ok "119 up and down"

# ---- 4. up and down on 66: mangle FORWARD pairs per network and DROP last, nothing else touched -
A=$TMP_ROOT/t4a; B=$TMP_ROOT/t4b; new_host "$A" a4; new_host "$B" b4
before=$(ipt_dump "$B")
nat_before=$(<"$B/fake/ipt/nat/POSTROUTING")
pair_up "$A" "$B" 51820
chain=$(<"$B/fake/ipt/mangle/TBWG-FWD")
expected="-i wg0 -o eth9 -j DROP"$'\n'"-i eth9 -o wg0 -j DROP"$'\n'
for n in 192.168.123.0/24 192.168.124.0/24 192.168.125.0/24 192.168.126.0/24; do
  expected+="-i wg0 -s 10.250.0.1/32 -d $n -j RETURN"$'\n'"-o wg0 -s $n -d 10.250.0.1/32 -j RETURN"$'\n'
done
expected+="-j DROP"
[[ "$chain" == "$expected" ]] || fail "TBWG-FWD differs:"$'\n'"$chain"
# The guard is in before the link and out after it (PreUp, PostDown): never wg0 without it.
up_at=$(grep -n "^systemctl: link created" "$B/fake/calls" | head -1 | cut -d: -f1)
guard_at=$(grep -n -- "-E TBWG-FWD-NEW TBWG-FWD$" "$B/fake/calls" | head -1 | cut -d: -f1)
[[ -n "$up_at" && -n "$guard_at" ]] && (( guard_at < up_at )) || fail "66 guard was not in place before wg0 came up"
# Verdicts of the 66 FORWARD path (mangle then the ACCEPT policy) for packets that matter.
verdict() {  # verdict <hostdir> <in> <out> <src> <dst> -> ACCEPT or DROP
  python3 - "$1/fake/ipt/mangle" "$2" "$3" "$4" "$5" <<'PY'
import ipaddress, sys
d, iif, oif, src, dst = sys.argv[1:6]
def rules(c):
    return [l.split() for l in open(f"{d}/{c}").read().splitlines() if l.strip()]
def match(r):
    i = 0
    while i < len(r) and r[i] != "-j":
        k, v = r[i], r[i + 1]
        if k == "-i" and v != iif: return False
        if k == "-o" and v != oif: return False
        if k == "-s" and ipaddress.ip_address(src) not in ipaddress.ip_network(v): return False
        if k == "-d" and ipaddress.ip_address(dst) not in ipaddress.ip_network(v): return False
        if k == "-p": return False      # only plain address rules decide these packets
        i += 2
    return True
def walk(c):
    for r in rules(c):
        if not match(r): continue
        t = r[r.index("-j") + 1]
        if t in ("DROP", "ACCEPT"): return t
        if t == "RETURN": return None
        v = walk(t)
        if v: return v
    return None
print(walk("FORWARD") or "ACCEPT")
PY
}
v() { local want=$1; shift; [[ "$(verdict "$B" "$@")" == "$want" ]] || fail "66 FORWARD $* should be $want"; }
v ACCEPT wg0 vmbr8 10.250.0.1 192.168.124.11    # poll into the web network through the router uplink bridge
v ACCEPT vmbr8 wg0 192.168.124.11 10.250.0.1    # its answer
v DROP wg0 eth9 10.250.0.1 192.168.124.11       # T2 taken back: no bridge, the poll would leave by the default route
v DROP eth9 wg0 192.168.124.11 10.250.0.1       # a forged testbed source from the office side
v DROP wg0 vmbr1 10.250.0.1 192.168.122.20      # the old testbed
v DROP wg0 vmbr8 10.250.0.1 192.168.127.21      # the management network
head -2 "$B/fake/ipt/mangle/FORWARD" | grep -qxF -- "-i wg0 -j TBWG-FWD" || fail "66 FORWARD -i jump"
head -2 "$B/fake/ipt/mangle/FORWARD" | grep -qxF -- "-o wg0 -j TBWG-FWD" || fail "66 FORWARD -o jump"
[[ "$(sed -n 3p "$B/fake/ipt/mangle/FORWARD")" == "-o wg0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu" ]] || fail "66 MSS clamp after the guard"
[[ "$(<"$B/fake/ipt/nat/POSTROUTING")" == "$nat_before" ]] || fail "66 nat rules changed"
[[ -e "$B/fake/ipt/filter/TBWG-FWD" ]] && fail "the 66 guard must not live in the filter table"
grep -q "wg0" "$B/fake/ipt/filter/FORWARD" && fail "66 filter FORWARD got wg0 rules (t4-net owns filter, tb-wg owns wg0)"
[[ -e "$B/fake/ipt/mangle/TBWG-IN" ]] && fail "66 got the 119 chain"
[[ -e "$A/fake/ipt/mangle/TBWG-FWD" ]] && fail "119 got the 66 chain"
grep -q " dev wg0 " <(grep -v "^10.250.0" "$B/fake/routes") && fail "66 routes testbed networks into the tunnel"
run_on "$B" hook-up >/dev/null
[[ "$(<"$B/fake/ipt/mangle/TBWG-FWD")" == "$expected" ]] || fail "TBWG-FWD changed on a second hook-up"
[[ "$(grep -c 'TBWG-FWD' "$B/fake/ipt/mangle/FORWARD")" == 2 ]] || fail "66 jumps stacked"
mark=$(wc -l <"$B/fake/calls")
sysctl_on "$B" disable --now wg-quick@wg0
down_at=$(tail -n +"$((mark + 1))" "$B/fake/calls" | grep -n "^systemctl: link removed" | head -1 | cut -d: -f1)
gone_at=$(tail -n +"$((mark + 1))" "$B/fake/calls" | grep -n -- "-X TBWG-FWD$" | head -1 | cut -d: -f1)
[[ -n "$down_at" && -n "$gone_at" ]] && (( down_at < gone_at )) || fail "66 guard left before wg0 was gone"
[[ "$(ipt_dump "$B")" == "$before" ]] || fail "66 rules after down differ from before"
ok "66 up and down"

# ---- 5. add-net and del-net: S network slot, refused networks, live on both ends --------------
A=$TMP_ROOT/t5a; B=$TMP_ROOT/t5b; new_host "$A" a5; new_host "$B" b5
pair_up "$A" "$B" 51820
for bad in 192.168.122.0/24 192.168.127.0/24 192.168.133.0/24 192.168.230.0/24 192.168.124.0/22 \
           192.168.131.5/24 10.42.0.0/24 192.168.031.0/24 192.168.300.0/24 0.0.0.0/0 ""; do
  if run_on "$A" add-net "$bad" 2>/dev/null; then fail "add-net accepted '$bad'"; fi
done
grep -q "192.168.122" "$A/root/etc/wireguard/wg0.nets" && fail "refused network written"
run_on "$A" add-net 192.168.131.0/24 >/dev/null
mark=$(wc -l <"$B/fake/calls")
run_on "$B" add-net 192.168.131.0/24 >/dev/null
# The live guard is never refilled in place: rules go into the build chain, which then takes over.
calls=$(tail -n +"$((mark + 1))" "$B/fake/calls" | grep '^iptables -t mangle')
grep -qE -- "-A TBWG-FWD " <<<"$calls" && fail "add-net appended to the live TBWG-FWD"
first_old_del=$(grep -n -- "-D FORWARD -i wg0 -j TBWG-FWD$" <<<"$calls" | head -1 | cut -d: -f1)
last_new_jump=$(grep -n -- "-I FORWARD 1 -[io] wg0 -j TBWG-FWD-NEW$" <<<"$calls" | tail -1 | cut -d: -f1)
flush_old=$(grep -n -- "-F TBWG-FWD$" <<<"$calls" | head -1 | cut -d: -f1)
[[ -n "$first_old_del" && -n "$last_new_jump" && -n "$flush_old" ]] || fail "add-net did not swap chains:"$'\n'"$calls"
(( last_new_jump < first_old_del && first_old_del < flush_old )) || fail "add-net swap order wrong:"$'\n'"$calls"
grep -qx "192.168.131.0/24" "$A/root/etc/wireguard/wg0.nets" || fail "119 nets file"
grep -qx "AllowedIPs = 10.250.0.2/32, 192.168.123.0/24, 192.168.124.0/24, 192.168.125.0/24, 192.168.126.0/24, 192.168.131.0/24" \
  "$A/root/etc/wireguard/wg0.conf" || fail "119 conf AllowedIPs after add-net"
grep -qx "AllowedIPs = 10.250.0.1/32" "$B/root/etc/wireguard/wg0.conf" || fail "66 AllowedIPs must not change"
tail -1 "$A/fake/wg-set" | grep -qx "set wg0 peer $(pub_of b5) allowed-ips 10.250.0.2/32,192.168.123.0/24,192.168.124.0/24,192.168.125.0/24,192.168.126.0/24,192.168.131.0/24" \
  || fail "119 live allowed-ips: $(tail -1 "$A/fake/wg-set")"
grep -q "^192.168.131.0/24 dev wg0 " "$A/fake/routes" || fail "119 live route for 131"
grep -qxF -- "-i wg0 -s 10.250.0.1/32 -d 192.168.131.0/24 -j RETURN" "$B/fake/ipt/mangle/TBWG-FWD" || fail "66 live rule for 131"
[[ "$(tail -1 "$B/fake/ipt/mangle/TBWG-FWD")" == "-j DROP" ]] || fail "66 DROP not last after add-net"
[[ "$(grep -c 'TBWG-FWD' "$B/fake/ipt/mangle/FORWARD")" == 2 ]] || fail "66 jumps stacked by add-net"
[[ -e "$B/fake/ipt/mangle/TBWG-FWD-NEW" ]] && fail "66 build chain left after add-net"
grep -q -- "TBWG-FWD-NEW" "$B/fake/ipt/mangle/FORWARD" && fail "66 jumps still name the build chain"
n_before=$(wc -l <"$A/root/etc/wireguard/wg0.nets")
run_on "$A" add-net 192.168.131.0/24 >/dev/null
[[ "$(wc -l <"$A/root/etc/wireguard/wg0.nets")" == "$n_before" ]] || fail "add-net twice added twice"
run_on "$A" del-net 192.168.131.0/24 >/dev/null
run_on "$B" del-net 192.168.131.0/24 >/dev/null
grep -q 192.168.131 "$A/root/etc/wireguard/wg0.conf" && fail "119 conf still has 131"
grep -q "^192.168.131.0/24 dev wg0" "$A/fake/routes" && fail "119 route for 131 left"
grep -q 192.168.131 "$B/fake/ipt/mangle/TBWG-FWD" && fail "66 rule for 131 left"
for n in 192.168.124.0/24 192.168.125.0/24 192.168.126.0/24; do run_on "$A" del-net "$n" >/dev/null; done
if run_on "$A" del-net 192.168.123.0/24 2>/dev/null; then fail "del-net left the tunnel with no network"; fi
ok "add-net and del-net"

# ---- 6. check: passes when healthy, fails on each broken part --------------------------------
A=$TMP_ROOT/t6a; B=$TMP_ROOT/t6b; new_host "$A" a6; new_host "$B" b6
pair_up "$A" "$B" 51820
out=$(FAKE_LAN=192.168.200.66 run_on "$A" check --target 192.168.123.254:22 --target 192.168.124.11) \
  || fail "119 check failed when healthy:"$'\n'"$out"
grep -q '^\[FAIL\]' <<<"$out" && fail "119 healthy check printed a FAIL"
for want in "latest handshake" "ping 10.250.0.2 through wg0: loss 0%" "route 192.168.126.0/24 dev wg0" \
            "TBWG-IN first in mangle (PREROUTING -i wg0), last rule DROP" "TCP MSS clamped" "conntrack 8010 of 262144" \
            "ping 192.168.123.254 from this host" "ping 10.250.0.2 from the collector-nms pod" \
            "tcp 10.250.0.2:9 from the collector-nms pod: refused" "tcp 192.168.123.254:22 from the collector-nms pod: refused" \
            "ping 192.168.124.11 from the collector-nms pod" "wg-quick@wg0 enabled"; do
  grep -qF "[PASS] $want" <<<"$out" || fail "119 check has no PASS for '$want':"$'\n'"$out"
done
grep -q "tcp 192.168.124.11" <<<"$out" && fail "a target without a port must not get a TCP probe"
grep -q "nsenter -t 4242" "$A/fake/calls" || fail "the pod pid from crictl was not used"
if run_on "$A" check --target 192.168.123.254:x >/dev/null 2>&1; then fail "check took a malformed target"; fi
out=$(FAKE_LAN=192.168.230.119 run_on "$B" check) || fail "66 check failed when healthy:"$'\n'"$out"
grep -qF "[PASS] TBWG-FWD lets 192.168.124.0/24 and 10.250.0.1 through" <<<"$out" || fail "66 check pairs"
grep -qF "[PASS] TBWG-FWD first in mangle (FORWARD -i wg0, FORWARD -o wg0), last rule DROP" <<<"$out" || fail "66 check jumps:"$'\n'"$out"
grep -qF "[PASS] TBWG-FWD drops wg0 to and from the uplink eth9 first" <<<"$out" || fail "66 check uplink pair:"$'\n'"$out"
grep -q 'collector-nms' <<<"$out" && fail "66 check looked for a pod"
export -f run_on
export SHIM TOOL SCRIPT_DIR
cf() {  # cf <host> <expected FAIL text> [VAR=value...]: check must fail and say so
  local h=$1 msg=$2 o rc; shift 2
  o=$(env FAKE_LAN=192.168.200.66 "$@" bash -c 'run_on "$0" check --target 192.168.123.254:22' "$h" 2>&1) && rc=0 || rc=$?
  (( rc != 0 )) || fail "check passed with: $msg"
  grep -qF "[FAIL] $msg" <<<"$o" || fail "check did not report '$msg':"$'\n'"$o"
}
cf "$A" "latest handshake" FAKE_HS=old
cf "$A" "no handshake yet" FAKE_HS=never
cf "$A" "ping 10.250.0.2 through wg0: loss 100%" FAKE_PING_LOSS=10.250.0.2
cf "$A" "ping 10.250.0.2 through wg0: avg 40.2ms, max 44.0ms against office path avg 3.7ms + 2ms, max 20ms, 3 rounds out of 3" FAKE_PING_SLOW=10.250.0.2
rm -f "$A/fake/slow-once"
out=$(FAKE_LAN=192.168.200.66 FAKE_PING_SLOW_ONCE=10.250.0.2 run_on "$A" check) || fail "one slow round failed the check:"$'\n'"$out"
grep -qF "[PASS] ping 10.250.0.2 through wg0: loss 0%" <<<"$out" && grep -qF "(over in 1 of the rounds taken)" <<<"$out" \
  || fail "the slow round was not retried:"$'\n'"$out"
cf "$A" "ping 192.168.123.254 from this host: loss 100%" FAKE_PING_LOSS=192.168.123.254
cf "$A" "tcp 10.250.0.2:9 from the collector-nms pod: no answer" FAKE_TCP=timeout
cf "$A" "tcp 192.168.123.254:22 from the collector-nms pod: no answer" FAKE_TCP=timeout
cf "$A" "no running collector-nms container through $A/k3sdata/agent/etc/crictl.yaml" FAKE_NO_POD=1
# crictl config: without the k3s data dir the default path has no config, so the pod is not found.
cf "$A" "no running collector-nms container through $A/root/var/lib/rancher/k3s/agent/etc/crictl.yaml" FAKE_K3S_DATADIR=
out=$(FAKE_LAN=192.168.200.66 FAKE_K3S_DATADIR= WG_CRICTL_CONFIG="$A/k3sdata/agent/etc/crictl.yaml" run_on "$A" pod-check) \
  || fail "WG_CRICTL_CONFIG was not used: $out"
mkdir -p "$A/root/etc/rancher/k3s"; printf 'node-name: x\ndata-dir: "%s"\n' "$A/k3sdata" >"$A/root/etc/rancher/k3s/config.yaml"
out=$(FAKE_K3S_DATADIR= run_on "$A" pod-check) || fail "data-dir in config.yaml was not used: $out"
rm -rf "$A/root/etc/rancher"
echo 250000 >"$A/ct/nf_conntrack_count"
cf "$A" "conntrack 250000 of 262144"
echo 8010 >"$A/ct/nf_conntrack_count"
cp "$A/fake/ipt/mangle/TBWG-IN" "$TMP_ROOT/keep"; sed -i '$d' "$A/fake/ipt/mangle/TBWG-IN"   # DROP gone
cf "$A" "TBWG-IN does not end in DROP"
cp "$TMP_ROOT/keep" "$A/fake/ipt/mangle/TBWG-IN"
(export FAKE="$A/fake" PATH="$SHIM:$PATH"; iptables -t mangle -I PREROUTING 1 -j SOMEONE-ELSE)
cf "$A" "TBWG-IN: '-A PREROUTING -i wg0 -j TBWG-IN' is not at the top of mangle PREROUTING"
(export FAKE="$A/fake" PATH="$SHIM:$PATH"; iptables -t mangle -D PREROUTING -j SOMEONE-ELSE)
grep -v "192.168.125.0/24" "$A/fake/routes" >"$TMP_ROOT/r"; cp "$A/fake/routes" "$TMP_ROOT/keep"; cp "$TMP_ROOT/r" "$A/fake/routes"
cf "$A" "route 192.168.125.0/24 dev wg0 missing"
cp "$TMP_ROOT/keep" "$A/fake/routes"
: >"$A/fake/ipt/mangle/FORWARD"
cf "$A" "TCP MSS clamp missing"
cp "$B/fake/ipt/mangle/TBWG-FWD" "$TMP_ROOT/keepf"; sed -i '1,2d' "$B/fake/ipt/mangle/TBWG-FWD"
o=$(FAKE_LAN=192.168.230.119 run_on "$B" check 2>&1) && fail "66 check passed without the uplink pair"
grep -qF "[FAIL] TBWG-FWD does not drop wg0 to and from the uplink eth9 before its RETURN rules" <<<"$o" || fail "66 check did not name the uplink pair:"$'\n'"$o"
{ sed -n '3,$p' "$TMP_ROOT/keepf" | sed '$d'; sed -n '1,2p' "$TMP_ROOT/keepf"; echo "-j DROP"; } >"$B/fake/ipt/mangle/TBWG-FWD"
o=$(FAKE_LAN=192.168.230.119 run_on "$B" check 2>&1) && fail "66 check passed with the uplink pair after the RETURN rules"
grep -qF "[FAIL] TBWG-FWD does not drop wg0 to and from the uplink eth9 before its RETURN rules" <<<"$o" || fail "66 check did not see the uplink pair order"
cp "$TMP_ROOT/keepf" "$B/fake/ipt/mangle/TBWG-FWD"
o=$(FAKE_LAN=192.168.230.119 WG_UPLINK_66=vmbr9 run_on "$B" check 2>&1) && fail "66 check passed with another uplink named"
grep -qF "[FAIL] TBWG-FWD does not drop wg0 to and from the uplink vmbr9" <<<"$o" || fail "WG_UPLINK_66 was not used"
grep -v "192.168.124.0/24" "$B/fake/ipt/mangle/TBWG-FWD" >"$TMP_ROOT/f"; cp "$TMP_ROOT/f" "$B/fake/ipt/mangle/TBWG-FWD"
o=$(FAKE_LAN=192.168.230.119 run_on "$B" check 2>&1) && fail "66 check passed with a network pair missing"
grep -qF "[FAIL] TBWG-FWD has no rule pair for 192.168.124.0/24" <<<"$o" || fail "66 check did not name the missing pair"
(export FAKE="$B/fake" PATH="$SHIM:$PATH"; iptables -t mangle -I FORWARD 1 -j SOMEONE-ELSE)
o=$(FAKE_LAN=192.168.230.119 run_on "$B" check 2>&1) && fail "66 check passed with a rule above the guard"
grep -qF "is not at the top of mangle FORWARD" <<<"$o" || fail "66 check did not name the guard position:"$'\n'"$o"
rm -f "$A/fake/enabled"
cf "$A" "wg-quick@wg0 not enabled"
ok "check"

# ---- 7. install-tools and remove: package only on 119, purge removes only what we installed ---
A=$TMP_ROOT/t7a; B=$TMP_ROOT/t7b; new_host "$A" a7; new_host "$B" b7
: >"$B/fake/pkg"                                 # 66 had wireguard-tools already
run_on "$A" install-tools 119 >/dev/null
grep -q "apt-get install -y --no-install-recommends wireguard-tools NEEDRESTART_SUSPEND=1" "$A/fake/calls" || fail "119 did not install the package with needrestart suspended"
[[ -e "$A/root/var/lib/tb-wg/installed-wireguard-tools" ]] || fail "119 install not marked"
run_on "$B" install-tools 66 >/dev/null
grep -q apt-get "$B/fake/calls" 2>/dev/null && fail "66 ran apt-get"
[[ -e "$B/root/var/lib/tb-wg/installed-wireguard-tools" ]] && fail "66 marked a package it did not install"
B2=$TMP_ROOT/t7c; new_host "$B2" c7
if run_on "$B2" install-tools 66 2>/dev/null; then fail "66 without the package installed it"; fi
if FAKE_NO_MODULE=1 run_on "$A" install-tools 119 2>/dev/null; then fail "install-tools passed without the kernel module"; fi
pair_up "$A" "$B" 51820
run_on "$A" remove >/dev/null
[[ -e "$A/fake/link-wg0" || -e "$A/fake/enabled" ]] && fail "remove left 119 up or enabled"
[[ -s "$A/root/etc/wireguard/wg0.key" ]] || fail "remove without --purge deleted the key"
[[ -e "$A/fake/ipt/mangle/TBWG-IN" ]] && fail "remove left TBWG-IN"
run_on "$A" remove --purge >/dev/null
for f in wg0.conf wg0.key wg0.pub wg0.peer wg0.nets tb-wg.role; do [[ -e "$A/root/etc/wireguard/$f" ]] && fail "purge left $f"; done
grep -q "apt-get remove -y wireguard-tools NEEDRESTART_SUSPEND=1" "$A/fake/calls" || fail "119 purge did not remove the package it installed, needrestart suspended"
[[ -e "$A/root/var/lib/tb-wg" ]] && fail "purge left /var/lib/tb-wg"
[[ -e "$A/root/usr/local/sbin/tb-wg" ]] && fail "purge left the tool"
run_on "$B" remove --purge >/dev/null
grep -q apt-get "$B/fake/calls" && fail "66 purge removed a package it did not install"
[[ -e "$B/fake/pkg" ]] || fail "66 package gone"
ok "install-tools and remove"

# ---- 8. orchestrator: probe picks a port, apply brings both up, keys never in its output -------
A=$TMP_ROOT/t8a; B=$TMP_ROOT/t8b; new_host "$A" a8; new_host "$B" b8; : >"$B/fake/pkg"
# A fake ssh: the remote command runs here with that host root mapped into the absolute paths.
cat >"$SHIM/fake-ssh" <<'SH'
#!/usr/bin/env bash
h=$1; shift
cmd="$*"
root=$h/root
cmd=${cmd//\/usr\/local\/sbin/$root\/usr\/local\/sbin}
cmd=${cmd//\/etc\/wireguard/$root\/etc\/wireguard}
export TB_WG_ROOT=$root FAKE=$h/fake FAKE_TAG=$(<"$h/tag") TB_WG_CONNTRACK_DIR=$h/ct FAKE_LAN=$(<"$h/lan") FAKE_K3S_DATADIR=$h/k3sdata
# FAKE_SSH_DOWN: this host stops answering once its tunnel is up (rc 255 like ssh).
# FAKE_SSH_SNAP_FAIL: snapshots of this host fail once its tunnel is up.
if [[ -e "$h/fake/link-wg0" ]]; then
  [[ -e "$h/ssh-down" ]] && exit 255
  [[ -e "$h/ssh-snap-fail" && "$cmd" == *snapshot* ]] && exit 255
fi
exec bash -c "$cmd"
SH
chmod +x "$SHIM/fake-ssh"
echo 192.168.200.66 >"$A/lan"; echo 127.0.0.1 >"$B/lan"
LOCK=$TMP_ROOT/testbed.lock; : >"$LOCK"
orch_nolock() {  # the hosts are $A and $B at call time
  env PATH="$SHIM:$PATH" TB_SSH_119="fake-ssh $A" TB_SSH_66="fake-ssh $B" WG_HOST_119=127.0.0.1 \
    TB_WG_RUN_DIR="$TMP_ROOT/runs-${A##*/}" TB_WG_PROBE_SECS=1 TB_WG_PROBE_LEAD=0 TB_WG_HANDSHAKE_WAIT=5 \
    FAKE_ORDER="$TMP_ROOT/order-${A##*/}" TB_LOCK_FILE="${ORCH_LOCK-$LOCK}" bash "$ORCH" "$@"
}
# The test holds the testbed lock on fd 8, as "flock <lock> t0-apply.sh" would; lock_off drops it.
exec 8>"$LOCK"; flock 8
lock_off() { flock -u 8; }
lock_on() { flock 8; }
orch() { orch_nolock "$@"; }
out=$(FAKE_ARRIVE="1194 4500" orch probe 2>&1) || fail "probe failed: $out"
[[ "$(grep -v '^\[' <<<"$out")" == $'4500\n1194' ]] || fail "probe should list arrived ports in candidate order, got: $out"
grep -qF "[PASS] found the collector-nms container with $A/k3sdata/agent/etc/crictl.yaml" <<<"$out" || fail "probe did not try the pod lookup: $out"
[[ -e "$A/root/usr/local/sbin/tb-wg" || -e "$B/root/usr/local/sbin/tb-wg" ]] && fail "probe installed the tool"
[[ -d "$A/root/etc/wireguard" || -d "$B/root/etc/wireguard" ]] && fail "probe wrote to /etc/wireguard"
grep -q -- "-p -n -l" "$A/fake/calls" || fail "udp-listen must run tcpdump without promiscuous mode"
ncalls() { { cat "$A/fake/calls" "$B/fake/calls" 2>/dev/null || true; } | wc -l; }
calls_before=$(ncalls)
if out=$(FAKE_ARRIVE=51820 orch apply 2>&1); then fail "apply ran without --yes"; fi
grep -q "pass --yes" <<<"$out" || fail "apply without --yes did not ask for it: $out"
[[ "$(ncalls)" == "$calls_before" ]] || fail "apply without --yes reached a host"
lock_off
if out=$(FAKE_ARRIVE=51820 orch apply --yes 2>&1); then fail "apply ran without the testbed lock held"; fi
grep -q "is not held" <<<"$out" || fail "apply without the lock did not say so: $out"
if orch rollback --yes >/dev/null 2>&1; then fail "rollback ran without the lock held"; fi
if orch add-net 192.168.132.0/24 >/dev/null 2>&1; then fail "add-net ran without the lock held"; fi
lock_on
if out=$(FAKE_ARRIVE=51820 ORCH_LOCK= orch apply --yes 2>&1); then fail "apply ran without TB_LOCK_FILE"; fi
grep -q "set TB_LOCK_FILE" <<<"$out" || fail "apply without TB_LOCK_FILE did not say so: $out"
[[ "$(ncalls)" == "$calls_before" ]] || fail "apply without the lock reached a host"
if out=$(FAKE_ARRIVE="" orch apply --yes 2>&1); then fail "apply went on with no passing port"; fi
grep -q "decision 8" <<<"$out" || fail "no-port message does not point at decision 8: $out"
[[ -e "$A/fake/link-wg0" ]] && fail "apply started the tunnel with no passing port"
out=$(FAKE_ARRIVE="4500 51821" orch apply --yes 2>&1) || fail "apply failed:"$'\n'"$out"
grep -q "using 51821" <<<"$out" || fail "apply did not take the first arrived candidate: $out"
grep -qx "ListenPort = 51821" "$A/root/etc/wireguard/wg0.conf" || fail "119 port"
grep -qx "Endpoint = 127.0.0.1:51821" "$B/root/etc/wireguard/wg0.conf" || fail "66 endpoint port"
for k in "$(priv_of a8)" "$(priv_of b8)" "$(pub_of a8)" "$(pub_of b8)"; do
  grep -qF "$k" <<<"$out" && fail "a key appeared in the apply output"
  grep -rqF "$k" "$TMP_ROOT/runs-t8a" && fail "a key was written to the run directory"
done
grep -q "\[PASS\] 119: only wg0 and TBWG lines added" <<<"$out" || fail "apply did not compare 119"
grep -q "\[PASS\] 66: only wg0 and TBWG lines added" <<<"$out" || fail "apply did not compare 66"
grep -q "Recording may start after" <<<"$out" || fail "apply did not state the 2h30 rule time"
[[ "$(stat -c %a "$A/root/etc/wireguard/tb-wg.env")" == 600 ]] || fail "env file mode"
[[ -e "$A/root/var/lib/tb-wg/installed-wireguard-tools" ]] || fail "119 package not marked"
out=$(orch verify --target 192.168.123.254 2>&1) || fail "verify failed:"$'\n'"$out"
grep -qF "[PASS] ping 192.168.123.254 from the collector-nms pod" <<<"$out" || fail "verify did not pass the target to 119"
out=$(orch verify --target 192.168.123.254:22 2>&1) || fail "verify with a port failed:"$'\n'"$out"
grep -qF "[PASS] tcp 192.168.123.254:22 from the collector-nms pod" <<<"$out" || fail "verify did not pass the target port"
grep -qF "[PASS] uplink eth9 matches the route toward 127.0.0.1" <<<"$out" || fail "66 check did not compare the pinned uplink:"$'\n'"$out"
out=$(FAKE_ROUTE_DEV=eth7 orch verify 2>&1) && fail "verify passed with the route moved to another interface"
grep -qF "[FAIL] uplink eth9 is pinned but the route toward 127.0.0.1 now leaves by eth7" <<<"$out" || fail "moved route not reported:"$'\n'"$out"
FAKE_NO_ROUTE=1 PATH="$SHIM:$PATH" fake-ssh "$B" "systemctl disable --now wg-quick@wg0 && systemctl enable --now wg-quick@wg0" >/dev/null 2>&1 \
  || fail "66 could not restart its tunnel without the route although the uplink is pinned"
[[ "$(head -1 "$B/fake/ipt/mangle/TBWG-FWD")" == "-i wg0 -o eth9 -j DROP" ]] || fail "66 guard after a boot without the route"
B2=$TMP_ROOT/t8c; new_host "$B2" c8
if run_on "$B2" pin-uplink >/dev/null 2>&1; then fail "pin-uplink wrote without a host env file"; fi
out=$(orch add-net 192.168.131.0/24 2>&1) || fail "orchestrated add-net failed: $out"
grep -q 192.168.131.0/24 "$A/root/etc/wireguard/wg0.nets" && grep -q 192.168.131.0/24 "$B/root/etc/wireguard/wg0.nets" || fail "add-net did not reach both ends"
if orch rollback >/dev/null 2>&1; then fail "rollback ran without --yes"; fi
out=$(orch rollback --yes 2>&1) || fail "rollback failed:"$'\n'"$out"
grep -q "\[PASS\] 119 has no wg0 route or TBWG rule left" <<<"$out" || fail "rollback 119"
grep -q "\[PASS\] 66 has no wg0 route or TBWG rule left" <<<"$out" || fail "rollback 66"
grep -q "\[PASS\] 119 matches the snapshot taken before apply" <<<"$out" || fail "rollback did not compare with the apply snapshot"
[[ "$(grep 'disable --now' "$TMP_ROOT/order-t8a" | awk '{print $1}' | paste -sd' ')" == "b8 a8" ]] \
  || fail "rollback must stop 66 (the side that dials) before 119: $(cat "$TMP_ROOT/order-t8a")"
[[ "$(grep 'enable --now' "$TMP_ROOT/order-t8a" | awk '{print $1}' | paste -sd' ')" == "a8 b8" ]] \
  || fail "apply must start 119 (the side that listens) before 66"
# The uplink is pinned on 66 at apply: a boot without the route still builds the guard, and check
# fails when the route later leaves by another interface.
grep -qx 'WG_UPLINK_66=${WG_UPLINK_66:-eth9}' "$B/root/etc/wireguard/tb-wg.env" || fail "66 uplink not pinned: $(grep UPLINK "$B/root/etc/wireguard/tb-wg.env")"
grep -q '^WG_UPLINK_66=' "$SCRIPT_DIR/vm-tunnel/tunnel.env" && ! grep -q 'eth9' "$SCRIPT_DIR/vm-tunnel/tunnel.env" || fail "pin-uplink touched the repository copy"
ok "orchestrator probe, apply, verify, add-net, rollback"

# ---- 9. apply rolls back by itself when a check or the comparison fails ----------------------
for case in stray vanish nohs nopod sshdown snapfail; do
  A=$TMP_ROOT/t9a-$case; B=$TMP_ROOT/t9b-$case; new_host "$A" a9; new_host "$B" b9; : >"$B/fake/pkg"
  echo 192.168.200.66 >"$A/lan"; echo 127.0.0.1 >"$B/lan"
  if [[ "$case" == stray ]]; then
    out=$(FAKE_STRAY=1 FAKE_ARRIVE=51820 orch apply --yes 2>&1) && fail "apply succeeded although a stray rule appeared"
    want="lines added that are not about wg0 or TBWG chains"
  elif [[ "$case" == vanish ]]; then
    out=$(FAKE_VANISH=1 FAKE_ARRIVE=51820 orch apply --yes 2>&1) && fail "apply succeeded although an existing rule disappeared"
    want="119: lines gone from routes and top level chains"
    grep -qF "filter -A FORWARD -j KUBE-ROUTER-FORWARD" <<<"$out" || fail "apply did not show the line that disappeared"
  elif [[ "$case" == nohs ]]; then
    out=$(FAKE_HS=never FAKE_ARRIVE=51820 orch apply --yes 2>&1) && fail "apply succeeded without a handshake"
    want="no handshake within"
  elif [[ "$case" == nopod ]]; then
    rm -f "$A/k3sdata/agent/etc/crictl.yaml"
    out=$(FAKE_ARRIVE=51820 orch apply --yes 2>&1) && fail "apply succeeded although check cannot find the pod"
    grep -q "nothing changed yet" <<<"$out" || fail "apply did not stop before touching the hosts: $out"
    [[ -e "$A/root/etc/wireguard" || -e "$A/root/usr/local/sbin/tb-wg" ]] && fail "apply touched 119 although the pod lookup failed"
    continue
  elif [[ "$case" == sshdown ]]; then
    : >"$B/ssh-down"   # 66 stops answering once its tunnel is up
    out=$(FAKE_ARRIVE=51820 orch apply --yes 2>&1) && fail "apply succeeded with 66 unreachable"
    grep -qF "could not take 66 down" <<<"$out" || fail "apply did not report the unreachable 66:"$'\n'"$out"
    [[ -e "$A/fake/link-wg0" || -e "$A/fake/enabled" || -e "$A/fake/ipt/mangle/TBWG-IN" ]] \
      && fail "119 left up because 66 could not be reached"
    grep -q "last change to 119 and 66" <<<"$out" || fail "failed apply did not state the change time ($case)"
    continue
  else
    : >"$B/ssh-snap-fail"   # the after snapshot of 66 dies under set -e
    out=$(FAKE_ARRIVE=51820 orch apply --yes 2>&1) && fail "apply succeeded although a snapshot failed"
    want="rolling back"
  fi
  grep -qF "$want" <<<"$out" || fail "apply did not say why ($case):"$'\n'"$out"
  grep -q "rolling back" <<<"$out" || fail "apply did not roll back ($case)"
  [[ -e "$A/fake/link-wg0" || -e "$B/fake/link-wg0" || -e "$A/fake/enabled" || -e "$B/fake/enabled" ]] && fail "tunnel left up after a failed apply ($case)"
  [[ -e "$A/fake/ipt/mangle/TBWG-IN" || -e "$B/fake/ipt/mangle/TBWG-FWD" ]] && fail "chains left after a failed apply ($case)"
  grep -q "last change to 119 and 66" <<<"$out" || fail "failed apply did not state the change time ($case)"
  if [[ "$case" == nohs ]]; then
    grep -q "routes and top level chains are back to what they were" <<<"$out" || fail "rollback was not compared with the before snapshot"
  fi
done
ok "apply rolls back on failure"

# ---- 10. wg-quick fails after PreUp: the guard stays but matches only wg0, and goes away -------
A=$TMP_ROOT/t10a; B=$TMP_ROOT/t10b; new_host "$A" a10; new_host "$B" b10; : >"$B/fake/pkg"
echo 192.168.200.66 >"$A/lan"; echo 127.0.0.1 >"$B/lan"
out=$(FAKE_FAIL_AFTER_PREUP=1 FAKE_ARRIVE=51820 orch apply --yes --no-rollback 2>&1) && fail "apply succeeded although wg-quick failed"
grep -q "left as it is (--no-rollback)" <<<"$out" || fail "apply --no-rollback did not say so:"$'\n'"$out"
[[ -e "$A/fake/link-wg0" ]] && fail "the failed start left wg0"
[[ "$(<"$A/fake/ipt/mangle/PREROUTING")" == "-i wg0 -j TBWG-IN" ]] || fail "leftover 119 jump is not the wg0-only one"
grep -h TBWG "$A"/fake/ipt/mangle/{PREROUTING,FORWARD} | grep -v -- "-i wg0 -j TBWG-IN" && fail "a leftover TBWG jump matches more than wg0"
grep -h TCPMSS "$A/fake/ipt/mangle/FORWARD" | grep -qv -- "-o wg0 " && fail "leftover MSS matches more than wg0"
PATH="$SHIM:$PATH" fake-ssh "$A" "systemctl enable --now wg-quick@wg0" >/dev/null 2>&1 || fail "the next start failed after a failed one"
[[ "$(grep -c TBWG-IN "$A/fake/ipt/mangle/PREROUTING")" == 1 && "$(grep -c TCPMSS "$A/fake/ipt/mangle/FORWARD")" == 1 ]] \
  || fail "the next start stacked rules on the leftovers"
[[ -e "$A/fake/ipt/mangle/TBWG-IN-NEW" ]] && fail "the next start left its build chain"
A=$TMP_ROOT/t10c; B=$TMP_ROOT/t10d; new_host "$A" a10c; new_host "$B" b10d; : >"$B/fake/pkg"
echo 192.168.200.66 >"$A/lan"; echo 127.0.0.1 >"$B/lan"
out=$(FAKE_FAIL_AFTER_PREUP=1 FAKE_ARRIVE=51820 orch apply --yes 2>&1) && fail "apply succeeded although wg-quick failed"
grep -q "rolling back" <<<"$out" || fail "apply did not roll back a start that failed after PreUp"
grep -l "TBWG\|wg0" "$A"/fake/ipt/mangle/* "$B"/fake/ipt/mangle/* && fail "rollback left the PreUp guard behind"
grep -q "routes and top level chains are back to what they were" <<<"$out" || fail "rollback after a failed start not back to before"
ok "a start that fails after PreUp"

echo "test-vm-tunnel: all passed"
