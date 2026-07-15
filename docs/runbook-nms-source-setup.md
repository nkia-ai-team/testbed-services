---
title: NMS 수집 소스 셋업 (.57)
status: Planned — 미실행, 서버 담당자 승인 대기
owner: project
last_reviewed: 2026-07-15
tags:
  - runbook
  - nms
  - testbed
  - scenario
summary: 외부 서버 192.168.200.57에 SNMP/NetFlow 수집 소스(snmpd·trap 송신·softflowd)를 셋업해 119의 NMS 수신 경로를 활성화하는 절차. G19·G20 시나리오의 전제 작업.
---

# NMS 수집 소스 셋업 (.57)

⚠ **미실행 상태의 계획 문서다.** .57은 공용 개발 서버(`dev-svr-200-57`)로
보이므로, **서버 담당자 승인 후** 실행한다. 실행 후 이 문서를 실측값으로
갱신하고 status를 Active로 바꾼다.

## 1. 배경 — 왜 하나

119(lucida-next AP)의 NMS 수신 경로는 준비돼 있으나 보내는 소스가 없어
`snmp_traps_local`·`netflow_records_local` 테이블이 **0건**이다
([시나리오 설계 §9](spec-scenario-design.md) 실측). .57에 소스를 만들면
NMS 계열 시나리오(G19 인터페이스 다운, G20 flow 급증)의 관측 기반이 열린다.

| 받는 쪽 (119, 이미 준비됨) | 프로토콜/포트 | 저장 테이블 |
| --- | --- | --- |
| `collector-nms` | SNMP 폴링 (수집기가 .57에 질의) | 메트릭 계열 |
| `collector-nms-trap` (host net) | UDP **162** 수신 | `snmp_traps_local` (TTL 90일) |
| `collector-tms` | NetFlow/sFlow/IPFIX UDP **2055** (기본값 — 정본은 등록 시 `polestarTmsPlugin.flowPort`) | `netflow_records_local` (TTL 30일) |

## 2. 대상 서버

| 항목 | 값 |
| --- | --- |
| 주소 | 192.168.200.57 |
| 접속 | `ssh root@192.168.200.57` / `Cloud!!25` (2026-07-15 확보·접속 실증) |
| OS | Rocky Linux 9.7, x86_64, hostname `dev-svr-200-57` |
| 승인 | **서버 담당자 확인 필요** — 설치 패키지: net-snmp, softflowd(EPEL). 둘 다 경량 데몬 |

## 3. 절차

### ① snmpd — SNMP 폴링 응답자

```bash
dnf install -y net-snmp net-snmp-utils
# /etc/snmp/snmpd.conf 최소 설정: 119에서의 조회 허용
#   rocommunity public 192.168.230.119
systemctl enable --now snmpd
# 자기 확인: snmpwalk -v2c -c public localhost system
```

이후 **lucida 쪽 등록**: .57을 NMS 폴링 대상으로 등록한다(UI/API — 절차
미확정, 실행 시 확인해 여기 기록). 등록돼야 collector-nms가 폴링을 시작한다.

### ② trap 송신 경로

```bash
# 시험 trap 1발 (linkDown OID) → 119 UDP 162
snmptrap -v2c -c public 192.168.230.119:162 '' 1.3.6.1.6.3.1.1.5.3
```

검증(§4)에서 수신 확인. G19에서는 실제 인터페이스 이벤트와 연동한 trap
송신으로 확장한다(합성 trap 단독은 가짜 상관 —
[설계 §9.3(b) G19](spec-scenario-design.md) 검토 지적 참조).

### ③ softflowd — NetFlow exporter

```bash
dnf install -y epel-release && dnf install -y softflowd
# 관측할 인터페이스에서 flow를 119:2055로 송신 (인터페이스명은 실행 시 확인)
softflowd -i <iface> -n 192.168.230.119:2055 -v 9
# 상주화는 systemd unit으로 (실행 시 작성해 여기 기록)
```

flow 수신 포트 정본은 TMS 등록값이므로, lucida 쪽 TMS collector 등록
상태·포트를 먼저 확인한다.

## 4. 검증 — 각 단계 후 119 ClickHouse 확인

```bash
CH=http://lucida:lucida123@192.168.230.119:18123
# trap: 0 → 양수 확인
curl -s "$CH" --data "SELECT count(), max(received_at) FROM snmp_traps_local WHERE source_ip='192.168.200.57'"
# flow: 0 → 양수 확인
curl -s "$CH" --data "SELECT count(), max(received_at) FROM netflow_records_local WHERE exporter_ip='192.168.200.57'"
```

## 5. 한계 — 이 셋업이 해주지 않는 것

이 셋업은 **신호 배관**까지다. G19·G20이 양성 시나리오가 되려면 별도로
**실영향 설계**가 필요하다([설계 §9.1 실영향 요건·§9.3(b)](spec-scenario-design.md)):
trap/flow가 가리키는 경로로 **실제 서비스 트래픽이 지나가고 그것이 실제로
끊기거나 느려져야** 한다. tb-runner는 109 호스트 내부 VM이라 물리 NIC를 타지
않으므로, 네트워크 경로 부하는 .57발이어야 한다([부하 규칙 R2](spec-scenario-load.md)).
