---
title: 시나리오 유사 장애 분류 장부
status: Active
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - harness
  - balance
summary: 정식 시나리오(정상 녹화 1개 이상)를 비슷한 장애끼리 묶어 둔 장부와, 막힌 시도 참고 목록. 시나리오 작성 하네스는 매번 처음부터 묶지 않고, 이 장부를 읽은 뒤 새로 생기거나 상태가 바뀐 시나리오만 분류해 갱신한다.
---

# 시나리오 유사 장애 분류 장부

[설계 원칙](spec-scenario-design-principles.md) 원칙 2(숫자로 정하고, 비슷한 장애가 쌓이지 않게 하고,
없던 장애를 우선한다)를 위한 장부다. 정답지(`scenario-metadata.json`)와 별개 파일이며, 정답지 구조는 바꾸지 않는다.

## 1. 하네스 사용법

1. 이 장부를 읽는다.
2. `scripts/scenarios/catalog.json`과 대조한다. **장부에 없는 id, readiness나 stage가 바뀐 id만** 골라낸다.
   분류와 집계는 **정식(`stage: official`)만** 한다. 정식은 eval-cases 에 정상 녹화가 1개 이상 있는 시나리오다([수명주기](spec-scenario-lifecycle.md)).
   후보(`readiness: ready`, `stage: candidate`)와 draft 는 §4-1 후보 표에 넣는다. 집계 비율에는 넣지 않지만,
   후보를 고를 때는 같은 묶음에 후보가 있는지 함께 본다(비슷한 것을 연달아 만들지 않기 위해).
   parked 는 §5 막힌 시도 표로 옮긴다. cut은 기록하지 않는다.
3. 골라낸 시나리오의 설명(`scenario-metadata.json`의 title, cause, description, 없으면 catalog의 slug와 prerequisite)을 읽고
   §2 묶음 정의 중 맞는 것에 넣고, `root_cause.target_id`와 설명으로 §2-1 정답 위치도 정한다. 맞는 묶음이 없으면 **새 묶음을 만들고** 정의를 한 줄로 §2에 더한다.
4. 두 원인이 겹친 시나리오는 `D+C`처럼 적고, 집계는 앞에 쓴 주 묶음으로 한다.
5. §3 집계와 §4 표를 갱신하고, §6 갱신 기록에 한 줄 남긴다.
6. 새 후보를 고를 때는 **§4 정식과 §4-1 후보를 합쳐** 센다(§3 집계는 정식만이지만, 후보가 쌓이는 동안 쏠림을 막기 위해서다). 합계 0개 묶음이 최우선, 그다음 현실 대비 적은 묶음. 합계의 20% 이상인 묶음에는 추가하지 않는다.
   서비스는 합계가 가장 적은 곳부터 찾되, 가장 많은 서비스도 정답 위치가 0인 부품이면 낸다(원칙 2, 2026-10-08 완화). **정답 위치(§2-1)도 같은 합계로 세어, 한 위치나 결제 경로 합계가 20% 이상이면 그 위치가 정답인 후보는 만들지 않는다.** `scenario-stats.py`(시나리오 생성 하네스)가 세 축을 함께 낸다.

묶음이 너무 굵거나 잘게 나뉘었다고 판단되면 다시 나눌 수 있다. 그때는 §6에 이유를 적는다.

## 2. 묶음 정의

| 묶음 | 무엇이 들어가나 | 현실 근거 (`ref-real-world-incidents.md`) |
|---|---|---|
| A 자원 부족, 한도 | 컨테이너나 노드의 CPU, 메모리, 디스크 IO, 디스크 용량이 모자라거나 한도에 걸림 | 용량 5~12% (Google, Ghosh) |
| B 부품 멈춤 | 시스템 안의 구성요소(컨슈머, 발행기, 캐시, 배치, DB 프로세스, 파드 준비 상태)가 멈추거나 죽어서 그걸 쓰는 기능이 멈춤. Redis 다운, Kafka 브로커 다운도 여기 | 의존 8~12% (Gunawi, Ghosh) |
| C 상대 서비스 오류, 지연 | 호출하는 외부 또는 내부 서비스가 오류(429 등)를 내거나 응답이 느려짐 | 의존 8~12%, M14 |
| D DB 잠금 | 행이나 테이블 잠금, 차단 세션으로 대기 | 상위 원인 아님 |
| E 처리 용량 초과 | 들어오는 요청이 스레드풀, 커넥션풀, bulkhead, 소비 속도 같은 처리 능력을 넘음 | 부하 9% (Gunawi), M4 |
| F 느린 쿼리 | 인덱스 부재나 비효율 쿼리로 DB 응답이 느려짐 | |
| G 설정 오배포 | 잘못된 설정값을 배포해서 동작이 깨짐 | **설정 배포 31%** (Google), M1 |
| H 데이터가 조용히 틀어짐 | 오류 없이 처리됐는데 데이터가 틀림(검증 우회, 쓰기 실패 삼킴) | 조용한 손상 17% (Liu, 버그 중) |
| I 인증 의존 실패 | 인증을 맡은 서비스 실패가 다른 기능의 실패로 번짐 | 인증 4% (Ghosh) |
| J 결함 있는 새 버전 배포 | 코드 결함이 있는 버전을 배포해서 깨짐 | **바이너리 배포 37%** (Google), M2 |
| K 네트워크 지연, 손실, 단절 | 서비스 사이 네트워크가 느려지거나 패킷이 빠지거나 끊김 | **네트워크 15%** (Gunawi), M15 |
| L 데이터 형식 불일치 | 보내는 쪽과 받는 쪽이 기대하는 데이터 형식이 안 맞음 | **버그의 21%** (Liu), M8 |
| M DNS | 이름 해석 실패나 지연 | M16 |
| N 재시도가 장애를 키움 | 재시도가 부하나 중복을 늘려 장애를 키우거나 유지시킴 | 메타스테이블 유지의 50% 이상 (Huang), M3 |
| O 자격 증명 만료, 회전 실수 | 접속에 쓰는 자격 증명(DB 계정, 비밀번호, 키, 인증서)이 만료되거나 회전, 비활성화되어 그것을 쓰던 쪽이 인증에 실패함. 인증을 맡은 서비스가 고장 나는 I 와 달리 인증 주체는 멀쩡하고 자격 증명 쪽이 바뀐다 | 배포 오류의 55%가 인증서 만료나 회전 실수, 전체의 약 11% (Ghosh), M7 |
| P 운영 작업의 대상 착오 | 운영자나 운영 도구(백필, 정리 스크립트, 일괄 명령)가 의도한 대상이 아닌 살아 있는 대상(표, 서버, 자원)을 지우거나 바꿔 그것을 쓰는 기능이 깨짐. 설정값을 잘못 넣은 G, 코드 결함인 J, 계획된 스키마 변경이 코드와 어긋난 L 과 달리, 의도한 변경 내용은 맞았는데 적용 대상이 틀렸다 | 운영 명령, 스크립트 실수 M22(Atlassian 2022-04-05 잘못된 id 로 사이트 삭제, AWS S3 2017-02-28 명령 오타로 서버 과다 제거), M21 GitHub 2026-07-24(백필 취소가 기반 표를 지움) |

- J 보충(2026-10-09, F17-H): J 는 코드 결함 외에 **실행할 수 없는 릴리스 산출물**(배포가 가리킨 이미지가 이미지 저장소에 없음)을 내보낸 새 버전 롤아웃도 포함한다. 현실 근거인 Google '바이너리 배포' 트리거가 산출물 결함을 함께 센다. 위 표의 정의 문장은 기존 항목이라 고치지 않고 이 줄로 넓힌다.

## 2-1. 정답 위치 정의

정답지 `root_cause.target_id`가 가리키는 **고장 난 부품이 무슨 역할인가**를 정해진 말로 적는다. 정답지 값은 시나리오마다 표기가 달라(`food-payment`, `testbed-payment:payment-service`, `None` 등) 기계로 세지 않고, 묶음처럼 이 장부에 한 번 분류해 둔다(정답지는 고치지 않는다).
관제 AI 가 "애매하면 늘 범인이던 곳을 찍는" 지름길을 배우지 않도록, 한 위치에 정답이 몰리지 않게 센다(원칙 2).

| 정답 위치 | 뜻 |
|---|---|
| 외부 결제 의존 | 외부 결제대행사(PG mock) |
| 결제 서비스 | 각 도메인의 payment 서비스 자체(코드, 설정, 컨테이너) |
| 주문 서비스 | order 서비스 자체 |
| 재고 서비스, 배송 서비스, 상품 서비스, 사용자 서비스, 알림 서비스, 가게 서비스, 배달 서비스, 게이트웨이, 가격 서비스 | 그 서비스 자체(가격 서비스는 commerce pricing: 견적과 그것이 읽는 프로모션, 쿠폰 같은 가격 설정) |
| 은행 이체 서비스, 은행 원장 서비스, 은행 계좌 서비스, 은행 API | 은행 도메인의 그 서비스 자체 |
| DB 테이블(<무엇>) | 특정 테이블(잠금, 쓰기 실패 등). 괄호에 업무 이름(결제, 재고, 은행 계좌 등) |
| DB 인스턴스 | DB 서버 하나 전체(OOM, 연결 한도 등) |
| 메시지 브로커 | Kafka 등 |
| 캐시 | Redis 등 |
| 노드, 디스크 | 쿠버네티스 노드, 디스크, 네트워크 등 인프라 |
| DNS, 인증 의존 | 이름 해석, 인증 서버 |
| DB 계정 | DB 서버의 특정 접속 계정(잠김, 비밀번호 불일치, 권한 회수). 인스턴스 전체가 아니라 한 계정으로 들어오는 접속만 막힌다 |
| 장바구니 서비스 | commerce cart 서비스 자체(코드, 설정, 컨테이너). 장바구니 담기, 조회, 비우기와 checkout 이 읽는 장바구니 |
| 쿠버네티스 리소스 할당량 | 네임스페이스의 ResourceQuota 같은 클러스터 승인 한도 객체. 떠 있는 파드는 그대로 두고 새 파드(롤아웃, 재배포)만 거절한다. 노드나 네트워크(노드, 디스크)와 달리 노드는 멀쩡하고 API 서버의 승인 단계가 막는다 |

맞는 말이 없으면 이 표에 한 줄 더한다. **결제 경로**(외부 결제 의존 + 결제 서비스 + DB 테이블(결제))는 따로 합쳐서도 센다: 서로 다른 부품이라도 "결제 쪽이 범인"이라는 같은 지름길을 만든다.

## 3. 집계 (2026-10-09, 정식만, 주 묶음 기준)

| 묶음 | 정식 | 정식 비율 |
|---|---|---|
| A 자원 부족, 한도 | 7 | 16% |
| B 부품 멈춤 | 7 | 16% |
| C 상대 서비스 오류, 지연 | 6 | 14% |
| D DB 잠금 | 7 | 16% |
| E 처리 용량 초과 | 2 | 5% |
| F 느린 쿼리 | 2 | 5% |
| G 설정 오배포 | 5 | 12% |
| H 데이터가 조용히 틀어짐 | 2 | 5% |
| I 인증 의존 실패 | 1 | 2% |
| J 결함 있는 새 버전 배포 | 0 | 0% |
| K 네트워크 지연, 손실, 단절 | 0 | 0% |
| L 데이터 형식 불일치 | 2 | 5% |
| M DNS | 1 | 2% |
| N 재시도가 장애를 키움 | 0 | 0% |
| O 자격 증명 만료, 회전 실수 | 1 | 2% |

서비스별 정식 (문제가 시작된 서비스): 쇼핑몰 23, 은행 11, 음식배달 9. 후보 14종은 집계에서 뺀다(§4-1).

해석: 현실에서 가장 흔한 변경(J 새 버전 배포, G 설정 배포)이 정식 5개뿐이고(계기가 설정 배포인 F30-R 은 주 묶음 L, F37-R 은 주 묶음 M 으로 센다) J, K, N은 0이다(K, N은 §5에 막힌 시도가 있다).
A, B, D는 각 7개(16%)로 20%에 가깝다. 서비스는 음식배달, 은행 순으로 채운다.

정답 위치별 정식 (2-1 정의, 정식 43종):

| 정답 위치 | 정식 | 비율 |
|---|---|---|
| 외부 결제 의존 | 6 | 14% |
| 주문 서비스 | 6 | 14% |
| DB 테이블(은행 계좌) | 3 | 7% |
| DB 테이블(재고) | 3 | 7% |
| 결제 서비스 | 3 | 7% |
| 노드, 디스크 | 3 | 7% |
| 은행 이체 서비스 | 3 | 7% |
| DB 인스턴스 | 2 | 5% |
| 재고 서비스 | 2 | 5% |
| DB 계정 | 1 | 2% |
| DB 테이블(가게) | 1 | 2% |
| DB 테이블(결제) | 1 | 2% |
| DB 테이블(배차) | 1 | 2% |
| 배달 서비스 | 1 | 2% |
| 배송 서비스 | 1 | 2% |
| 사용자 서비스 | 1 | 2% |
| 상품 서비스 | 1 | 2% |
| 은행 API | 1 | 2% |
| 은행 계좌 서비스 | 1 | 2% |
| 은행 원장 서비스 | 1 | 2% |
| 캐시 | 1 | 2% |
| **결제 경로 합계** | **10** | **23%** |

결제 경로는 정식만으로도 23%라 상한(20%)을 넘는다. 결제 쪽이 정답인 후보는 당분간 만들지 않는다.

## 4. 시나리오별 분류

**정식** 시나리오만 넣는다: `catalog.json`의 `stage: official`, 곧 eval-cases 에 정상 녹화가 1개 이상 있는 것([수명주기](spec-scenario-lifecycle.md)). 집계(§3)는 이 표만 센다.

| id | 상태 | 서비스 | 묶음 | 정답 위치 | 한 줄 |
|---|---|---|---|---|---|
| F05-P | 정식 | 쇼핑몰 | A | 노드, 디스크 | 노드 메모리 고갈 eviction |
| F05-R | 정식 | 쇼핑몰 | A | 결제 서비스 | payment 메모리 한도 OOM |
| F09-H | 정식 | 쇼핑몰 | A | 주문 서비스 | GC 압박 |
| F09-P | 정식 | 쇼핑몰 | A | 재고 서비스 | CPU limit 과소 |
| F10-H | 정식 | 음식배달 | A | 노드, 디스크 | MySQL 디스크 IO 포화 |
| F12-H | 정식 | 쇼핑몰 | A | 상품 서비스 | product CPU 한도 |
| F15-P | 정식 | 은행 | A | 노드, 디스크 | 노드 복합 자원 고갈 |
| F04-H | 정식 | 쇼핑몰 | B | 주문 서비스 | order 발행기 정지 |
| F04-R | 정식 | 쇼핑몰 | B | 배송 서비스 | shipping 컨슈머 중단 |
| F11-R | 정식 | 쇼핑몰 | B | 캐시 | Redis 중단 후 DB 폴백 폭주 |
| F17-R | 정식 | 은행 | B | 은행 이체 서비스 | transfer readiness 실패로 결제 롤백 |
| F18-P | 정식 | 은행 | B | 은행 이체 서비스 | 은행 발행기 정지 |
| F23-R | 정식 | 쇼핑몰 | B | 재고 서비스 | 재입고 배치 정지로 409 |
| F25-H | 정식 | 쇼핑몰 | B | DB 인스턴스 | PostgreSQL OOMKill |
| F01-H | 정식 | 쇼핑몰 | C | 외부 결제 의존 | 외부 결제 429가 502로 |
| F06-P | 정식 | 음식배달 | C | 외부 결제 의존 | 음식배달 외부 결제 429 |
| F06-R | 정식 | 쇼핑몰 | C | 외부 결제 의존 | 외부 결제 30초 hang |
| F08-H | 정식 | 쇼핑몰 | C | 외부 결제 의존 | 무죄 배포 직후 외부 결제 429 |
| F19-P | 정식 | 음식배달 | C | 외부 결제 의존 | 외부 결제 지연으로 주문 풀 고갈 |
| F19-S | 정식 | 음식배달 | C | 외부 결제 의존 | 외부 결제 지연으로 502 연쇄 |
| F01-P | 정식 | 은행 | D | DB 테이블(은행 계좌) | Oracle 정산계좌 잠금 |
| F01-R | 정식 | 쇼핑몰 | D | DB 테이블(재고) | inventory 행 잠금 |
| F06-H | 정식 | 쇼핑몰 | D | DB 테이블(결제) | payments 테이블 잠금 |
| F08-G | 정식 | 은행 | D | DB 테이블(은행 계좌) | Oracle 잠금 + 무죄 배포 |
| F15-G | 정식 | 은행 | D | DB 테이블(은행 계좌) | Oracle 잠금(쇼핑몰 증상) |
| F15-T1 | 정식 | 쇼핑몰 | D+A | DB 테이블(재고) | 재고 잠금과 음식배달 OOMKill 동시 |
| F15-H | 정식 | 쇼핑몰 | D+C | DB 테이블(재고) | 재고 잠금과 음식배달 429 동시 |
| F03-H | 정식 | 쇼핑몰 | E | 주문 서비스 | order 스레드풀 고갈(직렬 렌더러) |
| F07-H | 정식 | 쇼핑몰 | E | 주문 서비스 | 진입 트래픽 폭주 |
| F20-R | 정식 | 쇼핑몰 | F | 주문 서비스 | 통계 풀스캔 교차 지연 |
| F05-H | 정식 | 쇼핑몰 | G | 결제 서비스 | liveness 오설정 재시작 루프 |
| F08-P | 정식 | 쇼핑몰 | G | 주문 서비스 | read-timeout 20ms 오배포 |
| F14-P | 정식 | 은행 | H | 은행 원장 서비스 | 원장 쓰기 실패 삼킴 |
| F17-P | 정식 | 은행 | H | 은행 이체 서비스 | FROZEN 계좌 우회 이체 |
| F16-H | 정식 | 쇼핑몰 | I | 사용자 서비스 | user-service 실패로 쓰기 401 |
| F30-R | 정식 | 음식배달 | L+G | 결제 서비스 | payment JSON 명명 규칙 설정 배포로 order 결제 요청의 orderId 유실 |
| F32-R | 정식 | 음식배달 | G | 배달 서비스 | dispatch 배차 동시 한도 설정이 실제 배차 수 아래(2000→200)로 배포되어 주문 전량 503 |
| F33-R | 정식 | 음식배달 | F | DB 테이블(배차) | dispatches 인덱스 제거(DDL)로 주문마다 도는 배차 COUNT 가 500만 행 전수 스캔, MySQL 포화와 dispatch 풀 고갈로 주문 503 |
| F35-R | 정식 | 은행 | O | DB 계정 | 자격 증명 회전 단계가 Oracle 애플리케이션 계정 BANKING 을 잠가 풀 세션이 수명을 다하며 재접속 실패(ORA-28000), account, transfer, ledger NotReady 로 banking 과 commerce 정산 502 |
| F36-R | 정식 | 음식배달 | L | DB 테이블(가게) | restaurants.region 열 이름을 바꾸는 스키마 마이그레이션이 코드보다 먼저 적용되어 restaurant-service 의 가게 조회와 검색이 MySQL 1054 Unknown column 으로 실패, order 주문 전량 502 |
| F37-R | 정식 | 은행 | M+G | 은행 API | api-service 배포가 파드 dnsPolicy 를 노드 resolver(Default)로 바꿔 클러스터 Service 이름(testbed-account, testbed-transfer)을 해석하지 못함(노드에서는 SERVFAIL, 파드 네트워크에서는 응답 없이 시간 초과, 호스트 이름뿐인 I/O 오류), 이체와 거래 내역 502, 잔액 조회와 commerce 정산은 정상 |
| F38-R | 정식 | 음식배달 | G | DB 인스턴스 | 운영자의 SQL 세션이 food MySQL 에 SET GLOBAL read_only=ON 을 실행해 인스턴스 전체가 읽기 전용, 앱 계정의 모든 쓰기가 1290 '--read-only option' 으로 거절(읽기, ping, readiness 는 정상), order 주문 전량 500, dispatch 만료 배치 매 주기 실패 |
| F39-R | 정식 | 은행 | G | 은행 계좌 서비스 | account-service 설정 롤아웃이 이체 하류 주소 TRANSFER_SERVICE_URL 을 다른 내부 호스트(testbed-ledger:8082, 그 Service 는 8083 만 엶)로 덮어써 account 가 넘기는 이체가 연결 시간 초과(Connect timed out)로 실패, 재시도와 서킷 끝에 account, api 502. 잔액 조회, 거래 내역, commerce 정산, NodePort 직행 이체는 정상 |

## 4-1. 후보 (검증, 녹화 대기, 집계 제외)

러너에서 실행은 되지만 아직 정상 녹화가 없는 시나리오다(`stage: candidate`). 모두 [녹화 대기 큐](../scripts/scenarios/recording-queue.json)에 있다.
녹화에 성공하면 운영 하네스가 §4로 옮기고, 폐기되면 §5로 옮긴다.

| id | 상태 | 서비스 | 묶음 | 정답 위치 | 한 줄 |
|---|---|---|---|---|---|
| F17-H | 후보 | 은행 | J+B | 은행 이체 서비스 | banking transfer-service 를 노드 이미지 저장소(imagePullPolicy Never)에 적재된 적 없는 릴리스 태그 core-banking-transfer:2.1.0 으로 롤아웃, 정본 전략 maxSurge 0 이라 옛 파드가 먼저 내려가고 새 파드는 ErrImageNeverPull 로 컨테이너를 만들지 못해 엔드포인트가 빔, account, api 이체와 거래 내역 502, commerce 정산 실패로 checkout 502. 잔액 조회는 정상, transfer 는 로그도 재시작도 없음 |
| F40-R | 후보 | 쇼핑몰 | G | 가격 서비스 | 운영자가 pricing 프로모션을 '10% 할인' 대신 할인율 100.00 으로 넣고 반영하려고 pricing 을 재시작, 모든 checkout 견적이 0 원이 되어 core-banking 이 정산 이체를 400 'amount must be positive' 로 거절, payment, order 502 로 모든 checkout 실패. pricing, banking 은 Ready 이고 빠르다 |
| F32-H | 후보 | 음식배달 | L+G | 배달 서비스 | dispatch-service 설정 배포가 Jackson 의 '숫자를 문자열로 쓰기'(SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS=true)를 켜 용량 응답이 "available":"N" 이 되고, order 가 그 값을 Integer 로 꺼내다 ClassCastException 으로 주문 저장 전에 503 'Dispatch service unreachable'. dispatch 는 Ready 이고 200 으로 답하며 한도는 그대로, notify 가 받는 dispatch 이벤트의 숫자도 따옴표로 바뀜 |
| F33-P | 후보 | 쇼핑몰 | F | DB 테이블(인증 토큰) | user_schema.auth_tokens 의 겹친 token 인덱스 두 개(UNIQUE 제약, idx_auth_tokens_token)를 합치는 마이그레이션이 새 유일 인덱스 동시 생성을 statement_timeout 으로 끊겨 INVALID 로 남긴 채 옛 둘을 지우고 이름을 바꿔, 게이트웨이가 쓰기마다 부르는 토큰 확인이 약 240만 행 전수 스캔이 됨. PostgreSQL(0.5 CPU) 포화, user Hikari 고갈, 게이트웨이가 확인을 자기 호출해 재시도·서킷 없이 인가된 쓰기 500. user 재시작 때 CREATE INDEX IF NOT EXISTS 는 같은 이름 때문에 건너뜀 |
| F41-R | 후보 | 은행 | J | 은행 계좌 서비스 | banking account-service 를 릴리스 core-banking-account:1.3.0(fault-images/f41-r 패치로 만든 별도 태그)으로 롤아웃. 새로 더한 잔액 스냅샷이 0.5초마다 전체 계좌를 다시 읽어 계좌별 이력에 붙이기만 하고 비우지 않아, 한도는 그대로인 힙(약 246MiB)이 기동 약 6분 뒤 참. 전체 GC 연속 뒤 OutOfMemoryError 로 요청 처리가 멈추고 readiness, liveness 실패로 kubelet 이 재시작, 같은 릴리스가 다시 채워 약 11~13분 주기로 잔액 조회 시간 초과, 500, 502 와 account 를 거치는 이체 502 가 되풀이. 거래 내역, commerce 정산, Oracle 은 정상 |
| F42-R | 후보 | 은행 | J+F | 은행 이체 서비스 | banking transfer-service 를 릴리스 core-banking-transfer:2.2.0(fault-images/f42-r 패치로 만든 별도 태그)으로 롤아웃. 새로 더한 일일 이체 한도 확인이 이체마다 'trunc(created_at) = trunc(sysdate)' 조건으로 출금 계좌의 오늘 합계를 구해 인덱스를 못 타고 transfers 약 630만 행을 전수 스캔(호출당 약 9.9만 블록, CPU 0.24~0.43초). 동반 부하 이체 약 8건/초에 공유 Oracle(2 CPU)이 포화되어 세션이 CPU 를 기다리고, transfer Hikari 가 스캔에 묶여 500, account 서킷, api 시간 초과로 이체 502, commerce 정산 실패. 새 버전은 떠서 프로브를 통과하고 재시작 없음, Oracle 은 Ready 이고 잠금 대기, DDL, 통계 변경 없음 |
| F43-R | 후보 | 쇼핑몰 | J+E | 장바구니 서비스 | commerce cart-service 를 릴리스 commerce-cart:1.2.0(fault-images/f43-r 패치로 만든 별도 태그)으로 롤아웃. 새로 더한 장바구니 품목 수 상한 확인이 담기마다 'try (PreparedStatement ps = dataSource.getConnection().prepareStatement(...))' 로 풀 연결을 빌려 문장만 닫고 연결은 돌려주지 않아, 담기 약 20번에 Hikari 풀(20)이 total=20, active=20, idle=0 으로 굳음(대기 스레드는 거의 없음, PostgreSQL 은 idle 세션만 늘고 한가함). cart 의 DB 작업이 3초 대기 끝에 실패하고 health 의 DB 확인이 시간 초과되어 readiness 가 엔드포인트를 비우고 liveness 가 재시작, 같은 버전이 다시 새게 해 되풀이. 담기, 비우기 500, 502, order 가 장바구니를 못 읽어 checkout 502. 원본 Octopus Deploy 2025-11-25(인가 서비스 배포가 DB 연결 누수를 들여와 로그인 간헐 시간 초과, 재시작으로 임시 완화) |
| F44-R | 후보 | 음식배달 | K+G | 노드, 디스크 | 운영자가 워커 tb-w3 의 호스트 방화벽(iptables FORWARD)에서 restaurant API 포트 8081 로 가는 새 연결을 노드와 진입 망(10.244.2.1, 10.244.0.0/24, 192.168.122.0/24, 192.168.200.0/24)에서만 받도록 좁히면서 같은 노드 파드 대역의 order-service 를 빠뜨림. order 가 열어 둔 keep-alive 연결이 닫힌 뒤 새 연결 SYN 이 모두 버려져(커널 '[FW BLOCK] ... DPT=8081') 가게 조회가 'Connect timed out', 재시도와 서킷 끝에 주문 전량 502. restaurant 는 Ready 이고 NodePort 로 오는 손님 조회에 계속 답함 |
| F44-P | 후보 | 은행 | K+G | 노드, 디스크 | 운영자가 은행 워커 tb-w2 의 호스트 방화벽(iptables FORWARD)에서 transfer API 포트 8082 로 가는 새 연결을 banking 파드 대역과 진입 망(10.244.1.0/24, 10.244.0.0/24, 192.168.122.0/24, 192.168.200.0/24)에서만 받도록 좁히면서 다른 노드 tb-w1 의 commerce 파드 대역(모든 checkout 의 정산 이체를 부르는 payment-service)을 빠뜨림. payment 가 열어 둔 keep-alive 연결이 닫힌 뒤 정산 이체 새 연결 SYN 이 모두 버려져(커널 '[FW BLOCK] ... DPT=8082') payment 가 'Connect timed out' 502, order 결제 재시도와 서킷 끝에 commerce checkout 전량 502. transfer 는 Ready 이고 banking 자신의 이체, 조회와 NodePort 직행 이체에 계속 답하며 'from=commerce-settlement' 이체만 끊김 |
| F47-R | 후보 | 은행 | J+E | 은행 API | banking api-service 를 릴리스 core-banking-api:1.4.0(fault-images/f47-r 패치로 만든 별도 태그)으로 롤아웃. 새로 더한 이체 전 활성 계좌 확인이 account 의 계좌 목록 API 를 짧은 페이지가 나올 때까지 읽어(status=ACTIVE, size=20, 활성 976개 → 49번) 활성 계좌 집합을 만들고, 이체마다 CompletableFuture.runAsync 로 뒤에서 다시 읽는다(수명, 겹침 방지 없음, CPU 1개 컨테이너라 작업마다 새 스레드). 동반 부하 이체 약 8건/초면 account 로 목록 요청 약 390건/초가 몰려 account(CPU 한도 500m)가 포화, 잔액 조회와 목록이 초 단위로 늦어지고 3초 풀 대기 500, readiness 실패로 잠깐씩 엔드포인트 이탈, api 의 account 서킷이 열려 이체 502 몰림. account 는 코드, 설정, 자원이 그대로이고 Oracle 은 여유, 거래 내역과 commerce 정산은 정상. 원본 Cloudflare 2025-09-12(대시보드 릴리스 버그가 Tenant Service API 호출을 크게 늘려 과부하) |
| F48-R | 후보 | 음식배달 | P+L | DB 테이블(인기 메뉴 집계) | 운영자가 food MySQL fooddelivery.menu_popularity_summary(인기 메뉴 집계 캐시 표, 88행)에 restaurant_id 인덱스를 더하는 온라인 인덱스 백필을 시작(그림자 표 _vt_vrp_..., 새 인덱스, 행 복사)했다가 취소, 취소 정리가 그림자 대신 살아 있는 표를 보류 이름 _vt_hld_... 으로 치움(행 보존). restaurant 의 인기 메뉴 조회가 모두 MySQL 1146 Table doesn't exist 로 500, 가게 상세, 메뉴, 검색과 주문은 정상. 롤아웃, 재시작, 설정 변경 없음, DPM 표 수 16→17, 인덱스 수 31→33. 원본 GitHub 2026-07-24(Vitess keyspace 백필 워크플로 취소가 기반 표를 지워 PR 생성만 실패, 변경을 되돌려 즉시 복구) |
| F49-R | 후보 | 음식배달 | L+J | 가게 서비스 | food restaurant-service 를 릴리스 food-delivery-restaurant:1.4.0(fault-images/f49-r 패치로 만든 별도 태그)으로 롤아웃. 공개 API 정비로 가게 상세 GET /api/restaurants/{id} 응답의 status 를 문자열 "OPEN" 에서 {code, label} 객체로 바꿈. restaurant 는 상세에 200 으로 답하고 health, 메뉴, 목록, DB 가 그대로라 Ready 를 유지하는데, 이 응답을 공용 RestaurantResponse(status String)로 읽는 order 가 'Error while extracting response for type [com.fooddelivery.common.dto.RestaurantResponse]' 로 해석에 실패, 재시도 3회와 서킷 끝에 주문 생성 전량 502(약 0.6초, 아무것도 쓰지 않음). 가게 둘러보기, 메뉴, 검색, 배달 조회는 정상. 원본 GitHub 2021-10-08(공개 API 출시 중 Codespaces 핵심 API 응답 구조가 의도치 않게 바뀌어 안정 스키마에 기대던 기존 클라이언트가 깨짐, 되돌려 복구) |
| F33-H | 후보 | 음식배달 | J+F | 배달 서비스 | food dispatch-service 를 릴리스 food-delivery-dispatch:1.6.0(fault-images/f33-h 패치로 만든 별도 태그)으로 롤아웃. 배달 앱 목록의 페이지 번호를 위해 GET /api/deliveries 에 전체 건수(X-Total-Count)를 더하며 목록 쿼리를 Slice 에서 Page 로 바꿔, 목록 요청마다 SELECT COUNT(d1_0.id) FROM dispatches d1_0 WHERE d1_0.status=? 가 끝난 배차 약 174만 항목을 훑음(109 실측 0.44~0.53초). 배달 추적 피크(목록 약 7.2건/초)에서 food MySQL(CPU 0.5) 포화, dispatch Hikari 10개가 COUNT 에 묶여 용량 확인과 배차 요청이 3초 대기 뒤 500, order 재시도와 서킷 끝에 주문 503. 스키마, 인덱스(index_count 31), 설정은 그대로이고 같은 표 만료 스윕의 검사 행 수도 그대로다(F33-R 은 모든 COUNT 와 만료 스윕이 전수). 포화 중 dispatch health 실패로 재시작이 나올 수 있다. 원본 GitHub 2025-01-09(배포가 들여온 질의가 주 DB 서버를 포화), 질의 꼴 MIT Open Learning 2026 |
| F49-H | 후보 | 음식배달 | L+J | 주문 서비스 | food order-service 를 릴리스 food-delivery-order:2.3.0(fault-images/f49-h 패치로 만든 별도 태그)으로 롤아웃. 주문 API 시각 표기를 앱 화면 표준 yyyy-MM-dd HH:mm:ss 로 맞추려고 ObjectMapper 의 LocalDateTime 직렬화기와 역직렬화기를 바꾸고 하류 RestClient 도 같은 ObjectMapper 를 쓰게 해, 바뀌지 않은 dispatch 가 배차를 기록하고 200 과 ISO assignedAt 으로 답하는데 order 가 'Error while extracting response for type [...DispatchResponse]' 로 읽지 못함. 재시도와 dispatch 서킷 끝에 주문 생성 전량 503(주문 롤백, dispatch 에 주인 없는 배차가 남음). order, dispatch 모두 Ready. F49-R(피호출자 restaurant 릴리스)과 같은 '호출자가 하류 응답을 못 읽음' 증상에 정답이 호출자 자신인 H |
| F50-R | 후보 | 은행 | G+B | 쿠버네티스 리소스 할당량 | 운영자가 rca-testbed-banking 네임스페이스에 ResourceQuota compute-resources(hard requests.memory 4Gi)를 걸었는데 떠 있는 파드의 요청 합이 이미 4928Mi 라, 떠 있는 파드는 그대로지만 transfer 일상 재배포(rollout restart, maxSurge 0)가 옛 파드를 내린 뒤 새 파드가 'exceeded quota: compute-resources' 로 거절되어 transfer 파드가 없음. account, api 의 transfer 호출 Connection refused 로 이체와 거래 내역 502, commerce checkout 정산 502. 이미지, 프로브, env 는 그대로, 잔액 조회 정상 |
| F48-P | 후보 | 음식배달 | P+L | DB 테이블(주문 이벤트 outbox) | 운영자가 food MySQL fooddelivery.order_outbox_events(order 가 주문과 같은 트랜잭션에서 주문 이벤트를 쓰는 outbox 표, 24시간 보존 약 5만 6천 행)에 aggregate_id 인덱스를 더하는 온라인 인덱스 백필을 시작(그림자 표 _vt_vrp_..., 새 인덱스, 행 복사)했다가 취소, 취소 정리가 그림자 대신 살아 있는 표를 보류 이름 _vt_hld_... 으로 치움. createOrder 가 가게, 메뉴, 용량 확인, 배차, 결제를 마친 뒤 outbox INSERT 에서 MySQL 1146 'Table doesn't exist' 로 실패해 주문 트랜잭션이 되돌려지고 주문 생성 전량 500('Failed to record outbox event'), 릴레이 폴링도 2초마다 같은 오류. 주문 조회, 둘러보기, 배달 추적, dispatch, payment 는 정상(실패한 주문의 배차와 결제가 남음). order 와 MySQL 은 Ready, 롤아웃 없음, DPM 표 수 16→17 |
| F51-R | 후보 | 은행 | P+B | 은행 계좌 서비스 | 운영자의 용량 명령 입력이 잘못되어 요청 경로의 banking account-service Deployment(testbed-account)를 replicas 0 으로 줄임(kubectl scale). 템플릿은 그대로이고 HPA 가 없어 파드가 다시 생기지 않아, testbed-account 엔드포인트가 비고 잔액 조회, 계좌 목록(nginx 502)과 이체(api 'Connection refused', 서킷 열림, 502)가 실패. 거래 내역(api → transfer)과 commerce 정산은 정상(AWS S3 2017-02-28 잘못 입력된 용량 제거 명령 재구성) |
| F43-P | 후보 | 음식배달 | J+E | 배달 서비스 | food dispatch-service 를 릴리스 food-delivery-dispatch:1.7.0(fault-images/f43-p 패치로 만든 별도 태그)으로 롤아웃. order 의 재시도로 같은 주문에 배달원을 두 번 잡지 않으려고 배차 요청 앞에 더한 중복 배차 확인이 DataSource 에서 연결을 직접 빌리고 중복을 찾아 409 를 던질 때만 닫아, 보통의 배차마다 연결 1개가 새고 Hikari 풀(10)이 배차 약 10건 만에 total=10, active=10, idle=0(대기 0~3)으로 굳음. 용량 확인, 배차, 배달 추적, 배치, health 가 연결 대기 3초 끝에 실패해 order 가 'Failed to check dispatch capacity: 500' 뒤 재시도와 서킷 끝에 주문을 503 으로 거절하고, dispatch 는 readiness 이탈과 liveness 재시작을 되풀이(재시작이 연결을 풀어 배차 약 10건 동안만 회복). MySQL 은 한가함(idle 세션만 약 10 증가). 풀 크기, 대기 시간, 프로브, 자원, 설정은 그대로 |
| F40-H | 후보 | 은행 | L+J | 은행 이체 서비스 | banking transfer-service 를 릴리스 core-banking-transfer:2.3.0(fault-images/f40-h 패치로 만든 별도 태그)으로 롤아웃. 요청 검증 강화로 더한 '원화 금액은 원 단위 정수만' 확인이 값이 아니라 BigDecimal 표기 자릿수(scale)로 판정해, 금액을 소수 둘째 자리까지 적어 보내는 commerce 정산 이체(27000.00 꼴)를 모두 400 'amount must be in whole won' 으로 거절. commerce payment 가 502 로 바꾸고 롤백해 checkout 이 전부 502, 정각 정산 배치도 실패. banking 자체 정수 금액 이체는 계속 완료되고 transfer 는 Ready, Oracle 평시. F40-R 과 같은 증상(은행 400 → checkout 502)에 다른 원인(H) |
| F52-R | 후보 | 은행 | P+B | 노드, 디스크 | 운영자의 이미지 보존 정리가 banking 워커 tb-w2 containerd 에서 '레지스트리 다이제스트가 없는 = 지난 빌드' 규칙으로 앱 이미지를 지웠는데, 이 테스트베드의 banking 앱 이미지는 레지스트리 없이 ctr import 로 적재되어 다이제스트가 없어 쓰고 있는 core-banking-api, account, transfer, ledger:latest 가 모두 지워짐(이미지마다 sudo ctr images rm, containerd ImageDelete). 떠 있는 컨테이너는 스냅숏으로 그대로지만 transfer 일상 재배포(rollout restart, maxSurge 0)가 옛 파드를 내린 뒤 새 파드가 ErrImageNeverPull 로 컨테이너를 못 만들어 transfer 파드가 없음. account, api 의 transfer 호출 Connection refused 로 이체와 거래 내역 502, commerce checkout 정산 502. 파드 템플릿(이미지 이름 :latest), replicas, 할당량은 그대로, 잔액 조회 정상. 원본 Logto 2023-12-17(자동 이미지 보존 작업이 '태그 없는 옛 이미지' 규칙으로 운영 이미지를 지워 서비스가 이미지를 가져오지 못함) |
| F53-R | 후보 | 음식배달 | P+L | DB 테이블(주문) | 주문 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 food MySQL 을 가리켜, 그 표 지우기 단계가 살아 있는 fooddelivery.orders(약 173만 행)를 지움(재구성: 행을 잃지 않게 DPM 이 보지 않는 시스템 스키마 mysql 로 RENAME). order createOrder 가 가게, 메뉴, 용량 확인 뒤 첫 쓰기인 주문 INSERT 에서 1146 "Table 'fooddelivery.orders' doesn't exist" 로 실패해 배차, 결제 전에 주문 전량 500, 주문 조회 500, 보존 정리 WARN 6초마다. 둘러보기, 배달 추적, dispatch, payment 정상, order Ready. DPM table_count 16→15, index_count 31→27, row_count 약 170만 감소. 원본 Resend 2024-02-21(로컬 마이그레이션 명령이 운영을 가리켜 운영의 모든 표를 지움, 약 12시간 중단). F48-P(백필 취소가 outbox 표를 치움, 표 수 17, 마지막 단계 실패), F38-R(인스턴스 read_only 1290)과 같은 주문 500 다른 원인 |
| F53-P | 후보 | 음식배달 | P+L | DB 테이블(결제) | 결제 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 food MySQL 을 가리켜, 그 표 지우기 단계가 살아 있는 fooddelivery.payments(약 174만 행, payment 만 씀)를 지움(재구성: F53-R 과 같은 시스템 스키마 mysql 로의 RENAME). payment processPayment 가 PG 호출 전 첫 쓰기인 결제 INSERT 에서 1146 "Table 'fooddelivery.payments' doesn't exist" 로 500, order 는 주문 저장과 배차 뒤 결제 단계에서 전량 502(재시도 뒤 payment 서킷 열림), 보존 정리 WARN 6초마다. PG 는 불리지 않음, 둘러보기, 배달 추적, 배차 정상, payment, order Ready. DPM table_count 16→15, index_count 31→28, row_count 약 170만 감소. 원본 Resend 2024-02-21(F53-R 과 같은 원본). F19-S, F06-P(외부 PG 장애), F53-R(주문 표, order 500), F38-R(read_only 1290)과 같은 결제 단계 또는 주문 실패 다른 원인 |
| F42-P | 후보 | 음식배달 | J+F | 가게 서비스 | food restaurant-service 를 릴리스 food-delivery-restaurant:1.5.0(fault-images/f42-p 패치로 만든 별도 태그)으로 롤아웃. 인기 메뉴 순위를 실시간으로 바꾸려고 GET /api/restaurants/{id}/popular-menu 가 1시간 배치가 채우는 캐시 표(menu_popularity_summary, 88행) 대신 요청마다 최근 7일 주문을 바로 셈(order_items JOIN orders JOIN menus, 가게 한 곳, 메뉴별 COUNT). 109 MySQL 이 한가할 때 4.05~7.47초, 한 번에 버퍼 풀 밖 읽기 약 2만 9천 쪽. 인기 메뉴 초당 약 2.5건이면 food MySQL(CPU 0.5) 포화, restaurant Hikari 10개가 집계에 묶여 가게 상세, 메뉴, 인기 메뉴가 3초 대기 뒤 500, readiness 이탈과 재시작 가능, order 가 가게, 메뉴 조회 실패로 주문 502. 스키마, 인덱스, 캐시 표는 그대로(1146 없음), 긴 활성 세션이 restaurant 파드 IP 에서 옴. 원본 GitHub 2025-01-09(배포가 들여온 질의가 주 DB 서버를 포화, F42-R, F33-H 와 같은 원본). 실행 중 MySQL 재시작은 uptime 으로 배제, 시작은 MySQL anon 여유가 있을 때만 |
| F35-H | 후보 | 은행 | P+L | DB 테이블(이체) | 이체 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 banking Oracle 을 가리켜, 그 표 지우기 단계가 살아 있는 BANKING.TRANSFERS(약 619만 행, transfer 만 씀)를 지움(재구성: 실제 DROP TABLE, 휴지통 on. USERS 사용량이 표와 인덱스만큼 약 2.4GB 떨어지고 cleanup 은 FLASHBACK 뒤 이름 복원). 이체가 두 계좌 잠금 뒤 transfers INSERT 에서 ORA-04043 으로 500, account, api 502, commerce-payment 정산 이체 실패로 commerce checkout 502, 거래 내역 ORA-00942 로 502. 잔액 조회, 계좌 목록, 원장, Oracle, 파드는 정상. 원본 Resend 2024-02-21(F53-R 과 같은 원본의 Oracle 판), 겉 증상은 F35-R(계정 잠금), F01-P(정산 계좌 잠금)와 같고 정답이 다른 H 짝 |
| F32-P | 후보 | 음식배달 | P+L | DB 테이블(배차) | 배차 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 food MySQL 을 가리켜, 그 표 지우기 단계가 살아 있는 fooddelivery.dispatches(약 174만 행, 외래 키 참조 없음, dispatch 만 씀)를 지움(재구성: 행을 잃지 않게 DPM 이 보지 않는 시스템 스키마 mysql 로 RENAME). dispatch 의 용량 확인, 배달 추적, 보존 정리(6초), 만료 배치(30초)가 1146 "Table 'fooddelivery.dispatches' doesn't exist" 로 실패하고, order 는 첫 쓰기 전 용량 확인 500 을 재시도와 서킷 끝에 503 'Dispatch service unavailable' 로 바꿔 주문 전량 거절(주문 행, 배차, 결제 없음). 둘러보기, 주문 조회, payment 정상, order, dispatch Ready. DPM table_count 16→15, index_count 31→28, row_count 약 174만 감소. 원본 Resend 2024-02-21(F53-R, F53-P, F35-H 와 같은 원본, 같은 수단의 다른 표). F32-R(배차 한도 설정), F32-H(숫자 형식 설정), F33-R(같은 표 인덱스 제거, 시간 초과), F33-H, F43-P(dispatch 릴리스)와 같은 order 503 다른 원인 |

## 5. 막힌 시도 (참고 자료, 집계 제외)

러너에 보이지 않고 실행하지 않는 보류(parked), 초안(draft) 시나리오다. 통계에 넣지 않는다.
쓸모는 둘이다. **반증된 것**은 같은 아이디어를 다시 설계하지 않게 막고, **도구가 없어 멈춘 것**은 빈 묶음의 후보 재료가 된다.
상세 사유는 `catalog.json`의 prerequisite와 `docs/scenario-redesign-wip/` 재심사 문서에 있다.

| id | 상태 | 서비스 | 비슷한 묶음 | 막힌 이유 | 한 줄 |
|---|---|---|---|---|---|
| F02-H | parked | 쇼핑몰 | A | 실측 반증 | PostgreSQL 디스크 IO를 81%까지 올려도 p95 41.9ms, 인과 없음 |
| F04-P | parked | 은행 | E | 실측 반증 | 원장 소비 처리 능력(300~500/s)이 유입보다 커서 밀리지 않음 |
| F07-P | parked | 쇼핑몰 | E | 실측 반증 | pricing bulkhead를 넘기려면 약 4,000rps 필요, 부하 상한 180 |
| F14-R | parked | 쇼핑몰 | N | 실측 반증 | 비멱등 재시도 중복 조건(하류 타임아웃 안 커밋과 동시 실패)이 성립하지 않음 |
| F20-P | parked | 은행 | F | 실측 반증 | 진입점에 해당 경로가 없어 404, 쿼리가 빨라 임계와 무관 |
| F21-P | parked | 은행 | E | 실측 반증 | CPU limit 레버로는 "느려지되 죽지 않음"이 안 되고 롤아웃이 됨 |
| F21-Q | parked | 음식배달 | E | 실측 반증 | 노드 CPU 스트레스가 상류 지연 대신 입구를 죽임 |
| F24-Q | draft | 음식배달 | C+N | 실측 반증 | 캘리브레이션 1회에서 restaurant 응답이 빨라 차단이 안 됨 |
| F02-P | parked | 음식배달 | F | 배관 부족 | 인덱스를 타는 조회 경로 신설과 성공 조건 교체 필요 |
| F03-P | parked | 쇼핑몰 | G | 배관 부족 | payment 풀 축소 강도와 최소 rps 미정 |
| F09-R | parked | 쇼핑몰 | A | 배관 부족 | 같은 노드 배치 고정(placement lock) 필요 |
| F13-R | parked | 쇼핑몰 | K | 배관 부족 | Ingress 리소스 도입 필요 |
| F13-H | parked | 쇼핑몰 | K | 배관 부족 | 네트워크 주입기(network.fault)가 껍데기, connect 단계 관측 필요 |
| F13-P | parked | 쇼핑몰 | K | 배관 부족 | 주입기(business.fault) 껍데기, WPM 다운로드 단계 계약 필요 |
| F15-T3 | parked | 은행 | A+B | 배관 부족 | 주입기와 관측은 있음, 프로필 재배정 등 잔여 작업 |
| F15-T4 | parked | 쇼핑몰 | D+B | 배관 부족 | 주입기는 있음, 비중첩 인계 판정 잔여 |
| F10-R | parked | 쇼핑몰 | A | 구조적 불가 | DB 전용 디스크가 없어 채우면 노드 전체 DiskPressure로 원인이 가려짐 |
| F10-P | parked | 은행 | A | 관리 제외 | Oracle 디스크 IO 포화. 정상 녹화 없음, 2026-10-08 사용자 결정으로 관리 제외 |
| F15-R | parked | 쇼핑몰 | C | 관리 제외 | 외부 결제 429 재발. 정상 녹화 없음(8월 20일 calibration 녹화 1개만 있음), 2026-10-08 사용자 결정으로 관리 제외 |
| F15-T2 | parked | 쇼핑몰 | D+C | 관리 제외 | 재고 잠금 뒤 음식배달 429. 정상 녹화 없음, 2026-10-08 사용자 결정으로 관리 제외 |
| F20-Q | parked | 음식배달 | F | 관리 제외 | 무페이징 조회 힙 압박. 정상 녹화 없음, 2026-10-08 사용자 결정으로 관리 제외 |

## 6. 갱신 기록

- 2026-10-08: 장부 생성. 묶음 A~N 정의, ready 39종 분류.
- 2026-10-08: 분류와 집계는 ready만 하기로 함(사용자 결정: 러너에 안 보이고 실행하지 않는 것은 통계가 아니라 참고 자료). parked 16, draft 1은 §5 막힌 시도로 옮기고 cut 4(음성 시나리오 폐지)는 기록하지 않음. catalog에 없는 설계 단계 안은 기록하지 않음.
- 2026-10-08: F30-R(draft)을 §4-1에 추가. 묶음 L(데이터 형식 불일치, ready 0), 계기는 설정 배포라 L+G. 원본 사례 Flagsmith 2026-03-03. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-08: F30-R 을 첫 시험 실행을 위해 ready + calibration 으로 올림(compile-plan 이 controller 를 ready 에만 허용). 첫 실행 전이라 §4-1 에 그대로 둠.
- 2026-10-08: 정식의 기준을 ready 에서 "eval-cases 에 정상 녹화 1개 이상"(`stage: official`)으로 바꿈([수명주기](spec-scenario-lifecycle.md)). 녹화가 없는 ready 4종(F10-P, F15-R, F15-T2, F20-Q)을 §4에서 §4-1 후보로 옮기고 집계를 정식 35종으로 다시 셈. B 가 20%에 닿음.
- 2026-10-08: 후보 4종(F10-P, F15-R, F15-T2, F20-Q)을 폐기(parked). 정상 녹화가 하나도 없고 사용자가 파악하지 못하는 기존 시나리오라 관리 대상에서 뺌(사용자 결정). §4-1 에서 §5 로 옮김(막힌 이유: 관리 제외). 후보는 F30-R 하나. §3 집계는 정식만이라 변동 없음.
- 2026-10-08: 새 후보를 고를 때의 상한과 서비스 균형을 정식 + 후보 합계로 세기로 함(사용자 결정, 설계가 녹화보다 빨라 후보가 쌓이므로). §3 은 정식만 유지.
- 2026-10-08: §2-1 정답 위치 축 추가, §4, §4-1 표에 "정답 위치" 칸. 정식 35종 분류, 결제 경로 합계 10/36(정식 + 후보)로 상한 초과(F30-R 을 만든 세션의 지적, 사용자 결정).
- 2026-10-08: F32-R(후보)을 §4-1에 추가. 묶음 G(설정 오배포, 정식 + 후보 2), 정답 위치 배달 서비스(0), 음식배달(정식 + 후보 5). 원본 사례 Google 2020-12-14(쿼터 관리가 User ID Service 쿼터를 실제 사용량 아래로 줄여 인증 실패). 쿼터 축소가 아니라 "자원이 모자람"(A)으로 볼 수도 있으나 CPU, 메모리, 디스크 같은 물리 자원은 그대로이고 바뀐 것은 배포된 한도 값이라 G 로 둔다. F31 은 같은 날 폐기된 후보(로컬 기록)가 쓴 번호라 겹치지 않게 F32 를 쓴다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-08: F33-R(후보)을 §4-1에 추가. 묶음 F(느린 쿼리, 정식 + 후보 1), 정답 위치 DB 테이블(배차)(0, §2-1 의 "DB 테이블(<무엇>)" 꼴), 음식배달(정식 + 후보 6). 원본 사례 Buildkite 2025-11-10(마이그레이션이 인덱스를 제거해 고빈도 질의가 타임아웃되고 DB CPU 과부하). 계기는 스키마 변경이지만 설정값 배포가 아니라 G 가 아니고, 피해는 MySQL CPU 포화로 번지지만 CPU 한도는 그대로이고 바뀐 것은 한 질의의 검사 행 수라 A 가 아니다. 같은 "인덱스 제거" 발상의 F02-P(§5, 배관 부족)는 핫 경로 밖의 인덱스라 막혔고, F33-R 은 주문마다 도는 질의의 인덱스라 그 막힘이 없다. 서로 다른 인덱스라 같은 주입이 아니다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-08: F35-R(후보)을 §4-1에 추가. 새 묶음 O(자격 증명 만료, 회전 실수, 정식 + 후보 0)를 §2 에, 새 정답 위치 'DB 계정'(0)을 §2-1 에 더함. 은행(정식 + 후보 8, 음식배달 7 다음). 원본 사례 Harness 2026-01-08(예정된 비밀 회전 중 DB 사용자 자격 증명 하나가 잘못되어 Template Service 가 DB 인증을 잃음, 옛 DB 사용자를 다시 활성화해 복구). I(인증 의존 실패)는 인증을 맡은 서비스가 고장 나는 묶음이라 맞지 않고, DB 는 살아 있고 자원과 잠금도 정상이라 A, B, D 도 아니다. 음식배달이 1 적지만 음식배달은 게이트웨이 없이 order, restaurant 가 NodePort 로 바로 노출되고 네 서비스가 계정 하나와 DB 를 보는 liveness 를 함께 써서, 같은 장애에서 입구가 연결 불가(필수 중단 조건 entry_status 0)가 되어 실행이 중단된다. 은행은 api 와 nginx 가 DB 를 쓰지 않아 입구가 502 로 답하고 liveness 가 DB 를 보지 않는다. F34 는 같은 날 폐기된 후보(로컬 기록)가 쓴 번호라 F35 를 쓴다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-08: F30-R 운영 보강 1회차(후보 유지, §4-1 그대로). 실행 F30-R-run-867f92cd 가 dispatch 커넥션 풀 고갈로 must_rule_out "배차 쪽 아님"을 어겨(이전 실행에서도 재현) 동반 부하 target_rps 를 5 에서 2 로 낮춤. 주입 대상과 수단은 그대로. 근거 `~/dev/testbed-ops/verifications/2026-10-08-1536-F30-R-run-867f92cd.md`. 큐 attempts 0 → 1. §3 집계 변동 없음.
- 2026-10-08: F36-R(후보)을 §4-1에 추가. 묶음 L(데이터 형식 불일치, 정식 + 후보 1→2): 테이블 스키마(주는 쪽)와 그것을 읽는 ORM 엔티티(받는 쪽)가 기대하는 열 이름이 안 맞는다. 계기는 스키마 변경(DDL)이지만 설정값 배포가 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 도 아니며, 질의가 느려지는 것이 아니라 실행 전에 거절되어 F(F33-R)도 아니다. 정답 위치 DB 테이블(가게)(0, §2-1 의 "DB 테이블(<무엇>)" 꼴), 음식배달(정식 + 후보 7, 가장 적음). 원본 사례 Onfido 2024-10-18(Studio 테이블 스키마 마이그레이션이 새 코드가 모든 인스턴스에 퍼지기 전에 배포되어 옛 인스턴스가 더는 없는 열을 조회, 15분간 Studio 트래픽 약 23% 5xx, 공식 사후 보고). 0인 묶음(J, K, M, N)은 이번에도 막혔다: J 는 결함 이미지가 없고(app.release live_supported false, 앱 코드 변경은 사람 검토 대상), K 는 network.fault 가 의도적으로 막혀 있고, M 은 같은 날 폐기된 F31(사람이 되살릴 후보)과 겹치며, N 은 F14-R 막힘 그대로다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-08: F37-R(후보)을 §4-1에 추가. 묶음 M(DNS, 정식 + 후보 0)이 주 묶음이고 계기가 배포 설정이라 M+G 로 적는다. 정답 위치 은행 API(0, §2-1 에 이미 있는 말), 은행(정식 + 후보 9, 음식배달 8 다음). 원본 사례 Intercom 2024-11-05(서버 이미지에 더한 캐싱 resolver 가 공개 이름은 해석하지만 사설 DNS 레코드는 해석하지 못해 교체된 백그라운드 워커가 내부 DB 엔드포인트에 닿지 못함, 공식 사후 보고). 같은 M 의 폐기 후보 F31(로컬 기록, AWS 2025-10-19~20 빈 DNS 레코드 재구성, Service 삭제 주입)과 원본 사례, 주입, 정답 위치가 모두 다르다: F31 은 레코드 쪽이 사라졌고 F37-R 은 레코드는 그대로인데 한 서비스의 resolver 설정이 바뀌었다. 공용 CoreDNS 는 건드리지 않는다. 음식배달(8)을 먼저 보지 않은 이유: 사용자 요청을 받는 order, restaurant, dispatch, payment 는 모두 DB 를 보는 health 를 써서, 같은 배포를 하면 새 파드가 MySQL 이름을 해석하지 못해 Ready 가 되지 않고 롤아웃이 옛 파드를 남긴 채 멈춰 피해가 나지 않는다(원본의 웹 서버 쪽 모양). DB 를 쓰지 않는 notify 는 Kafka 소비자라 사용자 요청 경로에 없어 5xx 가 나지 않는다(원칙 7). 은행 api 는 DB 를 쓰지 않고 health 가 이름 해석과 무관해 새 파드가 옛 파드를 대체한다(원본의 워커 쪽 모양). catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-08: F38-R(후보)을 §4-1에 추가. 묶음 G(설정 오배포, 정식 + 후보 3→4): 바뀐 것은 DB 인스턴스의 설정값 read_only 하나이고(원본 상태 페이지 기록도 "A configuration error"라 부른다), DB 프로세스는 살아 있고 빠르며 자원, 잠금, 스키마, 계정은 그대로라 A, B, D, F, L, O 가 아니다. 해석: 계기는 배포가 아니라 운영 중 `SET GLOBAL` 로 바꾼 런타임 설정이지만, 런타임 설정 변경도 G 로 센다(§2 G 정의의 "배포"를 운영 중인 시스템에 설정값을 적용하는 일로 넓게 읽는다. 정의 문장은 기존 항목이라 고치지 않는다). 이 해석은 균형 판단을 바꾸지 않는다(G 4, 10%). 정답 위치 DB 인스턴스(정식 + 후보 1→2, §2-1 에 이미 있는 말), 음식배달(정식 + 후보 8→9, 가장 적음). 원본 사례 Vapi 2025-01-21(운영 DB 에 붙은 SQL 클라이언트 세션의 읽기 전용 설정이 커넥션 풀러를 거쳐 모든 세션에 퍼져 쓰기 실패, API 15분 중단, DB 재시작으로 복구, 공식 상태 페이지). 같은 '읽기 전용' 발상의 F14-P(정식, H)는 Oracle 테이블 하나를 읽기 전용으로 바꿔 원장 쓰기가 조용히 사라지는 장애이고, F38-R 은 인스턴스 전체라 모든 서비스의 쓰기가 같은 오류로 드러난다. 주입 수단(새 실행기 db.instance_readonly, 전역 변수 한 칸), 대상, 증상이 다르다. 0인 묶음(J, K, N)은 이번에도 막혔다: J 는 노드에 결함 이미지가 없고(food, banking 이미지는 latest 하나씩), K 는 CNI 가 flannel 단독이며 bandwidth 플러그인도 없어(2026-10-08 tb-w3 conflist 실측) 쿠버네티스 쪽 네트워크 주입 수단이 없고, N 은 모든 동기 호출의 서킷브레이커 때문에 F14-R 막힘 그대로다. 함께 검토하고 버린 것(로컬 기록): banking Oracle 인덱스 제거(원장 멱등 확인 idx_ledger_ref 또는 이체 조회 인덱스). 원장 쪽은 비동기 소비자라 사용자 5xx 로 이어질 계산이 서지 않고, 이체 내역 질의는 OR-null 꼴이라 지금도 인덱스를 타지 않아(전수 스캔, 실행 계획 실측) 인덱스를 지워도 달라지는 것이 없다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-08: `F30-R` 정식 승격(케이스 `case-f30-r-v3-0cd9193b`). 강도 보강 1회차(동반 부하 5→2rps, 901a56c) 뒤 실행 F30-R-run-0cd9193b 가 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 통과). 근거 `~/dev/testbed-ops/verifications/2026-10-08-1845-F30-R-run-0cd9193b.md`. §4-1 → §4, §3 을 정식 36종으로 다시 셈(L 0→1, 음식배달 4→5, 정답 위치 결제 서비스 2→3, 결제 경로 9→10 28%). 후보 6종.
- 2026-10-08: F39-R(후보)을 §4-1에 추가. 묶음 G(설정 오배포, 정식 + 후보 4→5, 43 중 12%): 바뀐 것은 account-service 배포의 설정값 하나(이체 하류 주소 env)이고 코드, 이미지, 자원, DB, 네트워크는 그대로라 J, A, B, K 가 아니다. 이름은 해석되고 실패는 잘못된 호스트로의 연결 시간 초과라 M 도 아니다. 정답 위치 은행 계좌 서비스(0, §2-1 에 이미 있는 말), 은행(정식 + 후보 10→11). 음식배달(9)이 1 적지만 고르지 않은 이유: 음식배달에서 같은 기전(호출하는 쪽 배포의 하류 주소 오설정)을 걸 곳은 order 의 SERVICES_DISPATCH_URL, SERVICES_RESTAURANT_URL 인데 정답 위치가 주문 서비스(6, 정식 + 후보 중 두 번째로 많음)가 되고, dispatch 쪽은 order 의 용량 확인이 503 으로 끝나는 F32-R 의 증상을 되풀이한다. restaurant 는 하류 호출이 없고 payment 쪽은 결제 경로(23%, 금지)다. 음식배달의 남은 표면은 MySQL 위주라 최근 후보(F33-R, F36-R, F38-R)와 겹친다. 원본 사례 GitHub 2026-03-05(운영에 롤아웃한 Redis 인프라 갱신이 로드밸런서에 잘못된 설정을 넣어 내부 트래픽을 잘못된 호스트로 보냄, 설정을 바로잡아 복구, 공식 월간 가용성 보고)와 GitHub 2026-06-10(롤아웃이 인증 서비스가 잘못된 호스트 설정을 집어 들게 함). 둘 다 M1 에 추가. 같은 "호출하는 쪽 설정 배포가 정상 하류 호출을 끊는" 계열의 F08-P(read-timeout), F37-R(dnsPolicy, 이름 해석 실패)와 주입 칸, 실패 단계(연결 시간 초과), 정답 위치가 다르다. F17-R 과는 증상(account → transfer 실패로 이체 502)이 같고 원인이 다르다(F17-R 은 transfer NotReady, Connection refused). 0인 묶음(J, K, N)은 이번에도 막혔다: J 는 노드에 결함 이미지가 없고, K 는 CNI 가 flannel 단독이라 쿠버네티스 쪽 네트워크 주입 수단이 없으며(Service 설정 변경은 kcm_resources_history 에 2026-09-17 뒤로 Service, Deployment 레코드가 쌓이지 않아 계기 흔적이 남지 않음, 2026-10-08 119 PG 실측), N 은 모든 동기 호출의 서킷브레이커 때문에 F14-R 막힘 그대로다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: `F32-R` 정식 승격(케이스 `case-f32-r-v3-63b10537`). 첫 실행 F32-R-run-c5e84509 뒤 재실행 F32-R-run-63b10537 이 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 통과). 근거 `~/dev/testbed-ops/verifications/2026-10-09-0111-F32-R-run-63b10537.md`. §4-1 → §4, §3 을 정식 37종으로 다시 셈(G 2→3, 음식배달 5→6, 정답 위치 배달 서비스 0→1, 결제 경로 10 27%). 후보 6종.
- 2026-10-08: 서비스 균형을 "차이 3 초과 시 최다 서비스 금지"에서 "적은 서비스 우선, 최다 서비스는 정답 위치 0인 부품만"으로 완화(생성 하네스가 후보 고갈로 멈춘 뒤 사용자 결정).
- 2026-10-09: `F33-R` 정식 승격(케이스 `case-f33-r-v3-73fed92a`). 첫 실행 F33-R-run-73fed92a 가 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 통과). 근거 `~/dev/testbed-ops/verifications/2026-10-09-0425-F33-R-run-73fed92a.md`. §4-1 → §4, §3 을 정식 38종으로 다시 셈(F 1→2, 음식배달 6→7, 정답 위치 DB 테이블(배차) 0→1, 결제 경로 10 26%). 후보 5종.
- 2026-10-09: F40-R(후보)을 §4-1에 추가. 묶음 G(설정 오배포, 정식 + 후보 5→6, 44 중 14%): 바뀐 것은 pricing 이 읽는 업무 설정(프로모션 행 하나의 할인율)이고 코드, 이미지, env, 자원, 스키마는 그대로라 J, A, L 이 아니다. 실패는 조용하지 않고 모든 checkout 이 502 라 H 도 아니다. 원본 사례 Google Cloud 2025-06-12(Service Control 이 읽는 정책 테이블에 빈 필드가 든 정책이 들어가 오류 처리, 기능 플래그 없는 경로가 그 값을 읽어 모든 요청 실패). 새 정답 위치 '가격 서비스'(0)를 §2-1 에 더함. 쇼핑몰(정식 + 후보 24, 가장 많은 서비스)이지만 정답 위치가 0 인 부품이라 낸다(원칙 2). 정답은 결제 경로가 아니다(결제는 증상, 부분 점수).
- 2026-10-09: F17-H(후보)를 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 0→1), 피해 모양이 파드 준비 상태라 J+B 로 적는다. 해석: J 정의 문장은 '코드 결함이 있는 버전'이지만 이 후보의 결함은 코드가 아니라 릴리스 산출물(배포가 가리킨 이미지가 이미지 저장소에 없음)이다. 계기와 결함이 모두 새 버전 롤아웃(이미지 참조 변경) 쪽에 있고 설정값, 자원, DB 는 그대로라 G, A, D 가 아니며, 현실 근거인 Google 의 '바이너리 배포' 트리거에 가장 가깝다(정의 문장은 기존 항목이라 고치지 않고 §2 아래 'J 보충' 한 줄로 넓혔다). 증상이 같은 F17-R(B, readinessProbe 오설정)과 묶음을 가르는 것은 원인이다. J 를 B 로 세더라도 B 7→8(45 중 18%)로 상한 아래다. 정답 위치 은행 이체 서비스(정식 + 후보 3→4, 9%), 은행(정식 + 후보 11→12). 원본 사례 Harness 2025-10-28(일상 배포가 레지스트리에서 삭제된 이미지를 필요로 해 새 파드가 뜨지 못하고 인증 엔드포인트 502, 이미지 복원과 재배포로 복구, 공식 상태 페이지 사후 보고)와 Pipefy 2024-05-22(설정이 가리킨 이미지가 레지스트리에 없음, 설정 되돌림). 둘 다 M2 에 추가. id 는 같은 증상(transfer 엔드포인트가 비어 banking 이체와 commerce 정산 실패), 다른 원인이라 F17 의 H 다. J 가 지금까지 막혔던 이유(노드에 결함 이미지가 없고 앱 코드 변경은 사람 검토 대상)는 이 후보에 해당하지 않는다: 결함 이미지를 만들지 않고 없는 태그를 가리킨다. 같은 '없는 이미지로 롤아웃' 주입의 cut F05-G(commerce payment, 기본 전략이라 옛 파드가 계속 서비스해 무영향, 음성 시나리오 폐지)는 장부에 없고, 이 후보는 정본 전략이 maxSurge 0 인 transfer 를 골라 피해가 난다(새 실행기 k8s.image 의 preflight 가 maxSurge 0 을 확인). 음식배달(9)을 고르지 않은 이유: 음식배달 Deployment 는 모두 기본 전략(maxSurge 25%)이라 같은 롤아웃이 옛 파드를 남겨 피해가 없다(F05-G 결과). 쇼핑몰에서 maxSurge 0 인 곳은 testbed-user 하나인데 정답 위치 사용자 서비스(1)가 0 이 아니라 쇼핑몰(24) 규칙에 걸린다. 판별력 한계: F17-R(readinessProbe 오설정)과 F01-P(Oracle 잠금으로 transfer NotReady)와 겉 증상(transfer 엔드포인트가 비어 account, api 의 testbed-transfer 'Connection refused', banking 이체 502, commerce 정산 실패)과 정답 서비스(transfer)가 같다. 서비스 입도 채점에서는 'transfer' 라는 답이 세 시나리오 모두에서 만점이므로, 이 후보의 판별력은 정답지 mechanism 채점(이미지 부재 대 readiness 오설정 대 DB 잠금)과 must_support 에만 있다. 정답 위치 축(은행 이체 서비스 4)에서 이 쏠림을 함께 본다. 함께 검토하고 버린 후보: commerce 클러스터 DNS(CoreDNS) 영역 전달 오설정(Let's Encrypt 2025-07-21). 119 KCM 이 kube-system 을 수집하지 않아(kcm_events_local 의 kube-system 0건, kcm_resources_history 의 kube-system 마지막 2026-07-31, kcm_pod_logs_local 0행) 계기 흔적이 남지 않는다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: F32-H(후보)를 §4-1에 추가. 주 묶음 L(데이터 형식 불일치, 정식 + 후보 2→3, 46 중 6.5%), 계기가 설정 배포라 L+G 로 적는다: 보내는 쪽(dispatch)의 JSON 값 형식(정수 → 문자열)과 받는 쪽(order)이 기대하는 형식이 어긋난다. 키 이름은 그대로라 F30-R(명명 규칙, 필드 유실)과 실패 꼴이 다르다. 정답 위치 배달 서비스(정식 + 후보 1→2, 4%), 음식배달(정식 + 후보 9→10, 가장 적음). 원본 사례 Flagsmith 2026-03-03(Edge API 릴리스가 정수형 기능 값의 JSON 형식을 문자열 "300" 과 숫자 300 사이에서 바꿔 정적 타입 언어 SDK 가 깨짐, 이전 릴리스로 롤백해 복구, 공식, M8 에 이미 있음). F30-R 과 원본이 같지만 원본의 기전(값의 타입 변경)에는 F32-H 가 더 가깝다. id 는 F32-R 과 증상(dispatch 롤아웃 직후 order 가 주문 저장 전에 503)이 같고 원인이 달라(한도 값 대 출력 형식) F32 의 H 다. 판별력: 서비스 입도 채점에서는 F32-R 과 정답(dispatch)이 같으므로 이 쌍의 판별력은 mechanism 과 must_support(order WARN 'cannot be cast' 대 INFO 'courier pool exhausted', notify 이벤트 숫자의 따옴표)에 있다. 0인 묶음(K, N)은 이번에도 막혔다: K 는 tb-w3 flannel 경로 삭제(Datadog 2023-03-08 재구성)를 검토했으나 원본 계기(systemd-networkd 재시작)는 flannel.1(networkd 관리 밖 링크)의 경로를 지우지 않아 재현이 수동 경로 삭제가 되고, 끊기는 것이 tb-cp 의 CoreDNS 뿐이라 증상이 F37-R(이름 해석 실패)과 같으며, Hikari 연결이 교체되며 food 의 DB 헬스가 떨어져 입구 재시작(entry_status 0) 위험이 있다. N 은 모든 동기 호출의 서킷브레이커 때문에 F14-R 막힘 그대로다. catalog와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: F32-H 적대적 평가 권고 메모. L 묶음 3개 중 F30-R, F32-H 가 같은 원본(Flagsmith 2026-03-03)과 같은 주입 계열(food 서비스의 Jackson 직렬화 env 배포)에 몰려 있다. 다음 L 이나 food 후보는 원본 사례와 부품(서비스, 주입 수단)을 바꿔 고른다. F32-R 과 F32-H 는 러너 success 가 같아 녹화 검증이 로그(order 'cannot be cast' 대 'courier pool exhausted')로 가른다.
- 2026-10-09: F33-P(후보)를 §4-1에 추가. 묶음 F(느린 쿼리, 정식 + 후보 2→3, 47 중 6%): 바뀐 것은 한 테이블의 인덱스 구성(DDL)이고 설정값, 코드, 이미지, 자원 한도는 그대로라 G, J, A 가 아니며, 잠금 대기가 아니라 질의 비용이 바뀌어 D 도 아니다. 정답 위치 DB 테이블(인증 토큰)(0, §2-1 의 'DB 테이블(<무엇>)' 꼴). 쇼핑몰은 정식 + 후보가 가장 많지만(24) 정답 위치가 0 인 부품이라 낸다(원칙 2). 음식배달(10), 은행(12) 후보는 이번 회차에 모두 막혔다(사유는 설계 시트 §3). 원본 사례 Vapi 2024-10-02(새 복합 인덱스 생성이 실패해 INVALID 로 남은 줄 모르고 옛 인덱스를 지워 가장 큰 테이블이 인덱스 없이 남음, DB CPU 100%, API 타임아웃, 쿠버네티스가 건강하지 않은 파드를 재시작해 악화, 인덱스 재생성으로 복구, 공식 상태 페이지 사후 보고). F33-R 과 원인 꼴(고빈도 질의가 인덱스를 잃음)이 같아 F33 사례군의 P(일부 비슷)로 둔다: 원본 사례, DB 엔진, 인덱스를 잃는 방식(동시 재생성 실패 뒤 교체, 이름은 남음), 전파(모든 인가된 commerce 쓰기의 게이트웨이 500)가 다르다. 단 INVALID 와 이름 바꾸기 차이는 119 에 직접 보이지 않아 녹화로 F33-R 과 가르는 것은 Top SQL 문장, DB 엔진, 실패 진입 경로다(적대적 평가 지적). 쏠림 기록: 이 후보로 DB 계열 정답 위치(DB 테이블 + DB 인스턴스 + DB 계정) 합계가 47 중 13(27.7%), commerce PostgreSQL 이 정답이거나 정답 테이블을 가진 시나리오가 6개가 된다. 위치별 20% 상한은 지키지만 다음 후보는 DB 밖 부품을 먼저 본다.
- 2026-10-09: `F35-R` 정식 승격(케이스 `case-f35-r-v3-bbbee443`). 첫 실행 F35-R-run-bbbee443 이 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 통과). 근거 `~/dev/testbed-ops/verifications/2026-10-09-0753-F35-R-run-bbbee443.md`. §4-1 → §4, §3 을 정식 39종으로 다시 셈(O 0→1 로 §3 표에 O 행 추가, 은행 8→9, 정답 위치 DB 계정 0→1, C 와 외부 결제 의존, 주문 서비스 16%→15%, 결제 경로 10 26%). 후보 8종.

- 2026-10-09: F41-R(후보)을 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 1→2, 48 중 4%): 코드 결함(메모리 누수)이 든 새 버전 롤아웃이 계기이자 결함이고 설정값, 자원 한도, 프로브, DB 는 그대로라 G, A, B 가 아니다. 피해 모양이 메모리 고갈이지만 A(자원 부족, 한도)는 한도가 모자라거나 줄어든 경우라 넣지 않는다(힙 한도는 평시와 같고 사용량을 끝없이 늘린 것이 새 코드다). F17-H(J+B, 산출물 결함)와 달리 J 정의 문장 그대로의 '코드 결함이 있는 버전'이고, 2026-10-09 결함 버전 이미지 규칙(설계 원칙 §3 예외)으로 만든 첫 시나리오다. 정답 위치 은행 계좌 서비스(1→2, 4%), 은행(정식 + 후보 12→13). 음식배달(10)이 더 적지만 고르지 않은 이유: 음식배달에서 메모리 누수 재시작 주기를 걸 수 있는 서비스가 막혔다(order 는 loadgen 생성 입구라 재시작 중 entry_status 0 이 abort, restaurant 는 주문 여정의 메뉴 조회 입구라 표본이 사라짐, dispatch 는 배달 서비스 2개와 order 503 증상이 겹침, payment 는 결제 경로 상한). 원본 사례 Honeycomb 2019-11-06(잘못된 커밋의 느린 메모리 누수가 모든 수집 백엔드에서 같은 속도로 진행해 몇 분 차이로 함께 죽는 오류 구간이 되풀이, 커밋 되돌림으로 복구, 공식 사후 보고). catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: F42-R(후보)을 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 2→3, 49 중 6%), 피해 모양이 느린 쿼리라 J+F 로 적는다: 계기이자 결함이 새 버전이 들여온 질의이고 스키마, 인덱스, 통계, 설정값, 자원 한도는 그대로라 F(인덱스 제거 F33-R, F33-P), G, A 가 아니다. F 로 세더라도 F 3→4(8%)로 상한 아래다. 정답 위치 은행 이체 서비스(정식 + 후보 4→5, 10%), 은행(정식 + 후보 13→14). 원본 사례 GitHub 2025-01-09(배포가 들여온 질의가 주 데이터베이스 서버를 포화시켜 여러 기능에서 서버 오류, 배포를 되돌려 복구, 공식 월간 가용성 보고, M2 에 추가). 결함 패턴 P8(슬로우 쿼리가 공유 DB CPU 를 오염, F20-R 계열)이지만 F20-R 은 원래 있던 비싼 경로를 부르는 트래픽이 늘어난 장애이고 F42-R 은 새 릴리스가 비싼 문장을 들여왔다. 음식배달(정식 + 후보 10)이 더 적지만 고르지 않은 이유: 같은 기전(요청 경로에 새 비싼 질의)을 걸 food 서비스가 막힌다. order 는 질의가 order 자신의 Hikari 를 묶어 readiness, liveness(둘 다 DB 를 보는 /actuator/health)가 실패하면 loadgen 입구가 연결 불가(필수 중단 조건 entry_status 0)가 되고, restaurant 는 주문 여정의 메뉴 조회 입구라 표본이 사라지며, dispatch 는 배달 서비스 2개와 F33-R(MySQL 포화로 order 503)이 이미 있고, payment 는 결제 경로 상한, notify 는 DB 를 쓰지 않는 Kafka 소비자다. 은행에서 account 가 아니라 transfer 를 고른 이유는 account 에 같은 날 J(F41-R)가 있어서다. 판별력 한계: 겉 증상(transfer 풀 'Connection is not available', 이체와 정산 실패)이 F01-P(정산 계좌 행 잠금)와 비슷하고 정답 서비스가 F17-R, F17-P, F18-P, F17-H 와 같은 transfer 다. 이 후보의 판별력은 DPM 대기 이벤트(On CPU, resmgr:cpu quantum 대 enq: TX), 롤아웃 직후 처음 나타난 sql_id, 이미지 태그에 있다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: `F36-R` 정식 승격(케이스 `case-f36-r-v3-776b522b`). 첫 실행 F36-R-run-776b522b 가 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 통과). 근거 `~/dev/testbed-ops/verifications/2026-10-09-1125-F36-R-run-776b522b.md`. §4-1 → §4, §3 을 정식 40종으로 다시 셈(L 1→2 3%→5%, 음식배달 7→8, 정답 위치 DB 테이블(가게) 0→1 로 행 추가, 결제 경로 10 26%→25%). 후보 9종.
- 2026-10-09: F43-R(후보)을 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 3→4, 50 중 8%), 피해 모양이 연결 풀 고갈이라 J+E 로 적는다: 계기이자 결함이 새 버전 코드(빌린 연결을 닫지 않음)이고 풀 크기, 대기 시간, 프로브, 자원 한도, DB 설정은 그대로라 G, A, O 가 아니며, DB 는 한가하고 잠금도 느린 질의도 없어 D, F 가 아니다. E 로 세더라도 E 2→3(6%)이다. 새 정답 위치 '장바구니 서비스'(0)를 §2-1 에 더함. 쇼핑몰(정식 + 후보 25→26, 가장 많은 서비스)이지만 정답 위치가 0 인 부품이라 낸다(원칙 2). 음식배달(10)을 먼저 봤으나 같은 기전을 걸 자리가 막혔다: order 는 loadgen 생성 입구이고 readiness 가 DB 를 봐 풀이 마르면 entry_status 0 abort, restaurant 는 주문 여정의 메뉴 조회 입구라 표본이 사라짐, payment 는 결제 경로 금지, notify 는 DB 가 없음, dispatch 는 order 503 증상이 이미 셋(F32-R, F32-H, F33-R)이고 F32-H 평가 메모가 다음 food 후보는 부품을 바꾸라고 권고. 원본 사례 Octopus Deploy 2025-11-25(공식 상태 페이지 사후 보고, 인가 서비스에 배포한 변경이 DB 연결 누수 버그를 들여와 로그인 요청이 간헐적으로 시간 초과, 서비스 재시작으로 연결을 풀어 임시 완화, 누수를 없앤 버전으로 해결). F33-P 와 러너 success(checkout 5xx)가 같아 녹화 검증이 cart 'Connection is not available'(total=20, active=20, 대기 적음)와 PostgreSQL 이 한가함으로 가른다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: F44-R(후보)을 §4-1에 추가. 주 묶음 K(네트워크 지연, 손실, 단절, 정식 + 후보 0→1, 51 중 2%), 계기가 운영자의 방화벽 설정 변경이라 K+G 로 적는다: 바뀐 것은 워커 tb-w3 의 호스트 방화벽 규칙(한 포트로 가는 새 연결의 허용 목록)이고 서비스의 코드, 이미지, env, 쿠버네티스 객체, DB 는 그대로라 J, L, B 가 아니다. 이름은 맞게 해석되고(M 아님) 주소도 평소 그대로라 하류 주소 오설정(F39-R, G)과 다르며, 실패는 응답 오류가 아니라 연결이 맺어지지 않는 것이라 C 가 아니다. 정답 위치 노드, 디스크(§2-1 '쿠버네티스 노드, 디스크, 네트워크 등 인프라', 3→4, 8%), 음식배달(정식 + 후보 10→11, 가장 적은 서비스). 결제 경로 합계 10/51(19.6%)로 이 후보가 결제 쪽이 아니라 그대로다. 원본 사례 Harness 2024-09-01(지나치게 열려 있던 레지스트리 방화벽 규칙을 좁히며 한 구성 요소의 NAT IP 를 빠뜨림, 지속 소켓 연결이 다시 맺어질 때 드러남, 공식 상태 페이지 사후 보고), M15 에 추가. 새 실행기 host.firewall(ssh, 규칙마다 sudo iptables). §5 의 K 막힌 시도(F13-H, F13-P, F13-R)는 주입기 부재였고, 이 후보는 쿠버네티스 쪽 수단(NetworkPolicy, flannel 단독이라 불가) 대신 노드 방화벽을 쓴다.
- 2026-10-09: F44-P(후보)를 §4-1에 추가. 주 묶음 K(네트워크 지연, 손실, 단절, 정식 + 후보 1→2, 52 중 4%), 계기가 운영자의 방화벽 설정 변경이라 K+G 로 적는다: 바뀐 것은 은행 워커 tb-w2 의 호스트 방화벽 규칙(한 포트로 가는 새 연결의 허용 목록)이고 서비스의 코드, 이미지, env, 쿠버네티스 객체, DB 는 그대로라 J, L, B, D 가 아니며, 이름은 해석되고 주소도 평소 그대로라 M 이나 하류 주소 오설정(F39-R)이 아니다. 정답 위치 노드, 디스크(4→5, 52 중 9.6%), 결제 경로 합계 10/52(19.2%)이고 이 후보의 정답은 결제 쪽이 아니다(commerce payment 는 증상, 부분 점수). 은행(정식 + 후보 14→15). 원본 사례 Central 1 2025-04-09(방화벽 오류가 결제 플랫폼 UCP 와 Interac 망 사이 연결을 끊음, UCP 는 응답 유지, e-Transfer 실패, 방화벽 규칙을 바로잡아 복구, 공식 상태 페이지 사후 보고), M15 에 추가. F44-R 과 원인 꼴(방화벽 허용 목록이 호출자 하나를 빠뜨림)과 주입 수단(host.firewall)이 같아 F44 사례군의 P(일부 비슷)로 둔다: 원본 사례, 원인 노드(tb-w3 food → tb-w2 banking), 전파(같은 노드 안 → 노드를 건너는 overlay, 도메인 횡단), 영향 범위(원인 도메인 자신의 주문 → 다른 도메인 commerce 의 checkout, 원인 도메인 banking 은 멀쩡)가 다르다. F44-R 설계 때 보류한 'tb-w2 Oracle 1521 방화벽'(같은 원본, F35-R 증상)과는 원본, 포트, 증상이 다르다. 음식배달(11)이 더 적지만 고르지 않은 이유: food 의 0 부품(kafka, notify)은 비동기 소비자라 사용자 증상이 없고, order 는 loadgen 생성 입구라 order 자신의 장애는 entry_status 0 abort, tb-w3 인프라 층은 방화벽(F44-R), 디스크 IO(F10-H), conntrack, MTU, 경로가 이미 쓰였거나 막혔다(설계 시트 §11). 판별력: commerce 정산 실패라는 겉 증상은 F17-R, F17-H, F01-P, F35-R, F42-R 과 같지만, 그 다섯은 banking 자신의 요청도 실패하고 F44-P 는 banking 이 멀쩡하며 실패가 'Connection refused' 나 풀 고갈이 아닌 'Connect timed out' 이다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: `F37-R` 정식 승격(케이스 `case-f37-r-v3-43c16731`). 첫 실행 F37-R-run-43c16731 이 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 통과). 근거 `~/dev/testbed-ops/verifications/2026-10-09-1449-F37-R-run-43c16731.md`. §4-1 → §4, §3 을 정식 41종으로 다시 셈(M 0→1 0%→2%, 은행 9→10, 정답 위치 은행 API 0→1 로 행 추가, 결제 경로 10 25%→24%, 나머지 비율은 분모 41 로 다시 셈). 후보 11종.
- 2026-10-09: F47-R(후보)을 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 4→5, 53 중 9.4%), 피해 모양이 서비스 처리 용량 초과라 J+E 로 적는다(E 로 세도 2→3, 5.7%): 계기이자 결함이 api 의 새 버전 코드(이체마다 account 목록 전체를 다시 읽음)이고, 과부하된 account 의 코드, 설정, 자원 한도와 게이트웨이에서 오는 사용자 요청은 그대로라 G, A, C 가 아니다. 재시도는 이미 있던 것이 거들 뿐 요청을 불린 주체는 새 코드라 N 이 아니다. DB 는 여유가 있어 D, F 가 아니다. 정답 위치 은행 API(정식 + 후보 1→2, 3.8%, §2-1 에 이미 있는 말), 결제 경로 10/53(18.9%, 이 후보와 무관). 은행(정식 + 후보 15→16): 음식배달(11)을 먼저 봤으나 같은 기전을 걸 자리가 막혔다(order 는 트랜잭션 안에서 restaurant 를 불러 restaurant 가 느려지면 order 풀이 마르고 readiness(DB)가 빠져 entry_status 0, 증상도 order→restaurant 실패가 F36-R, F44-R 과 겹침). 원본 사례 Cloudflare 2025-09-12(대시보드 릴리스의 버그가 Tenant Service API 호출을 재시도까지 포함해 크게 늘려 그 서비스가 과부하, 거기 기대는 API 요청 5xx, 공식 블로그). F45, F46 은 이번 실행에서 폐기된 후보(로컬 기록)가 쓴 번호라 겹치지 않게 F47 을 쓴다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: `F38-R` 정식 승격(케이스 `case-f38-r-v3-54918c10`). 첫 실행 F38-R-run-54918c10 이 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 통과. must_support 3번 "DPM insert_count, update_count 가 0 근처" 는 실측과 달라 정답지 문구 정정 대상, 재실행 불필요). 근거 `~/dev/testbed-ops/verifications/2026-10-09-1808-F38-R-run-54918c10.md`. §4-1 → §4, §3 을 정식 42종으로 다시 셈(G 3→4 7%→10%, 음식배달 8→9, 정답 위치 DB 인스턴스 1→2 2%→5%, 결제 경로 10 24% 그대로, C 와 외부 결제 의존, 주문 서비스 15%→14%, 나머지 비율은 분모 42 로 다시 셈해 변동 없음). 후보 11종.
- 2026-10-09: F48-R(후보)을 §4-1에 추가. 새 묶음 P(운영 작업의 대상 착오, 정식 + 후보 0→1, 54 중 1.9%)를 §2 에 더하고, 결함이 앱이 기대하는 표가 없는 꼴로 드러나 P+L 로 적는다. 해석: 바뀐 것은 표 하나의 존재(운영 도구가 그림자 표 대신 살아 있는 표를 보류 이름으로 치움)다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 적용된 것(F36-R, L)도 아니다: 백필이 의도한 변경(인덱스 추가)은 맞았고 적용 대상이 틀렸다. 그래서 같은 '스키마 쪽 고장' 인 F36-R 의 L 에 넣지 않고 새 묶음으로 둔다(L 로 세더라도 3→4, 7.4%). 기존 운영자 계기 시나리오(F38-R 운영 세션의 SET GLOBAL read_only, G)는 운영자가 그 설정값을 바꾸려 한 것이라 G 그대로 두고 다시 분류하지 않는다. 정답 위치 DB 테이블(인기 메뉴 집계)(0→1, §2-1 의 'DB 테이블(<무엇>)' 꼴, 새 줄 불필요). 음식배달(정식 + 후보 11→12, 가장 적은 서비스). 결제 경로 10/54(18.5%), 이 후보와 무관. 참고: DB 계열 정답 위치(DB 테이블 + DB 인스턴스 + DB 계정) 합계는 13→14(54 중 25.9%)지만, 정답 위치는 표, 인스턴스, 계정으로 나뉘어 각각 20% 상한을 세므로 버릴 사유가 아니다(2026-10-09 사용자 정정: 다양성의 목적은 같은 정답 위치가 몰려 외워 찍지 않게 하는 것뿐). 원본 사례 GitHub 2026-07-24(월간 가용성 보고, 풀 리퀘스트 데이터를 담은 Vitess keyspace 로의 백필 워크플로를 취소하자 잘못 이해된 Vitess 코드 경로가 기반 표를 지워 PR 생성만 57분 실패, 데이터베이스 변경을 되돌려 즉시 재개, 공식), M21 에 추가. F36-R 과는 같은 restaurant 와 food MySQL 의 표 쪽 고장이지만 원본, 계기(마이그레이션 대 취소된 백필), 오류(1054 없는 열 대 1146 없는 표), 피해 범위(가게 조회와 주문 대 인기 메뉴만)가 다르고, F36-R 녹화(2026-10-09 10:19~10:35 UTC)에서 인기 메뉴는 200 그대로였다(표본 98건 오류 0). 판별력: 계기가 DPM 표 수와 인덱스 수 증가로 보이고(9일 동안 16 고정), 정답 표가 오류 문장에 그대로 찍힌다. F45, F46 은 이번 실행에서 폐기된 후보(로컬 기록)가 쓴 번호라 F48 을 쓴다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: F49-R(후보)을 §4-1에 추가. 주 묶음 L(데이터 형식 불일치, 정식 + 후보 3→4, 55 중 7.3%), 계기가 새 버전 배포라 L+J 로 적는다(J 로 세도 5→6, 10.9%): 보내는 쪽(restaurant 새 릴리스)의 응답 구조(status 문자열 → 객체)와 받는 쪽(order)이 기대하는 형식이 어긋난다. env 설정 배포가 아니라 이미지 변경이라 F30-R, F32-H(L+G)와 계기가 다르고, restaurant 는 200 으로 답해 F36-R(표 열 이름 변경, restaurant 500), 연결이 맺어져 F44-R(K)와 다르다. 정답 위치 가게 서비스(정식 + 후보 0→1, §2-1 에 이미 있는 말), 음식배달(정식 + 후보 12→13, 가장 적음). 결제 경로 10/55(18.2%) 그대로. 같은 원본(GitHub 2021-10-08)과 같은 재구성의 반복 6 후보 F45-R 은 '롤아웃 서비스가 정답' 비공식 축과 증상 겹침만으로 폐기됐는데, 2026-10-09 사용자 정정으로 둘 다 버릴 사유가 아니게 되어 새 번호로 다시 냈다(F45 는 재사용하지 않음). catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: F33-H(후보)를 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 5→6, 56 중 10.7%), 피해 모양이 느린 쿼리(공유 DB 포화)라 J+F 로 적는다(F 로 세도 3→4, 7.1%): 계기이자 결함이 dispatch 의 새 버전 코드(목록 쿼리를 Page 로 바꿔 요청마다 전체 건수 COUNT)이고 스키마, 인덱스, 통계, 설정값, 자원 한도는 그대로라 F(F33-R 인덱스 제거), G, A 가 아니다. dispatch 풀 고갈은 결과이고 풀 크기와 유입 요청 꼴은 평시와 같아 E 도 아니다. 정답 위치 배달 서비스(정식 + 후보 2→3, 5.4%, §2-1 에 이미 있는 말), 음식배달(정식 + 후보 13→14, 가장 적음), 결제 경로 10(17.9%) 그대로. 관계: F33-R 과 겉 증상(MySQL 포화, dispatch 풀 'Connection is not available', 주문 503)이 같고 정답이 다른 H 다. 가르는 관측 근거는 넷이다: DPM index_count 31 그대로(F33-R 은 30), 같은 표 만료 스윕 digest(b79f4ca8…)의 rowExamined 가 그대로(F33-R 은 전수), status COUNT digest(4ca1b0a1…)의 평균 rowExamined 가 배정 중(약 300)과 끝난 배차(약 174만)가 섞인 값(F33-R 은 모든 호출이 약 175만, 호출 수는 MySQL 처리량에 묶여 판별에 쓰지 않음), 그리고 KCM 의 testbed-dispatch 롤아웃과 이미지 태그 1.6.0. 앞선 F42-R 장부 메모는 같은 기전의 dispatch 판을 '배달 서비스 2개와 F33-R 증상 겹침'으로 고르지 않았으나, 증상 겹침은 버릴 사유가 아니라는 2026-10-09 사용자 정정에 따라 낸다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-09: F49-H(후보)를 §4-1에 추가. 주 묶음 L(데이터 형식 불일치, 정식 + 후보 4→5, 57 중 8.8%), 계기가 새 버전 배포라 L+J 로 적는다(J 로 세도 6→7, 12.3%): 받는 쪽(order 새 릴리스)이 기대하는 시각 형식(yyyy-MM-dd HH:mm:ss)과 보내는 쪽(dispatch, 그대로)의 형식(ISO)이 어긋난다. F49-R 과 같은 L+J 지만 형식을 바꾼 쪽이 호출자라 정답 위치가 다르다. env 설정 배포가 아니라 이미지 변경이라 F30-R, F32-H(L+G)와 계기가 다르고, dispatch 는 200 으로 답해 F33-R, F33-H(F)와 다르다. 정답 위치 주문 서비스(6→7, 12.3%, 음식배달 order 는 처음), 결제 경로 10(17.5%) 그대로, 음식배달 14→15. 어느 축도 20% 미만.
- 2026-10-09: `F39-R` 정식 승격(케이스 `case-f39-r-v3-dc45499a`). 첫 실행 F39-R-run-dc45499a 이 검증에서 녹화 가능(주입, 피해, 원인 근거, must_rule_out 6개 통과). 근거 `~/dev/testbed-ops/verifications/2026-10-09-2127-F39-R-run-dc45499a.md`. §4-1 → §4, §3 을 정식 43종으로 다시 셈(G 4→5 10%→12%, 은행 10→11, 정답 위치 은행 계좌 서비스 0→1 로 행 추가, 결제 경로 10 24%→23%, A, B, D 17%→16%, 나머지 비율은 분모 43 으로 다시 셈해 변동 없음). §3 의 후보 수는 §4-1 실제 행 수로 맞춰 14종(그동안 후보 추가 때 11 로 남아 있었다).
- 2026-10-09: F50-R(후보)를 §4-1에 추가. 주 묶음 G(설정 오배포, 정식 + 후보 6→7, 58 중 12.1%), 피해 모양이 파드가 없는 부품 멈춤이라 G+B 로 적는다(B 로 세도 7→8, 13.8%): 바뀐 것은 네임스페이스 승인 설정값 하나(ResourceQuota 의 hard requests.memory 가 사용량 아래)이고 서비스의 코드, 이미지, env, 프로브, 자원 한도는 그대로라 J 가 아니며, 노드 자원은 남아 있고 컨테이너나 노드가 한도에 걸려 모자란 것이 아니라 A 도 아니다. F32-R(G, 배차 동시 한도를 실제 사용량 아래로 둔 서비스 설정)과 '한도를 실제 사용량 아래로 둠'은 같지만 층(클러스터 승인 객체)과 실패 꼴(떠 있는 것은 멀쩡하고 새 파드만 거절)이 다르다. 새 정답 위치 '쿠버네티스 리소스 할당량'(0→1, 1.7%)을 §2-1 에 더함(노드, 디스크와 달리 노드는 멀쩡하고 API 서버의 승인이 막는다). 결제 경로 10(17.2%) 그대로, 은행 16→17. 어느 축도 20% 미만. 같은 증상(transfer 파드가 없어 banking 이체와 commerce 정산 502)의 F17-H(없는 이미지), F17-R(readiness)과는 FailedCreate 'exceeded quota' 와 바뀌지 않은 파드 템플릿으로 갈린다(카탈로그 관계 H, id 는 새 사례군 F50).
- 2026-10-09: F48-P(후보)를 §4-1에 추가. 주 묶음 P(운영 작업의 대상 착오, 정식 + 후보 1→2, 59 중 3.4%), 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F48-R 처럼 P+L 로 적는다(L 로 세도 5→6, 10.2%): 바뀐 것은 표 하나의 존재(운영 도구가 그림자 표 대신 살아 있는 표를 보류 이름으로 치움)이고 설정값, 코드, 이미지, 자원은 그대로라 G, J, A 가 아니며, DB 는 빠르고 잠금 대기도 없어 D, F 가 아니다. 새 정답 위치 DB 테이블(주문 이벤트 outbox)(0→1, 1.7%, §2-1 의 'DB 테이블(<무엇>)' 꼴), 음식배달(정식 + 후보 15→16, 가장 적음), 결제 경로 10/59(16.9%) 그대로. 원본 사례는 F48-R 과 같은 GitHub 2026-07-24(백필 워크플로 취소가 기반 표를 지워 PR '생성'만 실패)이고, F48-R 이 그 사례의 '한 읽기 기능만 실패' 쪽을, F48-P 가 '만들기만 실패' 쪽을 재구성한다. 같은 수단(db.ddl 보류 표 모드)이지만 표, 인덱스, 진입 경로, 영향 범위가 달라 같은 주입 중복이 아니다(카탈로그 §1 R 조건: 진입 경로와 영향 범위가 다름). 관계 접미사는 F44-P 선례처럼 P 로 둔다(F48-R 과 원인 기전은 같고 전파와 영향 범위가 다름). 겉 증상(order 주문 생성 500)은 F38-R 과 같고 정답이 다르다(H 관계): F38-R 은 주문 INSERT 의 1290 '--read-only' 와 dispatch, payment 쓰기 실패, F48-P 는 outbox INSERT 의 1146 과 다른 쓰기 성공으로 갈린다. 반복 8 의 보류 후보 'order_items 를 같은 백필이 치움'과 원본은 같고 표가 다르다(order_items 는 258만 행이라 그림자 복사가 MySQL 을 오래 붙잡는다). 동반 부하는 두지 않는다: 주문이 배차, 결제 뒤에 되돌려져 주문 동반 부하가 바쁜 시간에 배달원 한도(2000)를 채우기 때문이다.
- 2026-10-09: F51-R(후보)를 §4-1에 추가. 주 묶음 P(운영 작업의 대상 착오, 정식 + 후보 2→3, 60 중 5%), 피해 모양이 파드가 없는 부품 멈춤이라 P+B 로 적는다(B 로 세도 7→8, 13.3%): 바뀐 것은 Deployment 의 replicas 하나(운영 명령의 입력 실수로 요청 경로 서비스의 용량을 없앰)이고 코드, 이미지, env, 프로브, 자원은 그대로라 J, G, A 가 아니다. 정답 위치 은행 계좌 서비스(2→3), 은행(17→18), 결제 경로 10/60(16.7%) 그대로. 원본 사례 AWS S3 2017-02-28(플레이북 명령의 입력 하나가 잘못되어 의도보다 많은 서버가 빠졌고 그 서버가 요청 경로 하위 시스템 index, placement 의 용량이었음). F50-R, F17-H, F17-R(transfer 파드 없음)과 겉 증상이 같고 정답과 실패 범위가 다른 짝, F41-R, F39-R 과 정답(account)이 같고 계기가 다른 짝. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-10: F43-P(후보)를 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 6→7, 61 중 11.5%), 피해 모양이 연결 풀 고갈이라 F43-R 처럼 J+E 로 적는다(E 로 세도 2→3, 4.9%): 계기이자 결함이 dispatch 의 새 버전 코드(빌린 연결을 한 갈래에서만 닫음)이고 풀 크기, 대기 시간, 프로브, 자원 한도, MySQL 설정은 그대로라 G, A, O 가 아니며, MySQL 은 한가하고 잠금도 느린 질의도 없어 D, F(F33-R, F33-H)가 아니다. 정답 위치 배달 서비스(정식 + 후보 3→4, 6.6%), 결제 경로 10/61(16.4%, 이 후보와 무관), 음식배달(정식 + 후보 16→17, 은행 18, 쇼핑몰 26 보다 적음). 원본 사례 Octopus Deploy 2025-11-25(인가 서비스에 배포한 변경이 DB 연결 누수를 들여와 로그인 시간 초과, 재시작으로 연결을 풀어 임시 완화, 누수 없는 버전으로 해결, 공식 상태 페이지 사후 보고, M2 에 이미 있음). F43-R 과 원본, 결함 꼴, 주입 수단(k8s.image release)이 같아 F43 사례군의 P(일부 비슷)로 둔다: DB 엔진(PostgreSQL → MySQL), 진입 경로(nginx → gateway → cart 대 loadgen → order NodePort → 용량 확인 서킷), 피해 서비스의 자리(진입 뒤 cart 대 호출자 order 가 따로 503 으로 거절), 누수 꼴(문장만 닫는 try-with-resources 대 중복 갈래에서만 닫음)이 다르다. F43-R 설계 때 dispatch 판을 증상 겹침(order 503 이 F32-R, F32-H, F33-R 과 넷째)으로 버렸으나 2026-10-09 사용자 정정(증상 겹침은 버릴 사유가 아님, 관계 H 는 오히려 가치가 큼)에 따라 다시 낸다. 같은 dispatch 풀 고갈인 F33-R, F33-H 와는 MySQL active 세션, 응답 시간, Top SQL, Hikari 대기 스레드 수로 가른다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-10: F40-H(후보)를 §4-1에 추가. 주 묶음 L(데이터 형식 불일치, 정식 + 후보 5→6, 62 중 9.7%), 계기이자 결함이 transfer 의 새 버전 코드라 F49-R, F49-H 처럼 L+J 로 적는다(J 로 세도 7→8, 12.9%): 새 버전이 받는 요청 형식의 기대를 좁혀(금액 표기 자릿수 0) 예전부터 받던 정당한 호출자(commerce 정산, 27000.00 꼴)의 형식을 거절한 것이라 L 이고, 설정값, 자원, DB 는 그대로라 G, A, D 가 아니며 거절이 즉시 400 이라 C(지연, 429)도 아니다. F40-R(G, 같은 은행 400 → checkout 502 증상, 정답 가격 서비스)과 증상이 같고 원인이 달라 관계 H. 정답 위치 은행 이체 서비스(정식 + 후보 5→6, 9.7%), 결제 경로 10/62(16.1%) 그대로, 은행(18→19). 원본 사례 Flagsmith 2024-01-18(릴리스가 더한 요청 검증이 일부 클라이언트가 정당하게 생략하던 키를 요구해 유효한 요청 거절), Buttondown 2026-06-14(스키마 강화가 자사 화면이 보내던 필드를 거절해 422), 자료 문서 M8 에 추가.
- 2026-10-10: F52-R(후보)를 §4-1에 추가. 주 묶음 P(운영 작업의 대상 착오, 정식 + 후보 3→4, 63 중 6.3%), 피해 모양이 파드가 없는 부품 멈춤이라 F51-R 처럼 P+B 로 적는다(B 로 세도 7→8, 12.7%): 운영 정리 작업(이미지 보존 정리)의 의도는 지난 빌드를 지우는 것이었는데 선택 규칙(레지스트리 다이제스트 없음)이 쓰고 있는 이미지까지 골라 살아 있는 대상을 지웠다. 서비스의 코드, 이미지 이름, env, 프로브, 자원, replicas, 할당량은 그대로라 J, G 가 아니고, 노드 자원이 모자란 것이 아니라 A 도 아니다. 정답 위치 노드, 디스크(정식 + 후보 5→6, 9.5%). 노드의 컨테이너 이미지 저장소는 §2-1 '노드, 디스크'의 '쿠버네티스 노드 등 인프라'에 해당한다. 결제 경로 10(15.9%) 그대로, 은행 19→20. 어느 축도 20% 미만. 같은 증상(transfer 파드가 없어 banking 이체와 commerce 정산 502)의 F17-H(롤아웃이 노드에 없던 릴리스 태그를 가리킴, 이벤트 사유도 같은 ErrImageNeverPull), F50-R(할당량 승인 거절), F51-R(replicas 0), F17-R(readiness)과 정답이 다른 짝(카탈로그 관계 H, id 는 새 사례군 F52)이다. 가르는 관측 근거는 tb-w2 syslog 의 sudo ctr images rm 넷과 containerd ImageDelete(30일 안 0건), 재배포 ReplicaSet 의 이미지 이름이 그대로 :latest 라는 점, 새 파드는 스케줄되고 FailedCreate 가 없다는 점이다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-10: F53-R(후보)를 §4-1에 추가. 주 묶음 P(운영 작업의 대상 착오, 정식 + 후보 4→5, 64 중 7.8%), 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F48-R, F48-P 처럼 P+L 로 적는다(L 로 세도 6→7, 10.9%): 마이그레이션이 의도한 변경 내용(기능 개발 중 주문 표 바꾸기)은 맞았는데 명령이 가리킨 대상(환경)이 로컬 DB 가 아니라 운영이었다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 운영에 적용된 F36-R(L)과도 다르다(운영에 적용할 변경이 아니었다). DB 프로세스, 자원, 잠금, 질의 비용은 그대로라 A, B, D, F 가 아니다. 정답 위치 DB 테이블(주문)(정식 + 후보 0→1, 1.6%, §2-1 'DB 테이블(<무엇>)' 꼴), 주문 서비스(7)가 아니다. 결제 경로 10/64(15.6%) 그대로, 음식배달 17→18(은행 20, 쇼핑몰 26 보다 적음). 어느 축도 20% 미만. 원본 사례 Resend 2024-02-21(기능을 만들던 엔지니어가 로컬에서 돌린 마이그레이션 명령이 운영 환경을 가리켜 운영의 모든 표를 지움, API, 대시보드 약 12시간 중단, 백업에서 복원, 공식 블로그 사고 보고, M22 에 추가). 같은 '표가 사라짐' 꼴의 F48-R, F48-P 와 원본(GitHub 2026-07-24 백필 취소)과 도구가 달라 새 사례군 F53 으로 둔다. 같은 food 주문 500 인 F48-P(표 수 16→17 그림자 표, outbox INSERT 마지막 단계 실패, 배차와 결제가 남음), F38-R(1290 read_only, dispatch, payment 쓰기도 실패)과 정답이 다른 짝(카탈로그 관계 H): 가르는 근거는 오류가 가리키는 표(orders), DPM 표 수가 줄어드는 방향(16→15)과 row_count 감소, 실패 지점(첫 쓰기, 'Created order' 멈춤, 배차와 결제 없음)이다. rejected 의 'food order_items 를 취소된 백필이 치움'(GitHub 원본, 그림자 복사가 큰 표에서 수십 초)과 '시험 작업 환경 변수가 운영 DB 를 가리켜 메뉴, 가게 표를 비움'(Travis CI 원본, 빈 표라 400)과는 원본, 주입, 피해 꼴이 다르다(그림자 복사 없음, 표가 없어 5xx). catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-10: F53-P(후보)를 §4-1에 추가. 주 묶음 P(운영 작업의 대상 착오, 정식 + 후보 5→6, 65 중 9.2%), 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F53-R 처럼 P+L 로 적는다(L 로 세도 7→8, 12.3%). 정답 위치 DB 테이블(결제)(정식 + 후보 1→2, 3.1%, F06-H 다음), 결제 경로 10→11/65(16.9%, 20% 미만), 음식배달 18→19(가장 적음, 은행 20, 쇼핑몰 26). 어느 축도 20% 미만. 원본 사례 Resend 2024-02-21(F53-R 과 같은 원본, M22 에 있음). F48-P 가 F48-R 의 원본과 수단을 다른 표에 쓴 것처럼 F53-R 의 원본과 수단(db.ddl 표 지우기 모드, 계약의 표만 payments)을 다른 표에 써서 F53 사례군의 P(일부 비슷)로 둔다: 표를 쓰는 서비스(order 대 payment), 전파(자기 INSERT 500 대 하류 500 이 서킷을 거쳐 order 502), 실패 지점(배차 전 대 배차 뒤 결제 단계), 정답 위치가 다르다. 같은 수단의 다른 표는 다른 파라미터라 같은 주입 중복이 아니다. 겉 증상(food 주문 502 'Payment service ...')은 외부 PG 장애 F19-S, F06-P(정답 외부 결제 의존)와 같고 정답이 달라(카탈로그 관계 H) 관제 AI 가 '결제 단계 502 면 PG' 를 외워 찍지 못하게 하는 짝이다: 가르는 근거는 payment 로그의 1146 과 표 이름, 'PG /pay failed' 없음과 PG 호출 스팬이 사라짐, DPM 표 수 16→15 다. rejected 의 '취소된 백필이 food payments 표를 치움'(GitHub 2026-07-24 원본, 174만 행 그림자 복사가 수십 초라 결제 INSERT 를 막음)과는 원본, 주입이 다르다(그림자 복사 없이 메타데이터 한 문장). catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-10: F42-P(후보)를 §4-1에 추가. 주 묶음 J(결함 있는 새 버전 배포, 정식 + 후보 7→8, 66 중 12.1%), 피해 모양이 느린 쿼리(공유 DB 포화)라 F33-H, F42-R 처럼 J+F 로 적는다(F 로 세도 3→4, 6.1%): 계기이자 결함이 restaurant 의 새 버전 코드(인기 메뉴를 캐시 표 대신 요청마다 주문 원장에서 셈)이고 스키마, 인덱스, 통계, 설정값, 자원 한도는 그대로라 F(인덱스 제거), G, A 가 아니다. restaurant 풀 고갈은 결과라 E 도 아니다. 정답 위치 가게 서비스(정식 + 후보 1→2, 3.0%, §2-1 에 이미 있는 말), 음식배달(정식 + 후보 19→20, 가장 적음), 결제 경로 11(16.7%) 그대로. 어느 축도 20%(13.2) 아래. 관계: F42-R(은행 transfer 릴리스의 전수 스캔 질의), F33-H(dispatch 릴리스의 목록 COUNT)와 '릴리스가 요청 경로에 비싼 질의를 넣어 공유 DB 와 자기 풀을 말림' 원인 꼴이 같고 대상, 질의, 피해 경로가 달라 F42 의 P 로 둔다. F48-R(인기 메뉴 캐시 표를 취소된 백필이 치움)과는 같은 인기 메뉴 끝점이 실패하지만 원인(표 소멸 대 집계 질의), 오류(1146 대 풀 대기 시간 초과), 범위(인기 메뉴만 대 가게 화면과 주문 전체, MySQL 포화)가 다르다. 가르는 관측 근거: KCM testbed-restaurant 롤아웃과 태그 1.5.0, dpm_session_local 의 restaurant 파드 IP 긴 활성 세션, table_count 와 index_count 불변. 원본은 GitHub 2025-01-09(배포가 들여온 질의 하나가 주 DB 서버를 포화, 되돌려 복구)로 F42-R, F33-H 와 같다. 장부상 가장 가까운 형제는 F33-H(같은 food MySQL, 같은 '릴리스 질의 → 자기 풀 고갈' 꼴)이고 가르는 근거는 롤아웃 대상, 풀 고갈 서비스, order 오류 문구, DPM 세션 출발 파드다. 처음 낸 MIT Open Learning 2026-03-24 원본은 평가에서 막힌 자원(임시 디스크 대 CPU, 버퍼 풀), 회복 꼴, 변경 동기가 다르다는 지적을 받아 뺐다. catalog 와 장부 대조 결과 다른 변동 없음.
- 2026-10-10: F35-H(후보)를 §4-1에 추가. 주 묶음 P(운영 작업의 대상 착오, 정식 + 후보 6→7, 67 중 10.4%), 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F53-R, F53-P 처럼 P+L 로 적는다(L 로 세도 6→7, 10.4%): 마이그레이션이 의도한 변경 내용(기능 개발 중 이체 표 바꾸기)은 맞았는데 명령이 가리킨 대상(환경)이 로컬 DB 가 아니라 운영이었다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계정, 잠금, 자원, 질의 비용은 그대로라 O, D, A, F 가 아니다. 정답 위치 DB 테이블(이체)(정식 + 후보 0→1, 1.5%, §2-1 'DB 테이블(<무엇>)' 꼴), 결제 경로 11/67(16.4%, 결제가 정답이 아님), 은행 20→21(음식배달 20 과 함께 가장 적던 쪽, 쇼핑몰 26). 어느 축도 20% 미만. 원본 사례 Resend 2024-02-21(M22 에 있음, F53-R, F53-P 와 같은 원본). id 는 F53 사례군의 R, P 가 이미 쓰여, 겉 증상(banking 이체와 commerce 정산 실패, Oracle 오류 로그)이 같고 정답이 다른 F35-R 의 H 로 둔다(카탈로그 §1: 엔진, 진입 경로, 영향 범위가 F53-R 과 다름).
- 2026-10-10: F32-P(후보)를 §4-1에 추가. 주 묶음 P(운영 작업의 대상 착오, 정식 + 후보 7→8, 68 중 11.8%), 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F53-R, F53-P, F35-H 처럼 P+L 로 적는다(L 로 세도 6→7, 10.3%): 마이그레이션이 의도한 변경 내용(기능 개발 중 배차 표 바꾸기)은 맞았는데 명령이 가리킨 대상(환경)이 로컬 DB 가 아니라 운영이었다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 인덱스, 잠금, 자원, 질의 비용은 그대로라 F, D, A 가 아니다. 정답 위치 DB 테이블(배차)(정식 + 후보 1→2, 2.9%, F33-R 과 같은 표지만 기전이 인덱스 제거 대 표 소멸로 다름), 결제 경로 11/68(16.2%, 결제가 정답이 아님), 음식배달 20→21(은행 21 과 함께 가장 적던 쪽, 쇼핑몰 26). 어느 축도 20%(13.6) 미만. 원본 사례 Resend 2024-02-21(M22 에 있음). id 는 F53 사례군의 R, P 가 이미 쓰여, 겉 증상(주문 생성이 배달원 용량 확인에서 전량 503)이 같고 원인이 다른 F32-R 사례군의 P 로 둔다(F32-H 는 이미 있음. 카탈로그 §1: 실패가 dispatch 설정이 아니라 DB 표에서 나고 롤아웃이 없으며 전파 일부만 같다). F32-R, F32-H, F33-R, F33-H, F43-P 와 가르는 관측 근거: dispatch 로그의 1146 과 표 이름(평시 0건), DPM 표 수와 인덱스 수 감소, KCM 무변화, MySQL 한가함. catalog 와 장부 대조 결과 다른 변동 없음.
