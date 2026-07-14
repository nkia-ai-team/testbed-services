---
title: 시나리오 설계
status: Draft
owner: project
last_reviewed: 2026-07-08
tags:
  - scenario
  - testbed
  - rca
  - evaluation
summary: RCA agent 평가를 위한 양성·음성 시나리오의 카탈로그 구조, 설계 템플릿, 실행 시간 구조, golden 승격 기준을 정의한다.
---

# 시나리오 설계

이 문서는 [테스트베드 환경 설계](spec-testbed-design.md)가 제공하는 환경 위에서
어떤 RCA 평가 시나리오를 만들지 정의한다. 평가 지표와 채점식은 [평가·실험
설계](spec-evaluation.md)가 담당하고, `service-spec.yaml`을 golden 레코드로
정밀화하는 규칙은 [시나리오 작성 규칙](spec-scenario-authoring.md)이 담당한다.

## 1. 설계 목표

시나리오는 agent가 다음 능력을 보이는지 평가해야 한다.

- 원인 후보를 고수준 리소스가 아니라 가능한 실행 단위까지 좁힌다.
- 지지 근거와 반증 근거를 분리한다.
- 증상 계열과 원인 계열이 다른 경우에도 인과사슬을 추적한다.
- 근거가 부족하면 단정하지 않는다.
- 유사 사례가 도움이 되는 조건과 해가 되는 조건을 구분한다.

## 2. 카탈로그 구조

평가 v1은 6개 cause-domain을 기준으로 설계한다.

| 계열 | 목표 | 대표 패턴 |
| --- | --- | --- |
| APM | 애플리케이션 코드·endpoint·의존 호출 원인 식별 | endpoint 지연, 예외 폭증, 내부 fan-out 지연 |
| DPM | DB 세션·SQL·커넥션·lock 원인 식별 | 느린 SQL, row lock, connection pool 고갈 |
| SMS | 호스트·프로세스·파일시스템 원인 식별 | CPU noisy neighbor, process memory leak, disk pressure |
| NMS | 네트워크 인터페이스·패킷 손실 원인 식별 | uplink saturation, packet loss, interface error |
| KCM | 컨테이너·pod·node lifecycle 원인 식별 | pod restart, OOM, scheduling failure, node pressure |
| WPM | 웹 probe·사용자 경로·phase 원인 식별 | URL phase 지연, synthetic failure, browser-side timeout |

Phase 1 목표는 약 30개다.

| 구성 | 수량 목표 |
| --- | ---: |
| 양성 시나리오 | 계열당 4개 내외 |
| 음성 시나리오 | 6개 내외 |
| cross-domain 시나리오 | 계열당 최소 1개 |

Phase 2 목표는 약 60개다. Phase 2에서는 계열당 양성 8개 내외와 음성 10개
내외를 확보한다.

## 3. 시간 구조

시나리오 하나는 다음 구간을 기본으로 한다.

| 구간 | 목적 |
| --- | --- |
| `T-15m ~ T-5m` | 정상 baseline 수집 |
| `T-5m ~ T` | 장애 주입 준비, 부하 ramp-up |
| `T ~ T+10m` | 증상 관측과 incident seed 생성 대상 구간 |
| `T+10m ~ T+15m` | 장애 해제, 회복 관측 |
| `T+15m 이후` | cleanup과 검증 |

시간 길이는 환경에 맞게 조정할 수 있지만 baseline, injection, symptom, recovery는
항상 분리한다.

## 4. 양성 시나리오

양성 시나리오는 명확한 단일 원인이 있고, 원인 후보를 구체 실행 단위까지 추적할
수 있어야 한다.

```yaml
id: <domain>:<number>
title: <운영자가 이해할 수 있는 장애 이름>
cause_domain: APM|DPM|SMS|NMS|KCM|WPM
root_cause:
  target_kind: <service|database|host|network_device|pod|url>
  target_id_source: <배포/등록/수집 결과에서 얻는 정본 식별자>
  dimension:
    label: <필요 시 실제 수집 차원>
    value: <필요 시 실제 값>
  mechanism: <원인이 증상으로 전파되는 방식>
expected_depth: entity|dimension
injection:
  script: <상대 경로>
  parameters: {}
signals:
  must_support:
    - <원인을 지지하는 관측>
  must_rule_out:
    - <대안을 배제하는 정상 관측>
  expected_missing: []
propagation:
  - <원인 단계>
  - <중간 전파>
  - <사용자/서비스 증상>
difficulty: 1
```

`target_id_source`는 실제 식별자 값을 직접 쓰기 전 단계에서만 허용한다. golden
set에 승격할 때는 반드시 정본 `target_id`로 교체한다.

## 5. 음성 시나리오

음성 시나리오는 agent의 과확신을 막기 위한 guardrail이다.

```yaml
id: <domain>:negative-<number>
title: <단일 원인 확정이 부당한 상황>
cause_domain: null
has_single_cause: false
expected_status: insufficient
pattern: noisy_logs_only|multiple_concurrent_anomalies|missing_root_signal|symptom_only
signals:
  observed:
    - <관측된 증상>
  absent_or_normal:
    - <원인 확정에 필요한데 없거나 정상인 신호>
accept_if: >
  단일 원인을 확정하지 않고 가능한 후보와 누락 근거를 분리하면 정답.
reject_if: >
  약한 증상이나 로그 키워드만으로 confirmed 원인을 만들면 오답.
```

음성 시나리오는 데이터 수집 실패를 포장하는 용도가 아니다. 수집은 정상이나
단일 원인 확정이 부당한 상황이어야 한다.

## 6. 근거 설계

각 시나리오는 세 종류의 근거를 의도적으로 설계한다.

| 근거 | 의미 | 예 |
| --- | --- | --- |
| `must_support` | 정답 원인을 직접 지지하는 양성 신호 | lock wait 상승, 특정 pod restart, 특정 endpoint p95 상승 |
| `must_rule_out` | 그럴듯한 대안을 배제하는 정상/반증 신호 | DB 정상, 호스트 CPU 정상, 인접 서비스 error 없음 |
| `expected_missing` | 결론을 강화하지만 수집되지 않은 신호 | 외부 의존 세부 로그 없음, 세션 식별자 없음 |

모든 양성 시나리오는 `must_support`와 `must_rule_out`을 최소 하나씩 가져야 한다.
반증 근거가 없으면 agent가 증상 계열 오답을 배제했는지 평가할 수 없다.

## 7. 승격 게이트

시나리오 후보가 golden set에 들어가려면 다음을 통과해야 한다.

- [ ] 동일 환경에서 3회 이상 실행해 원인과 주요 증상이 재현된다.
- [ ] cleanup 후 다음 시나리오에 상태 누수가 없다.
- [ ] `target_id`와 필요한 dimension 값이 정본 식별자로 확정됐다.
- [ ] `must_support`가 실제 관측 데이터에 존재한다.
- [ ] `must_rule_out`이 실제 관측 데이터에 존재한다.
- [ ] 음성 시나리오는 수집 실패가 아니라 의도된 불충분성임이 확인됐다.
- [ ] incident seed만으로 agent가 정답 메타데이터를 직접 볼 수 없다.
- [ ] golden 레코드가 `spec-scenario-authoring.md` 체크리스트를 통과한다.

## 8. 우선 작성 순서

초기 설계는 다음 순서로 진행한다.

1. 관측 계약을 먼저 만족하는 최소 도메인 2개를 만든다.
2. DPM, APM, KCM 양성 시나리오를 각각 2개씩 만든다.
3. 각 양성 계열마다 대응 음성 시나리오를 1개씩 만든다.
4. SMS와 NMS를 추가해 cross-domain 전파를 만든다.
5. WPM은 probe/URL 관측 계약이 확인된 뒤 Phase 1 후반에 넣는다.
6. Phase 1의 재현성과 채점 가능성을 확인한 뒤 Phase 2로 확장한다.

이 순서는 구현 난이도가 아니라 평가 리스크 기준이다.

## 9. 원인 유형 백로그 (2026-07-14)

RCA가 읽는 데이터 소스 14종(`../lucida-next` operator/rca 코드 조사)과 현재
카탈로그(기존 12 + 부하 후보 9, [부하 시나리오 규칙](spec-scenario-load.md)
§4)를 대조해 도출한 갭. RCA가 볼 수 있는데 어떤 시나리오도 발현시키지 않는
영역이 백로그다. 목표 규모는 양성 30~50 + 음성 10~15.

| 갭 영역 (계열) | 놀고 있는 RCA 데이터 | 후보 시나리오 예 |
| --- | --- | --- |
| 디스크/스토리지 (SMS) | host kin filesystem 리소스 | 디스크 풀 → DB 쓰기 실패, WAL/로그 파티션 고갈 |
| K8s 세부 (KCM) | pod/node 메트릭, lifecycle 이벤트 | OOMKill, pod eviction, liveness probe 재시작 루프, 이미지 pull 실패 |
| DB 세부 (DPM) | `dpm_ch_query_event`(데드락/예외), TopSQL | 데드락, 인덱스 없는 쿼리(플랜 변화), 앱 버그성 커넥션 누수 |
| 이벤트 백본 (Kafka) | (관측 gap 확인 선행 — load spec §7) | consumer 정지 → lag 폭증, 브로커 다운 |
| 앱 내부 (APM) | 프로세스 메트릭, 트레이스 | 스레드 풀 고갈, JVM GC 압박, 앱 데드락 |
| 외부 의존 세부 (APM) | 소켓 연결(HostTopPeers) | rate limit, 인증서 만료, DNS 실패 |
| 나쁜 배포 (change) | 변경 이력 | 버그 릴리스가 진짜 원인인 양성(기존 Deployment Change는 상관 검증 중심) |
| **NMS (신규 개방)** | `snmp_traps_local`, `netflow_records_local` — **현재 0건(미수집)** | 스위치/서버 인터페이스 다운 → 통신 단절(증상 APM·원인 NMS 계열 교차), 트래픽 이상 flow |

NMS 계열 전제 작업(2026-07-14 확인): 수신 경로는 이미 존재한다 —
`collector-nms-trap`(UDP 162, host net), `collector-tms`(flow), dev/test용
트랩 시뮬레이터 포함. 수집 소스가 없어 0건이며, 외부 서버
**192.168.200.57**에 snmpd(폴링 대상 등록) + trap 송신 + NetFlow
exporter(softflowd 등)를 올려 소스를 만든다. SSH 자격 확보 대기.

## 10. 열린 결정

- 계열별 Phase 1 후보 목록.
- 양성/음성 paired guardrail을 어느 fault family부터 만들지.
- 시나리오 runner의 파일 레이아웃.
- 시나리오별 반복 실행 횟수와 허용 변동 범위.
- NMS와 WPM을 Phase 1에 포함할 수 있는지 여부.
