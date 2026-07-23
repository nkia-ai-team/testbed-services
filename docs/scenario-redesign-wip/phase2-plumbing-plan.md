# Phase 2 배관 지도 + 공유 부품 구현 계획

작성: 2026-07-23 · 근거: repo `scripts/scenarios/` + 앱 소스 + `scripts/runner-patches/2026-07-20-newcontract-v2.patch` 실측
스코프: repo = `/home/ydkim/project-2025/testbed-services/`

---

## 0. 핵심 판정 — repo vs 러너 경계 (이 문서의 결론)

**관측 adapter 실행 코드는 전부 러너(GB10 `/app/backend/app/live_probes.py`)에 있다.** repo의 `queries.json`은
**선언(declaration)일 뿐**이고, 러너의 `LiveProbeSet`은 **query_id별 하드코딩 allowlist**로 동작한다
(`if query.query_id not in {…}: raise LiveProbeError`). 각 adapter 메서드가 field/selector/PromQL/SQL을
query_id마다 **코드로 박아** 갖고 있다. plan에서 raw selector·SQL·PromQL을 넘기는 것은 설계상 금지
(live_probes.py 주석: "cannot provide endpoints, command arguments, SQL, selectors, or PromQL").

→ **결과: 새 query_id는 queries.json에 선언만으로는 절대 동작하지 않는다. 러너 `live_probes.py`에 대응
query_id 분기를 추가해야 한다.** 러너는 비-git 배포본이라 변경은 `scripts/runner-patches/`에 스냅샷 patch로 보존.

| 배관 부품 | repo 측 | 러너 측(`/app/backend`) |
|---|---|---|
| query_id 선언 | `registry/queries.json` | — |
| loadgen_summary 값 산출 | surge.js(3) + **k6 monitor**(load_north_south_executor.py 내 heredoc) → `-live.json` 필드 | `_loadgen_observation`: query_id→field 하드코딩 map + SSH로 tb-runner `-live.json` cat |
| prometheus 값 | — | `PROMETHEUS_TEMPLATES` dict + `_prometheus_observation` query_id 분기, VictoriaMetrics `192.168.230.119:18428` 질의 |
| database 값 | — | `_database_observation`: query_id별 SQL 하드코딩 |
| kubernetes 값 | — | `_kubernetes_observation`: query_id별 selector 하드코딩 |
| **주입(injector) 전부** | `profiles/*.py` executor + `registry/profiles.json` + `controllers.json` | — (executor는 tb-runner에 SSH하는 repo 코드; 러너 백엔드 무관) |

**요지: 주입 계층 = repo-only. 관측 계층 = 항상 러너+repo 2-트랙(선언은 repo, 실행은 러너).**

---

## 배관 (1) 관측 — query_id가 값이 되기까지

### loadgen_summary (유일하게 repo가 값을 만드는 adapter)
- 강도·상태 값의 **생산자**는 repo: `load_north_south_executor.py` 안에 heredoc으로 박힌 k6 monitor(90~211행)가
  k6 `--out json` 스트림을 tail하며 `/tmp/rca-scenario-<id>-live.json`을 1초마다 원자적 재작성.
- monitor가 쓰는 필드: `achieved_rps`, `entry_status`, `checkout_5xx_rate`, `business_ok`, `observed_at`.
- **소비자**는 러너: `_loadgen_observation`이 SSH로 tb-runner(192.168.122.206)의 `-live.json`을 cat →
  `{"loadgen.achieved_rps":"achieved_rps","loadgen.checkout_5xx_rate":"checkout_5xx_rate"}` **하드코딩 map**으로 필드 추출.
  freshness 30s + scenario_tag 일치 검증. 즉 새 필드를 monitor가 써도 러너 map에 없으면 못 읽는다.

### surge.js step 태깅 구조
- k6가 `http_reqs` Point마다 `status` 태그를 **자동** 부여. surge는 각 요청에 `tags:{journey,step}`을 붙인다.
- monitor는 `http_reqs` 중 `tags.step == business_step`인 것만 골라 `entry_status = int(tags.status)`로 읽고
  `checkout_results`에 (stamp,status) 축적.
- `business_step`은 `profiles.json > load.north_south > parameter_contract.domain_profiles[entry_url].business_step`:
  commerce=`checkout`(surge step:'checkout'), food=`create`(surge step:'create'), banking=`transfer`(surge step:'transfer'). **3도메인 다 태그 존재 — surge.js는 이미 준비됨.**

### 왜 401·food 503이 checkout_5xx_rate에 안 잡히나 (코드 확정)
- monitor 190~193행: `checkout_5xx_rate = sum(status >= 500 …)/len(...)`. **버킷이 ">=500" 단 하나.**
  - **401(F16-H fail-close)**: 401 < 500 → checkout_5xx_rate에 0 기여. 구조적 불가시. (`entry_status` 스칼라엔
    잡히지만 rate query가 없고, `business_ok = entry_status in {200,400,409}`라 401은 business_ok를 flip만 함.)
  - **food 503**: 503 ≥ 500이라 산술적으론 잡혀야 하나 **구조적 2중 차단**:
    (a) food는 api-gateway가 없어 surge가 `RESTAURANT_URL/ORDER_URL/DISPATCH_URL` **3개 URL**을 씀. executor는
    `--env "$gateway_env=$entry_url"` **단일** URL만 주입 → business_step='create'(ORDER_URL)이 기본값으로 남아
    실제 주문경로가 구동 안 됨. (b) food surge order는 bounded(10%)+menu 실패 시 early-return이라 표본 희박.
  - 공통 근본: **monitor가 오직 한 step의 한 버킷(≥500)만 rate로 낸다.** 상태-클래스(2xx/4xx/5xx)·복수 step 미지원.

### prometheus adapter
- query 표현식은 repo가 아니라 러너 `PROMETHEUS_TEMPLATES`(live_probes.py)에 하드코딩. 현재 6종 전부
  `kcm.*`(KCM) / `apm.agent.otel.java.*`(APM/OTel) / `http_server_request_duration…`(OTel) — **전부 관측에이전트·OTel
  메트릭**. 앱 micrometer 메트릭은 하나도 없음. `raw_promql_allowed:false`.

### database adapter
- raw SQL 아님. query_id별 SQL이 러너 `_database_observation`에 하드코딩(예: `database.mysql_index_present`는
  information_schema SELECT 문자열이 러너 코드에 통째). repo엔 selector 문자열만 선언. **새 selector = 러너에 SQL 신설.**

---

## 배관 (2) 주입 — 계약이 executor를 부르기까지 (전부 repo)

### profiles.json 구조
- `profiles.<id>.parameter_contract.allowed_scenarios`: 시나리오 allowlist.
- load.north_south는 추가로 `allowed_entry_urls`·`allowed_script_paths`·`domain_profiles`(entry_url→health_path/
  gateway_env/business_step/baseline_unit)·`target_rps`bounds·`duration_pattern`·`tag_pattern`. **food(30181)·banking(30082)
  domain_profile + script_path 이미 등재.** allowed_scenarios엔 F16~F19 미등재(확장 필요).
- `scenario_parameters`(고정 파라미터) / `scenario_levels`(적응 사다리).

### executor의 APPROVED_TARGETS/CONTRACTS 하드코딩
- `k8s_probe_executor.py`: `APPROVED_TARGETS={"F05-H":(ns,deploy,container,probe)}` + `F05_H_PARAMETERS` 정확일치.
- `k8s_env_executor.py`: `APPROVED_TARGETS`{F03-P,F09-H,F08-P} + `APPROVED_KEYS`(시나리오별 허용 env 키 집합).
- `mock_expectation_executor.py`: validate에서 `path=="/v1/payments"` 하드코딩 + build_invocation 54행
  `namespace=="rca-testbed-commerce" & resource=="deployment/testbed-external-pg-mock"` 하드코딩.
- `load_north_south_executor.py`: 자체 `validate_parameters`가 profiles.json contract 전량 대조.

### build_invocation이 allowlist를 강제하나 — 감사 지적 "profile을 {}로 넘겨 미강제" **사실확인 결과**
- **주 경로는 강제된다.** `executor_common.main_for`(113~115행)가 **실제** profile을 로드해 `validator(scenario_id,
  params, profile)`에 넘긴다 → validate가 `profile["parameter_contract"]["allowed_scenarios"]` 대조. load.north_south도
  자체 main이 실 profile로 검증.
- 감사가 가리킨 `{}`는 k8s_probe/k8s_env의 **build_invocation 내부 재검증** `validate(…, {})`(probe 63행, env 47행).
  이 2차 호출은 `profile.get("parameter_contract",{}).get("allowed_scenarios")`가 None→allowlist 분기를 건너뛴다.
  **그러나** 같은 함수의 `APPROVED_TARGETS` 하드코딩이 여전히 강제하고, 1차(main_for)에서 실 profile로 이미 걸러짐.
  → **판정: 진짜 구멍 아님(이중 방어 중 2차가 약화). allowlist 확장 시 profiles.json + APPROVED_TARGETS 둘 다 고쳐야
  실효.** (2차 `{}`를 실 profile로 바꾸는 정리는 선택적 hardening.)

### controllers.json 바인딩
- `controllers.<id>.profile`: `kind`(fixed/adaptive) + `primary_ref`(주입 profile_id) + `companion_refs`(동반, 예
  load.north_south 부하) + `approved_profile_id` + `levels`(파라미터). `observations[]`이 query_id를 controller에 배선
  (`{id,adapter,query_id,freshness}`). `live_scenario_ids`가 live 허용 목록.

---

## 공유 부품 4종 — 파일:수정지점 단위 구현 계획

### 부품 1. status-class 관측 (loadgen.write/read/food_create/transfer_2xx_status_rate)
**목적**: 401·food503을 잡도록 step×status-class 버킷 rate 신설.

| 조각 | 파일(절대경로) | 수정 성격 | 트랙 |
|---|---|---|---|
| monitor 집계 | `scripts/scenarios/profiles/load_north_south_executor.py` (heredoc monitor 183~209행) | **확장**: `checkout_results`를 (stamp,status)로 이미 보유 → status-class 카운터(2xx/4xx/5xx) 추가, `-live.json`에 `status_2xx_rate/4xx_rate/5xx_rate`(+step별) 필드 추가 | repo |
| surge step 태그 | commerce/food-delivery/core-banking `loadgen/surge.js` | **무변경**: step 태그·status 자동태그 이미 존재 | repo |
| query 선언 | `scripts/scenarios/registry/queries.json` | **신규**: `loadgen.checkout_status_rate`류 4종, adapter=loadgen_summary, selector=신규 필드명 | repo |
| **필드 소비** | 러너 `live_probes.py::_loadgen_observation` field-map | **신규 분기**: query_id→새 필드 매핑 + 범위검증([0,1]) | **러너** |
| food 단일URL 한계 | `load_north_south_executor.py` remote_script + `profiles.json` domain_profiles | **확장(설계결정 필요)**: food는 3 URL이라 단일 entry_url 주입으론 order경로 불가 — food는 status-class를 order-service 직행 별도 계약으로 분리하거나 monitor가 복수 step 처리 | repo (설계 미결) |

- **repo만으로 가능? 아니오.** monitor가 필드를 써도 러너 `_loadgen_observation` map에 없으면 반환 불가.
- 리스크: food 3-URL 구조 미해결 시 food_create rate는 값이 안 나옴(중). monitor drain 성능(고 RPS fsync, 기존 F07-H 교훈) 재현 주의(하).

### 부품 2. CB open (prometheus.gateway_circuitbreaker_open{cb=user} / cb_open{pg})
**실측 판정: 현재 인프라로 불가 — resilience4j 메트릭이 export 자체가 안 됨.**
- 앱 pom엔 `spring-boot-starter-actuator`만(=/actuator/health용). `micrometer-registry-prometheus`·`resilience4j-micrometer`
  **의존성 없음**, `management.endpoints.web.exposure.include: prometheus` **설정 없음**. → `/actuator/prometheus` 미노출.
- 러너 prometheus adapter가 질의하는 VictoriaMetrics(119:18428)는 KCM/APM(OTel) 메트릭만 수집. 앱 micrometer 스크레이프 타깃 없음.
- CB open을 진짜 메트릭으로 관측하려면 **4-트랙**: (a) pom 의존성 2종 추가+(b) application.yml exposure — **앱 소스변경**;
  (c) 119 VictoriaMetrics 스크레이프 타깃 추가 — **인프라**; (d) 러너 `PROMETHEUS_TEMPLATES`+분기 — **러너**; (e) queries.json — repo.
- **권고**: 부품 2를 폐기하고 **부품 1(status-class 401/502 rate)로 CB-open 효과를 관측**한다(F16-H=401 rate, F19-S=502 rate).
  전용 CB 메트릭은 비용 대비 실익 낮음.
- 리스크: 앱 소스+빌드+재배포+스크레이프까지 손대면 baseline 오염·회귀 위험(상). → 채택 비권고.

### 부품 3. DB selector (integrity_violation_count / outbox_unpublished_count / ledger_imbalance)
| 조각 | 위치 | 성격 | 트랙 |
|---|---|---|---|
| selector 선언 | `scripts/scenarios/registry/queries.json` | 신규 query_id(adapter=database, selector 문자열, allowed_parameters) | repo |
| **SQL 실행** | 러너 `live_probes.py::_database_observation` | **신규 분기**: query_id별 SQL 문자열 하드코딩(기존 `database.mysql_index_present` 패턴). banking=Oracle(sqlplus), food=MySQL DSN | **러너** |
| 앵커 데이터 | core-banking outbox(`OutboxEvent`/`OutboxRelay` `@ConditionalOnProperty(outbox.relay.enabled)`) · transfer `Account.status` FROZEN | 기존 존재(무변경). outbox_unpublished = `SELECT count(*) … WHERE published=false` | repo(앵커만 확인) |
- **repo만으로 가능? 아니오. 러너에 SQL 신설 필수.** selector는 "러너측 정의"임이 확정(repo는 이름만).
- outbox 앵커는 실재(banking relay 빈 게이팅 `@ConditionalOnProperty` 확인). ledger_imbalance는 정의 SQL 설계 필요.
- 리스크: Oracle/MySQL 접속 크레덴셜·DSN이 러너 env 의존(중). ledger_imbalance 정의 모호(중).

### 부품 4. injector 계약확장 (신규코드 최소) — **전부 repo-only**
| 조각 | 파일(절대경로) | 성격 |
|---|---|---|
| mock.expectation food 일반화 | `scripts/scenarios/profiles/mock_expectation_executor.py` | build_invocation 54행 namespace/resource 하드코딩 → food(`rca-testbed-food` mock deploy) 허용 **guard 확장**; validate 22행 `path=="/v1/payments"` → food PG mock path 허용. + `profiles.json > mock.expectation.allowed_scenarios`에 F19-P/S 추가 + scenario_parameters |
| k8s.probe allowlist(transfer) | `scripts/scenarios/profiles/k8s_probe_executor.py` `APPROVED_TARGETS` + `F17-R` params 상수 + `profiles.json > k8s.probe.allowed_scenarios` | **확장**(APPROVED_TARGETS + profiles.json **양쪽**, §부품2 강제 판정 참조) |
| k8s.env allowlist(OUTBOX_RELAY) | `scripts/scenarios/profiles/k8s_env_executor.py` `APPROVED_TARGETS`+`APPROVED_KEYS`(F18-P: `OUTBOX_RELAY_ENABLED`) + `profiles.json > k8s.env.allowed_scenarios` | **확장** 양쪽 |
| load.north_south entry_url/surge | `scripts/scenarios/registry/profiles.json > load.north_south` | **선언 확장**: allowed_scenarios에 F16~F19 추가(entry_url·script_path·domain_profile은 food·banking 이미 등재) |
| F17-P dual-arm 스크립트 | 신규 `commerce/loadgen/frozen-bypass.js`(또는 tb-runner 배포) + load.north_south allowed_script_paths | **신규 파일**(부품4 중 유일한 실코드; north_south는 단일스크립트 전제라 executor 수용 여부 확인 필요) |
- **repo만으로 가능? 예(부품 4 전부).** 러너 무관. 단 주입은 되어도 **관측은 부품 1/3(러너)에 종속**.
- 리스크: mock food 일반화가 commerce mock 스냅샷-복원 계약(snapshot/restore) 가정을 깨지 않아야(중). allowlist 확장 시
  profiles.json↔executor 상수 **동기 필수**(불일치 시 refuse)(하).

---

## 우선순위 요약
1. **부품 4(injector, repo-only)** 먼저 — 러너 의존 없이 즉시 착수, 주입 실증 가능.
2. **부품 1(status-class)** — repo monitor 확장 + **러너 patch 1건**(가장 광범위 공유, F16-H·F19·F04-H·food429).
3. **부품 3(DB selector)** — repo 선언 + **러너 patch 1건**(F17-P·F18-P).
4. **부품 2(CB open)** — **비권고/보류**. 부품 1로 대체.
