---
title: F51-R 설계 시트 (운영 용량 명령의 입력 실수로 banking account-service 가 replicas 0)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - kubernetes
summary: 운영자의 용량 명령 입력이 잘못되어 요청 경로에 있는 banking account-service Deployment 가 replicas 0 이 되고, 템플릿은 그대로인데 파드가 다시 생기지 않아 잔액 조회, 계좌 목록, 이체가 502 가 되는 시나리오. 원본은 AWS S3 us-east-1 2017-02-28(플레이북 명령의 입력 하나가 잘못되어 의도보다 많은 서버가 빠졌고, 빠진 서버가 요청 경로 하위 시스템의 용량이었음).
---

# F51-R 설계 시트

## 1. 요약

운영자가 정해진 플레이북으로 rca-testbed-banking 의 용량을 줄이는 명령을 실행했는데 입력 하나가 잘못되어, 내리려던 하위 시스템 대신 요청 경로에 있는 account-service 의 용량을 없앤다(`kubectl scale deployment testbed-account --replicas=0`). Deployment 컨트롤러는 서비스 중이던 ReplicaSet 을 1 에서 0 으로 줄이고 account 파드가 종료된다. Deployment 가 이제 0 개를 원하고 HPA 가 없어 아무것도 다시 만들지 않는다. 템플릿(이미지, 프로브, env, 자원)은 그대로이고, 무엇도 죽거나 프로브에 실패하지 않는다. testbed-account Service 의 엔드포인트가 비어 kube-proxy 가 testbed-account:8081 로의 연결을 모두 거절한다. 잔액 조회와 계좌 목록(nginx → account 직행)은 nginx 502, 이체(nginx → api → account → transfer)는 api 의 'Connection refused', 재시도, 서킷 열림 끝에 502 다. account 를 지나지 않는 거래 내역(api → transfer)과 commerce checkout 정산(commerce-payment → transfer)은 정상이다.

비유: 은행 지점장이 "오늘 오후엔 원장 정리팀 두 명을 집에 보내자"고 근무표를 고치다가 줄을 잘못 짚어 창구팀 전원을 퇴근시켰다. 아픈 사람도, 고장 난 기계도 없다. 창구 자리가 비었을 뿐이고, 근무표를 다시 고치기 전까지는 아무도 돌아오지 않는다. 송금 내역 조회는 창구를 거치지 않아 그대로 된다.

## 2. 원본 사례

- 기업: Amazon Web Services(S3, 북버지니아 us-east-1)
- 날짜: 2017-02-28(PST)
- 링크: [공식 사후 보고 "Summary of the Amazon S3 Service Disruption in the Northern Virginia (US-EAST-1) Region"](https://aws.amazon.com/message/41926/)
- 요약(출처가 말한 것만, 2026-10-09 원문 확인): 9:37AM PST, 권한 있는 S3 팀원이 정해진 플레이북으로 S3 과금 처리에 쓰이는 하위 시스템 하나의 서버 몇 대를 빼려는 명령을 실행했다. 명령의 입력 하나가 잘못 들어가 의도보다 많은 서버가 빠졌다. 잘못 빠진 서버는 다른 두 하위 시스템(객체 메타데이터와 위치를 관리하는 index, 새 객체의 저장소를 배정하는 placement)을 받치고 있었다. 큰 용량을 잃은 두 하위 시스템은 전체 재시작이 필요했고 그동안 요청을 처리하지 못했다. 12:26PM index 가 GET, LIST, DELETE 를 다시 처리할 만큼 용량을 되찾았고 1:18PM index, 1:54PM placement 가 회복했다. 후속 조치로 용량 제거 도구가 더 천천히 빼게 하고, 어떤 하위 시스템이든 최소 필요 용량 아래로 내려가게 하는 제거는 막게 했다. `ref-real-world-incidents.md` M22 에 세부를 더했다.
- 현실 비중: Google SRE Workbook 포스트모템 트리거 가운데 바이너리 배포 37%, 설정 배포 31% 다음이 운영 행동이고, 이 테스트베드의 장부 묶음 P(운영 작업의 대상 착오)는 정식 + 후보 2 다. 클러스터의 워크로드 용량(replicas)을 바꾸는 운영 명령은 정답으로 쓰인 적이 없다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 플레이북에 따른 용량 제거 명령, 입력 하나가 잘못 들어감 | 운영 용량 명령(`kubectl scale`)의 대상 입력이 잘못되어 testbed-account 를 replicas 0 으로 |
| 원인이 된 결함 | 의도보다 많은 서버(요청 경로 하위 시스템 index, placement 의 용량)가 빠짐 | 요청 경로 서비스 account 의 용량이 0 이 됨. 템플릿, 코드, 설정은 그대로 |
| 전파 경로 | index, placement 가 요청을 처리하지 못함 → S3 GET, LIST, PUT, DELETE 실패 → S3 에 기대는 서비스 실패 | testbed-account 엔드포인트가 비어 연결 거절 → nginx(잔액, 목록) 502, api(이체) 502 |
| 사용자 증상 | S3 API 오류율 상승 | banking 잔액 조회, 계좌 목록, 이체 502. 거래 내역과 commerce 정산은 정상 |
| 원본의 탐지 경로 | 보고에 따로 적혀 있지 않음(서비스 오류율 상승과 상태 대시보드) | api 오류율과 ERROR 로그 급증, account 스팬과 로그 끊김, KCM ScalingReplicaSet 'to 0 from 1' |
| 완화와 복구 | 빠진 용량을 되돌리고 하위 시스템을 재시작 | replicas 를 1 로 되돌림, account JVM 기동 약 40초 |

기전은 원본과 같다: "운영 용량 명령의 입력 실수가 요청 경로 하위 시스템의 용량을 없애 그 하위 시스템이 요청을 처리하지 못하고, 용량을 되돌릴 때까지 이어진다". 바꾼 것 셋: ① 규모. 원본은 서버 여럿, 여기는 replicas 1 짜리 Deployment 하나라 '의도보다 많이 뺐다'가 '그 서비스의 전부를 뺐다'가 된다. ② 하위 시스템 둘(index, placement) 대신 account 하나. ③ 원본의 전체 재시작이 오래 걸린 이유(대규모 인덱스 재검증)는 여기 없어 복구가 수십 초다. 의도한 대상이 무엇이었는지는 관제 데이터에 남지 않고(원본도 '과금 하위 시스템'이라고만 적는다) 정답이 요구하지 않는다. 의도한 대상은 함께 줄이지 않는다: 줄였다면 그것은 정당한 작업이고 피해와 무관한 교란이 하나 더 생긴다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 59, 이 후보 전)

| 축 | 값 |
|---|---|
| 묶음 | A 7, B 7, D 7, G 7(각 11.9%), C 6, J 6(각 10.2%), L 5, F 3, E 2, H 2, K 2, P 2, I 1, M 1, O 1, N 0. 이 후보는 **P(운영 작업의 대상 착오, 2→3, 60 중 5%)**. 피해 모양이 파드가 없는 부품 멈춤이라 P+B 로 적는다(B 로 세도 7→8, 13.3%) |
| 정답 위치 | 주문 서비스 7(11.9%), 외부 결제 의존 6, 노드, 디스크 5, 은행 이체 서비스 5, 그 밖 3 이하. 결제 경로 합계 10(16.9% → 60 중 16.7%). 이 후보는 **은행 계좌 서비스(2→3, 5%)**, 결제 경로 그대로 |
| 서비스 | 쇼핑몰 26, 은행 17, 음식배달 16 → 은행 18 |
| 부품 지도 | 은행 account 5(F39-R, F41-R 정답, 그 밖 증상), 인프라 층 '이미지와 배포(롤아웃, 롤백)'의 workload 용량(replicas) 조작은 0 |

이 후보를 고른 이유:

- 묶음 P 가 2 로 적고, 그 둘(F48-R, F48-P)은 모두 DB 표를 치운 운영 도구다. 운영 명령이 워크로드 용량을 없앤 꼴은 처음이다.
- 같은 증상(서비스에 파드가 없어 'Connection refused', 502)의 F50-R, F17-H, F17-R 은 정답이 transfer 쪽이고 원인이 할당량, 없는 이미지, readiness 다. 이 후보는 정답이 account 이고 원인이 운영 명령이라, 관제 AI 가 "파드가 없는 그 서비스의 릴리스나 설정"을 찍는 지름길로는 맞히지 못한다. 같은 account 정답의 F41-R(릴리스), F39-R(설정)과도 계기가 다르다(카탈로그 관계 H). 가르는 관측 근거는 §8 에 있다.
- 음식배달(16)을 고르지 않은 이유: 같은 주입을 food 에 걸 수 있는 대상은 dispatch, payment, restaurant 다. dispatch 는 'order 503 → dispatch' 정답이 이미 셋(F32-R, F32-H, F33-H)이라 지름길을 굳히고, payment 는 결제 경로(10 → 11, 18.3%)를 상한 가까이 민다. restaurant 는 loadgen 의 진입(NodePort 직행)이라 파드가 없으면 서버 쪽 흔적이 남지 않는다(원칙 3, rejected 'restaurant NodePort 진입 경로 제거' 반려와 같은 벽). 은행(17)과 음식배달(16)의 차이는 1 이다.
- rejected 의 'banking account replicas 0 (Google Cloud 2019-06-02)' 행은 사유가 증상 겹침(F17-R, F17-H 와 같은 증상)뿐이라 2026-10-09 사용자 정정으로 막힌 목록에서 빠졌다. 이 후보는 원본을 기전이 더 가까운 AWS S3 2017-02-28(서버 용량 제거 명령의 입력 실수)로 바꾸고, 증상이 같은 시나리오들과 가르는 관측 근거를 §8 에 적는다. Google Cloud 2019-06-02 는 이번 실행에서 'tb-w3 네트워크 용량 축소'로 막힌 원본이라 쓰지 않는다.

### 3단계 후보 목록 (실제 기전 × 부품, 순서는 숫자 순)

| 순위 | 후보 | 원본 사례 | 부품 | 결과 |
|---|---|---|---|---|
| 1 | 운영 용량 명령 입력 실수로 banking account Deployment replicas 0 | AWS S3 2017-02-28 공식 | 워크로드 용량, 은행 계좌 서비스(2) | **채택(F51-R)** |
| 2 | 같은 명령으로 food dispatch replicas 0 | AWS S3 2017-02-28 공식 | 워크로드 용량, 배달 서비스(3) | 보류(버리지 않음): order 503 'Dispatch service unreachable' 의 정답이 이미 dispatch 셋이라 1번보다 뒤. 같은 실행기의 다른 계약으로 다음 후보가 될 수 있다 |
| 3 | 같은 명령으로 food payment replicas 0 | AWS S3 2017-02-28 공식 | 결제 서비스(3) | 보류: 결제 경로 10 → 11(18.3%)로 상한 가까이 |
| 4 | food order 의 dispatch 주소 env 를 이웃 서비스(testbed-payment:8083)로 잘못 넣은 설정 배포, 하류가 404 | GitHub 2026-03-05 공식(잘못된 호스트로 보냄) | 주문 서비스(7) | 버림(원칙 2, 카탈로그 §1): F39-R(account 의 하류 주소를 다른 내부 호스트로)과 같은 수단, 같은 기전의 서비스 바꾸기 |
| 5 | banking Oracle ALTER TABLE MOVE 로 transfers 인덱스가 UNUSABLE, 이체 INSERT ORA-01502 | 없음(웹 검색 1회: 벤더 KB, DBA 블로그뿐) | DB 테이블(이체)(0) | 버림(원칙 1): 공식 사후 보고 없음 |
| 6 | food notify 새 릴리스 메모리 폭주가 노드 메모리 압박으로 다른 파드 축출 | k8s.af 모음(2차) | 알림 서비스(0) | 버림(원칙 1, 9): 2차 출처뿐이고 모든 food 파드에 1Gi 한도가 있어 노드 압박 전에 notify 만 OOMKill |
| 7 | 운영 명령 실수로 food 네임스페이스 파드 전체 재시작 | Joyent 2014-05-27(운영자 명령으로 데이터센터 전체 재부팅) | 네임스페이스 전체 | 버림(컨트롤러 필수 중단 조건, 원칙 7): MySQL 재시작으로 order readiness(DB health)가 빠져 entry 0, 1~2분 안에 스스로 회복 |
| 8 | food payment 의 외부 결제 API 키 회전 실수 | Harness 2026-01-08 공식(비밀 회전 중 자격 증명 하나가 잘못됨) | 결제 서비스, 외부 결제 의존 | 버림(원칙 1 재현 불가): PG mock 이 키를 검사하지 않아(PgApiClient 에 인증 헤더 없음) mock 기준선을 바꿔야 함 |
| 9 | banking ledger 새 릴리스 결함 | (GitHub 2026-04-20 등) | 은행 원장 서비스(1) | 버림(원칙 7): ledger 는 Kafka 비동기 소비자뿐이라 사용자 경로 증상 없음(앞선 소비자 반려와 같은 벽) |

## 4. 인과 사슬 (코드와 인프라 위치)

1. 배포 정본: `core-banking/k8s/21-account-service.yaml:9` replicas 1, 전략 기본값(RollingUpdate maxSurge 25% / maxUnavailable 25%), `:18-19` nodeSelector tb-w2. 109 실배치(2026-10-09 kubectl): replicas 1, revision 18, readyReplicas 1, 네임스페이스에 HPA 없음(`kubectl get hpa` 빈 결과). 러너 kubeconfig 는 `deployments/scale` 패치 권한이 있다(`kubectl auth can-i patch deployments/scale` yes).
2. scale: Deployment 의 spec.replicas 가 0 이 되면 Deployment 컨트롤러가 현재 ReplicaSet(가장 새 revision)을 0 으로 줄이고 KCM 이벤트 'Scaled down replica set testbed-account-<해시> to 0 from 1' 을 남긴다. 템플릿이 그대로라 새 ReplicaSet 은 생기지 않는다. ReplicaSet 컨트롤러가 파드를 지우고 kubelet 이 종료한다('Killing').
3. 엔드포인트: 파드가 종료 단계에 들어가면 Service testbed-account 엔드포인트에서 빠지고, 엔드포인트가 빈 ClusterIP 로의 연결은 kube-proxy 가 거절한다(2026-10-09 06:57~07:12 account NotReady 창의 api 로그 'Connection refused' 423건 실측).
4. 잔액 조회와 계좌 목록: `core-banking/k8s/02-configmaps.yaml:30-32` upstream account-service → testbed-account:8081, `:45-46` location /api/accounts → account 직행. upstream 연결 거절로 nginx 502. nginx 는 OTel 이 없어 서버 쪽 스팬은 남지 않고 k6 가 본다.
5. 이체: `core-banking/api-service/src/main/java/com/corebanking/api/service/ApiService.java:28-37` submitTransfer → `client/AccountClient.java:28-58` requestTransfer. RestClientException 'I/O error on POST request for "http://testbed-account:8081/api/accounts/transfer": Connection refused' 를 'Account service call failed for order {}' 로 남기고 BAD_GATEWAY, Retry(max-attempts 3, 200ms), CircuitBreaker(accountClient, 창 10, 실패율 50%, 열림 5초) fallback 'Account service circuit open/exhausted' 로 502.
6. 거래 내역: `ApiService.java:40-41` listTransfers → transferClient(api → transfer)라 account 를 지나지 않는다. commerce 정산은 commerce-payment → transfer 직행이다. 둘 다 정상이어야 한다.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`target_id`) | `core-banking-account` | 결함을 가진 곳은 운영 명령이 용량을 0 으로 둔 account 의 Deployment 다. replicas 를 되돌려야 회복하고, 원본 후속 조치처럼 최소 용량 아래로 빼는 명령을 막아야 재발이 막힌다. target_kind service |
| 계기(`trigger_target_id`) | 비움(null) | 결함을 드러낸 사건과 결함이 같은 곳(account 용량을 없앤 운영 명령)이다 |
| 부분 점수 | core-banking-api(502 를 내는 증상 서비스), testbed-nginx(502 를 내는 진입점), rca-testbed-banking(네임스페이스만 맞힘) | |
| 입도 | service | |

- 원칙 6: 이체와 잔액 조회 요청은 정당하다. 그 요청을 받을 account 의 용량을 없앤 운영 명령이 원인이다. 원본 보고가 지목한 층위('잘못 입력된 명령이 의도보다 많은 서버를 뺐다', 근본은 그 용량 제거)와 같다.
- 원칙 5: 정답은 KCM 이벤트(ScalingReplicaSet 'to 0 from 1', 앞뒤 scale up 없음)와 ReplicaSet 스펙(replicas 0, 템플릿 그대로)이라는 전수 근거로 낸다. 의도한 대상이 무엇이었는지, 누가 입력을 틀렸는지는 요구하지 않는다.
- 합리적 관제 답 확인: "account 파드가 없어 api 와 nginx 가 502"는 정답(account)을 맞힌다. "account 가 운영 명령으로 0 으로 줄었다"는 mechanism 까지 맞힌다. "api 장애"는 부분 점수다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | core-banking-api 서버 스팬 502(POST /api/transfers), api ERROR 로그 급증('Account service call failed ... Connection refused', 'circuit open/exhausted'), core-banking-account 스팬 끊김 |
| 피해 판정 | 러너 | 동반 부하 이체(step transfer) 5xx 비율 ≥ 0.5 와 잔액 조회(step get) 실패율 ≥ 0.5 |
| 원인 설명 | 녹화 데이터 | §7 의 KCM ScalingReplicaSet, Killing, PG ReplicaSet 스펙(replicas 0), 로그(전수) |

- 이 모양(account 에 닿지 못해 api 502 와 'Connection refused')의 감지는 2026-10-09 06:57~07:12 창에서 실제로 났다(api 'Account service call failed ... Connection refused' 423건과 circuit 로그 423건).
- 평시 api ERROR 로그: 잔액 부족 400 을 api 가 ERROR 로 남겨 하루 약 1,400건('Account service call failed for order null: 400 ... Insufficient', 2026-10-03~08 일별 1,358~1,404). 고장 중에는 'Connection refused' 와 'CircuitBreaker accountClient is OPEN' 문구로 바뀌고 양이 수 배다. 평시 오류율(서버 스팬 5xx)은 0 이다(업무 거절은 400, F39-R 시트 §7).
- 표본량: 동반 부하 5rps 의 이체 40% = 초당 2건이 api 서버 스팬 502, 10% 표본으로 분당 약 12개 ERROR 스팬. 로그는 전수라 분당 약 120건 이상.
- 묶음 구성 위험: commerce 는 이 고장에 영향이 없어(정산은 transfer 직행) 묶음이 banking 에 잡힐 가능성이 높다. ledger 대사 잡음과 섞일 수 있다(F39-R 시트 §6).

## 7. 관측 근거 표 (119 실조회, 2026-10-09 22:00~22:45 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), PG ReplicaSet 스펙(전수), 로그(전수)다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 용량 제거 | CH `kcm_events_local` (namespace, object_kind, object_name, reason, body) | `reason='ScalingReplicaSet' AND namespace='rca-testbed-banking'` 최근 | 'Scaled down replica set testbed-account-787b559b5c to 0 from 1'(20:50:19) 꼴로 수집된다. 보존(2026-08-21~) 943건. 평시 'to 0' 은 롤링 업데이트의 끝에서만 나고 바로 앞에 새 ReplicaSet 의 'Scaled up ... to 1' 이 있다(20:49:38 d466dc8d4 up → 20:50:19 787b559b5c down, 41초 뒤). 고장에서는 서비스 중이던(가장 새 revision) ReplicaSet 이 0 으로 내려가고 앞뒤에 scale up 이 없어야 한다 |
| 근본: ReplicaSet 스펙 | PG `kcm_resources_history` (kind='replicaset', name, yaml, captured_at) | `namespace='rca-testbed-banking' AND kind='replicaset' AND name LIKE 'testbed-account-%'` | ReplicaSet 이 바뀔 때마다 다시 담긴다: 787b559b5c 20:34:23 `"replicas":1` revision 17 → 20:50:19 `"replicas":0`, d466dc8d4 20:49:38 `"replicas":1` revision 18. 고장에서는 가장 새 revision 의 ReplicaSet 이 `"replicas":0` 으로 담기고 어느 ReplicaSet 도 1 이 아니어야 한다. Deployment 레코드는 2026-07-28 뒤로 쌓이지 않아(kind 별 최신 captured_at) 근거로 쓰지 않는다 |
| 근본: 파드 부재 | CH `kcm_events_local` | `object_name LIKE 'testbed-account-%' AND reason IN ('Killing','SuccessfulCreate','Scheduled','Started')` | 롤링 업데이트 때 Killing 과 함께 새 파드의 SuccessfulCreate, Scheduled, Pulled, Created, Started 가 같은 분에 남는다(2026-10-09 20:49:38). 고장에서는 Killing 뒤 생성 이벤트가 없다 |
| 근본: 서비스 침묵 | CH `lucida_logs_local`, VM `apm.agent.otel.java.span_count{service_name="core-banking-account"}` | 최근 1시간 | account 스팬 분당 7~14, 로그 하루 약 2.5만 줄(INFO 22,266 등). 고장 중에는 둘 다 끊긴다. account 종료 로그('shutdown' 등)는 롤링 업데이트 때도 119 에 남지 않았다(20:49~20:51 조회: 'Starting', 'Started' 만) |
| 전파: 연결 거절 로그 | CH `lucida_logs_local` body | `body LIKE '%testbed-account%' AND (body LIKE '%refused%' OR body LIKE '%unavailable%')`, 최근 8일 | core-banking-api 'Account service call failed for order null: I/O error on POST request for "http://testbed-account:8081/api/accounts/transfer": Connection refused' 423건과 'Account service circuit open/exhausted ...' 423건, 모두 2026-10-09 06:57~07:12(다른 실행 창, account 가 떠서 readiness 실패, KCM 'Unhealthy' 동반). 그 밖 0건 |
| 대조: readiness 실패 꼴 | CH `kcm_events_local` | 2026-10-09 06:50~07:15 banking | testbed-account-d466dc8d4-84hhz 'Readiness probe failed ... context deadline exceeded' 가 10초마다 반복. F51-R 은 이 'Unhealthy' 가 없다 |
| 골든 시그널(감지) | VM `apm.agent.otel.java.error_rate`, CH MV `agg_service_golden_signals` | F39-R 시트 §7(2026-10-08) | api, account 오류율 99백분위 0 |
| 피해 | 동반 부하 k6 live 문서 | 이체 step 5xx 비율, 잔액 step 실패율 | 평시 0(업무 거절은 400) |

## 8. 감별

- must_support: 정답지 5항목(scale up 없는 'Scaled down ... to 0 from 1', ReplicaSet 스펙 replicas 0 과 템플릿 그대로, 파드 Killing 뒤 생성 이벤트 없음과 account 스팬, 로그 끊김, api 'Connection refused' 와 502, 이체와 잔액 실패에 거래 내역과 commerce 정산은 정상).
- must_rule_out(정답지): account 릴리스나 설정 배포(F41-R, F39-R), account 크래시나 메모리 고갈(F41-R), 할당량이나 스케줄링(F50-R), Oracle 이나 DB 계정 장애(F35-R, F01-P), transfer 장애(F17-R, F17-H, F50-R), api 결함(F47-R, F37-R), 노드 장애, 부하 증가.
- 같은 증상과 가르는 관측 근거:

| 시나리오 | 정답 | account 파드 | 템플릿 변화 | 지문 이벤트 | 실패 범위 |
|---|---|---|---|---|---|
| F51-R | account(운영 명령) | 없음, 다시 안 생김 | 없음(replicas 만 0) | 'Scaled down ... to 0 from 1', 앞뒤 scale up 없음 | 잔액, 목록, 이체 실패. 거래 내역, commerce 정산 정상 |
| F41-R | account(릴리스) | 떠 있다가 OOM, 재시작 반복 | image 태그 1.3.0 | 새 ReplicaSet scale up, OOMKilled, Unhealthy | 주기적으로 잔액, 이체 실패 |
| F39-R | account(설정) | 떠 있음 | env TRANSFER_SERVICE_URL | 새 ReplicaSet scale up | 이체만 실패(연결 시간 초과), 잔액 정상 |
| F35-R | DB 계정 | 떠 있음, NotReady | 없음 | Unhealthy(readiness), ORA-28000 로그 | account, transfer, ledger 모두, commerce 정산도 |
| F50-R, F17-H, F17-R | transfer 쪽 | 떠 있음 | (transfer 쪽) | FailedCreate, ErrImageNeverPull, Unhealthy | 이체, 거래 내역, commerce 정산 실패. 잔액 정상 |

- 실패 범위가 결정적으로 갈린다: account 가 없으면 nginx → account 잔액 조회가 실패하고 api → transfer 거래 내역은 성공한다. transfer 쪽 장애는 그 반대다.
- 러너 판정: success 가 이체 5xx 와 잔액 실패를 함께 요구해 F39-R(잔액 정상) 꼴이면 성공하지 않는다. transfer, Oracle NotReady 는 must_rule_out 이다. F41-R 과는 러너가 가르지 않는다(account 파드는 러너 kubernetes 프로브 허용 목록에 없음). 구별은 정답지 mechanism 채점과 must_support 에 있다.
- 부하 증가 경쟁 가설: 실패는 거절된 연결이고, 같은 부하의 거래 내역은 정상이며, 실패는 scale 시각에 맞춰 0 에서 거의 1 로 뛴다.
- contrast_with: F41-R, F39-R, F50-R, F17-H, F17-R, F35-R.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f51-r`). 강도라 할 값은 replicas 하나이고 0 이면 결과가 정해진다(파드가 없음). 1 보다 작은 다른 값은 없다.

- 시간: scale → Deployment 컨트롤러가 ReplicaSet 을 0 으로(1초 안) → 파드 Terminating, 엔드포인트에서 즉시 빠짐 → 종료 유예(기본 30초) 안에 JVM 종료. 고장은 scale 직후 시작한다. settle 60s, min_hold 15m 이라 판정(성공 3틱)은 scale 약 16분 뒤.
- cleanup: replicas 를 상태 파일에 적어 둔 값(1)으로 되돌리고 Deployment available 까지 기다린다(account JVM 기동 약 33초, 2026-10-09 20:49:46 Starting → 20:50:17 Started, 상한 180초). 템플릿이 그대로라 같은 ReplicaSet 이 다시 1 이 되고 새 revision 이 생기지 않는다.
- 피해 계산: 이체 단계 5xx 비율은 계좌 검증 400(이체의 약 4%)을 빼면 1.0 이고, api 서킷이 열리면 그 400 도 502 가 된다(F39-R 시트 §9). 잔액 조회는 nginx 502 로 1.0. 두 문턱 0.5 를 크게 넘는다. 요청량과 무관한 전면 실패라 부하 상한 계산이 필요 없다.
- 서킷브레이커: api accountClient 서킷은 열린 뒤 바로 502(피해를 숨기지 않음). nginx 는 재시도 없이 502. 연결 거절은 즉시라 스레드 대기가 쌓이지 않는다.
- 진입점: nginx, api 는 그대로 떠서 502 로 답한다. abort 조건 entry_status==0(기준선 문서의 이체 단계 nginx → api)은 이 고장으로 나오지 않는다. nginx 는 프로브가 없어(`30-nginx.yaml`) account 가 없어도 Ready 이고, nginx 의 /health 는 고정 200 이다.
- 다른 서비스: transfer, ledger, Oracle, Kafka 는 account 를 부르지 않는다(grep: account 를 부르는 곳은 api AccountClient 와 nginx 뿐). commerce 정산은 transfer 직행이라 영향이 없다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f51-r`(namespace rca-testbed-banking, deployment testbed-account, from_replicas 1, replicas 0), min_hold 15m, settle 60s, timeout 20m |
| preflight(실행기) | HPA 없음, testbed-account replicas 1, available, deployments/scale 패치 권한, 상태 파일 없음(109 에서 읽기 전용 preflight 실행 통과, 2026-10-09) |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking transfer-heavy-surge.js(이체 40, 잔액 35, 거래 내역 15, 계좌 목록 10), target_rps 5, entry 30082, ramp 2m + hold 21m + ramp_down 15s, seed 5151 |
| success | 동반 부하 이체 5xx 비율 ≥ 0.5 그리고 잔액 조회 실패율 ≥ 0.5, 3틱 연속 |
| must_rule_out | achieved_rps < 1.25(부하 끊김), transfer 파드 NotReady(하류 장애), Oracle NotReady(DB 장애), 2틱 |
| abort | entry_status(domain core-banking, nginx → api) == 0, 2틱 |
| recovery | target_health 200(형식), transfer Ready(형식), 기준선 이체 5xx < 0.05, 기준선 잔액 조회 실패율 < 0.05, 2틱, 10m |
| cleanup | replicas 를 1 로 되돌리고 Deployment available 확인(180초), 동반 부하 종료, 10m |

판정 근거(평시 값과 장애 값):

| 조건 | 관측 | 평시 | 장애 중 | 판정력 |
|---|---|---|---|---|
| success | 동반 부하 이체 5xx 비율 | 0 | 약 1.0 | 있음(문턱 0.5) |
| success | 동반 부하 잔액 조회 실패율 | 0 | 약 1.0 | 있음(문턱 0.5). F39-R 꼴(잔액 정상)을 가름 |
| must_rule_out transfer-down, oracle-down | transfer, Oracle Ready | true | true | 교란(하류 장애) 때만 발화 |
| recovery | 기준선 이체 5xx, 기준선 잔액 실패율 | 0, 0 | 약 1.0, 약 1.0 | 있음 |

새 관측 쿼리와 러너 변경은 없다. 새 실행기 `k8s.scale`(`scripts/scenarios/profiles/k8s_scale_executor.py`)을 더했다. 실행기는 replicas 를 상태 파일(네임스페이스와 Deployment 이름으로 지음)에 적고 `kubectl scale` 한 번을 낸다. 이미지, 프로브, env, 자원, Service 는 건드리지 않고 아무것도 지우지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(AWS S3 us-east-1 2017-02-28 공식 사후 보고, 원문 확인)와 요소별 대응표가 있고, 기전("운영 용량 명령의 입력 실수가 요청 경로 하위 시스템의 용량을 없애 용량을 되돌릴 때까지 요청 실패")이 같다. 바꾼 세 가지(규모, 하위 시스템 수, 재시작 시간)는 §2 에 적었다 (원칙 1)
- [x] 장부를 갱신했다: 묶음 P+B(P 2→3, 5%, B 로 세도 13.3%), 정답 위치 은행 계좌 서비스(2→3, 5%), 결제 경로 10(16.7%) 그대로, 서비스 은행 18(음식배달을 고르지 않은 이유는 §3), 어느 축도 20% 에 닿지 않는다 (원칙 2)
- [x] 근본 원인 위치: 인프라 지점(rca-testbed-banking Deployment testbed-account 의 replicas, `21-account-service.yaml:9`)과 전파 코드 `AccountClient.java:28-58`, nginx `02-configmaps.yaml:45-46` (G1, §4)
- [x] 근본 원인의 흔적: CH `kcm_events_local` 의 ScalingReplicaSet 본문과 PG `kcm_resources_history` 의 ReplicaSet replicas. 둘 다 수집됨을 같은 Deployment 의 2026-10-09 롤아웃으로 확인했다. Deployment 객체 레코드는 2026-07-28 뒤로 쌓이지 않음을 적었다 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트, ReplicaSet 스펙(전수), 로그(전수) (원칙 3)
- [x] 계기의 흔적: 계기가 곧 근본(scale 명령)이고 그 흔적이 ScalingReplicaSet 이벤트와 ReplicaSet replicas 0 이다. 인공 지연을 쓰지 않는다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: scale up 없는 'to 0 from 1' 뒤 account 가 사라지고 api 가 testbed-account 로의 연결 거절을 남긴다. 의도한 대상이나 사람의 실수 경위는 요구하지 않는다 (원칙 5)
- [x] 정답지 세 칸: 근본 `core-banking-account`(service), 계기 비움(근본과 같음), 부분 점수 api, nginx, 네임스페이스 (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 실행기는 네임스페이스, Deployment, replicas 만 kubectl 로 넘기고 클러스터에 남는 것은 replicas 값과 표준 이벤트뿐이다. 동반 부하 태그는 tb-runner 의 k6 쪽에만 남는다 (원칙 8)
- [x] 피해 계산: 요청량과 무관한 전면 실패, 서킷브레이커는 피해를 숨기지 않음, 스레드 대기 없음, 동반 부하 5rps 는 banking surge 건강 상한 20rps 아래, 진입점은 살아 있음 (원칙 9, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- KCM 이 'Scaled down replica set testbed-account-<가장 새 해시> to 0 from 1' 을 남기고 앞뒤에 scale up 이 없는지, PG kcm_resources_history 가 그 ReplicaSet 을 replicas 0 으로 다시 담는지.
- account 파드 Killing 뒤 생성 이벤트가 없고 core-banking-account 스팬과 로그가 끊기는지.
- api 'Connection refused' 와 서킷 열림 로그, core-banking-api 오류율, 잔액 조회(nginx 502)와 이체 실패, 거래 내역과 commerce 정산 정상.
- 판정이 promote 해 인시던트가 생기는지, 묶음 main_service 와 상위 3개 서비스. banking promote 지연이 약 18~20분이라 cleanup 뒤 30분 이상 지나서 확인한다. nginx 502 는 서버 스팬이 없어 감지는 api 쪽 신호가 맡는다.
- cleanup 뒤 같은 ReplicaSet 이 1 로 돌아오고 새 revision 이 생기지 않는지, account Ready 시각.
