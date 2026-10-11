---
title: F65-R 설계 시트 (banking transfer 일상 롤아웃이 낡은 DB 연결 설정을 다시 적용해 교체된 파드가 Oracle 에 붙지 못함)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - config-deploy
  - config-drift
summary: banking transfer-service 의 일상 롤아웃이 인프라 코드에 남아 있던 옛 Oracle 연결 서비스 이름(DB_SERVICE=XEPDB1)을 명시 env 로 다시 넣어 공용 ConfigMap 의 FREEPDB1 을 덮어쓰고, maxSurge 0 이라 하나뿐인 파드가 먼저 내려간 뒤 새 파드가 ORA-12514 로 DB 에 붙지 못해 Ready 가 되지 않아 transfer 가 비고 banking 이체와 commerce checkout 이 502 가 되는 시나리오(E2B 2026-06-25 재구성)의 설계 근거.
---

# F65-R 설계 시트

## 1. 요약

banking 의 네 서비스(api, account, transfer, ledger)는 Oracle 연결 정보(DB_HOST testbed-oracle, DB_PORT 1521, DB_SERVICE FREEPDB1)를 공용 ConfigMap `service-config` 에서 `envFrom` 으로 받는다. transfer-service 의 JDBC URL 은 `jdbc:oracle:thin:@//${DB_HOST}:${DB_PORT}/${DB_SERVICE}` 다.

일상 인프라 갱신 하나가 인프라 코드에 남아 있던 transfer 배포 정의를 다시 적용한다. 그 정의는 Oracle 연결 서비스 이름을 옛 값 `XEPDB1` 로 고정해 두고 있었다(운영 쪽 연결 설정은 손으로 바뀌었는데 인프라 코드에는 반영되지 않은 드리프트). 컨테이너 env 에 `DB_SERVICE=XEPDB1` 한 줄이 들어가고, 명시 env 는 envFrom 보다 앞서므로 transfer 의 JDBC URL 만 리스너가 모르는 서비스를 가리킨다.

transfer Deployment 의 정본 전략은 maxSurge 0 / maxUnavailable 1 이다. 롤아웃은 하나뿐인 옛 파드를 먼저 내리고 낡은 설정을 읽는 새 파드로 바꾼다. 새 컨테이너는 Spring Boot 를 띄우지만 모든 연결이 Oracle 리스너에서 `ORA-12514: Cannot connect to database. Service XEPDB1 is not registered with the listener` 로 거절된다. `/actuator/health` 의 db 가 DOWN(503)이라 기동 프로브가 실패하고 파드는 Ready 가 되지 못하며 150초마다 다시 뜬다. testbed-transfer Service 엔드포인트가 비어 account 의 이체, api 의 이체와 거래 내역, commerce-payment 의 정산 이체가 모두 거절되어 banking 이체 502, commerce checkout 502 가 된다. Oracle, BANKING 계정, account, ledger 는 정상이다.

비유: 은행 지점 하나가 본사 전산실 주소록을 매주 자동으로 받아 쓴다. 전산실이 이사하면서 새 주소를 지점 게시판에는 붙였지만 자동 배포되는 주소록 원본은 고치지 않았다. 다음 정기 배포 날 옛 주소록이 다시 내려오자 그 지점 창구 직원(하나뿐)이 교대하면서 새 직원은 옛 주소로만 전화를 건다. 전산실은 멀쩡하고 다른 지점은 게시판 주소로 잘 통화하는데, 이 지점만 문을 열지 못한다.

## 2. 원본 사례

- 기업: E2B(코드 실행 샌드박스 클라우드)
- 날짜: 2026-06-24 20:26~20:55 PDT(2026-06-25 03:26~03:55 UTC, 약 29분). 사후 보고 "Service Disruption Postmortem" 2026-06-25, 상태 페이지 사고 "Investigating 503 status code on API"(전면 중단)
- 링크: [공식 사후 보고](https://status.e2b.dev/incidents/01KVYDD98RNPD48XQ9RREZFPN3/write-up)
- 요약(출처가 말한 것만): 근본 원인은 운영 환경과 인프라 코드(IaC) 사이 설정 드리프트다. 6월 4일 일상 운영 중 운영 DB 자격 증명을 회전했는데 운영에 직접 적용하고 IaC 에는 반영하지 않았다. 6월 25일 일상 인프라 갱신이 낡은 IaC 설정을 다시 적용해 "published a new version of the database secret with an outdated connection string" 했고, API 인스턴스가 새 비밀값을 읽어 DB 에 연결하지 못했다. 비밀값 갱신이 모든 API 인스턴스의 동시 재시작을 일으켜 "every instance reloaded the bad configuration at once, leaving no healthy instances and taking the service down". API, 샌드박스 생성, 대시보드가 모두 사용 불가였다. 당직이 잘못된 DB 비밀값을 찾아 올바른 연결 문자열을 되돌리고 연결을 확인한 뒤 복구했다. 데이터 유실 없음. 탐지 경로, 비밀값 이름, 낡은 연결 문자열의 내용은 적혀 있지 않다. 후속: 설정, 비밀값 변경의 단계적 롤아웃과 health 확인, 운영 변경의 즉시 IaC 반영, 실제 DB 연결을 보는 health check, 비밀값 갱신과 재시작 분리.
- 현실 비중: 설정 배포는 Google 포스트모템 트리거의 31%(M1), Microsoft Teams 연구에서 배포 오류의 55% 가 인증서 만료나 회전 실수(M7)다. 이 사례를 자료 문서 M7 에 더했다(2026-10-11 웹 조사).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 일상 인프라 갱신이 낡은 IaC 설정을 다시 적용 | transfer Deployment 의 일상 롤아웃이 인프라 코드의 낡은 배포 정의를 다시 적용: 컨테이너 env 에 `DB_SERVICE=XEPDB1` |
| 원인이 된 결함 | 운영에서 손으로 바꾼 DB 접속 정보가 IaC 에 반영되지 않은 드리프트. 다시 적용된 비밀값의 DB 연결 문자열이 낡음 | 운영 쪽 연결 설정(ConfigMap 의 FREEPDB1)이 인프라 코드의 transfer 정의에 반영되지 않은 드리프트. 다시 적용된 값이 연결 문자열의 서비스 이름을 옛 값 XEPDB1 로 덮어씀 |
| 전파 경로 | 비밀값 갱신이 모든 인스턴스를 한꺼번에 재시작 → 모두 나쁜 설정을 읽어 건강한 인스턴스 없음 → DB 연결 불가 → API 503 | maxSurge 0 / maxUnavailable 1 이 하나뿐인 인스턴스를 먼저 내림 → 새 인스턴스가 낡은 설정으로 DB 연결 불가(ORA-12514), health DOWN 으로 Ready 못 됨 → 엔드포인트 없음 → 호출자들 Connection refused → 502 |
| 사용자 증상 | API, 샌드박스 생성, 대시보드 전면 불가(503) | banking 이체와 거래 내역 502, commerce checkout 502(은행 정산 실패). 잔액 조회, 계좌 목록은 정상 |
| 탐지 경로 | 보고에 없음. 당직이 잘못된 DB 비밀값을 찾음 | account, api 오류율과 ERROR 로그 급증, transfer 의 ORA-12514 오류 로그와 기동 프로브 실패 이벤트, KCM 롤아웃 이벤트와 새 ReplicaSet 스펙의 명시 DB_SERVICE |
| 완화와 복구 | 올바른 연결 문자열을 되돌리고 연결 확인 뒤 복구 | cleanup 이 env 배열을 원래대로 되돌리고 롤아웃 완료(새 파드가 FREEPDB1 로 붙어 Ready)까지 기다림 |

기전 대조: 원본의 고리는 "운영 직접 변경과 IaC 의 드리프트 → 일상 갱신이 낡은 DB 연결 설정을 다시 적용 → 모든 인스턴스가 한꺼번에 그 설정으로 재시작 → DB 에 붙지 못해 건강한 인스턴스가 없음 → 서비스 전면 불가 → 올바른 연결 설정을 되돌려 복구"다. 재구성도 같은 고리다. 대상이 E2B API 에서 banking transfer 로, 비밀값 객체에서 배포 env 로 바뀐 것은 스택에 맞춘 운반 수단의 차이다.

원본과 다른 점 한 가지를 밝혀 둔다. 원본에서 드리프트가 생긴 계기는 자격 증명 회전이고, 사후 보고는 낡은 연결 문자열의 어느 부분이 틀렸는지 적지 않았다. 재구성은 연결 문자열 가운데 서비스 이름을 낡은 값으로 둔다. 비밀번호를 낡은 값으로 두면 Oracle 의 실패 로그인 지연(`_sys_logon_delay=1`, 109 sysdba 조회 2026-10-11)이 같은 BANKING 계정으로 로그인하는 account, ledger 의 새 연결에 번질 수 있어 피해 범위가 설계로 정해지지 않기 때문이다. 서비스 이름이 틀리면 리스너가 인증 전에 거절해 로그인 시도 자체가 없다. "연결 문자열이 낡아 DB 에 붙지 못한다"는 원본 사실은 그대로다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 84, 이 후보 전)

- 묶음(정식 + 후보): J 14(16.7%), P 10, G 9(10.7%), D 8, A 7, B 7, L 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 20%(84 기준 16.8)에 닿은 묶음은 없다. 이 후보는 **G**(9 → 10, 85 중 11.8%). 현실에서 두 번째로 흔한 트리거(설정 배포 31%)다. 원본의 드리프트 기원은 자격 증명 회전(O)이지만 재구성에서 틀린 값은 연결 서비스 이름이라 인증 실패가 아니어서 O 로 세지 않는다.
- 정답 위치(정식 + 후보): 주문 서비스 8(9.5%)이 가장 많다. 이 후보는 **은행 이체 서비스**(6 → 7, 85 중 8.2%). 결제 경로 합계 12/84(14.3%)는 이 후보와 무관하다(정답이 결제 쪽이 아니다).
- 서비스(정식 + 후보, 문제가 시작된 서비스): commerce 29, core-banking 28, food-delivery 27. 이 후보는 banking(28 → 29). 음식배달이 가장 적지만 같은 원본의 food 판은 막혔다(§10 후보 목록 2번: food 서비스는 기본 롤링이라 옛 파드가 남아 피해가 없고, 원본의 "모든 인스턴스 동시 재시작"을 재현할 수단이 파드 강제 삭제뿐이다).
- 왜 이 후보인가: 실제 장애 원인 상위(설정 배포)이고, 같은 transfer "엔드포인트가 빔" 증상을 내는 정식 F17-H(이미지 부재), F17-R(readiness 오설정)과 원인이 다른 H 관계라 관제 AI 가 증상이 아니라 지문(새 ReplicaSet 의 명시 env, transfer 자신의 ORA-12514, 기동 프로브 503 과 재시작)으로 원인을 갈라야 한다. F35-R(DB 쪽 계정 잠금)과도 "ORA 오류가 난다"는 겉모습은 같고 범위가 다르다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: Deployment `testbed-transfer`(rca-testbed-banking, replicas 1, `core-banking/k8s/22-transfer-service.yaml:14-18` strategy maxSurge 0 / maxUnavailable 1) 컨테이너 `transfer-service` 의 env 배열에 `{"name":"DB_SERVICE","value":"XEPDB1"}` 한 줄이 더해진 롤아웃. 평시 DB_SERVICE 는 `envFrom service-config`(`22-transfer-service.yaml:40-42`)에서 오고 값은 FREEPDB1(`core-banking/k8s/02-configmaps.yaml:8-10`)이다. 명시 env 가 앞선다.
2. 옛 파드 먼저 내림: maxSurge 0 이라 컨트롤러가 옛 ReplicaSet 을 0, 새 ReplicaSet 을 1 로 같은 초에 바꾼다(F17-H 실행 2026-10-11 03:23:12 실측과 같은 꼴). 옛 파드가 엔드포인트에서 빠진다.
3. 연결 실패: 새 컨테이너의 `spring.datasource.url`(`core-banking/transfer-service/src/main/resources/application.yml:8`)이 `jdbc:oracle:thin:@//testbed-oracle:1521/XEPDB1` 이 된다. 리스너는 FREEPDB1 만 알아 모든 연결을 인증 전에 ORA-12514 로 거절한다. 104 실측(같은 소스 jar, DB_SERVICE=XEPDB1, 109 리스너): 앱은 11.5초에 Started, Hibernate 가 기동 때 HHH000342 'Could not obtain connection to query metadata' 를 남기고 계속 뜬다(dialect 를 명시해서), 이후 OutboxRelay(`core-banking/shop-common/src/main/java/com/corebanking/common/outbox/OutboxRelay.java:56`, 2초 주기)와 다른 배치가 WARN 'SQL Error: 12514, SQLState: 08006', ERROR 'ORA-12514 ...', ERROR 'Unexpected error occurred in scheduled task' 를 약 1초마다 남긴다.
4. Ready 못 됨: `/actuator/health` 가 db DOWN 으로 503(실측 응답 약 2.1초). startupProbe(`22-transfer-service.yaml:81-86`, /actuator/health, 5초 × 30, 매니페스트에 timeoutSeconds 없음 = 1초)가 503 또는 시간 초과로 실패하고, 30회(150초) 뒤 kubelet 이 컨테이너를 죽이고 다시 띄운다(CrashLoopBackOff 백오프가 더해짐). readinessProbe(`87-93`)도 같은 health 라 Ready 가 되지 않는다. liveness 는 db 와 무관하지만 기동 프로브가 끝나지 않아 돌지 않는다.
5. 엔드포인트 빔: testbed-transfer Service(와 NodePort 30282)에 엔드포인트가 없어 kube-proxy 가 연결을 거절한다.
6. 호출자 실패: account 의 이체 실행(`core-banking/account-service/src/main/java/com/corebanking/account/client/TransferClient.java:28-58`)이 'Transfer service call failed ...: I/O error on POST request for "http://testbed-transfer:8082/api/transfers": Connection refused', 재시도 3회, 서킷 transferClient 열림 끝에 502. api 는 그 502 를 받아 502(이체), 거래 내역 GET 도 transfer 로 바로 가서 Connection refused 로 502. commerce-payment 의 정산 이체가 거절되어 commerce checkout 502(F17-H 정식 녹화에서 확인된 같은 경로).
7. 정상인 것: nginx 가 /api/accounts 를 account 로 바로 보내 잔액 조회와 계좌 목록은 account → Oracle FREEPDB1 로 200. ledger 는 Kafka 소비와 Oracle 쓰기를 계속한다(이체가 줄어 소비량만 준다). Oracle, 리스너, BANKING 계정은 그대로다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `core-banking-transfer`(target_kind config). 결함을 가진 곳은 transfer 의 배포 설정(env DB_SERVICE)이고 고쳐야 재발이 막히는 곳도 그곳이다.
- 계기(`trigger_target_id`): 비움. 결함을 드러낸 사건(롤아웃)도 transfer 배포 자신이다.
- 부분 점수(`scoring.partial`): core-banking-account, core-banking-api(오류를 내는 호출자), commerce-payment(정산 실패 증상). 원인 근처지만 결함이 없는 곳이다.
- 원칙 6: 호출자들의 요청은 정당하고 Oracle 은 다른 서비스에 정상 응답한다. 연결을 망가뜨린 것은 transfer 자신의 배포 설정이다.
- 원칙 5: 정답은 KCM 의 롤아웃 이벤트와 새 ReplicaSet 스펙의 env 한 줄 차이, 그 직후 transfer 로그에 찍힌 'Service XEPDB1 is not registered with the listener' 라는 전수 근거 쌍으로 낸다. 코드 설계 결함 추론이 필요 없다. 왜 인프라 코드에 옛 값이 남았는지(드리프트)는 원본처럼 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 누가 | 무엇으로 | 이 시나리오에서 |
|---|---|---|---|
| 감지 | lucida-next | account, api 서버 스팬 5xx(골든 시그널 오류율), ERROR 로그 급증, transfer 오류 로그 | 평시 오류율 0(VM `apm.agent.otel.java.error_rate` 3일 99백분위 account 0, transfer 0. api 는 3일 안에 다른 시나리오 실행 구간이 있어 83). 같은 경로(transfer 빈 엔드포인트)인 F17-H 가 정식 녹화에서 인시던트를 만들었다(2026-10-11, case-f17-h-v3-632a04d1) |
| 피해 판정 | 러너 | 동반 부하 이체(step transfer) 5xx 비율 | 평시 0(업무 거절은 400), 고장 중 약 1.0. 잔액 조회 실패율은 0 그대로 |
| 원인 설명 | RCA | §7 의 KCM 이벤트, ReplicaSet 스펙(PG), transfer 오류 로그(전수), 호출자 로그 | 근본(명시 DB_SERVICE)과 그 결과(ORA-12514 XEPDB1)가 모두 전수 데이터에 남는다 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 05:00~05:30 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 KCM(이벤트 전수, ReplicaSet 스펙 전수)과 로그(전수)다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 연결 설정 | PG lucida `kcm_resources_history` (kind, namespace, name, yaml, captured_at) | `kind ILIKE 'replicaset' AND namespace='rca-testbed-banking'` 전체 수, `yaml ILIKE '%DB_SERVICE%'` 수 | banking ReplicaSet 303건 중 명시 DB_SERVICE 0건(값은 늘 ConfigMap). 마지막 기록 2026-10-11 03:38:22 |
| 근본: 스펙이 실제로 잡히는가 | 같은 표 | `name LIKE 'testbed-transfer-%'` 최근 5건, `yaml ILIKE '%APP_USER_PASSWORD%'` | testbed-transfer-74b498b67b, -598d85f5f4 가 롤아웃마다(2026-10-11 03:23, 03:38) 잡히고 yaml 에 env 배열(Secret 참조 줄 포함)이 담김 |
| 계기: 롤아웃 | CH `kcm_events_local` (namespace, object_kind, object_name, reason, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-transfer%'` reason 별 수 | ScalingReplicaSet 136, SuccessfulCreate 68, Scheduled 68, Started 66, Killing 36 등. 롤아웃 이벤트 모양이 수집된다(마지막 2026-10-11 03:38) |
| 전파: 기동 프로브 실패 | 같은 표 | `body ILIKE 'Startup probe failed%'`, `...statuscode: 503%`, `'%failed startup probe%'` 네임스페이스별 | 'Startup probe failed' 는 수집된다(banking 381, commerce 2,946, food 693, 모두 기동 중 dial tcp 거절). 503 으로 실패한 것과 'failed startup probe' 로 죽인 것은 전 네임스페이스 0건 → 고장 중 처음 나타나야 한다 |
| 근본 결과: transfer 오류 로그 | CH `lucida_logs_local` body, `log_attributes['exception.stacktrace']` | 30일(보존 시작 2026-10-04), 전 서비스 `ILIKE '%ORA-12514%'`, `'%XEPDB1%'` | 둘 다 0건 |
| 기동 로그가 수집되는가 | 같은 표 | core-banking-transfer 2026-10-11 03:37:30~03:39:30 | 'Starting TransferServiceApplication', 'HikariPool-1 - Starting...', 'HHH000204', 'Started TransferServiceApplication in 30.099 seconds' 까지 기동 로그가 다 들어온다 |
| 같은 꼴 로그가 수집되는가 | 같은 표 | core-banking-* 3일, body 'SQL Error:%', 'Unexpected error occurred in scheduled task%' | WARN 'SQL Error: 28000, SQLState: 99999' 800, ERROR 'Unexpected error occurred in scheduled task' 126(F35-R 계열 실행). Hibernate SqlExceptionHelper 와 배치 예외 로그가 수집된다 |
| 전파: 호출자 로그 | 같은 표 | core-banking-account, core-banking-api, commerce-payment `ILIKE '%testbed-transfer%Connection refused%'` 날짜별 7일 | 2026-10-09(api 1,072, account 44, payment 1), 2026-10-11(account 910, api 1,018). 둘 다 시나리오 실행 구간(F17-H 등)이고 그 밖의 날은 0 |
| 골든 시그널(감지) | VM `apm.agent.otel.java.error_rate` | `quantile_over_time(0.99, ...[3d])` account, api, transfer | account 0, transfer 0, api 83(3일 안의 실행 구간) |
| 배제: Oracle, 계정 | 109 sysdba 읽기 조회 | dba_users, dba_profiles, v$session | BANKING OPEN, DEFAULT 프로필 FAILED_LOGIN_ATTEMPTS UNLIMITED, BANKING 세션 11. `_sys_logon_delay=1`, `sec_max_failed_login_attempts=3`(그래서 비밀번호가 아니라 서비스 이름을 낡게 둔다, §2) |

## 8. 감별

- must_support(정답지 5항목): 롤아웃 이벤트와 새 ReplicaSet 스펙의 명시 DB_SERVICE=XEPDB1, transfer 자신의 WARN 'SQL Error: 12514' 와 ERROR 'ORA-12514 ... Service XEPDB1', 기동 프로브 503 실패와 재시작, account/api/commerce-payment 의 testbed-transfer Connection refused 와 502, 동반 부하 이체 5xx 와 잔액 조회 정상.
- must_rule_out(정답지 6항목): Oracle 이나 BANKING 계정 장애(F35-R, F25-H 꼴), 이미지 부재(F17-H), readiness 프로브 오설정(F17-R), 결함 있는 새 코드, account/api 장애, 부하 증가.
- contrast_with: F17-H(같은 빈 엔드포인트, 컨테이너가 안 뜸), F17-R(프로브 경로 404), F35-R(DB 쪽 계정 잠금, 전 서비스 ORA-28000), F39-R(같은 '명시 env 가 ConfigMap 값을 덮어쓰는' 꼴이지만 account 의 하류 HTTP 주소, account 는 Ready).
- 러너 must_rule_out: 동반 부하 미전달(achieved_rps<1.25), 잔액 조회 실패율≥0.2(account 나 Oracle 장애면 오른다).

## 9. 러너 판정 조건, 강도, 부하 계산 (원칙 9)

- 강도는 하나다: env 한 줄(DB_SERVICE=XEPDB1). 값이 맞느냐 틀리느냐의 문제라 사다리가 없다. 설계 강도 1단 고정 evaluation(`approved-fixed-f65-r`), min_hold 15m, settle 60s, timeout 20m, max_injection_duration 25m(F17-H 와 같음).
- 피해가 실제로 나는 계산: maxSurge 0 이라 패치 순간 옛 파드가 엔드포인트에서 빠지고, 새 파드는 db DOWN 이라 Ready 가 될 길이 없다(재시작해도 같은 env). 그러니 transfer 를 지나는 모든 호출이 고장 내내 거절된다. 서킷이나 재시도가 피해를 흡수할 하류가 없다. F17-H 정식 녹화(같은 transfer 빈 엔드포인트)에서 동반 부하 이체 5xx 1.0 이 25틱 이어졌다.
- 부하: 동반 north-south 5rps(core-banking transfer-heavy-surge.js, 이체 40%, 잔액 35%, 거래 내역 15%, 계좌 목록 10%, ramp 2m, hold 21m), 상주 기준선(loadgen-banking) 위에 더한다. 상한 180 의 3%.
- success: 동반 부하 이체 5xx 비율≥0.5, 3틱 연속.
- abort: banking 진입점(nginx → api) 0(연결 불가) 2틱. nginx, api, account 는 고장 중에도 살아서 502 로 답한다.
- recovery: target_health 200, transfer 파드 Ready, 가용 1, 기준선 이체 5xx<0.05 2틱(timeout 10m).
- cleanup: k8s.env 가 원래 env 배열을 되돌리면 다시 롤아웃되어(maxSurge 0) DB_SERVICE 가 없는 새 파드가 ConfigMap 의 FREEPDB1 로 붙는다(JVM 기동 약 30~45초, 실행기 대기 상한 180초). progressDeadlineSeconds 600 이 지나 Deployment 에 ProgressDeadlineExceeded 가 남아도 실행기의 healthy() 는 레플리카 수만 보므로 영향이 없다(F18-P 주석).
- Oracle 부담: 새 파드의 연결 시도는 리스너에서 바로 거절되는 짧은 TCP 연결이다(약 1초에 몇 번). 세션이 생기지 않고 BANKING 로그인도 없다.

## 10. 후보 목록과 고른 이유 (3단계, 버린 이유 포함)

| # | 실제 기전 × 부품 | 원본 사례 | 결과 |
|---|---|---|---|
| 1 | 낡은 DB 연결 설정 재적용 × banking transfer(maxSurge 0) | E2B 2026-06-25 공식 | **채택**(G 9, 은행 이체 서비스 6, banking 28) |
| 2 | 같은 기전 × food payment / dispatch | E2B 2026-06-25 | 원칙 9: food 서비스는 기본 롤링(maxSurge 1)이라 Ready 가 못 된 새 파드 옆에 옛 파드가 남아 피해가 없다. 원본의 "모든 인스턴스 동시 재시작"은 파드 강제 삭제로만 재현되어 계기가 원본과 달라진다 |
| 3 | 같은 기전에서 낡은 값을 비밀번호로 × banking transfer | E2B 2026-06-25 | 설계 단계에서 바꿈: Oracle `_sys_logon_delay=1` 이라 실패 로그인 지연이 같은 BANKING 계정의 다른 서비스 로그인에 번질 수 있어 피해 범위가 정해지지 않는다(1번으로 대체) |
| 4 | 연결 풀러 경유로 바꾸는 설정 변경 × DB 연결 | dbt Cloud 2025-04-22, Tuist 2026-04-07 | 원칙 1: 테스트베드에 연결 풀러(PgBouncer 등)가 없어 기전이 성립하지 않는다 |
| 5 | DB 파라미터 변경 + 장애 조치 재부팅 × food MySQL | Mews 2025-08-27 상태 페이지 | 컨트롤러 필수 중단 조건: food MySQL 재부팅은 order, restaurant(진입 서비스)를 함께 멈춰 entry 0 |
| 6 | 유지보수 작업 + 시간 제한 누락으로 스레드 고갈 × banking api | CircleCI 2026-07-02 | 원칙 1: 확인한 출처가 2차 분석 글이고 원본이 두 요소(유지보수 부하, 시간 제한 누락) 복합 |
| 7 | 정리 작업이 공유 권한 DB 를 포화 × banking Oracle | GitHub 2026-09-13 | 원칙 1: 확인한 출처가 2차 분석 글, 같은 M20 꼴은 F59-R, F59-P 가 있다 |
| 8 | 폐기 API 가 지나친 양을 읽어 DB 과부하 × banking transfer 거래 내역(페이지 무시) | OpenNode 2024-05-14 공식 | 순위가 1번 아래(정답 위치와 서비스가 같고 묶음 J 14 로 G 보다 많음). 설계하지 않음 |
| 9 | 캐시 저장소 축출 정책 오설정 × commerce cart Redis | Gleap 2026-06 | 원칙 7: cart 가 Redis 쓰기 실패를 삼키고 DB 로 폴백해 사용자 증상이 없다(rejected 37 과 같은 벽). commerce 는 최다 서비스이고 캐시 정답 위치도 0 이 아니다 |

## 11. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(E2B 2026-06-25, 링크)와 요소별 대응표가 있고 기전이 같다. 낡은 값의 위치(서비스 이름)를 고른 이유를 밝혔다(§2) (원칙 1)
- [x] 분류 장부를 갱신했고 묶음 G(9 → 10, 11.8%), 정답 위치 은행 이체 서비스(6 → 7, 8.2%), 결제 경로 12/84(14.3%, 무관), 서비스 banking 28 → 29 를 적었다. 어느 축도 20% 에 닿지 않는다(§3) (원칙 2)
- [x] 근본 원인 위치: `core-banking/k8s/22-transfer-service.yaml:40-42`(envFrom), `02-configmaps.yaml:8-10`, `application.yml:8`(JDBC URL), 전략 `22-transfer-service.yaml:14-18`(§4) (G1)
- [x] 근본 원인의 흔적이 119 에서 조회됐다: PG `kcm_resources_history` ReplicaSet yaml(평시 banking 303건에 명시 DB_SERVICE 0건, env 배열이 실제로 잡힘), CH `lucida_logs_local`(ORA-12514, XEPDB1 평시 0건, 기동 로그와 SqlExceptionHelper 로그 수집 확인)(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트, ReplicaSet 스펙, 로그 모두 전수다 (원칙 3)
- [x] 계기의 흔적: testbed-transfer 롤아웃 KCM 이벤트와 새 ReplicaSet 스펙, 기동 프로브 실패와 재시작 이벤트. 인공 지연을 쓰지 않는다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 + env 한 줄 + 그 값을 그대로 담은 ORA-12514 (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: env 값 XEPDB1 은 실제 쓰이던 PDB 이름 꼴이고, 부하 태그는 기존 관례(k6 안에만)다 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 나는 계산이 있다: 엔드포인트가 비어 흡수할 하류가 없고, 같은 경로의 F17-H 정식 녹화에서 이체 5xx 1.0(§9) (원칙 9)

## 12. 첫 실행에서 확인할 것

- KCM 이 새 ReplicaSet 을 명시 env `{"name":"DB_SERVICE","value":"XEPDB1"}` 로 잡는지.
- transfer 오류 로그가 'SQL Error: 12514' 와 'ORA-12514 ... XEPDB1' 로 들어오는지(OTel 로그 내보내기는 기동 직후부터 동작, §7).
- KCM 에 transfer 파드 'Startup probe failed' 가 503 또는 context deadline exceeded 로, 150초 뒤 Killing 'failed startup probe' 와 재시작이 남는지.
- account, ledger 에 ORA 오류가 없고 잔액 조회가 200 인지.
