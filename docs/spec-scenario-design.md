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

## 9. 열린 결정

- 계열별 Phase 1 후보 목록.
- 양성/음성 paired guardrail을 어느 fault family부터 만들지.
- 시나리오 runner의 파일 레이아웃.
- 시나리오별 반복 실행 횟수와 허용 변동 범위.
- NMS와 WPM을 Phase 1에 포함할 수 있는지 여부.
