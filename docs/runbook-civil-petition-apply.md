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
summary: SMS, NMS 전용 전통 3계층 테스트베드(T)와 같은 코드를 쓰는 S 테스트베드를 66 에 짓는 순서. 66 망 파일, VM 생성, 서버별 설치, 부하 시작, T2 완료 조건 점검, 평시 24시간과 규모 축 판정, SMS 에이전트 설치 자리, 되돌리기.
---

# 전통 3계층 테스트베드(civil-petition) 적용 runbook

설계 정본은 [SMS, NMS 전용 전통 3계층 테스트베드 설계](spec-testbed-vm-3tier.md)(이하 "설계")와 [SMS 에이전트만 쓰는 고객사 테스트베드 설계](spec-testbed-sms-only.md)(이하 "S 문서")다. 코드는 저장소 `civil-petition/` 하나이고, 테스트베드별 값은 `civil-petition/deploy/inventory/t.json`, `s.json` 둘뿐이다. 이 runbook 은 T 를 기준으로 적고 S 에서 다른 곳만 따로 적는다.

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
| `civil-petition/deploy/tools/` | 평시 판정(`stability_check.py`, `host-facts.sh`, `db-facts.sh`), 규모 축 판정(`scale_verdict.py`, `cpu-sample.sh`), 라우터 큐 버림 실기 점검 |

## 3. 비밀값 파일 (적용 단계에서 적용 에이전트가 만든다)

지금은 하나도 없다. 값은 화면에 내지 않는다(아래 명령은 값을 출력하지 않는다). 모든 파일은 `~/.config/testbed/` 아래 0600, 디렉터리는 0700. 인벤토리는 경로만 적는다. 개인 키는 만든 호스트 밖으로 복사하지 않는다.

| 파일(경로) | 권한 | 만드는 명령 | 쓰는 곳 |
|---|---|---|---|
| tb-ops `/root/.ssh/t4-executor`(개인 키) → 적용 호스트 `~/.config/testbed/t4-executor.pub`(공개 키) | 개인 0600, 공개 0600 | tb-ops: `ssh-keygen -q -t ed25519 -N '' -C opsjob -f /root/.ssh/t4-executor`, 공개 키만 `install -m 0600` 으로 복사 | 실행기 계정 `authorized_keys`(러너가 쓰는 키) |
| `~/.config/testbed/t4-operator.pub` | 0600 | 적용 호스트의 운영자 개인 키(예: 104 `~/.ssh/id_ed25519`)의 공개 키를 `install -m 0600 ~/.ssh/id_ed25519.pub ~/.config/testbed/t4-operator.pub` | cloud-init 사용자(운영자 계정), 66 에 복사해 `plan.py --sshkeys` |
| `~/.config/testbed/t4-petition-db` | 0600 | `(umask 077; tr -dc A-Za-z0-9 </dev/urandom \| head -c 24 > ~/.config/testbed/t4-petition-db)` | DB 앱 계정, Tomcat JNDI. 영문, 숫자 16자 이상이어야 설치가 받는다 |
| `~/.config/testbed/t4-snmp-community` | 0600 | `(umask 077; tr -dc A-Za-z0-9 </dev/urandom \| head -c 20 > ~/.config/testbed/t4-snmp-community)` | 라우터 snmpd 와 trap, 119 NMS 등록(T3) |

확인은 값이 아니라 크기와 권한만 본다: `stat -c '%a %s %n' ~/.config/testbed/t4-*`.
S 는 `s5-executor`, `s5-operator.pub`, `s5-petition-db` 를 같은 꼴로 만든다(SNMP 없음).

## 4. 66 망 (잠금, 녹화 정지 창)

전제: T0 의 WireGuard 적용(66 `wg0`, 119 `wg0`)이 이 파일과 따로 있다. `wg0` 인터페이스, `wg0` 의 FORWARD 허용과 거절(체인 TBWG-FWD), MSS 클램프는 T0 의 터널 스크립트가 터널과 함께 걸고 지운다(testbed-services `docs/runbook-testbed-tunnel.md` §3). 이 파일에는 `wg0` 가 나오지 않는다. 대신 터널이 내려가 있을 때 VM 의 관제 주소 행 보고가 사내망 NAT 로 새지 않게 `-d 10.250.0.0/30 -o vmbr0.200 DROP` 을 대역마다 둔다. 66 의 업무망 경로 3개는 T0 가 만들지 않으므로 이 파일이 둔다.

```
python3 civil-petition/deploy/render.py --inventory civil-petition/deploy/inventory/t.json --hypervisor /tmp/t4-hv
# /tmp/t4-hv/etc/network/interfaces.d/t4-net        브리지 5개(vmbr7~11), 업무망 경로 3개, MASQUERADE 4, 러너 SNAT 1,
#                                                   브리지마다 자기 훅(FORWARD 들고 남, 주소 있는 브리지는 INPUT)
# /tmp/t4-hv/etc/network/t4-net.rules               전용 체인 T4-FWD, T4-IN 의 선언과 규칙 전부(iptables-restore 꼴)
# /tmp/t4-hv/etc/chrony/conf.d/t4-net.conf          allow 192.168.127.0/24 (VM 이 66 시간을 따른다)
# /tmp/t4-hv/etc/modules-load.d/t4-net-qdisc.conf   sch_netem, sch_tbf, sch_prio, cls_flower
python3 civil-petition/deploy/render.py --inventory .../t.json --hypervisor-commands up     # ifup vmbr7 vmbr8 vmbr9 vmbr10 vmbr11
python3 civil-petition/deploy/render.py --inventory .../t.json --hypervisor-commands down   # ifdown vmbr11 vmbr10 vmbr9 vmbr8 vmbr7
```

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

브리지는 격리 없이 올라오지 않는다: 규칙 파일이 없거나 `iptables-restore` 나 훅 걸기가 실패하면 post-up 이 그 브리지를 내리고 실패로 끝난다(재부팅 뒤 규칙 파일을 잃은 경우 포함). 그때는 `/etc/network/t4-net.rules` 를 다시 렌더해 놓고 그 브리지를 `ifup` 한다.

66 에서(파일 넷을 같은 경로로 놓은 뒤):

1. 사전 확인: `ip -br link | grep -E 'vmbr(7|8|9|10|11)'` 빈 출력, `iptables-save > /root/t4-net.before`(적용 전 기준, T0 와 같은 창이면 T0 적용 뒤), chrony 의 `confdir /etc/chrony/conf.d` 있음(11-16 리뷰 조회로 있음).
2. `ifup vmbr7 vmbr8 vmbr9 vmbr10 vmbr11`, `systemctl restart chrony`, `modprobe` 넷.
3. 확인(설계 §10 T2 "기존 테스트베드 영향 없음"):
   - `iptables-save > /root/t4-net.after` 뒤 `python3 civil-petition/deploy/tools/hv_iptables_check.py --inventory .../t.json --mode up --before t4-net.before --after t4-net.after` 가 `PASS up`(지워진 줄 0, 늘어난 줄은 전용 체인과 MASQUERADE 4, SNAT 1 뿐, 같은 줄 두 벌 0).
   - `iptables -t nat -S | grep -c 192.168.122.0/24` 가 1.
   - `iptables -S FORWARD` 정책 ACCEPT 그대로, 앞쪽에 T4-FWD 로 가는 줄 10개(브리지 5개의 들고 남).
   - tb-ops 에서 `curl -s -o /dev/null -w '%{http_code}' http://192.168.200.66:8091/`, 사내망 핑, 기존 VM 6대의 바깥 통신.
4. 되돌리기(VM 을 지운 뒤): **파일을 지우기 전에** `ifdown vmbr11 vmbr10 vmbr9 vmbr8 vmbr7`(pre-down 이 훅, NAT, 경로를 지우고 마지막 훅이 빠질 때 체인도 지운다). 그 다음 `rm` 파일 넷, `systemctl restart chrony`. 끝으로 `iptables-save > /root/t4-net.rollback` 와 `hv_iptables_check.py --mode down --before t4-net.before --after t4-net.rollback` 가 `PASS down`.

S: `--inventory .../s.json`, 파일 `s5-net`, `s5-net.rules`(브리지 vmbr12~14, MASQUERADE 2, SNAT 1, 사용자망 → 웹 80 허용), `ifup vmbr12 vmbr13 vmbr14`.

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

각 서버는 `verify-host → os-base → network → accounts → time → syslog → syslog-forward → kernel-modules → firewall → lldpd → (역할) → verify` 를 돈다. `verify` 의 CHECK 줄이 모두 PASS 여야 끝난다(NIC 이름은 `nic-names`, `no-network-rollback-pending`). 한 단계만 다시 돌릴 때는 `--steps "web verify"`. `--dry-run` 은 묶음 목록과 명령만 찍는다.

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

손으로 cloud-init 설정까지 되돌리기: **firewall 단계가 아직 돌지 않은 서버에서만** 한다. `cp -p /var/lib/petition-deploy/netplan-cloud-init/50-cloud-init.yaml /etc/netplan/ && rm -f /etc/netplan/01-host.yaml && netplan apply`(이름이 eth0 ~ 으로 돌아온다). cloud-init 은 꺼진 채 둔다. firewall 뒤라면 같은 세션에서 먼저 `nft delete table inet mgmtguard` 를 한 뒤에 되돌린다(라우터는 그러지 않으면 되돌리는 순간 관리 SSH 가 막힌다). 되돌린 뒤 다시 설치할 때는 `--steps "verify-host network"` 로 이름을 먼저 바꾸고 그다음 단계 없이 전체를 돌린다(firewall 이 보호 표를 다시 놓는다).

S 는 `--inventory deploy/inventory/s.json`, 라우터 없음. 망 단계는 같다(서버마다 NIC 둘, eth0, eth1 → ens18, ens19). S 를 T 뒤에 지을 때는 T 의 판 목록을 그대로 쓴다: T 각 서버의 `/var/lib/petition-deploy/packages.txt` 를 모아 `--package-versions` 로 준다(쌍 조건 2, S 문서 §2-4).

설치 뒤: `plan.py --phase rename` 출력을 66 에 넘긴다.

## 7. T2 완료 조건 점검 (설계 §10 T2)

| 조건 | 명령(어디서) | 기대 |
|---|---|---|
| VM 6대 크기와 주소 | 66 `qm list`, 각 VM `ip -br addr` | §2-2 표 |
| 업무망 경로 | 각 서버 `ip route get 192.168.126.11` 등 | via 라우터 .1. 66 `ip -br addr show vmbr8 vmbr9 vmbr10` 주소 0 |
| 관리망 격리 (1) | web01 `curl -m 3 --interface 192.168.124.11 http://192.168.127.31:22` | 실패 |
| 관리망 격리 (2) | web01, loadgen `ping -I ens18 -c 3 192.168.127.1`, `192.168.127.2` | 실패, 라우터 `nft list chain inet mgmtguard forward` 의 `oifname ... counter drop` 패킷 수 증가 |
| 관리망 격리 (3) | 각 VM `nc -z -w 3 192.168.127.1 22`, `8006` | 실패 |
| 관리망 격리 (4) | 각 VM `ping -I ens19 -c 2 192.168.230.119`, `10.250.0.1` | 실패 |
| 관리망 격리 (5) | 119(읽기) `ping -c 2 192.168.127.21` | 실패 |
| 서비스 기동 | `systemctl is-active apache2 tomcat10 postgresql@16-main`, WAS `sha256sum -c /var/lib/petition-deploy/war.sha256` | active, OK |
| 시드 크기 | db01 `sudo -u postgres psql -Atc 'select count(*) from petition' petition` | 2,000,000 ±5% |
| 시간, 자동 갱신, APM 흔적, 커널 모듈 | 설치의 `verify` CHECK 줄 | PASS |
| rsyslog 첫 규칙 | 실행기 계정으로 한 번 접속, `sudo -n bash -s` 로 U8 가드 arm 하나 → 119(읽기) `syslog_local` 과 VM `/var/log/syslog`, `/var/log/auth.log` 에서 그 계정, uid, `run-r` 줄 0, `/var/log/opsjobs.log` 에 있음. 운영자 계정 sudo 한 줄은 119 에 있음 | 0, 있음, 있음 |
| 큐 버림 보고 | 라우터 `sudo civil-petition/deploy/tools/router-discards-selftest.sh ens19 192.168.124.11`(부하 켜기 전) | PASS 4줄 |
| 라우터 관제 흐름 notrack | 라우터 `sysctl -w net.netfilter.nf_conntrack_max=<지금 수 + 50>`, loadgen 에서 웹 80 으로 새 연결 수백 개, 라우터 `dmesg` 에 `table full` → 119(읽기) 라우터 상향 주소 SNMP 응답, `syslog_local` 에 그 줄. 끝나면 원래 값 | 둘 다 닿음 |
| 가드 단독 시험 | U8 시운전 절차 2번(`v4/agent-out/U8.md`)을 라우터와 web01 에서 | 통과 |
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
python3 deploy/tools/scale_verdict.py --summary k6-2x.json --cpu router=router.jsonl --cpu was01=was01.jsonl \
    --cpu was02=was02.jsonl --cpu web01=web01.jsonl --cpu db01=db01.jsonl --cpu hypervisor=hv.jsonl
sudo systemctl start loadgen.service
```

기준: 비정상 1% 이하, 라우터 CPU 50% 이하와 softirq 20% 이하, WAS CPU 70% 이하, 66 CPU 70% 이하, VM steal 5% 이하, 달성 요청률이 60 의 95% 이상. CPU 는 10초 표본의 95 백분위. 판정 이름 `PASS_2X_60RPS_30M`, `FAIL_2X_60RPS_30M`. 라우터가 넘으면 vCPU 를 4 로 올리고 다시 잰다(설계 §3-6).

**디스크.** db01 `df -B1 /pgdata` 를 부하 시작과 24시간 뒤에 재서 하루 증가를 구하고, 90일 뒤 사용률이 70% 이하인지 적는다. 주 표는 보관 배치의 건수 상한(민원 200만, 설계 §2-3)으로 크기가 묶이지만, 옮겨 간 행이 쌓이는 보관 표(`petition_archive`, `petition_history_archive`)는 하루 약 12만 민원과 그 처리 이력만큼 계속 자란다. 하루 증가는 대부분 이 보관 표 몫이다.

**보관 배치.** was02 의 `petition-archive.timer`(02:20 KST)가 앱의 보관 배치를 부른다. 설정 기본값은 WAR 의 `application.properties`(`petition.archive.keep-rows=2000000`, `chunk-rows=1000`, `pause-ms=1000`, `window=02:00-05:00`, `zone=Asia/Seoul`)이고 Tomcat 환경변수(`PETITION_ARCHIVE_KEEP_ROWS` 등)로 덮는다. 결과는 `batch_run` 표와 `/var/log/petition-batch.log` 줄의 `result=` 에 앱이 답한 값 그대로 남는다: `ok`(상한까지 옮김, 넘은 것이 없으면 0 건), `skipped`(창 밖 호출, 행 수도 세지 않음), `partial`(옮기는 중 창이 닫힘, 남은 몫은 다음 밤), `busy`(다른 보관 배치가 PostgreSQL advisory lock 을 쥐고 있어 아무것도 안 함). 앱이 답하지 못하면 `result=fail` 이고 유닛이 실패한다. 평시 24시간 판정은 `ok` 만 센다.

**보관 SQL 확인(수동, docker 필요).** Java 시험은 문장을 실행하지 않으므로 보관 문장의 뜻과 계획을 바꿨다면 저장소 뿌리에서 `flock ~/dev/testbed-ops/v4/locks/heavy systemd-run --user --scope -q -p MemoryMax=4G -- civil-petition/deploy/tools/archive-sql-check.sh [규모]` 를 돌린다(기본 200만, 시드 약 5분, 문법만 볼 때 20000). 일회 `postgres:16` 컨테이너에서 시드의 정상 상태 꼴(행 수, 열린 건 0.15%, 처리 중 비율, 미래 시각 없음, 이력 비, 규모 50만 이상이면 접수 시각 폭과 KST 시간 비), 행 수 추정치 `n_live_tup` 오차, 가장 오래된 순서, 처리 이력 이동, 답변과 첨부 cascade, 처리 이력 색인을 타는 계획, 묶음과 `count(*)` 시간, advisory lock 을 보고 `CHECK ...|t` 줄이 모두 t 이고 끝 줄이 `checks=16 failed=0`(규모 50만 미만은 14)이면 통과다. 끝에 컨테이너를 지운다.

끝에 `plan.py --phase snapshot` 으로 재해 복구용 스냅숏 하나(설계 §8, 녹화마다 되돌리지 않는다).

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
- 되돌릴 수 없는 단계는 없다. 119 등록 해제는 T3 의 몫이고 과거 데이터를 지우는지 먼저 확인한다(설계 §9).

## 11. SMS 에이전트 설치 자리 (T3 몫, T2 에서 하지 않는다)

| 서버 | 설치 | 인자 | 로그 감시 파일(일반 패턴, 시나리오별로 바꾸지 않음) |
|---|---|---|---|
| web01 | 기존 작업 노드와 같은 `lucida-agent` amd64(119 collector-sms 이미지 안 바이너리), systemd 유닛 | 보고 받는 주소 `10.250.0.1:18090`, `LUCIDA_HOST=192.168.124.11`(업무 주소) | `/var/log/apache2/access.log`, `error.log`, `/var/log/syslog`, `/var/log/auth.log` |
| was01, was02 | 같음 | `LUCIDA_HOST=192.168.125.11`, `.12` | `/var/log/tomcat10/catalina.out`, `petition.log`, `localhost.*.log`(앱이 처리하지 못한 예외는 Tomcat JULI 기본으로 여기에만 간다), `localhost_access_log.*.txt`, `/var/log/syslog`, `/var/log/auth.log` |
| db01 | 같음 | `LUCIDA_HOST=192.168.126.11` | `/var/log/postgresql/postgresql-16-main.log`, `/var/log/syslog`, `/var/log/auth.log` |
| rt-core01, loadgen | 설치하지 않음 | | |

수집 정책은 기본값 그대로 둔다. 서버 ICMP 상태와 RTT(`sms.server.icmp_status`, `sms.server.rtt`)는 SMS 등록에 딸려 오는 기본값대로 켜 두고, 끄는 장비 개별 덮어쓰기를 만들지 않는다(설계 §4-1, 11-17 검토 세션 답). `/var/log/opsjobs.log` 는 감시하지 않는다. 에이전트의 유닛 이름, 실행 사용자, 프로세스 이름에 테스트베드 낱말을 쓰지 않는다. S 는 websvr01 등 같은 자리, `LUCIDA_HOST` 는 S 업무 주소.

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
