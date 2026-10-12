---
title: F16-P 설계 시트 (commerce notification 릴리스가 회원 목록 전체를 이벤트마다 다시 읽어 인가를 맡은 user-service 를 묻고 쓰기 실패)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - release
  - auth
summary: commerce notification-service 를 주문 이벤트 수신자를 회원 목록에서 찾는 릴리스 1.3.0 으로 롤아웃하자, 목록에 없는 이름마다 뒤에서 user-service 회원 목록 전체를 페이지 상한 100번까지 다시 읽어 게이트웨이가 쓰기마다 토큰을 확인하는 user-service 가 CPU 한도와 풀 대기에 묻히고, 장바구니, 주문, checkout 쓰기가 500 으로 실패하는 시나리오. 원본은 Cloudflare 2025-09-12(대시보드 릴리스의 버그가 인가가 기대는 Tenant Service API 호출을 크게 늘려 과부하, API 5xx).
---

# F16-P 설계 시트

## 1. 요약

commerce notification-service 는 주문, 결제 이벤트를 Kafka 에서 받아 알림을 보내는 비동기 소비자다. 어느 사용자 요청 경로에도 없다. 주문 이벤트에는 고객 이메일이 비어 있어(119 로그가 주문마다 `Sending notification to null`) 새 릴리스 1.3.0 은 고객 이름으로 회원 목록에서 이메일을 찾는다. 회원 목록은 user-service `GET /api/users?page=N&size=100` 을 짧은 페이지가 나올 때까지 읽어 메모리에 두고, 목록에 없는 이름이 오면 소비를 늦추지 않으려고 뒤에서(`CompletableFuture.runAsync`) 다시 만든다.

그런데 user-service 회원 목록 API 는 페이지 인자를 받지 않고 매번 3,020명 전체(약 400KB)를 돌려준다. 짧은 페이지가 오지 않으니 새로 고침 한 번이 상한 100번까지 전체 목록을 받는다. 주문 이벤트의 고객 이름(`user-1` … `user-20`, 부하 발생기의 데모 사용자)은 회원 표의 이름이 아니어서 주문마다 새로 고침이 하나 더 뜬다. user-service 는 api-gateway 가 쓰기(POST/PUT/DELETE, 로그인과 가입 제외)마다 토큰을 확인하는 곳이다. user 가 CPU 한도(500m)와 Hikari 풀(10) 대기에 묻히자 토큰 확인이 초 단위로 늦어지고 500 으로 실패하며, readiness 가 빠지는 동안은 연결 거부로 곧 500 이다. 게이트웨이의 토큰 확인은 같은 클래스 안의 자기 호출(AuthGuard.java:44)이라 재시도, 서킷, 401 폴백 없이 user 의 실패를 그대로 500 으로 돌려준다. 상품 둘러보기는 그대로라 사이트는 떠 보이지만 아무도 살 수 없다.

비유: 건물 출입 카드 확인 창구(user)는 한 사람이 지킨다. 우편실(notification)이 새 규칙을 들였다: "수신자 주소를 모르면 입주자 명부를 처음부터 끝까지 다시 받아 와라". 명부 담당은 쪽수를 무시하고 매번 명부 전체를 건네는데 우편실은 "다음 쪽"을 백 번 달라고 한다. 그리고 우편물의 수신자 이름은 명부에 없는 별명이라 편지가 올 때마다 새 직원이 명부를 받으러 창구에 줄을 선다. 출입하려는 손님(쓰기 요청)은 그 줄 뒤에서 기다리다 돌아간다. 우편실은 손님과 마주친 적이 없어서, 손님 쪽에서 아무리 거슬러 올라가도 창구까지만 보인다.

## 2. 원본 사례

- 기업: Cloudflare
- 날짜: 2025-09-12 (16:32 UTC 대시보드 배포, 17:57 UTC 장애 시작, 19:12 UTC 대시보드 회복)
- 링크: [공식 블로그 사후 보고](https://blog.cloudflare.com/deep-dive-into-cloudflares-sept-12-dashboard-and-api-outage/) "Deep dive into Cloudflare's Sept 12 dashboard and API outage"
- 요약(원문 인용 중심): 대시보드 새 버전에 "a bug that will trigger many more calls to the /organizations endpoint, including retries in the event of failure" 가 있었다. React `useEffect` 의 의존성 배열에 렌더마다 새로 만들어지는 객체가 들어가 한 번 렌더에 호출이 여러 번 실행됐다. Tenant Service 는 "part of API request authorization": "Without Tenant Service, API request authorization can not be evaluated", 인가 평가가 실패하면 API 요청이 5xx 를 돌려준다. 17:57 Tenant Service 가 과부하되어 대시보드와 많은 API 가 실패했고, 자원 추가, Tenant Service 로 가는 트래픽 레이트 리밋, 문제 변경 되돌림, 대시보드 핫픽스로 복구했다. 탐지는 자동 경보였으나 재시도와 새 요청을 구분하기 어려워 대시보드 반복 호출을 찾는 데 시간이 걸렸다.
- `docs/ref-real-world-incidents.md` M2 에 이미 있다(F47-R 이 넣음, 인가 의존 문장까지 포함). 이번에 고칠 것이 없다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 대시보드(Tenant Service 의 호출자) 새 버전 배포 | notification(user-service 의 새 호출자) 릴리스 1.3.0 롤아웃 |
| 원인이 된 결함 | 의존성 배열 객체가 렌더마다 새로 만들어져 같은 API 호출이 되풀이됨, 실패하면 재시도 | 목록에 없는 이름마다 겹침 방지 없는 뒤쪽 새로 고침, 페이지 인자를 무시하는 API 에 짧은 페이지를 기다리며 상한 100번까지 전체 목록 요청, 실패한 페이지는 3번까지 다시 |
| 전파 경로 | 호출 폭증 → Tenant Service 과부하 → 인가 평가 실패 → API 요청 5xx | 전체 목록 요청 폭증 → user CPU 한도와 Hikari 대기 → 게이트웨이 쓰기마다 하는 토큰 확인 지연, 500 → readiness 이탈 동안 연결 거부. 토큰 확인이 AuthGuard 안의 자기 호출이라 재시도, 서킷, 폴백 없이 둘 다 게이트웨이 500 |
| 사용자 증상 | 대시보드와 많은 API 를 쓸 수 없거나 일부만 됨, 캐시된 CDN 은 정상 | 장바구니, 주문, checkout 쓰기가 수 초 걸리거나 500(원본의 '인가 평가 실패 → 5xx' 와 같음), 로그인은 게이트웨이 user 프록시 서킷이 열린 동안 502, 상품 둘러보기는 정상 |
| 원본의 탐지 경로 | 자동 경보, API 사용량 급증(재시도와 새 요청 구분이 어려움) | lucida 이상 탐지(user 오류 로그, 게이트웨이 지연과 오류율, KCM user Unhealthy), 인시던트. 원인은 user 로 새 트래픽을 보내는 쪽을 찾아야 나온다 |
| 완화와 복구 | 자원 추가, 레이트 리밋, 변경 되돌림, 핫픽스 | cleanup 이 notification 이미지를 매니페스트 이미지로 되돌림(롤백). 옛 프로세스와 함께 새로 고침 스레드가 사라지고 user 는 스스로 돌아온다 |

기전 확인: 호출자 새 버전의 결함이 내부 API 호출을 크게 늘리고(재시도 포함), 그 API 를 맡은 서비스가 과부하되며, 그 서비스가 인가 판단의 일부라 그것에 기대는 사용자 요청 전반이 실패한다. 원인에서 증상까지 고리가 원본과 같다. 원본처럼 과부하된 서비스(user)와 인가에 기대는 쪽(게이트웨이)은 바뀌지 않았다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 99, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | commerce 33, core-banking 33, food-delivery 33 | 세 도메인 동률이라 모두가 최다다. 원칙 2 에 따라 정답 위치가 0 인 부품만 정답으로 낼 수 있다 |
| 묶음(20% = 19.8) | J 16, D 14, P 13, G 12, L 8, A 7, B 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0 | 주 묶음 I(인증 의존 실패: 인증을 맡은 서비스 실패가 다른 기능의 실패로 번짐) 1 → 2(2%). 보조 J 로 세도 16 → 17(17%), 상한 아래 |
| 정답 위치 | 알림 서비스 0. 최다는 주문 서비스 8 | 알림 서비스 0 → 1. §2-1 표에 이미 있는 말이다 |
| 결제 경로 합계 | 13(13.1%) | 이 후보와 무관(13/100) |
| 부품 지도 | commerce: notification 0, kafka 0, nginx 0 | 정답 부품 notification 이 0 |
| 장부와 catalog 대조 | 어긋남 없음 | 장부 정정 없음 |

왜 이 후보인가: 세 도메인 동률이라 정답 위치 0 부품이 필요했고, 0 인 부품 가운데 notification, Kafka 는 지금까지 "비동기 소비자라 사용자 증상이 없다"(막힌 목록 27, 98, 146, 153, 188, 258행)는 벽에 막혔다. 이 후보는 비동기 소비자의 결함이 공유 의존(인가 서비스)을 통해 사용자 경로를 깨는 실제 고리라 그 벽을 넘는다. 묶음 I 는 현실 4%(Ghosh)인데 우리는 1 이다. 관제 AI 에게는 "실패한 요청을 따라가면 user 까지만 닿고, 원인은 user 로 새 트래픽을 보낸 쪽"이라 추론 없이 찍을 수 없는 짝이다(F16-H, F66-R 과 같은 증상).

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순, 버린 이유 포함)

| # | 기전 × 부품 | 원본 사례 | 결과 |
|---|---|---|---|
| 1 | M2 결함 릴리스가 내부 API 호출을 폭증 × commerce notification → user-service(인가) | Cloudflare 2025-09-12 공식 | **채택**. 정답 위치 알림 서비스 0, 묶음 I 1 |
| 2 | 같은 기전 × food notify → dispatch 배달 목록 API | Cloudflare 2025-09-12 공식 | 남김(순위 아래): 같은 0 위치(알림 서비스)를 쓰는데 묶음이 E(4)로 I(1)보다 많고, 증상 'dispatch 목록 포화로 주문 503' 이 이미 F33-R, F33-H, F59-R 셋이다 |
| 3 | M21 인덱스 제거 × banking Oracle OUTBOX_EVENTS(이체 이벤트 outbox 0) | Chargebee 2018-03-02 공식 | 버림(원칙 7, 9): idx_outbox_unpublished 를 쓰는 것은 outbox 릴레이 폴링뿐이라 사용자 경로 밖이고, 막힌 목록 15행(원장 인덱스)과 같은 벽 |
| 4 | M21 잠금 대기열 × commerce payment_schema.outbox_events(결제 이벤트 outbox 0) | Honeycomb 2024-08-06 공식 | 남김(순위 아래): 묶음 D 14 로 높고 F66-P 와 같은 수단(표만 다름) |
| 5 | M17 시간 어긋남 × tb-w1 노드 시계(새 위치) | Cloudflare 2017-01-01 공식 | 버림(원칙 1, 3): 원본은 윤초로 시간 차가 음수가 되어 코드가 panic 한 고리인데 testbed Java 코드에 그런 경로가 없고, 노드 시계를 옮기면 그 노드 파드의 로그와 지표 시각이 함께 옮겨져 119 녹화 구간이 깨진다 |
| 6 | M9, M10 × 세 도메인 Kafka 브로커(메시지 브로커 0) | 막힌 목록 원본들 | 버림(원칙 7): 발행이 모두 outbox 비동기(막힌 목록 27, 146, 188, 258행) |
| 7 | M16 resolver 설정 × 클러스터 DNS(DNS 0) | Intercom 2024-11-05 공식 | 버림(카탈로그 §1, 원칙 3·4): 파드 DNS 설정 꼴은 F37-R, CoreDNS 쪽은 kube-system 미수집 |
| 8 | M22 운영 명령 범위 착오 × food menus.available 일괄 갱신(메뉴 0) | Atlassian 2022-04-05 공식 | 버림(원칙 7): 주문이 400 'Menu sold out' 4xx 단일 신호(F34-R 교훈) |
| 9 | M21 유일 제약 제거 × commerce product_variants.sku(상품 옵션 0) | Chargebee 2018-03-02 공식 | 버림(원칙 9): 표가 작고 checkout 경로가 아님(막힌 목록 Hello Retail 행) |

부품 8종(commerce notification, food notify, banking Oracle outbox 표, commerce PostgreSQL outbox 표, 노드 시계, Kafka, 클러스터 DNS, food MySQL 메뉴 표, commerce 상품 옵션 표), 앱과 인프라, DB 를 함께 냈다. 지휘 세션이 넘긴 막힌 후보와 같은 원본·같은 주입은 없다(Cloudflare 2025-09-12 는 F47-R 이 banking api → account 로 썼다. 같은 원본의 다른 대상이고, 묶인 곳이 원본처럼 인가 판단을 맡은 서비스이며, 호출자가 실패한 요청 경로 위에 없다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 commerce-notification:1.3.0 을 tb-w1 containerd 에 올리고 `kubectl set image deploy testbed-notification notification-service=commerce-notification:1.3.0`. 노드는 Deployment 의 nodeSelector 에서 읽는다(F43-R 과 같은 스크립트, 계약만 더함).
2. 배포: `commerce/k8s/24-notification-service.yaml:9`(replicas 1), `:18`(nodeSelector tb-w1), `:27`(image, imagePullPolicy Never), `:31-33`(envFrom service-config → `USER_SERVICE_URL: http://testbed-user:8085`, `commerce/k8s/02-configmaps.yaml:29`), `:93-95`(limits cpu 500m, memory 1Gi). 전략은 기본값(maxSurge 25%)이라 새 파드가 Ready 가 된 뒤 옛 파드가 내려간다(109 kubectl 2026-10-11: testbed-notification 1/1, 이미지 commerce-notification:latest, tb-w1 이미지 파일시스템 55.6%).
3. 결함: `scripts/scenarios/fault-images/f16-p/notification-service.patch:72-76`(OrderEventConsumer — 이벤트 이메일이 비면 `recipientDirectory.findEmail(customerName)`), `:123-131`(RecipientDirectory.findEmail — 못 찾으면 `CompletableFuture.runAsync(this::refresh)`, 겹침 방지 없음), `:142-157`(load — PAGE_SIZE 100, MAX_PAGES 100, `while (chunk.size() >= PAGE_SIZE && page < MAX_PAGES)`), `:160-171`(fetchPage — 실패한 페이지 3번까지), `:34-40`(UserDirectoryClient.listUsers — `GET /api/users?page={page}&size={size}`, 읽기 10초). 매니페스트 버전은 `commerce/notification-service/src/main/java/com/commerce/notification/consumer/OrderEventConsumer.java:24-38`(받은 그대로 알림).
4. 스레드: notification 컨테이너는 CPU 하나라 JVM availableProcessors 가 1 이다(119 VM `apm.agent.otel.java.jvm.cpu.count{service_name="commerce-notification"}` 1). ForkJoinPool 공용 풀 병렬도가 1 이면 `CompletableFuture.runAsync` 는 작업마다 새 스레드를 만든다(F47-R 시트 §4 와 같은 JDK 동작, 로컬에서 스레드 이름 Thread-N 으로 확인).
5. 피해 쪽 1(목록): `commerce/user-service/src/main/java/com/commerce/user/controller/UserController.java:35-40`(searchUsers — 페이지 인자 없음), `commerce/user-service/src/main/java/com/commerce/user/service/UserService.java:102-113`(조건이 없으면 `userRepository.findAll()`, readOnly 트랜잭션 안에서 3,020개 엔티티를 응답으로 바꿈). 109 commerce PostgreSQL 읽기 전용 조회 2026-10-11: user_schema.users 3,020행. 로컬 실측: 한 번 응답 406,425바이트, 데운 뒤 0.5 CPU 에서 약 0.19~0.3초(CPU 약 35ms 꼴, 아래).
6. 피해 쪽 2(인가): `commerce/api-gateway/src/main/java/com/commerce/gateway/service/AuthGuard.java:31-48`(쓰기 메서드마다 토큰 확인, 로그인과 가입 면제). verifyIfWrite 가 같은 클래스의 verifyToken 을 직접 부르므로(`:44`) 자기 호출이고, `:51-58` 의 @CircuitBreaker(user) + @Retry(user) 와 `:60-63` 의 401 폴백은 적용되지 않는다(`GatewayProxyController.java:28-71` 은 verifyIfWrite 만 부름, F33-P 정답지 'AuthGuard 자기 호출'과 같음). 그래서 user 의 500 은 게이트웨이 SEVERE 'Servlet.service() ... HttpServerErrorException$InternalServerError: 500' 와 500, 연결 거부는 'ResourceAccessException: I/O error on POST' 와 500, 응답이 `application.yml:18-21` 의 읽기 10초를 넘어도 500 이다(119: 게이트웨이 30일 로그에 'Token verification unavailable' 0건, F33-P 실행 구간의 쓰기 실패는 이 두 문장 442건, 266건). 게이트웨이 user 서킷(`:51-57`)과 재시도(`:102-106`)는 로그인 프록시 `ProxyService.java:57-61`(forwardToUser, 폴백 502 'Downstream unavailable')에만 걸린다. user `application.yml:12-15`(Hikari 10, connection-timeout 3000), `commerce/k8s/25-user-service.yaml:90-103`(readiness `/actuator/health` 3초 x3, liveness 15초 x5), `:108-110`(limits cpu 500m). user 의 `/actuator/health` 에는 DB 확인이 들어 있다(F66-R 시트와 같음).
7. 영향 밖: 상품, 카테고리 조회(GET, 인가 없음, product-service), Redis, order 와 하류 서비스 자체. PostgreSQL 은 3,020행 전체 읽기가 늘 뿐이라 여유가 있다.

### 로컬 실측 (2026-10-11, 원칙 9 근거)

origin/main(3ae27f7) commerce 에 패치를 얹어 `./mvnw -o -pl commerce-common,user-service,notification-service,api-gateway -am package` 로 jar 를 만들고, eclipse-temurin:17-jre 컨테이너에 OTel 자바 에이전트(내보내기 끔)를 붙여 띄웠다. user, notification, gateway 는 `--cpus 0.5 -m 1g`(운영 한도와 같음), PostgreSQL 16-alpine(`--cpus 0.5 -m 512m`)에 `commerce/db/init-schemas.sql` 과 109 의 회원 3,020명(읽기 전용 `\copy` 로 복사), Kafka 3.8.1. 주문 이벤트는 운영과 같은 모양(`customerName` user-N, `customerEmail` null)으로 정해진 속도로 넣고, 게이트웨이 쓰기(POST /api/carts/1/items, 토큰 있음)를 초당 2~3건, user `/actuator/health` 를 5초마다(3초 제한) 보냈다. cart 는 띄우지 않아 토큰 확인을 통과한 쓰기는 502, 확인에서 실패하면 500 이다(토큰 확인이 자기 호출이라 401 폴백이 없음).

| 판 | 이벤트 속도 | 결과 |
|---|---|---|
| 매니페스트(notification 없음) | - | 쓰기 전부 토큰 확인 통과(502), p50 0.62초, health 0.01초 |
| 릴리스(재시도 없는 초판) | 0.25/s, 8분 | user CPU 1분 안에 한도(0.5), 쓰기 p50 0.6 → 2.2초, 500 가끔, health 3초 초과 가끔. 실패한 페이지에서 새로 고침이 끝나 홍수가 스스로 줄었다(새로 고침 66번 완주, 41번 실패) |
| 릴리스(채택판: 페이지 3번까지 다시) | 0.25/s, 8분 | user CPU 1분 안에 한도, 쓰기 p50 0.6 → 3.5초, 4분 뒤부터 15초 창마다 쓰기 3~43% 가 500, health 3초 초과가 절반 넘게 |
| 릴리스(채택판) | 0.5/s, 7분 | 2분 뒤부터 쓰기 p50 3.4~3.8초, 15초 창마다 20~40% 500, health 거의 매번 3초 초과, notification 스레드 39 → 122 |
| 릴리스(채택판) | 1/s, 7분 | 45초 뒤부터 쓰기 p50 3.6~3.8초, 15초 창마다 18~47% 500, health 거의 매번 3초 초과, user 로그 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=10, active=10, idle=0, waiting=65~73)', notification WARN 'Recipient directory paging stopped at page limit: pages=100 recipients=110' 과 'Recipient directory refresh failed: 500 ...' 가 이어짐 |
| 롤백(매니페스트 notification 으로 교체) | 1/s, 3분 | 옛 notification 컨테이너를 지우고 매니페스트 jar 로 띄우자 첫 15초 창부터 쓰기 전부 토큰 확인 통과(502), p50 0.61초, health 0.00~0.06초. 홍수는 옛 프로세스와 함께 끝났다 |

### kubelet 프로브를 흉내 낸 실측 (2026-10-12, 원칙 9 근거, 1차 평가 반영)

위 실측은 user health 가 3초를 넘어도 아무 일이 없었다. 운영에서는 readiness 가 user 를 엔드포인트에서 빼고, 그러면 새로 고침 스레드가 연결 거부로 끝나 홍수가 끊긴다(평가 모의의 지적). 그래서 같은 jar 로 다음을 흉내 내 다시 쟀다.

- user 앞에 Service 역할의 TCP 프록시를 두고 게이트웨이와 notification 이 그것으로 붙는다. 호스트의 kubelet 흉내가 `25-user-service.yaml:90-103` 그대로 readiness(10초마다 `/actuator/health`, 3초 제한, 3번 연속 실패면 프록시가 새 연결을 거부하고 열린 연결을 끊음, 1번 성공이면 되돌림)와 liveness(15초, 3초, 5번 연속 실패면 user 컨테이너 재시작, 그 뒤 150초는 startupProbe 몫으로 liveness 멈춤)를 돈다.
- 게이트웨이 뒤의 cart, order, product 는 곧바로 200 을 주는 대역(stub)으로 두어 토큰 확인만 지연과 실패를 낸다. 부하는 checkout 여정(장바구니 비우기, 담기, checkout: 토큰 확인 쓰기 셋)을 초당 2건, 읽기(GET /api/products)를 초당 3건 열린 고리로 보낸다. 토큰 확인을 통과한 checkout 의 절반이 주문 이벤트가 된다(119: checkout 약 0.47/s 에 이벤트 0.23/s). 처음 3분은 매니페스트(notification 없음), 그 뒤 릴리스 notification 을 띄웠다.
- 러너처럼 15초 틱마다 checkout 5xx 비율(30초 창)과 게이트웨이 p95(10초 묶음 p95 의 60초 창 최대)를 다시 계산했다.

| 실행 | readiness, liveness | 게이트웨이 p95 ≥ 1000ms | checkout 5xx ≥ 0.3 | 그 밖 |
|---|---|---|---|---|
| 1(13분 중 앞 11.5분 사용) | 고장 150초째 liveness 재시작, 180초째 NotReady, 220초 Ready, 338초째 다시 재시작, 350~410초 NotReady | 고장 75초째부터. 재시작 구간마다 끊김(재시작 중엔 연결 거부로 쓰기가 빠름), 가장 긴 연속 17틱, min_hold(8분)~11.5분에 3틱 연속 성립 | 고장 2분 뒤 틱의 약 60%, 가장 긴 연속 11틱 | checkout 5xx 0.57, 쓰기 p50 0.2초, p95 3.6초, 읽기 실패 0/1,733. 11.5분 뒤는 흉내 도구의 프록시 제어가 멈춰 버림 |
| 2(15분) | 고장 120, 200, 270, 360, 440, 600, 680, 780, 860초째 NotReady, 각 10초 뒤 Ready(주기 70~160초), 재시작 없음 | 고장 90초째부터 끝까지 2.1~3.9초, 54틱 연속 | 0.3 이상 틱 40%, 가장 긴 연속 5틱(30초 창 값 0~0.51) | checkout 5xx 0.25, 쓰기 p50 1.0초, p95 3.4초, 평시 쓰기 p95 0.02초, 평시 게이트웨이 p95 최대 86ms, 읽기 실패 0/2,240 |

notification 로그(실행 2): 'Recipient directory refresh failed: I/O error on GET request for "http://f16p-userproxy:8085/api/users": Connection refused' 592, 'Recipient directory refresh failed: 500 ...' 62, 'Recipient directory paging stopped at page limit: pages=100 recipients=110' 9. 운영에서는 주소가 http://testbed-user:8085 다. 평가 모의대로 주기마다 스레드가 연결 거부로 한꺼번에 끝나고 홍수가 다시 쌓인다. 그 사이 쓰기는 대부분 느린 성공(1~4초)이라 checkout 5xx 는 주기를 타고 오르내리지만, 게이트웨이 p95 는 주기를 건너 높게 남는다. 그래서 success 를 게이트웨이 p95 로 바꿨다(§9).

운영과의 차이: 운영은 JDK 17 aarch64(tb-w1)이고 에이전트가 119 로 스팬, 지표, 로그를 내보내 요청당 CPU 가 같거나 크다. 흉내 프록시는 이탈 때 열린 연결까지 끊는데, 운영의 kube-proxy 는 새 연결만 거부하고 열린 keep-alive 연결은 남긴다. 운영에서는 이탈 동안에도 일부 스레드가 user 를 계속 부르므로 홍수가 덜 끊기고 지연은 더 오래 남을 것이다(판정 신호에는 유리한 쪽). 운영 게이트웨이 p95 에는 기준선과 동반 부하의 읽기, 토큰 없는 장바구니 담기(빠른 401), 로그인이 섞인다. 동반 부하 3rps 에서 토큰 확인 쓰기는 게이트웨이 요청의 약 3분의 1(checkout 여정 초당 1.5건 x 쓰기 3)이다. 그 가운데 6분의 1 이상이 1초를 넘으면 p95 가 1초를 넘는데, 흉내 실측에서 쓰기 중앙값이 1초 안팎이었다.


## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-notification`(notification-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 사용자, loadgen 요청과 게이트웨이의 토큰 확인은 평시 그대로 정당하다(원칙 6).
- 부분 점수: `commerce-user`, `testbed-user`(묶인 증상 위치). user 의 목록 API 가 페이지를 받지 않고 용량에 여유가 없던 것도 원본 사후 보고가 Tenant Service 용량을 함께 든 것처럼 요인이지만, 직접 계기와 고칠 곳은 호출자의 새 버전이라 부분 점수로 둔다. 게이트웨이(fail-close 규칙대로 동작)는 부분 점수에도 넣지 않는다.
- 층위: 원본 사후 보고의 결론(대시보드 버그가 직접 계기, 되돌림과 핫픽스)과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "notification 을 1.3.0 으로 롤아웃한 직후부터 user 로 조건 없는 회원 전체 조회가 쏟아졌고(DPM, 트레이스, notification 로그) 그 요청은 게이트웨이가 아니라 notification 에서 온다. user 는 바뀐 것이 없고 CPU 한도에 붙었다 → notification 새 버전의 요청 폭증, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | user 오류 로그(Hikari 'Connection is not available', Servlet 예외, 평시 0), 게이트웨이 지연(p95 평시 9ms → 초 단위)과 오류율, KCM user Unhealthy, user JVM CPU 이상 |
| 피해 판정(러너) | 게이트웨이 p95(60초 창 최대) ≥ 1000ms 가 3틱(평시 7일 99분위 58ms). 기록: checkout 5xx·비정상·2xx 비율, user 파드 Ready |
| RCA | notification 롤아웃 이벤트와 새 이미지 태그, 그 뒤 notification 'Recipient directory refresh failed: ... Connection refused' 와 '... 500 ...' 로그, DPM 의 조건 없는 회원 조회, user CPU 한도와 p95 초 단위, user 로 오는 게이트웨이 요청은 그대로, notification 스레드 증가, user 는 바뀐 것이 없음 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 22:00~23:30 UTC)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-commerce' AND object_name LIKE 'testbed-notification%'` 30일 reason 별 | ScalingReplicaSet 88, Pulled 'Container image "commerce-notification:latest" already present on machine'(태그가 본문에 담김), Killing, SuccessfulCreate/Delete, Unhealthy(startup). 마지막 2026-10-01 20:17(notification 재배포가 수집됨) |
| 근본: 새 코드가 남기는 로그 | CH `lucida_logs_local`(service_name, body) | commerce-notification 7일 전체, 'Received order event%', 'Sending notification to null%', 'Recipient directory%' | 1,445,416 / 289,082 / 289,082 / 0. 주문 이벤트마다 이메일이 비어 있음이 전수 로그로 보이고, 새 문장은 평시 0건이다. 고장 때 대표 문장은 'Recipient directory refresh failed: I/O error on GET request for "http://testbed-user:8085/api/users": Connection refused'(user 이탈 때마다 스레드 수십 개가 한꺼번에)와 'Recipient directory refresh failed: 500 ...'이고, 'Recipient directory paging stopped at page limit' 은 드물다(흉내 실측 15분에 592, 62, 9건) |
| 근본: 이벤트 속도 | CH `lucida_logs_local` | commerce-notification 'Received order event%' 10분 단위, 2시간 | 10분당 88~155건(초당 약 0.15~0.26건). 주문 이벤트마다 새로 고침 하나 |
| 근본: 회원 전체 조회(DB 쪽) | CH `dpm_topsql_local`(engine postgresql, body 의 sqlId, calls, sqlText) | 7일 sqlText 에 user_schema.users | 이메일 조회(sqlId 8716608724904561152) 319행 2,551회, id 조회 4행 4,780회, count 1행. 조건 없는 'select u1_0.id,u1_0.created_at,... from user_schema.users u1_0' 는 0행(평시 없음). 고장 때 새 문장으로 상위에 오른다(DPM Top SQL 은 PostgreSQL 누적 통계의 차분이라 표본이 아님) |
| 근본: user 로 새 요청 | CH `otel_traces_local`(service_name, span_name, span_kind) | commerce-user 7일 SERVER 스팬, commerce-notification 7일 스팬 | user SERVER: verify-token 50,947, health 4,956, login 3,464, 그 밖 0('GET /api/users' 없음). notification: 'commerce.payments process' 13,873, 'commerce.orders process' 13,711, health 5,006, CLIENT 0. 고장 때 notification CLIENT 'GET' 과 user SERVER 'GET /api/users' 가 생긴다(표본 10%, 보조 증거) |
| 피해: user CPU | VM `apm.agent.otel.java.jvm.cpu.recent_utilization{service_name="commerce-user"}`, `jvm.cpu.count` | 7일 중앙값, 최대 | 중앙값 0.023, 최대 1(다른 시나리오 실행 때). cpu.count 1(notification 도 1). 에이전트가 10초마다 내보내는 런타임 지표라 트레이스 표본과 무관 |
| 피해: user 지연 | VM `apm.agent.otel.java.percentile95{service_name="commerce-user"}` | 7일 중앙값, 99분위, 최대 | 2.33ms, 7.73ms, 11,873ms(F33-P 등 실행 때) |
| 피해: 게이트웨이 지연 | VM `apm.agent.otel.java.percentile95{service_name="commerce-gateway"}` | 7일 중앙값, 95·99분위 | 9.37ms, 49.7ms, 57.8ms. 러너 기록 관측 |
| 전파: user 고장 로그 | CH `lucida_logs_local` | commerce-user 7일 '%Connection is not available%' 시간별 | 661건, 전부 2026-10-11 10시(F33-P 실행). 평시 0 |
| 전파: user readiness | CH `kcm_events_local` | `object_name LIKE 'testbed-user%'` | Unhealthy 42건(마지막 2026-10-11 10:11, F33-P 실행). user 프로브 실패가 수집된다 |
| 감별: 게이트웨이 쓰기 실패 문장 | CH `lucida_logs_local`(service_name, body) | commerce-gateway 30일 'Token verification unavailable', 'HttpServerErrorException$InternalServerError: 500', 'ResourceAccessException: I/O error on POST', 'Downstream unavailable for POST /api/users/login' 수, 그리고 F33-P 실행 구간(2026-10-11 09:50~10:30 UTC) | 30일: 0 / 442 / 716 / 574. F33-P 구간: 442 / 266. 토큰 확인 실패는 401 폴백 문장 없이 500 문장으로만 남고(자기 호출), 서킷 문장은 로그인 프록시에서만 나온다. F16-P 고장 때도 쓰기 실패는 이 두 500 문장, 로그인은 'Downstream unavailable' 502 로 남는다 |
| 참고: 회원 수와 이벤트 이름 | 109 commerce PostgreSQL(읽기 전용) | user_schema.users count, order_schema.orders 최근 30분 customer_name, customer_email | 3,020명(이름은 한글 이름, 같은 이름 31명씩), 주문 customer_name user-4 … user-19, customer_email 비어 있음(업무 데이터라 119 에는 없다. 설계 근거로만 씀) |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), notification 과 user 로그(전수), DPM Top SQL(누적 통계), JVM 런타임 지표(CPU)다.

## 8. 감별

- must_support: notification 롤아웃과 1.3.0 태그, notification 'Recipient directory refresh failed: ... Connection refused' 와 '... 500 ...' 로그(드물게 'paging stopped at page limit', 모두 평시 0), DPM 의 조건 없는 회원 조회, 트레이스의 notification → user 'GET /api/users', user CPU 한도와 p95 초 단위, user Hikari 오류와 Unhealthy, 게이트웨이 쓰기 500(SEVERE 'HttpServerErrorException$InternalServerError: 500', 'ResourceAccessException')과 읽기 정상, 게이트웨이 user 서킷 열림은 로그인 프록시에서만. user, 게이트웨이 롤아웃·설정 변경 없음, PostgreSQL Ready 이고 회원 표 잠금 대기 없음.
- must_rule_out: user 자체 고장(F16-H), 회원 표 잠금 대기열(F66-R), 토큰 표 문제(F33-P), 진입 트래픽 급증(F07-H), 게이트웨이 설정(F64-R).
- contrast_with: F16-H, F66-R, F47-R, F59-R, F07-H.
- 같은 증상의 짝을 가르는 관측 근거: F16-H 는 user 가 부하 없이 프로브만으로 빠지고(user CPU 평시, readiness 경로 변경), F66-R 은 PostgreSQL 의 잠금 대기 세션과 회원 표 DDL(user CPU 낮음), F16-P 는 notification 롤아웃 뒤 user CPU 포화와 user 로 오는 새 'GET /api/users' 다.
- 러너 success(게이트웨이 p95)는 user 가 묻히는 F16-H 계열이나 게이트웨이 지연이 나는 다른 commerce 시나리오(F66-R, F33-P, F64-R)에서도 선다. 녹화 검증이 롤아웃 대상(notification), notification 'Recipient directory refresh failed' 로그, user CPU 포화와 잠금 대기 없음으로 가른다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f16-p, 실행기 파라미터 = 이미지 태그). 피해는 주문 이벤트 수에 비례한다(이벤트 1건 = 전체 회원 목록 요청 최대 100건, 실패하면 페이지마다 3번까지).
- 부하 계산: 평시 주문 이벤트 초당 약 0.15~0.26건(119 로그). 동반 부하 load.north_south commerce surge.js 3rps(여정 50% checkout → 초당 약 1.5건, F43-R 과 같은 강도)가 성공하면 이벤트가 그만큼 는다. 포화된 user 의 전체 목록 처리량은 초당 약 14건이다(로컬 첫 판: 8분 동안 새로 고침 완주 66번 x 100 = 6,600건, 그동안 user CPU 가 내내 한도 0.5. 한 건에 CPU 약 35ms 꼴, 데운 단건은 0.5 CPU 에서 0.19~0.3초). 평시 이벤트만으로도 초당 15~26건 수요라 한도를 넘고, 동반 부하가 더하면 초당 백수십 건이다. 같은 부하에 매니페스트 notification 이면 user 는 토큰 확인(약 2ms)만 받는다.
- success: 게이트웨이 p95(60초 창 최대) ≥ 1000ms, 3틱. 평시 7일 중앙값 9.4ms, 99분위 58ms. user 가 묻히면 토큰 확인이 붙은 쓰기가 1~4초가 되고, readiness 이탈의 약 10초는 쓰기가 연결 거부로 빨라지지만 60초 창 최대라 주기를 건너 남는다(흉내 실측 실행 2: 고장 90초째부터 끝까지 54틱 연속, 실행 1: liveness 재시작 구간마다 끊기지만 min_hold 뒤에도 3틱 연속 성립). checkout 5xx 는 주기를 타고 오르내려(흉내 실측 0.3 이상 틱 40~60%, 가장 긴 연속 5~11틱) 판정에서 빼고 기록만 한다. 토큰 확인에 서킷, 폴백이 없어 실패는 401 이 아니라 500 이다. user 자신의 p95 는 러너 APM 허용 목록 밖이라 녹화로 본다.
- min_hold 8m, timeout 14m, max_injection_duration 20m: 새 notification 파드 기동 약 40초~1분(로컬 Started 25초 + 프로브), 동반 부하 ramp 2m, user 포화 1~2분(흉내 실측 고장 75~90초째 게이트웨이 p95 1초 넘음). 8분이면 readiness 이탈 주기 서너 번이 녹화 구간에 들어간다. 동반 부하 18분 15초(ramp 2m + hold 16m + ramp_down 15s)를 덮는다.
- must_rule_out: achieved_rps < 1, commerce PostgreSQL NotReady, 기준선 상품 목록 비정상 ≥ 0.1(읽기까지 깨지면 인증 경로 하나가 아니다).
- abort: entry_status == 0(2틱). 게이트웨이와 nginx 는 건드리지 않고, 쓰기는 user 응답(풀 대기 3초 뒤 500, 토큰 확인에 재시도가 없어 로컬 최대 약 5초) 또는 연결 거부(곧 500)로 답하며, 가장 늦어도 읽기 10초에서 끝난다(k6 제한 60초 안).
- recovery: target_health 200, user 파드 Ready, PostgreSQL Ready, 기준선 checkout 비정상 < 0.1(2틱), timeout 10m. notification 파드 Ready 는 러너 관측 허용 목록 밖이라 두지 않고 실행기 cleanup 이 available 을 확인한다.
- cleanup: 이미지 원복, available(180초), 1.3.0 을 쓰는 파드가 없어진 뒤 tb-w1 containerd 에서 1.3.0 의 이름 참조와 ID 참조를 모두 지우고 둘 다 없는지 확인. 옛 notification 프로세스가 내려가면 쌓인 새로 고침 스레드도 사라진다. 데이터 부작용 없음(새 코드는 읽기만 한다).
- 노드 디스크: tb-w1 이미지 파일시스템 55.6%(2026-10-11). 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 80% 미만을 요구한다. tb-w1 은 F43-R 실행(2026-10-11 20:22, 성공)으로 러너의 ssh 적재 경로가 확인됐다.
- notification 메모리: 로컬 최대 RSS 약 311MiB(스레드 125), 한도 1GiB 아래. 스레드가 더 쌓여 OOM 이 나더라도 notification 재시작은 새 코드가 만든 일이라 정답은 그대로다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Cloudflare 2025-09-12, 공식 블로그 링크)와 요소별 대응표, 기전 같음(호출자 릴리스의 호출 폭증 → 인가를 맡은 서비스 과부하 → 인가에 기대는 요청 실패) (§2)
- [x] 장부 갱신(§4-1, §6), 묶음 I 1 → 2(2%), J 로 세도 17%, 정답 위치 알림 서비스 0 → 1, 결제 경로 13/100(무관), 서비스 세 도메인 동률 33 이라 정답 위치 0 부품. 어느 축도 20% 에 닿지 않는다 (§3)
- [x] 근본 원인 위치: 패치 `notification-service.patch:72-76`, `:123-131`, `:142-157`, `:160-171`, `:34-40`, 매니페스트 `OrderEventConsumer.java:24-38`, `24-notification-service.yaml`, 피해 쪽 `UserController.java:35-40`, `UserService.java:102-113`, `AuthGuard.java:31-63`, `25-user-service.yaml` 각 줄 (§4)
- [x] 근본 원인의 흔적 조회: CH kcm_events_local Pulled 본문의 이미지 태그, CH lucida_logs_local notification 로그(새 문장 평시 0, 이메일 null 전수), CH dpm_topsql_local 회원 표 문장(조건 없는 조회 평시 0), CH otel_traces_local user 서버 스팬 종류, VM user jvm.cpu.recent_utilization·p95 (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: KCM 이벤트, 로그 전수, DPM 누적 통계, JVM 런타임 지표 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 지연은 user 의 실제 CPU 와 풀 포화에서 남) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "notification 롤아웃 직후 notification 에서 user 로 회원 전체 조회가 폭증, user 는 그대로이고 포화 → notification 새 버전, 롤백". 코드 줄 추론 불필요 (§5)
- [x] 정답지 세 칸(근본 commerce-notification, 계기 비움, 부분 commerce-user, testbed-user) (§5). class A(코드 결함). fault_pattern 은 P1~P8 어느 것도 '호출자의 요청 폭증'에 맞지 않아 생략(F47-R 과 같음)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치에 더한 줄(UserDirectoryClient, RecipientDirectory, findEmail, 'Recipient directory paging stopped at page limit', 'Recipient directory refresh failed', 'Loaded recipient directory')에 id, fault, bug, chaos, scenario, flood 없음(테스트로 확인), 태그 1.3.0, 실행기 인자와 상태 파일에 id 없음
- [x] 피해 계산: 평시 이벤트만으로 전체 목록 수요 초당 15~26건 > 포화 user 처리량 약 14건, 동반 부하로 백수십 건. kubelet 프로브를 흉내 낸 로컬 실측에서 readiness 이탈 주기(70~160초마다 약 10초)를 넣어도 게이트웨이 p95 가 고장 내내 2~4초(판정 문턱 1초)였고 checkout 25~57% 가 500. 게이트웨이의 토큰 확인에는 서킷이 없고(자기 호출) 홍수는 게이트웨이를 거치지 않아 어느 서킷도 홍수를 줄이지 않는다. 주기를 만드는 것은 readiness 이탈이고, 판정은 주기를 건너는 신호(게이트웨이 p95)로 한다 (§4, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f16-p` 로 109 docker 에 commerce-notification:1.3.0 이 있어야 preflight 가 통과한다(빌드 전후 기본 이미지 ID 비교는 스크립트가 한다).
- KCM 이 새 파드의 'Pulled ... commerce-notification:1.3.0' 을 잡는지.
- notification 'Recipient directory refresh failed: ... Connection refused' 가 user 이탈 때마다 몰려 남는지, DPM Top SQL 에 조건 없는 회원 조회가 오르는지, user jvm.cpu.recent_utilization 이 0.5 에 붙는지, user p95 가 초 단위인지.
- user readiness 이탈 주기(KCM Unhealthy)가 흉내 실측(70~160초마다 약 10초)과 비슷한지, 게이트웨이 p95 가 주기를 건너 1초 위에 남아 success 가 서는지, 인시던트가 생기는지(원칙 7).
- entry_status 가 0 이 되지 않는지, notification 메모리가 한도 아래인지, cleanup 뒤 tb-w1 containerd 에 1.3.0 의 이름 참조와 ID 참조가 남지 않았는지(recovery 가 확인).
- 녹화 검증: F16-H, F66-R, F33-P 와 러너 success 가 겹칠 수 있으므로 롤아웃 대상이 notification 이고 'Recipient directory' 로그와 user CPU 포화(잠금 대기 없음)가 있어야 F16-P 녹화로 인정.
