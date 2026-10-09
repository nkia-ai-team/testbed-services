---
title: F17-H 설계 시트 (banking transfer-service 를 노드에 없는 릴리스 이미지 태그로 롤아웃해 transfer 가 사라짐)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
summary: banking transfer-service 롤아웃이 노드 이미지 저장소에 적재된 적 없는 태그(core-banking-transfer:2.1.0)를 가리켜, 정본 전략(maxSurge 0)이 옛 파드를 먼저 내린 뒤 새 파드가 ErrImageNeverPull 로 뜨지 못하고 이체, 거래 내역, commerce 정산이 502 가 되는 시나리오. 원본은 Harness 2025-10-28 일상 배포가 레지스트리에서 삭제된 이미지를 필요로 해 새 파드가 뜨지 못한 장애.
---

# F17-H 설계 시트

## 1. 요약

banking 의 앱 이미지는 모두 `imagePullPolicy: Never` 이고, 빌드 스크립트가 노드마다 containerd 저장소에 `ctr import` 로 적재한다. 그래서 transfer 가 이미지를 가져오는 레지스트리는 tb-w2 노드의 저장소이고, 거기에는 `core-banking-transfer:latest` 만 있다. 롤아웃 하나가 testbed-transfer 의 컨테이너 이미지를 릴리스 태그 `core-banking-transfer:2.1.0` 으로 바꾼다. 이 태그는 노드에 적재된 적이 없다. transfer 의 정본 전략은 maxSurge 0 / maxUnavailable 1 이라 컨트롤러는 옛 ReplicaSet 을 0 으로, 새 ReplicaSet 을 1 로 같은 순간에 바꾼다. 옛 파드는 종료되며 Service 엔드포인트에서 빠지고, 새 파드는 tb-w2 에 스케줄되지만 `ErrImageNeverPull` 로 컨테이너가 만들어지지 않는다. 그 뒤 testbed-transfer 로 가는 모든 연결이 거절된다: account 의 이체 실행(api → account → transfer), api 의 거래 내역(api → transfer), commerce-payment 의 정산 이체(transfer FQDN), commerce 기준선의 NodePort 직행 이체. account, api 는 재시도와 서킷 끝에 502, commerce checkout 도 502 다. 잔액 조회와 계좌 목록(nginx → account → Oracle)은 정상이다. transfer 는 옛 파드 종료 뒤 로그를 하나도 남기지 않는다(새 버전 컨테이너가 뜨지 않으므로).

비유: 본점 송금부 직원을 새 담당자로 교대하기로 하고 기존 직원을 먼저 퇴근시켰는데, 새 담당자 출입증(이미지)이 발급되지 않아 건물에 들어오지 못한다. 송금 창구가 빈 채로 지점과 제휴사(commerce)의 송금 요청이 모두 돌아간다. 잔액 조회 창구는 그대로 열려 있다.

## 2. 원본 사례

- 기업: Harness(Feature Flags)
- 날짜: 2025-10-28(시작 1:47am GMT, 상태 페이지에 날짜 표기 없음. 해결 공지 2025-10-28 17:30 PDT, 사후 보고 2025-11-10 09:37 PST)
- 링크: [공식 상태 페이지 사후 보고](https://status.harness.io/incidents/2jkj4ryrktd8)
- 요약(출처가 말한 것만): SDK 트래픽을 흉내 내는 합성 검사가 실패하기 시작했고, `config.ff.harness.io/client/auth` 엔드포인트 일부 요청이 HTTP 502 였다. 일상 배포(routine deployment)가 쿠버네티스로 하여금 일부 파드를 필요한 컨테이너 이미지가 캐시되지 않은 새 노드로 다시 스케줄하게 했는데, 그 이미지는 Google Artifact Registry 에서 삭제된 상태라 새 파드가 시작하지 못했다. 이미 인증된 SDK 는 영향이 없었다. 이미지를 GAR 에 복원하고 영향받은 파드를 다시 배포해 복구했다. 재발 방지는 이미지 보존 정책 강화, 운영 이미지가 최소 기간 보존되는지 자동 검증, 이미지 pull 오류 감시와 경보.
- 같은 기전의 두 번째 사례: Pipefy 2024-05-22 2:18~2:33 PM(시간대 표기 없음) Application 전면 중단. 설정 파일에 지정한 이미지가 컨테이너 레지스트리에 없어 띄운 파드들이 이미지를 쓸 수 없었고, 이전 설정으로 되돌려 복구했다([공식](https://status.pipefy.com/incidents/wkn8llbcxxqb)). 둘 다 `ref-real-world-incidents.md` M2 에 더했다.
- 현실 비중: 바이너리(새 버전) 배포가 Google 포스트모템 트리거의 37%(M2)인데 이 장부의 J 는 정식 + 후보 0 이었다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 일상 배포(파드가 새 노드로 다시 스케줄됨) | testbed-transfer 릴리스 롤아웃(컨테이너 이미지 참조 변경) |
| 원인이 된 결함 | 배포가 필요로 한 이미지가 레지스트리에서 삭제되어 없음 | 롤아웃이 가리킨 이미지 태그가 노드 저장소(Never 정책이라 이것이 레지스트리)에 적재된 적 없음 |
| 전파 경로 | 새 파드가 시작하지 못해 그 서비스 용량이 빠짐 | 정본 전략 maxSurge 0 이 옛 파드를 먼저 내리고 새 파드는 ErrImageNeverPull → 가용 0, 엔드포인트 비어 연결 거절 → account, api, commerce-payment 실패 |
| 사용자 증상 | 인증 엔드포인트 일부 502 | banking 이체와 거래 내역 502, commerce checkout 502. 잔액 조회는 정상 |
| 원본의 탐지 경로 | 합성 검사 실패 | account, api, commerce-payment 오류율과 ERROR 로그 급증, KCM ErrImageNeverPull 이벤트, 새 ReplicaSet 의 이미지 태그 |
| 완화와 복구 | 이미지를 복원하고 파드 재배포 | 이미지 참조를 기존 태그로 되돌리는 롤아웃 |

기전은 원본과 같다: "배포가 요구한 이미지가 이미지 저장소에 없어 새 파드가 뜨지 못하고, 그 파드가 맡던 엔드포인트가 5xx 를 내며, 이미지를 되살리거나 참조를 되돌려 복구한다". 바꾼 것 둘: ① 이미지가 없는 이유. 원본은 레지스트리에서 지워졌고 여기서는 적재되지 않았다(Pipefy 꼴). 노드 저장소에서 이미지를 실제로 지우면 정리가 이미지 재적재에 기대게 되어 되돌리지 못할 위험이 있어 택하지 않았다. ② 옛 용량이 빠지는 이유. 원본은 파드가 새 노드로 옮겨졌고, 여기서는 Deployment 정본 전략(maxSurge 0)이 옛 파드를 먼저 내린다. 그 이미지가 왜 없었는지는 원본에도 없고 지어내지 않는다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 44, 이 후보 전)

| 축 | 값 |
|---|---|
| 묶음 | A 7, B 7, D 7(각 15%), C 6, G 6(13%), E 2, F 2, H 2, L 2, I 1, M 1, O 1, **J, K, N 0**. 이 후보는 **J+B(J 0→1)**. J 를 B 로 세더라도 B 7→8(45 중 18%)로 상한 아래 |
| 정답 위치 | 외부 결제 의존 6, 주문 서비스 6(각 13%), 결제 경로 합계 10(22%, 금지). 이 후보는 **은행 이체 서비스(3→4, 9%)** |
| 서비스 | 쇼핑몰 24, 은행 11, 음식배달 9 → 은행 12 |

이 후보를 고른 이유:

- 합계 0 인 묶음 J 를 처음 채운다. J 는 지금까지 "노드에 결함 이미지가 없고 앱 코드 변경은 사람 검토 대상"이라 막혔다. 이 후보는 결함 이미지를 만들지 않고 없는 태그를 가리킨다. 묶음 해석은 장부 §2 아래 'J 보충' 한 줄(J 는 실행할 수 없는 릴리스 산출물을 내보낸 롤아웃도 포함)과 §6 2026-10-09 에 적었다.
- K 와 N 은 이번에도 막혔다. K 는 CNI 가 flannel 단독이라 쿠버네티스 쪽 네트워크 주입 수단이 없고, N 은 모든 동기 호출의 서킷브레이커 때문에 F14-R 막힘 그대로다.
- 음식배달(9)을 고르지 않은 이유: 음식배달 Deployment 6개는 모두 기본 전략(maxSurge 25%)이라 같은 롤아웃은 새 파드만 실패하고 옛 파드가 계속 서비스한다(cut F05-G 의 결과와 `spec-scenario-design.md` G6 의 지적). 피해가 나지 않는다(원칙 9). 쇼핑몰에서 maxSurge 0 인 곳은 testbed-user 하나인데 정답 위치 사용자 서비스(1)가 0 이 아니라 쇼핑몰(24, 최다) 규칙에 걸린다. 은행 transfer 는 정본 매니페스트가 maxSurge 0 이다(`core-banking/k8s/22-transfer-service.yaml:14-18`).
- rejected 기록과 장부 §4-1, §5 에 같은 원본 사례나 같은 주입이 없다. 같은 "없는 이미지로 롤아웃" 주입은 cut F05-G(음성 시나리오 폐지, 장부에 없음)뿐이고, 피해가 없어 잘린 그 시나리오와 달리 이 후보는 피해가 난다.
- 같은 증상(transfer 엔드포인트가 비어 banking 이체와 commerce 정산 실패)의 F17-R 과는 원인이 다르다(§8). 그래서 id 는 F17 의 H(같은 증상, 다른 원인)다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 배포 정본: `core-banking/k8s/22-transfer-service.yaml:14-18` strategy maxUnavailable 1, maxSurge 0. `:36-37` image `core-banking-transfer:latest`, imagePullPolicy Never. nodeSelector tb-w2. 109 실배치(2026-10-09 kubectl): replicas 1, 같은 전략, progressDeadlineSeconds 600, terminationGracePeriodSeconds 30, revision 79.
2. 이미지 출처: `core-banking/k8s/build-and-deploy.sh:30-50` Phase 2 가 빌드한 이미지를 `IMPORT_SSH_NODES` 의 각 노드 containerd 에 `ctr -n k8s.io import` 로 적재한다. 레지스트리가 없다. tb-w2 노드 `status.images` 의 core-banking 이미지는 `core-banking-{transfer,account,ledger,api}:latest` 4개뿐이다(2026-10-09). 실행기 preflight 가 이 목록에서 기준 태그가 있고 장애 태그가 없음을 확인한다(109 에서 읽기 전용 preflight 실행: 통과. 장애 태그를 `:latest` 로 주면 실패. maxSurge 25% 인 testbed-account 로 주면 실패).
3. kubelet: Never 정책에 이미지가 없으면 컨테이너를 만들지 않고 `ErrImageNeverPull` 경고 이벤트('Container image "core-banking-transfer:2.1.0" is not present with pull policy of Never')를 남긴다. 파드는 Pending, 재시작 수 0.
4. 엔드포인트가 빈 Service 로의 연결은 kube-proxy 가 거절한다. 2026-10-02 08:00 transfer NotReady 창의 실제 로그 모양(119 CH): api 'Transfer service list call failed: I/O error on GET request for "http://testbed-transfer:8082/api/transfers": Connection refused', account 'Transfer service call failed for order null: I/O error on POST request for "http://testbed-transfer:8082/api/transfers": Connection refused', 그 뒤 서킷 열림 로그.
5. account: `account-service/.../client/TransferClient.java:28-58` (executeTransfer, ERROR 'Transfer service call failed', fallback 'Transfer service circuit open/exhausted' 502). api: `api-service/.../client/AccountClient.java:28-58`. commerce: `commerce/payment-service/.../client/BankingTransferClient.java:45-58` (transfer FQDN 동기 호출, RestClientException → 502, 서킷 없음) → order 가 재고를 보상하고 checkout 502(F17-R 정답지가 확인한 경로).

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`target_id`) | `core-banking-transfer` | 결함을 가진 곳은 transfer 의 배포(롤아웃이 가리킨 이미지가 저장소에 없음)다. 되돌려야 복구되는 곳도 같다. target_kind container |
| 계기(`trigger_target_id`) | 비움 | 계기(롤아웃)가 근본과 같은 곳에서 일어났다 |
| 부분 점수 | core-banking-account, core-banking-api, commerce-payment | 502 를 내는 증상 서비스. 결함이 없고 transfer 로의 연결 거절을 전할 뿐이다 |
| 입도 | service | |

- 원칙 6: 호출자들의 요청은 늘 하던 그대로 정당하다. 그 요청을 받을 파드를 없앤 것은 transfer 자신의 롤아웃이다. 원본 보고가 지목한 층위("배포가 필요로 한 이미지가 레지스트리에 없어 새 파드가 시작하지 못함")와 같다.
- 원칙 5: 정답은 KCM 의 롤아웃 이벤트, 새 ReplicaSet 의 이미지 태그, ErrImageNeverPull 이벤트라는 전수 근거로 낸다. 이미지가 왜 없었는지(빌드, 적재 누락)는 요구하지 않는다.
- 판별력 한계: F17-R(readiness 오설정)과 F01-P(Oracle 잠금으로 transfer NotReady)와 겉 증상(account, api 의 testbed-transfer 'Connection refused', 이체 502, commerce 정산 실패)과 정답 서비스(transfer)가 같다. 서비스 입도 채점으로는 'transfer' 라는 답이 세 시나리오 모두 만점이므로, 이 후보를 구별하는 힘은 정답지 mechanism 채점과 must_support 에만 있다(§8, 장부 §6).

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | core-banking-account, core-banking-api 서버 스팬 502, commerce-payment, commerce-order 오류, 각 서비스 ERROR 로그 급증 |
| 피해 판정 | 러너 | 동반 부하 이체(step transfer) 5xx 비율, 평시 0 에서 0.5 이상 |
| 원인 설명 | 녹화 데이터 | §7 의 KCM 이벤트, ReplicaSet 스펙(PG), 로그(전수) |

- 판정 입력은 CH MV `agg_service_golden_signals`(표본 SERVER 스팬 status ERROR), 최근 15분 대 그 전 3시간, 묶음 상위 3개 서비스다. 고장은 패치 직후(옛 파드 종료, 최대 30초) 전면 실패로 시작한다. 부분 실패 단계가 없다.
- 평시 오류율: core-banking-account, core-banking-api 오류율 7일 99백분위 0, MV 하루 오류 0건(F39-R 시트 §7, 2026-10-08). 업무 거절은 400(잔액 부족)이라 오류율에 안 들어간다.
- 같은 증상의 선례: 2026-10-02 08:00~08:10 UTC transfer NotReady(F01-P 실행) 때 api 'Connection refused' 와 서킷 로그 1,006건, account 560건, commerce-order 278건(10-02 하루, CH)이 났고 core-banking-api 묶음이 promote 됐다(promote 지연 약 18~20분, F39-R 시트 §6). 이 시나리오의 고장(약 15분)은 그보다 길고 동반 부하로 표본을 더 확보한다.
- 표본량(계산, F39-R 과 같은 동반 부하): 동반 부하 5rps 중 api 몫(이체 40% + 거래 내역 15%)은 초당 2.75건이고 이 시나리오에서는 둘 다 502 다(F39-R 은 이체만). 가장 한가한 KST 2~6시 기준선을 더해 api 는 초당 약 3.1건 중 약 2.9건이 오류, 10% 표본으로 분당 약 17개 ERROR 서버 스팬이다.
- 묶음 구성 위험: banking ledger 대사 불일치 잡음 인시던트(하루 약 34건)와 같은 묶음이 될 수 있다(F39-R 시트 §6). commerce 쪽 오류가 같은 창에 함께 나서 묶음이 commerce 로 잡힐 수도 있다.

## 7. 관측 근거 표 (119 실조회, 2026-10-09 04:30~05:10 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 KCM(이벤트 전수, ReplicaSet 스펙 전수)과 로그(전수)다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 롤아웃이 가리킨 이미지 | PG `kcm_resources_history` (kind, name, yaml 의 "image", captured_at) | `namespace LIKE 'rca-testbed-%' AND kind='replicaset'` 이미지별 집계, testbed-transfer 최근 레코드 | testbed-transfer 레코드는 전부 "image":"core-banking-transfer:latest"(ReplicaSet 175건, 파드 155건), ReplicaSet yaml 에 "imagePullPolicy":"Never" 가 담김. 마지막 transfer 롤아웃 2026-10-01 17:31:03 새 ReplicaSet 74b498b67b 를 잡음. 다른 태그가 잡힌 선례: cut F05-G 의 commerce-payment 다른 태그 ReplicaSet(2026-07-21)과 Pending 파드 레코드 |
| 근본: 이미지 부재 이벤트 | CH `kcm_events_local` (namespace, object_name, reason, event_type, body) | `reason ILIKE '%image%' OR body ILIKE '%pull policy%'`, 보존 전체(2026-08-13 05:28~) | ErrImageNeverPull, Failed 사유 모두 0건(선례 없음, 수집 여부는 첫 실행에서 확인). 같은 kubelet 의 이미지 이벤트는 수집된다: 2026-10-01 17:31:03 testbed-transfer 파드 'Pulled' 'Container image "core-banking-transfer:latest" already present on machine', kubelet Warning(BackOff, Unhealthy, FailedToRetrieveImagePullSecret)도 수집 |
| 계기: 롤아웃 | 같은 표 | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-transfer%'` | 롤아웃 모양 확인: 2026-10-01 17:31:03 ScalingReplicaSet 'Scaled down replica set testbed-transfer-64dd67bbcc to 0 from 1' 와 'Scaled up replica set testbed-transfer-74b498b67b to 1 from 0' 가 같은 초(maxSurge 0), SuccessfulDelete, Scheduled, Pulled, Created, Started. 고장에서는 새 파드에 Pulled, Created, Started 대신 ErrImageNeverPull 이 나와야 한다 |
| 전파: 연결 거절 로그 | CH `lucida_logs_local` body | `body LIKE '%testbed-transfer%Connection refused%'`, 보존 전체(2026-10-02 04:00~) | 2026-10-02 하루만 core-banking-api 1,006, core-banking-account 560, commerce-order 278건(F01-P 실행 창), 그 밖 0건 |
| 대조: transfer 자신의 로그 | 같은 표 | 2026-10-02 08:00~08:15 core-banking-transfer WARN/ERROR | F01-P 창의 transfer 는 떠 있어 'SQL Error', 'HikariPool-1 - Connection is not available' 를 남겼다. 이 시나리오에서는 옛 파드 종료 뒤 transfer 로그가 0 이어야 한다 |
| 골든 시그널(감지) | VM `apm.agent.otel.java.error_rate`, CH MV `agg_service_golden_signals` | F39-R 시트 §7(2026-10-08) | account, api 오류율 99백분위 0, 하루 오류 0건 |
| 호출량(보조) | CH `otel_traces_local` SERVER 스팬 | 최근 1시간, core-banking-* | transfer POST /api/transfers 표본 환산 약 1.3rps(p50 5.7ms), GET /api/transfers 약 1rps(p50 462ms), account POST /api/accounts/transfer 약 0.46rps |
| 피해 | 동반 부하 k6 live 문서 | 이체 step 5xx 비율 | 평시 0(업무 거절은 400) |

## 8. 감별

- must_support: 정답지 5항목(롤아웃 이벤트와 새 ReplicaSet 이미지 태그, ErrImageNeverPull 반복과 Pulled/Created/Started 부재, account, api 의 testbed-transfer Connection refused 와 502, commerce checkout 502, transfer 로그 부재와 재시작 0, 동반 부하 이체 5xx 와 잔액 조회 정상).
- must_rule_out(정답지): readiness 오설정(F17-R), transfer DB 나 Oracle 장애(F01-P, F35-R), 결함 있는 새 버전의 기동 실패나 크래시 루프, 노드 장애, account, api 장애, 부하 증가.
- F17-R 과의 구별(같은 증상): F17-R 은 readinessProbe 경로를 404 로 바꾼 롤아웃이라 새 컨테이너가 떠서 기동 로그를 남기고 kubelet 'Unhealthy' readiness 이벤트가 반복되며 새 ReplicaSet 스펙에서 바뀐 칸이 readinessProbe 다. F17-H 는 새 컨테이너가 만들어지지 않아 기동 로그가 없고, 'ErrImageNeverPull' 이 반복되며 바뀐 칸이 image 다. 러너 판정에서는 둘 다 transfer NotReady 라 구별하지 않는다(배제 조건은 크래시 루프만 가른다).
- 부하 증가 경쟁 가설: 실패는 거절된 연결이고, 같은 부하의 35% 인 잔액 조회는 정상이며, 실패는 롤아웃 시각(패치 직후)에 맞춰 0 에서 거의 1 로 뛴다. 동반 부하 5rps 는 banking surge 실측 건강 상한 20rps 아래다.
- contrast_with: F17-R, F39-R, F35-R, F05-G(cut).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f17-h`). 강도라 할 값은 없다: 이미지 참조는 바뀌거나 되돌려질 뿐이고, 옛 파드가 내려간 뒤 transfer 로 가는 호출은 요청량과 무관하게 전부 거절된다.

- 시간: 패치 → 옛 파드 Terminating(엔드포인트에서 즉시 빠짐, 종료 최대 30초) → 새 파드 ErrImageNeverPull. 고장은 패치 직후 시작한다(F39-R 처럼 새 파드 기동 1분을 기다리지 않는다). settle 60s, min_hold 15m 이라 판정(성공 3틱)은 패치 약 16분 뒤, 전면 실패는 약 16분. progressDeadlineSeconds 600 이 지나면 Deployment 가 ProgressDeadlineExceeded 가 되지만 상태 표시일 뿐 고장 모양은 같다. cleanup 은 이미지를 되돌리고 옛 ReplicaSet 파드가 Ready 가 될 때까지(JVM 기동 약 45초, 2026-10-01 'Started ... in 39.7 seconds', 상한 180초) 기다린다. 그 동안에도 실패가 이어진다.
- 피해 계산: 이체 단계 5xx 비율은 계좌 검증 400(ACC-1006 출금 일부, 이체의 약 4%)을 빼면 1.0 이고, api 서킷이 열리면 그 400 도 502 가 된다(F39-R 시트 §9). 성공 문턱 0.5 를 크게 넘는다.
- 서킷브레이커: account, api 의 서킷은 열린 뒤 바로 502 로 답하고 half-open 시도도 거절된다. commerce-payment 의 banking 호출은 서킷이 없어 요청마다 즉시 거절된다. 피해를 숨기지 않는다.
- 스레드: 연결 거절은 즉시라 대기 요청이 쌓이지 않는다.
- 진입점: nginx, api 는 살아서 502 로 답하므로 abort 조건 entry_status==0 은 이 고장으로 나오지 않는다. commerce 진입점(target_health)도 무관하다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f17-h`, min_hold 15m, settle 60s, timeout 20m |
| preflight(실행기) | 기준 이미지, Never 정책, replicas 1 과 maxSurge 0, 노드 status.images(기준 태그 있음, 장애 태그 없음), tb-w2 이미지 파일시스템 사용률 80% 미만, available |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking transfer-heavy-surge.js(이체 40, 잔액 35, 거래 내역 15, 계좌 목록 10), target_rps 5, entry 30082, ramp 2m + hold 21m + ramp_down 15s, seed 1717 |
| success | 동반 부하 이체 5xx 비율(`loadgen.food_create_status_rate`, 선택자 business.5xx.rate) ≥ 0.5, 3틱 연속 |
| must_rule_out | achieved_rps < 1.25(부하 끊김), 잔액 조회 실패율 ≥ 0.2(account 나 Oracle 장애), transfer 재시작 수 ≥ 2(이미지는 있고 새 버전이 기동 중 죽는 크래시 루프), 2틱 |
| abort | entry_status(domain core-banking, nginx → api) == 0, 2틱 |
| recovery | transfer 파드 Ready, transfer 가용 레플리카 1, 기준선 이체 5xx 비율 < 0.05, target_health 200(형식 조건), 2틱, 10m |
| cleanup | 이미지를 core-banking-transfer:latest 로 되돌린 뒤 Deployment available 확인(180초), 동반 부하 종료, 10m |

판정 근거(평시 값과 장애 값):

| 조건 | 관측 | 평시 | 장애 중 | 판정력 |
|---|---|---|---|---|
| success | 동반 부하 이체 5xx 비율 | 0 | 약 1.0 | 있음(문턱 0.5) |
| must_rule_out account-path-failing | 잔액 조회 실패율 | 0 | 0 | 교란(account, Oracle 장애) 때만 오름 |
| must_rule_out transfer-crashloop-alternative | transfer 재시작 수 | 0 | 0(컨테이너가 없음) | 다른 원인(크래시 루프) 때만 오름 |
| recovery | 기준선 이체 5xx 비율, transfer Ready, 가용 레플리카 | 0, true, 1 | 약 1.0, false, 0 | 있음 |

새 관측 쿼리와 러너 변경은 없다. 새 실행기 `k8s.image`(`scripts/scenarios/profiles/k8s_image_executor.py`)를 더했다. 실행기는 `kubectl set image` 로 컨테이너 이미지 한 칸만 바꾸고, preflight 에서 살아 있는 객체의 기준 이미지, Never 정책, replicas 1 과 maxSurge 0, 노드 status.images 에 기준 태그가 있고 장애 태그가 없음, available 을 확인한다. 노드에서 이미지를 지우지 않는다.

이미지 GC 위험(평가 권고 반영): 고장 동안 core-banking-transfer:latest 를 쓰는 컨테이너가 없고, Never 정책에 레지스트리가 없어 노드 사본이 유일하다. tb-w2 kubelet 은 imageGCHighThresholdPercent 85, Low 80, imageMinimumGCAge 2m 이고 2026-10-09 이미지 파일시스템(노드 파일시스템과 같음) 사용률이 79.7% 였다. 85% 를 넘으면 GC 가 쓰이지 않는 이 이미지를 지울 수 있어 cleanup 이 transfer 를 되살리지 못한다. 그래서 preflight 가 사용률 80%(GC 하한) 미만을 요구한다(GC 가 시작되려면 약 2GB 가 더 쓰여야 한다). 그래도 지워졌다면 cleanup 은 롤백하기 전에 노드에 기준 이미지가 없음을 알리고 실패한다. 109 에서 같은 이미지(docker `core-banking-transfer:latest`, ID b52ed7dbcd04, 노드 사본과 같은 ID)를 `docker save core-banking-transfer:latest | ssh -i /root/.ssh/tb_key nkia@192.168.122.11 'sudo ctr -n k8s.io images import -'` 로 다시 적재한 뒤 cleanup 을 다시 돌리면 된다. 디스크가 80% 를 넘은 채로 큐 차례가 오면 preflight 가 실패해 실행이 시작되지 않는다(사람이 tb-w2 디스크를 정리할 일).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Harness 2025-10-28 공식 사후 보고, 같은 기전의 Pipefy 2024-05-22)와 요소별 대응표가 있고, 기전("배포가 요구한 이미지가 저장소에 없어 새 파드가 뜨지 못하고 그 엔드포인트가 5xx, 이미지나 참조를 되돌려 복구")이 같다 (원칙 1, §2)
- [x] 장부를 갱신했다: 묶음 J+B(J 0→1, 장부 §2 'J 보충' 줄, B 로 세도 18%), 정답 위치 은행 이체 서비스(3→4, 9%), 결제 경로와 무관, 서비스 은행 12(음식배달을 고르지 않은 이유는 §3), 어느 축도 20% 에 닿지 않는다 (원칙 2)
- [x] 근본 원인 위치: `22-transfer-service.yaml:14-18`(전략), `:36-37`(image, Never), `build-and-deploy.sh:30-50`(노드별 적재), tb-w2 노드 status.images (G1, §4)
- [x] 근본 원인의 흔적: PG `kcm_resources_history` ReplicaSet 스펙의 image(평시 transfer ReplicaSet 175건, 파드 155건 전부 :latest, 다른 태그 ReplicaSet 이 잡힌 선례 있음), CH `kcm_events_local` 의 kubelet 이미지 이벤트(같은 수집 경로의 'Pulled' 가 잡힘) (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트와 ReplicaSet 스펙(전수), 로그(전수) (원칙 3)
- [x] 계기의 흔적: testbed-transfer 롤아웃 KCM 이벤트(ScalingReplicaSet 두 줄)와 새 ReplicaSet 스펙. 인공 지연을 쓰지 않는다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 직후 ErrImageNeverPull 과 새 이미지 태그, 그 직후 transfer 로의 연결 거절 (원칙 5)
- [x] 정답지 세 칸: 근본 `core-banking-transfer`(container), 계기 비움, 부분 점수 account, api, commerce-payment (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 실행기는 네임스페이스, Deployment, 컨테이너, 두 이미지 참조만 kubectl 로 넘기고 클러스터에 남는 것은 이미지 태그 `2.1.0` 과 롤아웃뿐이다(cut F05-G 의 태그는 시나리오 id 를 담았다. 이 후보는 담지 않는다). 동반 부하 태그는 tb-runner 의 k6 쪽에만 남는다 (원칙 8)
- [x] 피해 계산: 요청량과 무관한 전면 실패, 서킷브레이커는 피해를 숨기지 않음, 스레드 대기 없음, 동반 부하 5rps 는 건강 상한 20rps 아래 (원칙 9, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- KCM 이 새 파드의 `ErrImageNeverPull` 경고 이벤트를 실제로 잡는지(같은 kubelet 의 'Pulled', BackOff 는 잡히지만 보존 2026-08-13~ 전체에 ErrImageNeverPull, Failed 사유는 0건). 안 잡히면 근본 증거는 새 ReplicaSet 스펙의 image `core-banking-transfer:2.1.0`(전수, 평시 ReplicaSet 175건, 파드 155건 전부 :latest)과 그 파드의 Pulled/Created/Started 이벤트 부재로 좁아지므로 정답지 must_support 2 를 녹화본에 맞게 고친다. Pending 파드 레코드는 정상 롤아웃에도 남아 근거로 쓰지 않는다.
- 시작 전 tb-w2 이미지 파일시스템 사용률(preflight 80% 미만)과 고장 중 기준 이미지가 노드에 남아 있는지.
- 옛 파드가 패치 직후 내려가는지, 고장 시작 시각(엔드포인트가 비는 시각)과 옛 파드 종료 로그.
- account, api, commerce 의 'Connection refused' 로그와 오류율, 이체 p95, commerce checkout 5xx.
- 판정이 promote 해 인시던트가 생기는지, 묶음 main_service 와 상위 3개 서비스(commerce 쪽 오류와 섞이는지, ledger 잡음과 섞이는지). banking promote 지연이 약 18~20분이라 cleanup 뒤 30분 이상 지나서 확인한다.
- cleanup 뒤 옛 ReplicaSet 파드가 Ready 가 되고 기준선 이체 5xx 비율이 0 으로 돌아오는 시각.
