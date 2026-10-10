---
title: F58-R 설계 시트 (food order-service 를 운영에 없는 설정을 읽는 릴리스로 롤아웃해 주문 생성 전량 500)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
summary: food order-service 를 결함 있는 새 릴리스(food-delivery-order:2.5.0, fault-images/f58-r 패치로 만든 별도 태그)로 롤아웃하면, 새로 더한 최소 주문 금액 검사가 주문마다 읽는 정책값이 릴리스의 개발 프로필 파일에만 있고 운영 설정에는 없어서, order 가 Ready 인 채 하류를 부르기도 전에 모든 새 주문이 500 으로 실패하는 시나리오. 원본은 ServiceChannel 2025-05-08, 개발 환경에만 설정되고 운영에 동기화되지 않은 설정을 새 코드가 읽으려다 실패한 운영 릴리스 장애.
---

# F58-R 설계 시트

## 1. 요약

food order-service 의 새 릴리스 2.5.0 은 주문 생성 첫 단계에 "최소 주문 금액" 검사를 더한다. 운영 정책값이라 재배포 없이 바꿀 수 있게 하겠다며 값을 기동 때 한 번 묶지 않고 **주문마다** `Environment.getRequiredProperty("order.policy.minimum-amount", BigDecimal.class)` 로 읽는다. 개발자는 그 값을 릴리스에 함께 넣은 `application-dev.yml`(dev 프로필, 12000)에 적었고, 로컬과 개발 환경은 dev 프로필로 돌아 검사가 잘 동작했다. 운영 쪽(기본 `application.yml`, ConfigMap `service-config`, 파드 env, 활성 프로필 없음)에는 이 값이 옮겨지지 않았다.

기동과 `/actuator/health` 는 이 값을 읽지 않는다. 그래서 새 파드는 startup, readiness, liveness 프로브를 모두 통과해 Ready 가 되고, 기본 롤링 전략(maxSurge 25%)으로 옛 파드를 대신한다. 그 순간부터 모든 `POST /api/orders` 가 첫 줄에서 `IllegalStateException: Required key 'order.policy.minimum-amount' not found` 를 던진다. order 의 `GlobalExceptionHandler` 는 `ServiceException` 만 다루므로 이 예외는 500 이 되고, Tomcat 이 SEVERE `Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Request processing failed: java.lang.IllegalStateException: Required key 'order.policy.minimum-amount' not found] with root cause` 를 남긴다. 주문은 가게, 메뉴, 용량, 배차, 결제 호출과 주문 행 쓰기 **전에** 끝난다. 하류와 MySQL 에는 오류가 하나도 없다.

비유: 배달 앱 주문 접수처(order)에 "최소 주문 금액 미만은 받지 마라" 는 새 규정이 내려왔는데, 규정집 본문에는 금액이 없고 금액은 연습용 사본(dev 프로필)에만 적혀 있다. 접수처는 주문이 올 때마다 본문에서 금액을 찾다 없어서 주문을 모두 돌려보낸다. 가게, 배차실, 결제창구는 멀쩡하고 그저 일감이 끊긴다. 고칠 곳은 운영 규정집 없이 나간 접수처의 새 릴리스다.

## 2. 원본 사례

- **ServiceChannel, 2025-05-08** (공식 상태 페이지 사고와 사후 보고, 게시 2025-05-15): https://status.servicechannel.com/incidents/445h94td476q
  - 사고 제목 "Code Release Causes US Environment Outage". 예정된 미국 운영 코드 릴리스 뒤 2:29 AM EDT 부터 로그인 어려움(3:07 AM 까지)과 핵심 대시보드 기능 중단(4:12 AM 까지). 영향 Major, 사용자 Many, 빈도 Continuous.
  - 대시보드 원인: 개발 환경에는 올바르게 설정된 설정 하나가 "had not been fully synchronized to the production environment", 새 코드가 그 설정에 접근하려다 실패. 이 틈은 "wasn't detected until the new code attempted to access the setting during the deployment".
  - 탐지: 2:29 AM SRE 경보(대시보드, 로그인). 완화: 로그인 모듈 롤백(3:07 AM), 대시보드는 필요한 설정을 모두 운영에 적용해 회복(4:12 AM). 후속: 배포 절차에 설정 검증, 롤백 절차 개선.
  - 릴리스 내용과 설정 이름은 적혀 있지 않다. 로그인 쪽(배포 절차 개선으로 넣은 설정 조정이 운영에서 다르게 동작)은 재구성하지 않는다.
- `docs/ref-real-world-incidents.md` M2 에 이번에 추가했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 예정된 운영 코드 릴리스 | order-service 를 릴리스 food-delivery-order:2.5.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그) |
| 원인이 된 결함 | 새 코드가 접근하는 설정이 개발 환경에만 있고 운영에 동기화되지 않음 | 새 최소 주문 금액 검사가 주문마다 읽는 `order.policy.minimum-amount` 가 릴리스의 `application-dev.yml` 에만 있고 운영 설정(application.yml, ConfigMap, env)에 없음 |
| 전파 경로 | 새 코드가 설정에 접근할 때 실패 → 그 기능(대시보드) 불가 | 주문 생성 첫 줄에서 `getRequiredProperty` 실패 → 처리되지 않은 예외로 500 → 주문 생성 기능 전체 불가(다른 기능은 정상) |
| 사용자 증상 | 핵심 대시보드 기능 사용 불가(Major, Many, Continuous) | food 의 모든 새 주문이 500. 둘러보기, 메뉴, 검색, 배달 조회, 주문 조회는 정상 |
| 탐지된 경로 | 배포 중 SRE 경보 | order 오류율, Tomcat SEVERE 로그 급증으로 lucida 이상 탐지와 인시던트(원칙 7) |
| 완화와 복구 | 필요한 설정을 운영에 적용(로그인은 롤백) | cleanup 이 매니페스트 이미지로 되돌림(롤백). 쓴 데이터가 없어 되돌릴 것이 없다 |

기전 고리(새 코드 배포 → 운영에 없는 설정 접근 → 그 기능 실패)는 원본과 같다. 바꾼 것은 대상(대시보드 → 주문 생성)과 규모뿐이다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 32 = 75 → 76)

- 서비스(정식 + 후보): commerce 27, core-banking 25, **food-delivery 23 → 24**(가장 적은 서비스부터, 원칙 2).
- 묶음: **J(결함 있는 새 버전 배포) 10 → 11, 76 중 14.5%**(20% 미만). 결함이 "새 코드가 기대한 설정이 운영에 없음" 이라 J+G 로 적는다(G 로 세도 8 → 9, 11.8%). 금지 묶음 없음.
- 정답 위치: **주문 서비스 7 → 8, 10.5%**(food order 는 F49-H 하나뿐이었다). 결제 경로 합계 12/76(15.8%), 바뀌지 않음.
- 왜 이 후보인가:
  - 현실에서 가장 흔한 계기(바이너리 배포 37%, 설정 배포 31%, Google SRE Workbook)의 겹침이다. "코드는 맞는데 그 코드가 기대한 운영 설정이 빠짐" 은 테스트베드에 아직 없던 결함 꼴이다(기존 J 는 메모리, 질의, 연결 누수, 호출 폭증, 응답 형식, 요청 한도, 산출물 부재).
  - 증상(주문 생성 전량 500)은 F53-R(주문 표 1146), F48-P(outbox 표 1146), F38-R(read_only 1290)과 같고 정답이 다르다(카탈로그 관계 H 성격). 관제 AI 가 "order 500 이면 DB" 를 외우지 못하게 하고, 롤아웃 대상과 로그 문장으로 추론하게 한다.
- 이번 실행에서 막힌 후보, 장부 §4-1, §5, rejected 와 같은 원본 사례나 같은 주입이 아니다(ServiceChannel 은 처음 쓰는 원본이고, order 의 2.5.0 태그와 이 패치는 새 주입이다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `k8s.image` release 모드가 `testbed-order` 컨테이너 이미지를 `food-delivery-order:latest` → `food-delivery-order:2.5.0`(food-delivery/k8s/20-order-deploy.yaml:27-28, imagePullPolicy Never, replicas 1 :9, nodeSelector tb-w3 :18-19, envFrom service-config :31-33).
2. 새 파드 기동: 프로브가 모두 `/actuator/health`(:72-91)라 정책 키를 읽지 않아 통과, Ready → 옛 파드 Killing(평시 롤아웃 46초).
3. 결함: `scripts/scenarios/fault-images/f58-r/order-service.patch:38-45`(createOrder 첫 단계 `environment.getRequiredProperty("order.policy.minimum-amount", BigDecimal.class)`), `:68-78`(application-dev.yml, dev 프로필에만 값). 운영 기본 설정 `food-delivery/order-service/src/main/resources/application.yml:120-131` 의 `order:` 블록에는 cleanup, retention 뿐이고 ConfigMap `food-delivery/k8s/02-configmaps.yaml:4-18` 에도 없다. 활성 프로필을 정하는 `SPRING_PROFILES_ACTIVE` 는 어디에도 없다.
4. 실패: `AbstractPropertyResolver.getRequiredProperty` 가 `IllegalStateException("Required key 'order.policy.minimum-amount' not found")` → `food-delivery/order-service/src/main/java/com/fooddelivery/order/config/GlobalExceptionHandler.java:9-21` 은 `ServiceException` 만 처리 → 500, Tomcat `StandardWrapperValve` SEVERE.
5. 결과: `OrderService.createOrder`(food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:57-60) 의 가게 조회 전에 끝나 restaurant, dispatch, payment 호출과 주문 행, outbox 쓰기가 없다. `@Transactional` 이 연 트랜잭션은 바로 롤백되어 풀을 잡지 않는다.

로컬 실측(2026-10-10, 패치 jar, mysql:8.0 + food init.sql, 하류 주소는 닫힌 포트):
- 운영과 같은 프로필(활성 프로필 없음): `POST /api/orders` 200건 모두 500, 평균 0.011초, 최대 0.084초. 로그 'Required key' 400줄(요청마다 SEVERE 한 줄 + 스택 첫 줄), 하류 연결 시도 0건, `/actuator/health` UP(db UP).
- 같은 jar 를 `SPRING_PROFILES_ACTIVE=dev` 로: 검사를 지나 가게 조회로 넘어감(닫힌 포트라 502), 금액 9000 짜리 주문은 400 'Order total is below the minimum order amount 12000'. 개발 환경에서는 동작했다는 원본의 조건과 같다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-order`(target_kind container). 결함을 가진 곳은 운영 설정 없이 나간 order 릴리스이고, 되돌리거나 운영 설정을 더해야 재발이 막힌다. 원본 사후 보고가 지목한 층위(코드 릴리스와 설정 틈)와 같다.
- `trigger_target_id`: 비움(계기인 롤아웃과 근본이 같은 서비스).
- `scoring.partial`: 비움. 하류(restaurant, dispatch, payment)와 MySQL 은 이 장애와 무관하고, 관제 데이터로 합리적인 답은 order 하나다. "order 의 설정 누락" 이라는 답도 order 를 가리키므로 accept 에 든다.
- 원칙 5: 정답에 코드 설계 추론이 필요 없다. 롤아웃 시각과 바로 뒤 'Required key ... not found' 로그만으로 "order 새 릴리스가 운영에 없는 설정을 읽는다" 에 이른다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida 이상 탐지 | order 서버 스팬 오류율(500) 급증, order SEVERE 로그 급증, 'Created order' 로그와 하류 호출 소멸. 별도 업무 규칙 없이 겉 증상이 크다(주문 생성 전량 실패) |
| 피해 판정 | 러너 | 동반 부하 k6 의 주문 생성 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.2(3틱 연속) |
| RCA | 녹화 데이터 | KCM 롤아웃과 Pulled 태그 2.5.0, order 'Required key' SEVERE, 자식 스팬 없는 500, 하류와 MySQL 무오류 |

## 7. 관측 근거 (119 실조회, 평시)

| 증거 | 119 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(timestamp, reason, object_name, body) | `namespace='rca-testbed-food' AND object_name LIKE 'testbed-order%' AND reason='Pulled'` 30일, body 별 | `Container image "food-delivery-order:latest" already present on machine` 4건(2026-10-01 04:04 ~ 10-08 18:52)뿐. ScalingReplicaSet 'Scaled up replica set testbed-order-8694c6f795 to 1' 18:52:14 → Killing 18:53:00(46초) 도 수집됨. 장애 때는 2.5.0 이 본문에 담긴다 |
| 근본: 설정 조회 실패 로그 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | `body LIKE 'Servlet.service()%'` 30일 서비스별 | 처리되지 않은 예외의 Tomcat 로그가 SEVERE 로 수집됨(food-delivery-order 1,454건 JpaSystemException, 44건 CannotCreateTransaction 등). 이 경로의 로그가 녹화에 남는다 |
| 근본: 새 문장이 평시에 없음 | 같은 표 | `body LIKE '%Required key%' OR body LIKE '%IllegalStateException%'` 30일 전 서비스 | 0건. `service_name LIKE 'food-delivery%' AND body LIKE '%minimum%'` 30일 0건 |
| 피해: 주문 생성량 | 같은 표 | `service_name='food-delivery-order' AND body LIKE 'Created order%'` 시간별 24시간 | 2026-10-09 16시 ~ 10-10 16시 시간당 196 ~ 4,145건(초당 약 0.05 ~ 1.15). 장애 때 0 이 된다 |
| 전파: 하류 호출 소멸 | 같은 표(dispatch 'Dispatched courier', payment 로그), CH `otel_traces_local` | 서비스별 로그 수 | 평시 주문마다 남는 하류 로그가 장애 때 끊긴다(로그는 전수라 표본에 기대지 않는다) |

트레이스(10% 표본)와 APM 지표는 보조 증거로만 쓴다. 핵심 증거(롤아웃 이벤트, 'Required key' 로그, 'Created order' 소멸)는 전수 수집 표에 있다.

## 8. 감별

- must_support: 정답지 4개(롤아웃과 2.5.0 Pulled, 'Required key' SEVERE, 자식 스팬 없는 500 과 'Created order' 0, order Ready 와 하류, MySQL 무오류).
- must_rule_out:
  - F53-R, F48-P(표 소멸 1146): MySQL 오류가 없고 주문이 가게 조회 전에 끝난다. DPM 표 수 그대로.
  - F38-R(read_only 1290): 다른 서비스 쓰기가 평소대로.
  - 하류 고장(F49-R, F44-R, F42-P, F32-R, F33-H, F43-P, F55-R, F06-P, F19-P): order 가 하류를 부르지 않는다. 502, 503 이 아니라 500.
  - F49-H(order 다른 릴리스): 배차까지 가지 않고 응답 해석 실패 로그가 없다.
  - order 자원 고갈, 다운: Ready, 재시작 없음, 0.1초 안 응답.
- contrast_with: F53-R, F48-P, F38-R, F49-H, F17-H(정답지에 각 차이).

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: `k8s.image` release 모드, 레벨 하나 `approved-fixed-f58-r`(min_hold 15m, settle 30s, timeout 20m). 강도 사다리 없음(설정이 없으면 모든 주문이 실패하는 이진 결함).
- 동반 부하: `load.north_south` order-surge.js, target_rps 2, entry 30181, ramp_up 2m, hold 21m(F49-H 와 같은 값, 주문 생성 초당 약 1.4건). 상주 기준선은 하루 주기로 주문 생성 초당 0.05 ~ 1.15건이라 한가한 시간에도 판정 표본이 서게 한다.
- 부하 상한: 2rps 는 상한 180 의 1.1%. 주문이 0.01초 만에 끝나 order 스레드, 풀, MySQL 에 부담이 없다(로컬 health 내내 UP).
- 서킷브레이커: 실패가 order 안에서 하류 호출 전에 나므로 order 의 restaurant, dispatch, payment 서킷은 열리지 않는다. 피해는 서킷이나 재시도로 줄지 않는다(모든 주문 500).
- success: `order_create_5xx_rate ≥ 0.5` 와 `order_create_2xx_rate < 0.2`, 3틱. must_rule_out: `achieved_rps < 0.5`, food MySQL 파드 NotReady. abort: `entry_status == 0`(order 는 Ready 라 500 이 남는다). recovery: target_health 200, MySQL Ready, 기준선 2xx ≥ 0.7.
- cleanup: 매니페스트 이미지로 되돌리고 Deployment available, 2.5.0 을 쓰는 파드가 없어지면 tb-w3 containerd 에서 이름과 ID 참조 삭제. 장애 동안 쓴 데이터가 없다.
- 결함 이미지 규칙: 앱 소스와 매니페스트, 기본 이미지 그대로. 패치는 `food-delivery/order-service/src/` 안만(테스트가 확인). 태그 2.5.0 은 저장소 어디에도 없다(order 의 다른 릴리스는 F49-H 2.3.0). 추가 문자열에 시나리오 id, fault, bug, chaos, scenario 없음(테스트가 확인). 빌드는 `bash scripts/scenarios/fault-images/build.sh f58-r`.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(ServiceChannel 2025-05-08, 공식 상태 페이지 사후 보고, 링크)와 요소별 대응표가 있고 기전이 같다(§2)
- [x] 분류 장부를 갱신했고 묶음 J+G(J 11/76, 14.5%), 정답 위치 주문 서비스(8/76, 10.5%), 결제 경로 12/76(15.8%), 서비스 food 24(가장 적음)와 고른 이유를 적었다. 어느 축도 20% 에 닿지 않는다(§3)
- [x] 근본 원인 위치가 패치 줄과 운영 설정 파일 줄로 확인됐다(§4)
- [x] 근본 원인 흔적이 119 에서 조회됐다: kcm_events_local 의 order 롤아웃과 Pulled 태그, lucida_logs_local 의 Tomcat SEVERE 수집(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트와 전수 로그(§7)
- [x] 계기의 흔적이 조회됐다: 롤아웃 이벤트와 이미지 태그. 인공 지연 없음
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(롤아웃 + 'Required key' 로그)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 이미지 태그, 패치 문자열, 실행기 argv 에 없음(테스트)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다(§9, 로컬 실측 200/200 500)

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

숫자: food 23(가장 적음) → banking 25 → commerce 27(정답 위치 0 부품만). 묶음 J 10, P 10(각 13%)이 가장 많고 금지 없음. 정답 위치 상한 근처 없음, 결제 경로 16%.

| 순위 | 후보(원본 / 부품 / 기전) | 결과 |
|---|---|---|
| 1 | ServiceChannel 2025-05-08 / food order / 운영에 동기화되지 않은 설정을 읽는 새 릴리스 | **채택** |
| 2 | ServiceChannel 2025-05-08 / banking api / 같은 기전 | 보류: food 가 더 적고 같은 원본을 한 번에 둘 낼 이유 없음 |
| 3 | GitHub 2021-10-08 / banking transfer / 이체 응답 구조 변경 릴리스(account 와 commerce payment 해석 실패, 돈은 옮겨짐) | 보류: 같은 원본이 이미 F49-R, F49-P 둘. 다음 후보로 남김 |
| 4 | Octopus Deploy 2025-11-25 / banking transfer / DB 연결 누수 릴리스 | 버림: 같은 원본, 같은 엔진(Oracle)의 F51-H 와 서비스만 다른 복제(카탈로그 §1) |
| 5 | Honeycomb 2019-11-06 / banking api / 요청 키 메모리 누수 | 버림: F41-R 과 같은 꼴, 서비스만 다름(카탈로그 §1) |
| 6 | University of Alaska 2025-07-21 / banking Oracle / 아카이브 저장소 마운트 실패로 ORA-00257 | 버림: Oracle Free 가 NOARCHIVELOG 라 재현하려면 재기동과 모드 변경이 필요(원칙 9, cleanup) |
| 7 | GitLab production#18796 / food dispatches 또는 payments / NOT VALID 외래 키가 새 쓰기에 걸림 | 버림: 부모 행이 커밋 전인 서비스 간 트랜잭션이라 FK 위반이 아니라 잠금 대기가 되어 기전이 바뀜(원칙 1), order 풀이 묶여 입구 0 위험 |
| 8 | Val.town 2024-12-31 / food / 교착 + 질의 시간 제한 없음으로 풀 고갈 | 버림: 계기(동시 편집 교착)와 결함(시간 제한 없음) 복합, 묶음 D(원칙 1) |
| 9 | (사례 없음) / banking ACCOUNTS commerce-settlement / 정산 계좌 잔액 고갈로 이체 FAILED 침묵 | 버림: 공식 사후 보고 없음(웹 검색 1회), 조용한 장애(원칙 1, 7) |
| 10 | (사례 없음) / banking Oracle / 다른 환경으로 착각한 RESTRICTED SESSION 정비 모드 | 버림: 공식 사후 보고 없음(웹 검색 1회)(원칙 1) |
| 11 | Facebook 2010-09-23 / food restaurant / 잘못된 설정값이면 DB 를 다시 읽는 되먹임 | 버림: 테스트베드에 그런 폴백 경로가 없어 패치로 넣으면 결함이 둘(원칙 1) |
| 12 | GitHub 2025-09-15 / banking account / 리미터가 모든 요청을 403 | 버림: api 가 4xx 를 그대로 넘겨 4xx 단일 신호(F34-R 교훈), nginx 판은 이미 rejected |
