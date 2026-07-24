# 머지 스펙 — F21-Q / F21-P 배관 (Tomcat 200 스레드 포화)

작성자: f21-executor-2 (병렬 실행, 팀리드 지시). 대상 정본: `docs/scenario-redesign-wip/design-F21-threads-sheet.md` +
`design-F21-{Q,P}-manifest.json` + `design-F21-{Q,P}-metadata.json`. 아래는 공유 파일(profiles.json/queries.json/
controllers.json/scenario-metadata.json/catalog.json/tests/live_probes.py/observation_queries.json) 변경을 직접
하지 않고 정확한 조각으로 명세한 것이다 — 적용은 그 파일들의 소유자가 한다.

**핵심 결론**: 설계 시트(§4)가 "NEW capability gap"이라 표기한 항목 중 2개(`hikari_pending_connections`,
`food_create_status_rate`/`transfer_2xx_rate`)는 **이미 배선되어 있다** (F19-P/F20 승격 때 반입됨). 그러나
manifest 초안이 그 사실을 몰라 (a) 서비스명을 잘못 쓰고(`food-order`→실제는 `food-delivery-order`), (b) 러너가
거부하는 방식(파라미터 부착)으로 호출하도록 설계돼 있다. 반대로 시트가 "실재하나 거칠다"고만 표현한 두 항목
(`http_server_active_requests`, `circuitbreaker_open`)은 VictoriaMetrics 실측 결과 **메트릭 자체가 존재하지
않는다** — javaagent가 OTel semconv `http.server.active_requests`도, resilience4j/CB 메트릭도 전혀 emit하지
않는다(2026-07-24, 119:18428, `__name__` 라벨 전수 2055종 grep 확인, 0건). 아래 §2가 대체 신호를 제시한다.

---

## 0. 코드 앵커 재검증 (grep 실측, 2026-07-24)

시트의 앵커를 모두 재확인했다 — 라인 번호·내용 전부 일치:

- `food-delivery/order-service/.../OrderService.java:57`(`@Transactional createOrder`, timeout 없음),
  `:60`(`restaurantClient.getRestaurant`), `:73`(`restaurantClient.getMenu`), `:96`(`dispatchClient.checkCapacity`),
  `:128`(`orderRepository.save` = 첫 SQL), `:173`(`getOrder` 단건), `:182`(`searchOrders` — 시트가 인용한 GET
  목록 엔드포인트의 실제 서비스 메서드, 컨트롤러는 `OrderController.java:32` `@GetMapping` 무인자).
- `food-delivery/order-service/.../application.yml`: `server:` 블록에 `port: 8080`뿐, tomcat 스레드 설정
  전무(기본 200 확정). `services.restaurant.read-timeout: 5s`(§53-54 상당, 실측 라인은 다르나 값 일치).
- `core-banking/api-service/.../ApiServiceApplication.java:12` `@SpringBootApplication(exclude=
  {DataSourceAutoConfiguration, HibernateJpaAutoConfiguration})` — 확인. api-service `application.yml`도
  `datasource` 섹션 자체가 없다(Hikari 설정 항목 부재로 재확인).
- api `application.yml`: `services.account.read-timeout: 10s`, `services.transfer.read-timeout: 10s`(조회
  프록시), resilience4j CB(`accountClient`/`transferClient`, window10/min5/50%/open5s) + retry(max-attempts=3).
- account `application.yml`: `services.transfer.read-timeout: 15s`, Hikari `maximum-pool-size: 10`.
- `TransferController.java:12` `@RequestMapping("/api/transfers")`, `:21` POST, `:27` GET(무관 조회프록시,
  surge.js `transactionHistoryJourney`가 이미 침).

결론: 시트 ①(코드 앵커) 재검증 통과. 변경 불필요.

---

## 1. loadgen 스크립트 (신규 생성 완료)

### 1-A. `food-delivery/loadgen/create-heavy-surge.js` (F21-Q)
- order-surge.js 파생. 여정 가중 90(order-create) / 10(orders-read, 아래 §3 참조).
- `TARGET_RPS` 기본값 **56**(시트 §4-B λ≈50 create-req/s 목표, 0.9×56≈50.4로 정합). 시트 초안의 55보다
  근소 상향 — 90% 가중과 곱 계산을 역산해 맞춤.
- `step: 'create'` 태그 유지 — `profiles.json load.north_south` 기존 `domain_profiles["http://.../30181"]
  .business_step = "create"`와 그대로 정합(변경 불필요).
- `step: 'orders-read'` 태그는 **신규** — GET `${ORDER_URL}/api/orders?page=0&size=10`(order-service 자신의
  읽기, `OrderController.java:32`/`OrderService.java:182`). 기존 read_step("menu")는 restaurant-service
  메뉴조회라 order Tomcat 풀과 무관 — §3에서 이 신규 태그가 왜 지금 당장 수집되지 않는지 설명.

### 1-B. `core-banking/loadgen/transfer-heavy-surge.js` (F21-P)
**이미 존재함** — 병렬 실행 중인 다른 executor(f21-executor 계열)가 동시에 작성 완료한 파일을 발견, 그대로
채택한다(내 초안은 폐기 — 이 파일이 더 나은 설계였다: `step: 'transfer'`/`step: 'list'` 태그를 surge.js와
동일하게 유지해 `business_step="transfer"` 계약을 안 건드린다). 내용 요약:
- 이체 가중 **40%**(surge.js 10%의 4배), `TARGET_RPS` 기본값 **90** → 이체-rps ≈36 (목표 λ≈25 대비 여유).
- 시트/manifest 초안의 `target_rps: 180` 및 "transfer 85%" 가정은 **폐기 권고** — 90rps·40% 조합이 이미
  목표를 만족하면서 `load.north_south target_rps.maximum(180)` 대비 훨씬 큰 여유(캘리브레이션 실패 시 상향
  여지)를 남긴다. §4 scenario_parameters는 이 파일의 실제값(90/40%)을 반영해 수정했다.
- 무관 조회프록시는 기존 `transactionHistoryJourney()`(`step:'list'`, GET `/api/transfers?fromAccount=`)를
  그대로 재사용 — 신규 태그 불필요, 기존 `loadgen.read_step_status_rate`(banking entry의 read_step="list")
  계약과 그대로 정합.

두 스크립트 모두 mulberry32 재현성(R4)·시드 계약·에러 처리 관례를 기존 surge.js/order-surge.js와 동일하게
따른다.

---

## 2. 관측 배선 — VictoriaMetrics 실측 결과 (119:18428, 2026-07-24)

### 2-A. `http_server_active_requests` (Tomcat busy-thread 근사) — **부재 확인**
`curl -sG http://192.168.230.119:18428/api/v1/label/__name__/values`로 전체 2055개 메트릭명을 받아
`http_server|tomcat|thread|busy|pool|queue|active|inflight|concurrent` 정규식으로 grep한 결과:
`http.server.request.size`/`http.server.response.size`만 존재(요청/응답 바이트 크기, 무관). OTel javaagent가
`http.server.active_requests`(semconv UpDownCounter)를 emit하지 않는다 — **설계 전제가 틀렸다.**
추가로 `http_server_request_duration_seconds_bucket`(기존 `prometheus.user_p95` 템플릿이 참조하는 지표)도
food-delivery-order/core-banking-api 어디에도 시계열이 없다(`/api/v1/series` 매치 0건) — 이 템플릿 자체가
이 두 서비스에 대해선 이미 죽어있다는 뜻이나, 이는 F21 스코프 밖의 기존 결함이라 별도 기록만 남긴다.

**대체 신호**: `apm.agent.otel.java.jvm.thread.count`(라벨 `jvm_thread_state`)는 실측 존재하며 서비스별로
값이 나온다. 2026-07-24 임의 시점(관측 부하 상태 불명) 실측: food-delivery-order `runnable=156,
timed_waiting=377, waiting=39`; core-banking-api `runnable=130, timed_waiting=455, waiting=52`. 이는 JVM
전체 스레드(GC/Kafka client 등 포함)이지 Tomcat 전용이 아니며, **baseline 자체가 이미 130~160 runnable**이라
"200 근접"이라는 절대 임계값 해석을 그대로 못 쓴다 — 델타(주입 전후 증가폭) 기준으로 재해석해야 한다.

**권고 신규 query_id**(queries.json 소유자 적용 필요):
```json
"prometheus.jvm_runnable_threads": {
  "adapter": "prometheus",
  "template_id": "apm-agent-jvm-runnable-threads-v1",
  "allowed_parameters": ["service_name"],
  "value_type": "number",
  "freshness_sec": 30
}
```
**PROMETHEUS_TEMPLATES 추가**(`rca-scenario-runner/backend/app/live_probes.py`, 소유자 적용):
```python
"apm-agent-jvm-runnable-threads-v1": (
    'sum(apm.agent.otel.java.jvm.thread.count{service_name="%s",jvm_thread_state="runnable"})'
),
```
디스패치 분기(`_prometheus_observation`)에 `prometheus.jvm_runnable_threads` 케이스 추가, 서비스 allowlist는
기존 `APPROVED_APM_SERVICES`를 재사용(이미 `food-delivery-order`/`core-banking-api` 포함, 신규 추가 불필요).

**manifest 수정 필요**: F21-Q `order_busy_threads`/F21-P `api_busy_threads` observation의
`query_id`를 `prometheus.http_server_active_requests`→`prometheus.jvm_runnable_threads`로 교체,
`success` 임계값(`gte 180`)은 **캘리브레이션 전 확정 불가**(baseline이 이미 130~160이라 180 임계가 주입 유무와
무관하게 걸릴 수 있음) — §4 캘리브레이션 게이트에 "임계값 재산정" 항목으로 명시.

### 2-B. `prometheus.hikari_pending_connections` (F21-Q) — **이미 배선됨, 서비스명만 정정**
`queries.json`에 이미 `template_id: otel-hikari-pending-v1` 등록돼 있고 `live_probes.py`
`PROMETHEUS_TEMPLATES["otel-hikari-pending-v1"] = 'sum(db.client.connections.pending_requests{service_name="%s"})'`
로 이미 구현·라이브 검증됨(코드 주석: "live-verified 2026-07-24"). `APPROVED_HIKARI_SERVICES = {food-delivery-order,
core-banking-transfer}`에 `food-delivery-order`가 이미 있다(F19-P가 반입) — **추가 배선 불필요.**
**manifest 버그 정정**: F21-Q manifest 초안의 `order_hikari_pending.parameters.service_name = "food-order"`는
틀렸다 — 실제 allowlist 값은 `"food-delivery-order"`. 그대로 두면 `LiveProbeError("Hikari service_name is
not allowlisted")`로 실행 시 즉시 실패한다. F21-P는 api-service에 Hikari가 없어 이 query 자체를 쓰지 않는
현재 설계(§4-A #2 "부재가 증거")가 맞다 — 변경 없음.

### 2-C. `prometheus.circuitbreaker_open` (F21-P) — **메트릭 부재, 관측 자체를 폐기 권고**
전체 2055개 메트릭명에서 `resilience4j|circuitbreaker|cb_state` 정규식 grep 0건. api/account/transfer
어디도 resilience4j actuator 메트릭을 Prometheus로 노출하지 않는다(actuator exposure는 `health,info,metrics`뿐
— `metrics`가 Prometheus 포맷으로 스크레이프되는지와 무관하게 VictoriaMetrics엔 해당 시계열이 없다). **신규
query_id를 만들 수 없다** — 만들려면 애플리케이션 코드에 micrometer-registry-prometheus 노출 설정이 필요한데
이는 F21 배관 스코프 밖(서비스 코드 변경)이다.

**권고**: F21-P manifest에서 `api_cb_open` observation과 `must_rule_out.api-cb-open-fs2` 항목을 **삭제**하고,
이미 배선된 `api_502_rate`(`prometheus.apm_service_error_rate`, `core-banking-api`가 이미
`APPROVED_APM_SERVICES`에 있어 즉시 사용 가능)만으로 FS-1/FS-2 배제를 수행한다 — CB open은 즉시 502 폭증을
유발하므로(시트 §2 "FS-2(CB open)→api 즉시 502·이체 0%") `api_502_rate<0.05`(기존 success 조건) +
`must_rule_out.api-502-cascade-fs1`(기존, `api_502_rate>=0.2`)만으로 감별선이 이미 충분히 선다. 이 삭제는
시트의 감별 설계(§2 표)를 훼손하지 않는다 — 오히려 존재하지 않는 신호에 의존하던 취약점을 없앤다.

### 2-D. `loadgen.food_create_status_rate` / `loadgen.transfer_2xx_rate` — **이미 배선됨, 파라미터 사용 불가**
`queries.json`에 이미 등록(`selector: "business.5xx.rate"` / `"business.2xx.rate"`), `live_probes.py
_loadgen_observation`에도 이미 매핑됨(`business_5xx_rate`/`business_2xx_rate` 문서 필드). **그러나** 이 함수는
`if query.parameters or query.query_id not in {...}: raise LiveProbeError("unsupported loadgen query")`로
**파라미터가 하나라도 있으면 무조건 거부**한다. F21-Q manifest 초안의 `order_read_get_latency`(parameters:
`{step:"read", metric:"p95_latency"}`)와 `order_create_503_rate`(parameters: `{step:"create",
status_class:"503"}`), F21-P manifest 초안의 `transfer_slow_2xx_rate`(parameters: `{step:"transfer",
status_class:"2xx", slow:true}`)는 **전부 이 형태라 실행 시 즉시 실패한다.**

또한 k6-summary 문서 스키마(`_loadgen_observation`의 `field` 매핑)에는 `achieved_rps` /
`checkout_5xx_rate` / `business_nonok_rate` / `read_nonok_rate` / `business_5xx_rate` / `business_2xx_rate`
6개 필드만 존재 — **p95 latency 필드 자체가 없다.** `order_read_get_latency`(read GET의 p95 지연)는 현재
아키텍처로 **원천적으로 구성 불가능**하다(rate만 있고 latency 없음).

**manifest 수정 필요**:
- F21-Q: `order_create_503_rate` observation의 `parameters` 제거(→ 파라미터 없이 `business_5xx_rate` 그대로
  사용, 이는 create 스텝의 5xx rate 전체를 의미 — dispatch 503과 order 자체 500 등을 구분 못 하지만 현재
  가용한 유일한 신호). `order_read_get_latency`는 **삭제**(§3에서 대체 불가 이유 설명) — success 조건의
  `unrelated-get-degraded`(order_read_get_latency≥3) 조건도 함께 삭제, 대신 §3의 대안을 채택.
- F21-P: `transfer_slow_2xx_rate` observation의 `parameters` 제거(→ `business_2xx_rate` 그대로). "느린-200
  비율"이라는 원래 의도(2xx이되 느림)는 이 필드로는 **속도 정보가 없어 검증 불가** — 순수 2xx 비율만 확인
  가능. success 조건에서 이 observation의 역할을 "2xx 비율이 여전히 높다(CB 미개방 방증)"로 재정의(원래
  목적인 "느림" 자체는 §2-A 대체 신호 `jvm_runnable_threads` 상승 + `api_502_rate` 저조 조합으로 대신 방증).

### 2-E. read-step 재계약 충돌 — F21-Q `orders-read` 태그는 지금 수집되지 않는다
`profiles.json load.north_south.domain_profiles["http://192.168.122.77:30181"].read_step = "menu"`는 이미
**F20-Q(승격됨, `controllers.json`에 `loadgen.read_step_status_rate` 사용 확인)가 점유 중**이다. food entry의
read_step 슬롯은 엔트리당 1개뿐(`load_north_south_executor.py`가 `domain_profile.get("read_step","")` 단일값을
모니터에 전달)이라, F21-Q를 위해 `read_step`을 `"orders-read"`로 바꾸면 F20-Q의 기존 계측이 깨진다.
**이번 배선에서는 read_step을 바꾸지 않는다** — `create-heavy-surge.js`의 `orders-read` 태그는 k6 자체
통계(check() 카운터)로는 남지만 `loadgen.read_step_status_rate`로는 수집되지 않는다. F21-Q는 "무관 GET 전염"
정량 신호 없이 `order_busy_threads`(§2-A 대체지표) + `order_hikari_pending`(§2-B, pending≈0 증명) 2개 신호로
성립해야 한다 — 시트 §2의 3번째 감별 증거(무관 GET 지연)는 **이번 승격 범위에서 계측 불가, 향후
`load_north_south_executor.py`에 두 번째 read_step 슬롯을 추가하는 엔지니어링이 필요**(별도 이슈로 분리 권고,
프로파일 실행기 파일은 이번 배관 스코프의 편집 금지 대상이라 이 파일에서 직접 처리 불가).

### 2-F. `api_pod_ready` (F21-P) — k8s target 승인 목록 누락
`live_probes.py APPROVED_K8S_TARGETS`에 `("rca-testbed-banking", "testbed-api")` 항목이 **없다**(banking은
`testbed-oracle`/`testbed-transfer`만 등록됨). k8s manifest 확인(`core-banking/k8s/20-api-service.yaml`)
결과 label selector는 `app=testbed-api`. **추가 필요**:
```python
("rca-testbed-banking", "testbed-api"): "app=testbed-api",
```

---

## 3. profiles.json `load.north_south` 머지 조각

`parameter_contract.allowed_script_paths`에 추가:
```json
"/opt/loadgen/food-delivery/create-heavy-surge.js",
"/opt/loadgen/core-banking/transfer-heavy-surge.js"
```
`parameter_contract.allowed_scenarios`에 `"F21-Q"`, `"F21-P"` 추가. `tag_pattern` 정규식 alternation에도
`21-Q|21-P` 추가(예: `...20-Q|21-Q|21-P)$`). `target_rps.maximum`(180)은 변경 불필요 — 아래 두 값 모두 그
이내.

`scenario_parameters`에 추가(§1의 실제 스크립트 기본값과 정합, 시트 초안 대비 수정됨):
```json
"F21-Q": {
  "target_rps": 56,
  "ramp_up": "2m",
  "hold": "8m",
  "ramp_down": "1m",
  "entry_url": "http://192.168.122.77:30181",
  "script_path": "/opt/loadgen/food-delivery/create-heavy-surge.js",
  "scenario_tag": "scenario_id=F21-Q",
  "seed": 4242,
  "baseline_unit": "loadgen-food"
},
"F21-P": {
  "target_rps": 90,
  "ramp_up": "2m",
  "hold": "8m",
  "ramp_down": "1m",
  "entry_url": "http://192.168.122.77:30082",
  "script_path": "/opt/loadgen/core-banking/transfer-heavy-surge.js",
  "scenario_tag": "scenario_id=F21-P",
  "seed": 4242,
  "baseline_unit": "loadgen-banking"
}
```
(F21-P의 `target_rps`를 manifest 초안의 180에서 **90으로 정정** — §1-B 근거. `domain_profiles`는 기존
food/banking 항목 재사용, 변경 불필요.)

---

## 4. catalog/scenario-metadata/controllers 승격 조각 — **캘리브레이션 게이트로 보류**

시트 §3/§4가 이미 명시하듯 두 시나리오 모두 `readiness=draft`, `prerequisite_gate.state=blocked`,
`live_allowed=false`다. 이번 배관으로 ①②는 그대로 유지(✅), ③(관측)은 §2에서 **실행 가능한 형태로 정정**했으나
"busy-thread 임계값 재산정"(§2-A)과 "무관 GET 전염 신호 계측 불가"(§2-E)라는 새 사실이 드러나 시트가 원래
가정한 것보다 관측 능력이 **더 좁다**. ④(주입 캘리브레이션)는 시트 §4-C 그대로 미해결(host.stress/db.workload
거친 대체 + read-timeout 미만 유지). 따라서 **이번 패스에서 catalog.json/scenario-metadata.json/
controllers.json에 반입하지 않는다** — 아래는 게이트 통과 후 반입할 조각을 미리 확정해 둔 것이다.

### 4-A. controllers.json에 반입할 controller 블록 (게이트 통과 후)
manifest의 `execution.controller` 서브트리를 그대로 쓰되 다음을 반영:
- F21-Q: `observations[].query_id`를 §2 정정대로 교체(`http_server_active_requests`→
  `jvm_runnable_threads`, `order_hikari_pending.parameters.service_name`→`food-delivery-order`,
  `order_read_get_latency` 삭제, `order_create_503_rate`의 `parameters` 제거).
- F21-P: `api_busy_threads`의 query_id 교체, `api_cb_open` observation·`must_rule_out.api-cb-open-fs2`
  삭제, `transfer_slow_2xx_rate`의 `parameters` 제거.
- 계약(팀리드 지시 6키 메타 + runtime 블록): `dispatcher_mode=trusted`, `live_enabled=false`(게이트 통과
  전까지 유지), `tick_interval=15s`, `settle_after_change=45s`, baseline 4항목(coordinator-clean/
  clean-window/baseline-traffic/target-health), capture 정본(pre10m/post20m, model_snapshot 경로),
  abort(`entry_status eq 0`, consecutive_ticks 2), recovery(`target_health eq 200` +
  `*_busy_threads`/`pod_ready` 조합) — manifest 원안 그대로 유지, §2 교체분만 반영.

### 4-B. scenario-metadata.json — F21-Q/F21-P 메타 반입
`design-F21-Q-metadata.json`/`design-F21-P-metadata.json`의 `must_support`/`must_rule_out`/
`distinguishing_evidence`를 §2-E(무관 GET 전염 계측 불가) 사실에 맞게 한 군데만 수정 권고: "무관 GET
/api/orders 지연 전염"을 결정 증거 3종 중 하나로 서술한 부분에 "(이번 승격 범위에선 정량 신호 없음 — 정성
서술/로그 기반 확인만 가능, §2-E)" 각주 추가. 그 외 내용은 코드 앵커·인과사슬 서술과 100% 정합해 그대로
반입 가능.

### 4-C. catalog.json / test 기대값
게이트 통과 시: `catalog.json.scenarios` 배열에 F21-Q, F21-P 2건 추가 → **50 → 52**.
`scripts/scenarios/tests/test_registry_contracts.py:48`의 `self.assertEqual(len(self.catalog["scenarios"]),
50)`를 `52`로 변경. 같은 파일 65/136/144행과 `test_ready_profile_executors.py:141`행의 시나리오 id 리스트
(`"...F20-R", "F20-P", "F20-Q", "F15-R", "F03-H"...` 패턴)에 `"F21-Q", "F21-P"`를 F20-Q 뒤에 삽입.
`controllers.json.live_scenario_ids`에도 두 id 추가(단, `controller.live_enabled=false`인 동안은 live 목록에
넣지 않는 것이 기존 F20 계열 관례와 일치하는지 controllers.json 소유자가 F20-R/P/Q 처리 방식을 참조해 확정
— 이 문서 작성 시점엔 F20 3종도 아직 `live_scenario_ids`에 없었다).

---

## 5. 캘리브레이션 게이트 (승격 전 필수, 시트 §4-C 재확인 + 신규 사실)

1. **정밀 지연 injector 부재** (시트 원안 그대로): restaurant/transfer hop엔 mock 없음. host.stress(worker-w2/
   w3) 또는 db.workload(live 미지원)로 간접 지연, read-timeout(5s/10s) 미만 유지가 관건.
2. **(신규) busy-thread 대체지표 임계값 미검증**: `jvm_runnable_threads`가 baseline에서 이미 130~160대라
   "≥180" 절대 임계가 무의미할 수 있음 — 실제 주입 중 delta 관측 후 임계값을 재확정해야 한다(§2-A).
3. **(신규) 무관 GET 전염 신호 없음**: §2-E — food read_step 슬롯이 F20-Q에 점유돼 있어 F21-Q는 이 결정
   증거 없이 성립해야 하며, 이는 시트 §2의 감별표를 3신호→2신호로 축소시킨다. 감별력이 약화됐다는 점을
   승격 심사 시 반드시 재평가할 것.
4. `api_cb_open`(F21-P) 관측은 메트릭 부재로 완전히 제거 — CB 배제는 `api_502_rate` 단독으로 수행(§2-C).

---

## 6. 파일 목록 요약

생성:
- `food-delivery/loadgen/create-heavy-surge.js` (신규)
- `docs/scenario-redesign-wip/merge-spec-F21.md` (본 문서)

변경 없이 채택(병렬 executor 산출물 확인):
- `core-banking/loadgen/transfer-heavy-surge.js` (이미 존재, §1-B 검토 완료 — 그대로 사용 권고)

편집하지 않음(공유 파일, 위 조각을 소유자가 적용):
- `scripts/scenarios/registry/{profiles,queries,controllers,scenario-metadata}.json`, `scripts/scenarios/catalog.json`
- `scripts/scenarios/tests/test_registry_contracts.py`, `test_ready_profile_executors.py`
- `rca-scenario-runner/backend/app/live_probes.py`, `observation_queries.json`
- `docs/scenario-redesign-wip/design-F21-{Q,P}-manifest.json` (§2/§3 정정치를 반영해 갱신 필요 — 소유자 판단)
