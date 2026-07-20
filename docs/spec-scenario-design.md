---
title: 시나리오 설계
status: Draft
owner: project
last_reviewed: 2026-07-20
tags:
  - scenario
  - testbed
  - rca
  - evaluation
summary: 이상감지·이벤트 클러스터·인시던트 격상·RCA 평가를 위한 시나리오 카탈로그, 설계 템플릿, 실행 시간 구조, golden 승격 기준, 시나리오당 파일 레이아웃을 정의한다.
---

# 시나리오 설계

이 문서는 [테스트베드 환경 설계](spec-testbed-design.md)가 제공하는 환경 위에서
어떤 AIOps 파이프라인 평가 시나리오를 만들지 정의한다. 적용 범위는
**이상감지 → 이벤트 클러스터 → 인시던트 격상 → RCA**이며 챗봇은 제외한다.
평가 지표와 채점식은 [평가·실험
설계](spec-evaluation.md)가 담당하고, `service-spec.yaml`을 golden 레코드로
정밀화하는 규칙은 [시나리오 작성 규칙](spec-scenario-authoring.md)이 담당한다.
실제 전체 후보 목록은 [AIOps 시나리오 카탈로그](spec-scenario-catalog.md)에서
관리한다.

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

위 표는 시나리오 실행 타임라인이고 **캡처 시간창과는 별도**다. 실제 주입 시작을
`t1`, 종료를 `t2`로 기록하고, 평가 케이스는 시나리오 창 `[t1-10m, t2+20m]` +
그날의 일일 정상 세그먼트(00:00~02:00) 참조로 조립한다(2026-07-20 개정).
`t2+20m`는 클러스터·인시던트까지 완료시키는 고정 `pipeline_settle_window`이며,
상세 계약은 [평가용 데이터 캡처 §2.1](spec-eval-data-capture.md)을 따른다.

동시 또는 근접 시간에 서로 다른 장애를 발생시키는 합성 시나리오는 일반적인
시나리오 간격의 명시적 예외다. 각 하위 장애의 시간창을 별도로 기록하고, 두 장애가
겹친다는 이유만으로 같은 사건이라고 가정하지 않는다. 기대 결과는 다음 셋 중 하나를
사전에 고정한다.

- **split:** 원인과 topology가 독립이면 클러스터·인시던트·RCA를 각각 분리한다.
- **merge:** 서로 다른 증상이어도 하나의 공통 root가 입증되면 한 사건으로 묶는다.
- **multi-root:** 하나의 사용자 사건에 독립 root가 실제로 둘이면 단일 원인으로
  축약하지 않고 복수 원인 또는 불충분 상태를 기대한다.

실행·cleanup·캡처 규칙은 [부하 규칙 R6-1](spec-scenario-load.md)을 따른다.

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
exporter(softflowd 등)를 올려 소스를 만든다. ~~SSH 자격 확보 대기~~ →
**자격 확보(2026-07-15)**: `ssh root@192.168.200.57` / 비번 `Cloud!!25` —
접속 실증 완료(hostname `dev-svr-200-57`, Rocky Linux 9.7, x86_64).
네트워크 경로 부하 주입 원점 역할 겸용([부하 규칙 R2](spec-scenario-load.md)).

### 9.1 백로그 전개 — 신규 후보 목록 (G1~G20)

갭 8영역을 구체 시나리오로 전개한 것. 부하 후보(L1~L7,
[부하 시나리오 규칙 §4.3](spec-scenario-load.md))와 같은 지위의 설계
후보이며, 각각 승격 전에 작성 규칙(§4~§7)과 검증을 통과해야 한다. 기대
탐지는 lucida-next 탐지기 reason 기준(§9 조사와 동일 출처). 기존 21 + L군
9 + G군 20 ≈ 50으로 목표 규모에 도달한다.

**선정 원칙 — RCA에 국한하지 않는다(2026-07-15, 2026-07-15 범위 정정).**
이 백로그는 RCA 데이터 소스 갭에서 도출됐지만, Phase 1 후보 선정은
**AIOps 파이프라인 전체**(이상감지 → 이벤트 클러스터 → 인시던트 격상 → RCA,
[평가용 데이터 캡처 §6](spec-eval-data-capture.md) 모듈 표)에 대한 가치로
평가한다. 챗봇은 적용 범위에서 제외한다. 좋은 시나리오는 이 사슬을 끝까지
통과시킨다 — 이상 이벤트가 여럿 발생하고, 한 사건으로 묶이고, 인시던트로
격상되고, 원인이 추적된다. 추가 기준은 **실전 발생 빈도**(운영 환경에서 흔한
장애 우선)다. 후보별 "어느 모듈까지 발현시키는가"를 선정 시 명시한다.

**실영향 요건(2026-07-15).** 인시던트란 실제 서비스에 영향이 가는 문제
현상이다(체감 지연, 실패, 오동작). 따라서 양성 시나리오는 **실제 서비스
영향을 발생시켜야 한다** — 관측 신호만 출렁이고 서비스가 멀쩡한 주입은
시나리오로 인정하지 않는다. 예: G20이 iperf로 flow만 만들고 commerce에
영향이 없으면 탈락이며, 살리려면 서비스 경로 대역을 실제로 포화시켜 지연을
유발하도록 재설계해야 한다(G19는 iptables 실차단을 병행하므로 통과). 의도적
무영향은 음성 시나리오(§5)의 몫으로, 양성과 명확히 구분한다.

**유사 장애 기능을 고려한 시나리오 관계(2026-07-15).** 시나리오를 서로
독립된 단건으로만 만들지 않고, 일부는 의도적인 **사례군(family)** 으로
설계한다. 한 사례군에는 다음 관계를 포함할 수 있다.

1. **재발형** — 서비스·대상·발생 시점은 다르지만 원인 메커니즘과 전파 구조가
   같은 사건. 과거 장애가 현재 장애를 이해하는 데 실제로 참고가 된다.
2. **유사 증상·다른 원인형** — 제목이나 사용자 증상은 비슷하지만 원인이 다른
   사건. 예를 들어 동일한 checkout timeout이라도 하나는 DB lock, 다른 하나는
   외부 결제사 429로 만든다.
3. **부분 유사형** — 중간 전파 단계는 같지만 최초 원인이나 최종 영향 범위가
   다른 사건. 어떤 부분을 재사용하고 어디서 새 증거를 봐야 하는지가 드러난다.

사례군은 단순 복제본이 되면 안 된다. 재발형도 대상·노이즈·영향 범위 중 하나
이상을 달리하고, 다른 원인형은 둘을 구분할 수 있는 결정적 관측 근거를 각각
남겨야 한다. 과거 사례로 쓰일 사건이 먼저 실행·종결되고 이후 사건이 발생하는
시간 순서도 설계에 포함한다. 구체적인 채점 지표와 실행 비교 방식은 평가 설계
단계에서 별도로 정한다.

**주입 위치([부하 규칙 R2](spec-scenario-load.md) 승계).** 부하가 개입하는
후보(G3·G12·G13 등 surge 조합 포함)는 주입 위치를 R2 표(사용자 트래픽
surge=tb-runner→tb-cp NodePort / 내부 폭주=클러스터 내부 Job / DB 직접
부하=tb-runner→DB NodePort / 네트워크 경로 부하=**외부 서버 .57**→물리 NIC
경유, G19·G20 필수)에 따라 설계 시 명시한다. 부하 외 주입(파일 채움,
설정 패치, DROP 등)도 주입 실행 위치(어느 노드/pod에서)를 yaml
`execution.injection_points[]`에 기록한다. 중앙 script의 시작 위치는
`execution.orchestrator`로 분리한다. 유형과 위치가 어긋나면 topology가
비현실적이 되므로 runner placement policy가 실행 전에 거부한다. 64개 후보의
판정은 [실행·주입 위치 결정표](spec-scenario-execution-matrix.md)가 정본이다.

| id | 영역 | 시나리오 | 원인 주입 | 기대 탐지 | RCA 근거 | 비고 |
| --- | --- | --- | --- | --- | --- | --- |
| G1 | 디스크 | DB 데이터 파티션 디스크 풀 → 쓰기 실패 | worker에서 대용량 파일로 파티션 채움 | filesystem 메트릭 level shift + novel_template("No space left" ERROR) | host-kin filesystem + DB 에러 로그 | 실전 최고빈도 사고 |
| G2 | 디스크 | 디스크 IO 포화 → DB latency 저하 | 특정 worker에 IO stress | IO 메트릭 shift + latency 분포 이동 | host-kin + needle(한 노드만) | |
| G3 | K8s | 메모리 limit 축소 + 부하 → OOMKill 재시작 루프 | limit 패치 배포 | pod restart 메트릭 + KCM lifecycle 이벤트 | KCM pod 메트릭 + 변경 이력 선행 | 증폭기로 surge 조합 가능 |
| G4 | K8s | liveness probe 오설정 → 재시작 루프 | probe timeout 과소 패치 | pod restart + 가용성 저하 | 변경 이력 + KCM | 원인이 "변경"인 계열 교차 |
| G5 | K8s | 노드 메모리 압박 → pod eviction 연쇄 | 노드에 메모리 점유 pod 배치 | eviction 이벤트 + 재스케줄 | KCM node/pod + needle | |
| G6 | K8s | 이미지 pull 실패 → 롤아웃 중 가용성 저하 | 존재하지 않는 태그로 재배포 | ImagePullBackOff 이벤트 + 가용 replica 감소 | KCM lifecycle + 변경 이력 | |
| G7 | DB | 데드락 — 역순 락 트랜잭션 충돌 | 두 배치가 역순으로 행 갱신하는 스크립트 | dpm_ch_query_event(데드락) + ERROR 로그 | DPM query event + 세션 | Oracle·PG 각각 |
| G8 | DB | 인덱스 드랍 → full scan 플랜 변화 | 핵심 조회 인덱스 DROP | TopSQL 급등 + latency 분포 이동 | dpm_topsql(플랜/시간 변화) + 변경 없음(함정: 변경 이력에 안 남는 원인) | 난이도 상 |
| G9 | DB | 앱 버그성 커넥션 누수 → 점진 고갈 | 커넥션 미반환 코드 경로 토글 | 세션 수 단조 증가 → 고갈 시 ERROR | dpm_session 추세 + 로그 | 풀 고갈(L2)과 원인 구분 — paired 후보 |
| G10 | Kafka | consumer 정지 → lag 폭증 → 알림·배송 지연 | consumer deployment scale=0 | lag 메트릭(관측 gap 확인 선행) + 지연 전파 | Kafka 메트릭 + 토폴로지 | L6과 관측 gap 공유 |
| G11 | Kafka | 브로커 다운 → produce 실패 → outbox 적체 | kafka pod kill | produce 에러 로그 + outbox 증가 | KCM pod + 로그 + DB 테이블 추세 | |
| G12 | 앱 | 스레드 풀 고갈 — slow endpoint + 부하 | 지연 주입 + surge 조합 | tomcat thread 포화 메트릭 + 전 엔드포인트 대기 | 프로세스/APM 메트릭 + trace | DB pool(L2)과 구분 |
| G13 | 앱 | JVM GC 압박 — heap 축소 + 부하 | heap 옵션 축소 배포 + surge | GC pause 패턴의 주기적 latency 스파이크 | 프로세스 메트릭 + 변경 이력 | |
| G14 | 외부 의존 | 외부 PG rate limit(429) → 결제 실패 급증 | mockserver expectation을 429로 교체 | 에러 rate_spike + 결제 실패 로그 | trace error_chain + HostTopPeers | 기존 mock 인프라 재사용 |
| G15 | 외부 의존 | DNS 실패 → 외부 호출 전면 실패 | CoreDNS에 특정 도메인 NXDOMAIN 주입 | novel_template(UnknownHost ERROR) + 에러 급증 | 로그 + 소켓 연결 부재 | |
| G16 | 외부 의존 | 인증서 만료 → TLS handshake 실패 | 만료 cert 배치 | novel_template(SSLHandshake ERROR) | 로그 | 현재 mock이 http면 TLS 경로 신설 필요 — 보류 |
| G17 | 배포 | 버그 릴리스 — 배포 직후 에러 급증 | 에러 발생 코드가 든 이미지 태그 배포 | rate_spike + novel_template, 배포 직후 onset | 변경 이력 선행 게이트 + 로그 | change 양성 대표 |
| G18 | 배포 | 설정 오배포 — timeout 과소 → 간헐 실패 | configmap timeout 축소 배포 | 간헐 timeout ERROR + latency 상단 | 변경 이력 + trace | 간헐성 = 난이도 상 |
| G19 | NMS | 인터페이스 다운 — 트랩 + 실제 통신 단절 | .57 ifDown trap 송신 + iptables 차단 병행 | snmp trap 이벤트 + 서비스 에러 급증 | snmp_traps + 계열 교차(증상 APM·원인 NMS) | .57 셋업 선행 |
| G20 | NMS | 비정상 트래픽 flow 급증 | .57에서 iperf 등으로 flow 발생 | NetFlow 이상 + 대역 관련 메트릭 | netflow_records | .57 셋업 선행. ⚠ 실영향 요건 미충족 시 탈락 — 서비스 경로 대역 포화로 실지연 유발하게 재설계 필요 |

⚠ 위 표는 초안 시점 기술이다 — **후보별 실영향·실현성 판정은 §9.3 독립 검토
결과가 우선한다** (G6·G7·G10·G11·G15·G16·G19·G20은 현 설계로 실영향 미발생
실증/고위험, 재설계·재분류 대상).

음성 짝(paired guardrail)은 §5 규율대로 양성 확정 후 계열별로 선정한다 —
우선 후보: G8의 짝(플랜 변화 없이 데이터량 증가만으로 느려진 경우), G17의
짝(배포는 있었지만 무관한 경우 — 기존 Deployment Change 음성과 통합 검토).

### 9.2 관측 가능성 실측 (2026-07-14, 119 VictoriaMetrics 직접 질의)

G군의 원인 신호가 **실제 수집 중**인지 119에 라이브 확인한 결과. 총 1,474개
메트릭 유입 중.

| 후보 | 원인 신호 메트릭 (실측 확인) | 상태 |
| --- | --- | --- |
| G1 디스크 풀 | `sms.file_system.used`, `sms.file_system.inode_used` | ✅ 수집 중 |
| G2 IO 포화 | `sms.disk.busy_time`·`io_wait_counts`·read/write 8종 | ✅ 수집 중 |
| G3~G6 K8s | `kcm.pod.container_oom_killed`, `kcm.pod.container_restart_count` (650 series 라이브) | ✅ 수집 중 |
| G7 데드락 | `dpm.oracle.session.blocked_session`·`blocking_session` (PG/MySQL 대응 계열 존재) | ⚠️ 세션 blocking으로 관측 — `dpm_ch_query_event`는 ClickHouse 엔진 전용이라 PG/Oracle 데드락 "이벤트"는 ERROR 로그로 보강 |
| G9/L2 커넥션 | `db.client.connections.pending_requests`·`timeouts`·`usage`(HikariCP OTel) + `dpm.*.session.*` — 앱측·DB측 양쪽 수집 | ✅ 수집 중 |
| G10 Kafka lag | `kafka.consumer.records_lag`(_avg/_max 포함, 247 series 라이브) — **관측 gap 아니었음, 해소** | ✅ 수집 중 |
| G12 스레드 | `jvm.thread.count` (tomcat executor pool 메트릭은 없음 — 간접 관측) | ⚠️ 간접 |
| G13 GC | `jvm.memory.used_after_last_gc`, `jvm.memory.used/limit`, `jvm.cpu.*` | ✅ 수집 중 |
| G17/G18 배포 | RCA 변경 이력 소스는 PG `policy_deployments`/`change_history` — **k8s 앱 배포가 여기 남는지 미확인**, KCM pod 재생성으로 간접 관측 가능 | ❓ 확인 필요 |
| G19/G20 NMS | 수신기 존재·테이블 0건 — .57 셋업 선행 (§9 전제 작업) | 🔧 셋업 대기 |

공통 유의: **수집 ≠ 감지.** stream anomaly는 `ai_coverages`에 (target×metric)
등록된 것만 처리하므로([부하 규칙 §4.1-1](spec-scenario-load.md)), 각 시나리오
승격 전에 해당 메트릭의 coverage 등록 확인이 precondition이다.

### 9.3 독립 검토 결과 (2026-07-15, critic 에이전트 + Codex 이중 검토)

카탈로그(G1~G20 + L군 + scenario-01 구현)를 두 독립 검토자에게 교차 검토시킨
결과. Codex는 문서 외에 runner 스키마 import·golden 파일·서비스 소스까지 실증
확인. 원문: `.omc/artifacts/ask/codex-rca-aiops-*-2026-07-15T01-49-42-587Z.md`.
**종합 판정: 아이디어 백로그로는 유효하나, 기반 결함 수리 전 golden 승격 불가 —
후보 확장보다 기반 수리가 먼저다.**

#### (a) 기반 결함 (승격 전 필수 수리)

| # | 결함 | 근거 | 조치 |
| --- | --- | --- | --- |
| F1 | **scenario-01이 runner에 로드 불가** — yaml은 `root_cause` 객체/`propagation` 리스트인데 runner 모델(`models.py`)은 둘 다 str. import 시 ValidationError 실증 | Codex 직접 검증 | 스키마 정본(Pydantic/JSON Schema) 확정 + §10 per-scenario 전환 + loader glob + CI 검증을 한 작업으로 (rca-scenario-runner) |
| F2 | **golden 스키마 드리프트** — 첫 golden이 미정의 상태값 `conclusive` 사용(정본: confirmed/provisional/insufficient), 시나리오 ID 표기 `commerce:01` vs `commerce/scenario-01` 혼재 | 첫 케이스 파일 실측 | `schema_version` 있는 단일 golden 스키마 + validator, canonical scenario ID 규칙 |
| F3 | **15분 타임라인 vs 인시던트 judge 시계 불일치** — judge는 3m close/10m 보류/최대 30m 유예로 판정이 §3 타임라인보다 늦는 게 정상. 첫 캡처 "인시던트 미승격"도 이와 연관 가능 | eval-capture §8.2 대조 | **해소(2026-07-15)** — 주입 종료와 캡처 종료를 분리하고 `pipeline_settle_window=45m`, 캡처 범위 `[t1-2h,t2+45m]`로 확정 |
| F4 | **승격 게이트 모순** — 본 문서 §7 "3회 재현" vs 부하 규칙 §6 "1회 이상" | 문서 대조 | 층별 분리 권고: 결정층(탐지 이벤트) 3/3 + 비결정층(judge·RCA 텍스트) 의미 등가. 채택 여부 §11 |
| F5 | scenario-01 trace 기대값 자기모순 — `within: 10m` vs 스스로 명기한 60분 배치 주기 | critic | within ≥60m 완화 또는 1차 탐지축을 스트리밍 축(HikariCP metric·ERROR 로그)으로 |
| F6 | HikariCP 계열 귀속 흔들림(APM/DPM 문서 간 상이) → 층화 채점 오염 | critic | 규칙 고정: 앱측 풀(HikariCP)=APM, DB측 세션=DPM |
| F7 | 변경 이력 미확인 상태로 G3/G4/G6/G13/G17/G18이 change 근거 사용 | §9.2 ❓와 동일 | change ingestion 확인을 해당 후보 precondition으로 |

#### (b) 실영향 위험 — 재분류·재설계 대상

양 검토 합산. **탈락/재분류(음성 전환 포함) 우선 대상**: G6·G10·G11·G15·G16·
G19·G20·L4·L5·L6 (+G7 조건부). 코드 실증 근거가 있는 것:

| id | 왜 실영향이 안 나나 (실증) | 재설계 방향 |
| --- | --- | --- |
| G6 | rolling update 기본값이 ImagePullBackOff여도 구 pod 유지 → 무중단 | "실패 rollout 무영향" 음성으로 전환하거나 강제 교체 전략 명시 |
| G7 | 주입 트랜잭션끼리만 deadlock이면 victim은 주입 세션뿐, 사용자 정상 | 온라인 요청이 접근하는 hot row를 한쪽 트랜잭션에 포함 |
| G10/G11 | outbox 패턴이 Kafka 장애를 흡수 — 주문은 DB 커밋으로 성공, 체감 없음 | 배송/알림 SLA probe 추가로 가시화하거나 "흡수된 장애" 음성으로 |
| G15 | **PG 연결이 DNS 아닌 IP 리터럴**(payment application.yml 실측) — DNS 주입 무효 | 외부 호출을 전용 DNS 이름으로 전환 후 그 이름만 NXDOMAIN |
| G16 | mock이 HTTP — TLS handshake 자체가 없음 | TLS mock 경로 구축 전 보류 유지 |
| G19 | .57 합성 trap과 iptables 차단은 무관한 두 주입 — 가짜 상관 | 서비스 트래픽이 실제 지나는 인터페이스를 내리고 동일 인터페이스 trap 수집 |
| L4 | **cart는 원래 miss 시 DB 직행**(CartService.java 실측) — novel_path 불성립. fallback이 장애 흡수 | "Redis outage fallback overload"로 재정의, DB 용량 초과 유발 |
| L5 | **정산 배치에 FOR UPDATE 없음**(SettlementBatch.java 실측) — lock 경합 불가 | hot row 장시간 잠금 배치로 재구성 |
| L6 | 발행량 증가만으로 lag 안 생김 — consumer 제약 미정의 | consumer throttling을 원 결함으로, surge는 증폭기로 |

캘리브레이션 필요(무영향 위험, 실측 선행): G1(파티션 대상 특정)·G2(캐시
흡수)·G4(probe latency)·G5(통제 실패 시 전면 장애)·G8(대용량 시드
선행)·G9(고갈 시간 계산)·G12(공용 pool 포화 확인)·G13(OOM 전 GC 창)·G17(load
여정이 버그 경로 호출)·G18(tail latency와 겹침)·**L1(×8=트로프 32rps 무장애
실증 — run2)**·L3(CB 흡수)·L7(banking 용량). → R4 보강 필요: 배수 외에 최소
절대 RPS·achieved RPS 기록.

#### (c) RCA 정답 깊이 — 트리거/병목 분리 (양 검토 일치)

"트래픽 폭주했다"는 트리거 서술이지 원인 규명이 아니다. golden `cause`를
**initiator(외부 워크로드: 진입 target·route·baseline/observed rps)와
bottleneck(포화 자원: order-service target + mechanism)으로 분리**하고,
`expected_causal_depth: trigger_and_bottleneck` 축을 신설한다. 채점: 둘 다
답하면 완전정답, 하나만 답하면 부분정답, DB lock/배포 확정은 오답. L1↔L2
구별은 counterfactual로 고정(L1: 정상 pool도 실패 / L2: 정상 pool은 성공).
L1 entity 병기(api-gateway/order-service) 미확정도 해소 대상 — bottleneck
entity는 order-service 하나로.

#### (d) 파이프라인 갭 — 클러스터·인시던트 층 정답 부재

golden에 `expected_clusters`(members/must_exclude)·`expected_incident`
(decision/within/scope/severity) 계약이 없어 사슬 중간을 채점 못 한다. 빠진
구조 유형: 동일 사건 merge 양성, 독립 사건 anti-merge, distractor 공존,
flapping/reopen, 부분 영향(brownout) scope, 비자원형 기능 장애(잘못된
가격·중복 주문), 흡수된 하위 장애(음성), change 선행 체인, 다중 root. Phase 1
선정 시 이 유형들에서 최소 수 개를 포함한다.

**distractor 오귀속 자연 발생 실증 (2026-07-16).** 인시던트
`15d12f0c-424c`(promoted 05:01Z, critical): judge가 "192.168.200.136 호스트
자원 포화 → commerce-order 오류"로 서사화했으나 실측 반박 — 호스트는 CPU
load 0.3·메모리 47%로 여유. 실체는 **무관한 두 현상의 동시 발생**: ①worker-1
동거 Oracle 프로세스 RSS +252MB(전날 banking 프로브 여파)의
`sms.process.memory_rss_size` level shift 이벤트 다발 + ②commerce-order 주기적
502 버스트(진짜 원인은 재고 409가 CB를 열어 생긴 침묵 차단 —
[부하 규칙 §8.2](spec-scenario-load.md) 결함 6). 같은 노드·같은 시간대라는
이유로 하나의 인과로 병합된 스퓨리어스 상관. **distractor 공존/anti-merge
유형 시나리오의 필요성을 실환경에서 입증한 사례이자, 인시던트 층 golden
계약(`expected_incident`)이 오귀속을 채점할 수 있어야 하는 근거.** lucida-next
피드백감(judge의 동일 노드 상관 가중 검토).

## 10. 파일 레이아웃 (2026-07-15 확정)

시나리오는 **시나리오당 yaml 파일 하나**로 관리한다. 개별 시나리오 문서
(마크다운)는 따로 쓰지 않는다 — yaml + golden 레코드 + 주입 스크립트가
시나리오의 실체다.

```
rca-scenario-runner scenarios/services/<도메인>/
  service-spec.yaml            # service/target 공통 정보만
  scenarios/
    scenario-01-blackfriday-surge.yaml   # 구 scenarios: 리스트의 항목 하나가 최상위로
    ...
  scripts/
    scenario-01-blackfriday-surge.sh     # 주입 스크립트 — runner가 <도메인>/scripts/에서 해석 (기존 관례 유지)
```

- 근거: 시나리오 하나가 이미 ~70줄(부하 규칙 적용 후)이라 도메인당 한 파일
  누적은 Phase 1 30개 기준 비대해진다. 시나리오당 파일이면 PR 리뷰 단위·git
  이력·케이스 meta.json 참조(`commerce/scenario-01`)가 파일 단위로 일치하고,
  병렬 작성 시 merge 충돌이 없다.
- 주입 스크립트는 `<도메인>/scripts/`에 둔다 — runner의 스크립트 해석
  경로(`<script_dir>/<도메인>/scripts/<file>`) 기존 관례를 따른다(당초
  "yaml과 나란히" 안에서 2026-07-15 정정).
- ~~runner 로더 glob 확장·스키마 불일치(§9.3 F1)~~ → **구현 완료
  (2026-07-15, rca-scenario-runner PR #19 브랜치)**: Scenario 모델에 구조화
  필드(root_cause_detail·propagation_steps·injection·expected_anomalies·
  signals 등) 추가, 로더가 legacy(문자열)/구조화(객체) 양쪽 정규화 +
  `scenarios/*.yaml` per-scenario 파일 지원(중복 id는 하드 에러),
  `commerce/scenario-01` ID 표기도 해석. commerce는 per-scenario 레이아웃으로
  전환 완료, 실스펙 4도메인 전체 로드 회귀 테스트 추가(17 passed).

## 11. 열린 결정

- 계열별 Phase 1 후보 목록 — §9.3 반영: 기반 결함(F1~F7) 수리와 재분류
  대상 정리 후 선정.
- 양성/음성 paired guardrail을 어느 fault family부터 만들지.
- ~~시나리오 runner의 파일 레이아웃~~ → §10 확정(2026-07-15).
- 시나리오별 반복 실행 횟수와 허용 변동 범위 — §9.3 F4 층별 분리안(결정층
  3/3 + 비결정층 의미 등가) 채택 여부.
- NMS와 WPM을 Phase 1에 포함할 수 있는지 여부.
- ~~`pipeline_settle_window` 길이와 §3 타임라인·R6 간격 재조정~~ → 45분으로
  확정하고 캡처 시간창과 분리(2026-07-15, §3·§9.3 F3).
- golden `cause`의 initiator/bottleneck 분리 스키마 확정(§9.3 (c)) —
  spec-evaluation·authoring과 정합 필요.
- 클러스터·인시던트 층 golden 계약(`expected_clusters`/`expected_incident`)
  필드 확정(§9.3 (d)).
