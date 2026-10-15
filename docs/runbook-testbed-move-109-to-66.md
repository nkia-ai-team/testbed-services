---
title: 테스트베드 이전 작업 계획 (109 → 66)
status: Planned (0단계 조사 완료, 1단계 시작 전)
owner: project
last_reviewed: 2026-10-15
tags:
  - runbook
  - testbed
  - migration
  - observability
summary: 테스트베드를 GB10 서버 109(ARM)에서 Proxmox 서버 66(x86)으로 옮기는 단계별 계획. 109 에서 119 로 가던 데이터 종류가 하나도 빠지지 않는 것을 전환 조건으로 삼고, 녹화는 66 검증이 끝날 때까지 109 에서 계속한다.
---

# 테스트베드 이전 작업 계획 (109 → 66)

## 0. 한눈에 보는 결론

| 질문 | 답 (해야 할 일) |
|---|---|
| 왜 옮기나 | 109(GB10)를 팀이 새 LLM 시험에 쓴다. 테스트베드는 66(bon04-13, Proxmox VE 9.2)으로 간다 (2026-10-15 결정) |
| 언제까지 | 기한 없음. 확인할 것을 다 확인하고 넘어간다. 109 는 전환이 끝난 뒤 반납한다 |
| 그대로 옮길 수 있나 | **없다.** 109 는 ARM(aarch64), 66 은 x86(Xeon Silver 4214R × 2). VM 디스크, 앱 이미지 19종, 결함 이미지 25종, 에이전트, 러너 이미지를 x86 으로 새로 만든다 |
| 녹화는 멈추나 | 66 을 짓고 검증하는 동안 109 에서 계속 녹화한다. 멈추는 것은 전환하는 날 한 번뿐이다 |
| 전환 조건 | §2 기준표의 데이터 종류가 66 에서 **모두** 119 에 들어오고, 시험 녹화 덤프가 109 덤프와 같은 구성일 때만 넘어간다. 하나라도 비면 넘어가지 않는다 |
| 부하 강도는 그대로인가 | 메모리 한도로 터지는 시나리오는 거의 그대로다. CPU 포화, 디스크 IO(fio), stress-ng 계열은 x86 과 SATA SSD 에서 다시 재야 한다 (§4 5단계) |
| 접속 정보 | 66 관리자 계정은 컨플루언스 "bon04-13(192.168.200.66) Proxmox VE 가상화 호스트 구축 및 접속 정보" 문서를 본다. 이 문서와 저장소에는 비밀값을 적지 않는다 |

## 1. 두 서버 비교

| 항목 | 109 (지금) | 66 (이전 대상) |
|---|---|---|
| 하드웨어 | NVIDIA GB10, ARM 20코어, 121GB (LLM 과 같이 씀) | Lenovo SR670, Xeon Silver 4214R × 2 (24코어 48스레드), 256GB, 혼자 씀 |
| 디스크 | NVMe 3.6TB | SATA SSD 4개 RAID, VM 용 LVM-thin `vmdata` 6.5TB |
| 가상화 | libvirt, VM 5대 (자동 시작 꺼짐) | Proxmox VE 9.2, VM 없음 |
| 망 | br0(200.109) 브리지 + libvirt NAT 192.168.122.0/24 | `vmbr0` 브리지(200.66), NAT 망은 새로 만들어야 함 |

109 의 VM 사양: tb-cp 2vCPU/4G, tb-w1, tb-w2, tb-w3 각 4vCPU/8G, tb-runner 2vCPU/4G, 디스크 38G, Ubuntu 24.04.

## 2. 빠지면 안 되는 데이터 기준표

2026-10-15 실측(119 ClickHouse, VictoriaMetrics, PostgreSQL 읽기). 66 전환 뒤 같은 질의로 다시 세어 **같은 자릿수**면 통과다. 24h 행 수는 2026-10-14 ~ 10-15 기준이다.

### 2-1. 테스트베드가 만드는 원천 데이터 (필수)

| # | 데이터 종류 | 119 저장 위치 | 지금 경로 | 대상 | 규모 (24h 또는 1h) | 66 에서 확인 |
|---|---|---|---|---|---|---|
| 1 | KCM 쿠버네티스 지표 | VM `kcm.*` 276종 | 노드 4대 kcm-node-agent → 119:17575 | 클러스터 1 (노드 4, 파드 63) | 시리즈 1.1만, 62.8만 샘플/h | [ ] |
| 2 | KCM 이벤트 | CH `kcm_events_local`, `lucida_logs_local`(kubeEvent) | 위와 같음 | 클러스터 1 | 1,062행 | [ ] |
| 3 | KCM 리소스 상태 | PG `kcm_resources`, `kcm_resource_targets`, `kcm_pod_generations`, `kcm_metric_latest` | 위와 같음 | kubernetes_resource 568 | 갱신 수백 행 | [ ] |
| 4 | APM 추적 | CH `otel_traces_local` | 앱 javaagent → 109 팬아웃(:14318) → 119:14317 | 앱 19 (commerce 10, banking 4, food 5) | 약 398만 행 | [ ] |
| 5 | APM 로그 | CH `lucida_logs_local` | 위와 같음 | 앱 16 (3개는 원래 로그 없음) | 약 192만 행 | [ ] |
| 6 | APM 지표 | VM (`db.client.connections.*`, `kafka.*`, `apm.agent.otel.java.*` 등) | 위와 같음 | 앱 19 | 131만 샘플/h | [ ] |
| 7 | DPM PostgreSQL | VM `dpm.postgresql.*` 71종, CH `dpm_session_local`, `dpm_topsql_local` | 119 가 200.136:30432 로 직접 수집 | 인스턴스 1 + 하위 17 | session 1,500, topsql 2.6만 | [ ] |
| 8 | DPM Oracle | VM `dpm.oracle.*` 76종, CH 위 두 표, PG `db_sql_plan` | 119 → 워커 NodePort 30308 | 인스턴스 1 + 하위 11 | session 11.3만, topsql 2.1만 | [ ] |
| 9 | DPM MySQL | VM `dpm.mysql.*` 100종, CH 위 두 표, PG `db_sql_plan` | 119 → 워커 NodePort 30236 | 인스턴스 1 + 하위 10 | session 4,274, topsql 2.2만, plan 6,028 | [ ] |
| 10 | 호스트 SMS 지표 | VM `sms.*` 호스트당 약 130종 | 워커 3대 `lucida-agent-119.service` → 119:18090 | 서버 3 (+ server_resource 각 13) | 호스트당 130종 | [ ] |
| 11 | 프로세스 스냅샷 | CH `process_snapshot`, `process_meta`, `process_rollup_1m` | 위 에이전트 | 서버 3 | 호스트당 2시간 2.5만~3.5만 행 | [ ] |
| 12 | 호스트 연결 | CH `host_connections`, PG `collector_sms_netstat_peers` | 위 에이전트 | 서버 3 | 호스트당 2시간 약 2,070 행 | [ ] |
| 13 | 로그 파일 감시 | VM `sms.log_monitor.*` | 위 에이전트 | 모니터 13 (syslog, auth.log, PG serverlog, Oracle alertlog, MySQL errorlog 포함) | 매칭 건수 | [ ] |
| 14 | syslog | CH `syslog_local` | 6곳 rsyslog → 119:514/udp | 호스트 6 (VM 5 + 호스트) | 71만 행 | [ ] |
| 15 | ICMP 가용성 | VM `sms.server.icmp_status` | 119 가 워커 3대에 핑 | 서버 3 | 354 샘플/h | [ ] |

10~13번은 2026-10-13 00:54Z ~ 10-15 01:00Z 동안 끊겼다가 10-15 에 systemd 유닛으로 되살렸다(장부 §6 2026-10-15). 66 에서는 처음부터 systemd 로 설치한다.

### 2-2. 119 가 원천에서 스스로 만드는 파생물 (생기는지만 확인)

골든시그널(`agg_service_golden_signals`), 경로 서명, 오류 체인, 이상 이벤트(`lucida_events_local`: stream, log, trace anomaly, change-detect), 군집, 인시던트(PG `incidents`), 밴드와 예측. 원천이 들어오면 따라 생긴다. 다만 평소 패턴 학습에 며칠이 걸리므로 시험 녹화 전에 기준선을 쌓는 기간을 둔다(§4 5단계).

### 2-3. 녹화 케이스 덤프 구성 (전환 판정의 최종 대조 대상)

기준 케이스: `case-f49-r-*`(10-12, 호스트 데이터가 다 있는 마지막 케이스)와 66 첫 시험 케이스를 나란히 놓고 비교한다.

| 덤프 안 | 내용 | 대조 방법 |
|---|---|---|
| `data/victoriametrics.export` | 창 전체 VM 시계열 | 지표 계열(kcm, sms, dpm, apm, jvm, kafka)별 시리즈 수 |
| `data/clickhouse/*.parquet` 13개 | otel_traces, lucida_logs, lucida_events, dpm_session, dpm_topsql, kcm_events, process_snapshot, process_meta, host_connections, syslog, trace_error_chains, trace_path_signatures, event_cluster_projection | 파일 13개가 모두 있고 테스트베드 호스트 행이 0 이 아닐 것 |
| `data/postgres.dump`, `data/postgres/*.csv` 31개 | 인시던트, 군집, 대상 이력 등 | 파일 31개, 대상 이력에 새 대상 번호 |
| `topology/`, `data/topology/` | 러너 30초 토폴로지, 단발 토폴로지 | 스냅샷 수가 창 길이에 맞을 것 |
| `models/stream-anomaly/` | 이상 탐지 모델 스냅샷 | 있을 것 |

### 2-4. 기준에서 뺀 것

NMS 스위치 5대, 서버 192.168.200.57(지금 process_snapshot 의 대부분이 이 장비 몫), 119 자기 데이터, 행이 0 인 표 17개(KCM 파드 로그, flow edge, WPM, netflow 등). 테스트베드와 무관하거나 원래 비어 있다.

## 3. 66 목표 구성

| 항목 | 계획 | 이유 |
|---|---|---|
| VM 이름 | tb-cp, tb-w1, tb-w2, tb-w3, tb-runner 그대로 | 매니페스트 nodeSelector, 러너 하드코딩, 실행기가 노드 이름을 쓴다 |
| 도메인 배치 | w1 commerce, w2 banking, w3 food 그대로 | 위와 같음 |
| VM 사양 | 1차는 109 와 같게(cp 2/4G, 워커 4/8G, runner 2/4G) | 강도 재보정 범위를 줄인다. 여유는 재보정 뒤 판단 (결정 D2) |
| 내부 NAT 망 | 192.168.122.0/24 를 66 안에 똑같이 만든다(.77, .184, .11, .14, .206) | 러너, 실행기, 스크립트 47개 파일의 하드코딩을 고치지 않아도 된다 |
| 외부(사내망) IP | 결정 D1 | 200.136~138 은 109 VM 이 쓰는 중이라 병행 기간에는 겹칠 수 없다 |
| flannel podCIDR | cp 10.244.0, w2 .1, w3 .2, w1 .3 그대로 | host_firewall, db_account 계약이 대역을 쓴다 |
| 러너, 팬아웃, 빌드 호스트 | 결정 D3 | 지금은 109 호스트 docker 에 있다. 66 은 Proxmox 호스트라 docker 를 둘 별도 VM 이 필요할 수 있다 |

## 4. 단계

각 단계는 앞 단계의 확인 칸이 다 채워져야 시작한다. 1~5단계 동안 109 녹화는 그대로 돈다.

### 0단계: 조사 (완료 2026-10-15)

- [x] 119 가 받는 데이터 종류 실측 (§2)
- [x] 109 쪽 수집 경로, 덤프 구성, ARM 의존, 노드 주입 의존 실측
- [x] 이미 끊긴 데이터 발견과 복구 (워커 119용 SMS 에이전트, 10-15)
- [ ] 결정 D1~D7 (§5)

### 1단계: 66 기반

- [ ] Proxmox 에 내부 NAT 브리지(192.168.122.0/24, 게이트웨이 .1) 구성
- [ ] x86 Ubuntu 24.04 cloud image 로 VM 5대 생성, 디스크는 `vmdata`
- [ ] VM 자동 시작 켜기 (109 에서는 꺼져 있어 10-13 재부팅 뒤 사람이 켜야 했다)
- [ ] 사용자 nkia, tb_key 공개키, NOPASSWD sudo
- [ ] 워커 도구: containerd, ctr, crictl, fio, stress-ng, python3, iptables(nft), br_netfilter, `net.bridge.bridge-nf-call-iptables=1`, conntrack
- [ ] 워커 정적 경로 `192.168.230.0/24 via 192.168.200.1` (119 DPM 수집에 필요, 109 에서는 `/etc/netplan/99-testbed-230-route.yaml`)
- [ ] 사내망에서 VM 으로 들어오는 경로(109 의 `tb-br0-forward` 타이머에 해당하는 것)

### 2단계: 클러스터와 앱

- [ ] kubeadm v1.31 클러스터, flannel, local-path-provisioner(`/opt/local-path-provisioner`)
- [ ] 빌드 호스트에서 앱 이미지 19종 x86 빌드, 노드에 import (`build-and-deploy.sh`)
- [ ] DB 3종 기동과 시드, 수집 계정(`*/db/monitoring.sql.tmpl`)
- [ ] 저장소에 없는 DB 설정 손으로 반영: PostgreSQL `logging_collector` 등 로그 설정(지금은 PGDATA `postgresql.auto.conf` 에만 있음), Oracle diag 디렉터리 uid 54321
- [ ] OTel javaagent jar 를 워커 3대 `/opt/polestar10/apm/` 에 복사
- [ ] 골든 상태(시나리오 사이클마다 복원하는 DB 기준 상태)를 66 DB 에서 다시 만든다
- [ ] 저장소 매니페스트의 OTel 주소를 실제 쓰는 팬아웃 주소로 맞춘다(지금 저장소는 119:14317 직송, 라이브는 109:14318)

### 3단계: 관측 연결 (§2 기준표 1~15번)

- [ ] 팬아웃 컨테이너 이전: 109 `/opt/lucida/apm-fanout-{118,120,128,137}/config.yaml` 을 옮기고 같은 명령으로 기동 (저장소에 없음, 결정 D3, D4)
- [ ] 앱 19개와 시나리오 매니페스트 16개 파일의 `192.168.200.109:14318` 을 새 주소로
- [ ] KCM: 정식 amd64 이미지로 119 용 helm 릴리스 설치(주소 119:17575)
- [ ] SMS: 워커 3대에 x86 에이전트를 **systemd 유닛으로** 설치, 119 로그 모니터 13개 재등록(PVC 경로가 바뀐다)
- [ ] syslog: `scripts/setup-syslog-forwarding.sh` 로 VM 5대와 새 호스트 재배선
- [ ] 119 DPM 수집 대상 3개의 host:port 를 새 주소로
- [ ] 119 ICMP 대상 3개 새 주소로
- [ ] §2-1 표 15행을 66 기준으로 다시 세어 채운다

### 4단계: 러너, 부하, 주입

- [ ] 러너 이미지 x86 재빌드, `/root/tb-kubeconfig`, tb_key, **새 VM 호스트 키가 든 known_hosts**, 119 root ssh(모델 스냅샷, 골든 복원)
- [ ] tb-runner: k6(amd64), `/opt/loadgen`, 상주 부하 유닛 3개
- [ ] tb-w1 에 `grafana/k6` 이미지 적재 (109 에도 없다. 쓰는 시나리오가 있는지 먼저 확인)
- [ ] 결함 이미지 25종을 `fault-images/build.sh` 로 재빌드 (앱 이미지를 만든 docker 와 같은 데몬)
- [ ] 정상 구간 덤프 cron 이전 (결정 D5)
- [ ] 운영 하네스와 배포 스크립트의 109 주소(`deploy-live-promotions.sh`, `fault-images/build.sh`, testbed-operate 스킬) 교체 준비. 실제 교체는 6단계

### 5단계: 검증과 강도 재보정

- [ ] 66 기준선 쌓기: 상주 부하를 켜고 최소 3일 (이상 탐지 평소 패턴)
- [ ] 실행기 종류별 대표 시나리오 시험 녹화. 66 에서는 러너 큐를 따로 돌려 109 녹화와 섞지 않는다

  | 실행기 | 대표 시나리오 | 재보정 위험 |
  |---|---|---|
  | k8s_image release | F40-H | 낮음 |
  | db_ddl | F35-H | 낮음 |
  | k8s quota, scale | F50-R, F51-R | 낮음 |
  | host_image | F52-R | 낮음 |
  | host_firewall | F44-R | 중간 (대역, 인터페이스) |
  | host_stress fio | F10-H | **높음** (IOPS 사다리가 GB10 NVMe 18.6k 기준) |
  | host_stress cpu, pressure, memhog | F09-R, F15-P, F05-P | **높음** (코어 성능) |
  | 동반 부하로 CPU 포화를 만드는 시나리오 | 결정 D6 에서 고른다 | **높음** |

- [ ] 시험 케이스와 기준 케이스를 §2-3 표로 대조, 빈 칸 없음
- [ ] 새 대상 번호(target_id)를 정답 메타(`registry/scenario-metadata.json`)와 장부에 다시 연결
- [ ] 강도가 깨진 시나리오는 보강 절차(운영 하네스)로 고친다

### 6단계: 전환 (녹화가 멈추는 유일한 구간)

- [ ] 109 에서 돌던 주기가 끝나고 덤프까지 104 로 옮겨진 것을 확인
- [ ] 109 러너 큐를 멈추고 state, runs 를 보존 위치로 복사
- [ ] 109 `/data/eval-cases` 에 104 로 아직 안 옮긴 케이스가 없는지 확인, 정상 구간 덤프 28GB 처리(결정 D5)
- [ ] 운영 하네스, 배포 스크립트, 스킬 문서의 주소를 66 으로 교체
- [ ] 66 러너 큐 시작, 첫 녹화 검증 통과
- [ ] 109 테스트베드 VM 정지 (지우지 않는다)

### 7단계: 109 반납

- [ ] 66 녹화가 일주일 문제없이 돈 뒤 109 의 테스트베드 VM, 팬아웃 컨테이너, 러너 컨테이너 정리
- [ ] 109 의 결함 이미지 보존 목록(`~/dev/testbed-ops/keep-fault-images.md`) 해제
- [ ] 팀에 109 반납 알림

## 5. 결정이 필요한 것

| # | 질문 | 선택지 | 추천 |
|---|---|---|---|
| D1 | 66 VM 의 사내망 IP | (가) 새 IP 로 받고 119 등록을 바꾼다 (나) 전환 날 109 VM 을 끄고 200.136~138 을 넘겨받는다 | **결정 (가)** (2026-10-15). 66 에 새 VM 을 만들고 사내망 IP 를 새로 받는다. 빈 IP 배정은 확인 필요 |
| D2 | VM 사양 | 109 와 같게 / 메모리를 늘린다 | 1차는 같게, 재보정 뒤 판단 |
| D3 | 러너, 팬아웃, 빌드 docker 위치 | Proxmox 호스트에 직접 / 별도 VM | 별도 VM. Proxmox 호스트는 가상화만 맡긴다 |
| D4 | 남의 관측 서버(118, 120, 128, 129, 137)로 가는 복사 | 그대로 옮긴다 / 119 만 | **128 은 계속 받는다** (2026-10-15, 소유자 결정: 팬아웃 128 오버레이, KCM 128 릴리스, SMS 128 에이전트 5대). 118, 120, 129, 137 은 소유자에게 묻고, 답 전까지는 그대로 옮긴다 |
| D5 | 109 에만 있는 정상 구간 덤프 69일치(28GB) | 104 로 옮긴다 / 버린다 | 104 로 옮긴다 |
| D6 | CPU 포화 계열 재보정 대상 목록 | 하네스 보강 이력으로 고른다 | 5단계 전에 정한다 |
| D7 | 결손 케이스 11개 다시 녹화 | 66 에서 / 하지 않음 | 66 전환 뒤 대기열이 비면 검토 |

## 6. 위험

- **정답 대상 번호가 바뀐다.** 새 VM, 새 클러스터는 119 에 새 대상으로 생긴다. 정답 메타를 다시 잇지 않으면 녹화는 되어도 채점이 틀린다.
- **강도 재보정이 길어질 수 있다.** 109 에서도 F47-R 은 강도 조정에 세 번 실패했다. CPU, 디스크 계열은 5단계를 넉넉히 잡는다.
- **기준선 공백.** 새 환경은 평소 패턴이 없다. 쌓기 전에 녹화하면 이상 탐지가 엉뚱하게 동작한다.
- **109 재부팅.** 병행 기간에 109 에 LLM 이 올라가면 메모리 경합으로 109 가 다시 멈출 수 있다(10-13 사례). 그동안 녹화가 멈추고 VM 이 꺼진 채 남는다. 109 VM 자동 시작을 켜 두는 것을 함께 검토한다.

## 7. 진행 기록

- 2026-10-15: 계획 작성. 0단계 조사 완료. 조사 중 워커 3대의 119용 SMS 에이전트가 10-13 재부팅 뒤 꺼져 있던 것을 찾아 systemd 유닛으로 되살렸다(사용자 승인). 그 사이 녹화 11건은 장부 §6 에 결손으로 표시했다.
- 2026-10-15: D1 결정(66 에 새 VM, 새 사내망 IP). D4 중 128 은 66 테스트베드도 계속 받기로 결정.
