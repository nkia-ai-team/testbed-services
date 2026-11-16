---
title: 119 와 66 사이 WireGuard 터널 적용 runbook (T0)
status: Planned
owner: project
last_reviewed: 2026-11-16
tags:
  - runbook
  - testbed
  - network
  - wireguard
summary: SMS, NMS 전용 테스트베드(T)와 SMS 전용 테스트베드(S)가 같이 쓰는 119 와 66 사이 WireGuard 터널을 올리고, 확인하고, 내리는 절차. 적용 조건(녹화 정지, 2시간 30분 규칙), 바뀌는 것, 119 k3s 와 수집에 주는 영향, 되돌리기, S 망 추가.
---

# 119 와 66 사이 WireGuard 터널 적용 runbook (T0)

설계 정본은 [SMS, NMS 전용 전통 3계층 테스트베드 설계](spec-testbed-vm-3tier.md) §3-1, §3-4, §3-5, §10 T0 와
[SMS 에이전트만 쓰는 고객사 테스트베드 설계](spec-testbed-sms-only.md) §3-2, §3-5 다. 이 문서는 그 설계를
`scripts/vm-tunnel/` 도구로 적용하는 절차만 적는다. 시각은 KST.

상태: 도구와 시험만 있고 아직 적용하지 않았다(2026-11-16). 적용은 지휘 세션이 정한 적용 에이전트가 녹화가
멈춘 정비 창에서 한다.

## 1. 결론

| 질문 | 답 |
|---|---|
| 무엇이 생기나 | 119 에 `wg0`(10.250.0.1/30)과 `wireguard-tools` 패키지, 66 에 `wg0`(10.250.0.2/30). 66 이 119 로 먼저 건다 |
| 119 가 터널로 닿는 곳 | 192.168.123.0/24~126.0/24(T). S 업무망 192.168.131.0/24 는 ST2 때 `add-net` 으로 더한다 |
| 키 | 각 호스트에서 만들고 `/etc/wireguard/wg0.key`(0600)에만 둔다. `wg0.conf` 에도 키가 없다(PostUp 이 파일에서 읽는다). 공개 키는 제어 호스트의 셸 변수로만 건너간다 |
| 재부팅 뒤 | `wg-quick@wg0` 이 두 호스트에서 enabled 라 다시 선다 |
| 되돌리기 | `t0-apply.sh rollback --yes`(파일은 남김), `--purge` 면 파일과 119 의 패키지까지 지움 |
| 언제 | 녹화가 멈춘 정비 창에서만. 적용 시각이 119 와 66 의 마지막 변경 시각이 되어 녹화 시작은 그 뒤 2시간 30분부터 |

## 2. 읽기 확인 (2026-11-16 20:0x KST, 조회만)

| 확인 | 결과 | 근거 명령 |
|---|---|---|
| 119 커널 모듈 | 6.8.0-147-generic in-tree `wireguard.ko.zst` 1.0.0, 로드 안 됨 | `modinfo wireguard`, `lsmod` |
| 119 도구 | `wg` 없음. `wireguard-tools` 후보 1.0.20210914-1ubuntu4, 설치 모의 결과 새로 깔리는 것은 이 패키지 하나 | `apt-cache policy wireguard-tools`, `apt-get -s install wireguard-tools` |
| 66 커널 모듈과 도구 | 7.0.14-23-pve in-tree 1.0.0, 로드 안 됨. `wg`, `wg-quick` 있음(`wireguard-tools` 1.0.20210914-3), `/etc/wireguard` 빈 폴더 | `modinfo`, `lsmod`, `dpkg -l`, `ls -la /etc/wireguard` |
| 66 과 119 사이 | 서로 핑 5/5, 평균 3.7~5.1ms. 66 → 119 는 192.168.200.1 경유, 119 → 66 은 192.168.230.1 경유(다른 망, 사이에 사내 라우터) | `ping -c 5`, `ip route get` |
| UDP 통과 | 지금 확인된 것은 66 → 119 UDP 514 뿐(z7). 다른 포트는 패킷을 보내야 알 수 있어 이번에는 하지 않았다. `t0-apply.sh probe` 가 적용 창에서 한다 | |
| 119 방화벽 | ufw 꺼짐, INPUT 정책 ACCEPT, FORWARD 정책 DROP(docker 가 바꾼 값). 터널에서 파드로 가는 길은 `FLANNEL-FWD` 의 `-d 10.42.0.0/16 ACCEPT` 가 받는다 | `iptables -S` |
| 119 가 듣는 UDP | 8472(flannel vxlan), 53(루프백), 514. 후보 51820, 51821, 4500, 1194, 443 은 비어 있다 | `ss -Huln` |
| 119 k3s 망 | 파드 10.42.0.0/24(클러스터 10.42.0.0/16), 서비스 10.43.0.0/16(기본값, config 에 덮어쓰기 없음), flannel vxlan MTU 1450. 터널 대역과 겹치지 않는다. 라우팅 표는 기본, 10.42.0.0/24, 172.17~19/16(도커, 내려감), 192.168.230.0/24 뿐 | `k3s kubectl get node -o jsonpath`, `ip route`, `/etc/rancher/k3s/config.yaml` |
| 수집기 파드가 새 대역으로 나가는 길 | 파드(10.42.0.x) → cni0 → 호스트 라우팅(wg-quick 이 AllowedIPs 대역마다 `dev wg0` 경로를 만든다) → FORWARD `FLANNEL-FWD -s 10.42.0.0/16 ACCEPT` → POSTROUTING `FLANNEL-POSTRTG -s 10.42.0.0/16 ! -d 224.0.0.0/4 MASQUERADE` 로 출발지가 wg0 주소 10.250.0.1 이 된다. 설계 §3-2 의 "flannel 마스커레이드 → 출발지 10.250.0.1" 과 같다 | `iptables -t nat -S FLANNEL-POSTRTG`, `iptables -S FLANNEL-FWD` |
| 새 대역에서 119 로 들어오는 길 | SMS 보고 10.250.0.1:18090: `collector-sms-lb` 는 k3s servicelb(svclb 파드 hostPort 18090, `SRC_RANGES=0.0.0.0/0`)라 nat PREROUTING `CNI-HOSTPORT-DNAT`(`--dst-type LOCAL`, 주소 제한 없음)가 받는다. kube-proxy 의 LB 규칙은 192.168.230.119 에만 걸려 있어 터널 주소에는 쓰이지 않는다. syslog 514 는 `collector-syslog` 가 hostNetwork 로 `*:514`(UDP, TCP)에서 듣는다. 파드 쪽으로 넘어가면 출발지를 바꾸지 않는다(`! -s 10.42.0.0/16 -d 10.42.0.0/24 RETURN`) | `iptables -t nat -S`, `ss -Hulnp`, svclb 파드 env |
| NetworkPolicy | polestar 15개 가운데 수집기(sms, nms, syslog, trap, availability-store)를 고르는 것 없음. Egress 를 막는 것은 kdb-parser, query, selfsystem-api 셋뿐 | `k3s kubectl get networkpolicy -A` |
| conntrack | 119 8,010 / 262,144, 66 643 / 262,144 | `sysctl net.netfilter.nf_conntrack_count`, `_max` |
| 66 방화벽 | pve-firewall disabled/running, iptables legacy, FORWARD 정책 ACCEPT 규칙 0, nat 는 `tb-nat` 의 두 줄(122 MASQUERADE `-o vmbr0.200`, 8091 DNAT)뿐, nft 표 없음 | `pve-firewall status`, `iptables -S`, `iptables -t nat -S`, `nft list tables` |
| 119 crictl | k3s 가 `--data-dir /data/lucida-next/deploy/pkg/data` 로 떠 있어 기본 `crictl` 은 설정 파일을 못 찾고 rc 1. `crictl --config <data-dir>/agent/etc/crictl.yaml` 로는 collector-nms 컨테이너 id 와 pid 가 나온다(2회차 조회 rc 0). 도구는 `WG_CRICTL_CONFIG`, 실행 중인 k3s 유닛의 `--data-dir`, `/etc/rancher/k3s/config.yaml` 의 `data-dir`, k3s 기본 순으로 찾는다 | `systemctl show -p ExecStart --value k3s`, `crictl --config ... ps`, `inspect` |
| 119 재시작 대기 | needrestart 3.6 이 깔려 있고 재시작 대기 서비스 6개(dbus, docker 등)와 커널 6.8.0-149 가 대기 중이다(리뷰 rv-t0 조회). apt 훅이 비대화형 apt 에서 이것을 재시작할 수 있어 도구는 `NEEDRESTART_SUSPEND=1` 로 설치, 제거한다. 이 도구는 아무 서비스도 재시작하지 않는다 | `needrestart -b -r l`(리뷰) |
| 119 mangle | PREROUTING 은 비어 있고 FORWARD 도 규칙 없음. k3s 가 mangle 에 두는 것은 KUBE-IPTABLES-HINT, KUBE-KUBELET-CANARY, KUBE-PROXY-CANARY 빈 체인뿐 | `iptables -t mangle -S` |
| 66 syslog | 66 의 rsyslog 가 자기 syslog 전부를 119:514 UDP 로 보낸다(`60-lucida-forward.conf`). wg-quick 이 찍는 줄, 커널의 wireguard 적재 줄, ssh 로그인 줄이 119 에 남는다 | `/etc/rsyslog.d/60-lucida-forward.conf` |

적용 전에 알아 둘 것(T3 몫): 119 의 `collector-trap` 은 파드가 떠 있어도 UDP 162 를 듣지 않는다. 로그가
"포트는 등록된 수신기가 연다" 라고 적는다. 라우터 trap 을 받으려면 T3 에서 제품에 trap 수신기를 등록해야
한다. 터널 쪽 허용(udp 162)은 미리 열어 둔다.

## 3. 도구

| 파일 | 하는 일 |
|---|---|
| `scripts/vm-tunnel/tunnel.env` | 주소, 대역, 후보 포트, 119 가 터널에 여는 포트, 판정 한도. 키 없음 |
| `scripts/vm-tunnel/tb-wg.sh` | 각 호스트에서 도는 쪽(`/usr/local/sbin/tb-wg` 로 설치). keygen, render, hook-up, hook-down, add-net, del-net, check, snapshot, remove, udp-listen, udp-send |
| `scripts/vm-tunnel/t0-apply.sh` | 제어 호스트(104)에서 두 호스트를 ssh 로 다룬다. probe, apply, verify, add-net, rollback |
| `scripts/test-vm-tunnel.sh` | 호스트 도구를 모두 가짜로 바꾼 시험(망을 건드리지 않음) |

호스트에 더하는 규칙(자기 체인과 `wg0` 에만 걸리는 점프, 기본 정책은 그대로):

| 호스트 | 규칙 | 이유 |
|---|---|---|
| 119 | mangle `TBWG-IN`: 맺은 연결의 응답, 10.250.0.1 로 오는 echo-request, 10.250.0.1 의 tcp 18090, 514 와 udp 514, 162 만 RETURN, 나머지 DROP. mangle PREROUTING 맨 앞의 `-i wg0` 점프 하나 | 테스트베드 VM 이 119 의 NodePort, 다른 hostPort(평가용 PG, CH, VM LB 포함), 호스트 서비스(ssh, etcd, API, kubelet)에 닿지 않게 한다. PREROUTING 은 DNAT 전이라 터널 주소로 걸어 온 포트 그대로 판정한다. filter 표에 두면 kube-proxy 가 기동할 때마다 자기 점프를 맨 앞에 넣고 `KUBE-FORWARD` 가 NodePort 흐름을 먼저 ACCEPT 해 거르기를 건너뛴다(재부팅 때 wg-quick 이 k3s 보다 먼저 끝나므로 늘 그렇게 된다). mangle 은 k3s, kube-proxy, kube-router, flannel, docker 가 손대지 않고, 거기서 DROP 하면 그대로 끝난다 |
| 119 | mangle FORWARD `-o wg0` SYN 의 MSS 를 경로 MTU 로 | 파드 MTU 1450 이 터널 1420 보다 크다(설계 §3-4) |
| 66 | mangle `TBWG-FWD`: 맨 앞 두 줄이 wg0 과 상향(사내망 쪽 인터페이스, `WG_UPLINK_66`, 비면 119 로 가는 경로의 dev, 지금 vmbr0.200) 사이 DROP, 그다음 대역마다 10.250.0.1 과 그 대역 사이 양방향만 RETURN, 나머지 DROP. mangle FORWARD 맨 앞에 `-i wg0`, `-o wg0` 점프 | 설계 §3-4 의 "wg0 과 다른 망(122, 127, vmbr0.200) 사이 거절" 을 터널과 같이 걸고 같이 지운다. 상향 짝 DROP 은 T2 의 브리지가 내려가 있을 때 119 의 폴링(목적지 123~126)이 66 기본 경로로 사내망에 나가는 것과, 사내망 쪽에서 테스트베드 출발지를 속여 119 로 들어오는 것을 막는다. **소유: wg0 규칙과 MSS 는 `tb-wg` 만 다룬다.** `t4-net`(S 의 `s5-net`)은 filter 표에서 브리지 쪽만 맡고 wg0 규칙과 MSS 를 두지 않으며 ACCEPT 를 wg0 새 흐름에 맞게 쓰지 않는다(설계 문서에 적음). mangle 에 두므로 `t4-net` 의 체인이 filter FORWARD 맨 앞에 와도 이 거르기를 건너뛰지 못한다 |
| 66 | mangle FORWARD MSS 같은 규칙 | 같은 이유 |

`wg-quick` 이 AllowedIPs 대역마다 경로를 만든다. 119 는 10.250.0.2/32 와 터널 대역, 66 은 10.250.0.1/32 뿐이라
66 에는 테스트베드 대역 경로가 새로 생기지 않는다(그 경로는 T2 의 `t4-net` 이 라우터로 둔다). netplan, systemd-networkd,
`/etc/network/interfaces` 는 건드리지 않는다.

거르기는 wg-quick 의 PreUp 에서 걸고(규칙은 wg0 을 이름으로 맞추므로 링크 전에 걸 수 있다) PostDown 에서 지운다.
그래서 wg0 이 있는데 거르기가 없는 순간이 없다. 키를 넣는 줄만 PostUp 이다. 거르기 체인을 고칠 때(재기동, add-net)는 새 체인(`-NEW`)을 다 채운 뒤 그 점프를 넣고, 옛 점프와 옛 체인을 지운 다음
새 체인 이름을 바꾼다. 비거나 반쯤 찬 거르기를 지나는 패킷이 없다.

66 의 PVE 방화벽(`pve-firewall` disabled, `proxmox-firewall` active 이나 규칙 없음)을 나중에 켜면 PVEFW 체인이
filter 에 생긴다. 거르기는 mangle 이라 영향이 없지만, 켜기 전에 `t0-apply.sh verify` 를 한 번 돌린다.

## 4. 적용 조건

1. 녹화가 진행 중이 아니고, 운영 루프가 멈춰 있다(PAUSE). 터널 적용 줄과 ssh 로그인 줄이 66 syslog 를 거쳐
   119 에 남으므로 녹화 구간과 겹치면 안 된다.
2. 2시간 30분 규칙: 적용이 끝난 시각이 119 와 66 의 마지막 변경 시각이 된다. 녹화 시작은 그 뒤 2시간 30분부터다.
   `apply` 가 끝에 그 시각을 찍는다. 상황판의 `last_119_change`, `last_66_change` 를 고친다.
3. 테스트베드 잠금(`locks/testbed`)을 잡고 한다. 도구가 확인한다: apply, add-net, rollback 은 `TB_LOCK_FILE` 이 가리키는 잠금을 부른 쪽이 잡고 있지 않으면 거절한다. 같은 창에 T2 의 66 망 변경이 있으면 한 창에 묶어 차단을 한 번만 받는다.
4. 제어 호스트에 `sshpass` 와 `~/.config/testbed/ssh-192.168.230.119`, `ssh-192.168.200.66` 비밀번호 파일이 있다.
   파일 내용은 화면에 내지 않는다. `TB_SSH_119`, `TB_SSH_66` 을 직접 줄 때 `sshpass -p <비밀번호>` 는 쓰지 않는다
   (값이 프로세스 인자와 셸 기록에 남는다). `-f <파일>` 이나 키만.
5. 119 의 재시작 대기 서비스와 대기 커널(6.8.0-149)은 이 작업에서 건드리지 않는다. 다음 119 재부팅 때 커널이 바뀐다.

## 5. 순서

```
cd <testbed-services>/scripts/vm-tunnel
export TB_LOCK_FILE=<ops>/locks/testbed
./t0-apply.sh probe                                      # 0. 통하는 포트와 파드 찾기(호스트에 쓰기 없음)
flock "$TB_LOCK_FILE" ./t0-apply.sh apply --yes          # 1~7
./t0-apply.sh verify                                     # 언제든 다시
```

`apply` 가 하는 일(실행 기록은 `~/.local/state/tb-wg/<KST 시각>-apply/`):

1. 두 호스트의 경로와 최상위 체인 스냅숏(설치 전, 도구를 표준 입력으로 흘려 돌려 파일을 쓰지 않는다). 그리고
   119 에서 `pod-check`(읽기만): check 가 쓸 collector-nms 컨테이너를 crictl 설정으로 찾는다. 못 찾으면 아무것도
   바꾸기 전에 멈춘다.
2. 포트: `--port` 가 없으면 probe. 119 에서 `tcpdump -p`(무차별 모드 없음)를 20초 걸고 66 에서 후보 포트마다
   작은 UDP 3개를 보낸다. 도착한 것 가운데 후보 순서로 첫 포트를 쓴다. **하나도 안 오면 멈춘다**(결정 8: 제품 원격
   수집 워커 안으로 가고 S 는 결정 1 의 (나)로 내려간다. 사람 판단).
3. 도구와 `tunnel.env` 를 두 호스트에 설치. 119 에 `wireguard-tools` 설치(설치했다는 표시를 남겨 purge 때만 지움).
   66 은 이미 있어 설치하지 않는다(없으면 멈춤).
4. 각 호스트에서 키 생성, 공개 키를 상대에게 넘겨 `wg0.conf` 작성.
5. 119 먼저(듣는 쪽), 그다음 66 에서 `systemctl enable --now wg-quick@wg0`. 66 의 핸드셰이크를 90초까지 기다린다.
6. 두 호스트 `tb-wg check`, 스냅숏 다시 떠서 비교: 사라진 줄 0, 더해진 줄은 모두 `wg0` 이나 `TBWG-` 를 포함.
7. 호스트를 건드린 뒤에는 성공 말고 어떤 길로 끝나도(check 실패, 비교 실패, ssh 나 스냅숏이 죽음) 스스로 되돌린다
   (`--no-rollback` 으로 끌 수 있다). 66 먼저, 그다음 119 이고, 한쪽 ssh 가 안 되어도 다른 쪽은 되돌리고 손으로 할
   호스트를 알린다.

`check` 판정(설계 §10 T0, §6-4):

| 항목 | 기준 |
|---|---|
| 유닛 | `wg-quick@wg0` enabled 이고 active, `wg0` 있음 |
| 핸드셰이크 | 마지막 핸드셰이크 150초 이내 |
| 터널 핑 | 상대 터널 주소 10회 손실 0, 평균이 같은 순간의 사내망 경로 평균 + 2ms 이하, 최대 20ms 이하. 사내망과 터널을 번갈아 세 번까지 재고 세 번 모두 넘을 때만 FAIL(한 번 흔들림으로 되돌리지 않게). 손실은 한 번이라도 FAIL |
| 경로(119) | 대역마다 `dev wg0` 경로 |
| 거르기 | 점프가 mangle 의 그 훅(119 PREROUTING, 66 FORWARD)에서 맨 앞 규칙이고(위에 다른 규칙이 오면 FAIL), 체인 마지막 줄이 DROP, 66 은 상향 짝 DROP 이 RETURN 들보다 앞, MSS 있음 |
| conntrack | 사용 80% 이하 |
| 수집기 쪽(119) | `collector-nms` 컨테이너의 망 이름공간에서 상대 터널 주소로 핑 손실 0 과 TCP 9 번 포트 연결이 맺히거나 거절됨(거절도 길이 양쪽으로 열렸다는 뜻. 66 에 듣는 서비스가 없어 로그 줄이 생기지 않는다). `--target IP` 는 핑만, `--target IP:PORT` 는 그 포트로 TCP 도 본다. 대상이 그 포트에 응답(연결이나 거절)해야 PASS 다 |

## 6. 119 k3s 와 수집에 주는 영향

| 무엇 | 영향 | 근거 |
|---|---|---|
| 기존 파드, 서비스 경로 | 없음 | 새 경로는 192.168.123~126(그리고 131)/24 와 10.250.0.0/30 뿐, 10.42/16, 10.43/16, 172.17~19/16, 192.168.230/24 와 겹치지 않음 |
| k3s 가 다시 쓰는 규칙 | 서로 건드리지 않음 | 우리 규칙은 mangle 의 자기 체인과 `-i wg0`, `-o wg0` 점프뿐이다. k3s 는 filter 와 nat 를 다시 쓰고 mangle 에는 빈 표시 체인만 둔다. 거르기가 맨 앞인지는 check 가 본다 |
| flannel | 없음 | flannel 인터페이스는 기동 때 고른 enp1s0 그대로, 노드 InternalIP 192.168.230.119 그대로 |
| conntrack | 무시할 만함 | 터널 바깥 UDP 흐름 1개, 안쪽은 SNMP 폴링, 핑, 에이전트 TCP 몇 개, syslog 흐름 몇 개(T, S 합 수십). 지금 8,010 / 262,144 |
| 수집 | 등록 전에는 없음 | 터널 대역에 등록된 대상이 없으므로 수집기가 할 일이 늘지 않는다. 대상 등록은 T3 |
| 패키지 | `wireguard-tools` 하나 | 의존 패키지 없음(설치 모의) |
| 로그 | 66 의 wg-quick 줄, 커널 적재 줄, ssh 로그인 줄이 119 syslog 에 | 녹화 정지 창이라 녹화에 들어가지 않는다 |

66 쪽: 기존 테스트베드(192.168.122.0/24, `tb-nat`)와 FORWARD 기본 정책은 그대로다. 66 이 테스트베드 대역 경로를
새로 갖지 않으므로 지금은 터널을 지나는 테스트베드 흐름이 없다(T2 에서 생긴다).

## 7. 되돌리기

```
./t0-apply.sh rollback --yes            # 66 먼저 내리고 119. 파일, 키, 패키지는 남는다
./t0-apply.sh rollback --yes --purge    # 파일, 키, 도구, 119 의 wireguard-tools 까지 지운다(66 패키지는 원래 있던 것이라 그대로)
```

`rollback` 은 내린 뒤 스냅숏을 떠서 `wg0`, `TBWG-` 줄이 남지 않았는지, 마지막 apply 전 스냅숏과 같은지 본다.
손으로 할 때: 각 호스트에서 `systemctl disable --now wg-quick@wg0`(PreDown 이 규칙을 지운다), 남은 것이 있으면
`tb-wg hook-down`. 커널 모듈은 내려도 남는다(해 없음, 재부팅 때 사라짐).

재부팅 뒤 확인(다음 119 또는 66 정비 재부팅 때 `t0-apply.sh verify`): 유닛이 다시 섰는지, 거르기 점프가 mangle 의
맨 앞에 있는지(check 가 본다), 119 커널이 6.8.0-147 에서 6.8.0-149 로 바뀌었는지(wireguard 는 커널 안 모듈이라 그대로
적재될 것으로 본다, 추정).

## 8. S 업무망 더하기 (ST2)

```
./t0-apply.sh add-net 192.168.131.0/24
```

119 는 피어 AllowedIPs 를 그 자리에서 바꾸고 경로를 더하며 `wg0.conf` 도 고친다(터널을 다시 세우지 않는다). 66 은
`TBWG-FWD` 에 그 대역 쌍을 더한다. /24 하나만 받고, 122, 127, 132, 133, 200, 213, 230 대역과 묶은 대역은 거절한다.
119 설정 변경이라 2시간 30분 규칙의 마지막 변경으로 센다.

## 9. T2 뒤 확인

T0 완료 조건의 "새 망 VM 핑, SNMP" 는 VM 이 있어야 한다. T2 가 라우터를 세운 뒤:

```
./t0-apply.sh verify --target 192.168.123.254:<라우터 상향이 응답하는 tcp 포트>
```

로 119 호스트와 `collector-nms` 파드에서 라우터 상향 주소 핑과 TCP 도달을 본다. 라우터 nftables 가 10.250.0.1 에서
온 그 포트를 DROP 하면 FAIL 이므로, T2 는 라우터 상향 주소가 10.250.0.1 에서 온 tcp 한 포트(예를 들지 않음, T2 가
정함)에 응답이나 거절(RST)을 주게 하거나 포트 없이 `--target 192.168.123.254` 로 핑만 본다. S 서버는 10.250.0.1 의
ICMP 만 받으므로(S 문서 §3-3) 포트 없이 준다. SNMP 는 T3 의 NMS 등록 뒤
`nms.network.snmp_status` 가 1 인지로 본다(제품이 직접 SNMP 를 묻는 값이라 손으로 만든 SNMP 요청보다 확실하다).

## 10. 시험

```
bash scripts/test-vm-tunnel.sh </dev/null
```

호스트 도구를 PATH 앞의 가짜로 모두 바꿔 실제 망을 건드리지 않는다. 가짜 systemctl 이 wg-quick 처럼 PostUp,
PreDown 을 실제로 돌려 규칙 코드가 가짜 iptables 위에서 돈다. 키가 conf, 출력, 실행 기록에 없음, 규칙의 모양과
멱등, 거르기가 mangle 맨 앞이고 k3s 가 filter 에 점프를 쌓아도 그대로임, add-net 이 새 체인을 채운 뒤 바꿈, crictl 이
설정 없이는 실패하는 가짜로 설정 경로 찾기, check 의 각 실패(세 번 재는 RTT 포함), `NEEDRESTART_SUSPEND=1`, 패키지를
설치한 쪽만 지움, 잠금 확인, probe 포트 고르기, 실패 때 스스로 되돌림(66 ssh 가 끊겨도 119 를 되돌림, 스냅숏이 죽어도
되돌림), 되돌리는 순서(66 먼저), 66 FORWARD 판정(브리지가 없을 때 wg0 에서 상향으로 나가는 폴링과 상향에서 속인 출발지 DROP, 122, 127 DROP, 정상 폴링과 응답 ACCEPT), 거르기가 링크보다 먼저 서고 나중에 지워짐을 본다.
