---
title: F50-R 설계 시트 (banking 네임스페이스 메모리 할당량이 사용량 아래라 transfer 재배포의 새 파드가 거절됨)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - kubernetes
summary: 운영자가 rca-testbed-banking 에 이미 쓰는 양(4928Mi)보다 작은 requests.memory 4Gi ResourceQuota 를 걸어, 떠 있는 파드는 그대로지만 transfer-service 일상 재배포(maxSurge 0)가 옛 파드를 내린 뒤 새 파드가 'exceeded quota' 로 거절되고 이체, 거래 내역, commerce 정산이 502 가 되는 시나리오. 원본은 mybinder.org 2022 'pod limit reached'(할당량 객체가 차지 않은 네임스페이스를 찼다고 말해 새 파드가 모두 거절, 객체를 지우자 회복).
---

# F50-R 설계 시트

## 1. 요약

쿠버네티스 ResourceQuota 는 객체를 받아들일 때(승인 단계)만 검사된다. 운영자가 banking 네임스페이스에 ResourceQuota `compute-resources`(hard `requests.memory=4Gi`)를 건다. 그 네임스페이스에 떠 있는 파드 7개의 메모리 요청 합은 이미 4928Mi(api, account, transfer, ledger 각 512Mi, Kafka 768Mi, Oracle 2Gi, nginx 64Mi)라 할당량은 만들어진 순간부터 사용량보다 작다. 떠 있는 것은 아무것도 멈추지 않고 모든 서비스가 계속 답한다. 할당량은 네임스페이스가 다음 파드를 만들어야 할 때 드러난다. 그 파드는 transfer-service 의 일상 재배포(`kubectl rollout restart`)에서 나온다. transfer 의 정본 전략은 maxSurge 0 / maxUnavailable 1 이라 컨트롤러가 옛 파드를 먼저 내리고, ReplicaSet 컨트롤러의 새 파드 생성은 API 서버가 `exceeded quota: compute-resources, requested: requests.memory=512Mi, used: requests.memory=4416Mi, limited: requests.memory=4Gi` 로 거절한다. 컨트롤러는 백오프하며 다시 시도하지만 매번 거절되어 transfer 파드가 하나도 없다. testbed-transfer Service 의 엔드포인트가 비어 account 의 이체, api 의 거래 내역, commerce-payment 의 정산 이체가 모두 Connection refused 로 끝나고 account, api, commerce checkout 이 502 다. 잔액 조회(nginx → account → Oracle)는 정상이다. 이미지, 프로브, env, 자원 한도, DB 는 그대로다.

비유: 건물 관리실이 송금부 층의 출입 정원을 이미 앉아 있는 인원보다 적게 적어 붙였다. 앉아 있는 사람은 아무도 내보내지 않으니 하루는 멀쩡히 돌아간다. 저녁에 송금 담당이 교대하려고 먼저 퇴근하자, 새 담당은 "정원 초과"라며 출입증을 받지 못한다. 송금 창구가 빈 채로 모든 송금이 돌아가고, 잔액 조회 창구는 그대로 열려 있다.

## 2. 원본 사례

- 기업: mybinder.org(Jupyter 커뮤니티가 운영하는 공개 Binder 서비스, GKE)
- 날짜: 2022(보고 제목은 "2022-01-27, pod limit reached", 문서 파일 이름과 시간표 머리는 2022-06-02로 어긋난다). 시각은 CET.
- 링크: [운영팀 SRE 문서의 사고 보고](https://mybinder-sre.readthedocs.io/en/latest/incident-reports/2022-06-02-pod-limit.html)
- 요약(출처가 말한 것만): 10:00 부터 prod hub 가 새 파드를 만들지 못해 mybinder.org 가 새 세션을 띄우지 못했다. 21:00 사용자가 Matrix 채널에 제보해 알았다(약 11시간 동안 큰 장애인 줄 몰랐다고 적음). 21:06 로그에서 `exceeded quota: gke-resource-quotas, requested: pods=1, used: pods=15k, limited: pods=15k` 를 찾았는데 실제로는 15k 파드 근처도 쓰지 않았다. 21:22 이 오류가 GKE 의 `gke-resource-quotas` 객체 버그이고 지우면 쿠버네티스가 바르게 다시 만든다는 글을 찾았고, 21:24 그 resourcequota 객체를 지우자 파드 생성이 바로 돌아와 실행 성공률이 거의 100% 로 회복했다. 후속 조치는 가동 감시와 경보 개선(mybinder.org-deploy 이슈 611). 요약 절은 "약 9시간 뒤 개입 없이 정상 운영"이라고도 적어 시간표와 어긋난다. `ref-real-world-incidents.md` M13 에 더했다.
- 함께 볼 사례(같은 '한도 객체가 실제 사용량을 넘지 못하게 막음' 계열): Google 2020-12-14(M1, 쿼터 관리가 User ID Service 쿼터를 실제 사용량 아래로 줄여 인증 실패, F32-R 의 원본). 층이 다르다(서비스 쿼터 대 클러스터 승인 할당량).
- 현실 비중: 설정 배포가 Google 포스트모템 트리거의 31%(M1)이고, 쿠버네티스 할당량과 스케줄링 층은 이 테스트베드에서 정답으로 쓰인 적이 없다(부품 지도 인프라 층 0).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | hub 가 새 세션마다 파드를 만들어야 함(평소 동작) | transfer 일상 재배포가 새 파드를 만들어야 함(rollout restart, 이미지 그대로) |
| 원인이 된 결함 | 할당량 객체가 네임스페이스가 찼다고 말함(used 15k = limited 15k, 실제로는 아님. 보고는 GKE 버그로 적음) | 할당량 객체의 hard 값이 떠 있는 파드의 요청 합보다 작음(used 4416Mi + 요청 512Mi > limited 4Gi) |
| 전파 경로 | API 서버가 새 파드 생성을 403 으로 거절 → 세션이 뜨지 않음 | API 서버가 새 transfer 파드 생성을 거절 → maxSurge 0 이라 옛 파드는 이미 내려가 transfer 가용 0, 엔드포인트가 비어 연결 거절 → account, api, commerce-payment 실패 |
| 사용자 증상 | 새 세션 실행 실패 | banking 이체와 거래 내역 502, commerce checkout 502. 잔액 조회 정상 |
| 원본의 탐지 경로 | 사용자 제보 뒤 로그의 `exceeded quota` 오류 | account, api, commerce 오류율과 ERROR 로그 급증, KCM FailedCreate 이벤트의 `exceeded quota: compute-resources` |
| 완화와 복구 | 할당량 객체를 지움 → 파드 생성이 바로 돌아옴 | 할당량을 지움 → 30초 안에 파드가 안 뜨면(ReplicaSet 컨트롤러 백오프) 다시 배포 |

기전은 원본과 같다: "할당량 객체의 숫자가 네임스페이스를 찼다고 말해 API 서버가 새 파드를 모두 거절하고, 새 파드가 필요한 기능이 멈추며, 그 객체를 지우면 회복한다". 바꾼 것 둘: ① 할당량 숫자가 틀린 이유. 원본은 GKE 가 만든 객체의 used 가 틀렸고(버그), 여기서는 운영자가 넣은 hard 가 사용량보다 작다. 쿠버네티스의 used 계산을 망가뜨릴 수단이 없고, 결과(승인 단계에서 used + 요청 > limited)는 같다. 그 값이 왜 그렇게 정해졌는지는 지어내지 않는다. ② 새 파드가 필요한 이유. 원본은 세션마다 파드를 만드는 것이 서비스의 본업이고, 이 테스트베드의 앱은 고정 파드라 재배포 때만 새 파드가 필요하다. 그래서 계기를 transfer 일상 재배포로 두고, 옛 파드가 먼저 내려가는 정본 전략(maxSurge 0)인 transfer 를 고른다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 57, 이 후보 전)

| 축 | 값 |
|---|---|
| 묶음 | A 7, B 7, D 7(각 12%), C 6, G 6, J 6(각 10.5%), L 5, F 3, E 2, H 2, K 2, I 1, M 1, O 1, P 1, N 0. 이 후보는 **G+B(G 6→7, 58 중 12.1%)**. B 로 세도 7→8(13.8%) |
| 정답 위치 | 주문 서비스 7(12%), 외부 결제 의존 6, 노드, 디스크 5, 은행 이체 서비스 5. 결제 경로 합계 10(17.5%). 이 후보는 **새 정답 위치 '쿠버네티스 리소스 할당량'(0→1)**, 결제 경로 그대로 |
| 서비스 | 쇼핑몰 26, 은행 16, 음식배달 15 → 은행 17 |
| 부품 지도 | 인프라 층 '리소스 할당량과 스케줄링' 0 |

이 후보를 고른 이유:

- 정답 위치가 0 인 새 부품(클러스터 승인 할당량)이다. 관제 AI 가 "롤아웃한 그 서비스" 또는 "파드가 없는 그 서비스"를 찍으면 계기나 증상 서비스(부분 점수)에 그친다. 같은 날 J(롤아웃한 서비스가 정답) 후보가 여섯이라, 롤아웃이 계기이되 범인이 아닌 후보가 그 지름길을 깬다.
- 같은 증상(transfer 파드가 없어 banking 이체와 commerce 정산 502)의 F17-H(없는 이미지), F17-R(readiness)과 정답이 다르다(카탈로그 관계 H). 셋을 가르는 관측 근거는 §8 에 있다.
- 음식배달(15)을 고르지 않은 이유: food Deployment 는 모두 기본 전략(maxSurge 25%)이라 할당량은 덧붙는 새 파드만 거절하고 옛 파드가 계속 서비스한다. 피해가 없다(원칙 9). 쇼핑몰에서 maxSurge 0 인 곳은 testbed-user 하나인데 쇼핑몰(26)이 가장 많고 은행(16)이 그 다음으로 적다.
- rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본(mybinder.org)이나 같은 주입(ResourceQuota)이 없다. rejected 의 'maxSurge 0 이 아닌 서비스의 롤아웃 실패류(ResourceQuota, startupProbe, ...)' 는 기본 전략 서비스라 버려진 것이고, 이 후보는 maxSurge 0 인 transfer 라 그 사유가 해당하지 않는다.

### 3단계 후보 목록 (실제 기전 × 부품, 순서는 숫자 순)

| 순위 | 후보 | 원본 사례 | 부품 | 결과 |
|---|---|---|---|---|
| 1 | banking 네임스페이스 할당량이 사용량 아래 + transfer 일상 재배포 | mybinder.org 2022 pod limit reached(+ Google 2020-12-14) | 리소스 할당량(0), 은행 | **채택(F50-R)** |
| 2 | ledger 의 자원 요청을 과하게 잡은 설정 배포가 노드 할당 가능량을 묶어 transfer 재배포의 새 파드가 Pending | Allegro 2018-07-18 공식(일부 서비스가 필요보다 훨씬 많은 자원을 예약해 여유 하드웨어가 있는데도 새 인스턴스가 뜨지 못함) | 스케줄링, 은행 원장 서비스(1) | 보류: 1번과 같은 '새 파드가 못 뜸' 꼴이고 원본 계기(트래픽 급증 자동 확장)를 재배포로 바꿔야 함. 다음 후보 재료 |
| 3 | commerce orders 의 폐기 예정 열 유일 제약 제거로 checkout 조회 전수 스캔 | Chargebee 2018-03-02 공식 | DB 테이블(주문)(0), 쇼핑몰 | 보류: 쇼핑몰 최다, 인덱스 제거 꼴이 F33-R, F33-P 에 이어 셋째 |
| 4 | banking Oracle 설정 변경이 헬스체크 응답을 깨 엔드포인트에서 빠짐 | GitHub 2024-08-14 공식(DB 설정 변경이 라우팅 서비스 헬스체크에 응답하지 못하게 해 읽기 엔드포인트 접근 불가) | DB 인스턴스, 은행 | 버림(원칙 9, 7): Oracle 의 readiness 와 liveness 가 같은 healthcheck.sh 라 헬스체크가 깨지면 liveness 가 Oracle 을 재시작해 F25-H 꼴 DB 중단이 된다 |
| 5 | food restaurant 릴리스가 IP 기준 레이트 리미터를 더해 모든 호출이 order 파드 IP 하나로 보임 | PostHog 2025-10-24 공식 | 가게 서비스(1), 음식배달 | 버림(원칙 7, F34-R 교훈): order 가 하류 4xx 를 그대로 전파해 429 단일 신호, 서버 스팬 ERROR 와 오류율이 오르지 않음 |
| 6 | commerce outbox 표 죽은 튜플로 릴레이 폴링이 느려짐 | incident.io 2025-10-20 공식 | DB 테이블(outbox), 쇼핑몰 | 버림(원칙 7): outbox 발행은 비동기라 사용자 경로 증상 없음 |
| 7 | 필드 형식을 바꾼 릴리스와 그 되돌리기 사이에 쓰인 행을 옛 버전이 못 읽음 | CircleCI 2021-11-08 공식 | 데이터 형식, 은행 원장 | 버림(원칙 7, 1): 그 경로(ledger 소비)가 비동기라 사용자 증상 없음, 두 번의 배포가 필요 |
| 8 | 시험 작업의 환경 변수가 운영 DB 를 가리켜 표를 비움 | Travis CI(상태 페이지, 시험이 운영 DB 를 TRUNCATE) | food 메뉴, 가게 표 | 버림(원칙 7, F34-R 교훈): 표가 비면 order 가 400 으로 거절해 4xx 단일 신호 |
| 9 | 참조 열이 기본 키보다 좁은 형식이라 넘침 | Heroku 2023-06(incident 2558), GitHub 2021-05 공식 | food, banking 표 | 버림(원칙 1 재현 불가): 세 도메인의 id 와 참조 열이 모두 BIGINT, NUMBER |
| 10 | 요청마다 불필요한 트랜잭션이 느린 외부 호출 동안 연결을 쥐어 풀 고갈 | incident.io 공식 블로그(database performance) | banking account 풀 | 버림(원칙 9): account → transfer 호출이 수십 ms 라 풀 10 으로 초당 200 건 넘게 감당, 180rps 상한 안에서 고갈 계산이 서지 않음 |

## 4. 인과 사슬 (코드와 인프라 위치)

1. 배포 정본: `core-banking/k8s/22-transfer-service.yaml:14-18` strategy maxUnavailable 1, maxSurge 0. `:104-107` requests memory 512Mi. 109 실배치(2026-10-09 kubectl): replicas 1, 같은 전략, progressDeadlineSeconds 600, revisionHistoryLimit 10.
2. 네임스페이스 사용량: 떠 있는 파드 7개의 requests.memory 합 4928Mi(5,167,382,528바이트). 큰 몫은 StatefulSet 인 Oracle 2Gi(`core-banking/k8s/10-oracle.yaml:43-46`)와 Kafka 768Mi(`core-banking/k8s/11-kafka.yaml:66-69`). 2026-10-09 클러스터 전체에 ResourceQuota, LimitRange 가 없다.
3. 할당량 승인: 할당량이 생긴 뒤 새 파드는 `used + 요청 <= hard` 일 때만 받아들여진다. 옛 transfer 파드가 사라진 뒤 used 4416Mi + 512Mi = 4928Mi > 4096Mi 라 거절되고, 옛 파드가 종료 유예 중일 때는 used 4928Mi 라 역시 거절된다. 할당량 컨트롤러가 status.used 를 세기 전에는 사유가 'status unknown for quota' 라, 실행기는 status.used 가 생긴 뒤 재배포한다.
4. Deployment 와 ReplicaSet 컨트롤러: 재배포는 새 ReplicaSet 을 만들고 maxUnavailable 1 이라 옛 ReplicaSet 을 0 으로, 새 ReplicaSet 을 1 로 같은 순간에 바꾼다(2026-10-01 04:01:38 같은 재배포의 실제 이벤트 모양). 새 파드 생성 거절은 ReplicaSet 의 Warning 이벤트 FailedCreate 로 남고 지수 백오프로 다시 시도된다.
5. 엔드포인트가 빈 Service 로의 연결은 kube-proxy 가 거절한다. 2026-10-02 08:00 transfer NotReady 창의 실제 로그(119 CH): api 'Transfer service list call failed: I/O error on GET request for "http://testbed-transfer:8082/api/transfers": Connection refused', account 'Transfer service call failed for order null: I/O error on POST request for ...: Connection refused'.
6. account: `account-service/.../client/TransferClient.java:28-58` (executeTransfer, fallback 'Transfer service circuit open/exhausted' 502). api: `api-service/.../client/AccountClient.java:28-58` (requestTransfer). commerce: `commerce/payment-service/.../client/BankingTransferClient.java:45-58` (transfer FQDN 동기 호출, 502).

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`target_id`) | `rca-testbed-banking/compute-resources` | 결함을 가진 곳은 사용량보다 작은 hard 값을 담은 할당량 객체다. 지워야 회복하고 고쳐야 재발이 막힌다. target_kind config |
| 계기(`trigger_target_id`) | `core-banking-transfer` | 결함을 드러낸 사건은 transfer 의 재배포다(이미지, 설정 그대로인 정당한 작업) |
| 부분 점수 | core-banking-transfer, testbed-transfer(계기, 파드가 없는 서비스), core-banking-account, core-banking-api, commerce-payment(502 를 내는 증상 서비스) | |
| 입도 | service(정해진 넷 가운데 이름 하나로 맞추는 칸. 그 이름이 '네임스페이스/할당량 이름') | |

- 원칙 6: 재배포는 정당한 요청이다. 같은 꼴의 재배포(rollout restart, 이미지 그대로)가 2026-10-01 04:01:38 UTC 에 성공했고 할당량을 지우면 다시 성공한다. 새 파드를 막은 것은 할당량의 잘못된 한도다. 원본 보고가 지목한 층위('할당량 객체가 네임스페이스가 찼다고 말해 새 파드가 거절되었다')와 같다.
- 원칙 5: 정답은 FailedCreate 이벤트 본문(할당량 이름, used, limited)이라는 전수 근거로 낸다. 할당량 값이 왜 그렇게 정해졌는지는 요구하지 않는다.
- 한계: ResourceQuota 객체 자체는 119 가 수집하지 않는다(PG `kcm_resources_history` 의 kind 13종에 resourcequota 가 없음, 2026-10-09). 근본 증거는 이벤트 본문에만 있다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | core-banking-account, core-banking-api 서버 스팬 502, commerce-payment, commerce-order 오류, 각 서비스 ERROR 로그 급증 |
| 피해 판정 | 러너 | 동반 부하 이체(step transfer) 5xx 비율, 평시 0 에서 0.5 이상 |
| 원인 설명 | 녹화 데이터 | §7 의 KCM 이벤트(FailedCreate, ScalingReplicaSet), ReplicaSet 스펙(PG), 로그(전수) |

- 겉 증상은 F17-H 와 같다(transfer 엔드포인트가 빔). 이 모양의 감지는 F17-R 정식 녹화와 2026-10-02 transfer NotReady 창(api 'Connection refused' 와 서킷 로그 1,006건, account 560건, commerce-order 278건, core-banking-api 묶음 promote)에서 실제로 났다.
- 평시 오류율: core-banking-account, core-banking-api 오류율 7일 99백분위 0(F39-R 시트 §7). 업무 거절은 400 이라 오류율에 안 들어간다.
- 표본량: F17-H 와 같은 동반 부하 5rps 라 api 는 초당 약 2.9건이 오류, 10% 표본으로 분당 약 17개 ERROR 서버 스팬(F17-H 시트 §6).
- 묶음 구성 위험: commerce 쪽 오류가 같은 창에 함께 나서 묶음이 commerce 로 잡힐 수 있다. banking ledger 대사 잡음과 섞일 수 있다(F39-R 시트 §6).

## 7. 관측 근거 표 (119 실조회, 2026-10-09 21:00~21:30 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수)와 로그(전수)다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 할당량 거절 | CH `kcm_events_local` (namespace, object_kind, object_name, reason, event_type, component, body) | `body ILIKE '%exceeded quota%' OR body ILIKE '%resourcequota%'`, 보존 전체(2026-08-13 05:28~) | 0건(선례 없음). 같은 수집 경로가 승인 거절을 잡는 것은 확인: `reason='FailedCreate'` 4건, 2026-09-17 06:10~06:33 kcm-128 Job kcm-agent-enroll, component job-controller, 'Error creating: pods "kcm-agent-enroll-" is forbidden: error looking up service account ...'. kcm-128 이벤트의 host_target_id(a072cd3d...)가 rca-testbed-banking 이벤트와 같아 같은 tb 클러스터 수집기다. 할당량 거절도 같은 'Error creating: pods ... is forbidden:' 꼴의 컨트롤러 Warning 이다 |
| 근본: 할당량 객체 | PG `kcm_resources_history` (kind) | `SELECT kind, count(*) ... GROUP BY 1` | pod 2612, replicaset 2308, deployment 141, configmap 83, service 79, node 74, job 44, pvc 38, pv 28, namespace 23, statefulset 23, daemonset 11, storageclass 2. resourcequota 는 수집되지 않는다(근본 객체는 이벤트 본문으로만 보인다) |
| 계기: 재배포 | CH `kcm_events_local` | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-transfer%'` 2026-10-01 04:00~04:03 | 같은 꼴의 재배포 모양: 04:01:38 ScalingReplicaSet 'Scaled down replica set testbed-transfer-7ccdd5d58c to 0 from 1' 와 'Scaled up replica set testbed-transfer-74b498b67b to 1 from 0'(같은 초, maxSurge 0), SuccessfulDelete, Killing, SuccessfulCreate 'Created pod: ...', Scheduled, 04:01:39 Pulled, Created, Started, 그 뒤 Startup probe 실패 약 45초. 고장에서는 SuccessfulCreate 자리에 FailedCreate 가 나고 Scheduled 이하가 없어야 한다 |
| 계기: 재배포 스펙 | PG `kcm_resources_history` (kind='replicaset', name, yaml, captured_at) | `namespace='rca-testbed-banking' AND name LIKE 'testbed-transfer-%'` | ReplicaSet 74b498b67b 의 yaml 에 `"kubectl.kubernetes.io/restartedAt":"2026-10-01T13:01:38+09:00"` 와 `"image":"core-banking-transfer:latest"`. 재배포 ReplicaSet 은 이 주석만 옛 것과 다르다 |
| 전파: 연결 거절 로그 | CH `lucida_logs_local` body | `body LIKE '%testbed-transfer%Connection refused%'`, 최근 8일 | 2026-10-09 06:55~07:12 만 core-banking-api 1,072, core-banking-account 44, commerce-order 166, commerce-payment 1건(다른 시나리오 실행 창), 그 밖 0건 |
| 대조: 할당량 문구 로그 | 같은 표 | `body ILIKE '%exceeded quota%'`, 최근 8일 | 0건(kubelet syslog 의 'forbidden' 4건은 2026-10-06 commerce secret 조회 거절이라 무관) |
| 골든 시그널(감지) | VM `apm.agent.otel.java.error_rate`, CH MV `agg_service_golden_signals` | F39-R 시트 §7(2026-10-08) | account, api 오류율 99백분위 0, 하루 오류 0건 |
| 피해 | 동반 부하 k6 live 문서 | 이체 step 5xx 비율 | 평시 0(업무 거절은 400) |

## 8. 감별

- must_support: 정답지 5항목(FailedCreate 'exceeded quota: compute-resources' 반복, 그 직전 재배포 이벤트와 restartedAt 만 다른 새 ReplicaSet, 새 ReplicaSet 의 파드 부재, account, api 의 Connection refused 와 502 와 commerce checkout 502, 동반 부하 이체 5xx 와 잔액 조회 정상).
- must_rule_out(정답지): transfer 릴리스나 이미지 문제(F17-H, F42-R), readiness 오설정(F17-R), 노드 장애나 노드 자원 부족, transfer DB 나 Oracle 장애(F01-P, F35-R), 재배포 자체, account, api 장애, 부하 증가.
- 같은 증상과 가르는 관측 근거:

| 시나리오 | 새 파드 | 바뀐 파드 템플릿 | 지문 이벤트 | transfer 로그 |
|---|---|---|---|---|
| F50-R | 없음(생성 거절) | 없음(restartedAt 주석만) | ReplicaSet FailedCreate 'exceeded quota: compute-resources' | 없음 |
| F17-H | 스케줄됨, 컨테이너 없음 | image 태그 2.1.0 | Pod ErrImageNeverPull | 없음 |
| F17-R | 떠 있음 | readinessProbe 경로 | Pod Unhealthy(readiness 404) | 기동 로그 있음 |
| F42-R | 떠 있음 | image 태그 2.2.0 | (Oracle 포화) | 이체 지연, Oracle 대기 |
| 노드 자원 부족 | Pending | 없음 | Pod FailedScheduling 'Insufficient memory' | 없음 |

- 러너 판정은 F17-H 와 같아 둘을 구별하지 않는다(배제 조건은 크래시 루프만 가른다). 구별은 정답지 mechanism 채점과 must_support 에 있다.
- 부하 증가 경쟁 가설: 실패는 거절된 연결이고, 같은 부하의 35% 인 잔액 조회는 정상이며, 실패는 재배포 시각에 맞춰 0 에서 거의 1 로 뛴다.
- contrast_with: F17-H, F17-R, F42-R, F32-R.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f50-r`). 강도라 할 값은 hard 하나인데, 사용량(4928Mi)에서 transfer 몫(512Mi)을 빼고 다시 더한 값보다 작기만 하면 결과가 같다(거절되거나 아니거나). 4Gi 는 그 문턱(4928Mi)보다 832Mi 작다. 실행기 preflight 가 매번 '떠 있는 파드 요청 합 > hard' 를 확인해, 매니페스트가 바뀌어 사용량이 줄면 실행을 시작하지 않는다(109 에서 읽기 전용 preflight 실행: 통과, used 5,167,382,528 대 limit 4,294,967,296. hard 8Gi 로 주면 실패, maxSurge 25% 인 testbed-account 로 주면 실패).

- 시간: 할당량 생성 → status.used 집계(보통 1초 안, 실행기가 최대 30초 기다림) → rollout restart → 옛 파드 Terminating(엔드포인트에서 즉시 빠짐) → 새 파드 생성 거절. 고장은 재배포 직후 시작한다. settle 60s, min_hold 15m 이라 판정(성공 3틱)은 재배포 약 16분 뒤. progressDeadlineSeconds 600 이 지나면 Deployment 가 ProgressDeadlineExceeded 가 되지만 상태 표시일 뿐이다.
- cleanup: 할당량을 지우고 30초 안에 파드가 뜨지 않으면 다시 배포한다. ReplicaSet 컨트롤러는 거절된 생성을 지수 백오프(5ms 부터 두 배씩, 상한 1000초)로 다시 시도해, 15분 넘게 거절된 뒤에는 다음 시도가 10분 넘게 늦을 수 있다. 원본처럼 지우기만 하면 회복이 recovery 상한(10m)을 넘길 수 있어 재배포로 앞당긴다. JVM 기동 약 45초, 상한 180초.
- 피해 계산: 이체 단계 5xx 비율은 계좌 검증 400(이체의 약 4%)을 빼면 1.0 이고, api 서킷이 열리면 그 400 도 502 가 된다(F39-R 시트 §9). 성공 문턱 0.5 를 크게 넘는다.
- 서킷브레이커: account, api 서킷은 열린 뒤 바로 502, commerce-payment 의 banking 호출은 서킷이 없어 요청마다 즉시 거절. 피해를 숨기지 않는다. 연결 거절은 즉시라 스레드 대기가 쌓이지 않는다.
- 진입점: nginx, api 는 그대로 떠서 502 로 답한다(할당량은 떠 있는 파드를 건드리지 않음). abort 조건 entry_status==0 은 이 고장으로 나오지 않는다.
- 같은 네임스페이스의 다른 파드: 고장 중 다른 파드가 새로 만들어질 일은 없다(StatefulSet 과 다른 Deployment 는 그대로). 그 사이 다른 파드가 축출되면 그 파드도 다시 만들어지지 않는다(노드 축출 이벤트는 별도로 남는다). cleanup 이 할당량을 지우면 함께 풀린다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f50-r`, min_hold 15m, settle 60s, timeout 20m |
| preflight(실행기) | 네임스페이스에 ResourceQuota 없음, 떠 있는 파드 메모리 요청 합 > hard, testbed-transfer replicas 1 과 maxSurge 0, available, resourcequotas 생성과 deployments 패치 권한 |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking transfer-heavy-surge.js(이체 40, 잔액 35, 거래 내역 15, 계좌 목록 10), target_rps 5, entry 30082, ramp 2m + hold 21m + ramp_down 15s, seed 5050 |
| success | 동반 부하 이체 5xx 비율(`loadgen.food_create_status_rate`, 선택자 business.5xx.rate) ≥ 0.5, 3틱 연속 |
| must_rule_out | achieved_rps < 1.25(부하 끊김), 잔액 조회 실패율 ≥ 0.2(account 나 Oracle 장애), transfer 재시작 수 ≥ 2(파드가 생겨 기동 중 죽는 다른 장애), 2틱 |
| abort | entry_status(domain core-banking, nginx → api) == 0, 2틱 |
| recovery | transfer 파드 Ready, transfer 가용 레플리카 1, 기준선 이체 5xx 비율 < 0.05, target_health 200(형식 조건), 2틱, 10m |
| cleanup | 할당량 삭제, 30초 안에 파드가 없으면 rollout restart, Deployment available 확인(180초), 동반 부하 종료, 10m |

판정 근거(평시 값과 장애 값):

| 조건 | 관측 | 평시 | 장애 중 | 판정력 |
|---|---|---|---|---|
| success | 동반 부하 이체 5xx 비율 | 0 | 약 1.0 | 있음(문턱 0.5) |
| must_rule_out account-path-failing | 잔액 조회 실패율 | 0 | 0 | 교란(account, Oracle 장애) 때만 오름 |
| must_rule_out transfer-crashloop-alternative | transfer 재시작 수 | 0 | 0(파드 없음) | 다른 원인(크래시 루프) 때만 오름 |
| recovery | 기준선 이체 5xx 비율, transfer Ready, 가용 레플리카 | 0, true, 1 | 약 1.0, false, 0 | 있음 |

새 관측 쿼리와 러너 변경은 없다. 새 실행기 `k8s.quota`(`scripts/scenarios/profiles/k8s_quota_executor.py`)를 더했다. 실행기는 `kubectl create quota` 로 할당량 하나를 만들고 집계를 기다린 뒤 `kubectl rollout restart` 한 번을 낸다. 이미지, 프로브, env, 자원, Service 는 건드리지 않고 아무것도 지우지 않는다(cleanup 의 할당량 삭제만).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(mybinder.org 2022 운영팀 사고 보고, 함께 볼 Google 2020-12-14)와 요소별 대응표가 있고, 기전("할당량 객체가 네임스페이스가 찼다고 말해 API 서버가 새 파드를 모두 거절, 새 파드가 필요한 기능이 멈춤, 객체를 지우면 회복")이 같다. 바꾼 두 가지(숫자가 틀린 이유, 새 파드가 필요한 이유)는 §2 에 적었다 (원칙 1)
- [x] 장부를 갱신했다: 묶음 G+B(G 6→7, 12.1%, B 로 세도 13.8%), 새 정답 위치 쿠버네티스 리소스 할당량(0→1, §2-1 에 정의 추가), 결제 경로 10(17.2%) 그대로, 서비스 은행 17(음식배달을 고르지 않은 이유는 §3), 어느 축도 20% 에 닿지 않는다 (원칙 2)
- [x] 근본 원인 위치: 인프라 지점(rca-testbed-banking 네임스페이스의 ResourceQuota, 사용량 4928Mi 를 이루는 `10-oracle.yaml:43-46`, `11-kafka.yaml:66-69`, `22-transfer-service.yaml:104-107`)과 전략 `22-transfer-service.yaml:14-18` (G1, §4)
- [x] 근본 원인의 흔적: CH `kcm_events_local` 의 FailedCreate 본문(할당량 이름, used, limited). 같은 클러스터 수집기가 승인 거절 FailedCreate 를 잡은 선례(2026-09-17)를 확인했다. 할당량 객체 자체는 수집되지 않음을 적었다 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트, ReplicaSet 스펙(전수), 로그(전수) (원칙 3)
- [x] 계기의 흔적: testbed-transfer 재배포 KCM 이벤트(ScalingReplicaSet 두 줄)와 restartedAt 주석의 새 ReplicaSet 스펙. 인공 지연을 쓰지 않는다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 재배포 직후 FailedCreate 'exceeded quota: compute-resources' 와 그 직후 transfer 로의 연결 거절. 할당량 값이 정해진 사연은 요구하지 않는다 (원칙 5)
- [x] 정답지 세 칸: 근본 `rca-testbed-banking/compute-resources`(config), 계기 `core-banking-transfer`, 부분 점수 transfer, account, api, commerce-payment (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 실행기는 네임스페이스, 할당량 이름(compute-resources, 쿠버네티스 문서의 흔한 이름), 자원, 값, Deployment 만 kubectl 로 넘기고 클러스터에 남는 것은 할당량 객체와 restartedAt 주석뿐이다. 동반 부하 태그는 tb-runner 의 k6 쪽에만 남는다 (원칙 8)
- [x] 피해 계산: 요청량과 무관한 전면 실패, 서킷브레이커는 피해를 숨기지 않음, 스레드 대기 없음, 동반 부하 5rps 는 banking surge 건강 상한 20rps 아래, 진입점은 살아 있음 (원칙 9, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- KCM 이 새 ReplicaSet 의 FailedCreate 'exceeded quota' 를 실제로 잡는지(같은 꼴의 Job FailedCreate 는 잡혔다). 안 잡히면 근본 증거가 '새 ReplicaSet 에 파드가 생기지 않음'과 Deployment 상태로 좁아져 정답이 관제 데이터로 도달하기 어렵다. 그때는 녹화하지 말고 설계를 다시 본다.
- 거절 사유가 'status unknown' 이 아니라 'exceeded quota' 인지(실행기가 status.used 를 기다린 뒤 재배포한다).
- 옛 파드가 재배포 직후 내려가는지, 고장 시작 시각과 옛 파드 종료 로그.
- account, api, commerce 의 'Connection refused' 로그와 오류율, commerce checkout 5xx, 잔액 조회 정상.
- 판정이 promote 해 인시던트가 생기는지, 묶음 main_service 와 상위 3개 서비스. banking promote 지연이 약 18~20분이라 cleanup 뒤 30분 이상 지나서 확인한다.
- cleanup 뒤 할당량이 없고 새 파드가 Ready 가 되는 시각, 재배포 대체가 쓰였는지(30초 안에 뜨지 않았는지).
