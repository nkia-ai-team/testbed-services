#!/bin/bash
# 66 하이퍼바이저에 실행기 계정, 강제 명령 키, sudoers, 도우미 둘, rsyslog 거르기를 둔다(설계 docs/spec-testbed-vm-3tier.md
# §6-1 "66 호스트 권한", §6-2 "66 호스트", 결정 2). 순서, 확인, 되돌리기는 docs/runbook-civil-petition-apply.md §15.
#
#   install-hv-helper.sh render  --out <폴더>                  설치할 파일을 폴더에 만든다(66 을 건드리지 않음, 시험과 검토용)
#   install-hv-helper.sh install --pubkey <공개 키 파일>        66 에서 root 로. 이미 같은 것은 다시 쓰지 않는다
#   install-hv-helper.sh check                                  66 에서 root 로. 항목마다 PASS 나 FAIL 한 줄, 하나라도 FAIL 이면 끝 코드 1
#   install-hv-helper.sh remove                                 66 에서 root 로. install 이 놓은 것을 모두 지운다(sudo 꾸러미는 남김)
#
# 이 스크립트와 같은 폴더의 t4-hv-exec, t4-hv-exec-root(build.py 가 인벤토리로 이 폴더에 만든 설치본), sudoers-hvjob,
# rsyslog-05-hvjobs.conf 를 쓴다.
# 계정 이름, uid, 러너 주소는 아래 값(러너 계약, 결과 T4S.md 맨 위)이고 환경변수로만 바꾼다. 키 원문은 화면에 내지 않는다.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
account="${HV_ACCOUNT:-hvjob}"
uid="${HV_UID:-1460}"
runner_addr="${HV_RUNNER_ADDR:-192.168.122.20}"
sbin="${HV_SBIN:-/usr/local/sbin}"
sudoers="${HV_SUDOERS:-/etc/sudoers.d/60-hvjob}"
filter="${HV_FILTER:-/etc/rsyslog.d/05-hvjobs.conf}"
logrotate_file="${HV_LOGROTATE:-/etc/logrotate.d/hvjobs}"
forward="/etc/rsyslog.d/60-lucida-forward.conf"

log() { printf '%s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

render_to() {
  local out=$1 re
  mkdir -p "$out"
  re=${runner_addr//./[.]}
  sed -e "s/{{account}}/$account/g" -e "s/{{uid}}/$uid/g" -e "s/{{runner_addr}}/$runner_addr/g" \
      -e "s/{{runner_addr_re}}/$re/g" "$here/rsyslog-05-hvjobs.conf" >"$out/05-hvjobs.conf"
  sed -e "s/{{account}}/$account/g" "$here/sudoers-hvjob" >"$out/60-hvjob"
  printf '/var/log/hvjobs.log {\n\tweekly\n\trotate 4\n\tcompress\n\tmissingok\n\tnotifempty\n\tpostrotate\n\t\t/usr/lib/rsyslog/rsyslog-rotate\n\tendscript\n}\n' \
    >"$out/hvjobs.logrotate"
  printf 'command="%s/t4-hv-exec",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,from="%s" ' \
    "$sbin" "$runner_addr" >"$out/authorized_keys.prefix"
  install -m 0755 "$here/t4-hv-exec" "$out/t4-hv-exec"
  # 설치본은 build.py 가 인벤토리로 이 폴더에 만들어 둔다(저장소에는 없다, runbook §15-1 차례 2).
  [[ -s "$here/t4-hv-exec-root" ]] || fail "$here/t4-hv-exec-root 없음(build.py --inventory <인벤토리> --out 으로 먼저 만든다)"
  install -m 0755 "$here/t4-hv-exec-root" "$out/t4-hv-exec-root"
}

same() { [[ -e "$2" ]] && cmp -s "$1" "$2"; }

# 순서: 거르기 파일을 맨 먼저 놓고 rsyslog 가 실제로 그 줄을 돌리는지 본 뒤에야 계정을 만든다. 계정 추가 줄
# (useradd 의 new user: name=<계정>, UID=<uid>)과 그 뒤 모든 줄이 66 의 관제 서버 전달보다 먼저 걸러져야 runbook §15-1
# 차례 8 의 기대(119 에 0 줄)가 맞는다(리뷰 rv-t4s M1).
do_install() {
  local pubkey=$1 stage home
  [[ "$(id -u)" == 0 ]] || fail "root 로 돈다"
  [[ -r "$pubkey" ]] || fail "공개 키 파일 없음: $pubkey"
  [[ "$(wc -l <"$pubkey")" -le 1 ]] || fail "공개 키 파일에 줄이 둘 이상"
  grep -Eq '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9-]+) [A-Za-z0-9+/=]+( .*)?$' "$pubkey" || fail "공개 키 꼴이 아님"
  [[ -e "$forward" ]] || fail "$forward 없음(66 의 관제 서버 전달 설정이 바뀌었다, 거르기 순서를 다시 본다)"
  stage=$(mktemp -d)
  trap 'rm -rf "$stage"' RETURN
  render_to "$stage"
  # 1. 거르기
  if ! same "$stage/05-hvjobs.conf" "$filter"; then
    local backup=""
    if [[ -e "$filter" ]]; then backup="$stage/05-hvjobs.conf.prev"; cp -p "$filter" "$backup"; fi
    install -m 0644 "$stage/05-hvjobs.conf" "$filter"
    if ! rsyslogd -N1 >/dev/null 2>"$stage/n1.err"; then
      if [[ -n "$backup" ]]; then install -m 0644 "$backup" "$filter"; else rm -f "$filter"; fi
      cat "$stage/n1.err" >&2
      fail "rsyslogd -N1 이 거르기 파일을 받지 않아 되돌렸다"
    fi
    systemctl restart rsyslog
    log "placed $filter, rsyslog restarted"
  fi
  if ! same "$stage/hvjobs.logrotate" "$logrotate_file"; then install -m 0644 "$stage/hvjobs.logrotate" "$logrotate_file"; fi
  filter_diverts || fail "거르기가 계정 줄을 돌리지 않는다(계정을 만들기 전에 멈춤)"
  log "filter diverts account lines"
  # 2. sudo 꾸러미, 계정, 잠금
  if ! command -v sudo >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q sudo >/dev/null
    log "installed package sudo"
  fi
  if ! id "$account" >/dev/null 2>&1; then
    useradd --uid "$uid" --create-home --shell /bin/bash --comment "" "$account"
    log "added account $account"
  fi
  [[ "$(id -u "$account")" == "$uid" ]] || fail "$account uid 가 $uid 이 아님"
  passwd -S "$account" | awk '{exit !($2=="L"||$2=="NP")}' || passwd -l "$account" >/dev/null
  # 3. 도우미, sudoers, 강제 명령 키
  for f in t4-hv-exec t4-hv-exec-root; do
    if ! same "$stage/$f" "$sbin/$f"; then install -o root -g root -m 0755 "$stage/$f" "$sbin/$f"; log "placed $sbin/$f"; fi
  done
  visudo -cf "$stage/60-hvjob" >/dev/null || fail "sudoers 문법"
  if ! same "$stage/60-hvjob" "$sudoers"; then install -o root -g root -m 0440 "$stage/60-hvjob" "$sudoers"; log "placed $sudoers"; fi
  home=$(getent passwd "$account" | cut -d: -f6)
  install -d -m 0700 -o "$account" -g "$account" "$home/.ssh"
  { cat "$stage/authorized_keys.prefix"; cat "$pubkey"; } >"$stage/authorized_keys"
  if ! same "$stage/authorized_keys" "$home/.ssh/authorized_keys"; then
    install -m 0600 -o "$account" -g "$account" "$stage/authorized_keys" "$home/.ssh/authorized_keys"
    log "placed authorized_keys (forced command)"
  fi
  do_check
}

# 거르기가 계정 이름이 든 줄을 hvjobs.log 로만 돌리는가(줄 하나를 보내 본다).
filter_diverts() {
  local mark
  mark="check-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
  logger -p auth.info "$account $mark"
  sleep 2
  grep -q "$mark" /var/log/hvjobs.log 2>/dev/null && ! grep -q "$mark" /var/log/syslog /var/log/auth.log 2>/dev/null
}

do_check() {
  local bad=0 home line
  pass() { printf 'PASS %s\n' "$*"; }
  nope() { printf 'FAIL %s\n' "$*"; bad=1; }
  if command -v sudo >/dev/null; then pass sudo; else nope "sudo 없음"; fi
  if [[ "$(id -u "$account" 2>/dev/null)" == "$uid" ]]; then pass "account $account uid $uid"; else nope "account $account uid"; fi
  for f in t4-hv-exec t4-hv-exec-root; do
    if same "$here/$f" "$sbin/$f" && [[ "$(stat -c '%U %a' "$sbin/$f")" == "root 755" ]]; then pass "$f $(sha256sum "$sbin/$f" | cut -c1-12)"; else nope "$f 가 저장소 판과 다르거나 root 755 가 아님"; fi
  done
  if [[ -e "$sudoers" ]] && visudo -cf "$sudoers" >/dev/null && [[ "$(stat -c %a "$sudoers")" == 440 ]]; then pass sudoers; else nope sudoers; fi
  home=$(getent passwd "$account" | cut -d: -f6 || true)
  line=$(cat "$home/.ssh/authorized_keys" 2>/dev/null | head -n1 || true)
  if [[ "$line" == "command=\"$sbin/t4-hv-exec\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,from=\"$runner_addr\" "* ]] \
     && [[ "$(wc -l <"$home/.ssh/authorized_keys")" -le 1 ]]; then pass "authorized_keys forced command"; else nope "authorized_keys"; fi
  # 거르기가 전달보다 먼저 읽히는가(파일 이름 순서)와 rsyslog 가 그 파일을 실제로 받는가
  if [[ -e "$filter" ]] && [[ "$(basename "$filter")" < "$(basename "$forward")" ]] && rsyslogd -N1 >/dev/null 2>&1; then
    pass "rsyslog filter before $(basename "$forward")"
  else
    nope "rsyslog filter"
  fi
  # 거르기가 실제로 줄을 돌리는가: 계정 이름이 든 줄 하나를 보내고 hvjobs.log 에만 있는지 본다.
  if filter_diverts; then
    pass "filter diverts account lines"
  else
    nope "filter did not divert the probe line"
  fi
  if [[ -d /sys/module/sch_netem ]]; then pass sch_netem; else nope "sch_netem 적재 안 됨"; fi
  return $bad
}

# 순서: 계정, 도우미, sudoers 를 먼저 지우고 거르기는 맨 끝에 지운다(계정 삭제 줄도 걸러지게, 리뷰 rv-t4s M1).
do_remove() {
  local home
  [[ "$(id -u)" == 0 ]] || fail "root 로 돈다"
  rm -f "$sudoers" "$sbin/t4-hv-exec" "$sbin/t4-hv-exec-root"
  if id "$account" >/dev/null 2>&1; then
    home=$(getent passwd "$account" | cut -d: -f6)
    pkill -u "$account" 2>/dev/null || true
    userdel "$account"
    rm -rf -- "$home"
    log "removed account $account"
  fi
  sleep 2
  rm -f "$logrotate_file"
  if [[ -e "$filter" ]]; then rm -f "$filter"; systemctl restart rsyslog; log "removed $filter, rsyslog restarted"; fi
  log "removed helper files and sudoers (package sudo stays; apt-get remove sudo if it was installed only for this)"
}

case "${1:-}" in
  render) [[ "${2:-}" == --out && -n "${3:-}" ]] || fail "render --out <폴더>"; render_to "$3" ;;
  install) [[ "${2:-}" == --pubkey && -n "${3:-}" ]] || fail "install --pubkey <파일>"; do_install "$3" ;;
  check) do_check ;;
  remove) do_remove ;;
  *) fail "render, install, check, remove 중 하나" ;;
esac
