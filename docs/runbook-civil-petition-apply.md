---
title: 전통 3계층 테스트베드(civil-petition) 적용 runbook
status: Planned (적용 전, 코드와 적용물만 준비됨)
owner: project
last_reviewed: 2026-11-18
tags:
  - runbook
  - testbed
  - sms
  - nms
summary: SMS, NMS 전용 전통 3계층 테스트베드(T)와 같은 코드를 쓰는 S 테스트베드를 66 에 짓는 순서. 66 망 파일, VM 생성, 서버별 설치, 부하 시작, T2 완료 조건 점검, 평시 24시간과 규모 축 판정, SMS 에이전트 설치 자리, 되돌리기, S 에서 다른 단계(§14).
---

# 전통 3계층 테스트베드(civil-petition) 적용 runbook

설계 정본은 [SMS, NMS 전용 전통 3계층 테스트베드 설계](spec-testbed-vm-3tier.md)(이하 "설계")와 [SMS 에이전트만 쓰는 고객사 테스트베드 설계](spec-testbed-sms-only.md)(이하 "S 문서")다. 코드는 저장소 `civil-petition/` 하나이고, 테스트베드별 값은 `civil-petition/deploy/inventory/t.json`, `s.json` 둘뿐이다. 이 runbook 은 T 를 기준으로 적고 S 에서 다른 단계는 §14 한 곳에 모아 적는다.

**이 runbook 의 모든 단계는 적용 에이전트가 지휘 세션의 지시로 한다.** 66 에 닿는 단계(망 파일, VM 생성, 이름 바꾸기, 스냅숏)는 테스트베드 잠금과 녹화 정지 창 아래에서만 한다(설계 §11). 시각은 KST 로 적는다.

## 1. 결론

| 질문 | 답 |
|---|---|
| 무엇을 하나 | 66 망 파일 하나와 VM 6대를 더하고, 서버마다 묶음 하나로 설치한 뒤 부하를 켠다 |
| 순서 | 비밀값 파일 → 66 망 → VM 생성 → 라우터 → DB → WAS 두 대 → 웹 → 부하기 → Proxmox 이름 바꾸기 → 점검 → 부하 시작 → 24시간 판정 → 규모 축 → 스냅숏 |
| 시간 | 설치 약 2시간(DB 시드 포함), 평시 판정 24시간, 규모 축 30분 |
| 66 자원 | vCPU 14, 메모리 22GB, 디스크 256GB(thin). 할당 26 → 40 vCPU(48 스레드), 메모리 여유 약 183GB → 약 161GB |
| 되돌리기 | §10. 사람 승인 뒤 사람이 66 에서 직접 |

## 2. 무엇이 어디에 있나

| 경로 | 내용 |
|---|---|
| `civil-petition/petition-web/` | WAR 소스(Spring Boot 3.4, 독립 Tomcat 10.1 배포, JNDI 데이터소스). `mvn -B package` 로 `petition-web/target/petition.war` |
| `civil-petition/db/schema.sql`, `seed.sql` | 스키마와 결정적 시드(민원 200만, 처리 이력 약 450만, 사용자 5만, 부서 300. 곧 정상 상태 꼴, 설계 §2-3) |
| `civil-petition/loadgen/` | k6 스크립트와 진입 스크립트(하루 모양은 기존 도메인과 같은 표) |
| `civil-petition/deploy/inventory/` | 테스트베드별 값(주소, 이름, 도메인 값, 켜기 값, 비밀값 파일 경로) |
| `civil-petition/deploy/render.py` | 인벤토리 → 서버별 묶음, 66 망 파일 |
| `civil-petition/deploy/templates/` | 설정 틀(주소와 이름을 쓰지 않는다) |
| `civil-petition/deploy/steps/`, `install-host.sh` | VM 안 설치 단계 |
| `civil-petition/deploy/apply.sh` | 묶음을 만들어 관리망으로 보내고 설치 |
| `civil-petition/deploy/provision/plan.py` | 66 Proxmox 명령을 찍기만 한다(실행 안 함) |
| `civil-petition/deploy/router/ifdiscards.py` | 라우터 ifOutDiscards 도우미(GT0 결정 4) |
| `civil-petition/deploy/tools/` | 평시 판정(`stability_check.py`, `host-facts.sh`, `db-facts.sh`), 규모 축 판정(`scale_verdict.py`, `cpu-sample.sh`), 라우터 큐 버림 실기 점검, 적용 전 rsyslog 설정 확인(`rsyslog-check.sh`), T 와 S 짝 서버의 패키지 판 대조(`pair_packages.py`, §14) |

## 3. 비밀값 파일 (적용 단계에서 적용 에이전트가 만든다)

지금은 하나도 없다. 값은 화면에 내지 않는다(아래 명령은 값을 출력하지 않는다). 모든 파일은 `~/.config/testbed/` 아래 0600, 디렉터리는 0700. 인벤토리는 경로만 적는다. 개인 키는 만든 호스트 밖으로 복사하지 않는다.

| 파일(경로) | 권한 | 만드는 명령 | 쓰는 곳 |
|---|---|---|---|
| tb-ops `/root/.ssh/t4-executor`(개인 키) → 적용 호스트 `~/.config/testbed/t4-executor.pub`(공개 키) | 개인 0600, 공개 0600 | tb-ops: `ssh-keygen -q -t ed25519 -N '' -C opsjob -f /root/.ssh/t4-executor`, 공개 키만 `install -m 0600` 으로 복사 | 실행기 계정 `authorized_keys`(러너가 쓰는 키) |
| `~/.config/testbed/t4-operator.pub` | 0600 | 적용 호스트의 운영자 개인 키(예: 104 `~/.ssh/id_ed25519`)의 공개 키를 `install -m 0600 ~/.ssh/id_ed25519.pub ~/.config/testbed/t4-operator.pub` | cloud-init 사용자(운영자 계정), 66 에 복사해 `plan.py --sshkeys` |
| `~/.config/testbed/t4-petition-db` | 0600 | `(umask 077; tr -dc A-Za-z0-9 </dev/urandom \| head -c 24 > ~/.config/testbed/t4-petition-db)` | DB 앱 계정, Tomcat JNDI. 영문, 숫자 16자 이상이어야 설치가 받는다 |
| `~/.config/testbed/t4-snmp-community` | 0600 | `(umask 077; tr -dc A-Za-z0-9 </dev/urandom \| head -c 20 > ~/.config/testbed/t4-snmp-community)` | 라우터 snmpd 와 trap, 119 NMS 등록(T3) |

확인은 값이 아니라 크기와 권한만 본다: `stat -c '%a %s %n' ~/.config/testbed/t4-*`.
S 는 §14 의 차례 1.

## 4. 66 망 (잠금, 녹화 정지 창)

전제: T0 의 WireGuard 적용(66 `wg0`, 119 `wg0`)이 이 파일과 따로 있다. `wg0` 인터페이스, `wg0` 의 FORWARD 허용과 거절(체인 TBWG-FWD), MSS 클램프는 T0 의 터널 스크립트가 터널과 함께 걸고 지운다(testbed-services `docs/runbook-testbed-tunnel.md` §3). 이 파일에는 `wg0` 가 나오지 않는다. 대신 터널이 내려가 있을 때 VM 의 관제 주소 행 보고가 사내망 NAT 로 새지 않게 `-d 10.250.0.0/30 -o vmbr0.200 DROP` 을 대역마다 둔다. 66 의 업무망 경로 3개는 T0 가 만들지 않으므로 이 파일이 둔다.

```
python3 civil-petition/deploy/render.py --inventory civil-petition/deploy/inventory/t.json --hypervisor /tmp/t4-hv
# /tmp/t4-hv/etc/network/interfaces.d/t4-net        브리지 5개(vmbr7~11), 업무망 경로 3개, MASQUERADE 4, 러너 SNAT 1,
#                                                   브리지마다 자기 훅(FORWARD 들고 남, 주소 있는 브리지는 INPUT),
#                                                   브리지마다 IPv6 끔(disable-ipv6 yes)과 그 확인(post-up)
# /tmp/t4-hv/etc/network/t4-net.rules               전용 체인 T4-FWD, T4-IN 의 선언과 규칙 전부(iptables-restore 꼴)
# /tmp/t4-hv/etc/chrony/conf.d/t4-net.conf          allow 192.168.127.0/24 (VM 이 66 시간을 따른다)
# /tmp/t4-hv/etc/modules-load.d/t4-net-qdisc.conf   sch_netem, sch_tbf, sch_prio, cls_flower
python3 civil-petition/deploy/render.py --inventory .../t.json --hypervisor-commands up     # ifup vmbr7 vmbr8 vmbr9 vmbr10 vmbr11
python3 civil-petition/deploy/render.py --inventory .../t.json --hypervisor-commands down   # ifdown vmbr11 vmbr10 vmbr9 vmbr8 vmbr7
python3 civil-petition/deploy/render.py --inventory .../t.json --hypervisor-commands ipv6-state   # 브리지마다 IPv6 상태 한 줄을 찍는 명령
```

**브리지 IPv6 는 끈다.** 66 의 sshd(`[::]:22`), pveproxy(`*:8006`)는 IPv6 로도 듣고 ip6tables 는 비어 있다. 아래 INPUT, FORWARD 격리는 IPv4 만 막으므로, 브리지에 IPv6 링크 로컬 주소가 있으면 VM 이 그 주소로 66 관리면에 닿는다(11-18 ap-t2 적용 중 발견). 그래서 브리지마다 ifupdown2 속성 `disable-ipv6 yes` 를 둔다. 66 의 ifupdown2(3.3.0-1+pmx12)는 이 값을 브리지를 만든 뒤, 관리상 올리기 전(pre-up 의 address 애드온)에 걸므로 주소가 한 번도 생기지 않는다. 내렸다 올려도(ifdown 은 브리지를 지우고 ifup 이 다시 만든다), 재부팅 뒤에도 같다. 그 뒤 post-up 한 줄이 `disable_ipv6=1` 이고 IPv6 주소가 0 인지 보고, 아니면 그 브리지를 내린다(체인 훅과 같은 fail closed). 기존 vmbr0, vmbr0.200, vmbr1 과 전역 값(`all`, `default`)은 건드리지 않는다. **66 에서 `net.ipv6.conf.all.disable_ipv6` 를 쓰지 않는다**(손 `sysctl -w` 든 sysctl.d 파일이든). 커널은 `all` 에 쓴 값을 모든 인터페이스에 퍼뜨려 이 브리지들의 IPv6 가 다시 켜지고, post-up 확인은 다시 돌지 않아 아무도 모른다. `all` 을 만졌거나 의심되면 `ipv6-state` 명령 출력을 다시 떠서 브리지마다 `1 0` 인지 본다.

**새로 만드는 길의 확인(재부팅 뒤, ifdown 뒤 ifup, S 첫 적용).** 속성의 본뜻(브리지를 새로 만들 때 올리기 전에 꺼서 주소가 한 번도 생기지 않음)은 손 sysctl 이 없는 새 브리지에서만 보인다. 66 을 재부팅했거나 이 파일의 브리지를 새로 만든 뒤에는: `ipv6-state` 명령 출력이 브리지마다 `<이름> 1 0 <MAC>`, `ip -br link show <브리지>` 가 UP, 그리고 위 3 의 검사기가 `PASS up`. 브리지가 DOWN 이면 post-up 확인이 내린 것이니(아래 문단) `journalctl -u networking` 의 `post-up cmd` 경고를 본다.

**`ifreload -a` 를 쓰지 않는다.** 66 의 ifupdown2 는 reload 때 모든 auto 인터페이스의 post-up 을 다시 돌려, 확인 없는 `iptables -A` 로 된 기존 `tb-nat` 의 122 MASQUERADE 와 8091 DNAT 를 한 벌씩 더 만든다(리뷰 rv-t2 지적 1, 66 ifupdown2 3.3.0 소스). 이 테스트베드 브리지 이름만 `ifup`, `ifdown` 으로 다룬다.

체인은 어느 브리지가 먼저 올라오든 없을 때만 규칙 파일로 한 번에 만들고, 브리지마다 자기 훅만 걸고 지운다. 체인은 훅이 하나도 남지 않을 때만 비우고 지운다. 그래서 관리 브리지 하나만 내렸다 올려도 업무 브리지의 격리(122 와 123~126 사이, 119 실주소, 10.250 누수)는 그대로다. 규칙을 고쳐 다시 넣을 때는 `iptables-restore --noflush /etc/network/t4-net.rules` 한 번(두 체인을 통째로 바꾸고 훅은 그대로).

**T0 와 같은 정비 창에서 적용할 때의 순서와 기준 시점.** 검사기(`hv_iptables_check.py`)는 기준과 확인 사이에 끼어든 다른 iptables 변경을 "이 테스트베드 몫이 아님" 으로 센다(T0 의 wg0, TBWG 줄이 들어 있으면 그 사실을 따로 알린다). 그래서 둘을 겹치지 않게 차례로 한다.

| 차례 | 할 일 | 기준과 확인 |
|---|---|---|
| 1 | T0 터널 적용(T0 runbook) | T0 의 검사(T0 몫) |
| 2 | T2 기준 뜨기 | **T0 적용이 끝난 뒤** `iptables-save > /root/t4-net.before` |
| 3 | T2 적용(아래 1~3) | 직후 `t4-net.after`, `--mode up` |
| 되돌리기 1 | T2 되돌리기(아래 4) | 직후 `t4-net.rollback`, `--mode down`(기준은 2 의 before) |
| 되돌리기 2 | T0 되돌리기 | T0 의 검사 |

T2 만 적용하는 창이면 2 를 적용 직전에 뜬다. 되돌리기는 늘 T2 가 먼저다(T0 를 먼저 되돌리면 T2 의 down 확인이 T0 줄 차이로 실패한다).

브리지는 격리 없이 올라오지 않는다: 규칙 파일이 없거나 `iptables-restore` 나 훅 걸기가 실패하면 post-up 이 그 브리지를 내린다(재부팅 뒤 규칙 파일을 잃은 경우 포함). **`ifup` 의 종료 코드는 그래도 0 이다.** 66 ifupdown2 는 post-up 명령 실패를 `warning: <브리지>: post-up cmd ... failed` 로만 남기고 다음 명령으로 간다(`addons/usercmds.py`, 재부팅 때 `networking.service` 도 성공으로 끝남). 그래서 판정은 종료 코드가 아니라 검사기(`PASS up`)와 `ip -br link show <브리지>` 의 UP 으로 한다. 그때는 `/etc/network/t4-net.rules` 를 다시 렌더해 놓고 그 브리지를 `ifup` 한다. IPv6 가 꺼지지 않았을 때도 같다(stderr `<브리지>: IPv6 가 꺼지지 않아 내림`, 종료 코드 0). 그때는 망 파일에 `disable-ipv6 yes` 가 있는지, ifupdown2 가 그 속성을 아는지(`ifquery --check <브리지>` 의 `disable-ipv6` 줄)를 본다.

66 에서(파일 넷을 같은 경로로 놓은 뒤):

1. 사전 확인: `ip -br link | grep -E 'vmbr(7|8|9|10|11)'` 빈 출력, `iptables-save > /root/t4-net.before`(적용 전 기준, T0 와 같은 창이면 T0 적용 뒤), chrony 의 `confdir /etc/chrony/conf.d` 있음(11-16 리뷰 조회로 있음).
2. `ifup vmbr7 vmbr8 vmbr9 vmbr10 vmbr11`, `systemctl restart chrony`, `modprobe` 넷.
3. 확인(설계 §10 T2 "기존 테스트베드 영향 없음"):
   - `iptables-save > /root/t4-net.after`, `--hypervisor-commands ipv6-state` 가 찍은 명령을 66 에서 돌려 `/root/t4-net.ipv6` 에 받는다. 셋을 가져와 `python3 civil-petition/deploy/tools/hv_iptables_check.py --inventory .../t.json --mode up --before t4-net.before --after t4-net.after --ipv6-state t4-net.ipv6` 가 `PASS up`(지워진 줄 0, 늘어난 줄은 전용 체인과 MASQUERADE 4, SNAT 1 뿐, 같은 줄 두 벌 0, 브리지 5개 모두 `disable_ipv6=1` 과 IPv6 주소 0). `--ipv6-state` 없이는 검사기가 up 을 거절한다. PASS 와 함께 찍히는 `IPV6` 줄의 링크 로컬 주소는 그 순간 브리지 MAC 으로 셈한 참고값이다(브리지 MAC 은 첫 포트를 따라 바뀐다, §7 관리망 격리 (3)).
   - `iptables -t nat -S | grep -c 192.168.122.0/24` 가 1.
   - `iptables -S FORWARD` 정책 ACCEPT 그대로, 앞쪽에 T4-FWD 로 가는 줄 10개(브리지 5개의 들고 남).
   - 러너 API: 104 에서 `curl -s -o /dev/null -w '%{http_code}' http://192.168.200.66:8091/` 가 200, tb-ops 에서 `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8091/` 가 200. tb-ops 에서 192.168.200.66:8091 로 묻는 길은 T2 전에도 안 되는 길이라 판정에 쓰지 않는다(66 의 8091 DNAT 는 `-i vmbr0.200` 에만 걸려 있고 66 자신은 8091 을 듣지 않아 vmbr1 에서 오면 거절된다, 11-18 ap-t2 확인).
   - 사내망 핑, 기존 VM 6대의 바깥 통신.
4. 되돌리기(VM 을 지운 뒤): **파일을 지우기 전에** `ifdown vmbr11 vmbr10 vmbr9 vmbr8 vmbr7`(pre-down 이 훅, NAT, 경로를 지우고 마지막 훅이 빠질 때 체인도 지운다). 그 다음 `rm` 파일 넷, `systemctl restart chrony`. 끝으로 `iptables-save > /root/t4-net.rollback` 와 `hv_iptables_check.py --mode down --before t4-net.before --after t4-net.rollback` 가 `PASS down`.

**IPv6 끄기가 없던 판으로 이미 브리지를 올린 66 에 이 판을 놓을 때.** 파일 넷을 새로 렌더해 같은 경로에 놓고 `ifup vmbr7 vmbr8 vmbr9 vmbr10 vmbr11` 을 한 번 더 돈다(post-up 은 다시 돌아도 한 벌, `bridge-ports-condone-regex` 기본값이 VM 의 tap 포트를 떼지 않는다). 손 sysctl 이 없으면 address 애드온이 `disable_ipv6=1` 을 써 이미 붙은 링크 로컬이 지워진다. 그 뒤 위 3 의 확인(기준은 처음의 `t4-net.before`)을 다시 한다. **손으로 건 `sysctl -w net.ipv6.conf.<브리지>.disable_ipv6=1` 이 이미 1 이면 이번 `ifup` 은 속성 동작을 증명하지 않는다**: address 애드온은 지금 값과 설정 값이 같으면 아무것도 쓰지 않으므로(66 `addons/address.py` 1182~1185행) `PASS up` 은 손으로 건 값을 다시 볼 뿐이고, `ifquery --check <브리지>` 의 `disable-ipv6` pass 는 "ifupdown2 가 이 속성을 안다" 까지만 보인다. 손 sysctl 은 같은 값이라 그대로 두어도 되고, 재부팅이나 ifdown, ifup 뒤에는 망 파일의 속성이 같은 값을 건다. VM 이 붙은 뒤에는 ifdown 이 브리지를 지워 tap 이 떨어지므로 ifdown, ifup 으로 확인하지 않는다. 새로 만드는 길을 66 에서 처음 보는 것은 **S 첫 적용**(`ifup vmbr12 vmbr13 vmbr14` 뒤 S 의 `--ipv6-state` 가 `PASS up`, 손 sysctl 을 걸지 않고 판정) 또는 66 재부팅 뒤다(위 "새로 만드는 길의 확인").

S 는 §14 의 차례 2.

## 5. VM 생성

```
python3 civil-petition/deploy/provision/plan.py --inventory .../t.json --phase precheck   # 66 에서 읽기 확인
scp ~/.config/testbed/t4-operator.pub root@192.168.200.66:/root/t4-operator.pub
python3 civil-petition/deploy/provision/plan.py --inventory .../t.json --phase create --sshkeys /root/t4-operator.pub
```

`create` 는 Proxmox 이름을 **게스트 호스트 이름**(rt-core01, web01 ...)으로 복제한다. cloud-init 이 Proxmox 이름을 게스트 호스트 이름으로 쓰기 때문에 처음부터 t4-* 로 만들면 게스트에 그 이름이 남는다(원칙 8). Proxmox 이름은 설치가 cloud-init 을 끈 뒤 `--phase rename` 으로 t4-* 로 바꾼다. 출력은 사람이 읽고 66 에 넘긴다(`plan.py` 는 66 에 접속하지 않는다).

확인: `qm list` 에 201~206 Running, 크기는 설계 §2-2 와 같다(vCPU 14, 메모리 22GB, 디스크 256GB). 각 VM 관리 주소로 66 에서 `ssh sysop@192.168.127.x hostname`.

NIC 이름: 새 VM 안의 NIC 이름은 **eth0, eth1 ...** 이다. Proxmox cloud-init 이 NIC 를 MAC 으로 맞춰 `set-name: eth0` 을 쓰기 때문이다(커널 예측 이름은 ens18 ~ ens22). 인벤토리와 설치 코드는 ens18 ~ 을 쓰고, 이름은 §6 의 network 단계가 바꾼다. 여기서는 각 VM `ip -br link` 가 eth0 부터 NIC 수만큼이고, 그 MAC 이 66 `qm config <vmid> | grep ^net` 의 netN MAC(대소문자 무시)과 같은 번호끼리 같은지만 본다. netN 과 ipconfigN 을 같은 번호로 주므로 인벤토리 주소를 가진 NIC 가 그 netN 이다.

첫 접속 호스트 키: `apply.sh` 는 이 테스트베드 전용 `~/.ssh/known_hosts_<도메인 값>` 에 `StrictHostKeyChecking=accept-new` 로 받아 둔다. VM 을 지우고 다시 만들었으면 먼저 `ssh-keygen -f ~/.ssh/known_hosts_civil-petition -R 192.168.127.x` 로 옛 키를 지운다. 적용 때 `ssh sysop@... id -u` 가 1000 인지 본다(운영자 uid 가정).

## 6. 서버 설치 (순서가 중요하다)

**적용 전 점검: rsyslog 설정(104, docker).** 서버 설치 전에, 그리고 설치에 쓰는 커밋을 옮길 때마다 저장소 뿌리에서 돌린다.

```
flock ~/dev/testbed-ops/v4/locks/heavy systemd-run --user --scope -q -p MemoryMax=4G -- civil-petition/deploy/tools/rsyslog-check.sh
```

다이제스트로 고정한 일회 `ubuntu:24.04` 컨테이너에 apt 로 rsyslog 를 받아(대상 VM 과 같은 패키지. `RSYSLOG_CHECK_VERSION` 을 주면 그 꼬리 판으로 고정) T, S 모든 서버의 렌더한 rsyslog 설정을 `rsyslogd -N1 -d` 로 읽는다. 오류 없음, 놓은 `rsyslog.d` 파일을 파서가 실제로 읽음, 읽는 파일과 포함 대상이 모두 `/etc/rsyslog.conf`, `/etc/rsyslog.d/` 안(Ubuntu rsyslogd AppArmor 프로필의 읽기 범위를 경로로 흉내 냄)을 본다. 이어 rsyslog 를 띄워 첫 규칙에 실행기 줄(계정 이름이나 uid 가 줄 끝에 오는 줄, 실행기 uid 로 보낸 줄 포함)과 아닌 줄을 보내 실행기 줄은 `/var/log/opsjobs.log` 에만, 아닌 줄은 syslog 나 auth.log 에만 가는지 본다. 원격 전달 파일은 읽기 검사만 하고 띄울 때는 빼서 검사 줄이 관제 서버로 가지 않는다. `CHECK ...|t` 줄이 모두 t 이고 끝 줄이 `failed=0`(지금 인벤토리 T 6대, S 5대로 `checks=264`) 이면 통과다. 첫 줄의 rsyslog 꼬리 판을 대상 VM 의 `dpkg-query -W rsyslog` 와 견준다. 기본 단위 시험은 큰따옴표 문자열 안의 맨 `$`(rsyslog 가 설정 전체를 거절한다. `\$` 로 쓴다) 같은 꼴만 본다. 컨테이너 안에서 rsyslog 를 받으므로 바깥 길이 있어야 하고, 끝에 컨테이너를 지운다.

이 점검이 대상 VM 과 다른 점과 그것을 보는 곳: (1) 컨테이너 커널에는 AppArmor 가 없다. 프로필 범위는 경로로 흉내 내고, `/proc` 거부 조각은 패키지 rsyslogd 프로필에 포함해 파싱되는지만 본다(`apparmor-fragment`, `apparmor_parser -QTK` 와 `-p`). 실제 거부는 설치 단계의 읽기 확인과 재시작 뒤 `/proc` 거부 감사 0 확인(아래), `verify` 의 `rsyslog-no-apparmor-denial`, `rsyslog-no-proc-audit` 이 본다. (2) 줄이 journald 를 거치지 않고 `/dev/log` 로 바로 들어온다. VM 의 길(journald 가 넘겨 주는 자격 증명의 uid)은 `syslog` 단계의 실행기 uid 줄 확인과 §7 이 본다. (3) VM 의 `rsyslog.d` 에만 있는 파일(`20-ufw.conf`, `21-cloudinit.conf`, WAS 의 tomcat10 파일)은 없다. 그 파일과 함께 읽는 검사는 설치 단계의 사본 검사가 VM 에서 한다.

WAR 는 한 번 빌드해 T 와 S 가 같은 파일을 쓴다(`project.build.outputTimestamp` 로 같은 커밋이면 같은 체크섬). k6 는 tb-runner 의 `/usr/local/bin/k6`(v0.57.0, md5 c7e01ff21ecd3a2f2c43362a285a5906)를 복사해 쓴다.

```
cd civil-petition && mvn -B package            # 104 무거운 잠금 아래
I=deploy/inventory/t.json
deploy/apply.sh --inventory $I --host rt-core01                     # 1. 라우터(서버의 바깥 통신 길)
deploy/apply.sh --inventory $I --host db01                          # 2. DB, 스키마, 시드(시드 수십 분)
deploy/apply.sh --inventory $I --host was01 --host was02 --war petition-web/target/petition.war
deploy/apply.sh --inventory $I --host web01
deploy/apply.sh --inventory $I --host loadgen --k6 <k6 바이너리>      # 설치만, 부하는 켜지 않는다
```

`deploy/apply.sh` 는 저장소에서 실행 비트를 가진다(이 고침 전 체크아웃에서 `Permission denied` 이면 `bash deploy/apply.sh`). 각 서버는 `verify-host → os-base → network → accounts → time → syslog → syslog-forward → kernel-modules → firewall → lldpd → (역할) → sms-agent(웹, WAS, DB 만, 관제 서버 등록 창에서, §11) → verify` 를 돈다. 등록 창 전에는 `--skip-steps sms-agent` 로 뺀다(서버마다 묶음의 단계 목록에서 그 단계만 빠진다. `--steps` 와 함께 쓰지 않는다). `verify` 의 CHECK 줄이 모두 PASS 여야 끝난다(NIC 이름은 `nic-names`, `no-network-rollback-pending`). 한 단계만 다시 돌릴 때는 `--steps "web verify"`. `--dry-run` 은 묶음 목록과 명령만 찍는다.

**패키지 색인 시각.** `apply.sh` 는 `t.json` 의 서버별 `apt_snapshot`(처음 apt 설치 때의 Ubuntu snapshot 색인 시각, UTC, §14-3-1 과 같은 값)을 묶음 값에 넣고, 설치 단계 `os-base` 는 그 시각 색인으로 받고 깐다. T 서버를 다시 깔 때도 같은 시각 색인이라 의존 패키지까지 처음 판과 같다(S 의 쌍 조건 2 를 지킨다). 값이 없는 서버는 보내지 않는다. `os-base` 는 쓴 시각을 `/var/lib/petition-deploy/apt-snapshot` 에 남기고, 다음에 다른 시각이나 시각 없이 돌면 apt 앞에서 멈춘다. T 서버는 이 기록 없이 깔렸으므로 처음 다시 돌 때 기록이 생긴다. 스냅숏 색인을 못 받았을 때의 멈춤과 대응은 §14-3-1.

`syslog`, `syslog-forward` 단계는 새 rsyslog 설정을 먼저 검사 사본에서 본다. 사본은 `/etc/rsyslog.d/.check-<pid>/` 에 둔다(Ubuntu 의 rsyslogd AppArmor 프로필이 설정으로 읽게 두는 곳은 `/etc/rsyslog.conf` 와 `/etc/rsyslog.d/**` 뿐이라 `/tmp` 사본은 `Permission denied` 로 막힌다. 숨은 하위 디렉터리라 `*.conf` 포함에 들지 않는다). 지금 `rsyslog.conf`, `rsyslog.d` 의 `*.conf`, `rsyslog.d/remote` 의 `*.conf` 위에 새 파일을 놓고 포함 경로를 사본으로 바꿔 `rsyslogd -N1 -d` 로 읽은 뒤, 오류가 없고 이 단계의 rsyslog 파일을 파서가 모두 실제로 읽었을 때만 `/etc` 에 놓는다(`include(... mode="optional")` 는 AppArmor 가 막아도 오류가 아니므로 읽었는지를 따로 본다). 사본 검사가 실패하면 `/etc` 의 설정을 바꾸지 않고 `rsyslog 설정 검사(놓기 전, 단계 <단계>)` 로 멈춘다. 놓기나 놓은 뒤 진짜 설정 검사가 실패하면 그 단계가 놓은 파일을 앞 내용으로 되돌리고(없던 파일은 지운다. 되돌리지 못한 파일은 경로를 적는다) 멈춘다. 어느 쪽이든 rsyslog 를 재시작하지 않으므로 돌던 rsyslog 는 앞 설정 그대로다. 사본은 끝나면 지우고, 끊겨 남은 `.check-*` 는 다음 실행이 처음에 지운다.

원격 전달 설정은 `/etc/rsyslog.d/remote/10-forward.conf` 에 놓이고 `rsyslog.conf` 끝의 `include(file="/etc/rsyslog.d/remote/*.conf")` 가 모든 로컬 규칙 뒤에 읽는다(설계 §2-4). 옛 자리 `/etc/rsyslog.remote.d/` 는 AppArmor 범위 밖이라 rsyslog 가 읽지 못했다. `syslog` 단계가 비어 있으면 지운다. `syslog` 단계는 원격 전달이 꺼진 첫 설치에서 실행기 계정으로 한 줄(`setpriv ... logger`)을 보내 journald 를 거친 줄이 `/var/log/opsjobs.log` 에만 있고 syslog, auth.log 에는 없는지 본다(아니면 실패). 전달을 손으로 끄기: `sudo rm /etc/rsyslog.d/remote/10-forward.conf && sudo systemctl restart rsyslog`(다시 켤 때는 `--steps "syslog-forward verify"`).

`syslog` 단계는 rsyslog 설정을 놓기 전에 AppArmor 조각 `/etc/apparmor.d/rsyslog.d/annotate-proc`(rsyslogd 프로필의 `include if exists <rsyslog.d>` 자리)를 놓고 `apparmor_parser -r /etc/apparmor.d/usr.sbin.rsyslogd` 로 프로필을 다시 읽는다. 로컬 소켓 주석(`SysSock.Annotate`)이 보낸 프로세스의 `/proc/<pid>/comm`, `cmdline`, `exe` 를 읽으려다 거부될 때 줄마다 감사 기록(그리고 119 로 가는 커널 줄)이 남지 않게, 그 읽기를 감사 없이 거부하는 deny 규칙만 둔다(허용 범위를 넓히지 않는다). 다시 읽기가 실패하면 조각을 앞 상태로 되돌리고 프로필을 한 번 더 읽은 뒤 `AppArmor 프로필 다시 읽기 실패(단계 syslog)` 로 멈춘다(그때 rsyslog 설정은 아직 놓이지 않았다). 재시작 뒤 rsyslog 가 선 이후 `profile="rsyslogd"` 의 `/proc` 또는 ptrace 거부 줄이 하나라도 있으면 `rsyslogd 의 /proc 읽기 거부가 감사됨` 으로 멈춘다. 조각을 손으로 빼기: `sudo rm /etc/apparmor.d/rsyslog.d/annotate-proc && sudo apparmor_parser -r /etc/apparmor.d/usr.sbin.rsyslogd`.

### 6-1. NIC 이름 바꾸기(network 단계)와 관리망

`verify-host` 는 NIC 를 이름이 아니라 인벤토리 주소로 찾는다(처음엔 eth0 ~ 이라도 통과하고 `nic ens18 = eth0 <MAC>` 줄을 찍는다). `os-base` 가 cloud-init 을 끈다(`/etc/cloud/cloud-init.disabled`, 망 설정 `config: disabled`). 그래서 다음 부팅에 cloud-init 이 eth 이름과 자기 망 설정을 되살리지 않는다. `network` 는 이 둘이 없으면 아무것도 바꾸지 않고 멈춘다.

`network` 단계 순서:

0. 앞 실행의 되돌림 타이머가 기다리는 중이거나 되돌림이 도는 중이거나 실패한 채면 아무것도 바꾸지 않고 멈춘다(실패면 `journalctl -u netplan-rollback` 을 보고 `systemctl reset-failed netplan-rollback.service`).
1. 인벤토리 NIC 마다 지금 그 주소를 가진 장치의 MAC 을 `/var/lib/petition-deploy/nic-macs` 에 적는다. 이미 적힌 MAC 과 다르면 멈춘다.
2. 지금 `/etc/netplan/*.yaml` 사본을 `/var/lib/petition-deploy/netplan-before/` 에 둔다. 처음 한 번은 cloud-init 설정을 `netplan-cloud-init/` 에도 남긴다.
3. 되돌림 타이머 `netplan-rollback.timer` 를 건다(300초 뒤 `/usr/local/sbin/netplan-rollback` 이 사본으로 되돌리고 `netplan apply`).
4. `/etc/netplan/01-host.yaml`(NIC 마다 `match: macaddress` 와 `set-name: ens18 ...`)을 놓고 `50-cloud-init.yaml` 을 지운 뒤 `netplan generate` 로 먼저 시험한다.
5. `netplan apply` 를 세션과 떨어진 유닛 `netplan-switch` 로 돌린다(120초 넘으면 끊는다. 적용 120 + 확인 60 이 타이머 300 안에 들게). 이름이 바뀌는 NIC 는 netplan 이 잠깐 내렸다 올린다(관리 NIC 포함, 몇 초). 주소는 같아 SSH 세션은 보통 이어진다.
6. 60초 안에 확인: NIC 마다 인벤토리 이름, 적어 둔 MAC, 인벤토리 주소. 인벤토리에 없는 물리 NIC 없음. 관리 NIC 에 기본 경로 없음. 관리 NIC 로 66 관리 주소(T 192.168.127.1, S 192.168.133.1)가 ARP 에 답함(66 은 VM 의 핑에 답하지 않으므로 이웃 표로 본다).
7. 타이머가 아직 기다리는 중일 때만 끄고 확정한다(`network ok: eth0->ens18 ...` 줄). 그 사이 발화했으면 `network ok` 를 내지 않고 실패한다. 확인이 안 되면 바로 사본으로 되돌리고 실패한다(`사본으로 되돌림`). 세션이 끊겨 6, 7 에 닿지 못하면 300초 뒤 타이머가 되돌린다. 어느 쪽이든 관리 접속은 cloud-init 설정(eth 이름)으로 돌아온다.

첫 서버는 망 단계까지만 먼저 돌리고 바깥에서 본다:

```
deploy/apply.sh --inventory $I --host loadgen --k6 <k6 바이너리> --steps "verify-host os-base network"
ssh -o UserKnownHostsFile=~/.ssh/known_hosts_civil-petition -J root@192.168.200.66 sysop@192.168.127.10 'ip -br link; cat /var/lib/petition-deploy/nic-macs; systemctl is-active netplan-rollback.timer'
```

| 확인 | 기대 |
|---|---|
| 새 SSH 세션(66 을 거쳐 관리 주소) | 붙는다 |
| `ip -br link` | ens18 부터 NIC 수만큼, eth 이름 없음 |
| `nic-macs` | 66 `qm config <vmid>` 의 netN MAC 과 같은 번호끼리 같음 |
| `systemctl is-active netplan-rollback.timer` | `inactive` |

그다음 같은 서버를 단계 없이 다시 돌린다(망 단계는 다시 돌아도 이름을 바꾸지 않는다). 나머지 서버는 순서대로 그냥 돌린다.

서버마다 MAC 대조: 66 에서 `plan.py --inventory .../t.json --phase nics` 출력을 `bash -s` 로 돌리면 `<호스트> <NIC 이름> <주소> <MAC>` 이 인벤토리 NIC 순서로 나온다(읽기만). 각 서버 설치 출력의 `nic <NIC 이름> = <장치> <MAC>` 줄과 같은 NIC 이름끼리 MAC 이 같아야 한다. VM 을 다시 만든 뒤에도 같은 대조를 한다.

**NIC 이름에 기대는 단계(firewall, lldpd, router)는 첫 줄에서 이름을 본다.** 이름이 인벤토리와 다르면(network 앞, 또는 손으로 cloud-init 설정으로 되돌린 뒤) 아무것도 바꾸지 않고 `NIC 이름이 인벤토리와 다름(network 단계를 먼저)` 으로 멈춘다. eth 이름 위에서 이 단계들이 돌면 라우터 보호 표(`iifname != "ens22" ... drop`)가 66 → 관리 SSH 를 막고 nftables 는 부팅 때도 올라와 재부팅으로 풀리지 않는다. 서버 보호 표는 관리 NIC 에 걸리지 않아 보호가 조용히 사라진다. 그래서 `--steps` 로 부분만 돌릴 때도 이름이 바뀐 서버에서만 이 단계들을 돌린다.

세션이 network 단계에서 끊겼으면: VM 을 재부팅하지 말고 5분 기다린 뒤 다시 접속한다(타이머가 cloud-init 설정으로 되돌렸다. 타이머는 일시 유닛이라 재부팅하면 사라진다). `journalctl -u netplan-switch -u netplan-rollback` 로 무엇이 있었는지 보고 `--steps "verify-host network"` 로 다시 돈다. 5분 뒤에도 닿지 않으면 66 에서 `qm reboot <vmid>`(디스크의 netplan 으로 부팅 때 망이 선다. 되돌림이 돌았으면 cloud-init 설정이다). 직렬 콘솔(`qm terminal`)은 있지만 운영자 계정에 비밀번호가 없어 로그인할 수 없다.

손으로 cloud-init 설정까지 되돌리기: **firewall 단계가 아직 돌지 않은 서버에서만** 한다. `cp -p /var/lib/petition-deploy/netplan-cloud-init/50-cloud-init.yaml /etc/netplan/ && rm -f /etc/netplan/01-host.yaml && netplan apply`(이름이 eth0 ~ 으로 돌아온다). cloud-init 은 꺼진 채 둔다. firewall 뒤라면 같은 세션에서 먼저 `nft delete table inet mgmtguard` 를 한 뒤에 되돌린다(라우터는 그러지 않으면 되돌리는 순간 관리 SSH 가 막힌다). 이어서 `mv /etc/nftables.d/10-mgmt-guard.nft /var/lib/petition-deploy/` 로 보호 표 파일도 옮긴다. 그러지 않으면 `qm reboot` 뒤 `nftables.service` 가 그 파일을 다시 올려 관리 SSH 를 또 막는다. 옮긴 파일은 다시 설치할 때 firewall 단계가 묶음에서 새로 놓으므로 되돌려 둘 필요가 없다. 되돌린 뒤 다시 설치할 때는 `--steps "verify-host network"` 로 이름을 먼저 바꾸고 그다음 단계 없이 전체를 돌린다(firewall 이 보호 표를 다시 놓는다).

S 는 §14 의 차례 5.

설치 뒤: `plan.py --phase rename` 출력을 66 에 넘긴다.

## 7. T2 완료 조건 점검 (설계 §10 T2)

| 조건 | 명령(어디서) | 기대 |
|---|---|---|
| VM 6대 크기와 주소 | 66 `qm list`, 각 VM `ip -br addr` | §2-2 표 |
| 업무망 경로 | 각 서버 `ip route get 192.168.126.11` 등 | via 라우터 .1. 66 `ip -br addr show vmbr8 vmbr9 vmbr10` 주소 0(IPv4, IPv6 모두. IPv6 는 §4 의 `PASS up` 이 브리지 5개 모두 보았다) |
| 관리망 격리 (1) | web01 `curl -m 3 --interface 192.168.124.11 http://192.168.127.31:22` | 실패 |
| 관리망 격리 (2) | web01, loadgen `ping -I ens18 -c 3 192.168.127.1`, `192.168.127.2`. 거절이 남는 곳은 핑 앞뒤로 읽는다: web01 쪽은 라우터 `nft list chain inet mgmtguard prerouting`, loadgen 쪽은 66 `iptables -nvxL T4-IN`, `iptables -nvxL T4-FWD`(읽기) | 모두 실패. web01 의 핑은 라우터 `prerouting` 의 `iifname != "ens22" ip daddr 192.168.127.0/24 counter drop` 패킷 수가 는다(이 거절이 forward 훅보다 먼저라 `forward` 의 `oifname ... counter drop` 은 늘지 않는다). loadgen 의 업무 NIC 는 기본 경로가 66(123.1)이라 라우터에 닿지 않는다. 127.1 은 66 `T4-IN` 의 `-i vmbr7 -j DROP`(vmbr7 ICMP 허용은 목적지 192.168.123.1 만), 127.2 는 `T4-FWD` 의 `-o vmbr11 -j DROP` 패킷 수가 는다 |
| 관리망 격리 (3) | IPv4: 각 VM `nc -z -w 3 192.168.127.1 22`, `8006`. IPv6: §4 의 3 확인을 지금 상태로 다시(기준 `t4-net.before`, 새 `iptables-save`, 새 `ipv6-state` 출력) 돌려 `PASS up`(판정의 본체). 덧확인으로 각 VM 에서 자기 NIC 마다 `ping -6 -c 2 -w 3 -I <NIC> ff02::1` 의 응답 출발지를 66 `qm config <vmid>` 의 그 세그먼트 VM NIC MAC(EUI-64)과 맞춘다 | IPv4 연결 실패. IPv6 는 `PASS up` 이고, 멀티캐스트 핑 응답 출발지가 모두 그 세그먼트 VM NIC 의 주소뿐(66 이 답하지 않음). 검사기 `IPV6` 줄의 링크 로컬은 그 순간 브리지 MAC 으로 셈한 참고값이다. 브리지 MAC 은 첫 포트(tap)를 따라 바뀌므로 그 주소로 `nc -6` 이 실패하는 것은 판정이 되지 않는다 |
| 관리망 격리 (4) | 각 VM `ping -I ens19 -c 2 192.168.230.119`, `10.250.0.1` | 실패 |
| 관리망 격리 (5) | 119(읽기) `ping -c 2 192.168.127.21` | 실패 |
| 서비스 기동 | `systemctl is-active apache2 tomcat10 postgresql@16-main`, WAS `cd /var/lib/tomcat10/webapps && sha256sum -c /var/lib/petition-deploy/war.sha256`(체크섬 파일에 파일 이름만 적혀 있어 webapps 에서 돌린다. 다른 곳에서 돌리면 "could not be read") | active, `petition.war: OK` |
| 시드 크기 | db01 `sudo -u postgres psql -Atc 'select count(*) from petition' petition` | 2,000,000 ±5% |
| 시간, 자동 갱신, APM 흔적, 커널 모듈 | 설치의 `verify` CHECK 줄 | PASS |
| rsyslog 설정을 실제로 읽음 | 설치의 `verify` 의 `rsyslog-first-rule`, `syslog-forward-matches-inventory`(파일이 아니라 `rsyslogd -N1 -d` 가 그 파일을 읽었는지), `rsyslog-no-apparmor-denial`(rsyslog 가 마지막으로 선 뒤 `profile="rsyslogd"` 의 `apparmor="DENIED"` 줄 0), `rsyslog-no-proc-audit`(그 가운데 `/proc` 읽기와 ptrace 거부 0) | PASS |
| rsyslog 첫 규칙 | 실행기 계정으로 한 번 접속, `sudo -n bash -s` 로 U8 가드 arm 하나 → 119(읽기) `syslog_local` 과 VM `/var/log/syslog`, `/var/log/auth.log` 에서 그 계정, uid, `run-r` 줄 0, `/var/log/opsjobs.log` 에 있음. 운영자 계정 sudo 한 줄은 119 에 있음 | 0, 있음, 있음 |
| 큐 버림 보고 | 라우터 `sudo civil-petition/deploy/tools/router-discards-selftest.sh ens19 192.168.124.11`(부하 켜기 전) | PASS 4줄 |
| 라우터 관제 흐름 notrack | 라우터 `sysctl -w net.netfilter.nf_conntrack_max=<지금 수 + 50>`, loadgen 에서 웹 80 으로 새 연결 수백 개, 라우터 `dmesg` 에 `table full` → 119(읽기) 라우터 상향 주소 SNMP 응답, `syslog_local` 에 그 줄. 표가 찬 동안 66 에서 라우터 관리 NIC 로 새 SSH 하나(관리 SSH 는 `mgmtguard` 에서 notrack). 끝나면 원래 값(그 새 SSH 로 되돌린다) | 셋 다 됨(SNMP 응답, 그 줄, 새 관리 SSH) |
| 가드 단독 시험 | U8 시운전 절차 2번(`v4/agent-out/U8.md`)을 라우터와 web01 에서. 2-4 의 부팅 규칙 확인은 `sudo systemd-tmpfiles --boot --remove <probe.unit 셋째 칸>` 이다(규칙이 `r!` 부팅 전용이라 `--boot` 없이는 아무것도 지우지 않는다). 그 뒤 남은 일시 타이머는 파일이 없어 disarm 이 못 찾으므로 `systemctl stop <run-r 타이머>` 로 손으로 멈춘다(실제 재부팅이면 사라지는 것). 2-3 의 "disarm 이 앞 발화 시각을 낸다" 는 바로 앞에 발화가 있었을 때만이다(arm 이 표지를 지운다) | 통과 |
| S 와 같이 쓰는 코드 | 저장소 시험 `test_civil_petition_deploy.py` 의 SharedCodeTests | OK |
| PostgreSQL 서버 로그 | §9 의 값, 24시간 동안 `wc -l /var/log/postgresql/postgresql-16-main.log` 증가와 T3 로그 감시 줄 수 | 결과에 적음 |
| DPM 흔적 없음 | 설치의 `verify` 의 `no-dpm-traces`, `ls ~/.config/testbed/ \| grep -c t4-dpm` | PASS, 0 |

66 쪽 가드(하이퍼바이저 실행기 도우미 `t4-hv-exec`)는 실행기와 함께 T4 에서 짓는다. 66 가드 단독 시험은 그때 한다.

## 8. 부하 시작과 판정

```
deploy/apply.sh --inventory $I --start-load
```

부하기는 `loadgen.service`(k6, 사용자 k6) 하나이고 관제 서버로 아무것도 보내지 않는다. 요약은 `/tmp/k6-civil-petition-live.json`(30초 창, `http_abnormal_rate`, `http_p95_ms` 포함), 5분 창 기록은 `/var/lib/k6/k6-civil-petition-windows.jsonl`(창마다 공개 민원 검색 `search`, 내 민원 목록 `my-list`, 담당자 함 `inbox` 의 p95 도 `step_p95_ms` 로 싣는다).

**평시 24시간(Q3).** 시작과 끝에 서버마다 사실을 모은다.

```
# web01: host-facts.sh apache2.service / was01, was02: tomcat10.service / db01: postgresql@16-main.service / rt-core01: snmpd.service nftables.service
ssh ... 'sh -s apache2.service' < deploy/tools/host-facts.sh >> facts-before.jsonl     # 24시간 뒤 facts-after.jsonl
ssh db01 'sudo -n sh -s petition' < deploy/tools/db-facts.sh > db-before.jsonl         # 24시간 뒤 db-after.jsonl
scp loadgen:/var/lib/k6/k6-civil-petition-windows.jsonl .                              # db-after 를 뜬 뒤에 가져온다
python3 deploy/tools/stability_check.py --windows k6-civil-petition-windows.jsonl \
    --end "$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read())["at"])' db-after.jsonl)" \
    --facts-before facts-before.jsonl --facts-after facts-after.jsonl \
    --db-before db-before.jsonl --db-after db-after.jsonl
```

`--end` 는 db-after 를 뜬 시각(`at`)이다. 그래야 행 수(사실 시각 기준)와 보관 배치, 창 판정이 같은 구간을 본다. db-before 는 그보다 24시간 앞(판정 구간 시작)에 뜬다. 대표 질의는 판정 구간 앞 2시간의 창도 쓰므로 부하 시작에서 26시간이 넘은 뒤에 판정한다. 그 전에 돌리면 짧게 견주지 않고 `INCOMPLETE_Q3_24H` 다. 구간마다 search 표본이 있는 5분 창이 22개보다 적으면(부하가 멈춤) 실패다.

기준(설계 값): 전체 비정상(5xx 와 응답 없음) 0.1% 이하, 5분 창의 99% 이상에서 0.5% 이하(기록이 없는 창은 못 지킨 창), 피크(KST 15~17시) p95 800ms 이하, 서비스 재시작 0, 커널 OOM 0, chrony 50ms 이하, 창 안 재부팅 없음. 정상 상태(검토 세션 Q21 답 ③): 주 표(민원, 처리 이력) 행 수가 처음과 끝에 같은 자릿수, 보관 배치가 구간 안에서 결과 `ok` 로 한 번 이상, 대표 질의(공개 민원 검색 `search`) p95 가 정확히 24시간 떨어진 같은 시계 구간, 즉 판정 구간 바로 앞 2시간과 마지막 2시간(5분 창 p95 의 중앙값)에서 10% 안(부하기가 시간별 일주기로 돌기 때문, 어느 쪽이든 창이 없으면 실패). DB 사실 파일이 없으면 실패로 센다. 밤 배치 처리 능력(검토 세션 Q24 답 조건 ①): 그날 결과 `ok` 이고 행을 옮긴 보관 배치의 실측 처리량(옮긴 행 / 걸린 시간) × 창 길이(02:20~05:00)가 설계 하루 쓰기 2.5일 몫 이상(못 재면 실패). 설계 가정 값은 `python3 deploy/tools/design_load.py`(하루 쓰기, 문턱, 창 용량, 판정 `PASS_ARCHIVE_CAPACITY`)로 본다. 판정 이름 `PASS_Q3_24H`, `FAIL_Q3_24H`, `INCOMPLETE_Q3_24H`. `db-facts.sh` 의 점검 읽기는 세션에서 느린 문장 기준을 꺼 서버 로그에 남지 않는다.

**규모 축(피크 2배 30분).** 녹화가 없는 창에서.

```
sudo systemctl stop loadgen.service                                  # loadgen
k6 run --env TARGET_RPS=60 --env DURATION=30m --env ENTRY_URL=<진입> --env PRE_ALLOCATED_VUS=80 --env MAX_VUS=200 \
    --summary-export /tmp/k6-2x.json /opt/loadgen/civil-petition/script.js
# 같은 30분 동안: 라우터, was01, was02, web01, db01 에서 cpu-sample.sh <이름> 10 180, 66 에서 cpu-sample.sh hypervisor 10 180
python3 deploy/tools/scale_verdict.py --inventory $I --summary k6-2x.json --cpu router=router.jsonl --cpu was01=was01.jsonl \
    --cpu was02=was02.jsonl --cpu web01=web01.jsonl --cpu db01=db01.jsonl --cpu hypervisor=hv.jsonl
sudo systemctl start loadgen.service
```

기준: 비정상 1% 이하, 라우터 CPU 50% 이하와 softirq 20% 이하, WAS CPU 70% 이하, 66 CPU 70% 이하, VM steal 5% 이하, 달성 요청률이 60 의 95% 이상. CPU 는 10초 표본의 95 백분위. 판정 이름 `PASS_2X_60RPS_30M`, `FAIL_2X_60RPS_30M`. `--inventory` 를 주면 라우터가 없는 인벤토리(S)에서 라우터 표본을 요구하지 않는다(주지 않으면 늘 요구한다). 라우터가 넘으면 vCPU 를 4 로 올리고 다시 잰다(설계 §3-6).

**디스크.** db01 `df -B1 /pgdata` 를 부하 시작과 24시간 뒤에 재서 하루 증가를 구하고, 90일 뒤 사용률이 70% 이하인지 적는다. 주 표는 보관 배치의 건수 상한(민원 200만, 설계 §2-3)으로 크기가 묶이지만, 옮겨 간 행이 쌓이는 보관 표(`petition_archive`, `petition_history_archive`)는 하루 약 12만 민원과 그 처리 이력만큼 계속 자란다. 하루 증가는 대부분 이 보관 표 몫이다.

**보관 배치.** was02 의 `petition-archive.timer`(02:20 KST)가 앱의 보관 배치를 부른다. 설정 기본값은 WAR 의 `application.properties`(`petition.archive.keep-rows=2000000`, `chunk-rows=1000`, `pause-ms=1000`, `window=02:00-05:00`, `zone=Asia/Seoul`)이고 Tomcat 환경변수(`PETITION_ARCHIVE_KEEP_ROWS` 등)로 덮는다. 결과는 `batch_run` 표와 `/var/log/petition-batch.log` 줄의 `result=` 에 앱이 답한 값 그대로 남는다: `ok`(상한까지 옮김, 넘은 것이 없으면 0 건), `skipped`(창 밖 호출, 행 수도 세지 않음), `partial`(옮기는 중 창이 닫힘, 남은 몫은 다음 밤), `busy`(다른 보관 배치가 PostgreSQL advisory lock 을 쥐고 있어 아무것도 안 함). 앱이 답하지 못하면 `result=fail` 이고 유닛이 실패한다. 평시 24시간 판정은 `ok` 만 센다.

**보관 SQL 확인(수동, docker 필요).** Java 시험은 문장을 실행하지 않으므로 보관 문장의 뜻과 계획을 바꿨다면 저장소 뿌리에서 `flock ~/dev/testbed-ops/v4/locks/heavy systemd-run --user --scope -q -p MemoryMax=4G -- civil-petition/deploy/tools/archive-sql-check.sh [규모]` 를 돌린다(기본 200만, 시드 약 5분, 문법만 볼 때 20000). 일회 `postgres:16` 컨테이너에서 시드의 정상 상태 꼴(행 수, 열린 건 0.15%, 처리 중 비율, 미래 시각 없음, 이력 비, 규모 50만 이상이면 접수 시각 폭과 KST 시간 비), 행 수 추정치 `n_live_tup` 오차, 가장 오래된 순서, 처리 이력 이동, 답변과 첨부 cascade, 처리 이력 색인을 타는 계획, 묶음과 `count(*)` 시간, advisory lock 을 보고 `CHECK ...|t` 줄이 모두 t 이고 끝 줄이 `checks=16 failed=0`(규모 50만 미만은 14)이면 통과다. 끝에 컨테이너를 지운다.

끝에 `plan.py --phase snapshot` 으로 재해 복구용 스냅숏 하나(설계 §8, 녹화마다 되돌리지 않는다). 이름은 T 가 `t2-baseline`, S 가 `st2-baseline` 이다.

## 9. PostgreSQL 서버 로그 설정 (평시 속성, 정본 `deploy/templates/db/logging.conf`)

검토 세션 Q20-3 답(11-17 13:0x KST): 한 번 정하고 고정한다. 기본값에서 바꾸는 항목마다 출처를, 기본보다 자세히 남기는 항목은 그 이유를 적는다. 정한 뒤 시나리오가 안 보인다고 바꾸지 않는다. 출처 약칭: [CIS] CIS PostgreSQL 16 Benchmark v1.1.0 Level 1 의 3.1 로깅 항목(Tenable 감사 항목 목록 `tenable.com/audits/CIS_PostgreSQL_16_v1.1.0_L1_OS_Linux_PostgreSQLDB` 에서 본문 확인), [GitLab] GitLab 문서 "Monitoring and logging setup for external databases"(`docs.gitlab.com/administration/postgresql/external_metrics/`)의 권장 매개변수 목록, [pgBadger] pgBadger README 의 "POSTGRESQL CONFIGURATION" 절. 세 곳 모두 2026-11-17 KST 에 읽었다.

| 설정 | 값(PostgreSQL 16 기본) | 출처 | 기본보다 자세히 남기는 이유 |
|---|---|---|---|
| 로그 위치, 줄 머리, 회전 | Ubuntu 패키지 기본(`/var/log/postgresql/postgresql-16-main.log`, `'%m [%p] %q%u@%d '`, 주 단위 10개) | 배포판 기본(postgresql-common) | 바꾸지 않음 |
| `log_min_duration_statement` | 1000(-1) | [GitLab] "1000: log slow queries (slower than 1 second)" | 느린 질의 기록은 DB 운영 점검의 기본 근거다. 0(모든 문장, [pgBadger])은 운영 부하가 커서 쓰지 않는다 |
| `log_lock_waits` | on(off) | [GitLab] "a common cause of poor query performance", [pgBadger] | 잠금 대기는 느린 질의의 흔한 원인이고 1초 넘는 대기만 남아 평시 줄이 거의 없다 |
| `log_checkpoints` | on(on) | PostgreSQL 15 부터 기본 | 바꾸지 않음 |
| `log_connections`, `log_disconnections` | on(off) | [CIS] 3.1.20, 3.1.21, [pgBadger] | CIS 본문대로 PostgreSQL 은 접속 시도를 따로 기록하지 않아 감사에 이 로그가 필요하다. 연결 풀(유휴 60초 정리)이 부하 곡선을 따라 늘고 줄 때 줄이 생긴다(24시간 줄 수는 실측해 적는다) |
| `log_statement` | ddl(none) | [CIS] 3.1.25 "recommended this be set to ddl", [GitLab] | 스키마 변경의 감사 기록. mod, all 은 쓰지 않는다 |
| `log_temp_files`, `log_autovacuum_min_duration` | 기본(-1, 10분) | 바꾸지 않음([GitLab], [pgBadger] 는 0 을 권하지만 로그 양을 패키지 기본에서 멀게 하지 않는다) | |
| `lc_messages` | C(C.UTF-8 클러스터의 initdb 값과 같은 영어 메시지) | [pgBadger] 'C' | |

값은 이 파일 한 곳에만 있고 시나리오마다 바꾸지 않는다. 다시 고칠 일이 생기면(검토 세션 결정) 이 파일만 고치고 `--steps "db verify"` 로 다시 놓는다(PostgreSQL 재시작 한 번). 단계를 골라 돌려도 설치가 끝나면 VM 의 묶음 `secrets/` 는 지워진다. 설치 단계는 앱 계정 비밀번호 문장만 그 세션에서 `log_statement` 를 꺼 서버 로그에 남지 않게 한다.

## 10. 되돌리기 (사람 승인)

- VM: `plan.py --phase destroy` 출력을 사람이 읽고 66 에서 직접 실행한다. VMID 와 이름이 인벤토리와 맞을 때만 지운다.
- 66 망: §4 의 4(ifdown 을 파일 지우기 전에, 끝에 `--mode down` 확인).
- 되돌릴 수 없는 단계는 없다. 119 등록 해제와 SMS 에이전트 제거는 §11, §12-5 이고, 등록 해제가 과거 데이터를 지우는지 먼저 확인한다(설계 §9).

## 11. SMS 에이전트 설치 (T3)

T3 적용 창 안에서만 한다(순서는 §12-0). 에이전트가 처음 보고하는 순간 관제 서버에 등록 대기 server 대상이 생기므로(관제 서버 쓰기) T2 평시 판정 전에는 설치하지 않는다. 설치는 서버 묶음의 `sms-agent` 단계다(`deploy/steps/sms-agent.sh`, 웹, WAS, DB 역할만, 역할 설치 뒤 `verify` 앞).

| 서버 | `LUCIDA_HOST`(대상 이름이 됨) | 보고 주소 | 로그 감시 파일(서버 쪽 등록, §12 의 log-monitors 단계) |
|---|---|---|---|
| web01 | 192.168.124.11(업무 주소) | `http://10.250.0.1:18090` | `/var/log/apache2/access.log`, `error.log`, `/var/log/syslog`, `/var/log/auth.log` |
| was01, was02 | 192.168.125.11, .12 | 같음 | `/var/log/tomcat10/catalina.out`, `petition.log`, `localhost.[YYYY]-[MM]-[DD].log`(앱이 처리하지 못한 예외는 Tomcat JULI 기본으로 여기에만 간다), `localhost_access_log.[YYYY]-[MM]-[DD].txt`, `/var/log/syslog`, `/var/log/auth.log` |
| db01 | 192.168.126.11 | 같음 | `/var/log/postgresql/postgresql-16-main.log`, `/var/log/syslog`, `/var/log/auth.log` |
| rt-core01, loadgen | 설치하지 않음 | | |

S 는 websvr01 등 같은 자리, `LUCIDA_HOST` 는 S 업무 주소, 같은 바이너리 파일이다.

| 항목 | 값 | 근거 |
|---|---|---|
| 바이너리 출처 | 관제 서버 SMS 수집기 이미지 안 `/agent-dist/lucida-agent-linux-amd64`(Go 판). 11-18 조회: 이미지 `polestar/collector-sms:develop-ai-2026.11.09-01`, sha256 `b0ef5768...238c`. 배포 경로 `:18090/agent/dist/lucida-agent-linux-amd64` 는 404(설치 패키지가 비어 있다) | 기존 작업 노드도 같은 방식(이미지 안 바이너리)으로 깔았다(`runbook-testbed-move-109-to-66.md` 3단계). 그 노드들은 10-15 이미지의 판(`2026.08.23-01`)이라 판은 다르다 |
| 자리 | `/opt/lucida-agent/{bin/lucida-agent, agent.env(0600), logs/, tmp/}` | `agent.env` 는 ASCII 만(에이전트와 lucidactl 이 읽는 꼴) |
| 유닛 | `/etc/systemd/system/lucida-agent.service`, `Type=simple`, `Restart=always`, 표준 출력과 오류는 `logs/agent.log` 에 붙여 쓴다(journal 에 남지 않음). Go 판은 이 파일을 스스로 회전하지 않으므로(회전은 lucidactl 감싸개 몫) `/etc/logrotate.d/lucida-agent`(매일, 7개, 50M 넘으면, `copytruncate`)를 함께 놓는다 | 기존 작업 노드의 유닛 꼴 그대로(이름과 자리만 다름) |
| 사용자, 프로세스 이름 | root, `lucida-agent` | 로그 파일(`syslog:adm 0640`, `postgres:adm`, `tomcat:adm`)을 읽는다. 이름, 유닛, 자리에 테스트베드 낱말 없음(시험) |
| `agent.env` | `LUCIDA_SERVER`, `LUCIDA_HOST` 둘뿐. 나머지는 에이전트 기본값과 수집 정책 기본값 | 대상 주소(서버 ICMP 와 가용성이 핑하는 주소)는 `LUCIDA_HOST` 가 아니라 에이전트가 관제 서버로 나가는 출발 주소(업무 NIC, 기본 경로)에서 온다(lucida-next `agent/sms/goagent/inventory.go`) |
| 감시하지 않는 것 | `/var/log/opsjobs.log`(실행기 배관 줄, 설계 §6-2) | 에이전트는 서버가 내려 준 파일만 읽는다. journal 을 읽지 않는다(코드, §13 journal 행) |

바이너리 꺼내기(104 에서, 관제 서버는 읽기만):

```
A=~/dev/testbed-ops/v4/artifacts/lucida-agent-linux-amd64
mkdir -p "$(dirname "$A")"
S119="sshpass -f ~/.config/testbed/ssh-192.168.230.119 ssh root@192.168.230.119"
$S119 'k3s kubectl -n polestar exec deploy/collector-sms -c collector-sms -- sha256sum /agent-dist/lucida-agent-linux-amd64'
$S119 'k3s kubectl -n polestar exec deploy/collector-sms -c collector-sms -- cat /agent-dist/lucida-agent-linux-amd64' > $A
sha256sum $A          # 위 값과 같아야 한다. 결과 파일에 sha256 을 적고 아래 --sms-agent-sha256 에 준다(S 도 같은 값)
```

설치(서버 네 대, 관리망):

```
deploy/apply.sh --inventory $I --host web01 --host was01 --host was02 --host db01 \
    --steps "sms-agent verify" --sms-agent $A --sms-agent-sha256 <이미지 안 sha256> --war petition-web/target/petition.war
```

`--sms-agent-sha256` 을 주면 보내기 전에 파일을 그 값과 대조해 다르면 아무것도 보내지 않고 멈춘다(T 와 S 가 같은 파일인지 눈 대조에만 기대지 않게). `--war` 는 WAS 묶음이 늘 요구해서 준다(`was` 단계를 돌지 않으므로 WAR 는 놓이지 않는다). 모든 단계를 돌거나 `--steps` 에 `sms-agent` 가 있으면 `--sms-agent` 가 없을 때 `apply: <서버> 에 --sms-agent 필요` 로 멈춘다. 단계는 바이너리 체크섬(묶음 `SHA256SUMS`)을 본 뒤 자리를 만들고, 바이너리, `agent.env`, 유닛 중 하나라도 바뀌었거나 유닛이 돌지 않을 때만 다시 띄운다(다시 돌려도 평시에 없는 재시작을 남기지 않는다). 에이전트 로그의 새 시작 줄 `agent started (... host=<업무 주소>, server=<보고 주소>, ...)` 를 60초 안에 기다리고(settle), 설치본 체크섬을 `/var/lib/petition-deploy/lucida-agent.sha256` 에 적는다.

`verify` 는 서버마다 `sms-agent-active`(유닛), `sms-agent-binary`(도는 root 의 `lucida-agent` 하나가 설치본이고 설치본이 적힌 체크섬과 같음), `sms-agent-env`(`agent.env` 의 두 줄이 인벤토리 값)를 더 본다. 설치 기록(`/var/lib/petition-deploy/lucida-agent.sha256`)도 자리(`/opt/lucida-agent`)도 없는 서버(등록 창 전)는 세 항목 대신 `CHECK sms-agent NOT-INSTALLED` 한 줄만 찍는다. 하나라도 있으면 세 항목을 모두 본다. 등록 창 전(T2, S 는 ST2)에 서버를 처음부터 설치할 때는 `--skip-steps sms-agent` 를 준다(모든 단계를 돌면 에이전트가 깔려 관제 서버에 등록 대기 대상이 생긴다). 역할마다 단계 목록이 달라 `--steps` 로 손으로 적으면 단계를 빠뜨리거나 다른 역할의 단계를 돌리기 쉽다.

되돌리기(서버마다, 운영자 계정):

```
sudo systemctl disable --now lucida-agent && sudo rm -f /etc/systemd/system/lucida-agent.service && sudo systemctl daemon-reload
sudo rm -rf /opt/lucida-agent /var/lib/petition-deploy/lucida-agent.sha256
```

에이전트를 먼저 멈춘 뒤에 관제 서버의 대상을 지운다(§12-5). 순서가 거꾸로면 다음 보고에 등록 대기 대상이 다시 생긴다.

## 12. 관제 서버(119) 등록 (T3)

설계 §4 의 등록 대상만, 제품 화면이 부르는 것과 같은 API 로 한다. 도구는 `deploy/tools/t3_register.py` 이고 단계마다 `--yes` 없이 먼저 돌려(읽기와 로그인만, 쓸 호출은 찍기만) 본 뒤 `--yes` 로 쓴다. 계정은 러너 계정이다. 값은 러너 컨테이너 환경변수에서 셸 변수로만 옮기고 출력하지 않는다.

```
OPS="ssh -o BatchMode=yes -o UserKnownHostsFile=$HOME/.ssh/known_hosts_tb66 -J root@192.168.200.66 nkia@192.168.122.20"
export LUCIDA_LOGIN_USER=$($OPS docker exec scenario-runner printenv LUCIDA_LOGIN_USER)
export LUCIDA_LOGIN_PASSWORD=$($OPS docker exec scenario-runner printenv LUCIDA_LOGIN_PASSWORD)
RT="ssh -o BatchMode=yes -o UserKnownHostsFile=$HOME/.ssh/known_hosts_civil-petition -J root@192.168.200.66 sysop@192.168.127.2"
export LUCIDA_QUERY_URL=http://192.168.230.119:13080            # 러너와 같은 주소
R="python3 deploy/tools/t3_register.py --inventory $I"
```

관제 서버에 쓰는 일은 이 절의 단계 안에서만 한다. 수집 정책은 건드리지 않는다(장비 개별 덮어쓰기 없음, 서버 ICMP 는 기본 켬 그대로, 설계 §4-1). DPM 은 등록하지 않는다.

### 12-0. 적용 순서 (시각 기준: 부하 시작 11-18 12:42:48 KST + 26시간 = 11-19 14:42:48 KST 이후)

| 순서 | 할 일 | 명령, 확인 |
|---|---|---|
| 0 | **T2 평시 24시간 판정을 먼저 끝낸다.** db-after, facts-after 를 뜨고 그 시각 E 로 `stability_check.py --end E`(§8). E 는 가장 이르면 11-19 14:42:48 KST | 판정 구간은 [E-24시간, E] 이고 대표 질의는 처음 구간(판정 구간 앞 2시간, [E-26시간, E-24시간])과 끝 구간(마지막 2시간)을 견준다. E 가 부하 시작 + 26시간 이상이어야 하는 까닭이 처음 구간이다. 아래 어느 것도 E 전에 하지 않는다(링크 내림의 부하 오류, 에이전트 설치가 판정 창에 들어가지 않게) |
| 1 | 사전 확인과 녹화 정지(지휘 세션). 119 디스크 80% 미만(`df -P /`, 11-18 13:1x 조회 55%), 터널 핸드셰이크, 러너 idle | 등록은 녹화 시작 차단(2시간 30분)을 한 번 받는다(설계 결정 7). 차단 시계는 관제 서버에 마지막으로 쓴 때(순서 8 의 끝, nic 이나 log-monitors 를 다시 돌렸으면 그 끝)부터 센다 |
| 2 | Q8 등록 전 사실(캡처 구간을 빼고 재는 것은 도구가 한다) | `python3 deploy/tools/t3_check.py --inventory $I --budget --start <지금-24시간> --end <지금> --out t3-budget-before.json` |
| 3 | trap 수신기, 라우터 NMS 장비 | `$R --phase trap`, `$R --phase nms`(각각 `--yes` 없이 본 뒤 `--yes`). 수신기가 있어야 관제 서버가 UDP 162 를 연다. 2분 뒤 `t3_check.py --since <지금>` 의 NMS 행 |
| 4 | **trap 확인(T3 의 첫 주입 단계).** 라우터 하위 인터페이스 하나(인벤토리 첫 하위, 웹 쪽 `ens19`)를 90초 내렸다 올린다(snmpd 링크 감시 주기 60초보다 길게). 웹망만 끊겨 부하기 요청이 그동안 실패하고 WAS, DB 연결은 그대로다 | 시각 T 를 적고 `$RT "sudo -n sh -c 'ip link set dev ens19 down; sleep 90; ip link set dev ens19 up' </dev/null >/dev/null 2>&1 &"`(관리망 세션이 끊겨도 올림이 돈다). 3분 뒤 `$RT ip -br link show ens19` 가 UP, `t3_check.py --since T --trap-since T` 의 trap 행(linkDown, linkUp 각 1 이상, 출발 192.168.123.254). 되돌리기: `sudo ip link set dev ens19 up` |
| 5 | SMS 에이전트 네 대 | §11 |
| 6 | 등록 대기 서버 승인. 승인 시각 R 을 적는다(이후 판정 창의 시작) | `$R --phase approve`. 등록 대기 대상의 이름이 업무 주소이고 주소도 업무 주소일 때만 승인한다(관리 주소면 ICMP 가 닿지 않아 멈춘다) |
| 7 | 관리 NIC 를 수집에서 뺌(Q7d) | 승인 뒤 첫 지표(약 1~2분)로 NIC 하위 대상이 생긴 다음 `$R --phase nic`. 하위 대상이 아직 없으면 멈추고 다시 돌리라고 알린다 |
| 8 | 로그 감시 | `$R --phase log-monitors`(역할별 `deploy/monitoring/log-monitors.json`, 같은 파일 감시가 있으면 건너뜀) |
| 9 | 대상 번호 표 | `$R --phase record --out deploy/inventory/t.targets.json`(쓰기 없음, 꼴 §12-4). 저장소 커밋은 지휘 세션 |
| 10 | 바로 확인 | `t3_check.py --inventory $I --since R --table deploy/inventory/t.targets.json`(§13). 밴드와 부담은 24시간 뒤 |
| 11 | 24시간 뒤. `--vm-reads` 는 서버 네 대에서 운영자 sudo(`ls /proc/<pid>/fd`, 감시 파일 `grep -c`)를 돌려 sudo 줄이 관제 서버 syslog 와 auth.log 감시(warn 패턴)에 남는다. 그래서 이 줄은 **녹화 정지와 잠금 아래에서** 돌리고, 끝난 때부터 2시간 30분 규칙을 새로 센다. `--vm-reads` 를 뺀 판정과 예산은 녹화 중에 돌려도 된다(관제 서버 읽기만) | `t3_check.py --since R --until R+24시간 --trap-since T --table ... --vm-reads --json t3-check.json`, `t3_check.py --budget --start R --end R+24시간 --out t3-budget-after.json`, `t3_check.py --budget-verdict --before t3-budget-before.json --after t3-budget-after.json` |
| 12 | 원칙 문서 §3 수집 특성 표에 이 도메인 행(더하기만) | 값은 11 의 출력(SMS 표본 간격, NMS 폴링 간격, ICMP 표본)과 §12-3 |

처음 설계(§10 T3 trap 행)는 trap 확인을 "T2 끝 부하 시작 전" 에 두었다. 부하가 11-18 12:42:48 KST 에 이미 켜졌으므로 11-18 지휘 세션 결정으로 평시 판정 뒤 T3 의 첫 주입 단계로 옮겼다. 수신기 등록(3)이 링크 내림(4)보다 먼저인 것은 수신기가 없으면 관제 서버가 UDP 162 를 듣지 않아 trap 이 버려지기 때문이다.

### 12-1. 단계가 부르는 API (lucida-next develop-ai d0186576d2 의 화면 코드와 같은 꼴)

| 단계 | 호출 | 되돌리기 |
|---|---|---|
| approve | `GET /api/v1/targets?type=server&registration_status=pending&search=<업무 주소>` → `POST /api/v1/targets/approve {"ids":[...]}`(그룹과 수집 모드는 보내지 않음 = 화면 기본) | 승인 취소 API 없음. 대상 삭제(§12-5) |
| nic | `GET /api/v1/targets/<서버>/subtree` 의 `resource_type=sms.network_interface`, `resource_key=<관리 NIC>` → `PUT /api/v1/authz/nodes/target/<하위>/state?scope=unified {"state":"unmanaged"}`. 에이전트 정책의 관리 안 함 목록으로 내려가 에이전트가 그 NIC 를 수집하지 않고 서버도 적재 전에 버린다 | `DELETE /api/v1/authz/nodes/target/<하위>/state?scope=unified`(상위 상속으로 돌아감) |
| log-monitors | `GET /sm/api/custom-monitors?kind=log&parent=<서버>&owner=sms` → 없는 파일만 `POST /sm/api/custom-monitors`(kind log, enabled, 감시 꼴은 기존 세 도메인 감시와 같게 `unified_log_enabled` true 와 `log_type_tag`(syslog, auth.log 는 system, DB 서버 로그는 database, 앱 파일은 없음, 11-18 지휘 세션 결정. 두 칸은 collector-sms 에서 저장만 되고 적재 경로에 쓰이지 않는다), params 는 화면 기본 `matchingType Exact`, `scanType End`, `encodingType NOT_SPECIFIED`, `caseSensitive` true, 레벨별 패턴과 빈 제외 패턴). `/sm/` 은 웹 게이트웨이가 SMS 수집기로 넘기고 같은 세션 쿠키로 인가한다 | `DELETE /sm/api/custom-monitors/<id>` |
| nms | `POST /api/v1/targets {"type":"network","name":"<라우터 이름>","address":"<상향 주소>","discovery":"manual","meta":{"collector":"polestar-nms","group":"Polestar NMS","endpoint_port":"161","name_sync":false}}` → `POST /api/v1/targets/<id>/collectors {"kind":"polestar_nms","enabled":true,"config":{"port":161,"version":"2c"},"config_encrypted":{"community":<t4-snmp-community 파일 값>},"collection_policy_id":"00000000-0000-4000-8009-000000000001"}`. 수집기를 못 만들면 방금 만든 대상을 지운다(화면과 같음). 인터페이스 하위 대상(`network_resource`)은 폴링이 스스로 만든다. 장비 폴링이 매번 장비 주소를 핑한다(`nms.network.icmp_status`, `rtt`) | `DELETE /api/v1/targets/<id>` |
| trap | `POST /api/v1/targets {"type":"network","name":"trap-udp-162","discovery":"manual","meta":{"collector":"polestar-trap","group":"Polestar TRAP","endpoint_port":"162","endpoint_protocol":"udp"}}` → `POST .../collectors {"kind":"polestar_trap","enabled":true,"config":{"port":162,"protocol":"udp","v3_username":"","v3_auth_protocol":"","v3_priv_protocol":"","allow_unregistered":false}}`(커뮤니티 거르기 없음 = 화면 기본). 라이선스 `trap-receiver` 수량 10000(11-18 조회). trap 출발 주소를 등록된 network, server 대상 주소와 맞춘다. 수신기는 collector-trap(hostNetwork)이 관제 서버 모든 주소의 UDP 162 를 열게 하므로, 관제 서버 실주소로 trap 을 보내도록 설정된 기존 장비가 있으면 그 줄도 받기 시작한다(11-18 90초 관찰 0 패킷). 확인 도구가 라우터 아닌 출발 trap 수를 기록한다 | `DELETE /api/v1/targets/<id>` |

### 12-2. 등록하지 않는 것과 그대로 두는 것

- DPM(db01 PostgreSQL), loadgen, 66 호스트, APM, KCM, WPM.
- 서버 ICMP 와 RTT, 가용성 스위퍼 핑, 라우터 폴링 ICMP: 제품 기본(설계 §4-1).
- 에이전트 등록 때 제품이 스스로 다는 «기본 로그 모니터 (Linux)»(`/var/log/messages`, Ubuntu 에는 없는 파일): 제품 기본이라 그대로 둔다.
- syslog 수신기: P2 에서 이미 udp, tcp 514 로 등록됐다. VM 과 라우터의 줄은 지금도 들어온다(`registered=0`). 서버와 라우터를 등록하면 출발 주소가 대상 주소와 맞아 귀속된다.

### 12-3. 미리 확인한 제품 동작 (11-18 KST, lucida-next d0186576d2 코드와 119 읽기)

| 물음 | 답 | 근거 |
|---|---|---|
| SMS 서버는 손 등록이 필요한가 | 아니다. 에이전트 첫 보고에 `targets`(type server, 이름 = `LUCIDA_HOST`, `registration_status=pending`)가 생기고, 승인 전에는 지표를 저장하지 않는다. 자동 승인 설정은 없다 | `collector-sms/store/pg.go` `UpsertTargetForHost`, `registration_gate.go`. 119 `SM_BEARER_REQUIRED=false` |
| 관리 NIC 를 SMS 수집에서 뺄 수 있나(Q7d) | 있다. 에이전트 환경변수로는 못 빼고(NIC 제외 변수 없음, IPv4 가 있고 UP 인 NIC 를 모두 수집) 그 NIC 하위 대상을 관리 안 함으로 두면 빠진다 | `goagent/metrics.go` `nicBlocked`, `agent_ingest.go` |
| 에이전트가 journal 을 읽나 | 읽지 않는다. 서버가 내려 준 파일만 tail 한다. 기존 작업 노드 에이전트의 열린 파일에도 journal 이 없다(tb-w1 조회: `agent.log`, cgroup `cpu.max`, 잠금 파일뿐) | `goagent` 에 journal, sd_journal 없음 |
| 프로세스 계열의 단위 | (호스트, 프로세스 이름, pid). 15초마다 CPU 상위 50개를 보낸다. 프로세스 하위 대상은 «프로세스» 하나이고 프로세스별 대상은 사용자 정의 감시로만 생긴다. `process_snapshot`(ClickHouse)은 60초마다 전체 목록(이름, 사용자, pid) | `goagent/main.go` `collectProcesses(cfg, 50)`, VM 라벨 `name`, `pid`, `host_target_id`. tb-w1 하루 고유 2,651 계열 |
| 가용성 판정 | 보고가 끊긴 지 180초(`SM_AVAIL_GRACE_SEC`) 뒤 스위퍼(30초 주기)가 핑해 실패면 DOWN, 성공이면 UNKNOWN. 약 3~3.5분 | `collector-sms/store/availability.go`, 119 환경변수 |
| 서버 ICMP 와 RTT 주기 | 수집 정책의 두 지표 주기(정책 행이 없으면 30초) | `rtt_prober.go` |
| 로그 감시 파일 이름의 날짜 | 에이전트가 `[YYYY] [MM] [DD]` 자리를 자기 시각(VM 은 UTC, Tomcat 파일 이름도 UTC 날짜)으로 채운다(`matchingType Exact`) | `goagent/custommon_file.go` |

### 12-4. 대상 번호 표 꼴 (`deploy/inventory/<테스트베드>.targets.json`)

`t3_register.py --phase record` 가 관제 서버를 읽어 만든다(설계 §10 T3 "대상 번호 표"). 사람이 고치지 않는다.

```
{"schema": "petition-portal-targets/1", "testbed": "t", "domain": "<도메인>",
 "servers": [{"name": "<게스트 이름>", "role": "web|was|db", "target_id": "<uuid>", "agent_host": "<업무 주소>",
              "children": {"<resource_type, 예 sms.network_interface>": {"<resource_key, 예 NIC 이름>": "<uuid>"}},
              "unmanaged": ["<관리 안 함으로 둔 하위 uuid>"],
              "log_monitors": {"<감시 파일>": "<감시 uuid>"}}],
 "network_devices": [{"name": "<라우터 이름>", "target_id": "<uuid>", "address": "<상향 주소>",
                      "interfaces": {"<인터페이스 키>": "<uuid>"}}],
 "trap_receivers": [{"name": "trap-udp-162", "target_id": "<uuid>", "protocol": "udp", "port": 162}]}
```

DB 대상은 없다. 서버 하위의 NIC, 파일 시스템, 프로세스, CPU, 메모리 대상 번호가 `children` 에 든다.

### 12-5. 되돌리기

1. 서버 네 대 에이전트를 먼저 멈추고 지운다(§11 되돌리기).
2. `$R --phase rollback --record deploy/inventory/t.targets.json`(찍기만) 뒤 `--yes`: trap 수신기, NMS 장비, 로그 감시, 관리 NIC 경계, 서버 대상 순으로 지운다. 2차 인증을 켠 설치면 삭제가 `X-Function-Auth` 를 요구해 4xx 로 멈춘다. 그때는 화면에서 지운다.
3. 등록 해제가 과거 데이터를 지우는지는 확인하지 않았다(설계 §13-7). 되돌리기 전에 지휘 세션이 정한다.

## 13. T3 완료 조건 확인 (`deploy/tools/t3_check.py`, 읽기 전용)

설계 §10 T3 표의 행마다 항목 하나다. 관제 서버 ClickHouse(파드 안 `clickhouse-client --readonly=1 --max_threads=2 --max_execution_time=60`, 모든 질의에 시각 범위), PostgreSQL(파드 안 psql, read only, statement_timeout 20초, 큰 열 안 읽음), VictoriaMetrics(104 에서 평가용 LB `:18428`), 119 호스트(`df`, `sadf`, wg0 바이트, `kubectl get`)만 읽는다. `--vm-reads` 일 때만 서버 VM 을 관리망 운영자 계정으로 읽는다(에이전트의 열린 파일, 감시 파일 `grep -c`). 끝 코드 0 은 모든 항목 충족.

| 행 | 읽는 것 | 통과 |
|---|---|---|
| SMS | VM `sms.*` 이름(호스트 대상 `host_target_id` 또는 `target_id`) | CPU, 메모리, 디스크, 파일 시스템, NIC, 프로세스 이름군이 모두 있음. 활성 계열 수, `sms.cpu.utilization` 표본 간격은 기록 |
| 관리 NIC | PG 하위 대상 관리 상태, VM 최근 10분 `sms.network_interface.*{device=<관리 NIC>}` | 관리 안 함이고 최근 계열 0 |
| journal | 코드 사실 + (`--vm-reads`) 에이전트 `/proc/<pid>/fd` | journal 파일 0 |
| 프로세스 계열 단위 | VM `sms.process.cpu_utilization` 라벨 | 계열이 있으면 라벨을 기록하고 충족(설계 행은 «기록»). 라벨이 `name`, `pid` 와 다르면 그 사실을 적는다 |
| ICMP | VM `sms.server.icmp_status`, `sms.server.rtt`, PG 대상 주소, `target_availability_latest`(server), `policy_device_overrides`(collection), 라우터 `nms.network.icmp_status`, `rtt` | 서버 넷 상태 1, RTT 계열, 주소 = 업무 주소, 가용성 UP, 덮어쓰기 0, 라우터 상태 1 과 RTT |
| NMS | PG 장비 하위 대상(`resource_kind` network_interface 의 `resource_key` = ifDescr, `meta.snmp_index`), VM 라우터 계열(`target_id` 장비, `snmp_index` 라벨) | 인벤토리의 상향과 하위 인터페이스(T 는 ens18 ~ ens21) 하나하나에 상태, 옥텟, 오류, 버림 계열이 모두 있음(관리 NIC, lo 는 세지 않음), 장비 `nms.cpu.usage` 있음. 폴링 간격 기록 |
| trap | CH `snmp_traps_local`(`--trap-since` 부터, 출발 라우터 상향 주소) | linkDown, linkUp 각 1행 이상. 참고(판정 아님): 같은 구간 라우터 아닌 출발 trap 수와 주소 |
| DPM 없음 | VM `dpm.*`(이 도메인 대상), PG type database 이고 주소가 db01 업무 주소 | 둘 다 0 |
| DB 서버 관제 | VM 데이터 마운트 파일 시스템, 디스크 IO, CH `process_snapshot` 의 postgres 행, CH 서버 로그 감시 줄, (`--vm-reads`) 서버 로그의 패턴 맞는 줄 수 | 앞 셋이 있고, 서버 로그 감시 줄은 로그 감시 행과 같은 규칙(줄이 없을 때 파일에도 맞는 줄이 없으면 실패 아님, 따로 셈) |
| syslog | CH `syslog_local` 출발 주소별 10분 칸 | 서버 넷과 라우터의 칸마다 1행 이상, 부하기 출발 0 |
| 로그 감시 | PG 감시 등록, CH `lucida_logs_local`(`event_source=log_monitor`, `file_path`), (`--vm-reads`) 파일의 패턴 맞는 줄 수 | 감시마다 줄이 있음. 줄이 없는데 파일에도 맞는 줄이 없으면 «줄 없음(파일에도 없음)» 으로 따로 센다(감시가 못 읽는 증거가 아님). 파일에 맞는 줄이 있는데 줄이 없거나, VM 을 읽지 않았으면 실패. 패턴에 맞을 줄을 일부러 만들지 않는다 |
| 탐지 밴드 | VM `sms.*.bands`, `nms.*.bands`(등록 24시간 뒤) | 대상마다 밴드 계열이 있고 24시간 최댓값 +Inf 0 |
| 부담(Q8) | `--budget` 사실 두 개(등록 전 24시간, 뒤 24시간): CH `system.asynchronous_metric_log` `MemoryResident`(1초 표본)를 녹화 캡처 구간 밖과 안으로 나눈 최댓값, p99, 캡처 순간 최댓값, 뺀 구간 목록, ClickHouse 상한(`max_server_memory_usage` 와 컨테이너 한도 중 낮은 것, 11-19 09:3x 조회 7.2GiB), `sadf -r` 노드 가용 메모리 최솟값, VM 상주 메모리 최댓값과 파드 한도, 디스크, `sadf -u` CPU 와 steal 평균, wg0 바이트 | 뒤 사실의 캡처 뺀 최댓값과 p99 가 3.6GiB 미만, 노드 가용 메모리 최솟값이 'ClickHouse 상한 - 캡처 뺀 p99' 이상, VM 한도 80% 미만, 디스크 80% 미만, steal 증가 3%p 이하, CPU 70% 이하. 따로(Q8 판정과 별개) **캡처 순간 여유**: 두 사실 가운데 큰 캡처 순간 최댓값 + T, S 추정 증가분(0.22GiB, 설계 §3-6)이 ClickHouse 상한의 85% 미만(넘으면 T3 는 진행하고 ST3 전에 검토 세션에 다시 묻는다). 참고(판정 아님): 캡처 포함 최댓값, 이 도메인 활성 계열, 구간 고유 계열, 원천 표 행, 터널 평균 |
| 대상 번호 표 | 표의 모든 번호가 PG `targets` 에 있음 | 없는 번호 0, 서버가 인벤토리 수만큼, 서버 하위에 NIC, 파일 시스템, 프로세스. 라우터가 있는 인벤토리면 장비 하나, 그 인터페이스 번호, trap 수신기 번호가 비지 않음 |

설계와 다르게 둔 것(판정 바꿈 기록): (1) 로그 감시 행과 DB 서버 관제 행의 서버 로그 감시 줄은 «줄이 없고 VM 파일에도 패턴에 맞는 줄이 없으면 실패가 아니라 따로 센다»(설계 문구 "감시 항목별 행이 생김" 보다 느슨, 11-18 지휘 세션 결정. 맞는 줄을 일부러 만들지 않는다). (2) Q8 의 VictoriaMetrics 는 설계 "파드 메모리" 대신 VictoriaMetrics 자기 지표 `process_resident_memory_bytes`(프로세스 상주 메모리)의 구간 최댓값을 파드 한도와 견준다(구간 최댓값을 다시 읽을 수 있는 기록이 이것뿐이다). (3) 관리 NIC 행은 관리 안 함 상태와 최근 계열 0 을 함께 본다(설계보다 엄격). (4) ClickHouse 예산은 하루 최댓값 하나가 아니라 녹화 캡처 구간을 뺀 최댓값과 p99, 노드 가용 메모리로 판정한다(검토 세션 Q31 답, 11-19). 녹화 캡처 구간의 정의는 `t3_check.py` 의 `capture_intervals` 한 곳이다: 출처는 119 ClickHouse `system.query_log` 의 lucida 표 `FORMAT Parquet` 내보내기(끝났거나 실패한 것, 시작 `query_start_time`, 끝 `event_time`)이고, 내보낸 시각 범위가 하루의 00:00~02:00 KST 인 평시 구간 덤프(`scripts/dump-normal-segment.sh`, 날마다 도는 평시 작업이라 남긴다)는 빼며, 600초 안의 내보내기는 한 구간으로 묶고 앞뒤 60초를 더한다. 뺀 구간 목록은 결과에 함께 찍힌다. 기준값(3.6GiB, 80%, 3%p, 70%, 85%, 증가분 0.22GiB, CPU 와 노드 가용 메모리 정의)은 `deploy/monitoring/t3-budget.json` 한 곳에 있다. 앱 로그 감시 패턴은 `deploy/monitoring/log-monitors.json` 이고 Q31 답으로 확정했다(시나리오가 안 잡혀도 고치지 않는다).

11-18 13:1x KST 조회(등록 전, 지난 24시간): ClickHouse `MemoryResident` 최댓값 **3.475GiB**(예산 3.6GiB 까지 0.125GiB), VictoriaMetrics 상주 최댓값 2.47GiB / 한도 8GiB, 디스크 55%, CPU 42.5%, steal 1.36%.

## 14. S 적용 (ST2)

S 는 같은 코드, 같은 WAR, 같은 단계를 인벤토리 `deploy/inventory/s.json` 으로 돈다. 아래는 **T 절차와 다른 것만** 적는다. 적지 않은 것(명령 꼴, 확인 방법, 멈춤과 되돌리기)은 같은 번호의 T 절을 따른다. 설계는 S 문서 §2-2(VM), §2-3(바꾸는 것과 빼는 것), §3(망), §11 ST2(완료 조건)다. 66 에 닿는 차례는 테스트베드 잠금과 녹화 정지 창 아래에서만 한다.

### 14-1. T 와 다른 점

| 항목 | T | S |
|---|---|---|
| 인벤토리, 도메인 값 | `t.json`, `civil-petition` | `s.json`, `civil-petition-sms` |
| VM | 201~206, 6대(라우터 포함), vCPU 14, 22GB, 256GB | 211~215, 5대, vCPU 12, 20GB, 240GB |
| 66 망 파일 | `t4-net`, `t4-net.rules`, `chrony/conf.d/t4-net.conf`, `modules-load.d/t4-net-qdisc.conf` | `s5-net`, `s5-net.rules`, `chrony/conf.d/s5-net.conf`(큐 모듈 파일 없음, 하이퍼바이저 실행기 없음) |
| 66 브리지 | vmbr7~11, 66 주소는 상향과 관리만 | vmbr12(업무 131), vmbr13(사용자 132), vmbr14(관리 133), 셋 다 66 주소 .1 |
| 66 NAT | MASQUERADE 4, SNAT 1 | MASQUERADE 2(131, 132), SNAT 1(러너 → 133.1) |
| 서버의 기본 경로 | 라우터(각 망 .1) | 66(192.168.131.1), 부하기는 66(192.168.132.1) |
| 119 가 서버에 닿는 길 | 66 → 라우터 상향 → 하위망 | 66 에서 바로 업무망(경로 줄 없음, 연결된 망). 터널 대역은 `add-net 192.168.131.0/24` |
| 빠지는 단계 | 없음 | `syslog-forward`, `lldpd`, 라우터 전부(`router` 단계, `rt-core01`) |
| 비밀값 파일 | `t4-executor.pub`, `t4-operator.pub`, `t4-petition-db`, `t4-snmp-community` | `s5-executor.pub`, `s5-operator.pub`, `s5-petition-db`(SNMP 없음) |
| 관리 접속 known_hosts | `~/.ssh/known_hosts_civil-petition` | `~/.ssh/known_hosts_civil-petition-sms` |
| 부하 요약 | `/tmp/k6-civil-petition-live.json`, `/var/lib/k6/k6-civil-petition-windows.jsonl` | `/tmp/k6-civil-petition-sms-live.json`, `/var/lib/k6/k6-civil-petition-sms-windows.jsonl` |
| 스냅숏 | `t2-baseline` | `st2-baseline` |
| 패키지 색인 | `t.json` 서버별 `apt_snapshot`(처음 설치 시각, 다시 깔 때 같은 색인, §6) | `s.json` 서버별 `apt_snapshot` = 짝 T 서버 값(14-3-1) |

T 적용(11-18 APT2)에서 고친 저장소 결함(NIC 이름, 브리지 IPv6, rsyslog 검사 사본과 AppArmor, 데이터 디스크 serial, psql 표준 입력, no-dpm-traces, 66 ICMP 좁힘, 관리 SSH notrack, settle 다시 읽기)은 모두 같은 코드라 S 에도 걸린다. 라우터, syslog 전달, lldpd 쪽 고침은 S 에 해당 없다.

### 14-2. 언제

선행: T2 끝(설치와 §7 점검), T0 터널. **S 는 T 평시 판정 끝 E(가장 이르면 2026-11-19 14:42:48 KST) 뒤에 짓는다**(11-18 지휘 세션 결정). 까닭: 계획 순서가 ST2 를 T 뒤에 두고, T 판정 창([E-26시간, E])에 S 의 VM 생성, DB 시드, 부하가 66 에 더해지지 않게 하기 위해서다. 차례는 §12-0 의 T3 적용(관제 서버 등록)과 같은 66 변경 창에 이어서 한다: T3 의 순서 1(녹화 정지, 잠금)~10 을 마친 뒤 같은 정지 창 안에서 아래 차례 0~6 을 하고, 부하 시작(차례 8) 뒤 창을 닫는다. 2시간 30분 녹화 시작 차단은 이 창 안 S 의 마지막 변경 끝부터 센다(차례 7 의 가드 시험과 차례 8 의 S 부하 시작 포함). S 부하 시작은 66 경합 수준을 바꾸므로 그 앞에서 세면 다음 녹화의 평소 구간(t0 앞 3시간)에 S 부하 시작 전후가 섞인다. 차례 8 확인 칸에 그 시각을 적는다. S 의 ST3(119 등록)는 S 평시 24시간 판정 뒤라 T3 창에는 들지 않는다(따로 창을 연다).

### 14-3. 차례

`I=deploy/inventory/s.json`, 저장소 뿌리는 `civil-petition/` 위.

| 차례 | 할 일 | 확인 | 되돌리기 |
|---|---|---|---|
| 0 | 사전(읽기). 66 `ip -br link \| grep -E 'vmbr1[234]'` 빈 출력, `qm status 211`~`215` 없음, `iptables-save \| grep -c S5-` 0, 자원(아래 14-5) | 빈 출력, 0 | 없음 |
| 1 | 비밀값 파일(§3 과 같은 명령, 이름만 `s5-`): tb-ops `ssh-keygen -q -t ed25519 -N '' -C opsjob -f /root/.ssh/s5-executor` 의 공개 키를 `~/.config/testbed/s5-executor.pub`, `install -m 0600 ~/.ssh/id_ed25519.pub ~/.config/testbed/s5-operator.pub`, `(umask 077; tr -dc A-Za-z0-9 </dev/urandom \| head -c 24 > ~/.config/testbed/s5-petition-db)`. SNMP 파일은 만들지 않는다 | `stat -c '%a %s %n' ~/.config/testbed/s5-*` 셋 모두 600 | 104 `rm ~/.config/testbed/s5-*`, tb-ops `rm /root/.ssh/s5-executor /root/.ssh/s5-executor.pub` |
| 2 | 66 망(§4). `render.py --inventory $I --hypervisor /tmp/s5-hv` 의 파일 셋을 66 같은 경로에. **기준은 T 망이 선 지금 뜬다**: `iptables-save > /root/s5-net.before`. `ifup vmbr12 vmbr13 vmbr14`, `systemctl restart chrony`(modprobe 없음) | `iptables-save > /root/s5-net.after`, `render.py --inventory $I --hypervisor-commands ipv6-state` 명령 출력을 `/root/s5-net.ipv6` 로, `hv_iptables_check.py --inventory .../s.json --mode up --before s5-net.before --after s5-net.after --ipv6-state s5-net.ipv6` 가 `PASS up`(늘어난 줄은 S5-FWD, S5-IN 과 그 훅 9, MASQUERADE 2, SNAT 1). **손 sysctl 없이** 판정한다(T2V6 의 "새로 만들 때 끔" 을 처음 보는 자리, §4). `ip -br link show vmbr12 vmbr13 vmbr14` 셋 다 UP. `iptables -t nat -S \| grep -c 192.168.122.0/24` 1, `iptables-save \| grep -c '^-A T4-'` 가 before 와 같음. `ip route` 에 131, 132, 133 은 연결된 망만(via 없음). 104 → `192.168.200.66:8091` 200, T 부하 요약(`/tmp/k6-civil-petition-live.json`)의 비정상 0 그대로 | §4 의 4 를 S 이름으로: `ifdown vmbr14 vmbr13 vmbr12`, `rm` 파일 셋, `systemctl restart chrony`, `--mode down --before s5-net.before` 가 `PASS down` |
| 3 | 터널 대역 더하기(터널 runbook §8): 104 에서 `./t0-apply.sh add-net 192.168.131.0/24`(잠금 아래). 119 설정 변경이라 2시간 30분 시계를 새로 센다 | 차례 4 뒤 `./t0-apply.sh verify --target 192.168.131.11`(포트 없이, S 서버는 10.250.0.1 의 ICMP 만 받는다) | 66, 119 각각 `tb-wg del-net 192.168.131.0/24`(잠금 아래) |
| 4 | VM 생성(§5). `plan.py --inventory $I --phase precheck` 를 66 에서, `scp ~/.config/testbed/s5-operator.pub root@192.168.200.66:/root/s5-operator.pub`, `plan.py --inventory $I --phase create --sshkeys /root/s5-operator.pub` | `qm list` 에 211~215 running, 크기는 S 문서 §2-2(저장소 시험 `SmsOnlyDesignTests` 가 같은 표를 대조한다). 각 VM `ssh -o UserKnownHostsFile=~/.ssh/known_hosts_civil-petition-sms -J root@192.168.200.66 sysop@192.168.133.x id -u` 가 1000. `ip -br link` 가 eth0, eth1 | §10(destroy 는 사람) |
| 5 | 서버 설치(§6). 적용 전 점검 `rsyslog-check.sh`(T, S 를 함께 본다). WAR 는 T 와 **같은 파일**(sha256 이 T 의 `/var/lib/petition-deploy/war.sha256` 과 같아야 한다), k6 도 같은 파일. **패키지 색인은 짝 T 서버가 apt 로 설치한 시각의 Ubuntu snapshot 색인**이다(`s.json` 서버별 `apt_snapshot`, `apply.sh` 가 넣는다, 아래 14-3-1 표). 차례 4 와 차례 5 의 `os-base` 를 VM 을 만든 다음의 06:00 UTC(15:00 KST) 전에 끝낸다(템플릿의 apt 자동 갱신 타이머가 그때 지금 색인으로 보안 갱신을 깔 수 있다. `os-base` 가 타이머를 끄고 unattended-upgrades 를 지운다). 판 고정도 함께 준다: 짝 T 서버의 지금 목록 `ssh ... sysop@<T 관리 주소> "dpkg-query -W -f '\${Package} \${Version}\n'" > t-pkgs/<T 이름>.txt`. 순서는 부하기 망 단계(§6-1, 관리 주소 192.168.133.10) → dbsvr01 → wassvr01, wassvr02 → websvr01 → 부하기. 라우터가 없으므로 서버의 바깥 길은 차례 2 로 이미 있다. 모든 서버를 `--skip-steps sms-agent` 로 돈다. 예: `deploy/apply.sh --inventory $I --host dbsvr01 --skip-steps sms-agent --package-versions t-pkgs/db01.txt` | `os-base` 전 각 VM `grep ^Start-Date /var/log/apt/history.log` 가 템플릿 항목(2026-09-26)뿐. 서버마다 os-base 출력에 `apt snapshot <ID>`, VM `/var/lib/petition-deploy/apt-snapshot` 가 그 ID. `verify` 의 CHECK 모두 PASS 와 `CHECK sms-agent NOT-INSTALLED`, `syslog-forward-matches-inventory` PASS(전달 파일 0). `plan.py --inventory $I --phase nics` 출력과 `nic ...` 줄의 MAC 대조. `--dry-run` 의 `steps:` 줄에 `sms-agent`, `syslog-forward`, `lldpd` 없음, `apt-snapshot:` 줄이 표의 ID(서버마다 다름) | §6 과 같다 |
| 6 | `plan.py --inventory $I --phase rename`(s5-*) | `qm list` 이름, 게스트 `hostname` 그대로 | `qm set <vmid> --name <게스트 이름>` |
| 7 | ST2 점검(14-4 의 `차례 7` 행) | 14-4 | |
| 8 | 부하 시작 `deploy/apply.sh --inventory $I --start-load`, 이어서 14-4 의 `차례 8 뒤` 행 | `/tmp/k6-civil-petition-sms-live.json` 생김, 비정상 0. T 부하 요약도 그대로. **부하 시작 시각(`systemctl show -p ActiveEnterTimestamp loadgen.service`)과 창 안 마지막 변경 끝 시각을 적는다(2시간 30분 시계의 시작, 14-2)** | 부하기 `sudo systemctl stop loadgen.service` |
| 9 | 평시 24시간, 규모 축(§8 을 S 이름으로), 스냅숏 `plan.py --inventory $I --phase snapshot`(`st2-baseline`) | 14-4 의 `차례 9` 행 | 스냅숏 지우기 `qm delsnapshot <vmid> st2-baseline` |

#### 14-3-1. 패키지 색인 시각 (쌍 조건 2)

`apt_install` 의 판 고정은 이름을 준 꼭대기 패키지만 묶고 의존 패키지는 그때 색인의 최신을 받는다. 그래서 S 는 짝 T 서버가 apt 로 설치한 시각의 Ubuntu snapshot 색인(`https://snapshot.ubuntu.com/ubuntu/<ID>/`, apt 2.8 의 `--snapshot`)으로 받고 설치한다. 시각은 T 서버의 `/var/log/apt/history.log` 첫 설치 명령의 `Start-Date`(VM 시각 UTC)이고 APT2 의 단계 시각과 맞는다.

| S 서버 | T 짝 | T apt 설치 시작(UTC) | KST, APT2 단계 | 인벤토리 `apt_snapshot`(두 인벤토리 같음) |
|---|---|---|---|---|
| websvr01 | web01 | 2026-11-18 02:20:12 | 11:20:12, §6-8(11:19:50 ~) | `20261118T022012Z` |
| wassvr01 | was01 | 2026-11-18 02:16:48 | 11:16:48, §6-7(11:16:02 ~) | `20261118T021648Z` |
| wassvr02 | was02 | 2026-11-18 02:18:27 | 11:18:27, §6-7 | `20261118T021827Z` |
| dbsvr01 | db01 | 2026-11-17 22:41:05 | 07:41:05, §6-3(07:40:18 ~) | `20261117T224105Z` |
| loadgen | loadgen | 2026-11-17 19:24:45 | 04:24:45, §4-1(04:24:04 ~) | `20261117T192445Z` |

미리 본 것(11-18 KST): T 서버에서 snapshot 저장소 `InRelease`(noble, updates, security)가 66 바깥 길로 200(loadgen, db01). 다섯 ID 의 색인에서 T 가 apt 로 깐 판(lldpd 단계 몫을 뺀 84 항목)이 모두 그 색인의 후보 판과 같다. 일회 `ubuntu:24.04` 컨테이너에서 이 판의 `apt_update`, `apt_install` 로 db01, web01 짝 묶음을 깔면 T 가 apt 로 깐 판과 같다(컨테이너에 없던 패키지 하나 빼고).

설치 단계는 스냅숏 색인을 못 받으면 멈춘다(apt 는 인증서나 길 문제로 못 받아도 경고만 내고 끝 코드 0 으로 옛 색인을 쓰고, 미래 시각에는 최신 색인을 준다). 그래서 `W:`, `E:` 줄, 미래 시각, 받은 색인 파일(`snapshot.ubuntu.com_ubuntu_<ID>_dists_<배포판>{,-updates,-security}_InRelease`)을 따로 본다. 멈추면 설치는 그 앞이라 VM 에 바뀐 패키지가 없다. 원인(인증서 `ca-certificates`, 66 바깥 길, snapshot 저장소 상태)을 보고 다시 돈다. 스냅숏 없이 깔지 않는다(쌍 조건 2 가 깨진다). 실제 apt 확인은 컨테이너였고 VM 에는 컨테이너에 없는 apt 훅(`20apt-esm-hook.conf`, `50command-not-found`, `99needrestart`)이 있다. 멈추면 `W:`, `E:` 줄 원문을 결과에 적고, 훅이 낸 줄이면 지휘 세션에 알린다(멈춤은 안전 쪽이라 그대로 둔다).

대조는 14-4 의 `pair_packages.py` 다. 짝마다 `dpkg-query -W` 전체 목록(의존 패키지와 템플릿 패키지 포함, 설치 상태 칸 포함)을 견준다. 넘기는 것은 T 에만 도는 `lldpd` 단계가 깐 넷과 올린 하나뿐이다.

### 14-4. ST2 점검 (S 문서 §11 ST2 행마다)

`차례 7` 행은 S 부하 전에, `차례 8 뒤` 행은 S 부하를 켠 뒤(T 와 S 부하 요약, 부하 중 119 읽기), `차례 9` 행은 S 부하 시작 + 26시간 뒤와 녹화 없는 창에서 한다.

| 조건 | 언제 | 명령(어디서) | 기대 |
|---|---|---|---|
| VM 5대 크기와 주소 | 차례 7 | 66 `qm list`, 각 VM `ip -br addr` | S 문서 §2-2. websvr01 131.11, wassvr01 131.21, wassvr02 131.22, dbsvr01 131.31, loadgen 132.10, 관리 133.x |
| 경로 | 차례 7 | 서버 `ip route`, 부하기 `ip route` | 서버 기본 경로 `via 192.168.131.1 dev ens18`, 부하기 `via 192.168.132.1 dev ens18`, 관리 NIC(ens19)에 기본 경로 없음 |
| 기존, T 영향 없음 | 차례 8 뒤 | 66 `iptables -t nat -S`(줄 수), 104 → 8091, 기존 VM 바깥 통신, T 부하 요약, websvr01 `nc -z -w 3 192.168.124.11 80`, `ping -c 2 192.168.122.20`, T web01 `nc -z -w 3 192.168.131.11 80`, 66 `iptables -nvxL S5-FWD`(앞뒤 읽기) | nat 줄은 기존 2, T 5 그대로에 S 3 만 늘어남. 200. 정상. 비정상 0. 연결 셋 모두 실패하고 S5-FWD 의 해당 DROP 패킷 수가 는다 |
| 관리망 격리 | 차례 7 | websvr01 `curl -m 3 --interface 192.168.131.11 http://192.168.133.21:22`, 각 VM `nc -z -w 3 192.168.133.1 22`, `8006`, IPv6 는 차례 2 의 확인을 지금 상태로 다시(`PASS up`)와 §7 (3) 의 멀티캐스트 덧확인, 각 VM `ping -I ens19 -c 2 192.168.230.119`, `10.250.0.1`, 119(읽기) `ping -c 2 192.168.133.11` | 모두 실패. 첫 줄은 66 `S5-FWD` 의 `-o vmbr14 -j DROP` 패킷 수가 는다 |
| 66 관리면 보호 | 차례 7 | websvr01 `nc -z -w 3 192.168.131.1 22`, `8006`, loadgen `nc -z -w 3 192.168.132.1 22`, `8006`, websvr01 `ping -I ens18 -c 2 192.168.133.1`, `ping -c 2 192.168.131.1` | 연결과 133.1 핑은 실패(66 `S5-IN` 의 `-i vmbr12 -j DROP`, `-i vmbr13 -j DROP` 이 는다), 게이트웨이 핑(131.1)은 됨. IPv6 는 차례 2 의 `PASS up`(브리지 셋 `disable_ipv6=1`, 주소 0) |
| 서비스 기동 | 차례 7 | `systemctl is-active apache2 tomcat10 postgresql@16-main`, WAS `cd /var/lib/tomcat10/webapps && sha256sum -c /var/lib/petition-deploy/war.sha256` | active, `petition.war: OK` |
| 쌍 조건 1, 2 | 차례 7 | WAR: S 두 대와 T 두 대의 `/var/lib/petition-deploy/war.sha256` 이 같음. 설정: 저장소 시험 `SharedCodeTests`(정규화 체크섬과 설치 값). 커널: S 서버마다 `uname -r` 가 T 와 같음. 패키지: 짝마다 `dpkg-query -W -f '\${db:Status-Abbrev} \${Package} \${Version}\n'` 목록(설치 상태 칸 포함, 판 고정용 `t-pkgs/` 와 다른 꼴)을 `t-st/<T 이름>.txt`, `s-st/<S 이름>.txt` 로 떠서 `python3 deploy/tools/pair_packages.py --t-inventory deploy/inventory/t.json --s-inventory $I --t-dir t-st --s-dir s-st --allow-t-only lldpd --allow-t-only libsnmp40t64 --allow-t-only libsnmp-base --allow-t-only libevent-2.1-7t64 --allow-version libevent-core-2.1-7t64` | 같음, OK, 같음, `PASS`. 넘기는 다섯 이름은 T 에만 도는 `lldpd` 단계가 깐 것과 올린 것이다(11-18 T 서버 apt 기록, S 문서 §2-3 "빼는 것"). 그 밖의 차이가 나오면 쌍 조건 2 가 깨진 것이니 멈추고 알린다 |
| 시드 크기 | 차례 7 | dbsvr01 `sudo -u postgres psql -Atc 'select count(*) from petition' petition` | 2,000,000 ±5% |
| 평시 24시간 | 차례 9 | §8 의 명령을 S 이름으로(websvr01 apache2, wassvr01, wassvr02 tomcat10, dbsvr01 postgresql@16-main, 라우터 없음. 창 기록 `k6-civil-petition-sms-windows.jsonl`) | `PASS_Q3_24H`(부하 시작 + 26시간 뒤) |
| 규모 축 | 차례 9 | §8 의 명령. CPU 표본은 wassvr01, wassvr02, websvr01, dbsvr01, hypervisor, 판정은 `scale_verdict.py --inventory $I ...`(라우터 표본 없음). 녹화가 없는 창에서, T 부하는 그대로 둔다 | `PASS_2X_60RPS_30M`(66 CPU 70% 이하는 T 부하를 포함한 값) |
| 시간, 자동 갱신, APM 흔적, 커널 모듈 | 차례 7 | 설치의 `verify` | PASS(서버 4대 kernel-modules) |
| syslog 전달 없음 | 차례 8 뒤 | `verify` 의 `syslog-forward-matches-inventory`, 다섯 대 `ls /etc/rsyslog.d/remote/`, 119(읽기) 관제 서버 ClickHouse(§13 의 읽기 설정) `select count() from lucida.syslog_local where received_at >= '<차례 2 시각 UTC>' and (source_ip like '192.168.131.%' or source_ip like '192.168.132.%' or source_ip like '192.168.133.%')` | PASS, 빈 디렉터리, 0 |
| rsyslog 첫 규칙 | 차례 7 | §7 과 같게 실행기 계정(tb-ops `/root/.ssh/s5-executor`, 66 SNAT 133.1)으로 접속, `sudo -n bash -s` 로 가드 arm 하나 | VM `/var/log/syslog`, `/var/log/auth.log` 에 그 계정, uid, `run-r` 줄 0, `/var/log/opsjobs.log` 에 있음(119 로 가는 길이 없으니 119 는 보지 않는다) |
| 가드 단독 시험 | 차례 7 | §7 의 U8 시운전 절차 2번을 서버 1대(websvr01)에서 | 통과 |
| DPM 흔적 없음 | 차례 7 | `verify` 의 `no-dpm-traces` | PASS |

### 14-5. 66 자원 (2026-11-18 16:15 KST 읽기)

| 항목 | 지금 | S 를 더하면 |
|---|---|---|
| 실행 중 VM vCPU(48 스레드) | 46(기존 6대 24, dm-train 8, T 14) | 58(121%) |
| 실행 중 VM 메모리 할당(251.6GiB) | 102GB | 122GB |
| 66 사용 메모리 하루 최댓값 | 117.9GiB | 최대 약 138GiB(S 20GB 를 모두 쓴다고 보면) |
| 66 CPU(하루 1분 평균의 평균과 최댓값) | 9.4%, 22.7%(10초 순간값 12.6%) | S 2배 시험에서 70% 이하인지 잰다 |
| vmdata(thin, 6.68TiB) | 3.76% | S 할당 240GB 를 모두 써도 약 7.3% |

S 문서 §0-1 의 "할당 24 → 50 vCPU(104%)" 는 dm-train(8 vCPU, 32GB)이 생기기 전 값이다. 메모리와 디스크는 넉넉하다. vCPU 는 할당이 스레드보다 많지만 실측 사용이 낮아 판정은 S 문서 §11 ST2 규모 축(66 CPU 70% 이하, S VM steal 5% 이하)으로 한다.

### 14-6. S 되돌리기

차례 역순: 부하 정지 → VM(§10, 사람) → 66 망(차례 2 의 되돌리기, `--mode down` 기준 `s5-net.before`) → 터널 대역(차례 3) → 비밀값 파일(차례 1). T 를 함께 되돌릴 때는 S 를 먼저 되돌린다(S 기준은 T 가 선 상태에서 떴다).

## 수정 기록

- 2026-11-16 KST, v4 T2(ag-t2): 처음 작성. 적용 전.
- 2026-11-17 KST, v4 T2C(ag-t2c): 검토 세션 Q20-3, Q21 답 반영. §9 서버 로그 값마다 출처와 이유(값은 그대로), §8 평시 24시간에 DB 사실(`db-facts.sh`)과 정상 상태 기준(주 표 행 수, 보관 배치, 대표 질의 p95), 보관 배치 설정과 디스크 문단, §11 서버 ICMP 는 수집 정책 기본값대로 켬.
- 2026-11-17 KST, v4 T2C 2회차(리뷰 rv-t2c): §8 대표 질의 구간을 24시간 떨어진 같은 시계 구간으로, `--end` 를 db-after 시각으로, 배치 로그 `result=` 가 앱 결과(ok, skipped, partial, busy)를 따름, 보관 SQL 확인 도구 실행법, §9 문구(출처 없는 서술 뺌, GitLab 은 목록).
- 2026-11-17 KST, v4 T2C 3회차(재리뷰 L8, L10): §8 26시간 전 판정은 INCOMPLETE, 구간마다 표본 창 22개, `db-facts.sh` 가 `last_archive_ok`(마지막 보관 ok 시각)도 낸다(설계 §6-4 의 기록용).
- 2026-11-17 KST, v4 T2C 5회차: 시드 크기 서술(처리 이력 약 450만, 정상 상태 꼴), 보관 SQL 확인 도구의 통과 기준.
- 2026-11-17 KST, v4 T2C 5회차(D): db01 에 표 통계 사본 타이머 `pg-table-stats.timer`(30분, 설치 단계 `db-backup`, `verify` 의 `table-stats-timer`), 결과 `/var/lib/pg-table-stats/last.json`.
- 2026-11-17 KST, v4 T2C 7회차(Q23): 부하기 담당자 처리 11%, 통계 화면 6%, 시드의 열린 건 0.15%(처리 중 12.5%), `archive-sql-check.sh` 의 시드 기준을 그에 맞춤.
- 2026-11-17 KST, v4 T2C 8회차(Q24): §8 평시 24시간에 밤 배치 처리 능력 판정과 `design_load.py`.
- 2026-11-18 KST, v4 T2NIC(ag-t2nic): §5 NIC 이름(새 VM 은 eth0 ~ , cloud-init 의 set-name), §6-1 network 단계가 MAC 맞춤과 set-name 으로 인벤토리 이름(ens18 ~ )을 주는 순서, 되돌림 타이머, 확인과 세션이 끊겼을 때.
- 2026-11-18 KST, v4 T2NIC 2회차(리뷰 rv-t2nic M1, M2, L1 ~ L3): §6-1 이름에 기대는 단계의 첫 줄 이름 확인과 그 제한, 손 되돌리기는 firewall 앞에서만과 되돌린 뒤 순서, 앞 되돌림이 남았을 때 멈춤, 적용 120초 상한과 확정 전 발화 확인, 서버마다 `plan.py --phase nics` 대조, 확인 ssh 의 known_hosts.
- 2026-11-18 KST, v4 T2NIC 3회차(재리뷰 rv-t2nic N1): §6-1 손 되돌리기에 보호 표 파일을 옮기는 줄(재부팅 뒤 다시 막지 않게).
- 2026-11-18 KST, v4 T2V6(ag-t2v6): ap-t2 적용 중 발견 두 가지. §4 브리지마다 IPv6 끔(`disable-ipv6 yes`)과 post-up 확인, 검사기 `--ipv6-state`(up 에서 반드시), 이미 올린 66 에 이 판을 놓는 법, §7 관리망 격리 (3) 에 IPv6 확인과 업무망 브리지 주소 0 에 IPv6 포함. §4 확인의 러너 API 판정을 실제로 되는 길(104 → 200.66:8091, tb-ops → 127.0.0.1:8091)로 바꿈.
- 2026-11-18 KST, v4 T2V6 리뷰 반영(rv-t2v6 M1, L1 ~ L3): 손 sysctl 이 이미 1 이면 다시 `ifup` 은 속성 동작을 증명하지 않음과 새로 만드는 길의 확인(S 첫 적용, 재부팅 뒤), post-up 실패에도 `ifup` 종료 코드 0 이라 판정은 검사기와 링크 상태로, `all.disable_ipv6` 를 쓰지 않음, §7 (3) IPv6 덧확인을 멀티캐스트 응답 출발지가 VM NIC 뿐인지로(브리지 MAC 은 tap 을 따라 바뀜).
- 2026-11-18 KST, v4 T2RSYS(ag-t2rsys, APT2 §4-2 멈춤): §6 적용 전 점검 `rsyslog-check.sh`(docker, 진짜 rsyslog 파서와 첫 규칙 동작), `syslog`, `syslog-forward` 단계가 사본 검사 뒤에만 놓고 실패 때 되돌림, `deploy/apply.sh` 실행 비트.
- 2026-11-18 KST, v4 T2RSYS 2회차(리뷰 rv-t2rsys H1, H2, M2, M3, L3): §6 검사 사본 자리(AppArmor 범위 안 `/etc/rsyslog.d/.check-<pid>/`)와 실제 읽기 확인, 원격 전달 자리 `/etc/rsyslog.d/remote/`(옛 `/etc/rsyslog.remote.d/` 는 AppArmor 가 막아 읽히지 않았음, 지휘 세션 결정)와 손으로 끄기, 실행기 uid 줄 확인, 점검 도구가 VM 과 다른 점과 다이제스트 고정, §7 설정을 실제로 읽음 행.
- 2026-11-18 KST, v4 T2RSYS 3회차(리뷰 rv-t2rsys 2회차 중간 보고, 지휘 세션 결정): §6 `syslog` 단계의 AppArmor 조각(로컬 소켓 주석의 `/proc` 읽기를 감사 없이 거부)과 다시 읽기, 재시작 뒤 `/proc` 거부 0 확인, 조각 손으로 빼기, 점검 도구의 `apparmor-fragment`, §7 `rsyslog-no-proc-audit`.
- 2026-11-18 KST, v4 T2VER(ap-t2 §7 점검): §7 관리망 격리 (2) 의 기대를 실제 규칙대로(web01 은 라우터 `mgmtguard prerouting` 거절 카운터, loadgen 은 66 `T4-IN`, `T4-FWD` 거절 카운터), 66 상향 브리지 ICMP echo 허용을 브리지 자기 주소로 좁힘(렌더러), 라우터 prerouting 관리 대역 거절에 counter.
- 2026-11-18 KST, v4 T2VER(ap-t2 §7 관찰, 지휘 세션 결정): 관리 SSH notrack(라우터와 서버 `mgmtguard`, conntrack 표가 가득 차도 정리 SSH 가 붙게)과 §7 notrack 행의 새 관리 SSH 확인, 가드 단독 시험 2-4 명령 `systemd-tmpfiles --boot --remove`, §7 WAR 체크섬 대조는 webapps 에서.
- 2026-11-19 KST, v4 T3P 3회차(검토 세션 Q31 답): §13 부담을 녹화 캡처 구간 뺀 최댓값과 p99, 노드 가용 메모리로, 캡처 구간 정의와 뺀 구간 목록, 캡처 순간 여유(상한 85%), 앱 로그 감시 패턴 확정.
- 2026-11-18 KST, v4 T3P 2회차(리뷰 rv-t3p, 지휘 세션 결정): §11 로그 회전 파일, 바이너리 자리 만들기, `--sms-agent-sha256`, §12-0 대표 질의 구간 문장, 차단 시계 시작점, `--vm-reads` 는 녹화 정지 아래, §12-1 감시 꼴(기존 도메인과 같게)과 trap 수신기가 모든 주소에서 듣는다는 점, §13 NMS 인터페이스별, DB 서버 관제의 줄 없음 규칙, 대상 번호 표, 프로세스 계열 단위, 설계와 다르게 둔 것, 기준값 파일.
- 2026-11-18 KST, v4 T3P(ag-t3p): T3 적용 준비. §11 SMS 에이전트 설치(`sms-agent` 단계, 바이너리 출처와 꺼내기, 자리, 유닛, 멱등, verify 세 항목, 되돌리기), §12 관제 서버 등록(적용 순서와 시각 기준, T2 판정 끝 E 전 금지, trap 확인을 평시 판정 뒤 T3 첫 주입 단계로 옮김과 수신기 등록이 먼저인 까닭, 단계별 API 와 되돌리기, 미리 확인한 제품 동작, 대상 번호 표 꼴), §13 완료 조건 확인 도구와 행별 통과 기준, 등록 전 119 자원 조회 값. §10 되돌리기 줄.
- 2026-11-18 KST, v4 ST2P(ag-st2p): §14 S 적용(ST2) 절을 두고 흩어져 있던 S 문장(§3, §4, §6)을 그리로 모음. T 와 다른 점 표, 언제(T 판정 창과 겹침은 지휘 세션 결정), 차례와 확인과 되돌리기, ST2 점검 행별 명령, 66 자원 읽기 값. `apply.sh --skip-steps sms-agent`(등록 창 전 처음 설치), `scale_verdict.py --inventory`(라우터 없는 S), `pair_packages.py`(쌍 조건 2 패키지 판), 스냅숏 이름 `st2-baseline`.
- 2026-11-18 KST, v4 ST2P 2회차(지휘 세션 결정): §14-2 S 는 T 판정 끝 E 뒤, T3 와 같은 66 변경 창에 이어서. §14-3-1 짝 T 서버의 apt 설치 시각 Ubuntu snapshot 색인(`apply.sh --apt-snapshot`, 설치 단계가 스냅숏 색인을 못 받으면 멈춤), 서버별 ID 표.
- 2026-11-18 KST, v4 ST2P 3회차(리뷰 rv-st2p, 지휘 세션 결정): 색인 시각을 인벤토리 서버별 `apt_snapshot` 으로(§6 T 다시 깔기, §14-3-1, `--apt-snapshot` 인자 없앰), 다른 시각이나 시각 없는 다시 돌리기 멈춤, §14-2 의 2시간 30분 시계를 창 안 S 마지막 변경(가드 시험, S 부하 시작 포함)부터, 차례 5 의 06:00 UTC 전과 apt 기록 확인, 훅 경고 대응, §14-4 언제 칸, `pair_packages.py` 입력에 설치 상태 칸.
