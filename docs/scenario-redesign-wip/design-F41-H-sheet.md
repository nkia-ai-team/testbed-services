---
title: F41-H 설계 시트 (헬스 하위 끝점을 끈 banking account 릴리스가 liveness 404 로 멀쩡한 컨테이너를 되풀이해 재시작시킴)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
  - probe
summary: banking account-service 의 새 릴리스 1.5.0 이 관리 끝점을 정리하며 헬스 하위 그룹(liveness, readiness)을 꺼 /actuator/health/liveness 가 404 가 된다. 시작, 준비 프로브(/actuator/health)는 통과해 새 파드가 옛 파드를 대체하지만, 매니페스트가 그대로 둔 liveness 가 404 로 실패해 kubelet 이 멀쩡한 컨테이너를 되풀이해 죽이고, 재시작 대기가 늘며 account 엔드포인트가 빈 구간마다 잔액 조회와 이체가 502 로 실패한다. 원본은 Spreedly 2023-01-25(코드 배포가 인프라 감시가 쓰던 헬스 엔드포인트를 없애 멀쩡한 인스턴스가 교체됨).
---

# F41-H 설계 시트

## 1. 요약

banking account-service 의 새 릴리스 `core-banking-account:1.5.0` 은 관리 끝점을 정리했다. `application.yml` 에 `management.endpoint.health.probes.enabled: false` 를 두고 "상태 확인은 `/actuator/health` 하나로 통일, 하위 경로는 따로 쓰는 곳이 없어 끈다" 고 적었다. Spring Boot 는 쿠버네티스에서 liveness, readiness 그룹을 자동으로 켜는데, 명시적 false 로 꺼져 `/actuator/health/liveness`, `/actuator/health/readiness` 는 404 가 되고 `/actuator/health` 는 그대로 답한다.

매니페스트(`core-banking/k8s/21-account-service.yaml`)의 프로브는 그대로다. 시작, 준비 프로브는 `/actuator/health` 라 통과해 새 파드가 Ready 가 되고 옛 파드를 대체한다. 그런데 liveness 는 `/actuator/health/liveness`(15초 간격, 5회)라, Ready 약 75초 뒤 kubelet 이 멀쩡한 컨테이너를 죽인다("Container account-service failed liveness probe, will be restarted"). 같은 버전이 다시 떠 되풀이되고, 재시작 대기(10, 20, 40, 80, 160, 최대 300초)와 기동 약 40초 동안 account 엔드포인트가 비어 그 구간이 점점 길어진다. 빈 구간마다 잔액 조회(nginx → account)는 502, 이체(nginx → api → account → transfer)는 api 재시도와 서킷 끝에 502 이고, Ready 인 약 75초 동안은 정상이다. 메모리, Oracle, transfer, 거래 내역, commerce 정산은 그대로다.

비유: 건물 관리실이 매장마다 "영업 중" 표시등을 15초마다 확인하고, 다섯 번 연속 꺼져 있으면 매장을 닫았다가 다시 연다. 새로 인테리어한 매장이 "쓰는 사람이 없다" 며 표시등을 떼어 냈다. 매장은 멀쩡히 장사하는데 관리실은 매번 닫았다 열기를 되풀이하고, 다시 여는 대기 시간은 갈수록 길어진다. 고칠 곳은 관리실(kubelet)이나 확인 규칙(프로브)이 아니라 표시등을 뗀 새 인테리어(릴리스)다.

## 2. 원본 사례

- 기업: Spreedly (결제 오케스트레이션 API)
- 날짜: 2023-01-25 (배포 22:41 UTC, 영향 시작 23:09 UTC, 조치 19:56 EST, 해결 20:47 EST, 사후 보고 게시 2023-01-31)
- 링크: [502 Bad Gateway Errors, Incident Report for Spreedly (공식 상태 페이지)](https://status.spreedly.com/incidents/kkkpw1vlh20v) (WebFetch 로 원문 확인, 자료 문서 `ref-real-world-incidents.md` M11 에 이번에 추가)
- 요약(출처가 말한 것만): 영향 구성 요소 Core Transactional API. 애플리케이션 코드 배포가 "removed application health endpoints that were used for critical infrastructure monitoring purposes". 그 밖에는 멀쩡한 인스턴스가 종료가 필요한 것으로 잘못 표시되어 교체 인스턴스가 만들어졌고, 때로 트래픽을 처리할 인스턴스가 모자라 고객이 502 Bad Gateway 를 받았다. 문제 코드를 되돌리고 새 인스턴스를 띄워 복구했다. 어떤 엔드포인트, 어떤 감시 도구인지, 오류 비율, 탐지 경로는 적혀 있지 않다. 재발 방지는 변경 릴리스 절차 재검토, API 간 인터페이스 요구 문서화와 교차 팀 교육, 배포 감시와 자동 호출 강화.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 애플리케이션 코드 배포(22:41 UTC) | account-service 를 릴리스 `core-banking-account:1.5.0` 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그) |
| 원인이 된 결함 | 새 코드가 인프라 감시가 쓰던 헬스 엔드포인트를 없앰 | 새 버전이 `management.endpoint.health.probes.enabled=false` 로 헬스 하위 그룹을 꺼 kubelet liveness 가 쓰던 `/actuator/health/liveness` 가 404 |
| 전파 경로 | 감시가 멀쩡한 인스턴스를 종료 대상으로 표시 → 교체 인스턴스 생성 → 때로 인스턴스 부족 | kubelet liveness 가 404 다섯 번 → 멀쩡한 컨테이너 종료와 재시작 → 같은 버전이 다시 떠 되풀이, 재시작 대기가 늘며 엔드포인트가 빈 구간이 길어짐 |
| 사용자 증상 | 502 Bad Gateway, 때로 요청 처리 손상 | 빈 구간마다 잔액 조회 nginx 502, 이체 api 502, Ready 구간에는 정상 |
| 원본의 탐지 경로 | 출처에 적히지 않음 | api 오류율과 오류 로그('Account service call failed ... Connection refused', 서킷 열림), KCM Unhealthy(liveness 404), Killing, BackOff → 119 이상 탐지 → 인시던트 |
| 완화와 복구 | 코드를 되돌리고 새 인스턴스 기동 | cleanup 이 이미지를 매니페스트 이미지로 되돌림(롤백). 되돌린 버전은 하위 경로를 다시 열어 재시작이 멈춤 |

기전 일치: "배포된 코드가 인프라 감시(여기서는 kubelet liveness)가 쓰던 헬스 엔드포인트를 없앰 → 멀쩡한 인스턴스가 비정상으로 판정되어 교체(재시작)됨 → 교체 동안 인스턴스가 모자라 502 → 코드를 되돌리면 복구". 우리 스택에 맞춘 것은 감시 주체(원본의 인프라 감시 → kubelet liveness)와 규모(인스턴스 여럿 → replicas 1 파드의 재시작 반복)다. 원본은 배포 약 28분 뒤 영향이 시작되었고, 테스트베드는 liveness 간격(15초 × 5회) 때문에 Ready 약 75초 뒤 첫 재시작이 온다. 그래서 '배포와 증상 사이가 멀어 배포를 의심하지 않는' 지연은 재현하지 않는다.

관계와 id: 사용자 증상(account 가 주기적으로 죽어 잔액 조회와 이체가 구간마다 502)이 F41-R(같은 account 의 릴리스 1.3.0 메모리 누수로 OOM 뒤 재시작)과 같고 원인이 달라 사례군 F41 의 H 다. kubelet 증상('Liveness probe failed: HTTP probe failed with statuscode: 404' 로 재시작 반복)은 F05-H(commerce payment 의 Deployment 프로브 경로를 바꾼 설정 오배포)와 같지만, 여기서는 프로브 스펙이 그대로이고 이미지만 바뀌었다. rejected 의 'food dispatch 또는 banking account liveness 오설정 재시작 루프'(2026-10-10, 반복 26)는 k8s.probe 로 경로를 바꾸는 주입이라 F05-H 복제였고 원본도 없었다. 이 후보는 프로브를 건드리지 않고(k8s.image release 모드의 새 계약) 공식 원본이 있다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 36 = 79, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 27, 은행 26, 음식배달 26 | 음식배달과 은행이 가장 적다. 이 기전은 '시작, 준비 프로브는 남고 liveness 경로만 사라짐' 이 필요한데 음식배달 다섯 서비스는 세 프로브 모두 `/actuator/health` 하나라(`food-delivery/k8s/2*-deploy.yaml`) 그 경로를 없애면 새 파드가 시작 프로브에서 죽고 기본 롤아웃(maxSurge 1)이 옛 파드를 남겨 피해가 없다(원칙 1 재현 불가). liveness 만 하위 경로인 곳은 banking account, transfer, ledger 뿐이라 은행(26→27)에서 낸다 |
| 묶음 | J 12(15.2%), P 10, G 8, A, B, D, L 7, C 6, E 4, F 3, H, K 2, I, M, O, Q 1, N 0 | 주 묶음 J(결함 있는 새 버전 배포) 12→13(80 중 16.3%). 피해 모양이 파드 재시작에 따른 부품 멈춤이라 J+B(B 로 세도 7→8, 10%). 0 인 N 은 이번 목록에서도 공식 사례로 재현되는 후보가 없었다(4절 목록 7) |
| 정답 위치 | 주문 서비스 8, 노드·디스크, 외부 결제 의존, 은행 이체 서비스 6, 배달 서비스 5, 은행 계좌 서비스 4, … | 은행 계좌 서비스 4→5(6.3%). 같은 기전을 transfer 에 걸면 은행 이체 서비스 6→7 이고 commerce 정산까지 번져 F17-H, F17-R 과 피해 범위가 겹쳐 account 를 먼저 골랐다 |
| 결제 경로 | 12/79(15.2%) | 이 후보와 무관(12/80, 15%) |

다양성 메모: 같은 증상(account 재시작 반복)에 정답이 둘(F41-R 메모리 누수, F41-H 끝점 제거)이고, 같은 kubelet 메시지(liveness 404)에 정답이 둘(F05-H 프로브 설정, F41-H 릴리스)이 된다. 관제 AI 는 Unhealthy 본문(시간 초과 대 404), JVM 메모리 모양, 새 ReplicaSet 에서 바뀐 칸(프로브 대 이미지)을 읽어야 가를 수 있다.

## 4. 후보 목록 (실제 기전 × 부품, 이번 반복)

| 순위 | 후보 | 원본 사례 | 부품, 정답 위치(정식 + 후보) | 결과 |
|---|---|---|---|---|
| 1 | banking account 릴리스가 헬스 하위 그룹을 꺼 liveness 404 재시작 반복 | Spreedly 2023-01-25 공식 상태 페이지 | banking account, 은행 계좌 서비스(4) | **채택(F41-H)** |
| 2 | 같은 릴리스 꼴을 banking transfer 에(liveness `/actuator/health/liveness`, maxUnavailable 1/maxSurge 0) | 같음 | banking transfer, 은행 이체 서비스(6) | 보류(버리지 않음): 정답 위치가 1번보다 많고 피해가 commerce 정산으로 번져 F17-H, F17-R 과 범위가 겹친다. 다음 후보가 될 수 있다 |
| 3 | 같은 릴리스 꼴을 food 서비스에 | 같음 | food order, dispatch 등 | 버림(원칙 1 재현 불가): food 는 시작, 준비, liveness 가 모두 `/actuator/health` 라 경로를 없애면 새 파드가 시작 프로브에서 죽고 기본 롤아웃이 옛 파드를 남겨 피해가 없다 |
| 4 | 자동 재색인 작업이 표현식 인덱스의 통계를 잃어 계획이 나빠짐 | GitLab 2020-10-24 공식 production#2885 | commerce PostgreSQL, DB 테이블(0) | 버림(원칙 1): 세 DB 모두 표현식 인덱스가 없다(pg_indexes 조회, init 스키마). 일반 열 통계는 재색인으로 사라지지 않는다 |
| 5 | 중첩 트랜잭션이 두 번째 연결을 잡아 풀 hold-and-wait | WorkOS 2026-07-16 공식 블로그 | banking account, food payment | 버림(원칙 9, 1): 180rps 와 ms 지연에서 동시성이 풀 크기(10~15)에 못 미치고, 원본도 잠재 결함 위 트래픽 급증이라 계기가 둘이다 |
| 6 | DB 점 릴리스의 동작 변화로 교착 | Daily 2023-02-07 공식 상태 페이지 | food MySQL | 버림(원칙 1, 9): DB 엔진 업그레이드가 필요하고 되돌릴 수 없다 |
| 7 | 응답 코드 탓에 백오프가 안 걸린 재시도(묶음 N 0) | GitHub 2026-05-05 공식 월간 보고 | food payment → 외부 결제 mock | 버림(원칙 1, 컨트롤러 필수 중단 조건): 결제 경로는 이제 12/79 로 상한 아래지만, 원본이 정기 증설 부하와 잠재 백오프 결함의 두 요소이고, order 가 주문 저장 뒤 결제 호출 동안 DB 연결을 쥐어(createOrder 트랜잭션) 결제가 느려지면 order 풀과 readiness 가 빠져 entry 0 이 된다 |
| 8 | 큰 표 온라인 재구성이 피크에 연결 용량을 포화 | GitHub 2026-05-04 공식 월간 보고 | food MySQL(payments 403MB, dispatch_events 375MB) | 버림(원칙 9): 가장 큰 표가 400MB 안쪽이라 재구성이 수십 초에 끝나 피해 구간이 짧다(2026-07-24 hold 행과 같은 벽) |
| 9 | DB 접속 한도 도달 | Buttondown 2026-03-31 공식 블로그 | food MySQL, DB 인스턴스(2) | 버림(원칙 1): 무엇이 접속 수요를 늘렸는지(배포, 설정, 작업자)를 원본이 밝히지 않는다 |
| 10 | 노드 conntrack 표가 차 새 연결을 떨굼 | (공식 사후 보고 없음, 웹 검색 1회: 벤더 안내, 강의 요약뿐) | tb-w2 노드 | 버림(원칙 1) |
| 11 | commerce gateway 릴리스 메모리 누수(게이트웨이 0) | Honeycomb 2019-11-06 공식 | commerce gateway | 버림(컨트롤러 필수 중단 조건): commerce 부하가 testbed-gateway:8089 로 바로 들어가 gateway 가 멈추거나 재시작하면 k6 상태가 0 이 되어 abort |
| 12 | food dispatch 릴리스가 order 의 배차 요청을 엄격 검증으로 400 → order 503 | Flagsmith 2024-01-18 공식 | food dispatch, 배달 서비스(5) | 보류(버리지 않음): 'order 503 Dispatch' 증상의 정답이 이미 배달 서비스 넷, DB 테이블(배차) 하나, 내부 배치 작업 하나라 1번보다 뒤 |

부품: banking account, banking transfer, food 서비스, commerce PostgreSQL, food MySQL, food payment, tb-w2 노드, commerce gateway, food dispatch(9개). 이번 실행에서 막힌 후보 목록, rejected, 장부 §4-1, §5 와 같은 원본과 주입은 뺐다.

## 5. 인과 사슬과 위치

1. 결함: `scripts/scenarios/fault-images/f41-h/account-service.patch:5-18`. 매니페스트 버전 `core-banking/account-service/src/main/resources/application.yml:54-70` 의 `management.endpoint.health.probes.enabled: true`(그 위 주석: liveness 만 DB 에서 떼어낸다)를 릴리스가 `false` 로 바꾼다. Spring Boot 3.4.5 `AvailabilityProbesAutoConfiguration$ProbesCondition` 은 `management.endpoint.health.probes.enabled` 값이 있으면 그 값을 따르고 없을 때만 쿠버네티스 감지로 켠다(javap 확인). 그래서 false 면 liveness, readiness 그룹과 `livenessState` 기여자가 없어 하위 경로가 404 다.
2. 로컬 실측(2026-10-10, 104): origin/main core-banking 에 패치를 얹어 `./mvnw -o -pl account-service -am package` 로 jar 를 만들고 `KUBERNETES_SERVICE_HOST` 를 준 채(쿠버네티스 감지) 띄움. 패치 jar: `/actuator/health` 503(로컬은 DB 가 없어 DOWN, 운영은 200), `/actuator/health/liveness` 404, `/actuator/health/readiness` 404. 기준 jar: 같은 조건에서 `/actuator/health` 503, liveness 200, readiness 200. 패치는 상태 판정 자체를 바꾸지 않고 하위 경로만 없앤다.
3. 프로브: `core-banking/k8s/21-account-service.yaml` startupProbe `/actuator/health` period 5s failureThreshold 30(:67-72), readinessProbe `/actuator/health` period 10s timeout 3s(:73-79), livenessProbe `/actuator/health/liveness` period 15s timeout 3s failureThreshold 5(:80-87). 전략 미지정이라 기본 maxSurge 25%(→1), maxUnavailable 25%(→0) 이고 replicas 1(:9).
4. 롤아웃: 새 파드가 시작, 준비 프로브를 통과해 Ready → 옛 파드 종료(롤아웃 완료). liveness 는 시작 프로브 성공 뒤부터 돌아 404 다섯 번(약 75초)에 kubelet 이 컨테이너를 죽인다. restartPolicy Always 라 같은 이미지로 다시 뜨고, 재시작이 거듭되면 kubelet 의 재시작 대기(10초에서 두 배씩, 최대 300초)가 붙는다.
5. 전파: 빈 구간에는 Service `testbed-account` 엔드포인트가 비어 nginx → account(잔액 조회, 계좌 목록)는 502, api → account(`core-banking/api-service/src/main/java/com/corebanking/api/client/AccountClient.java:28-58`)는 연결 거부 → 재시도 3회 → 서킷 열림 → BAD_GATEWAY. commerce 정산(commerce-payment → transfer)과 거래 내역(api → transfer)은 account 를 거치지 않는다.

## 6. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `core-banking-account`(account 의 새 릴리스 1.5.0). 고쳐야 재발이 막히는 곳은 끝점을 없앤 릴리스다. kubelet 은 매니페스트의 정당한 프로브를 수행했고, 프로브 설정은 2026-08-07 부터 바뀐 적이 없다(kcm_resources_history).
- 계기(`trigger_target_id`): 비움(계기가 같은 릴리스 롤아웃).
- 부분 점수: `core-banking-api`(증상이 가장 크게 보이는 곳).
- 원본 사후 보고와 같은 층위: "배포된 코드가 헬스 엔드포인트를 없애 멀쩡한 인스턴스가 교체되었다". 패치의 어느 줄인지는 요구하지 않는다(원칙 5). "프로브 경로가 잘못되었다" 는 답은 원인 근처지만, 바뀐 것이 프로브가 아니라 이미지라는 관측과 맞지 않아 정답이 아니다.

## 7. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | api 서버 스팬 오류율(502), api ERROR 로그 급증('Account service call failed', 서킷 열림), KCM Unhealthy, Killing, BackOff 급증(banking liveness 실패 평시 0) |
| 피해 판정 | 러너 | 동반 부하 잔액 조회(step get) 실패율 ≥ 0.5, 3틱 |
| RCA | 녹화 데이터 | 8절 표 |

## 8. 관측 근거 (119 실조회, 평시. 표본 데이터만으로 증명하지 않는다)

| 증거 | 119 표와 칸 | 조회 | 결과(2026-10-10) |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | PG `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-banking' AND name LIKE 'testbed-account%'`, yaml 에서 `"image"` 와 `livenessProbe.httpGet.path` 추출 | ReplicaSet 37건 모두 `core-banking-account:latest`, 파드 28건 모두 `:latest`. liveness 경로는 2026-08-07 부터 ReplicaSet 10건 모두 `/actuator/health/liveness`(그 전 27건은 `/actuator/health`). 주입 뒤 새 ReplicaSet 은 이미지만 1.5.0 이고 경로는 같아야 한다 |
| 계기: kubelet 이벤트 | CH `kcm_events_local`(namespace, object_name, reason, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-account%'`, 30일 | ScalingReplicaSet, Scheduled, Pulled('Container image "core-banking-account:latest" already present on machine' 4건), Created, Started, Killing('Stopping container'), Startup probe 연결 거부(기동 중 약 30초), Readiness 시간 초과(같은 시각 account 의 ORA-28000 로그, 앞선 F35-R 류 실행). liveness 실패는 0건. 새 파드의 Pulled 본문에 태그가 남는다 |
| 근본: liveness 404 와 재시작 | CH `kcm_events_local` | `body ILIKE '%statuscode%'` 전체 | 'Liveness probe failed: HTTP probe failed with statuscode: 404' 72건(commerce, F05-H 실행)으로 이 꼴이 수집됨을 확인. banking 의 liveness 실패는 `body ILIKE '%liveness%'` 로 0건. 'Back-off restarting failed container' 도 수집됨(commerce postgres, food payment) |
| 감별: JVM 은 멀쩡함 | VM `apm.agent.otel.java.jvm.memory.used{service_name="core-banking-account",jvm_memory_pool_name="Tenured Gen"}` | 7일 min/max(파드별 host_name) | 약 24.6~86.4MB(한도 170MiB). 주입 뒤에도 이 대역에 머물고 재시작마다 낮은 값에서 다시 시작해야 한다(F41-R 은 한도까지 직선 상승) |
| 전파: api 오류 | CH `lucida_logs_local`(service_name, severity_text, body) | `service_name IN ('core-banking-api','core-banking-account') AND severity_text IN ('ERROR','WARN')`, 30일 | 'Account service call failed for order null: I/O error on POST request for "http://testbed-account:N/api/accounts/transfer": Connection refused' 423건, 'Account service circuit open/exhausted ... accountClient is OPEN' 6,670건(앞선 시나리오 실행 때). 로그는 전수 수집이다. 평시 잡음은 'Insufficient balance' 400 |
| 감지 지표 | VM `apm.agent.otel.java.error_rate{service_name="core-banking-api"}` | 현재 | 0. 표본 기반 지표라 추세로만 쓴다 |

수집 특성: 결정 증거(KCM 이벤트, 이미지와 프로브 스펙 이력, 오류 로그)는 표본이 아니다. 트레이스(10%)와 APM 지표는 보조다. nginx 는 OTel 이 없어 nginx 의 502 는 119 에 남지 않지만 api 쪽 이체 실패와 account 스팬 공백이 같은 구간을 보여 준다.

## 9. 감별 (must_support, must_rule_out, contrast_with)

정답지 `scenario-metadata.json` F41-H 에 적었다. 요점:

- must_support: ① 롤아웃과 Pulled 의 1.5.0 태그, 프로브 경로 불변 ② 같은 파드의 liveness 404 Unhealthy, Killing, BackOff 반복 ③ 죽기 전까지 JVM 정상, OOM, Hikari, ORA 오류 없음 ④ 빈 구간마다 잔액 조회 실패율 0.5 이상, api 'Connection refused'·서킷, 빈 구간이 길어짐. 거래 내역과 commerce 정산 정상.
- must_rule_out: F05-H(프로브 설정), F41-R(메모리 누수), F51-H(DB 연결 고갈), F51-R(replicas 0), F35-R, F01-P(Oracle, DB 계정), F17-R, F17-H(transfer).
- contrast_with: F41-R, F05-H, F51-H, F51-R.

## 10. 러너 판정과 강도, 부하 계산 (원칙 9)

- 수단: 기존 `k8s.image` release 모드에 계약 F41-H(`core-banking-account:latest` → `core-banking-account:1.5.0`)를 더한다. 태그는 F41-R(1.3.0), F51-H(1.4.0)와 겹치지 않는다(저장소 grep). 실행기 코드 변경 없음.
- 강도: 하나로 고정(`approved-fixed-f41-h`). 패치는 꺼짐 하나라 강도 축이 없다.
- 시간 계산: 롤아웃 뒤 기동과 Ready 약 40초(109 KCM 2026-10-09: 시작 프로브 연결 거부 약 30초), Ready 뒤 liveness 404 다섯 번 약 75초에 첫 재시작. 빈 구간 = 재시작 대기 + 기동(10+40, 20+40, 40+40, 80+40, 160+40, 300+40초), Ready 구간 약 75초. 누적으로 첫 재시작 약 2분, 두 번째 빈 구간 약 4분, 세 번째 약 7분. 30초 창 실패율 0.5 이상 3틱(45초)은 빈 구간이 약 60초 이상일 때 서므로 두세 번째 빈 구간(롤아웃 약 4~7분 뒤)에서 판정된다. min_hold 12m 으로 녹화에 재시작 주기 너덧 번이 들어가고, timeout 20m 은 기동이 느린 경우를 받는다.
- 부하: 동반 north-south 5rps(core-banking transfer-heavy-surge.js: 이체 40%, 잔액 35%, 거래 내역 15%, 계좌 목록 10%) → 잔액 조회 약 1.75/s, 30초 창에 약 50건. 상한 180rps 의 3%. 서킷브레이커는 api 쪽이라 피해를 줄이지 않고 502 로 바꿀 뿐이다. 잔액 조회는 nginx → account 직행이라 서킷이 없다.
- 중단 조건: banking 진입점 상태는 nginx → api 이체 단계다. account 가 빠져도 nginx 와 api 는 살아 502 로 답하므로 entry_status 0 은 나오지 않는다(F41-R, F51-R 과 같은 판단). account 는 다른 서비스의 연결을 쥐고 기다리게 하지 않는다(연결 거부가 즉시 돌아온다).
- 배제: 동반 부하 미전달(achieved_rps < 1.25), transfer NotReady, Oracle NotReady.
- cleanup: 이미지를 `:latest` 로 되돌리고 available 확인(상한 180초), 1.5.0 을 쓰는 파드가 없어지면 tb-w2 containerd 에서 이름과 ID 참조를 지운다. 되돌린 버전은 하위 경로를 다시 연다.

## 11. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Spreedly 2023-01-25, 공식 상태 페이지 링크, WebFetch 로 원문 확인)와 요소별 대응표, 기전 같음(2절)
- [x] 장부 갱신(§4-1, §6), 묶음 J 13/80(16.3%), 정답 위치 은행 계좌 서비스 5/80(6.3%), 결제 경로 12/80(15%), 은행 27. 어느 축도 20% 미만, 음식배달이 아닌 이유 기록(3절)
- [x] 근본 원인 위치: 패치 `account-service.patch:5-18`, `application.yml:54-70`, 매니페스트 `21-account-service.yaml:67-87`(5절)
- [x] 근본 원인 흔적의 119 조회: KCM 이벤트 표의 liveness 404 꼴과 banking 평시 0건, kcm_resources_history 의 이미지와 프로브 경로 이력(8절)
- [x] 핵심 증거가 표본 데이터만이 아님: KCM 이벤트, 리소스 이력, 로그 전수(8절)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 끝점 제거는 실제 설정 변경)(8절)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "새 버전 롤아웃 직후 프로브 스펙은 그대로인데 liveness 가 404, 같은 파드 재시작 반복, JVM 정상 → 새 버전이 헬스 경로를 없앴다". 코드 줄 추론 불필요(6절)
- [x] 정답지 세 칸(근본 core-banking-account, 계기 비움, 부분 api)(6절). class A(배포된 변경). fault_pattern 은 P1~P8 어느 것도 맞지 않아 생략(새 P 번호를 만들지 않는 규칙)
- [x] 감지, 피해 판정, RCA 증거 구분(7절)
- [x] 시나리오 id 흔적 없음: 패치가 더한 문자열(주석 '관리 끝점 정리(1.5.0)', 설정값)에 id, fault, bug, chaos, scenario 없음(테스트로 확인), 태그 1.5.0, 실행기 인자와 상태 파일에 id 없음
- [x] 피해 계산: 10절. 로컬 실측으로 패치 jar 의 liveness 404, 기준 jar 200 확인

## 12. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f41-h` 로 109 docker 에 core-banking-account:1.5.0 이 있어야 preflight 가 통과한다(빌드 전후 기본 이미지 ID 비교는 스크립트가 한다).
- 새 파드가 Ready 가 되어 롤아웃이 끝나는지(시작, 준비 프로브 `/actuator/health` 200), 그 뒤 KCM 이 'Liveness probe failed: HTTP probe failed with statuscode: 404', 'Killing ... failed liveness probe', 'BackOff' 를 잡는지.
- 실제 빈 구간 길이(운영 account 는 JDK 17 aarch64 라 기동 시간이 다를 수 있다)와 success 가 두세 번째 빈 구간에서 섰는지. 크게 다르면 운영 하네스가 min_hold, timeout 을 조정한다.
- 인시던트가 생기는지(원칙 7). api 오류율과 오류 로그가 빈 구간마다 오르는지.
- cleanup 뒤 tb-w2 containerd 에 1.5.0 의 이름 참조와 ID 참조가 남지 않았는지(recovery 가 확인).
