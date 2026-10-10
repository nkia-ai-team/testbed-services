---
title: F52-R 설계 시트 (banking 워커의 이미지 보존 정리가 쓰고 있는 앱 이미지를 지워 transfer 재배포의 새 파드가 뜨지 못함)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - kubernetes
  - node
summary: 운영자의 이미지 보존 정리가 banking 워커 tb-w2 의 containerd 저장소에서 '레지스트리 다이제스트가 없는 = 지난 빌드' 규칙으로 앱 이미지를 지우는데, 이 테스트베드의 앱 이미지는 레지스트리 없이 노드에 적재되어 다이제스트가 없어 쓰고 있는 이미지 넷이 모두 지워진다. 떠 있는 컨테이너는 그대로지만 transfer-service 일상 재배포(maxSurge 0)가 옛 파드를 내린 뒤 새 파드가 ErrImageNeverPull 로 뜨지 못해 이체, 거래 내역, commerce 정산이 502 가 되는 시나리오. 원본은 Logto 2023-12-17(자동 이미지 보존 작업이 '태그 없는 옛 이미지' 규칙으로 운영 이미지를 지워 서비스가 이미지를 가져오지 못함).
---

# F52-R 설계 시트

## 1. 요약

banking 의 앱 이미지(api, account, transfer, ledger)는 모두 `imagePullPolicy: Never` 이고, 빌드 스크립트가 레지스트리를 거치지 않고 `docker save | ctr -n k8s.io images import` 로 워커 tb-w2 의 containerd 저장소에 직접 넣는다. 그래서 그 노드 저장소가 이 서비스들이 이미지를 받는 유일한 곳이고, 거기 있는 앱 이미지에는 레지스트리 다이제스트(repoDigests)가 없다(nginx, Kafka, Oracle 처럼 레지스트리에서 받은 이미지에는 있다).

운영자의 이미지 보존 정리가 tb-w2 에서 "레지스트리 다이제스트가 없는 banking 앱 이미지 = 레지스트리에서 밀려난 지난 로컬 빌드"로 보고 지운다. 이 규칙은 노드에서 쓰고 있는 `core-banking-api`, `core-banking-account`, `core-banking-transfer`, `core-banking-ledger:latest` 를 모두 고른다. 이미지마다 이름과 CRI 가 옆에 두는 `sha256:<이미지 ID>` 참조를 `sudo ctr -n k8s.io images rm` 으로 지운다. 이미 떠 있는 컨테이너는 자기 스냅숏으로 돌기 때문에 아무것도 멈추지 않고 모든 서비스가 계속 답한다.

정리는 노드가 다음 컨테이너를 띄워야 할 때 드러난다. 그 컨테이너는 transfer-service 의 일상 재배포(`kubectl rollout restart`)에서 나온다. transfer 의 정본 전략은 maxSurge 0 / maxUnavailable 1 이라 옛 파드가 먼저 내려가고, tb-w2 에 스케줄된 새 파드는 kubelet 이 컨테이너를 만들지 못한다(`ErrImageNeverPull: Container image "core-banking-transfer:latest" is not present with pull policy of Never`). transfer 컨테이너가 하나도 남지 않아 testbed-transfer 엔드포인트가 비고, account 의 이체, api 의 거래 내역, commerce-payment 의 정산 이체가 모두 Connection refused 로 끝나 account, api, commerce checkout 이 502 다. 잔액 조회(nginx → account → Oracle)는 정상이다. Deployment 의 파드 템플릿(이미지 이름, 프로브, env, 자원)과 replicas, 네임스페이스 할당량은 그대로다.

비유: 창고 정리 담당이 "바코드 없는 상자는 반품된 옛 재고"라는 규칙으로 선반을 비웠다. 그런데 이 가게는 공장에서 직접 받아 바코드를 붙이지 않은 채 쓰던 상자가 모두 그랬다. 이미 매대에 꺼내 둔 물건은 그대로 팔리니 아무도 모른다. 저녁에 송금 창구 담당이 교대하며 새 물건을 꺼내러 가자 선반이 비어 창구를 열지 못하고, 모든 송금이 돌아간다.

## 2. 원본 사례

- 기업: Logto(오픈소스 인증 서비스, Logto Cloud)
- 날짜: 2023-12-17(UTC)
- 링크: [공식 블로그 사후 보고 "Postmortem: Docker image not found"](https://blog.logto.io/postmortem-docker-image-not-found)
- 요약(출처가 말한 것만): Logto cloud 와 core 서비스가 약 18분 중단됐다. 원인은 자동 GitHub 이미지 보존(retention) 작업이 운영 Docker 이미지(`logto`, `logto-cloud`)를 잘못 지운 것이다("The automated GitHub image retention workflow deleted the production images by mistake"). 작업의 의도는 "3일 지난 태그 없는 옛 이미지"를 지우는 것이었고, 운영 배포마다 새 이미지에 `prod` 태그를 옮겨 옛 이미지가 태그 없이 남는 방식이었다. 이미지를 buildx 로 다중 아키텍처로 빌드해 `prod` 를 포함한 태그는 매니페스트 목록에만 있고 아키텍처별 하위 이미지에는 태그가 없어, 작업이 그 하위 이미지를 지워 매니페스트 목록이 깨졌다. 그 결과 클라우드 서비스가 GitHub Container Registry 에서 이미지를 가져오지 못해("failed to fetch the image") 사용할 수 없게 됐다. 시간표(UTC): 03:56 두 서비스 사용 불가와 감시 탐지, 04:03 당직 확인, 04:10 최신 이미지로 두 서비스 새 배포, 04:15 사용 가능. 완화는 보존 작업을 멈추고 `prod` 태그의 새 이미지를 배포한 것. 기여 요인은 작업을 검토, 시험 없이 운영에 낸 것과 삭제 규칙을 정하기 전에 이미지의 태그와 매니페스트 목록 구조를 확인하지 않은 것. 보고는 그 시각에 무엇이 이미지를 다시 가져오게 했는지(재시작, 확장, 배포)는 적지 않는다. `ref-real-world-incidents.md` M22 에 더했다.
- 함께 볼 계열: 같은 M22 의 AWS S3 2017-02-28(F51-R 원본, 운영 명령 입력 실수로 의도보다 많은 용량 제거), GitHub 2026-07-24(F48-R, F48-P 원본, 취소된 백필의 정리가 살아 있는 표를 치움). 셋 다 "운영 작업이나 정리가 의도한 대상이 아닌 살아 있는 대상을 지움"이고 지운 대상(서버 용량, 표, 이미지)이 다르다.
- 현실 비중: 운영 명령, 스크립트 실수는 M22, Google 포스트모템 트리거의 설정 배포 31% 와 별개로 Ghosh 2022 배포 오류 20% 안에 든다. 이 테스트베드의 인프라 층 '이미지와 배포' 에서 정답이 노드 저장소인 시나리오는 없었다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 서비스가 이미지를 다시 가져와야 함(보고는 무엇 때문인지 적지 않음) | transfer 일상 재배포가 새 컨테이너를 띄워야 함(rollout restart, 파드 템플릿 그대로) |
| 원인이 된 결함 | 보존 작업의 규칙("태그 없는 3일 지난 옛 이미지")이 태그가 매니페스트 목록에만 있는 운영 이미지의 하위 이미지와 맞아 운영 이미지를 지움 | 노드 이미지 정리의 규칙("레지스트리 다이제스트 없는 앱 이미지 = 지난 빌드")이 레지스트리 없이 노드에 적재된 운영 이미지와 맞아 쓰고 있는 이미지 넷을 지움 |
| 전파 경로 | 서비스가 레지스트리에서 이미지를 가져오지 못함 → 서비스 사용 불가 | 새 파드가 노드 저장소에서 이미지를 찾지 못함(ErrImageNeverPull, 이미지를 받을 곳이 노드뿐) → maxSurge 0 이라 옛 파드는 이미 내려가 transfer 가용 0, 엔드포인트가 비어 연결 거절 → account, api, commerce-payment 실패 |
| 사용자 증상 | Logto cloud, core 약 18분 사용 불가 | banking 이체와 거래 내역 502, commerce checkout 502. 잔액 조회 정상 |
| 원본의 탐지 경로 | 감시가 03:56 사용 불가 탐지, 서비스 로그 | account, api, commerce 오류율과 ERROR 로그 급증, KCM 의 재배포와 ErrImageNeverPull, tb-w2 syslog 의 sudo ctr images rm 과 containerd ImageDelete |
| 완화와 복구 | 보존 작업을 멈추고 운영 이미지를 새로 배포 | 109 docker 의 같은 이미지(이미지 ID 비교)를 노드에 되돌려 넣고, 1분 안에 안 뜨면 다시 배포 |

기전은 원본과 같다: "이미지 보존 정리의 선택 규칙이 운영 이미지가 저장된 모양(원본은 태그가 매니페스트 목록에만 있음, 여기는 레지스트리 다이제스트 없이 노드에 적재)과 맞아 쓰고 있는 이미지를 지우고, 그 이미지를 가져와야 하는 순간 서비스가 뜨지 못하며, 이미지를 되돌리면 회복한다". 바꾼 것 둘: ① 이미지 저장소. 원본은 레지스트리(GHCR)이고 여기서는 `imagePullPolicy: Never` 라 노드의 containerd 저장소가 그 역할을 한다(F17-H 와 같은 대응). ② 이미지를 다시 가져오게 한 사건. 원본 보고는 적지 않았고, 이 테스트베드의 앱은 고정 파드라 재배포 때만 새 컨테이너가 필요해 F50-R 처럼 계기를 transfer 일상 재배포로 둔다. 옛 파드가 먼저 내려가는 정본 전략(maxSurge 0)인 transfer 를 고른다. 정리 규칙의 "3일" 같은 나이 조건은 넣지 않는다(재빌드 시점에 따라 preflight 가 흔들리고, 원본에서도 결함은 나이가 아니라 '태그 없음' 판정에 있었다).

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 62, 이 후보 전)

| 축 | 값 |
|---|---|
| 묶음 | A 7, B 7, D 7, G 7, J 7(각 11%), C 6, L 6(각 10%), F 3, P 3(4.8%), E 2, H 2, K 2, I 1, M 1, O 1, N 0. 이 후보는 **P+B(P 3→4, 63 중 6.3%)**. B 로 세도 7→8(12.7%) |
| 정답 위치 | 주문 서비스 7(11%), 외부 결제 의존 6, 은행 이체 서비스 6, 노드, 디스크 5(8%). 결제 경로 합계 10(16%). 이 후보는 **노드, 디스크(5→6, 9.5%)**, 결제 경로 그대로(15.9%) |
| 서비스 | 쇼핑몰 26, 은행 19, 음식배달 17 → 은행 20 |
| 부품 지도 | 인프라 층 '이미지와 배포'에서 노드 이미지 저장소가 정답인 시나리오 0(F17-H 는 롤아웃이 가리킨 릴리스 태그가 정답, 서비스 쪽) |

이 후보를 고른 이유:

- 묶음 P 는 4.8% 로 적고, 정답 위치 노드, 디스크도 상한(20%)에서 멀다. 정답이 노드인 시나리오(F05-P, F10-H, F15-P, F44-R, F44-P)는 모두 노드 자원, 디스크 IO, 방화벽이었고 노드의 이미지 저장소는 처음이다.
- 같은 증상(transfer 파드가 없어 banking 이체와 commerce 정산 502)에 정답이 다른 짝이 넷 있다: F17-H(롤아웃이 노드에 없던 릴리스 태그를 가리킴, 이벤트 사유까지 같은 ErrImageNeverPull), F50-R(할당량 승인 거절), F51-R(replicas 0, account 쪽), F17-R(readiness). 특히 F17-H 와는 kubelet 이벤트 사유가 같아, 관제 AI 가 "ErrImageNeverPull 이면 롤아웃한 그 서비스" 라는 지름길로 찍으면 계기(부분 점수)에 그친다. 가르는 근거(새 ReplicaSet 의 이미지 이름이 그대로, 직전 노드의 sudo ctr images rm 과 containerd ImageDelete)는 §8 에 있다.
- 음식배달(17)을 고르지 않은 이유: food Deployment 는 모두 기본 전략(maxSurge 25%)이라 새 컨테이너가 뜨지 못해도 옛 파드가 계속 서비스한다(F50-R 시트 §3 과 같은 판단). food MySQL 은 이미지 저장소에서 지워지면 재시작 때 MySQL 이 뜨지 못해 order readiness(DB 확인)가 빠지고 entry 0 이 되며, 그 재시작 시각도 정할 수 없다(컨트롤러 필수 중단 조건). 쇼핑몰(26)은 가장 많고 노드, 디스크는 0 이 아니다.
- rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본(Logto)이나 같은 주입(노드 저장소 이미지 삭제)이 없다. rejected 의 '잘못된 산출물 롤아웃 또는 노드에 남은 옛 이미지로 롤백'은 다른 이미지로 바꾸는 롤아웃이라 다르고, '노드의 <none> 이미지는 kubelet 이미지 GC 대상이라 재현 불가'는 이 후보와 무관하다(지우는 것은 이름 붙은 운영 이미지이고 109 docker 에 같은 ID 가 있다).

### 3단계 후보 목록 (실제 기전 × 부품, 순서는 숫자 순)

| 순위 | 후보 | 원본 사례 | 부품 | 결과 |
|---|---|---|---|---|
| 1 | 노드 이미지 보존 정리가 쓰고 있는 banking 앱 이미지를 지우고 transfer 일상 재배포 | Logto 2023-12-17 공식 | 노드 이미지 저장소(노드, 디스크 5), 은행 | **채택(F52-R)** |
| 2 | food MySQL 의 큰 표(dispatches, orders)에 온라인 스키마 마이그레이션이 피크와 겹쳐 DB 포화 | GitHub 2026-05-04 공식 월간 보고(일상 온라인 스키마 마이그레이션이 크고 많이 쓰는 표에서 돌며 DB 연결 용량 포화, 마이그레이션 일시 중지로 회복) | food MySQL, DB 테이블(배차 1, 주문 0) | 버림(원칙 9, 컨트롤러 필수 중단 조건): 109 실측 dispatches 127+154MB, orders 114+250MB, 버퍼 풀 128MB, MySQL CPU 0.5. 재구축 스레드 하나가 OLTP 를 얼마나 늦출지 계산이 서지 않고(F49-R 시트 후보 5 와 같은 결론), 행 복사(INSERT … SELECT, REPEATABLE READ)는 원본 표 끝에 공유 잠금을 걸어 주문 INSERT 가 기다리면 order 풀(15)이 묶여 readiness 위험(rejected 반복 15 order_items 와 같은 벽). Oracle 판은 인덱스 빌드가 10~15초라 너무 짧다(반복 11 실측) |
| 3 | banking 정산 계좌(commerce-settlement) 갱신을 낙관적 비교 갱신과 백오프 없는 재시도(상한 500)로 바꾼 릴리스 | Square 2017-03-16 공식(인접 서비스 배포 뒤 Redis 낙관적 트랜잭션 재시도가 상한 500, 백오프 없이 돌아 Redis 용량 한계) | 은행 이체 서비스, Oracle | 버림(원칙 9, 1): 평시 정산 이체 동시성(초당 수 건 × 수 ms)이 낮아 충돌이 드물고 재시도 폭주 계산이 서지 않음. 원본처럼 폭주하려면 인접 배포라는 두 번째 계기가 필요하고, 묶음 N(0)을 채우는 데 쓸 수 없음 |
| 4 | '마지막 조회 시각' 갱신을 더한 릴리스가 읽기 모델에 없는 칸을 빈 값으로 덮어씀 | Cloudflare 2023-01-24 공식(서비스 토큰 'last seen' 갱신이 비밀값을 가린 읽기 모델로 쓰기를 해 client secret 이 빈 문자열이 됨, 토큰 인증 실패) | food 메뉴, 가게 표 / banking 계좌 | 버림(F34-R 교훈): 덮어써질 칸(available, status)이 order, account 의 400 거절(4xx 단일 신호)로만 나타나고 5xx 로 이어지는 칸이 없음. 원본의 비밀값에 해당하는 것은 commerce 인증 토큰뿐인데 쇼핑몰은 최다 서비스이고 정답 위치(사용자 서비스, DB 테이블(인증 토큰))가 0 이 아님 |
| 5 | 외부 PG 가 새(더 긴) 결제 상태값을 보내 payment 가 VARCHAR(16) status 에 저장하지 못함(1406) | Cloudflare 2023-10-04 공식(루트 영역의 새 레코드 유형 ZONEMD 를 해석하지 못함) | food 결제 서비스(3), 결제 경로 | 버림(원칙 1): 원본은 해석 실패 뒤 묵은 영역을 계속 제공하다 서명 만료로 SERVFAIL 이라 고리가 다르고, '공급자가 새 값을 보내 저장 실패'가 원인인 공식 사후 보고를 찾지 못함(웹 검색 2회) |
| 6 | 계정이나 가게를 한 트랜잭션에서 연쇄 하드 삭제해 잠금을 쥐고, 재시도가 뒤에 줄 섬 | Inngest 2026-09-18 공식(ON DELETE CASCADE 그래프로 계정을 하드 삭제, 잠금이 PgBouncer 연결 고갈로) | food, banking 표 | 버림(원칙 1 재현 불가): food, banking 스키마에 ON DELETE CASCADE 가 없고, banking 에서 비슷하게 만들면 잠기는 것이 accounts 행이라 F01-P 와 같은 주입 |
| 7 | ledger 자원 요청을 과하게 잡아 노드 할당 가능량을 묶고 transfer 재배포의 새 파드가 Pending | Allegro 2018-07-18 공식(일부 서비스가 필요보다 훨씬 많은 자원을 예약해 여유 하드웨어가 있는데도 새 인스턴스가 뜨지 못함) | 스케줄링, 은행 원장 서비스(1) | 버림(원칙 9): 단일 레플리카 재배포는 옛 파드가 자기 요청을 먼저 돌려받으므로, 과다 예약 파드가 스케줄될 수 있는 크기면 transfer 새 파드도 늘 들어간다. 원본 고리(트래픽 급증 확장)는 HPA 가 없어 재현 불가 |
| 8 | ledger 소비자가 같은 이체 이벤트를 중복 처리해 공유 Oracle 포화 | Signhost 2025-09-02~03 공식 상태 페이지(배포가 같은 거래 이벤트의 중복 메시지 폭주를 일으켜 DB 포화) | 은행 원장 서비스(1), Oracle | 버림(원칙 1): 원본이 버그를 특정하지 못했다고 밝혀 결함 꼴을 정할 수 없음 |
| 9 | 새 인증 방식 릴리스가 설정 변수 전달 전에 배포되어 내부 API 호출이 401 | Heroku Incident #2105(2020, 공식 블로그) | commerce gateway | 버림(F34-R 교훈, 원칙 2): 서비스 사이 인증은 commerce 뿐이고 401 단일 신호 |
| 10 | 배포 중 전체 캐시 비우기로 Redis, DB 재적재 폭주와 스레드 고갈 | Shepherd 2026-02-26 공식 상태 페이지 | commerce cart Redis | 버림(원칙 2, 9): 캐시는 commerce cart 뿐이라 쇼핑몰 최다에 정답 위치 캐시 1, cart DB 풀이 폴백 부하를 흡수(rejected 의 Slack 2022 와 같은 벽) |
| 11 | 자격 증명 회전이 새 값을 다른 환경에 배포하고 옛 값을 지움 | Cloudflare 2025-03-21 공식(R2 자격 증명) | DB 계정(1) | 버림(같은 주입, 컨트롤러 필수 중단 조건): banking 은 F35-R 과 같은 재접속 실패, food 는 order readiness 가 DB 를 봐서 entry 0 |
| 12 | 워커 conntrack 표 가득으로 새 연결 SYN 버림 | 없음 | tb-w3 | 버림(원칙 1): 공식 사후 보고를 이번에도 찾지 못함(웹 검색 2회, 벤더 KB 와 블로그뿐). rejected 2026-10-09 와 같은 결론 |

## 4. 인과 사슬 (코드와 인프라 위치)

1. 이미지 적재: `core-banking/k8s/build-and-deploy.sh:44-54` 가 앱 이미지를 `docker save "core-banking-${svc}:latest" | ssh <node> "sudo ctr -n k8s.io images import -"` 로 노드에 직접 넣는다. 레지스트리를 거치지 않아 노드의 이미지에 repoDigests 가 없다. 2026-10-10 tb-w2 `crictl inspecti`: `core-banking-transfer:latest` repoTags ['docker.io/library/core-banking-transfer:latest'], repoDigests [], `nginx:alpine` 은 repoDigests ['docker.io/library/nginx@sha256:54f2...']. `ctr images ls -q` 에는 이름 넷과 `sha256:<이미지 ID>` 참조 넷이 따로 있다.
2. 이미지 ID: api f37dcf717dd0, account e7cf7c33e836, transfer b52ed7dbcd04, ledger 3a23005488be. 109 docker 의 같은 이름(`docker images`)과 같다. 실행기 preflight(109 에서 읽기 전용 실행, 통과)가 매번 같은지 확인한다.
3. 배포 정본: `core-banking/k8s/22-transfer-service.yaml:14-18` strategy maxUnavailable 1, maxSurge 0. `:27-28` nodeSelector tb-w2. `:36-37` image core-banking-transfer:latest, imagePullPolicy Never.
4. 컨테이너 생성: kubelet 은 Never 정책에 노드 저장소에 이미지가 없으면 컨테이너를 만들지 않고 `ErrImageNeverPull`(`Container image "core-banking-transfer:latest" is not present with pull policy of Never`)을 남긴다. 파드는 스케줄되어 있고 컨테이너가 없어 재시작 수 0, Ready 아님. 떠 있는 컨테이너는 자기 스냅숏을 쓰므로 이미지를 지워도 계속 돈다.
5. Deployment 와 ReplicaSet 컨트롤러: 재배포는 새 ReplicaSet 을 만들고 maxUnavailable 1 이라 옛 ReplicaSet 을 0 으로, 새 ReplicaSet 을 1 로 같은 순간에 바꾼다(KCM 2026-10-01 17:31:03 같은 꼴 재배포: ScalingReplicaSet 두 줄, SuccessfulDelete, Scheduled, Pulled 'Container image "core-banking-transfer:latest" already present on machine', Created, Started).
6. 엔드포인트가 빈 Service 로의 연결은 kube-proxy 가 거절한다. 2026-10-09 06:55~07:12 transfer 가 빠졌던 창의 로그(119 CH): core-banking-api 'Connection refused' 1,072, core-banking-account 44, commerce-order 166, commerce-payment 1건.
7. account: `account-service/.../client/TransferClient.java:28-58`(executeTransfer, fallback 'Transfer service circuit open/exhausted' 502). api: `api-service/.../client/AccountClient.java:28-58`(requestTransfer). commerce: `commerce/payment-service/.../client/BankingTransferClient.java:45-58`(transfer FQDN 동기 호출, 502).

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`target_id`) | `tb-w2` | 결함을 가진 곳은 워커 tb-w2 의 이미지 저장소(쓰고 있는 이미지가 지워진 상태)다. 이미지를 되돌려야 회복하고, 정리 규칙을 고쳐야 재발이 막힌다. target_kind node(F44-P, F44-R, F10-H 와 같은 노드 이름) |
| 계기(`trigger_target_id`) | `core-banking-transfer` | 결함을 드러낸 사건은 transfer 의 재배포다(파드 템플릿 그대로인 정당한 작업) |
| 부분 점수 | core-banking-transfer, testbed-transfer(계기, 파드가 뜨지 못한 서비스), core-banking-account, core-banking-api, commerce-payment(502 를 내는 증상 서비스) | |
| 입도 | node | |

- 원칙 6: 재배포는 정당한 요청이다. 같은 꼴의 재배포(rollout restart, 이미지 그대로)가 2026-10-01 에 'already present on machine' 으로 성공했고, 이미지를 되돌리면 다시 성공한다. 새 컨테이너를 막은 것은 노드 저장소에서 쓰고 있는 이미지를 지운 정리다. 원본 보고가 지목한 층위('자동 보존 작업이 운영 이미지를 잘못 지워 서비스가 이미지를 가져오지 못했다')와 같다.
- 원칙 5: 정답은 tb-w2 syslog 의 이미지 삭제 명령과 containerd 삭제 이벤트(전수), 재배포 ReplicaSet 의 이미지 이름이 그대로라는 점, 새 파드의 ErrImageNeverPull 이라는 전수 근거로 낸다. 정리 규칙이 왜 다이제스트로 골랐는지는 요구하지 않는다.
- F17-H 와의 차이: F17-H 는 롤아웃이 노드에 없던 태그를 가리켜 '그 롤아웃(transfer 릴리스)'이 정답이고, 여기서는 롤아웃이 늘 쓰던 태그를 가리켰는데 노드에서 그 이미지가 사라져 '노드'가 정답이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | core-banking-account, core-banking-api 서버 스팬 502, commerce-payment, commerce-order 오류, 각 서비스 ERROR 로그 급증 |
| 피해 판정 | 러너 | 동반 부하 이체(step transfer) 5xx 비율, 평시 0 에서 0.5 이상 |
| 원인 설명 | 녹화 데이터 | §7 의 tb-w2 syslog(sudo, containerd, kubelet), KCM 이벤트(ScalingReplicaSet, ErrImageNeverPull), ReplicaSet 스펙(PG), 로그(전수) |

- 겉 증상은 F17-H, F50-R 과 같다(transfer 엔드포인트가 빔). 이 모양의 감지는 F17-R 정식 녹화와 2026-10-02 transfer NotReady 창(api 'Connection refused' 와 서킷 로그 1,006건, account 560건, commerce-order 278건, core-banking-api 묶음 promote)에서 실제로 났다(F50-R 시트 §6).
- 평시 오류율: core-banking-account, core-banking-api 오류율 7일 99백분위 0(F39-R 시트 §7). 업무 거절은 400 이라 오류율에 안 들어간다.
- 표본량: 동반 부하 5rps 라 api 는 초당 약 2.9건이 오류, 10% 표본으로 분당 약 17개 ERROR 서버 스팬(F17-H 시트 §6).
- 묶음 구성 위험: commerce 쪽 오류가 같은 창에 함께 나서 묶음이 commerce 로 잡힐 수 있다(F50-R 시트 §6 과 같음).

## 7. 관측 근거 표 (119 실조회, 2026-10-10 04:30~05:30 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 syslog(전수), KCM 이벤트(전수), 로그(전수)다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 이미지 삭제 명령 | CH `syslog_local` (hostname, app_name, message, received_at) | `hostname='tb-w2' AND app_name='sudo'`, `message LIKE '%COMMAND=%ctr%'` 30일 | tb-w2 sudo 294건(30일), ctr 명령은 'images ls' 와 'ls -q'(다른 시나리오 preflight 와 설계 조회), 그리고 배포의 'images import' 4건(2026-09-17 1, 2026-10-01 3. tb-w1 6, tb-w3 7, tb-cp 1). 'images rm' 은 30일 안 전 노드 0건이라, 고장 때의 삭제 명령은 평시에 없는 꼴이다. 설계 조회 자신이 05:04:19 'COMMAND=/usr/bin/ctr -n k8s.io images ls' 로 몇 초 안에 잡혀 수집 경로를 확인했다 |
| 근본: containerd 삭제 이벤트 | CH `syslog_local` (app_name='containerd') | `message LIKE '%ImageDelete event%' OR message LIKE '%RemoveImage%'` 60일, 노드별 | tb-w2 2026-08-24 62줄(예 'ImageDelete event name:\"sha256:...\"', 'ImageDelete event name:\"docker.io/grafana/k6@sha256:...\"'), tb-w1 2026-09-13 94줄, tb-w3 2026-10-08 20줄. 이름 붙은 이미지를 지우면 'ImageDelete event name:\"docker.io/library/...\"' 꼴로 이름이 남는다(같은 노드 import 때 'ImageUpdate event name:\"docker.io/library/food-delivery-order:latest\"' 꼴로 이름이 남는 것 확인, tb-w3 2026-10-08) |
| 계기: 재배포 | CH `kcm_events_local` (namespace, object_kind, object_name, reason, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-transfer%'` 2026-10-01 17:31:00~10 | ScalingReplicaSet 'Scaled down replica set testbed-transfer-64dd67bbcc to 0 from 1' 와 'Scaled up replica set testbed-transfer-74b498b67b to 1 from 0'(같은 초), SuccessfulDelete, SuccessfulCreate, Scheduled 'to tb-w2', Pulled, Created, Started. 고장에서는 Pulled, Created, Started 자리에 ErrImageNeverPull 이 나와야 한다 |
| 계기: 재배포 스펙 | PG `kcm_resources_history` (kind='replicaset', name, yaml) | F50-R 시트 §7 | ReplicaSet 74b498b67b 의 yaml 에 restartedAt 주석과 `"image":"core-banking-transfer:latest"`. 재배포 ReplicaSet 은 이 주석만 옛 것과 다르다 |
| 전파: 이미지 부재 | CH `kcm_events_local` | `reason ILIKE '%NeverPull%' OR body ILIKE '%pull policy of Never%'` 보존 전체 | 0건(선례 없음, F17-H 와 같이 수집 여부는 첫 실행에서 확인). transfer 파드 이벤트 사유: Unhealthy 1,483, ScalingReplicaSet 128, Pulled 64(64건 모두 'Container image "core-banking-transfer:latest" already present on machine'), Created, Scheduled, SuccessfulDelete, Started, SuccessfulCreate 각 64, Killing 34 |
| 전파: kubelet 오류 | CH `syslog_local` (app_name='kubelet') | `message LIKE '%Error syncing pod%'` 60일 | tb-w3 17, tb-w1 83건(예 'Error syncing pod, skipping ... CrashLoopBackOff'), tb-w2 0건(그 노드에서 기동 실패가 없었음). tb-w2 kubelet 로그는 30일 3,388줄 수집. ErrImageNeverPull 도 같은 'Error syncing pod' 로 남을 것으로 보고 녹화본에서 확인 |
| 전파: 연결 거절 로그 | CH `lucida_logs_local` body | `body LIKE '%testbed-transfer%Connection refused%'`, 최근 8일 | core-banking-api 1,072, core-banking-account 44, commerce-order 166, commerce-payment 1건(2026-10-09 06:55~07:12 다른 시나리오 실행 창), 그 밖 0건 |
| 대조: 노드 상태 | CH `kcm_events_local` | tb-w2 의 NodeNotReady, Evicted, FreeDiskSpaceFailed | 고장 중 없어야 한다(정리는 노드를 건드리지 않음) |
| 골든 시그널(감지) | VM `apm.agent.otel.java.error_rate`, CH MV `agg_service_golden_signals` | F39-R 시트 §7(2026-10-08) | account, api 오류율 99백분위 0 |
| 피해 | 동반 부하 k6 live 문서 | 이체 step 5xx 비율 | 평시 0(업무 거절은 400) |

## 8. 감별

- must_support: 정답지 5항목(tb-w2 syslog 의 sudo ctr images rm 넷과 containerd ImageDelete, 그 직후 재배포 이벤트와 restartedAt 만 다른 새 ReplicaSet, 새 파드의 ErrImageNeverPull 과 Pulled/Created/Started 부재, account, api 의 Connection refused 와 502 와 commerce checkout 502, 동반 부하 이체 5xx 와 잔액 조회 정상, 다른 파드와 노드 Ready).
- must_rule_out(정답지): transfer 릴리스나 잘못된 이미지 태그(F17-H, F42-R, F40-H), 할당량 승인 거절(F50-R), 용량 명령 실수(F51-R), readiness 오설정(F17-R), 노드 장애나 자원 부족, transfer DB 나 Oracle 장애(F01-P, F35-R), 재배포 자체, 부하 증가.
- 같은 증상과 가르는 관측 근거:

| 시나리오 | 새 파드 | 바뀐 파드 템플릿 | 지문 | 노드 쪽 흔적 |
|---|---|---|---|---|
| F52-R | 스케줄됨, 컨테이너 없음 | 없음(restartedAt 주석만, 이미지 :latest 그대로) | Pod ErrImageNeverPull 'core-banking-transfer:latest' | 직전 tb-w2 sudo ctr images rm 넷, containerd ImageDelete |
| F17-H | 스케줄됨, 컨테이너 없음 | image 태그 2.1.0 | Pod ErrImageNeverPull 'core-banking-transfer:2.1.0' | 없음 |
| F50-R | 없음(생성 거절) | 없음 | ReplicaSet FailedCreate 'exceeded quota' | 없음 |
| F51-R(account) | 없음(replicas 0) | 없음 | ScalingReplicaSet 'to 0' 만 | 없음 |
| F17-R | 떠 있음 | readinessProbe 경로 | Pod Unhealthy(readiness 404) | 없음 |
| 노드 장애 | Pending 이나 축출 | 없음 | NodeNotReady, Evicted, FailedScheduling | kubelet, 커널 오류 |

- 러너 판정은 F17-H, F50-R 과 같아 셋을 구별하지 않는다(배제 조건은 크래시 루프와 account 경로만 가른다). 구별은 정답지 mechanism 채점과 must_support 에 있다.
- F17-H 와 이벤트 사유가 같다는 점이 이 시나리오의 핵심이다. 관제 AI 는 이벤트 본문의 태그(:latest, 그 전 64번 'already present' 였던 태그)와 노드 syslog 를 함께 봐야 정답(노드)에 닿는다.
- 부하 증가 경쟁 가설: 실패는 거절된 연결이고, 같은 부하의 35% 인 잔액 조회는 정상이며, 실패는 재배포 시각에 맞춰 0 에서 거의 1 로 뛴다.
- contrast_with: F17-H, F50-R, F51-R, F17-R, F44-P.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f52-r`). 강도라 할 값이 없다: 이미지가 노드에 있거나 없거나다. 지우는 범위는 정리 규칙이 고르는 banking 앱 이미지 넷 전부다(이 노드에서 다이제스트가 없는 banking 앱 이미지는 이 넷뿐, 2026-10-10). transfer 만 지우면 '나이 조건 없이 다이제스트로 고른 정리'라는 원본 꼴과 맞지 않는다.

- 시간: 이미지 삭제(이미지마다 ssh 한 번, 몇 초) → 남은 것 없음 확인 → rollout restart → 옛 파드 Terminating(엔드포인트에서 즉시 빠짐) → 새 파드 ErrImageNeverPull. 고장은 재배포 직후 시작한다. settle 60s, min_hold 15m 이라 판정(성공 3틱)은 재배포 약 16분 뒤.
- cleanup: run 은 지우기 전에 네 이미지의 ID 를 상태 파일에 적는다(`<이름> <sha256 ID>` 한 줄씩). cleanup 은 노드에 없는 이미지마다 109 docker 의 같은 이름이 적어 둔 ID 와 같은지 먼저 보고, 같을 때만 `docker save | ssh tb-w2 sudo ctr -n k8s.io images import -` 로 넣는다. 109 의 :latest 는 평소 배포가 다시 빌드하므로(transfer, ledger :latest 2026-10-01 빌드, tb-w2 sudo images import 2026-10-01 3건) 그 사이 다시 빌드되었으면 아무것도 넣지 않고 'not loading a different build' 로 실패해 사람이 지운 빌드를 되돌리게 남긴다. 넣은 뒤 넷의 노드 ID 가 적어 둔 ID 와 같은지 확인하고, kubelet 이 기다리던 새 파드를 다시 시도하도록 60초 두고, Ready 가 아니면 다시 배포해 180초 안에 available 을 확인한다. 상태 파일이 없으면(러너 컨테이너가 다시 만들어져 상태를 잃은 경우) 노드에서 그 이미지로 아직 도는 컨테이너(api, account, ledger)의 imageRef(`crictl ps`)를 기대 ID 로 쓰고, 도는 컨테이너가 없는 이미지(재배포로 옛 파드가 내려간 transfer)는 넣지 않고 실패한다. recovery 는 이름만이 아니라 tb-w2 에서 도는 banking 앱 컨테이너 넷의 imageRef 가 그 이름이 가리키는 노드 이미지 ID 와 같은지 본다(다른 빌드가 들어왔으면 계속 돌던 api, account, ledger 와 어긋난다). 이 세 경로는 실행기 단위 테스트(가짜 ssh, docker, kubectl 로 실제 스크립트를 돌림)로 고정했다.
- 피해 계산: 이체 단계 5xx 비율은 계좌 검증 400(이체의 약 4%)을 빼면 1.0 이고, api 서킷이 열리면 그 400 도 502 가 된다(F39-R 시트 §9). 성공 문턱 0.5 를 크게 넘는다. F17-H, F50-R 과 같은 사슬이다.
- 서킷브레이커: account, api 서킷은 열린 뒤 바로 502, commerce-payment 의 banking 호출은 서킷이 없어 요청마다 즉시 거절. 피해를 숨기지 않는다. 연결 거절은 즉시라 스레드 대기가 쌓이지 않는다.
- 진입점: nginx(nginx:alpine 은 다이제스트가 있어 정리 대상이 아님), api 는 그대로 떠서 502 로 답한다. abort 조건 entry_status==0 은 이 고장으로 나오지 않는다.
- 같은 노드의 다른 앱: api, account, ledger 의 이미지도 지워져, 고장 중 그 컨테이너가 다시 떠야 하면(liveness 실패, 축출) 그것도 뜨지 못한다. 평시 그 셋의 재시작은 0(2026-10-10 kubectl, account 8시간, api 14시간, ledger 9일 재시작 0)이고, account 가 빠지면 잔액 조회 실패로 must_rule_out(account-path-failing)이 실행을 무효로 돌린다. cleanup 이 넷을 함께 되돌린다.
- 노드 디스크: 지우면 공간이 늘고, 되돌리는 이미지는 지운 것과 같은 크기라 kubelet 이미지 GC 문턱(85%)과 무관하다(tb-w2 / 74%, 2026-10-10).

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f52-r`, min_hold 15m, settle 60s, timeout 20m |
| preflight(실행기) | testbed-transfer replicas 1, maxSurge 0, imagePullPolicy Never, nodeSelector tb-w2, 네 이미지 가운데 하나로 돔, available. 네 이미지가 tb-w2 containerd 에 있고 repoDigests 가 비었으며 109 docker 와 이미지 ID 가 같음. deployments 패치 권한. 109 에서 읽기 전용 실행(2026-10-10): 통과 |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking transfer-heavy-surge.js(이체 40, 잔액 35, 거래 내역 15, 계좌 목록 10), target_rps 5, entry 30082, ramp 2m + hold 21m + ramp_down 15s, seed 5252 |
| success | 동반 부하 이체 5xx 비율(`loadgen.food_create_status_rate`, 선택자 business.5xx.rate) ≥ 0.5, 3틱 연속 |
| must_rule_out | achieved_rps < 1.25(부하 끊김), 잔액 조회 실패율 ≥ 0.2(account 나 Oracle 장애), transfer 재시작 수 ≥ 2(컨테이너가 떠서 죽는 다른 장애), 2틱 |
| abort | entry_status(domain core-banking, nginx → api) == 0, 2틱 |
| recovery | transfer 파드 Ready, transfer 가용 레플리카 1, 기준선 이체 5xx 비율 < 0.05, target_health 200(형식 조건), 2틱, 10m. 실행기 recovery 는 넷의 노드 이미지 ID 와 도는 컨테이너 imageRef 가 같은지까지 본다 |
| cleanup | 노드에 없는 이미지를 109 docker 가 적어 둔 ID 를 그대로 가질 때만 되돌려 넣음(다르면 넣지 않고 실패), 60초 안에 transfer 가 안 뜨면 rollout restart, Deployment available 확인(180초), 동반 부하 종료, 10m |

새 관측 쿼리와 러너 변경은 없다. 새 실행기 `host.image`(`scripts/scenarios/profiles/host_image_executor.py`)를 더했다. 러너에서 kubectl 로 Deployment 를 확인하고, ssh nkia@192.168.122.11 로 이미지마다 `sudo -n ctr -n k8s.io images rm <이름> <sha256:ID>` 한 번을 낸 뒤 `kubectl rollout restart` 한 번을 낸다. 파드 템플릿, Service, 다른 노드, 다이제스트 있는 이미지(nginx, Kafka, Oracle)는 건드리지 않고 파드를 지우지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Logto 2023-12-17, 공식 블로그 사후 보고)와 요소별 대응표가 있고, 기전('보존 정리의 선택 규칙이 운영 이미지가 저장된 모양과 맞아 쓰고 있는 이미지를 지우고, 이미지를 가져와야 하는 순간 서비스가 뜨지 못함')이 원본과 같다. 바꾼 것(레지스트리 대신 노드 저장소, 계기를 일상 재배포로)과 이유를 §2 에 적었다 (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 P+B(P 3→4, 6.3%), 정답 위치 노드, 디스크(5→6, 9.5%), 결제 경로 10(15.9%), 은행 19→20. 어느 축도 20% 미만 (원칙 2, §3)
- [x] 근본 원인 위치가 인프라 지점으로 확인됐다: tb-w2 containerd 의 이미지 넷(이름, sha256 참조, repoDigests 비어 있음, 109 docker 와 같은 ID), build-and-deploy.sh:44-54 의 직접 적재, 22-transfer-service.yaml 의 전략, nodeSelector, Never 정책 (G1, §4)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: syslog_local 의 tb-w2 sudo 명령(설계 조회의 ctr images ls 가 몇 초 안에 잡힘), 같은 노드 containerd ImageDelete 이벤트 선례(2026-08-24), KCM 의 재배포 이벤트와 'already present' 이벤트 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: syslog, KCM 이벤트, 로그는 전수 (원칙 3)
- [x] 계기의 흔적이 조회됐다: KCM ScalingReplicaSet 두 줄과 restartedAt 주석만 다른 ReplicaSet 스펙(2026-10-01 같은 꼴 재배포 실측). 인공 지연을 쓰지 않는다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 노드의 이미지 삭제 명령과 삭제 이벤트, 그 직후 같은 태그의 ErrImageNeverPull. 정리 규칙의 설계 결함 추론을 요구하지 않는다 (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다: 근본 tb-w2(노드 이미지 저장소), 계기 core-banking-transfer(재배포), 부분 점수 계기와 증상 서비스 (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 실행기 인자와 스크립트에 id 가 없고, 상태 파일은 노드와 네임스페이스 이름, sudo 명령줄은 이미지 이름과 ID 뿐이다(단위 테스트로 확인) (원칙 8)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 이체 5xx 약 1.0, 연결 즉시 거절, 동반 부하 5rps (원칙 9, §9)

## 11. 첫 실행에서 확인할 것

- KCM 이 새 파드의 `ErrImageNeverPull` 경고 이벤트를 실제로 잡는지(F17-H 와 같은 미확인 항목). 안 잡히면 tb-w2 kubelet syslog 'Error syncing pod ... ErrImageNeverPull' 과 새 ReplicaSet 파드의 Pulled/Created/Started 부재로 좁혀지므로 정답지 must_support 3 을 녹화본에 맞게 고친다.
- containerd 'ImageDelete event' 가 이름(docker.io/library/core-banking-...:latest)으로 남는지. sha256 참조로만 남으면 must_support 1 의 이벤트 꼴을 녹화본에 맞게 고친다(sudo 명령줄에는 이름이 남는다).
- `ctr images rm` 이 실행 중인 컨테이너가 쓰는 이미지를 지우고, 그 컨테이너가 계속 도는지. 지우지 못하면 run 이 재배포 전에 실패로 끝나(남은 것 없음 확인) 피해 없이 멈춘다.
- 운영 하네스의 앱 이미지 점검(image-drift-guard.sh)은 Deployment 스펙의 이미지 이름만 봐서, cleanup 이 실패해 노드에 이미지가 없어도 다음 녹화를 막지 못한다. cleanup 실패는 실행 결과로 드러나므로 그때는 사람이 지운 빌드를 노드에 되돌려야 한다(사람이 할 일).
