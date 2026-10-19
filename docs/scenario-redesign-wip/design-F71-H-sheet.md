---
title: F71-H 설계 시트 — commerce PostgreSQL 접근 규칙을 좁히며 같은 노드 서비스 파드 대역을 빠뜨려 여덟 서비스의 풀이 수명대로 빔
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - database
  - access-control
summary: 운영자가 commerce PostgreSQL 의 pg_hba.conf 에서 너무 넓던 네트워크 규칙(host all all all)을 노드 주소와 관리망만 받도록 좁히고 다시 읽혀, 같은 노드에서 자기 파드 주소로 붙는 여덟 commerce 서비스의 새 로그인이 'no pg_hba.conf entry for host' 로 거절되고 Hikari 풀이 수명(10분, 30분)대로 비어 readiness 에서 빠지고 재시작을 되풀이하는 시나리오(Harness 2024-09-01 재구성, F71-R 과 겉 증상이 같고 원인이 다른 H)의 설계 근거.
---

# F71-H 설계 시트

- id `F71-H`, slug `f71-h-commerce-postgres-access-rule-omits-app-pods`, 사례군 F71 의 H 판(겉 증상은 F71-R 과 같고 원인이 다르다)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 묶음 G(설정 오배포), 정답 위치 DB 인스턴스, 서비스 쇼핑몰

## 1. 요약과 비유

commerce 의 여덟 서비스(user, cart, pricing, shipping, product, order, payment, inventory)는 tb-w1 의 자기 파드 주소(10.244.3.0/24)에서
역할 `commerce` 로 PostgreSQL 에 네트워크 로그인을 한다. DB 의 호스트 기반 접근 파일 `pg_hba.conf` 에는 local 과 127.0.0.1 trust 말고
네트워크 규칙이 하나뿐이다: 128행 `host all all all scram-sha-256`(모든 주소). 운영자가 이 너무 넓은 규칙을 좁힌다.
"DB 에 바깥에서 오는 접속은 모두 NodePort 를 거쳐 노드 주소로 들어온다"고 보고 노드 주소 10.244.3.1(119 DPM, 109 러너가 SNAT 되어 닿는 주소)과
호스트, 관리망(192.168.122.0/24, 192.168.213.0/24, 192.168.230.0/24)만 남기고 `SELECT pg_reload_conf()` 로 다시 읽힌다.
서비스는 같은 노드의 ClusterIP Service 로 들어와 자기 파드 주소를 그대로 가지므로 어느 규칙에도 맞지 않는다.
PostgreSQL 은 pg_hba 를 연결을 시작할 때만 보므로 열린 연결은 그대로 쓰이고, 새 로그인만 비밀번호를 묻기 전에
`FATAL: no pg_hba.conf entry for host "10.244.3.x", user "commerce", database "commerce", no encryption` 으로 거절된다.
그 뒤는 F71-R 과 같은 길이다: user, cart, pricing, shipping(max-lifetime 10분)이 먼저, product, order, payment, inventory(30분)가 나중에 풀이 비어
readiness 에서 빠지고 liveness 가 재시작하며, 새 컨테이너는 기동 중 풀을 열지 못해 재시작을 되풀이한다. 게이트웨이만 남아 쓰기 500, 로그인 502, 상품 조회 502.

비유: 건물 보안팀이 "외부 손님은 모두 정문(노드)을 거쳐 온다"며 출입 명단을 정문과 관리 사무실로 좁혔다. 그런데 직원들은 지하 주차장(같은 노드의 파드 대역)으로
바로 들어온다. 이미 안에 있는 사람은 계속 일하지만, 교대하려 다시 들어오는 직원은 출입증(비밀번호)을 보여 주기도 전에 "명단에 없는 입구"라며 막힌다.
F71-R 은 출입증 날짜가 지나 막힌 경우라, 막히는 모습(교대 주기대로 사무실이 빈다)은 같지만 막는 이유와 고칠 곳이 다르다.

## 2. 원본 사례

- **Harness, 2024-09-01**, 상태 페이지 사고 "Harness cloud builds failing at initialise step for MAC users"(사후 보고 게시 2024-09-17) [공식](https://status.harness.io/incidents/bs6qp18g8l21). `docs/ref-real-world-incidents.md` M15 에 이미 있음.
  - 9월 1일 17:00 UTC: "We tightened a firewall rule for our Mac VM registry that was previously too permissive."
  - 새 규칙이 레지스트리를 쓰는 구성 요소 하나의 NAT 주소를 빠뜨림: "the new rule did not account for the NAT IP address of one of these components".
  - 그 구성 요소는 지속 소켓 연결을 유지해, 연결이 다시 맺어지거나 재시작할 때까지 영향을 받지 않아 문제가 바로 드러나지 않음.
  - 영향: 일부 고객의 macOS CI 파이프라인이 초기화 단계에서 실패. 탐지: 9월 4일 06:03 UTC 고객 보고. 완화: 9월 4일 08:39 UTC 규칙을 다시 만들고 검증.
  - 후속: 필요한 NAT 주소를 넣어 다시 좁히기, 방화벽 제한을 적용할 때 관련 서비스 재시작, 변경 시 연결을 비우고 다시 맺게 하기.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 너무 넓던 레지스트리 앞 접근 규칙(방화벽)을 좁힘 | 너무 넓던 DB 앞 접근 규칙(pg_hba `host all all all`)을 노드 주소와 관리망으로 좁히고 다시 읽힘 |
| 원인이 된 결함 | 새 규칙이 실제 클라이언트 하나의 주소(NAT IP)를 헤아리지 않음 | 새 규칙이 실제 클라이언트(같은 노드 서비스 파드, 자기 주소로 접속)를 헤아리지 않음. NAT 되어 오는 접속만 생각함 |
| 전파 경로 | 지속 연결은 유지 → 다시 맺을 때 거절 → 그 구성 요소에 기대는 단계 실패 | 열린 풀 연결은 유지 → 수명대로 물러나며 새 연결 거절 → 풀 빔 → health DOWN, readiness 이탈, liveness 재시작 → 게이트웨이 쓰기 500, 로그인 502, 상품 조회 502 |
| 사용자 증상 | 파이프라인 초기화 단계 실패 | 로그인, 장바구니, checkout 불가(약 10분 뒤), 상품 조회와 검색 불가(약 30분 뒤) |
| 원인 쪽 상태 | 레지스트리 자체는 정상, 접근 규칙이 그 클라이언트를 막음 | PostgreSQL 인스턴스, DPM 수집(노드 주소로 들어옴), local 관리 접속 정상. 서비스 파드 대역의 새 로그인만 거절 |
| 탐지된 경로 | 고객 보고(지연된 탐지) | 여덟 서비스 로그와 DB 로그의 같은 거절 문구, 풀 지표 0, DPM 세션 감소, KCM 재시작 |
| 완화와 복구 | 규칙을 다시 만들고 검증 | cleanup 이 모든 주소 규칙을 되돌리고 다시 읽힘, 재시작 반복 중인 파드가 다음 재시작에서 붙음 |

기전 확인: "너무 넓던 접근 규칙을 좁히며 실제 클라이언트의 주소를 빠뜨렸고, 지속 연결이 다시 맺어질 때에야 그 클라이언트가 실패한다"가 원본과 같다.
원본의 규칙은 네트워크 방화벽이고 여기서는 DB 가 스스로 적용하는 호스트 기반 접근 규칙이다. 둘 다 "출발지 주소에 따라 새 연결을 받거나 거절하는 허용 목록"이고
기존 연결에는 적용되지 않는 성질까지 같다. 같은 원본을 노드 방화벽(iptables)으로 재구성한 것이 F44-R(서비스 API), F72-R(Oracle 리스너)이다.
F71-H 는 엔진(PostgreSQL), 적용 지점(DB 자신의 접근 설정), 실패 모양(TCP 연결 시간 초과가 아니라 DB 가 받아서 FATAL 로 거절, DB 로그에도 남음)이 달라
카탈로그 §1 의 R 조건(DB 엔진 등 하나 이상 달리)도 넘지만, 이 시나리오의 주된 가치는 F71-R 과의 H 관계다(§3).
로컬 postgres:16-alpine 실측(2026-10-12): 모든 주소 규칙을 노드, 관리망 규칙으로 바꾸고 pg_reload_conf() 하자 열려 있던 세션은 25초 뒤에도 질의를 처리했고,
빠진 대역의 새 로그인은 클라이언트와 서버 로그 양쪽에 `FATAL:  no pg_hba.conf entry for host "...", user "commerce", database "commerce", no encryption` 으로 거절되었으며,
원래 줄을 되돌리고 다시 읽히자 로그인이 되었다. 파일 소유자(uid 70)와 권한(0600)은 그대로였다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 114 종, 이 후보 전)

- 서비스: commerce 38, core-banking 38, food-delivery 38. 셋이 동률이라 어느 것이든 된다(원칙 2, 2026-10-12 결정). **commerce 38 → 39**.
- 묶음: A 9, B 7, C 6, D 17, E 4, F 4, **G 12 → 13**, H 2, I 2, J 19, K 3, L 9, M 1, O 3, P 14, Q 2. 20% 상한(22.8)에 닿는 묶음 없음(가장 큰 J 16.7%).
  G 는 13/115 = 11.3%. 현실 근거: Google 설정 배포 31%, Ghosh 설정 19%.
- 정답 위치: **DB 인스턴스 2 → 3**(3/115 = 2.6%). 가장 많은 은행 이체 서비스 11(9.6%). 결제 경로 13/115 = 11.3%(무관).
- 왜 이 후보인가: 겉 증상이 F71-R(후보, commerce 역할 비밀번호 기한 경과)과 같다. 두 시나리오 모두 여덟 서비스의 풀이 10분과 30분 두 물결로 비고 같은 순서로 빠진다.
  "commerce 에서 서비스들이 차례로 DB 로그인에 실패하면 계정이 범인"이라는 지름길을 외운 관제 AI 는 F71-H 에서 틀린다.
  맞히려면 거절 문구(비밀번호 확인 뒤의 거절인가, 주소 규칙의 거절인가)와 그 문구 안의 host 를 읽어야 한다(카탈로그 관계 H 의 가치).
  정답 위치 DB 인스턴스는 정식 2(F25-H, F38-R) 뿐이고 둘 다 "DB 가 멈추거나 쓰기를 거절"이라 접근 설정 꼴은 처음이다.
- 이번 반복(78)의 후보 목록과 버린 이유는 §11.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 접속 주소: 서비스는 `commerce/k8s/02-configmaps.yaml:13`(DB_HOST testbed-postgres, ClusterIP Service)로 붙는다. 같은 노드에서 ClusterIP DNAT 뒤에도 출발지는 파드 주소다.
   109 읽기 전용 조회(2026-10-12 21:00 UTC): pg_stat_activity 의 commerce 연결은 10.244.3.81(10), .82(10), .83(3), .84(3), .88(10), .92(5), .95(5), .210(10) 56개.
   tb-w1 conntrack: NodePort 30432 로 오는 접속(119 DPM 192.168.230.128, 109 의 192.168.200.109·192.168.122.1)은 파드에 10.244.3.1 로 닿는다(SNAT).
2. 접근 파일: `SHOW hba_file` = /var/lib/postgresql/data/pg_hba.conf(소유자 uid 70, 0600, 2026-07-13 이후 그대로). pg_hba_file_rules: 117 local trust, 119 127.0.0.1 trust, 121 ::1 trust,
   124~126 replication trust, **128 `host all all all scram-sha-256`**. 서비스 접속이 맞는 규칙은 128행 하나다.
3. 역할과 풀: `commerce/k8s/01-secrets.yaml:9-10`(POSTGRES_USER commerce), 풀 설정은 F71-R 과 같다: user `commerce/user-service/src/main/resources/application.yml:7-17`(10, 3, 3000, max-lifetime 600000),
   cart(20, 5), pricing(15, 5), shipping(10, 3) max-lifetime 600000, product, order, payment, inventory 는 Hikari 기본값(10, max-lifetime 30분, connection-timeout 30초).
4. Hikari 동작: 연결은 max-lifetime(최대 2.5% 앞당김)에 물러나고 채우려는 시도가 거절되면 풀이 줄어든다. 풀이 0 이 되면 요청은 connection-timeout 뒤
   `HikariPool-1 - Connection is not available, request timed out after …ms (total=0, active=0, idle=0, …)` 를 받고 이 예외가 마지막 연결 실패(PSQLException FATAL)를 다음 예외로 달아
   Hibernate SqlExceptionHelper 가 ERROR 로 남긴다(같은 경로가 banking F35-R 실행에서 ORA-28000 본문으로 119 에 실제 남음).
5. 프로브: `commerce/k8s/21-product-service.yaml:79-97` 등 여덟 매니페스트 모두 startup, readiness, liveness 가 `/actuator/health`(DB 확인 포함). 풀이 비면 readiness 30초 안에 빠지고
   liveness 75초 뒤 재시작, 새 컨테이너는 기동 중 풀 초기화에서 같은 FATAL 로 'Application run failed' → CrashLoopBackOff.
6. 게이트웨이: `commerce/api-gateway/src/main/java/com/commerce/gateway/service/AuthGuard.java:31-61`(쓰기마다 user 토큰 확인), `ProxyService.java:90-105`(하류 실패 502).
7. DB 로그 수집: PostgreSQL 은 logging_collector on, log/postgresql.log 에 쓴다. tb-w1 SMS 로그 감시가 그 파일(local-path PVC `…_rca-testbed-commerce_pgdata-testbed-postgres-0/log/postgresql.log`)의
   ERROR, FATAL 줄을 119 `lucida_logs_local`(collector_kind polestar_sms, log_attributes.file_path)로 가져온다. 설계 전 7일 23줄(ERROR 22, FATAL 1) 모두 들어왔다. LOG 줄('received SIGHUP')은 가져오지 않는다.
8. 실행기: `scripts/scenarios/profiles/db_account_executor.py` `POSTGRES_HBA_REMOTE`(이번에 더한 PostgreSQL 접근 규칙 좁히기 모드). 러너에서 kubectl exec 로 파드 local 소켓 psql(trust, pg_stat_statements 기록 끔).
   preflight: pg_hba_file_rules 로 모든 주소 규칙 1·좁힌 규칙 0·파싱 오류 0, DB 파드 주소가 10.244.3.0/24 안, 역할의 네트워크 세션이 모두 10.244.3.0/24 안이고 허용 출발지 밖(있음), 파드 주소 네트워크 로그인 성공.
   run: 128행을 제자리에서 네 규칙으로 바꿈(awk 결과를 `cat` 으로 덮어써 소유자, 권한 유지) → 파싱된 규칙 확인 → `SELECT pg_reload_conf()` → 같은 네트워크 로그인이 거절될 때까지(최대 10초) 확인.
   cleanup: 첫 좁힌 규칙 자리에 모든 주소 줄을 되돌리고 나머지를 지움(이미 원상이면 편집 생략) → 다시 읽힘 → 로그인 성공까지 확인. 109 에서 preflight 실행(읽기와 로그인 한 번) 통과,
   로컬 postgres:16-alpine 에 가짜 kubectl 로 preflight → run → (preflight, run 재실행은 실패) → cleanup 두 번 → recovery → preflight 전 과정 통과, 원래 파일 크기(5743 바이트)로 돌아옴, pg_stat_statements 에 흔적 0.

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `commerce-postgres`(인스턴스). 결함을 가진 곳은 인스턴스의 접근 설정(잘못 좁힌 pg_hba)이고, 고쳐야(정당한 클라이언트 대역을 넣어야) 재발이 막힌다.
  규칙은 역할 하나가 아니라 인스턴스로 오는 모든 네트워크 접속에 걸린다.
- `trigger_target_id`: 비움. 계기(규칙 변경과 다시 읽기)가 결함과 같은 곳이다. 서비스의 로그인은 정당하다(원칙 6 의 "A 가 정당했고 B 가 잘못" 중 B = 접근 규칙).
- `scoring`: granularity service(F25-H, F38-R 과 같은 DB 정답 표기), accept `commerce-postgres`, partial `commerce-postgres:commerce`(역할만 지목, 거절 사유인 주소 규칙을 놓침),
  `commerce-user`, `commerce-product`, `commerce-order`, `commerce-payment`, `commerce-cart`(증상 서비스), `commerce-gateway`(피해 전달).
- 원본 층위: "좁힌 접근 규칙이 정당한 클라이언트의 주소를 빠뜨렸다"까지. 누가 왜 좁혔는지, 변경 절차가 무엇이었는지는 요구하지 않는다(원칙 5).
- 관제 데이터로 낼 수 있는가: 거절 문구가 'pg_hba.conf' 와 클라이언트 주소를 말하고, 같은 줄이 DB 로그에도 있으며, 여덟 서비스가 같은 문구를 내고 롤아웃이 없고 DB 는 살아 있다.
  합리적인 관제 AI 는 "commerce PostgreSQL 의 접근 규칙이 서비스 파드 주소를 거절한다"에 닿는다. 규칙이 바뀐 순간 자체는 수집되지 않으므로(§7) "언제 누가 바꿨나"는 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오에서 |
|---|---|---|
| 감지 | 겉 증상 | 약 10분부터 user, cart, pricing, shipping 의 ERROR 급증과 readiness, liveness 실패, BackOff, 게이트웨이 쓰기 500·로그인 502. 약 30분부터 product, order, payment, inventory 도 같은 꼴, 상품 조회 502. DB 로그의 FATAL 줄도 분당 수십 건. commerce 전체가 멈춰 인시던트가 생기지 않을 수 없는 크기 |
| 피해 판정 | 러너 | 기준선(loadgen-commerce) 조회 단계 비정상 비율 ≥ 0.5(평시 0 근처). 쓰기 비율은 부하 쪽 로그인 불능으로 꺼지므로 판정에 쓰지 않고 회복에만 쓴다(F71-R 과 같음) |
| RCA | 녹화 데이터 | §7 의 서비스 로그와 DB 로그 문구, 풀 지표, DPM 세션, KCM 이벤트 |

## 7. 관측 근거 표 (119 실조회, 2026-10-12 20:50~21:10 UTC)

표본 데이터(트레이스 10%, APM 골든 시그널)로 증명하지 않는다. 아래는 모두 전수 로그, OTel 런타임 지표(표본 아님), DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 | 장애 때 기대 |
|---|---|---|---|---|
| 접근 규칙 거절 문구(서비스) | CH `lucida.lucida_logs_local.body`, `service_name` | `SELECT count() FROM lucida.lucida_logs_local WHERE body ILIKE '%pg_hba%'` | 0(보존 시작 2026-10-05 이후 전체, 1366만 줄) | 여덟 서비스에서 Hikari total=0 시간 초과에 이어진 예외, 'Application run failed' 스택으로 다수, host 는 서비스마다 자기 파드 주소 |
| 같은 문구(DB 자신의 로그) | 같은 표, `collector_kind='polestar_sms'`, `log_attributes['file_path']` | `… WHERE log_attributes['file_path'] LIKE '%pgdata-testbed-postgres-0/log/postgresql.log'` (7일) | 23줄(ERROR 22: 운영자, 감시 질의의 문법 오류 등, FATAL 1: 2026-10-11 10:06 병렬 워커 종료), pg_hba 문구 0 | `FATAL:  no pg_hba.conf entry for host "10.244.3.x", user "commerce", database "commerce", no encryption` 이 서비스 파드 주소마다 |
| 빈 풀 시간 초과 | 같은 표 | `… service_name LIKE 'commerce-%' AND body ILIKE '%Connection is not available%total=0,%'` (8일) | 0 | total=0, active=0, idle=0 |
| 서비스별 풀 크기 | VM `db.client.connections.usage` | `min_over_time(sum by (service_name) ({__name__="db.client.connections.usage", service_name=~"commerce-.*"})[7d:5m])` | 7일 최저 cart 4, user 3, shipping 3, pricing 5, product 10, order 10, payment 10, inventory 9 | 10분 풀 넷이 10분 안에, 30분 풀 넷이 30분 안에 0 |
| DB 세션 | VM `dpm.postgresql.session.current_session` | `quantile_over_time(0.5, …[7d])`, `min_over_time(…[7d])` | 중앙 62, 최저 57 | 계단식으로 0 근처(DPM 등 몇 개만). DPM 은 노드 주소로 들어와 수집이 계속된다 |
| 파드 상태 | CH `lucida.kcm_events_local` | `… namespace='rca-testbed-commerce' AND (body ILIKE 'Readiness probe failed%' OR body ILIKE 'Liveness probe failed%' OR reason='BackOff')` (8일) | Unhealthy 105건, 모두 앞선 시나리오 실행 창(마지막 2026-10-11 20:27), BackOff 0 | 여덟 파드의 readiness, liveness 실패와 BackOff, 두 물결. 롤아웃, 새 ReplicaSet 없음 |
| 계기(규칙 변경, 다시 읽기) | 없음 | 파일 편집은 kubectl exec 안이라 노드 auth 로그에 안 남고, 서버의 LOG 'received SIGHUP, reloading configuration files' 는 로그 감시가 가져오지 않음(ERROR, FATAL 만). DPM Top SQL 은 실행기 세션을 기록하지 않음(pg_stat_statements.track=none) | — | 첫 흔적은 DB 로그와 서비스 로그의 거절 문구, 풀 지표와 DPM 세션의 감소 |

원칙 4 보충: 계기의 흔적은 "DB 가 자기 접근 규칙으로 서비스 주소를 거절하기 시작했다"는 문구 자체다. 문구가 원인(pg_hba 의 접근 규칙)과 거절된 출발지를 직접 말한다.
인공 지연이 아니라 실제 접근 거절이다. 원본에서도 변경은 고객 보고로 드러났다(지연된 탐지).

## 8. 감별

- must_support: 정답지 6항(서비스 로그 문구, DB 로그 문구, 풀 지표 두 물결, DPM 세션 감소, KCM 두 물결과 롤아웃 없음, 조회·쓰기 실패와 요청량 불변).
- must_rule_out: 역할 비밀번호 기한 경과나 잠금(F71-R 'password authentication failed', F35-R ORA-28000: 비밀번호 확인 뒤의 거절),
  네트워크나 노드 방화벽 차단(F72-R ORA-12170, F44-R 'Connect timed out' 과 '[FW BLOCK]': 여기서는 DB 가 받아서 거절하고 DPM 이 계속 수집),
  DB 다운(F25-H), 잠금이나 느린 질의로 꽉 찬 풀(F66-R, F67-H, F33-P: total=10), 배포나 연결 설정 오설정(F65-P, F39-R: 롤아웃 없음, 여덟 서비스), 부하 증가(동반 부하 없음).
- contrast_with: F71-R, F72-R, F65-P, F25-H(정답지 문장).
- F71-R 과 가르는 결정 근거 한 줄: 같은 두 물결이지만 문구가 'password authentication failed for user' 가 아니라 'no pg_hba.conf entry for host "<서비스 파드 주소>"' 이고 DB 로그에도 같은 줄이 있다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도라 할 값이 없다: 규칙은 서비스 대역을 받거나 안 받거나다. 피해 크기는 요청량이 아니라 풀 연결의 수명으로 정해진다. 평가 모드 `approved-fixed-f71-h` 하나.
- 시간 계산(F71-R 과 같음): 다시 읽힌 시각에 있던 연결은 모두 그 전에 만들어졌으므로 user, cart, pricing, shipping 은 10분, product, order, payment, inventory 는 30분 안에 마지막 연결이 물러난다.
  user 등은 idle-timeout 60초와 minimum-idle 이 있어 유휴분은 더 일찍 물러난다. readiness 3회(30초) 뒤 빠진다. `min_hold 31m` 뒤 3틱(45초)이라 약 31.75분에 판정이 나고,
  그때 쓰기와 로그인은 약 20분, 상품 조회는 1분 넘게 실패 중이다. `timeout 37m`, `max_injection_duration 40m`.
- 부하: 동반 부하 없음. 기준선 loadgen-commerce 는 하루 주기 2~10 요청/초이고 브라우징 65%, 검색 15% 는 로그인이 필요 없어 가장 한가한 시간에도 30초 창에 조회 약 48건이 흐른다.
  서킷브레이커는 피해를 숨기지 않는다(게이트웨이 product 서킷 폴백도 502).
- 배제: 진입점 429, commerce PostgreSQL 파드 NotReady. 중단: 진입점 연결 불가(게이트웨이는 DB 를 안 써 살아 있어야 한다).
- 회복: cleanup 뒤 재시작 반복 중인 파드는 CrashLoopBackOff 대기(최대 5분)와 기동, startupProbe(30회 x 5초 안)를 지나 붙는다. 약 7.5분이라 recovery 15m.
- F71-R 과 다른 점: 러너의 commerce DB 조회(NodePort 30432 → 10.244.3.1)는 허용 목록 안이라 고장 중에도 된다. DPM 도 같다. 그래서 "DB 는 답한다, 서비스 대역만 거절"이 관측으로 갈린다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Harness 2024-09-01, 공식 상태 페이지 링크)와 요소별 대응표, 기전이 원본과 같다(넓던 접근 규칙을 좁히며 실제 클라이언트 주소를 빠뜨림 → 지속 연결이 다시 맺어질 때 거절 → 기능 중단) (원칙 1, §2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 G 13/115(11.3%), 정답 위치 DB 인스턴스 3/115(2.6%), 결제 경로 13/115, 서비스 commerce 39·은행 38·음식배달 38, 고른 이유(§3). 어느 축도 20% 아래 (원칙 2)
- [x] 근본 원인 위치: 109 pg_hba_file_rules 128행, hba_file 경로, pg_stat_activity 출발지, tb-w1 conntrack, 02-configmaps.yaml:13, 풀 설정 줄 (G1, §4)
- [x] 근본 원인 흔적: CH lucida_logs_local 의 서비스 로그 문구(평시 0)와 DB 로그 문구(SMS 로그 감시 경로 확인, 평시 0), VM 풀 지표, DPM 세션 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 전수 로그 두 갈래, OTel 런타임 지표, DPM, KCM (원칙 3)
- [x] 계기 흔적: DB 와 서비스 양쪽의 접근 규칙 거절 문구, 풀과 세션 감소. 인공 지연 없음 (원칙 4, §7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(DB 접근 규칙이 서비스 주소를 거절)이다. 코드 설계 결함 추론 불필요 (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 실행기 스크립트와 인자, pg_hba 에 쓰는 줄에 id 없음(테스트로 고정), pg_stat_statements 기록 끔 (원칙 8)
- [x] 부하와 서킷브레이커를 고려한 피해 계산(§9) (원칙 9)

## 11. 후보 목록 (반복 78, 2026-10-12)

숫자(원칙 2): 세 서비스 동률 38, 0 인 묶음 N, 적은 묶음 M 1, H·I·Q 2, K·O 3. 0 인 부품 Kafka(세 도메인), nginx(commerce, banking), 정답 위치 0: 메시지 브로커, DNS.
상한에 닿은 묶음, 정답 위치 없음(가장 큰 J 16.7%, 은행 이체 서비스 9.6%).

| 순 | 후보(실제 기전 × 부품) | 원본 | 결과 |
|---|---|---|---|
| 1 | commerce PostgreSQL 접근 규칙(pg_hba)을 좁히며 같은 노드 서비스 파드 대역을 빠뜨림(G, DB 인스턴스) | Harness 2024-09-01 공식 | **채택(F71-H)**. F71-R 과 같은 겉 증상, 다른 원인(H) |
| 2 | commerce payment → 외부 결제 mock(tb-w1, 1080) 송신을 노드 방화벽으로 막음(K, 노드) | Central 1 2025-04-09, Ex Libris 2019-07-09 | 버림(카탈로그 §1, 같은 주입): mock 이 같은 노드 파드라 rejected 185 의 food 판과 같은 벽(F44-R 의 포트 교체), commerce 쪽에서 보면 F44-P(payment → banking transfer 연결 시간 초과, 같은 원본)와 실패 모양이 같다 |
| 3 | commerce 큰 표 인덱스 생성 실패(INVALID) 뒤 옛 인덱스 삭제(F, DB 테이블) | Vapi 2024-10-02 공식 | 버림(원칙 9): 109 pg_stat_user_indexes 실측에서 사용자 경로의 큰 표 조회가 모두 PK(orders, payments, shipments)이고 order_items, payment_logs, shipment_events 의 보조 인덱스는 idx_scan 0~3. 지울 만한 고빈도 보조 인덱스는 users_email_key(작은 표)와 auth_tokens(F33-P) 뿐 |
| 4 | food MySQL 앱 계정 호스트를 '%' 에서 관리망으로 좁힘(G, DB 계정) | Harness 2024-09-01 | 버림(컨트롤러 필수 중단 조건): 네 서비스가 한 계정이라 order, restaurant(food 진입 NodePort) 풀이 비어 entry 0(rejected 의 food 계정 잠금, 비밀번호 만료 판과 같은 벽) |
| 5 | DB 권한 정합화(reconciliation)가 선언 안 된 권한을 회수(G, DB 계정) | Inngest 2026-07-16 공식 RCA | 버림(원칙 9, 1): food 는 DB 단위 GRANT 라 회수가 기존 세션에 다음 USE 전까지 안 먹고, commerce 역할은 슈퍼유저, banking 은 스키마 소유자라 회수할 권한이 없음. 원본은 메시지 브로커 권한(Kafka ACL)인데 testbed Kafka 는 인가기가 없고 outbox 비동기라 사용자 경로 증상 없음 |
| 6 | 엄격한 요청 검증 릴리스가 선언 밖 필드를 거절(L+J) | Buttondown 2026-06-14 공식 | 버림(원칙 1): commerce, banking 의 서비스 사이 요청 DTO 가 모두 공유 레코드라 보내는 쪽이 선언 밖 필드를 보내지 않음(commerce payment → banking transfer 도 같은 네 필드). rejected 302 의 food 판과 같은 벽 |
| 7 | 항목마다 새 연결을 여는 결함으로 임시 포트 고갈(J+A) | Bluesky 2026-04 공개 사후 보고(Lorin Hochstein 정리) | 버림(원칙 9, 1): TIME_WAIT 60초에 같은 목적지로 초당 약 470개 넘는 새 연결이 필요한데 부하 상한 180rps, 원본은 큰 배치 호출자 + 동시성 제한 없는 끝점 + 연결 풀 없음의 복합 |
| 8 | 노드 MTU 변경으로 큰 패킷만 버려짐(K, 노드) | LiveKit 2025-09 상태 페이지 | 버림(원칙 1, 9): 원본은 UDP SIP 게이트웨이의 패킷 크기 한도인데 testbed 통신은 TCP 라 MSS 가 맞춰지고 VXLAN 바깥 패킷은 쪼개져 실패가 성립하지 않음 |
| 9 | DEBUG 로그 켜진 채 디스크 가득(A) | GitLab 2024-04 production#17848 | 버림(원칙 1): 컨테이너 로그는 kubelet 이 회전시켜 노드 디스크가 차지 않고, 앱이 파일로 쓰는 로그가 없음 |
| 10 | 롤백한 옛 버전이 새 버전이 쓴 상태값을 못 읽음(J) | 공식 사후 보고 없음(웹 검색 1회, 개인 글뿐) | 버림(원칙 1) |
| 11 | 비밀번호 해시 비용 상향 릴리스로 로그인 CPU 포화(J+A) | 공식 사후 보고 없음(웹 검색 1회) | 버림(원칙 1) |
