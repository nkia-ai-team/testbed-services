---
title: F55-R 설계 시트 (food dispatch-service 를 요청 IP 기준 요청 한도를 더한 릴리스로 롤아웃해 주문이 503)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
summary: food dispatch-service 를 결함 있는 새 릴리스(food-delivery-dispatch:1.8.0, fault-images/f55-r 패치로 만든 별도 태그)로 롤아웃하면, 배달 API 앞에 더한 클라이언트별 요청 한도(요청의 원격 주소로 구분, 순간 20회, 이후 3초에 1회)가 이 배포에서는 order 파드 주소 하나와 NodePort 진입의 노드 주소 하나로 모인 모든 호출에 걸려, dispatch 는 Ready 이고 빠른 채 429 를 돌려주고 order 가 용량 확인과 배차의 429 를 재시도와 서킷 끝에 503 으로 바꿔 거의 모든 새 주문을 거절하는 시나리오. 원본은 PostHog 2025-10-24 IP 기준 요청 한도가 로드밸런서 IP 하나만 보고 요청의 97% 를 429 로 거절한 장애.
---

# F55-R 설계 시트

## 1. 요약

food dispatch-service 의 새 릴리스 1.8.0 은 "한 클라이언트의 배달 상태 폴링이 배차 API 를 느리게 하지 않게" 하려고 `/api/deliveries` 로 시작하는 모든 요청 앞에 서블릿 필터를 더했다. 필터는 요청의 원격 주소(`request.getRemoteAddr()`)로 클라이언트를 구분해 클라이언트마다 순간 20회, 이후 3초에 1회씩 채워지는 토큰만큼 받고, 넘는 요청은 처리하지 않고 429 Too Many Requests 로 돌려준다(본문 `{"status":429,"error":"Too Many Requests","message":"Rate limit exceeded for client <주소>"}`). 거절이 있으면 클라이언트마다 30초에 한 번 WARN 을 남긴다. 프로브(`/actuator/health`)는 제한하지 않는다. 릴리스 자신의 단위 시험은 서로 다른 주소를 가진 클라이언트로 "폴링하는 클라이언트 하나만 막히고 다른 클라이언트는 자기 몫을 쓴다" 를 확인한다.

이 배포에서 dispatch 를 부르는 쪽은 서로 다르지 않다. 내부 호출(용량 확인, 배차)은 전부 order 파드 하나(10.244.2.77)에서 오고, 외부 NodePort 호출(부하 발생기의 배달 추적)은 전부 tb-cp 노드의 flannel 주소 10.244.0.0 으로 바뀌어 들어온다. 119 의 평시 dispatch 서버 스팬 client.address 가 정확히 이 둘이다(§7). 새 파드가 트래픽을 받으면 order 주소의 토큰 20개가 몇 초 만에 바닥나고, 그 뒤로는 3초에 1회만 통과한다. order 는 주문마다 dispatch 를 두 번(저장 전 `GET /api/deliveries/capacity`, 저장 뒤 `POST /api/deliveries/dispatch`) 불러야 하므로 성공 주문은 많아야 6초에 1건이다. 나머지는 order 가 'Failed to check dispatch capacity: 429 ...' 나 'Failed to dispatch courier for order N: 429 ...' 를 남기고 재시도(3회), 서킷 열림 끝에 503 으로 거절하고 주문 행을 되돌린다. dispatch 는 Ready 이고 응답이 빠르며 연결 풀, MySQL, 프로브는 그대로다.

비유: 배차실(dispatch)이 "한 손님이 같은 창구에 몰리지 않게 손님마다 3초에 한 번만 받는다" 는 새 규칙을 걸었다. 그런데 손님을 얼굴이 아니라 "누가 문을 열고 들어왔나" 로 센다. 주문 접수처(order) 직원 한 명이 모든 손님의 주문서를 들고 오고, 바깥 손님은 모두 같은 회전문(노드 주소 변환)을 지나 들어오니, 배차실은 손님이 둘뿐이라고 보고 거의 다 돌려보낸다. 배차실은 한가하고 장부(MySQL)도 그대로다. 고칠 곳은 새 규칙(릴리스)이다.

## 2. 원본 사례

- **PostHog, 2025-10-24** (공식 사후 보고 "Feature Flags Incidents, October 21–30, 2025" 의 두 번째 사고): https://posthog.com/handbook/company/post-mortems/2025-10-21-feature-flags-recurring-outages.md
- 18:00~19:12 UTC(72분) 전 세계 flag 평가 요청의 약 97% 가 HTTP 429. 계기는 보호 조치로 /flags 엔드포인트에 IP 기준 요청 한도를 배포한 것(PR #40074). 기전: tower-governor 미들웨어가 "saw all traffic as coming from a single IP (our load balancer) rather than actual client IPs". X-Forwarded-For 를 무시하는 기본값이 신뢰하는 로드밸런서 뒤 구성에 맞지 않았고, 시험은 서비스로 직접 들어오는 트래픽만 다뤄 운영 로드밸런서 뒤 트래픽을 다루지 않았다. 탐지는 고객 보고("No alerting configured for 429 errors", 62분). 19:02 원인 확인, 19:05 요청 한도를 꺼서 19:12 회복.
- 자료 문서 M19 표에 이미 한 줄 있었고, 이번에 위 세부를 공식 사후 보고에서 보강했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | /flags 에 IP 기준 요청 한도를 더한 코드 배포(PR #40074) | dispatch-service 를 릴리스 food-delivery-dispatch:1.8.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 요청 한도가 클라이언트를 직접 연결의 IP 로 구분(X-Forwarded-For 무시 기본값), 실제로는 모든 요청이 로드밸런서 IP 하나로 보임 | 요청 한도가 클라이언트를 요청의 원격 주소로 구분, 실제로는 모든 내부 호출이 order 파드 하나, 모든 외부 호출이 노드 주소 변환 하나로 보임 |
| 전파 경로 | 한 IP 의 한도가 모든 요청에 적용 → 대부분 429 → flag 평가 실패 | order 주소의 토큰이 바닥 → 용량 확인, 배차가 429 → order 재시도, 서킷 → 주문 503 |
| 사용자 증상 | flag 평가 요청 약 97% 가 429 | 새 주문 거의 전부 503(약 6초에 1건만 통과), 배달 추적 429. 가게 둘러보기와 메뉴는 정상 |
| 원본의 탐지 경로 | 고객 보고(429 경보 없음), 62분 | lucida-next 의 order 오류율과 오류 로그 급증(order 가 429 를 503 으로 바꿔 서버 스팬 오류), dispatch WARN 'Rate limit exceeded for client ...' |
| 완화와 복구 | 요청 한도를 끔 | cleanup 이 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(보호용으로 배포한 클라이언트별 요청 한도가 클라이언트를 직접 연결 주소로 구분)에서 증상(그 주소가 실제로는 많은 사용자를 모은 한 지점이라 한도가 거의 모든 요청을 429 로 거절)까지 고리가 같다. 원본이 "시험은 직접 들어오는 트래픽만 다뤘다" 고 한 점은 릴리스의 시험이 서로 다른 클라이언트 주소로만 확인하는 꼴로 둔다.

바꾼 것(기전 고리 밖):
- 스택: Rust tower-governor → Spring 서블릿 필터(토큰 버킷, 의존성 추가 없이 릴리스 코드로). governor 도 GCRA(토큰 버킷 계열)라 "순간 허용 + 일정 간격 보충" 꼴이 같다.
- 모이는 지점: 원본은 로드밸런서 하나. 테스트베드는 내부 호출의 order 파드와 NodePort 의 노드 주소 변환 둘이다. 사용자 피해는 order 경유 주문에서 난다.
- 영향 비율: 원본 97%. 테스트베드는 성공이 보충 속도로 정해져 동반 부하에서 약 90%(§9).
- 탐지: 원본은 429 경보가 없어 고객 보고로 알았다. 테스트베드는 order 가 429 를 503 으로 바꿔 오류율로 드러난다(관제 AI 의 감지 대상이 되게 하는 차이이고 원인 고리는 그대로다).
- 집약점과 고칠 방법: 원본의 집약점은 실제 클라이언트들 앞의 로드밸런서였고, 고친 방법은 한도를 끄는 것과 재발 방지로 신뢰하는 로드밸런서의 X-Forwarded-For 를 쓰는 것이었다. 재구성의 주 집약점은 진짜 단일 호출자인 order 파드다(외부 NodePort 의 노드 주소 변환 10.244.0.0 이 원본의 로드밸런서에 더 가깝지만 그쪽 거절은 부하 발생기의 배달 추적 429 로만 끝난다). order 파드 호출에는 X-Forwarded-For 가 없어 "XFF 신뢰" 로는 고쳐지지 않고, 맞는 고침은 롤백이나 내부 호출자를 한도에서 빼는 것이다. 원인 고리("클라이언트를 직접 연결 주소로 구분한 한도가 많은 사용자를 모은 한 지점에 걸림")는 같고, 그 한 지점이 프록시가 아니라 상류 서비스라는 점이 다르다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 27 = 70, 이 후보 전)

- 묶음(정식 + 후보): G 8, J 8, P 8(각 11%), A 7, B 7, D 7, L 7(10%), C 6, F 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0. 금지(20%, 14) 없음. 이 후보 J 8→9(71 중 12.7%), 결함 꼴이 한도 값과 키 선택이라 G 를 함께 적는다(G 로 세도 8→9, 12.7%).
- 정답 위치: 주문 서비스 7(10%), 노드, 디스크 6, 외부 결제 의존 6, 은행 이체 서비스 6, **배달 서비스 4**(5.7%) → 5(7.0%). 결제 경로 합계 12(17.1% → 71 중 16.9%, 이 후보와 무관).
- 서비스: 쇼핑몰 26, 은행 22, 음식배달 22. 음식배달 22 → 23.
- 부품 지도: food dispatch 6(배달 서비스 4 + DB 테이블(배차) 2). 0 인 food 부품은 kafka, notify 뿐이고 둘 다 비동기 소비자라 사용자 증상이 없다(막힌 목록의 여러 행).
- 왜 이 후보인가: 가장 적은 서비스(은행, 음식배달 22)에서, 정답 위치가 같은 서비스의 다른 후보(은행 이체 서비스 6)보다 적은 쪽이다. 기전(M19 레이트 리밋 오설정)이 테스트베드에 처음이고 원본이 공식 사후 보고다. 같은 원본으로 막힌 세 후보(banking nginx limit_req, food MySQL 계정 쓰기 한도, food restaurant 릴리스의 IP 한도)는 각각 관측 불가(nginx OTel 없음), 기전 거리(DB 계정 한도), 4xx 단일 신호(order 의 RestaurantClient 가 429 를 그대로 전파)로 떨어졌다. dispatch 는 order 의 DispatchClient 가 모든 실패(429 포함)를 502, 503 으로 바꾸므로 그 벽에 걸리지 않는다(`food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:40-46, 64-68`).
- 겉 증상(order 503)은 F32-R, F32-H, F33-R, F33-H, F43-P 와 같다. 2026-10-09 사용자 정정대로 증상 겹침은 버릴 사유가 아니고, dispatch 가 건강한 채 429 를 돌려준다는 점이 다섯 모두와 다른 감별 근거라 관제 AI 가 "order 503 이면 dispatch 풀이나 DB" 로 외워 찍지 못하게 한다.

### 같은 원본으로 막힌 후보와의 관계 (규칙 해석, 1차 평가 차단 지적에 대한 판단)

`$OPS/rejected-candidates.md` 16, 24, 170행이 같은 원본(PostHog 2025-10-24)이다. 1차 평가는 iteration.md 3단계 4("rejected ... 와 같은 원본 사례, 같은 주입은 지운다")를 "같은 원본이면 지운다" 로 읽어 차단했다. 이 시트는 그 문구를 "같은 원본 사례이면서 같은 주입인 것을 다시 내지 않는다" 로 읽고 후보가 성립한다고 판단한다. 근거:

- iteration.md 3단계(2026-10-09 사용자가 고친 판)가 버리는 사유를 닫힌 목록으로 적는다: "원칙 1~9, 20% 상한, 같은 주입 중복(같은 수단과 같은 파라미터), 컨트롤러 필수 중단 조건뿐이다". "같은 원본" 은 이 목록에 없다.
- 이번 실행의 앞머리(design-header.md)는 막힌 목록을 "원본 / 부품 / 주입" 세 칸으로 적고 "같은 원본 사례·같은 주입을 다시 내지 말라는 뜻일 뿐이다. 부품, 서비스, 묶음, 원인 종류 전체를 피하라는 뜻으로 넓혀 읽지 말라" 고 한다. 같은 원본으로 여러 시나리오를 만든 선례가 이번 실행에만 여럿이다(GitHub 2025-01-09 의 F42-R, F42-P, F33-H, Resend 2024-02-21 의 F53-R, F53-P, F35-H, F32-P).
- 사용자 정정(2026-10-09)은 다양성의 목적을 "같은 범인(정답 위치)이 몰리지 않게 하는 것" 하나로 둔다. 이 후보의 정답 위치(배달 서비스)는 5/71(7.0%)로 상한 아래다.
- 이 실행의 앞선 판정 중 "이미 막힌 원본" 을 든 막힌 목록 40, 101행은 각각 원칙 9(끊길 질의 없음), 원칙 1(공식 사례 없음, 원본을 찾는 웹 검색에서 막힌 원본만 나옴)이 함께 있었다. 원본만으로 버린 판정은 아니다.

같은 주입이 아니다(같은 수단, 다른 대상과 파라미터): 수단은 k8s.image release 로 같지만 대상 Deployment(testbed-dispatch 대 testbed-restaurant), 이미지 태그(food-delivery-dispatch:1.8.0), 패치 내용이 다르다. 170행 후보는 등록되지 않아 파라미터가 겹칠 실물도 없다. 16행(nginx limit_req 설정 배포), 24행(MySQL ALTER USER 쓰기 한도)은 수단부터 다르다.

앞선 세 후보를 막은 사유를 각각 비켜 가는가:

| 행 | 막힌 후보와 사유 | 이 후보 |
|---|---|---|
| 16 | banking nginx limit_req. 원칙 7, 3: nginx 는 OTel 이 없어 거절이 119 에 남지 않고 commerce 정산은 nginx 를 거치지 않음 | 한도가 OTel 이 붙은 dispatch(Spring) 안에 있다. dispatch WARN 'Rate limit exceeded for client ...' 와 order ERROR 의 429 본문이 전수 로그로 남고 dispatch 서버 스팬도 429 로 끝난다(§7) |
| 24 | food MySQL 앱 계정 쓰기 한도. 증상 중복(F38-R), 원칙 1(원본은 HTTP 리미터라 DB 계정 한도와 기전이 멂) | 원본과 같은 HTTP 요청 한도이고, 결함 꼴(직접 연결 주소로 클라이언트 구분)도 같다. DB 는 건드리지 않는다 |
| 170 | food restaurant 릴리스의 IP 한도. 원칙 7, F34-R 교훈: order 의 RestaurantClient 가 4xx 를 그대로 전파해 429 단일 신호, 서버 스팬 오류와 오류율이 오르지 않음 | order 의 DispatchClient 는 모든 RestClientException(429 포함)을 ServiceException 502, 503 으로 바꾸고(`DispatchClient.java:40-44, 64-67`), dispatch 서킷과 재시도는 ClientErrorException 만 무시하므로 429 가 실패로 집계된다. 주문은 503 으로 끝나 order 서버 스팬 오류와 오류율이 오른다(로컬 270건 중 503 244건) |

의존 명시: 이 후보는 order 의 DispatchClient 가 4xx 를 5xx 로 바꾸는 데 기댄다. 3도메인 클라이언트의 "4xx→502 둔갑 수리"(dc44dc6, 4d2df10)는 업무 거절 4xx 를 그대로 전파하도록 다른 클라이언트들을 고쳤는데 food order 의 DispatchClient 만 그 수리에서 빠져 있다(용량 확인 실패를 '배차 불가' 503 으로 다루는 설계로 보인다). 누가 DispatchClient 에 같은 수리를 하면 이 시나리오는 170행과 같은 429 단일 신호가 되므로, 테스트 `test_f55r_relies_on_order_dispatch_client_turning_4xx_into_5xx`(scripts/scenarios/tests/test_ready_profile_executors.py)가 그 매핑을 고정하고, 깨지면 F55-R 을 다시 평가하라고 알린다.

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | dispatch 릴리스가 요청 IP 기준 클라이언트 요청 한도를 더함, 호출자가 order 파드와 노드 주소 하나씩이라 거의 모든 호출 429, 주문 503 | 앱, food dispatch(J+G, 배달 서비스 4) | PostHog 2025-10-24 공식 | **채택** |
| 2 | banking transfer 릴리스가 같은 요청 한도를 더함, account 파드와 commerce payment 파드가 각각 한 주소라 이체 429, commerce 정산 502 | 앱, banking transfer(J+G, 은행 이체 서비스 6) | PostHog 2025-10-24 공식 | 남김(차순위): 숫자 순(같은 서비스 수, 정답 위치 6 > 4)에서 1 이 앞선다. banking 쪽 api, account 는 4xx 를 그대로 전파해 은행 자체 경로는 429 단일 신호이고, commerce 쪽 증상(transfer 릴리스 4xx → 정산 502)은 F40-H 와 같은 자리다 |
| 3 | banking api 릴리스가 요청 한도를 더함(nginx 뒤라 원본에 가장 가까움) | 앱, banking api(J+G, 은행 API 2) | PostHog 2025-10-24 공식 | 버림: 원칙 7. api 가 받은 429 는 nginx 를 거쳐 그대로 사용자에게 가고(서버 스팬 오류 아님) nginx 는 OTel 이 없어, 4xx 단일 신호(F34-R 교훈) |
| 4 | commerce gateway 에 요청 IP 기준 한도 설정 배포 | 앱, commerce gateway(G, 게이트웨이 0) | PostHog 2025-10-24 공식 | 버림: 원칙 7. gateway 가 맨 앞이라 429 가 그대로 사용자에게 가는 단일 신호(막힌 목록의 gateway 401 행들과 같은 벽) |
| 5 | tb-w3 nf_conntrack_max 축소로 새 연결 SYN 버림 | 인프라, 노드(K, 노드, 디스크 6) | 찾지 못함 | 버림: 원칙 1. 웹 검색 1회(2026-10-10)에서 벤더 KB, 포럼, 가이드뿐이고 회사 공식 사후 보고 없음. 막힌 목록 25행과 같은 주입 |
| 6 | food 서비스 liveness 가 DB 를 봐서 의존 저하 때 재시작 폭풍 | 인프라, 프로브(B, dispatch 등) | Microsoft Tech Community AKS 블로그(2025-03-25, 06-02 재시작 폭풍) | 버림: 원칙 1, 같은 주입. 원본 계기는 API 서버 연결 지연이라 프로브 설정 변경과 다른 두 계기 복합이고, 프로브 경로 변경 주입은 막힌 목록 230행(F05-H 복제) |
| 7 | dispatch_event_logs 의 idx_dispatch_events_dispatch 제거로 배달 이벤트 조회 전수 스캔 | DB, food dispatch_event_logs(F, DB 테이블(배차 이벤트) 0) | Buildkite 2025-11-10 공식 | 버림: 원칙 7, 9. 그 표를 읽는 `GET /api/deliveries/{id}/events` 가 기준선과 동반 부하 어디에도 없다(`food-delivery/loadgen/script.js:157-168` 은 상세와 목록만) |
| 8 | food payments 의 idx_payments_order 제거 | DB, food payments(F, DB 테이블(결제) 2) | Chargebee 2018-03-02 공식 | 버림: 원칙 9. payment 의 사용자 경로 질의 중 order_id 로 찾는 것이 없다(`PaymentRepository` 는 정산 배치, 검색, 보존 질의뿐). 결제 경로도 상한 근처(17.1%) |
| 9 | commerce notification 소비자 정지 | 앱, commerce notification(B, 알림 서비스 0) | (막힌 목록 행의 incident.io 2022) | 버림: 원칙 7. 소비자가 사용자 경로 밖의 비동기 경로라 5xx 가 없다(막힌 목록 행과 같은 벽) |

DB 가 정답인 후보 2개(7, 8), 부품 8종(food dispatch, banking transfer, banking api, commerce gateway, tb-w3 노드, 프로브, food 배차 이벤트 표, food 결제 표, commerce notification), 앱, 인프라, DB 세 층.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 food-delivery-dispatch:1.8.0 을 tb-w3 containerd 에 올리고(`docker save | ssh nkia@<tb-w3> sudo ctr -n k8s.io images import -`) `kubectl set image deploy testbed-dispatch dispatch-service=food-delivery-dispatch:1.8.0`. F33-H, F43-P 와 같은 스크립트이고 계약(`CONTRACTS["F55-R"]`)만 더했다.
2. 배포: `food-delivery/k8s/22-dispatch-deploy.yaml:9`(replicas 1), `:18-19`(nodeSelector tb-w3), `:27-28`(image, imagePullPolicy Never), `:67-86`(startup, readiness, liveness 모두 `/actuator/health`), 기본 전략(maxSurge 25%). Service `testbed-dispatch`(ClusterIP 8082, `:95-105`)와 `testbed-dispatch-external`(NodePort 30182, `:108-120`). 109 docker 에 1.8.0 은 아직 없다(배포 단계에서 `build.sh f55-r`).
3. 결함: `scripts/scenarios/fault-images/f55-r/dispatch-service.patch:32-38`(ClientRateLimitFilter, BURST 20, REFILL_SECONDS 3), `:55-57`(shouldNotFilter: `/api/deliveries` 로 시작하지 않으면 통과, 프로브 제외), `:62`(클라이언트 키 `request.getRemoteAddr()`), `:65-78`(토큰이 없으면 30초에 한 번 WARN 'Rate limit exceeded for client ...' 와 429 본문), `:81-116`(Bucket: 3초마다 1개 보충, 상한 20), `:158-166`(ClientRateLimitFilterTest.otherClientsKeepTheirOwnLimit: 서로 다른 주소로만 시험). 매니페스트 버전에는 필터가 없다(`food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/config/` 에 GlobalExceptionHandler, RestClientConfig 뿐).
4. 호출자: order `OrderService.createOrder` 가 주문 저장 전 `DispatchClient.checkCapacity`(`food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:94-112`), 저장 뒤 `dispatchCourier`(`:143-151`)를 부른다. `DispatchClient.java:32-50` checkCapacity 는 모든 RestClientException(429 포함)을 'Failed to check dispatch capacity: ...' ERROR 와 502 로, `:55-71` dispatchCourier 는 'Failed to dispatch courier for order N: ...' ERROR 와 503 으로 바꾼다. 재시도 3회(200ms 지수), 서킷 10건 창 50%, 5초 열림, fallback 503 'Dispatch service unavailable: ...'(`order-service/src/main/resources/application.yml:55-58, 78-86, 105-111`). 배차 실패는 createOrder 트랜잭션을 되돌려 주문 행이 남지 않는다.
5. 호출자 주소: 내부 호출은 order 파드 → ClusterIP → dispatch 파드(같은 노드 tb-w3, 주소 변환 없음)라 원격 주소가 order 파드 IP 다. NodePort 30182 호출은 tb-cp(192.168.122.77)에서 tb-w3 파드로 넘어가며 kube-proxy 가 출발지를 tb-cp 의 flannel 주소 10.244.0.0 으로 바꾼다. 119 평시 스팬이 이를 그대로 보여 준다(§7).

### 로컬 실측 (2026-10-10, 원칙 9 근거)

origin/main(d50c207)의 food-delivery 를 풀어 restaurant, order 는 그대로, dispatch 는 패치를 얹어 `mvn -o package` 로 jar 를 만들고(패치를 얹은 dispatch 시험 18건 통과, ClientRateLimitFilterTest 4건 포함) JDK 21 로 띄워 mysql:8.0(food init.sql, 배차 2만 행)에 붙였다. Kafka 와 payment 는 띄우지 않았다(결제 단계 502 는 "배차까지 통과한 주문" 을 세는 표지로 쓴다). 주문 POST 를 초당 1.5건, 180초 보냈다. 로컬에서는 order 와 시험 클라이언트가 모두 127.0.0.1 이라 운영의 order 파드 주소 하나와 같은 조건이다.

| 15초 창 | 결과 |
|---|---|
| 0~15초 | 23건 중 12건이 배차까지 감(토큰 20개 소진), 11건 503 |
| 15~180초 | 창마다 22~23건 중 1~2건만 배차를 지나고 나머지 503 'Dispatch service unavailable: ...' |
| 합계 | 270건 중 503 244건, 배차 통과 26건(대부분 첫 창), 응답 p50 0.63초 |

로그(같은 jar): order ERROR 'Failed to check dispatch capacity: 429 : "{"status":429,"error":"Too Many Requests","message":"Rate limit exceeded for client 127.0.0.1"}"' 93건, 'Failed to dispatch courier for order N: 429 ...' 15건. 503 본문의 다른 꼴은 "CircuitBreaker 'dispatch' is OPEN and does not permit further calls". dispatch WARN 'Rate limit exceeded for client 127.0.0.1: N requests rejected in the last 30s (limit: burst 20, 1 request per 3s)' 6건(30초마다, N 은 1, 24, 19 …: 서킷이 열려 있는 동안 호출이 오지 않아 작다). 부하가 끝난 뒤 dispatch health 200(5ms), 용량 GET 200. dispatch 와 order 는 내내 health 200.

이미지 빌드 경로 확인: `build.sh` 와 같은 순서로 origin/main 의 food-delivery 를 git archive 로 풀고 `git apply -p1 --check` 가 통과했다. 109 docker 의 food-delivery-dispatch:1.8.0 은 배포 단계에서 `build.sh f55-r` 로 만든다.

운영과의 차이: 운영은 eclipse-temurin 17-jre, OTel 에이전트, CPU 한도 500m. 한도는 요청 수와 시간에만 달려 있어(3초에 1개) JVM 판, CPU 속도와 무관하다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `food-delivery-dispatch`(dispatch-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 호출자(order, loadgen)의 요청률은 평시 그대로 정당하다(원칙 6: 정당한 요청 A, 잘못된 로직 B → B).
- 부분 점수: `food-delivery-order`(주문 503 이 드러나는 곳).
- 층위: 원본 사후 보고의 결론("보호용으로 배포한 IP 기준 요청 한도가 모든 트래픽을 한 IP 로 보고 거절, 한도를 꺼서 복구")과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "dispatch 를 1.8.0 으로 롤아웃한 직후부터 건강한 dispatch 가 order 의 호출에 429 'Rate limit exceeded for client <order 파드 주소>' 를 돌려주고 dispatch 도 그 주소를 거절했다고 남긴다, 요청률은 평시와 같다 → 새 버전의 요청 한도가 호출자 하나에 걸림, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | food-delivery-order 오류율(503, 서버 스팬 ERROR), order ERROR 로그 급증('Failed to check dispatch capacity: 429 ...'), dispatch WARN 새 템플릿 |
| 피해 판정(러너) | 동반 부하 주문 생성 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.2 가 3틱. 평시 주문 5xx 는 0 근처(order error_rate 6시간 최대 0) |
| RCA | 롤아웃 이벤트와 새 이미지 태그, 429 본문의 'Rate limit exceeded for client 10.244.2.x', dispatch WARN 의 두 주소, dispatch Ready 와 재시작 없음, Hikari 와 MySQL 평시, 요청률 평시 |

## 7. 관측 근거 표 (119 실조회, 2026-10-10 11:30~12:00 UTC)

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), 자원 이력, 로그(전수)다. 스팬의 client.address 는 "호출자가 둘뿐" 이라는 조건을 보여 주는 보조 증거다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(reason, object_name, body) | `object_name LIKE 'testbed-dispatch%'` reason 별 수, `reason='Pulled'` 본문의 이미지별 수 | 30일: Unhealthy 1070, Pulled 47, Killing 46, Created 44, Started 42, ScalingReplicaSet 24(마지막 2026-10-09 00:27:33). Pulled 본문 이미지는 전부 'food-delivery-dispatch:latest' 49건. 장애 때 1.8.0 이 처음 나타나야 한다 |
| 계기: ReplicaSet, 파드 스펙의 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-food' AND name LIKE 'testbed-dispatch%'` 종류별 수와 `position('food-delivery-dispatch:latest' in yaml)>0` 수(READ ONLY 트랜잭션) | replicaset 40건 전부 :latest(마지막 2026-10-09 00:27:33), pod 33건 전부 :latest(마지막 2026-10-09 00:26:52). 장애 때 1.8.0 ReplicaSet 이 남아야 한다 |
| 근본: 429 와 한도 로그 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | 30일 `body LIKE '%Too Many Requests%' OR body LIKE '%Rate limit exceeded%' OR body LIKE '% 429 %'` 서비스별 | 0건(모든 서비스). 장애 때 order ERROR 'Failed to check dispatch capacity: 429 ... Rate limit exceeded for client 10.244.2.x' 와 dispatch WARN 'Rate limit exceeded for client ...' 가 처음 나타난다 |
| 전파: 호출자의 dispatch 실패 로그 | CH `lucida.lucida_logs_local` | `service_name='food-delivery-order' AND (body LIKE 'Failed to check dispatch capacity%' OR body LIKE 'Failed to dispatch courier%')` 앞 90자별 | 수집 시작(2026-10-04) 이후 602건: '... 500 : "<html><body><h1>Whitelabel Error Page...' 310, '... I/O error on GET request ...' 292(F33-R 등 실행 때), 'Error while extracting response ...' 수 건(F32-H). 429 는 0건 |
| 조건: 호출자 주소가 둘뿐 | CH `lucida.otel_traces_local`(span_kind 'SERVER', span_attributes['client.address'], ['http.response.status_code']) | 6시간 `service_name='food-delivery-dispatch'` span_name, client.address, 상태 코드별 수 | POST /api/deliveries/dispatch 10.244.2.77 200 2367, GET /api/deliveries/capacity 10.244.2.77 200 2367, GET /api/deliveries 10.244.0.0 200 630, GET /api/deliveries/{id} 10.244.0.0 404 582, GET /actuator/health 10.244.2.1 200 385. API 호출자는 order 파드(10.244.2.77)와 노드 주소 변환(10.244.0.0) 둘뿐이다. 장애 때 같은 두 주소의 스팬이 429 로 끝나야 한다 |
| 감별: dispatch 는 건강함 | VM `db.client.connections.usage{service_name="food-delivery-dispatch"}`, `pending_requests`, CH `kcm_events_local` | 위와 같음 | 평시 used 1~2, pending 0(F43-P 시트 7일 조회). 장애 동안 그대로이고 Unhealthy, Killing 이 없어야 한다(F43-P 와 가르는 근거) |
| 감별: 부하는 평시 | CH `lucida.lucida_logs_local` | dispatch 'Dispatched courier' 시간별 수 24시간 | 시간당 450~4125건(초당 0.13~1.15). 요청률이 평시와 같은데 거절이 나는 것이 한도가 새로 생겼다는 근거다 |
| 감지: order 오류율 | VM `apm.agent.otel.java.error_rate{service_name="food-delivery-order"}` | 6시간 최대, 중앙값 | 둘 다 0. 장애 때 오른다(order 가 429 를 503 으로 바꿔 서버 스팬 오류) |
| 참고: 버전 표시 | VM, CH 의 `service_version` | dispatch 지표, 로그 | 1.0.0. 릴리스는 소스만 바꾸고 pom 버전은 그대로라 1.8.0 에서도 1.0.0 이다. 버전 변화는 이미지 태그(KCM)로만 보인다 |

## 8. 감별

- must_support: 롤아웃과 1.8.0 태그(과거 기록은 전부 :latest), order 'Failed to check dispatch capacity: 429 ... Rate limit exceeded for client 10.244.2.x' 와 'Failed to dispatch courier ...: 429 ...'(30일 0건이던 꼴), dispatch WARN 'Rate limit exceeded for client ...' 의 두 주소, dispatch Ready, 재시작 없음, Hikari 와 MySQL 평시, 주문 503 비율 0.5 이상과 약 6초에 1건의 성공.
- must_rule_out: dispatch 연결 누수(F43-P), MySQL 포화(F33-R, F33-H), 배차 한도 설정, 응답 형식(F32-R, F32-H), order 쪽 릴리스(F49-H), 부하 폭주, 가게, 결제 쪽(F49-R, F36-R, F44-R, F49-P).
- contrast_with: F43-P, F33-H, F33-R, F32-R, F32-H.
- 관계: F43-P, F33-H 와 정답(dispatch 릴리스)과 증상(order 503)이 같고 결함 꼴이 다르다. 서비스 입도 채점에서는 정답이 같으므로 이 짝의 판별력은 mechanism 과 must_support(429 본문, dispatch 가 Ready 이고 빠름, 풀과 MySQL 평시)에 있다. F32-R(배차 동시 한도 설정 축소)과는 "한도에 걸린 거절" 이라는 겉모양이 같지만 F32-R 은 env 롤아웃과 업무 용량 한도('Courier pool exhausted', dispatch 가 503), F55-R 은 이미지 롤아웃과 호출자 주소 기준 요청 한도(dispatch 가 429)다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f55-r, 실행기 파라미터 = 이미지 태그). 성공 주문 상한은 보충 속도로 정해진다: order 주소의 토큰은 3초에 1개, 주문 하나에 두 개가 필요해 많아야 초당 약 0.17건. 시각과 무관하게 결정적이다.
- 동반 부하: load.north_south food order-surge.js 2rps(주문 초당 약 1.4건), ramp 2m + hold 21m + ramp_down 15s, entry 30181. F43-P, F49-H 와 같은 값. 기준선 주문(초당 0.13~1.15)과 합치면 order 의 dispatch 호출은 초당 약 3~5회로 보충 속도(초당 0.33)의 약 10배다. 한가한 시간에도 동반 부하만으로 초당 약 2.8회다.
- 2xx 예상: 성공 초당 약 0.17건을 동반 부하(1.4)와 기준선(0.13~1.15)이 나눠 가져 동반 부하 2xx 비율 0.07~0.11. 실제로는 토큰 일부가 배차 단계에서 거절될 주문의 용량 확인에 쓰이고 서킷 열림 동안 쌓인 토큰도 반열림 3건이 먼저 쓰므로 더 낮다(로컬 1.5 주문/초에서 첫 창 뒤 4~9%).
- success: order_create_5xx_rate ≥ 0.5 와 order_create_2xx_rate < 0.2, 3틱(틱 15초). 토큰 버킷이라 성공이 시간에 고르게 퍼져 매 틱 조건이 선다(분 단위 고정 창이면 창 첫머리 틱에 성공이 몰렸을 것).
- min_hold 15m, timeout 20m, max_injection_duration 25m(F43-P 와 같은 틀).
- must_rule_out: achieved_rps < 0.5(동반 부하 2rps 의 1/4, 거절이 빨라 반복률이 유지됨), food MySQL 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). 진입점 order, restaurant NodePort 는 이 주입과 무관하게 Ready 다. order 는 429 를 곧바로 받아(재시도 200ms, 400ms) 주문당 1초 안쪽으로 끝나 자기 DB 연결을 오래 쥐지 않는다(로컬 p50 0.63초). 용량 확인은 주문 저장 전이라 연결을 쥐는 것은 배차 단계까지 간 소수 주문뿐이다.
- recovery: target_health 200, MySQL Ready, 기준선 주문 2xx 비율 ≥ 0.7(2틱, 10분).
- cleanup: 이미지 원복, available(180초), 1.8.0 을 쓰는 파드가 없어진 뒤 tb-w3 containerd 에서 1.8.0 의 이름 참조와 ID 참조를 모두 지우고 둘 다 없는지 확인. 원복한 새 파드에는 한도가 없다. 데이터 부작용 없음(거절된 주문은 주문 행과 배차를 남기지 않음).
- 노드 디스크: 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 tb-w3 이미지 파일시스템 80% 미만을 요구한다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(PostHog 2025-10-24 공식 사후 보고)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 J+G(J 8→9, 71 중 12.7%), 정답 위치 배달 서비스(4→5, 7.0%), 결제 경로 12(16.9%), 서비스 음식배달 22→23(은행과 함께 가장 적던 쪽), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, order 클라이언트와 서비스)과 인프라 지점(22-dispatch-deploy.yaml, NodePort 주소 변환)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 dispatch 롤아웃과 Pulled 본문 태그, kcm_resources_history 의 dispatch ReplicaSet, 파드 이미지, 로그 표의 429 부재(새 서명), 스팬의 호출자 주소(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, 자원 이력, 전수 로그(order ERROR 의 429 본문에 호출자 주소, dispatch WARN). 스팬은 보조
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(거절은 즉시, 이유는 새 한도)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(자기 롤아웃 직후 건강한 dispatch 가 특정 호출자 주소를 429 로 거절), 코드 설계 결함 추론 불필요(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 dispatch 릴리스, 계기 같음, 부분 점수 order)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug, chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_food_dispatch_third_release` 가 확인)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 보충 속도로 정해지는 성공 상한, 서킷이 열려도 fallback 503, 로컬 실측 270건 중 503 244건, order 연결 여유)
