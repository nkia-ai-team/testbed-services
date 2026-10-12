---
title: F77-R 설계 시트 - 이름이 겹친 원장 워커 매니페스트가 banking transfer Deployment 를 덮어씀
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design-sheet
  - core-banking
summary: transfer-service 매니페스트를 복사해 이미지만 원장 이미지로 바꾸고 이름을 그대로 둔 새 워크로드 매니페스트를 적용하자, 살아 있는 transfer Deployment 가 원장 이미지로 롤아웃되어 원장 앱이 transfer 프로브(8082)에 답하지 못하고 재시작을 되풀이해 banking 이체와 commerce checkout 이 502 가 되는 시나리오(Coinbase 2026-07-14 재구성).
---

# F77-R 설계 시트

## 1. 요약

banking 팀이 원장 재처리 워커를 더하려고 transfer-service 매니페스트(`core-banking/k8s/22-transfer-service.yaml`)를 복사해 컨테이너 이미지만 `core-banking-ledger:latest` 로 바꾸고, 이름 `testbed-transfer` 와 라벨은 그대로 둔 채 적용한다. 이름이 겹쳐 새 워크로드가 생기지 않고 살아 있는 transfer Deployment 의 이미지 한 칸이 원장 이미지로 바뀐다. transfer 의 정본 전략 maxSurge 0 / maxUnavailable 1 이 옛 파드를 먼저 내리고, 새 파드는 transfer 의 환경으로 원장 앱을 띄워 원장 포트 8083 에서 듣는다. transfer 프로브는 8082 를 보므로 기동 확인(30회 x 5초)이 연결 거절 끝에 실패해 kubelet 이 약 150초마다 재시작한다. transfer 엔드포인트가 비어 banking 이체, 거래 내역, commerce checkout 정산이 Connection refused 끝에 502 가 된다. 잔액 조회와 진짜 원장(testbed-ledger)은 정상이다.

비유: 새 직원 책상을 마련하려고 옆 사람 명패를 복사해 이름만 바꾸려다 이름 바꾸기를 잊고 걸었더니, 이체 창구 명패 자리에 원장 담당자가 앉았다. 손님은 이체 창구를 찾아가지만 그 자리의 직원은 다른 번호로만 대답한다.

## 2. 원본 사례

- **Coinbase, 2026-07-14** — 공식 블로그 사후 보고 "A postmortem of our July 14, 2026 incident"(게시 2026-07-21), <https://www.coinbase.com/blog/a-postmortem-of-our-july-14-2026-incident>. 보조 보도: Bitcoin.com News 2026-07-22 <https://news.bitcoin.com/coinbase-reveals-how-one-configuration-error-triggered-a-50-minute-outage/>.
- 요약(출처가 말한 것만): 12:34 PM ET 공유 운영 쿠버네티스 클러스터에 일상적이고 위험이 낮아 보인 설정 변경을 배포했다. "The configuration change inadvertently modified Kubernetes resources associated with this gateway due to a collision in resource names that was not detected by our pre-production checks, resulting in unavailability of the gateway." 12:37 PM ET 클러스터로 들어오는 트래픽이 모두 멈춰("all inbound traffic to the cluster stopped") 거래, 입출금, 정산, 카드 승인, 온체인 서비스가 약 50분 영향을 받았다. 평소 되돌림 도구도 그 게이트웨이에 기대 있어 클라우드 제공자에서 일시 권한으로 되돌림 배포를 직접 실행했고, 1:20 PM ET 게이트웨이 복구, 1:23 PM ET 완화. 재발 방지로 이름 충돌 검사, 복구 도구 분리.
- 원문은 2026-10-12 직접 열면 403 이라 인용은 같은 날 웹 검색 색인의 원문 발췌와 보도에서 옮겼다(자료 문서 M1 에 추가). 어떤 종류의 자원이 겹쳤는지, 탐지 경로는 원문 발췌에 없다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 공유 운영 클러스터에 일상적 설정 변경 배포 | 원장 재처리 워커 매니페스트를 rca-testbed-banking 에 적용(KCM Deployment testbed-transfer ScalingReplicaSet) |
| 원인이 된 결함 | 사전 검사가 못 잡은 자원 이름 충돌로 변경이 게이트웨이 자원을 바꿈 | transfer 매니페스트를 복사한 새 매니페스트가 이름 testbed-transfer 를 그대로 둬, 적용이 살아 있는 transfer Deployment 의 이미지를 core-banking-ledger:latest 로 바꿈 |
| 전파 경로 | 게이트웨이를 쓸 수 없게 되어 클러스터 진입 트래픽 정지, 내부 클라이언트가 인프라 서비스에 닿지 못함 | maxSurge 0 이 옛 transfer 파드를 먼저 내림 → 새 파드는 원장 앱(8083)이라 8082 프로브 실패, 재시작 반복 → testbed-transfer 엔드포인트 없음 → account, api, commerce-payment 의 transfer 호출이 Connection refused |
| 사용자 증상 | 거래, 입출금, 정산, 카드 승인 등 약 50분 실패 | banking 이체와 거래 내역 502, commerce checkout 502(정산 단계). 잔액 조회는 정상 |
| 탐지된 경로 | 원문 발췌에 없음 | 오류율(account, api, commerce), Connection refused 로그, transfer 파드 재시작 이벤트 |
| 완화와 복구 | 변경을 되돌림(되돌림 배포) | cleanup 이 transfer 이미지를 core-banking-transfer:latest 로 되돌림 |

기전 대조: 원본의 고리는 "다른 대상을 겨냥한 일상 변경 → 이름 충돌로 살아 있는 핵심 경로 자원이 바뀜 → 그 경로를 지나는 모든 호출 실패 → 되돌림으로 복구"다. 재구성도 같다. 바뀐 자원이 게이트웨이 대신 이체 서비스(banking 이체와 commerce 정산이 모두 지나는 핵심 경로)의 Deployment 다. 원본이 어떤 자원 종류였는지 밝히지 않아, 119 에 흔적이 남는 Deployment 충돌로 둔다(Service 충돌은 Service 스펙 변경이 119 에 남지 않아 원칙 3, 4 를 못 지킨다, rejected 17행).

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 합계 113, 이 후보 전)

- 서비스(정식 + 후보): commerce 38, core-banking 37, food-delivery 38 → 가장 적은 은행에서 만든다(원칙 2, 2026-10-12 규칙). 이 후보로 은행 38, 세 도메인 동률.
- 묶음: 이 후보는 **P 운영 작업의 대상 착오**(정식 0 + 후보 13 = 13, 11.5%) → 14/114 = 12.3%. 20% 상한(22.8) 아래. 보조 B.
  - J 가 아닌 이유: 바뀐 것은 transfer 자신의 새 버전이 아니라 다른 서비스의 이미지다. G 가 아닌 이유: 값을 잘못 넣은 설정이 아니라 적용 대상이 틀렸다. P 정의("의도한 변경 내용은 맞았는데 적용 대상이 틀렸다")에 맞는다.
- 정답 위치: **은행 이체 서비스**(10, 8.8%) → 11/114 = 9.6%. 결제 경로 합계 13/114 = 11.4%. 모두 상한 아래.
- 부품 지도(은행): oracle 9, kafka 0, api 5, account 11, transfer 12, ledger 2, nginx 0. kafka, nginx 는 이번 실행에서 여러 번 막혔다(outbox 비동기라 사용자 증상 없음, nginx 는 119 미수집). 인프라 층 "이미지와 배포(롤아웃, 롤백)"의 새 기전(이름 충돌 적용)으로 transfer 를 고른다.

### 후보 목록 (3단계, 줄 세운 결과와 버린 이유)

| 순위 | 원본 사례 / 부품 / 주입 | 판정 |
|---|---|---|
| 1 | Coinbase 2026-07-14 / banking transfer Deployment / 이름이 겹친 새 워크로드 매니페스트 적용으로 이미지가 원장 이미지로 바뀜(k8s.image foreign 모드) | **채택**: 새 원본, 새 수단 모드, P 12.3%, 정답 위치 9.6%, 119 흔적(Pulled 이미지, ReplicaSet 스펙, 원장 앱 로그) 확인 |
| 2 | Coinbase 2026-07-14 / banking testbed-transfer Service / 새 워크로드의 Service 가 같은 이름으로 셀렉터를 덮어씀 | 버림: 원칙 3, 4(Service 스펙 변경이 kcm_resources_history, KCM 이벤트에 남지 않음, rejected 17행 벽) |
| 3 | Inngest 2026-07-16·07-23 / banking transfer / 릴리스가 기동 의존(새 토픽 확인)을 더해 새 파드가 Ready 불가 | 버림: 원칙 1, 6(원본 의존 대상의 장애를 재현하려면 토픽 미준비를 지어내야 하고 '스키마-코드 순서' 꼴 사람 결정 대기와 같은 정답 층위 문제) |
| 4 | Stripe 2019-07-10 / banking transfer / 최근 설정과 맞지 않는 옛 버전으로 롤백 | 버림: 원칙 1(원본은 DB 클러스터 선거 프로토콜 롤백과 설정의 CPU 고갈, 앱 재구성은 두 단계 주입에 기전 바뀜) |
| 5 | (사례 없음) / banking Oracle TRANSFERS / 보관 스크립트가 운영 표를 READ ONLY 로 | 버림: 원칙 1(표 단위 읽기 전용의 공식 사후 보고 없음, 웹 검색 1회), 카탈로그 §1(F38-R, rejected 177행과 같은 쓰기 거절 꼴) |
| 6 | (사례 없음) / banking transfer / 상태값이 열 길이를 넘는 릴리스(ORA-12899) | 버림: 원칙 1(공식 사후 보고 없음, 웹 검색 3회는 이슈와 지원 문서뿐) |
| 7 | (사례 없음) / banking account / 계좌 행 잠금을 쥔 채 transfer 를 불러 분산 교착 + 재시도 중복 | 버림: 원칙 1(공식 사후 보고 없음, 웹 검색 1회는 블로그와 이슈뿐) |
| 8 | Clerk 2026-02-19 / banking Oracle TRANSFERS 통계 / 자동 통계 뒤 계획 뒤집힘 | 버림: 원칙 1, 9(rejected 324행 벽: 거의 NULL 인 열을 거르는 뜨거운 질의가 없어 통계를 지어내야 함) |
| 9 | PostHog 2025-10-28 / banking transfer 기동 확인 임계 / 기동 확인 문턱을 정상 기동 시간 아래로 줄이는 설정 | 버림: 원칙 1(원본 계기는 CPU 과밀, rejected 237행) |
| 10 | GitLab 2026-03-17 꼴 / banking account 계좌 목록 / 예금주 마스킹 릴리스가 일부 이름에서 예외 | 순위 낮음(버림 아님): J 묶음 16%이고 은행 J 가 이미 11종, 같은 결함 꼴 세 번째(F76-R, F76-P). 1순위가 통과해 쓰지 않음 |

서로 다른 부품: transfer Deployment, Service, Kafka 토픽, Oracle 표, Oracle 통계, account 코드, 프로브(7종).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 적용: 실행기(k8s.image foreign 모드)가 `kubectl set image deploy/testbed-transfer transfer-service=core-banking-ledger:latest`. 복사한 매니페스트와 살아 있는 객체의 차이가 이 한 칸뿐이라 적용 결과와 같다(`core-banking/k8s/22-transfer-service.yaml:4` 이름, `:36-37` 이미지와 Never 정책). 원장 이미지는 `core-banking/k8s/23-ledger-service.yaml:25-30` 의 testbed-ledger 가 tb-w2 에서 쓰는 것이다(노드 status.images 에 core-banking-{transfer,account,ledger,api}:latest, 2026-10-12).
2. 롤아웃: `22-transfer-service.yaml:14-18` maxUnavailable 1 / maxSurge 0 → 옛 ReplicaSet 0, 새 ReplicaSet 1 같은 초(2026-10-11 17:02:30 선례), 옛 파드 Terminating 이 엔드포인트에서 즉시 빠짐.
3. 새 파드: 원장 이미지가 transfer 의 env(DB Secret, service-config, -Xmx512m, OTEL_SERVICE_NAME core-banking-transfer)로 뜬다. 원장 앱 `core-banking/ledger-service/src/main/resources/application.yml:1-2` server.port 8083, `:28` Kafka group-id ledger-service(banking.transfers 3 파티션 중 일부를 받아 원장 기록, `LedgerService.recordTransfer` 는 transfer_ref 로 멱등), 대사와 보존 배치가 기동 직후 한 번씩 돈다(`ReconciliationBatch.java:26`, `LedgerRetentionBatch.java:41`).
4. 프로브: `22-transfer-service.yaml:81-86` startupProbe GET :8082/actuator/health, failureThreshold 30, period 5 → 8082 를 아무도 열지 않아 'connection refused' 30회, 약 150초 뒤 kubelet 'Killing ... failed startup probe, will be restarted', 재시작 대기(BackOff 10초부터 두 배, 최대 5분).
5. 엔드포인트 없음: Ready 파드가 없어 Service testbed-transfer(ClusterIP, NodePort 30282)가 비고 kube-proxy 가 연결을 거절.
6. 호출자: account `TransferClient.java:28-58`(Connection refused, 재시도 3, 서킷 열림, 502) → api `AccountClient` 502. api `TransferClient.java:32-58`(거래 내역, 502). commerce `BankingTransferClient.java:45-60`(RestClientException → 502) → commerce-order 'Checkout payment failed ... core-banking transfer call failed' → checkout 502.

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `core-banking-transfer`(lucida 의 APM 서비스 이름). 결함 보유 대상은 transfer 의 배포 정의(파드 템플릿)다: 그것을 바로잡아야(이미지를 되돌려야) 복구되고, 이름 충돌을 막는 검사가 재발을 막는다.
- `trigger_target_id`: 비움. 계기(적용)와 결함(덮어쓴 배포 정의)이 같은 곳이다. 원장 이미지와 testbed-ledger 는 멀쩡하다.
- 원칙 6: 호출자(account, api, commerce-payment)의 요청은 늘 하던 그대로 정당하다. 원본 Coinbase 가 지목한 층위('설정 변경이 이름 충돌로 게이트웨이 자원을 바꿔 게이트웨이를 쓸 수 없게 됨')와 같게 'transfer Deployment 가 다른 워크로드의 이미지로 덮어써져 transfer 가 사라짐'을 정답으로 둔다. 왜 이름이 겹쳤는지(복사 실수)는 요구하지 않는다(원칙 5).
- `scoring.accept`: core-banking-transfer, banking-transfer, testbed-transfer. `partial`: core-banking-account, core-banking-api, commerce-payment(증상 서비스). 원장(core-banking-ledger)은 부분 점수에도 넣지 않는다: 원장은 고장 나지 않았고 잘못 놓인 이미지의 주인일 뿐이다.
- 관제 데이터로 낼 수 있는가(원칙 5): 롤아웃 이벤트 + 새 ReplicaSet, 파드 스펙의 이미지 + transfer 이름으로 나오는 원장 앱 로그 + 8082 프로브 거절이면 'transfer 가 원장 이미지로 바뀌었다'를 코드 추론 없이 낼 수 있다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | core-banking-account, core-banking-api 서버 스팬 오류율(평시 0), Connection refused 와 서킷 열림 ERROR 로그 급증, commerce-order 'Checkout payment failed' 급증, transfer 파드 재시작과 Unhealthy 이벤트 | 이체 단계 실패가 계속이라 인시던트가 생긴다(F17-H 와 같은 피해 꼴, F17-H 는 정식) |
| 피해 판정(러너) | 동반 부하 이체(step transfer) 5xx 비율 ≥ 0.5, 잔액 조회 실패율 < 0.2 | 평시 0, 장애 약 1.0 |
| RCA | KCM 롤아웃 이벤트, PG kcm_resources_history 이미지, kubelet Pulled 이미지, transfer 이름의 원장 앱 로그, 8082 기동 확인 실패 | §7 |

## 7. 관측 근거 표 (119 실조회, 2026-10-12 19:00~19:40 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 KCM 이벤트, PG 쿠버네티스 기록, 전수 로그다.

| 증거 | 119 표와 칸 | 조회 | 결과(평시) |
|---|---|---|---|
| 계기: transfer 롤아웃과 이미지 | CH `lucida.kcm_events_local`(timestamp, object_kind, object_name, reason, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-transfer%'` 2026-10-11 16:55~17:20 | 'Scaled up replica set testbed-transfer-544774c447 to 1 from 0' 와 'Scaled down ... 74b498b67b to 0 from 1' 같은 초(17:02:30), 새 파드 'Pulled: Container image "core-banking-transfer:2.2.0" already present on machine', Created, Started. Pulled 본문에 이미지 이름이 담긴다 |
| 근본: 덮어쓴 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-banking' AND name LIKE 'testbed-transfer-%'` 이미지별 집계 | ReplicaSet 165건, 파드 153건이 core-banking-transfer:latest, 릴리스 태그 2.1.0, 2.2.0 각 ReplicaSet 3·파드 2. **core-banking-ledger:latest 0건**. 2026-10-11 17:02:30 새 ReplicaSet 을 0.3초 안에 잡음 |
| 원장 앱이 transfer 로 보임 | CH `lucida_logs_local`(service_name, host_name, body) | `service_name='core-banking-transfer'` 표본 행 | host_name 이 파드 이름(testbed-transfer-74b498b67b-7sw9r), k8s_pod_name 칸은 비어 있음. 원장 앱이 transfer env(OTEL_SERVICE_NAME core-banking-transfer)로 뜨면 그 로그가 service core-banking-transfer, host 새 파드로 남는다. 기동 로그 'Starting ...Application v1.0.0' 와 'Started ... in N seconds' 가 수집됨(transfer 2026-10-11 17:12:42, 31.5초) |
| 기동 확인 실패 | CH `kcm_events_local` reason Unhealthy, Killing, BackOff | 20일 집계, transfer 2026-10-11 | Unhealthy 3,651건(banking 337), 'Startup probe failed: Get "http://10.244.1.229:8082/actuator/health": dial tcp ...: connect: connection refused' 가 기동 중 5초 간격으로 잡힘. BackOff 4건(최근 2026-10-01) — reason 이 수집되는 것을 확인 |
| 전파: 엔드포인트 없음 | CH `lucida_logs_local` | `body LIKE '%testbed-transfer%Connection refused%'` 30일 | account 'Transfer service call failed for order null: I/O error on POST request for "http://testbed-transfer:8082/api/transfers": Connection refused' 532건, api 'Transfer service list call failed: ... Connection refused' 1,080건(2026-10-11 17:03~17:13 transfer 롤아웃 구간) |
| 피해: commerce | CH `lucida_logs_local` | `body LIKE '%core-banking transfer call failed%'` 7일 | commerce-order ERROR 'Checkout payment failed, releasing reserved stock: ... Payment service error: 502 ... core-banking transfer call failed' 수집 |
| 대조: 진짜 원장, 잔액 조회 | kubectl(109 읽기), CH 로그 | testbed-ledger 가용, account 'Account validation passed' | testbed-ledger 1/1 Running(tb-w2, 11일), 잔액 조회는 account → Oracle 이라 transfer 와 무관 |

## 8. 감별

- must_support: 롤아웃 이벤트 직후 실패 시작 / ReplicaSet·파드 스펙과 Pulled 의 이미지가 core-banking-ledger:latest / transfer 이름의 원장 앱 기동 로그(LedgerServiceApplication, Tomcat 8083) / 8082 기동 확인 연결 거절 반복과 Killing, BackOff / account, api Connection refused, commerce checkout 502 / 이체 5xx ≥ 0.5, 잔액 조회 정상.
- must_rule_out: F17-H(노드에 없는 태그, 컨테이너 없음), F52-R(노드 이미지 정리, syslog ctr images rm), F65-R(transfer 앱의 ORA-12514), F17-R(readiness 경로 404), 원장 장애(testbed-ledger 정상), Oracle·account·api 장애(잔액 조회 200), 부하 증가.
- contrast_with: F17-H, F52-R, F65-R, F51-R, F17-R. F17-H, F52-R, F65-R 와는 카탈로그 관계 H(같은 증상, 다른 원인). 가르는 결정 근거는 "다른 서비스의 이미지"와 "transfer 이름으로 나오는 원장 앱 로그"다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f77-r`). 강도라 할 값은 없다: 이미지 참조가 바뀌면 옛 파드가 내려가고, 그 뒤 transfer 로 가는 호출은 요청량과 무관하게 전부 거절된다.

- 시간: set image → 옛 파드 Terminating(엔드포인트에서 즉시 빠짐) → 새 파드 원장 앱 기동(약 30~40초 뒤 Tomcat 8083), 8082 기동 확인 실패 30회 → 약 150초에 첫 재시작, 그 뒤 BackOff 10, 20, 40, 80, 160초가 더해져 15분 동안 재시작 5회 안팎. 고장은 set image 직후 시작한다. settle 60s, min_hold 15m 이라 판정(성공 3틱)은 약 16분 뒤. cleanup 은 이미지를 되돌리고 옛 ReplicaSet 파드가 Ready 가 될 때까지 기다린다(JVM 기동 약 31~45초, 상한 180초).
- 피해 계산: F17-H 와 같다. 동반 부하 5rps 의 이체 40%(2건/초)가 모두 nginx → api → account → transfer 이고 transfer 엔드포인트가 없어 502(계좌 검증 400 약 4% 도 api 서킷이 열리면 502). 성공 문턱 0.5 를 크게 넘는다. commerce 기준선 checkout 도 정산 단계에서 모두 실패한다.
- 서킷브레이커: account, api 서킷이 열리면 바로 502, commerce-payment 의 banking 호출은 서킷 없이 즉시 거절. 피해를 숨기지 않는다.
- 부작용: 원장 앱이 살아 있는 약 150초마다 ledger-service 소비자 그룹에 들어왔다 나가 리밸런스가 일어나고, 대사(원장 전수 합)와 보존 정리가 재시작마다 한 번 더 돈다(평시 10분, 1분 주기와 같은 질의). 원장 기록은 transfer_ref 멱등이라 중복이 남지 않는다. 고장 동안 새 이체가 거의 없어 원장 지연도 작다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f77-r`, min_hold 15m, settle 60s, timeout 20m |
| preflight(실행기) | 기준 이미지, Never 정책, replicas 1 과 maxSurge 0, 노드 status.images 에 기준 이미지와 원장 이미지, testbed-ledger 가 같은 노드에서 그 이미지로 가용, tb-w2 이미지 파일시스템 80% 미만(2026-10-12 71.5%), available |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking transfer-heavy-surge.js(이체 40, 잔액 35, 거래 내역 15, 계좌 목록 10), target_rps 5, entry 30082, ramp 2m + hold 21m + ramp_down 15s, seed 7717 |
| success | 동반 부하 이체 5xx 비율 ≥ 0.5, 3틱 |
| must_rule_out | achieved_rps < 1.25, 잔액 조회 실패율 ≥ 0.2, 2틱. transfer 재시작 수는 관측으로만 받는다(이 장애에서 오르는 값이라 배제 조건이 아니다) |
| abort | entry_status(core-banking, nginx → api) == 0, 2틱 |
| recovery | transfer Ready, 가용 레플리카 1, 기준선 이체 5xx < 0.05, target_health 200(형식), 2틱, 10m |
| cleanup | 이미지를 core-banking-transfer:latest 로 되돌린 뒤 available(180초). 원장 이미지와 testbed-ledger 는 건드리지 않는다. 동반 부하 종료 |

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Coinbase 2026-07-14 공식 사후 보고, 링크)와 요소별 대응표, 기전 같음(일상 변경이 이름 충돌로 핵심 경로 자원을 바꿔 그 경로 정지, 되돌림 복구) (원칙 1, §2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 P 13→14(12.3%), 정답 위치 은행 이체 서비스 10→11(9.6%), 결제 경로 13/114, 은행 37→38, 고른 이유(§3) (원칙 2)
- [x] 근본 원인 위치: `core-banking/k8s/22-transfer-service.yaml:4, 14-18, 36-37, 81-86`, `ledger-service application.yml:1-2` (G1, §4)
- [x] 근본 원인의 흔적이 119 에서 조회됨: PG kcm_resources_history 의 ReplicaSet·파드 이미지(평시 원장 이미지 0건), CH kcm_events Pulled 본문의 이미지 이름 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트, PG 쿠버네티스 기록, 전수 로그 (원칙 3)
- [x] 계기의 흔적: ScalingReplicaSet 두 줄과 새 파드 이벤트. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: 'transfer 가 원장 이미지로 롤아웃되어 8082 를 열지 못함' (원칙 5)
- [x] 정답지 세 칸: target core-banking-transfer, trigger 비움, partial 증상 서비스 (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거 구분 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: kubectl set image 의 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘, 원장 Deployment 이름뿐이고 상태 파일은 Deployment 로 이름 짓는다(테스트) (원칙 8)
- [x] 피해 계산(§9): 엔드포인트 없음이라 요청량과 무관하게 이체 전부 502, F17-H(정식)와 같은 피해 꼴 (원칙 9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 원장 앱 로그가 service core-banking-transfer, host_name 새 파드로 수집되는지('Starting LedgerServiceApplication', 'Tomcat started on port 8083'). 안 되면 must_support 셋째 항목을 Pulled 이미지와 Created, Started 로 고친다.
- kubelet 'Killing ... failed startup probe' 와 BackOff 가 수집되는지, transfer 재시작 수가 오르는지.
- cleanup 뒤 testbed-ledger 가 그대로(재시작 0)인지, 원장 이미지가 노드에 남는지.
