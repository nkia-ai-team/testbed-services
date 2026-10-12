---
title: F76-R 설계 시트 (commerce product-service 릴리스의 니코틴 함량 칸이 함량 표기 없는 액상 상품에서 예외를 내 상품 목록이 500)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - release
summary: commerce product-service 를 릴리스 1.4.0(fault-images/f76-r 패치로 만든 별도 태그)으로 롤아웃한다. 성인 인증 표시와 함량 필터를 위해 상품 목록, 상세 응답에 액상 상품의 니코틴 함량(mg)을 더했는데, 설명의 'N mg' 을 정규식으로 찾으며 Matcher.find() 결과를 보지 않아 함량 표기가 없는 액상 상품(운영 카탈로그의 대량 등록분 500개)을 응답에 담는 순간 IllegalStateException 으로 500 이 된다. 그런 상품이 든 상품 목록과 검색이 실패하고 다른 카테고리, 상세, checkout 은 정상이다. 원본은 GitLab 2026-03-17 production#21573, 기능 변경의 잘못된 호출이 애드온이 있는 네임스페이스의 프로젝트에서만 API 직렬화 중 예외를 낸 장애.
---

# F76-R 설계 시트

## 1. 요약

commerce product-service 의 새 릴리스 1.4.0 은 "액상 상품에 니코틴 함량을 따로 보여 주자"(성인 인증 표시, 함량 필터 준비)는 기능을 더한다. 상품 목록, 상세 응답 형을 product-service 안의 `CatalogProductResponse` 로 바꿔 공통 상품 칸에 `nicotineMg` 를 더하고, 카테고리 2(액상)면 설명에 '무니코틴' 이 있을 때 0, 아니면 설명에서 정규식 `(\d+)\s*mg` 를 찾아 그 숫자를 싣는다. 그런데 `matcher.find()` 의 결과를 보지 않고 `matcher.group(1)` 을 부른다.

개발 시드(대표 상품 16개)의 액상 4개는 설명이 '니코틴 9mg', '니코틴 6mg', '니코틴 3mg', '무니코틴' 이라 이 경로가 실패한 적이 없다. 운영 카탈로그는 2,016개이고 그 가운데 대량 등록 액상 500개의 설명은 '자동 생성 시드 상품 #n' 이라 함량 표기가 없다(109 읽기 전용 조회: 액상 504개 중 'N mg' 이나 '무니코틴' 이 있는 것 4개). 기동과 `/actuator/health` 는 그대로라 새 파드가 Ready 가 되어 기본 롤링 전략으로 옛 파드를 대신하고, 그 뒤 그런 상품이 하나라도 든 목록 응답은 만드는 중에 `IllegalStateException: No match found` 가 나 500 이 된다.

비유: 상품 진열대 안내판 담당(product)이 새 안내판 양식에 "액상은 니코틴 몇 mg" 칸을 더했다. 칸을 채우려고 상품 설명서에서 'mg' 숫자를 찾는데, 숫자가 없으면 멈추라는 확인을 빼먹었다. 시험 매장(개발 시드)의 액상 상품 설명서에는 다 숫자가 적혀 있어 문제가 없었지만, 실제 매장에 대량으로 들여놓은 액상 상품 설명서에는 숫자가 없다. 그 상품이 한 칸이라도 걸린 진열대 안내판은 통째로 인쇄가 멈춘다. 계산대(checkout)와 상품 상세는 이 칸을 쓰지 않아 멀쩡하다. 고칠 곳은 확인 없이 칸을 채우게 만든 안내판 담당의 새 버전이다(되돌림).

## 2. 원본 사례

- **GitLab, 2026-03-17** (공식 운영 사고 기록 production#21573, incident.io INC-8530, 심각도 2): https://gitlab.com/gitlab-com/gl-infra/production/-/work_items/21573
  - 근본 원인: 최근의 Duo Code Review 기능 변경이 결함을 들였고 "an incorrect call to the Feature.enabled? method caused a NoMethodError during API serialization". 이 예외는 GitLab Credits 애드온이 있는 네임스페이스의 프로젝트에서만 났다.
  - 영향: 그 프로젝트들은 머지 리퀘스트를 만들 수 없고 /api/v4/projects 를 포함한 여러 API 엔드포인트로 접근하지 못함, "Over 15,000 errors occurred", 프로젝트가 많은 고객은 우회책 적용에 큰 수작업.
  - 완화: 프로젝트 단위로 GitLab Duo 를 끄는 우회책 안내, "A revert merge request fixing the root cause was created, reviewed, merged, and is now fully deployed to production." 오류율 정상화.
  - 기록에 UTC 시각과 탐지 경로는 적혀 있지 않다.
- `docs/ref-real-world-incidents.md` M2(버그 있는 버전 배포)에 이번에 추가했다(출처가 말한 사실만).
- 같은 꼴의 보조 근거(자료 문서 M1): Google Cloud 2025-06-12, 오류 처리와 기능 플래그 없이 들어간 코드 경로가 빈 필드가 든 데이터를 만나 널 포인터로 죽음. 다만 그 사례는 코드 배포와 데이터 변경의 두 계기라 원본으로 쓰지 않는다(rejected 219행). 같은 결함 꼴의 LaunchDarkly 2021-09-30(개인 속성을 선언한 환경에서만 난 결함)은 브라우저 화면 오류라 원본으로 쓰지 않았다(§10 후보 10).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Duo Code Review 기능 변경 배포 | product-service 릴리스 1.4.0 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그 1.4.0) |
| 원인이 된 결함 | 새 코드의 잘못된 호출(`Feature.enabled?`)이 API 직렬화 중 NoMethodError | 새 코드의 확인 빠진 호출(`matcher.group(1)`, find() 결과 무시)이 응답 변환 중 IllegalStateException |
| 결함이 드러나는 범위 | GitLab Credits 애드온이 있는 네임스페이스의 프로젝트만(그 기능 경로를 타는 데이터) | 카테고리 2(액상)이면서 설명에 함량 표기가 없는 상품만(그 기능 경로를 타는 데이터, 운영 500개, 개발 시드 0개) |
| 전파 경로 | 그 프로젝트를 응답에 담는 API 와 MR 생성이 500 | 그 상품을 담는 목록, 검색 응답 전체가 500, 게이트웨이가 그대로 넘김 |
| 사용자 증상 | 해당 고객의 MR 생성, 프로젝트 API 실패(15,000건 넘는 오류), 다른 고객 정상 | 상품 목록 보기의 절반 넘게 500(전체 카탈로그 모든 쪽, 액상 카테고리, 액상이 걸리는 검색), 다른 카테고리, 상세, 장바구니, checkout 정상 |
| 탐지된 경로 | 적혀 있지 않음 | product 오류율, SEVERE 로그 급증으로 lucida 이상 탐지와 인시던트(원칙 7) |
| 완화와 복구 | 기능 끄기 우회책, 변경 되돌림 | cleanup 이 product 를 매니페스트 이미지로 되돌림. 쓴 데이터가 없다 |

기전 고리(기능 변경 배포 → 새 코드 경로의 호출 결함 → 그 경로를 타는 일부 데이터의 응답 직렬화에서 예외 → 그 데이터를 담는 API 500, 나머지는 정상 → 되돌림으로 복구)는 원본과 같다. 바꾼 것은 대상(프로젝트 API → 상품 목록 API)과 결함 호출의 종류뿐이다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 52 + 후보 59 = 111 → 112)

- 서비스(정식 + 후보): commerce 37, core-banking 37, food-delivery 37(세 서비스 동률, 원칙 2 에 따라 어느 것이든 된다) → **commerce 38**.
- 묶음: **J(결함 있는 새 버전 배포) 17 → 18, 112 중 16.1%**. 바뀐 것은 product-service 이미지 하나이고 설정, 자원, DB, 데이터는 그대로라 G, A, P 가 아니고, 주고받는 쪽의 형식 어긋남이 아니라 릴리스 자신이 특정 데이터에서 응답을 못 만드는 것이라 L 이 아니다. 금지 묶음 없음(최다 D, J 각 17, 15%).
- 정답 위치: **상품 서비스 1 → 2, 112 중 1.8%**. 결제 경로 합계 13/112(11.6%, 무관). 20% 에 닿는 위치 없음(최다 은행 이체 서비스 10).
- 왜 이 후보인가:
  - 현실에서 가장 흔한 계기(바이너리 배포 37%, Google SRE Workbook)이고, "새 코드 경로가 운영에만 있는 데이터 모양에서 실패" 는 테스트베드에 없던 결함 꼴이다. 기존 J 는 무거운 질의, 연결, 스레드 누수, 응답 형식 변경, 설정 키 누락, 요청 한도, 경로 이동이다.
  - 실패가 부하나 자원이 아니라 데이터를 따른다(같은 엔드포인트가 카테고리에 따라 200 과 500). 관제 AI 가 "상품 500 이면 DB" 나 "목록이 깨지면 표 문제" 를 외워 찍지 못하고, 롤아웃 시각과 새 예외, 실패하는 요청의 공통점으로 추론해야 한다. 같은 '상품 둘러보기 500' 의 F23-P(표 지우기), F73-R(잠금 대기열)과 정답이 갈리는 짝이다.
  - commerce 의 릴리스 시나리오는 F43-R(cart), F16-P(notification) 둘뿐이고 상품 서비스 정답은 1 이다.
- 이번 실행에서 막힌 후보, 장부 §4-1, §5, rejected 와 같은 원본 사례나 같은 주입이 아니다(GitLab 2026-03-17 은 처음 쓰는 원본이고, product 1.4.0 태그와 이 패치는 새 주입이다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `k8s.image` release 모드가 `testbed-product` 컨테이너 이미지를 `commerce-product:latest` → `commerce-product:1.4.0`(commerce/k8s/21-product-service.yaml:27-28, imagePullPolicy Never, replicas 1 :9, nodeSelector tb-w1 :18-19).
2. 새 product 파드 기동: 프로브가 모두 `/actuator/health`(:78-95)라 통과, Ready → 기본 전략(maxSurge 25%)으로 옛 파드 교체.
3. 결함: `scripts/scenarios/fault-images/f76-r/product-service.patch:107-109`(`LIQUID_CATEGORY_ID = 2L`, `NICOTINE_MG = (\d+)\s*mg`), `:172-183`(`nicotineMg`: '무니코틴' 이면 0, 아니면 `matcher.find();` 다음 줄 `Integer.valueOf(matcher.group(1))`), `:156-170`(`toResponse` 가 상품마다 `nicotineMg` 를 부름).
4. 데이터: commerce/db/seed-all.sql:122-131(대량 등록 2,000개, `category_id = 1 + (n % 4)`, 설명 '자동 생성 시드 상품 #n'), commerce/product-service/src/main/resources/data.sql:8-16(대표 액상 4개는 함량 또는 '무니코틴'). 109 읽기 전용 조회: 카테고리별 504개, 액상 중 파싱되는 것 4개, 설명 NULL 0.
5. 실패 범위: `ProductService.getProducts`(commerce/product-service/src/main/java/com/commerce/product/service/ProductService.java:42-54)가 쪽의 상품마다 `toResponse` 를 부르므로 한 상품의 예외가 쪽 전체를 500 으로 만든다. 상세 `getProductWithStock`(:75-87)은 `inventoryClient.getStock` 이 먼저라 재고 행이 없는 대량 등록 상품은 함량 계산 전에 404(평시와 같음), 대표 상품은 통과. `reserveStock`(:89-99)은 `ReserveStockResponse` 를 만들어 새 칸을 거치지 않는다(order ProductClient.java:30 이 부르는 checkout 경로).
6. 전파: 게이트웨이 `ProxyService.doForward`(commerce/api-gateway/src/main/java/com/commerce/gateway/service/ProxyService.java:108-128)가 `RestClientResponseException` 을 받아 같은 상태로 돌려줘 500 이 그대로 나가고 product 서킷은 열리지 않는다(예외가 없음).

로컬 실측(2026-10-12, PostgreSQL 16(0.5 CPU, 1GiB) + product, inventory schema.sql, data.sql + seed-all.sql 의 대량 상품과 옵션 2,016개, 매니페스트 inventory jar, 매니페스트 product jar 와 패치 product jar 를 나란히):

| 요청 | 매니페스트 jar | 릴리스 jar |
|---|---|---|
| `GET /api/products?page=0..50&size=20` | 51/51 쪽 200 | **51/51 쪽 500** |
| `?category=2&page=0..50` | 51/51 200 | **26/51 500**(상품 있는 0~25쪽), 나머지 빈 쪽 200 |
| `?category=1,3,4&page=0..50` | 200 | 200(0/51 실패) |
| 검색 `q=` loadgen 키워드 8개 x 0~5쪽 | 모두 200 | 'Nova' 6/6 500, 나머지 7개 200 |
| 상세 1~16 | 200 | 200(6번 nicotineMg 6) |
| 상세 1001~1008 | 404 | 404(재고 행 없음, 평시와 같음) |
| `GET /api/categories/2/products` | 200 | 500 |

릴리스 jar 로그: `ERROR ... o.a.c.c.C.[.[.[/].[dispatcherServlet] : Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Request processing failed: java.lang.IllegalStateException: No match found] with root cause`, 스택 `java.util.regex.Matcher.group` → `ProductService.nicotineMg(ProductService.java:137)` → `ProductService.toResponse(ProductService.java:123)`, 168줄. 운영에서는 이 로그가 OTel 로그 수집기로 `severity_text = SEVERE` 로 들어간다(아래 §7).

기준선 상품 목록 단계(commerce/loadgen/script.js:129-137, `step: 'list'`)는 절반이 카테고리 없는 목록, 절반이 category=1..4 라 실패 비율 예상치는 0.5 + 0.5 × 0.25 × 26/51 ≈ **0.56**.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `commerce-product`(target_kind container). 결함을 가진 곳은 응답 변환에 실패 처리 없는 코드를 들인 product 릴리스이고, 되돌리거나 고쳐야 재발이 막힌다. 함량 표기가 없는 상품 설명은 운영 카탈로그의 정당한 데이터다("A 가 정당하고 B 의 로직이 잘못" → B). 원본 기록이 지목한 층위(기능 변경이 들인 잘못된 호출, 되돌림으로 복구)와 같다.
- `trigger_target_id`: 비움(계기인 롤아웃과 근본이 같은 서비스).
- `scoring.partial`: `commerce-gateway`(500 을 사용자에게 돌려준 곳). 상품 데이터나 PostgreSQL 은 부분 점수에 넣지 않는다: 데이터는 바뀌지 않았고 DB 는 정상이다.
- 원칙 5: 정답에 코드 설계 추론이 필요 없다. product 롤아웃 시각, 바로 뒤 product 자신의 SEVERE `IllegalStateException: No match found`(스택에 nicotineMg), 실패가 특정 카테고리 목록에만 나고 DB 와 inventory 가 멀쩡하다는 것으로 "product 새 릴리스가 일부 상품 응답을 못 만든다" 에 이른다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida 이상 탐지 | product, 게이트웨이 서버 스팬 오류율(500) 급증, product SEVERE 로그 급증. 별도 업무 규칙 없이 겉 증상이 크다(상품 목록 보기 절반 넘게 실패) |
| 피해 판정 | 러너 | 상주 기준선 상품 목록 단계 4xx+5xx 비율 ≥ 0.3(3틱 연속, 예상 약 0.56, 평시 0 근처) |
| RCA | 녹화 데이터 | KCM product 롤아웃과 Pulled 태그 1.4.0, product SEVERE 'No match found'(스택 nicotineMg), product GET /api/products 500, category=1,3,4·상세·reserve-stock·checkout 정상, PostgreSQL·inventory 정상 |

## 7. 관측 근거 (119 실조회, 평시)

| 증거 | 119 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 계기: product 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(timestamp, namespace, object_kind, object_name, reason, body) | `namespace='rca-testbed-commerce'` 30일 object_name, reason 별 | `testbed-product%` 0건(30일 동안 product 롤아웃 없음). 같은 표에 commerce 롤아웃이 수집된다: ScalingReplicaSet 431, Pulled 395(마지막 2026-10-11 20:32). 릴리스 롤아웃 예: 2026-10-11 20:22:18 `testbed-cart-59cf66998c-fj6hn` Pulled 'Container image "commerce-cart:1.2.0" already present on machine' → 20:31:00 'commerce-cart:latest' 로 되돌림. 장애 때는 'commerce-product:1.4.0' 이 본문에 담긴다 |
| 근본: product 의 응답 변환 예외 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | 30일 모든 서비스 `body LIKE '%No match found%'` | 0건. product 의 SEVERE/ERROR/WARN 30일: `Request processing failed: org.springframework.web.client.ResourceAccessException` 9,265, `RestClientException` 129(inventory 로 가는 호출 실패, 다른 시나리오 실행분, 마지막 2026-10-06 11:31). 처리되지 않은 예외의 `Servlet.service() ... threw exception [...]` SEVERE 가 product 에서 전수 수집됨을 확인했다 |
| 전파: product 와 게이트웨이 서버 스팬 상태 | CH `lucida.otel_traces_local`(service_name, span_name, span_kind, span_attributes['http.response.status_code']) | 1일, product SERVER 와 게이트웨이 SERVER `%product%` | product: `GET /api/products` 200 41,804건(500 없음), `GET /api/products/{id}` 404 50,060 / 200 378, `GET /api/products/{id}/variants` 200 10,146, `POST /api/products/{id}/reserve-stock` 200 6,356. 게이트웨이 `GET /api/products/**` 200 52,324, 404 54,982, 500 0. 장애 때 목록이 500 으로 바뀌고 상세, 옵션, reserve-stock 은 그대로다(표본 10%, 보조 증거) |
| 데이터 모양(관제 데이터 아님, 설계 확인용) | 109 commerce PostgreSQL 읽기 전용 | `SET default_transaction_read_only=on; SELECT category_id, count(*), count(*) FILTER (WHERE description ~ '[0-9]+\s*mg' OR description LIKE '%무니코틴%') ...`(2,016행 표) | 카테고리 1~4 각 504개, 액상 중 파싱되는 것 4개(대표 상품 5~8), NULL 설명 0, 재고 행 16개 |
| 피해 범위가 DB 아님 | CH `lucida.dpm_topsql_local`(engine, body JSON sqlText, calls, totalExecTime) | 1시간 commerce 사용자 질의 상위 30 | product 목록 질의(`select ... from product_schema.products p1_0 left join product_schema.categories ...`) 평균 0.09~1.11ms. 릴리스는 질의를 바꾸지 않아 장애 때도 같은 질의, 같은 시간이어야 한다 |

트레이스(10% 표본)와 APM 지표는 보조 증거로만 쓴다. 핵심 증거(롤아웃 이벤트, product SEVERE 예외 로그, 다른 경로 정상)는 전수 수집 표에 있다.

## 8. 감별

- must_support: 정답지 4개(product 롤아웃과 1.4.0 Pulled, product SEVERE 'No match found' 와 nicotineMg 스택, product 와 게이트웨이 목록 500, 실패가 카테고리를 따르고 상세·reserve-stock·checkout·DB 정상).
- must_rule_out:
  - F23-P(상품 표 지우기): DB 오류와 DROP 이 없고 같은 표를 읽는 상세와 reserve-stock 이 성공.
  - F73-R(상품 표 잠금 대기열): 몇 ms 안에 500, 시간 초과·풀 대기·재시작·잠금 대기 없음.
  - F12-H(product CPU 한도): 지연 증가 없이 특정 카테고리 응답만 실패.
  - F09-P, F01-R(inventory 쪽): inventory 를 부르지 않는 목록이 실패하고 inventory 를 부르는 상세, reserve-stock 이 성공.
  - F64-R(게이트웨이 서킷 설정): product 자신의 서버 스팬과 SEVERE 가 500, 게이트웨이는 그대로 넘김.
- contrast_with: F23-P, F73-R, F12-H, F43-R, F49-R(정답지에 각 차이).

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: `k8s.image` release 모드, 레벨 하나 `approved-fixed-f76-r`(min_hold 8m, settle 60s, timeout 14m, max_injection_duration 20m). 강도 사다리 없음(데이터에 함량이 없으면 매번 실패하는 결정적 결함).
- 동반 부하: `load.north_south` surge.js, target_rps 3, entry 30080, ramp_up 2m, hold 16m(F16-P, F43-R 과 같은 값). 여정 35% 가 카테고리 없는 목록 둘러보기(모두 실패), 50% 가 checkout(정상이어야 함).
- 판정은 상주 기준선(`loadgen.read_step_status_rate`, domain commerce, step list)으로 한다. 기준선 목록 호출은 하루 표본 41,804건(약 초당 4.8건)이라 60초 창마다 표본이 넉넉하다. 예상 실패 비율 약 0.56, 문턱 0.3.
- 부하 상한: 3rps 는 상한 180 의 1.7%. 예외는 질의가 끝난 뒤 응답 변환에서 나 product 스레드, 풀, PostgreSQL 에 더 얹는 부담이 없다(목록 질의 0.1~1ms).
- 서킷브레이커: 게이트웨이 product 서킷은 예외가 없어 열리지 않는다. 따라서 실패가 서킷으로 가려지거나 줄지 않고 데이터에 따라 일정하다. order 의 productClient 서킷은 reserve-stock 이 성공해 영향이 없다.
- success: `read_nonok_rate ≥ 0.3`, 3틱. must_rule_out: `achieved_rps < 1`, commerce PostgreSQL NotReady, 동반 checkout 5xx ≥ 0.3(checkout 까지 깨지면 다른 장애). abort: `entry_status == 0`. recovery: target_health 200, product pod_ready, PostgreSQL Ready, 기준선 목록 실패 < 0.1.
- cleanup: product 를 매니페스트 이미지로 되돌리고 Deployment available, 1.4.0 을 쓰는 파드가 없어지면 tb-w1 containerd 에서 이름과 ID 참조 삭제. 장애 동안 쓴 데이터가 없다.
- 결함 이미지 규칙: 앱 소스와 매니페스트, 기본 이미지 그대로. 패치는 `commerce/product-service/src/` 안만(테스트가 확인, origin/main 사본에 `git apply --check` 통과, 같은 사본으로 빌드한 jar 로 §4 실측). 태그 1.4.0 은 저장소 어디에도 없다(product 의 다른 릴리스 없음). 추가 문자열에 시나리오 id, fault, bug, chaos, scenario 없음(테스트가 확인). 빌드는 `bash scripts/scenarios/fault-images/build.sh f76-r`(배포 스크립트가 녹화 구간 확인 뒤 실행).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitLab 2026-03-17 production#21573, 공식 운영 사고 기록, 링크)와 요소별 대응표가 있고 기전이 같다(§2)
- [x] 분류 장부를 갱신했고 묶음 J(18/112, 16.1%), 정답 위치 상품 서비스(2/112, 1.8%), 결제 경로 13/112(11.6%), 서비스 commerce 38(세 서비스 동률에서)과 고른 이유를 적었다. 어느 축도 20% 에 닿지 않는다(§3)
- [x] 근본 원인 위치가 패치 줄과 product, 게이트웨이 소스 줄, 시드 줄로 확인됐다(§4)
- [x] 근본 원인 흔적이 119 에서 조회됐다: kcm_events_local 의 commerce 롤아웃과 Pulled 태그, lucida_logs_local 의 product SEVERE 수집, otel_traces_local 의 product, 게이트웨이 경로별 상태(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트와 전수 로그(§7)
- [x] 계기의 흔적이 조회됐다: 롤아웃 이벤트와 이미지 태그. 인공 지연 없음
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(product 롤아웃 + product 자신의 예외 + 데이터를 따르는 실패)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 이미지 태그, 패치 문자열, 실행기 argv 에 없음(테스트)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다(§4 로컬 실측, §9)

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

숫자: commerce, banking, food 모두 37(동률). 묶음 D, J 각 17(15%)이 가장 많고 금지 없음, 0 인 묶음 N. 정답 위치 상한 근처 없음(최다 은행 이체 서비스 10, 9%), 결제 경로 11.7%. 부품 지도 0: 세 도메인 kafka, commerce·banking nginx(모두 앞선 반려의 벽: outbox 비동기, nginx 119 미수집).

| 순위 | 후보(원본 / 부품 / 기전) | 결과 |
|---|---|---|
| 1 | GitLab 2026-03-17 production#21573 / commerce product-service / 릴리스의 새 코드 경로가 운영에만 있는 데이터 모양(함량 표기 없는 액상)에서 응답 변환 중 예외, 그 데이터를 담는 목록 500 | **채택** |
| 2 | Harness 2026-04-24(status.harness.io bsp454fy2v0p, 일괄 처리 없는 한 트랜잭션 대량 DELETE 의 잠금 경합) / food MySQL dispatches, dispatch_events + 정리 Job / 정리 Job 이 새 배차 쓰기를 막음 | 버림: 원칙 9, 1. 109 EXPLAIN 에서 자연스러운 정리 DELETE(배달 완료 7~28일 지난 것)는 모두 PK 전수 스캔이라 새 행이 들어갈 끝(supremum)은 문장 끝에야 잠겨 막는 창이 몇 초다. 길게 막으려면 여러 문장을 한 트랜잭션에 묶어야 하는데, 커밋되면 7~30일 배차 데이터가 사라져 다른 시나리오의 표 크기 기준(F70-P 등)을 깨고, 끊으면 되돌림이 잠금을 쥔 채 수 분 이어져 cleanup 이 불확정이다. PostgreSQL, Oracle 은 DELETE 가 지우는 행만 잠가 새 쓰기와 겨루지 않는다(기전 불성립). food 판은 F69-R(내보내기 Job 의 표 잠금)과 Job, 잠금, food 쓰기 막힘 꼴도 겹친다 |
| 3 | Azure 2026-05-29 PIR LYXT-C1Z, DigitalOcean 2026-08-24(status.digitalocean.com 4qwm6pzlx1z3) / banking api → account → transfer 재시도 층 / 재시도하지 않던 실패가 재시도 가능한 5xx 로 바뀌어 층마다 재시도가 증폭(묶음 N 0) | 버림: 원칙 9. 층마다 서킷(10건 창 50%)이 증폭을 끊고, 바뀔 업무 4xx 가 시간당 몇 건(119 api '400' 로그 시간당 4건)이라 9배로 늘어도 transfer, Oracle 부하가 미미. commerce 게이트웨이는 하류 5xx 응답을 예외 없이 넘겨 재시도하지 않는다. DigitalOcean 은 느린 질의 + 헤지 재시도 두 계기다 |
| 4 | RevenueCat 2022-11-23 의 주 원인(이전 뒤 ANALYZE 미실행으로 질의 계획 비효율) / commerce PostgreSQL / 이전된 DB 의 통계 없음 | 버림: 원칙 9. commerce 사용자 경로 상위 질의가 PK, 유일, 부분 인덱스 조회와 작은 표(DPM 평균 0.05~3ms)라 통계 없는 기본 선택도로도 인덱스 계획이 유지될 계산이고, 같은 원본과 이전 이야기가 F57-P 다 |
| 5 | Slido 2022-04-22(status.slido.com h0y425yzsf95, 기계 생성 요청이 MySQL max_prepared_stmt_count 에 닿음) / food MySQL 인스턴스 | 버림: 원칙 1. food 서비스는 Connector/J 기본(useServerPrepStmts=false)이라 서버 준비문을 만들지 않아 한도에 닿을 길이 없고, 만들려면 준비문 누수 릴리스와 트래픽 두 계기가 필요하다 |
| 6 | Box 2025-02-19(status.box.com 4shks2nlxwyl, 불필요하다고 본 인증 키 삭제로 앱과 DB 인증 실패), OpenAI 2026-09-25(내부 서비스 자격 증명 오탐 회수) / DB 계정 | 버림: 카탈로그 §1. 우리 DB 인증은 연결 때 한 번이라 풀 수명 뒤에야 실패하는 F35-R, F71-R 끝 상태와 같고, 서비스 전용 계정이 없다(commerce 는 여덟 서비스가 한 역할, food 는 네 서비스가 한 계정이라 입구 0) |
| 7 | Cloudflare 2022-10-25(릴리스의 추적 래퍼가 제어 헤더를 지움) / commerce 게이트웨이 | 버림: 원칙 1. 게이트웨이가 요청 헤더를 하류로 넘기지 않는 구조라 지울 헤더 경로가 없다 |
| 8 | MongoDB Atlas 2026-03-01(쓰이던 옛 API 버전 은퇴, 410) / banking transfer | 버림: 카탈로그 §1. 같은 경로를 account 와 commerce 가 함께 써 버전 구분이 없고, 경로 은퇴 꼴은 F75-R(짝 없이 옮긴 경로)과 같다 |
| 9 | Spotify 2025-04-16(Envoy 힙이 쿠버네티스 메모리 한도보다 큼) / commerce, banking JVM 설정 배포 | 버림: 원칙 1. 원본에서 힙 설정은 크래시 루프를 키운 요인이지 계기가 아니고, 계기로 세우면 끝 상태가 F05-R(메모리 한도 OOM 루프)이다 |
| 10 | LaunchDarkly 2021-09-30(개인 속성을 선언한 환경에서만 난 결함) / commerce product | 원본 교체: 원본이 브라우저 화면 오류라 서버 5xx 가 아니어서(rejected 93행 벽) 같은 꼴의 서버 쪽 공식 사례 GitLab 2026-03-17 로 바꿨다 |
| 11 | (공식 사례 없음) PostgreSQL synchronous_standby_names 잘못 설정으로 쓰기 커밋 멈춤, max_locks_per_transaction 공유 잠금표 고갈, idle_in_transaction_session_timeout 축소 / commerce PostgreSQL | 버림: 원칙 1(웹 검색 각 1~2회, 메일링 리스트와 블로그뿐) |
| 12 | Coinbase 2020-05-09(인스턴스 단위 DNS 질의 속도 제한) / 파드 DNS | 버림: 원칙 3. 질의 한도가 걸릴 곳이 kube-system CoreDNS 라 119 미수집이고 JVM DNS 캐시로 질의가 적다 |
