---
title: 실제 운영 장애 사례와 원인 분포 참고 자료
status: Active
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - incident
  - postmortem
  - reference
summary: 공개된 기업 포스트모템과 학술 실증 연구에서 모은 장애 원인 분포와 기전별 실제 사례. 시나리오 설계의 근거 자료로만 쓰고, 테스트베드 재현 판단은 설계 문서에 둔다.
---

# 실제 운영 장애 사례와 원인 분포 참고 자료

새 장애 시나리오를 고를 때 "현실에서 실제로 일어나는가, 얼마나 흔한가"의 근거로 쓰는
자료 모음이다. **출처가 말한 내용만 적는다.** 이 테스트베드에서 재현할 수 있는지,
무엇을 먼저 만들지 같은 판단은 이 문서에 넣지 않고 각 시나리오 설계 시트에 둔다.

- 조사일: 2026-10-07 (M8 Flagsmith 사례와 M1 Google 2020-12-14 사례, M21 Buildkite 2025-11-10 과 Chargebee 2018-03-02 사례, M7 Harness 2026-01-08 사례, M8 Onfido 2024-10-18 사례, M16 Intercom 2024-11-05 와 Let's Encrypt 2025-07-21 사례, M1 Vapi 2025-01-21 사례, M1 GitHub 2026-03-05 와 2026-06-10 사례는 2026-10-08 추가, M1 Google Cloud 2025-06-12 세부는 2026-10-09 공식 보고에서 보강, M2 Harness 2025-10-28 과 Pipefy 2024-05-22 사례는 2026-10-09 추가, M21 Vapi 2024-10-02 사례는 2026-10-09 추가, M12 Honeycomb 2019-11-06 세부는 2026-10-09 공식 보고에서 보강, M2 GitHub 2025-01-09 사례는 2026-10-09 추가, M2 Octopus Deploy 2025-11-25 사례는 2026-10-09 추가, M15 Harness 2024-09-01 사례는 2026-10-09 추가, M15 Central 1 2025-04-09 사례는 2026-10-09 추가, M2 Cloudflare 2025-09-12 사례는 2026-10-09 추가, M21 GitHub 2026-07-24 사례는 2026-10-09 추가, M8 GitHub 2021-10-08 사례는 2026-10-09 추가, M2 MIT Open Learning 2026 사례는 2026-10-09 추가, M8 Onfido 2025-01-24 와 Piano 2024-03-20 사례는 2026-10-09 추가, M13 mybinder.org 2022 사례는 2026-10-09 추가, M22 AWS S3 2017-02-28 세부는 2026-10-09 공식 보고에서 보강, M8 Flagsmith 2024-01-18 과 Buttondown 2026-06-14 사례는 2026-10-10 추가, M22 Logto 2023-12-17 사례는 2026-10-10 추가, M22 Resend 2024-02-21 사례는 2026-10-10 추가)
- 출처 표기: 공식 = 기업 공식 포스트모템 또는 상태 페이지, 논문 = 학술 논문,
  보도 = 언론 보도, 2차 = 집계 사이트나 요약 글. 보도와 2차 출처는 원문을 확인하지 못했다는 뜻이다.

## 1. 장애 원인 분포 (학술 실증 연구)

| 연구 | 표본 | 원인 분포 (원문 수치) | 출처 |
|---|---|---|---|
| Gunawi 외, "Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages", SoCC 2016 | 서비스 32개의 장애 597건(2009~2015). 원인이 밝혀진 것은 242건 | 원인이 밝혀진 장애 기준: 업그레이드 16%, 네트워크 15%, 버그 15%, 설정 10%, 부하 9%, 서비스 간 의존 8%, 전원 6%, 보안 5%, 사람 실수 4%, 스토리지 4%, 서버 3%, 자연재해 3%, 하드웨어 1%. 복구 직후 재접속이 몰려 다시 장애가 나는 현상(post-outage request storm)도 보고 | [논문 PDF](https://ucare.cs.uchicago.edu/pdf/socc16-cos.pdf) |
| Google, "The Site Reliability Workbook" 부록 C (포스트모템 분석) | 포스트모템 수천 건(2010~2017) | 트리거: 바이너리 배포 37%, 설정 배포 31%, 사용자 행동 변화 9%, 처리 파이프라인 6%, 외부 제공자 변경 5%, 성능 저하 누적 5%, 용량 관리 5%, 하드웨어 2%. 근본 원인: 소프트웨어 41.35%, 개발 프로세스 20.23%, 복잡계 상호작용 16.90% | [sre.google](https://sre.google/workbook/postmortem-analysis/) |
| Ghosh 외, "How to Fight Production Incidents? An Empirical Study on a Large-scale Cloud Service", SoCC 2022 (Microsoft Teams) | 고심각도 장애 152건 | 코드 버그 27%, 배포 오류 20%(그 가운데 인증서 만료나 회전 실수가 55%, 전체의 약 11%), 설정 19%, 인프라와 용량 12%, 의존 서비스 12%, DB와 네트워크 6%, 인증 4%. 약 80%는 코드나 설정 수정 없이 롤백, 페일오버 같은 조치로 완화 | [Microsoft Research](https://www.microsoft.com/en-us/research/?p=881646), [요약(2차)](https://systemsdistributed.substack.com/p/how-to-fight-production-incidents) |
| Liu 외, "What bugs cause production cloud incidents?", HotOS 2019 (Azure) | 버그가 원인인 고심각도 장애 112건(2018년 3~9월). 버그는 전체 장애의 약 40% | 오류 처리 결함 31%(처리 안 된 오류 43%, 응답 없음과 타임아웃 29%, 조용한 데이터 손상 17%), 데이터 형식 불일치 21%, 타이밍 13%, 상수와 설정값 7%. 56%는 패치 없이 완화 | [Microsoft Research](https://www.microsoft.com/en-us/research/?p=610323), [요약(2차)](https://blog.acolyer.org/2019/06/21/what-bugs-cause-cloud-production-incidents/) |
| Huang 외, "Metastable Failures in the Wild", OSDI 2022 | 11개 조직의 메타스테이블 장애 22건 | 트리거의 약 45%가 엔지니어 실수(설정, 코드 배포), 약 35%가 부하 급증. 장애를 유지시키는 효과의 50% 이상이 재시도. 최근 10년 AWS 대형 장애 15건 가운데 최소 4건이 이 유형 | [USENIX](https://www.usenix.org/conference/osdi22/presentation/huang-lexiang) |
| Yuan 외, "Simple Testing Can Prevent Most Critical Failures", OSDI 2014 | 분산 시스템 5종의 사용자 보고 실패 198건 | 치명적 실패의 92%가 치명적이지 않은 오류를 잘못 처리해서 생김 | [USENIX](https://www.usenix.org/conference/osdi14/technical-sessions/presentation/yuan) |

- 위 연구 어디에도 "DB 잠금이나 블로킹 세션"이 별도 상위 원인으로 나오지 않는다.
  Teams 연구의 "DB 중단과 DB 확장 부족"은 전체의 약 3%다.
- Gunawi 수치는 원인이 밝혀진 242건 기준이고 시기가 2009~2015년이다.

## 2. 기전별 실제 사례

기전 단위로 묶었다. "결정적 증거"는 출처가 원인 판단에 쓴 증거를 옮긴 것이다.

### M1. 설정 오배포 (잘못된 값, 크기 한도 초과)

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Google Cloud, 2025-06-12 | 빈 필드가 든 정책 데이터가 전역으로 배포되어 널 포인터로 Service Control이 다운. 해당 코드 경로에 기능 플래그가 없었음. 재시작한 작업들이 무작위 지수 백오프 없이 Spanner로 몰림. 세부(공식 보고): 약 10:45 PDT 정책 변경이 Service Control 이 정책을 읽는 지역 Spanner 테이블에 들어갔고, 쿼터 메타데이터라 몇 초 안에 전역 복제됨. 정책 데이터에 의도치 않은 빈 필드가 있었고, 지역마다 쿼터 검사가 그 값을 읽어 널 포인터 경로를 타 바이너리가 크래시 루프. 그 코드 경로는 5월 29일 기능 추가로 생겼는데 "did not have appropriate error handling nor was it feature flag protected". 외부 API 요청이 503. 2분 안에 분류, 10분 안에 원인 식별, 약 40분 안에 해당 서빙 경로를 끄는 red-button 배포 완료. 보고서의 근본 원인 요약: "an invalid automated quota update to our API management system which was distributed globally". 재발 방지로 fail open, 전역 복제 데이터의 점진 전파와 검증, 기능 플래그 보호 | [공식](https://status.cloud.google.com/incidents/ow5i3PPK96RduMcb1SsW) |
| Cloudflare, 2025-11-18 | DB 권한 변경 뒤 피처 파일이 2배로 커져 200개 한도를 넘었고 프록시가 panic. 5분마다 정상 파일과 불량 파일이 번갈아 생성됨 | [공식](https://blog.cloudflare.com/18-november-2025-outage/) |
| Facebook, 2010-09-23 | 잘못된 설정값 때문에 모든 클라이언트가 캐시를 버리고 DB를 다시 조회하는 되먹임이 생김 | [공식](https://engineering.fb.com/2010/09/23/uncategorized/more-details-on-today-s-outage/) |
| Cloudflare, 2019-07-02 | WAF 규칙의 정규식 하나가 CPU를 100%까지 사용 | [공식](https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019/) |
| Google, 2020-12-14 | 새 쿼터 시스템으로 옮기는 중 남아 있던 옛 시스템이 User ID Service 의 사용량을 0으로 잘못 보고했고, 쿼터 적용 유예 기간이 끝나자 자동 쿼터 관리가 이 서비스의 쿼터를 줄임. 단일 서비스가 0 부하를 보고하는 경우는 기존 안전 검사가 막지 못함. 계정 DB 쿼터가 깎여 Paxos 리더가 쓰기를 못 하고 읽기 대부분이 낡은 데이터가 되어 인증 조회 오류, 인증이 필요한 Google Cloud 와 Workspace API 전반에 5xx. 03:43 용량 경보, 03:46 User ID Service 오류 경보, 04:08 원인 식별, 04:22~04:27 쿼터 적용을 끄는 것으로 완화, 04:33 오류율 정상(영향 약 47분). 재발 방지로 쿼터 관리 자동화가 전역 변경을 빠르게 적용하지 못하게 검토, 잘못된 설정을 더 빨리 잡는 감시 | [공식](https://status.cloud.google.com/incident/zall/20013) |
| Vapi, 2025-01-21 | 상태 페이지 사고 "Updates to DB are failing". 05:03:04 운영 데이터베이스에 커넥션 풀러를 거쳐 붙은 SQL 클라이언트가 의도치 않게 데이터베이스를 읽기 전용으로 만듦. 근본 원인: 읽기 전용 모드로 설정된 직접 SQL 클라이언트 연결이 커넥션 풀러를 통해 그 설정을 모든 세션에 퍼뜨려 갱신, 삽입, 삭제가 막힘("A configuration error caused the production database to switch to read-only mode"). 05:05 쓰기 실패 시작, 05:18 오류가 쌓여 API 중단, 약 05:23 데이터베이스 재시작 시작, 05:25 재시작, 05:33 완전 복구(타임라인에 시간대 표기 없음, 사고 등록 1:20pm UTC, 해결 1:23pm UTC). 쓰기 30분 차단, API 15분 중단. 재발 방지로 원인으로 의심되는 복제 작업 중지, DDL 과 역할 작업의 상세 감사 로그, 운영 데이터베이스 직접 접속 제거. 데이터베이스 종류와 풀러 제품, 읽기 전용이 설정된 정확한 방식은 적혀 있지 않음 | [공식](https://status.vapi.ai/incident/499408) |
| GitHub, 2026-03-05 | 월간 가용성 보고. 16:24~19:30 UTC GitHub Actions 저하(보고 머리글은 16:35 시작, 2시간 55분). 워크플로 실행의 95% 가 5분 안에 시작하지 못했고 평균 지연 30분, 10% 는 인프라 오류로 실패. 복원력을 높이려고 운영에 롤아웃한 Redis 인프라 갱신이 Redis 로드밸런서에 잘못된 설정 변경을 넣었고, 그 설정이 내부 트래픽을 잘못된 호스트로 보냄. 잘못된 호스트가 무엇인지, 어떻게 탐지했는지는 적혀 있지 않음. 로드밸런서 설정을 바로잡아 17:24 UTC 부터 작업이 정상 실행, 나머지 시간은 밀린 작업 큐 처리. 재발 방지로 해당 갱신을 즉시 롤백하고 그 영역 변경 동결, 잘못된 설정 변경이 인프라로 퍼지지 못하게 자동화 개선, 잘못 설정된 로드밸런서 경보, Actions 의 Redis 클라이언트가 짧은 캐시 중단을 견디게 조정 | [공식](https://github.blog/news-insights/company-news/github-availability-report-march-2026/) |
| GitHub, 2026-06-10 | 월간 가용성 보고. 15:05~16:25 UTC REST, GraphQL API 요청의 약 9% 가 잘못된 401 로 실패(간헐적 로그아웃처럼 보임, 영향받은 요청에 약 800ms 지연 추가). 내부 API 인프라로의 memcached 프록시 서비스 롤아웃이 인증 서비스가 잘못된 호스트 설정을 집어 들게 해 인증 조회가 간헐적으로 실패. memcached 서비스가 올바른 호스트를 가리키게 설정을 바꿔 완화. 재발 방지로 인증 시스템을 새 캐시 인프라로 옮기고, 인증 시스템의 일시 오류를 게이트웨이가 잘못된 자격 증명으로 보고하지 않게 고침. 탐지 경로는 적혀 있지 않음 | [공식](https://github.blog/news-insights/company-news/github-availability-report-june-2026/) |

### M2. 버그 있는 버전 배포와 롤아웃, 롤백 부작용

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Stripe, 2019-07-10 | DB 새 버전의 페일오버 결함으로 1차 장애. 롤백한 버전이 최근 설정 변경과 충돌해 2차 장애 | [공식](https://stripe.com/rcas/2019-07-10) |
| Knight Capital, 2012-08-01 | 기능 플래그를 재사용했는데 서버 1대에만 새 코드가 배포되지 않음 | [SEC 문서](https://www.sec.gov/litigation/admin/2013/34-70694.pdf) |
| Harness, 2025-10-28 | Feature Flags 의 `config.ff.harness.io/client/auth` 엔드포인트 일부 요청이 HTTP 502. 시작 1:47am GMT(상태 페이지에 날짜 표기 없음, 해결 공지 2025-10-28 17:30 PDT, 사후 보고 2025-11-10). 일상 배포(routine deployment)가 일부 파드를 필요한 컨테이너 이미지가 캐시되지 않은 새 노드로 다시 스케줄했는데, 그 이미지는 Google Artifact Registry 에서 삭제된 상태라 새 파드가 시작하지 못함. 탐지는 SDK 트래픽을 흉내 내는 합성 검사. 이미 인증된 SDK 는 무영향, 영향 비율은 기재 없음. 이미지를 GAR 에 복원하고 영향받은 파드를 다시 배포해 복구. 재발 방지로 GAR 이미지 보존 정책 검토와 강화, 운영 이미지가 정해진 최소 기간 보존되는지 자동 검증, 이미지 pull 오류 감시와 경보 | [공식](https://status.harness.io/incidents/2jkj4ryrktd8) |
| Pipefy, 2024-05-22 | 2:18~2:33 PM(시간대 표기 없음) Application 전면 중단. connection manager 설정 중 설정 파일에 지정한 이미지가 컨테이너 레지스트리에 없어("the specified image in the configuration file was not present in the container registry") 띄운 파드들이 이미지를 쓸 수 없어 문제를 일으킴. 이전 설정으로 되돌려 복구하고 변경 절차를 고침. 탐지 경로는 기재 없음 | [공식](https://status.pipefy.com/incidents/wkn8llbcxxqb) |
| GitHub, 2025-01-09 | 월간 가용성 보고. 01:26~01:56 UTC 많은 서비스가 광범위하게 중단되어 사용자가 여러 기능에서 서버 오류를 받음. 원인은 배포가 들여온 질의 하나가 주(primary) 데이터베이스 서버를 포화시킨 것("a deployment which introduced a query that saturated a primary database server"). 오류율은 평균 6%, 갱신 요청의 최대 6.85%. 내부 도구와 대시보드로 문제 질의의 출처를 찾아 배포를 되돌려 완화했고, 대응 시작부터 문제 질의를 찾기까지 모두 14분. 재발 방지로 배포 전에 문제 질의를 잡는 도구에 투자. 질의의 내용과 데이터베이스 종류는 적혀 있지 않음 | [공식](https://github.blog/news-insights/company-news/github-availability-report-january-2025/) |
| Octopus Deploy, 2025-11-25 | 상태 페이지 사고 "OctopusID signin intermittent for cloud customers"(사후 보고 게시 2025-12-08). 11-25 12:43 pm(AEST) 인가(authorization) 서비스에 변경을 배포했는데 "This change introduced a bug resulting in database connection leaks." 그 결과 Octopus Cloud 고객의 로그인 요청 일부가 시간 초과("a database connection leak, which caused some sign-in requests to time out"), 증상은 간헐적이었고 일부 고객에게는 약 5분 뒤 저절로 풀린 것처럼 보임. 10:24 pm 고객 보고로 사고 선언(배포부터 탐지까지 9시간 41분), DB 연결 시간 초과까지 추적, 11:42 pm 서비스 재시작으로 DB 연결을 풀어 임시 완화("restarting services to release database connections"), 다음 날 06:55 am 근본 원인을 연결 누수로 확인하고 누수를 없앤 버전을 배포. 테스트가 놓친 이유: "The connection leak only became apparent in high-traffic scenarios, which our current test suite doesn't replicate." | [공식](https://status.octopus.com/incidents/q5mhnrsm7q6j) |
| Cloudflare, 2025-09-12 | 대시보드와 일부 API 가 약 1시간 쓸 수 없거나 일부만 됨(17:57 UTC 시작). 16:32 배포한 대시보드 새 버전에 버그가 있어 `/organizations` 엔드포인트 호출이 훨씬 많아짐("a bug that will trigger many more calls to the /organizations endpoint, including retries in the event of failure"): API 호출을 하는 React useEffect 의 의존성 배열에 렌더마다 새로 만들어지는 객체가 들어가 한 번 렌더에 호출이 여러 번 실행됨. 17:50 Tenant Service API 새 버전 배포, 17:57 새 버전이 배포되는 중 Tenant Service 가 과부하. Tenant Service 는 API 요청 인가 판단의 일부라 그것 없이는 인가를 평가할 수 없고, 인가 평가가 실패하면 API 요청이 5xx 를 반환. 탐지는 자동 경보, API 사용량의 급증을 봤으나 재시도와 새 요청을 구분하기 어려워 대시보드 반복 호출을 찾는 데 시간이 걸림. 18:17 자원을 더해 API 가용성 98% 로 회복(대시보드는 회복 안 됨), 18:58 오류 처리 경로를 지운 Tenant Service 새 버전이 API 에 다시 영향, 19:01 Tenant Service 로 가는 트래픽에 임시 레이트 리밋, 19:12 문제 변경을 되돌려 대시보드 100% 회복, 이어 대시보드 핫픽스. 사후 보고는 직접 계기를 대시보드 버그로, Tenant Service 에 이런 부하 급증을 감당할 용량이 배정되지 않은 것을 함께 듦. 재발 방지로 대시보드 재시도에 무작위 지연, 용량과 경보 보강, API 요청에 재시도 여부 표시, Tenant Service 를 Argo Rollouts 로 옮겨 자동 롤백 | [공식](https://blog.cloudflare.com/deep-dive-into-cloudflares-sept-12-dashboard-and-api-outage/) |
| MIT Open Learning(learn.mit.edu), 2026-03-24 | 팀 공개 사후 보고 "20260324 MIT Learn Outage Post-Mortem"(문서 제목은 20260324, 사이트 목차에는 20260423 으로 표기). 막 롤아웃된 정규 릴리스의 질의 패턴 변경(PR #3091)이 원인: "A set of query pattern changes introduced in a deployed release caused DRF and Postgres to fill up storage and temp space and thrash causing a traffic jam inside the Postgres DB server". 작성자 설명으로는 DRF 가 페이지 응답의 count 칸을 "SELECT COUNT * ($QUERY)" 로 계산하는데, 주 질의 안에 집계를 넣은 변경이 "fine on datasets with smaller ordinality but not in prod". 느린 학습 자료 API(특히 featured resources)를 빠르게 하려던 변경이었고 시니어 엔지니어 두 명이 검토했으나 로컬과 RC 데이터 규모가 운영과 달라 드러나지 않음. 18:35 UTC 시작, 운영 DB 저장 공간을 모두 소모, Postgres 임시 파일시스템 thrash, Sentry 에 504 다수, 2:54 PM 보고, 3:17 PM 작성자가 원인으로 보고 되돌림 착수, 이후 읽기 복제본 삭제와 복제 슬롯 제거, AWS 지원과 함께 10분 넘은 활성 질의를 pg_terminate_backend 로 끊어 회복(타임라인 시간대 표기 혼재). 보고서 기준 중단 "3 hours and 35 seconds". 재발 방지로 DiskQueueDepth 경보, API 응답 시간 경보, 운영 규모 익명화 데이터로 부하 시험, temp_file_limit 과 log_temp_files 설정 권고, 집계는 미리 계산한 열로 바꿈(해당 API 응답 13~15초 → 150~200ms) | [공식](https://engineering.ol.mit.edu/runbooks_post_mortems/20260324_mitlearn_outage) |

### M3. 재시도 폭풍, 몰림(thundering herd)

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| AWS us-east-1, 2021-12-07 | 자동 확장 활동이 클라이언트 연결 폭주와 재시도를 일으켜 내부 네트워크 장비가 포화 | [공식](https://aws.amazon.com/message/12721/) |
| Google Cloud, 2025-06-12 | M1 사례. 재시작 작업들이 백오프 없이 몰림 | [공식](https://status.cloud.google.com/incidents/ow5i3PPK96RduMcb1SsW) |
| Slack, 2022-02-22 | M6 사례. 비싼 쿼리와 재시도가 겹쳐 DB 과부하 | [공식](https://slack.engineering/slacks-incident-on-2-22-22/) |

### M4. 타임아웃 불일치와 누락, 지연 전파, 스레드나 커넥션 풀 고갈

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| AWS DynamoDB, 2015-09-20 | 멤버십 데이터가 커지며 처리 시간이 타임아웃에 근접. 짧은 네트워크 단절 뒤 동시 재요청과 재시도로 장애 지속 | [공식](https://aws.amazon.com/message/5467D2/) |
| Google SRE 책 22장 | 스레드 고갈이 헬스체크 실패로 번지는 연쇄 장애 설명 | [sre.google](https://sre.google/sre-book/addressing-cascading-failures/) |
| PostHog, 2025-10-21 | 배포 뒤 과도한 병렬 처리로 커넥션 풀 고갈 | [공식](https://posthog.com/handbook/company/post-mortems/2025-10-21-feature-flags-recurring-outages.md) |

### M5. OS나 런타임 고정 한도 도달

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| AWS Kinesis, 2020-11-25 | 용량 증설 뒤 모든 서버의 스레드 수가 OS 설정 최대값을 넘음 | [공식](https://aws.amazon.com/message/11201/) |
| GitHub, 2026-05-06 | Vitess 조회 테이블의 32비트 키가 최대값에 도달 | [공식](https://github.blog/news-insights/company-news/github-availability-report-may-2026/) |
| Cloudflare, 2025-11-18 | M1 사례의 200개 한도 | [공식](https://blog.cloudflare.com/18-november-2025-outage/) |

### M6. 캐시 장애나 만료가 DB 폭주로 이어짐

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Slack, 2022-02-22 | Consul 업그레이드로 캐시 노드가 교체되어 적중률 급락. 모든 샤드를 훑는 비싼 쿼리와 재시도가 겹쳐 DB 과부하 | [공식](https://slack.engineering/slacks-incident-on-2-22-22/) |
| Slack, 2021-01-04 | 연휴 뒤 빈 캐시 상태로 클라이언트가 몰려 네트워크(TGW) 포화 | [공식](https://slack.engineering/slacks-outage-on-january-4th-2021/) |
| Facebook, 2010-09-23 | M1 사례 | [공식](https://engineering.fb.com/2010/09/23/uncategorized/more-details-on-today-s-outage/) |

### M7. 인증서 만료, 서명 키 회전 실수

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Azure Storage, 2013-02-22 | HTTPS 인증서 만료 | [보도](https://www.datacenterknowledge.com/archives/2013/02/25/windows-azure-cloud-crashed-by-expired-ssl-certificate) |
| Azure AD, 2021-03-15 | 자동화가 보존 표시를 무시하고 서명 키를 삭제 | [공식 RCA 사본](https://s3.documentcloud.org/documents/20515443/authentication-errors-across-multiple-microsoft-services-tracking-id-ln01-p8z.pdf) |
| Microsoft Teams, 2020-02-03 | 인증 인증서 만료 | [보도](https://geekwire.com/2020/microsofts-slack-competitor-teams-due-expired-authentication-certificate) |
| Harness, 2026-01-08 | 10:25~10:47 UTC. 예정된 비밀(secret) 회전 중 DB 사용자 자격 증명 하나가 회전 과정에서 잘못되어("one database user credential errored out during rotation") Template Service 가 DB 인증을 잃고 인증 실패를 냄. 고객이 Harness UI 에서 템플릿을 불러오지 못함, 실행 중인 파이프라인은 무영향. 옛 DB 사용자를 다시 활성화해("Re-enabled the old database user") 인증과 기능을 복구, 탐지 뒤 2분 안에 완전 복구. 재발 방지로 옛 사용자를 비활성화하기 전에 새 사용자가 GCP Secret Manager 에서 활성인지 확인하는 단계를 더하고 회전 절차의 자격 증명 갱신 틈을 막음 | [공식](https://status.harness.io/incidents/5650h9byz6l5) |

### M8. 데이터 형식 불일치 (버전 비호환)

- Liu 2019: Azure 버그 장애의 21%. Ghosh 2022: 버그 가운데 하위 호환성 문제 14.6%.

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Flagsmith, 2026-03-03 | 17:00 UTC Edge API 배포가 정수형 기능 값의 JSON 형식을 문자열(`"300"`)에서 숫자(`300`)로 바꿈. 로컬 평가 모드 SDK가 쓰는 `/api/v1/environment-document/` 응답이 대상이고, 주로 정적 타입 언어 클라이언트가 영향받음. Core API는 무영향. 기존 테스트가 형식 변화를 잡지 못함. 18:12 사용자 보고, 18:27 P0 분류, 18:36 이전 안정 릴리스로 롤백해 복구. 재발 방지로 Core와 Edge 응답 형식 비교 통합 테스트와 직렬화 변경 검토 강화 | [공식](https://status.flagsmith.com/incidents/q3h5kr2z2sgb) |
| Onfido, 2024-10-18 | 08:18 릴리스 중 Studio 테이블 하나에 데이터베이스 스키마 마이그레이션이 배포됐는데, 갱신된 코드가 아직 모든 인스턴스에 퍼지지 않은 상태였음. 마이그레이션이 데이터베이스와 애플리케이션 쪽 ORM 모델 사이에 일시적 불일치를 만들어 옛 코드를 돌던 인스턴스가 더는 없는 열에 접근하려 함. 여러 Studio 엔드포인트에서 5xx, 15분 구간 트래픽의 약 23%(하루 전체 0.52%), 워크플로 실행이 오류로 끝남(15분 구간 취소율 31.8%). 08:20 자동 알람, 08:31 원인 식별, 08:33 해결(조치 방법은 기재 없음, 시간대 표기 없음). 재발 방지로 두 단계로 나눠야 하는 스키마 변경을 자동으로 찾아 강제하도록 마이그레이션 검토와 출시 절차를 고침. 사후 보고 게시는 2024-11-14 | [공식](https://status.onfido.com/incidents/fvl7r5k973p2) |
| GitHub, 2021-10-08 | 월간 가용성 보고(2021년 10월). 17:16 UTC 부터 1시간 36분. 공개 API 출시 과정에서 Codespaces 의 핵심 API 응답 하나의 구조가 의도치 않게 바뀌었고("inadvertently restructured"), 안정된 스키마에 기대던 기존 API 클라이언트가 깨졌다. VS Code 데스크톱 클라이언트에서 새 Codespace 를 시작할 수 없었고, 웹 편집기와 기존 데스크톱 세션은 저하되어 확장이 오류를 띄우고 Remote Explorer 에 Codespace 정보가 빠졌다. 모니터링이 처음에는 영향을 잡지 못했고, 사고 중 시작된 무관한 배포가 되돌림을 늦췄다. 회귀를 되돌려 모든 클라이언트가 다시 연결됐다. 재발 방지로 확장의 API 사용에 대한 종단 간 시험 도구와 내부 서비스 경계의 모니터링 확대 | [공식](https://github.blog/2021-11-03-github-availability-report-october-2021/) |
| Onfido, 2025-01-24 | 상태 페이지 사고 "Device Intelligence Report Failure to run"(사후 보고 게시 2025-01-31). 14:48~15:26 UTC. 원인은 Device Intelligence 보고서 계산 갱신 중 들여온 타임스탬프 필드의 하위 비호환 변경("backwards-incompatible change to a timestamp field"). Device Intelligence 보고서가 철회되고 함께 돌던 보고서도 영향을 받아, 이 보고서가 든 Studio 워크플로가 Error 상태로 감(실행 중이던 Studio 워크플로의 약 12%, Check 의 약 9%). 14:59 내부 경보가 떴으나 우선순위가 비긴급으로 잘못 설정돼 있었고(일부 보고서만 영향), 15:19 담당 팀에 알려 15:25 되돌림 시작, 15:27 해결. 후속: 경보 우선순위 조정과 배포 중 경보 민감도 상향, 배포 모니터링 세분화, 시스템 전반의 엄격한 타임스탬프 처리 표준 | [공식](https://status.onfido.com/incidents/r2pc6vdpjsm2) |
| Piano(Cxense API), 2024-03-20 | 공개 RCA "2024-03-20 RCA Cx APIs Failed Requests". 2024-03-20 13:20 UTC 에 Java 날짜, 시각 라이브러리 갱신을 API 서버와 Elastic 관련 서비스에 배포. 그 뒤 start, stop 필드나 cx 인증 헤더에 "+0000" 꼴 시간대 표기를 쓰던 api.cxense.com 요청이 실패(심각도 중간, 약 15시간). 자동 시험 어느 것도 그 표기로 API 를 부르지 않아 시험이 모두 통과했고 변경이 운영에 나감. 03-21 01:34 UTC 고객 문의로 알게 되어 04:18 UTC 변경을 되돌림. 후속: 고객이 실제로 쓰는 모든 날짜, 시각 표기를 시험에 더하고, API 실패율 상승 경보를 더함 | [공식 RCA(PDF)](https://docs.piano.io/wp-content/uploads/2024/04/2024-03-20-RCA-Cx-APIs-Failed-Requests.pdf) |
| Flagsmith, 2024-01-18 | 상태 페이지 사고 "Increased error rates on the Edge API"와 같은 날 사후 보고. 그날 앞선 릴리스가 숫자 identity 식별자를 준 요청에만 해당하는 검증을 더했고, 그 검증 문제를 고치려고 약 13:45(사후 보고 시각, 시간대 표기 없음) 배포한 변경이 traits 키가 있어야 한다고 요구함. 일부 클라이언트는 traits 가 비면 그 키를 빼고 보내는데(사후 보고는 빈 traits 목록을 빼는 Go 클라이언트를 듦) 그 때문에 traits 가 없는 identity 의 유효한 요청이 잘못 거절됨. 감시 경보와 영향받은 고객이 알림. 14:54 일부 경우만 고친 부분 수정, 15:06 영향 지역 롤백, 15:48 시험을 더한 영구 수정 배포. 상태 갱신 시각(UTC) 14:40 조사 시작, 15:50 해결. traits 데이터 유실 없음. 재발 방지로 더 세분된 오류율로 자동 롤백, SDK 마다 새 변경과의 호환을 확인하는 종단 시험 | [공식](https://status.flagsmith.com/incidents/0t2jlh80q0zt) |
| Buttondown, 2026-06-14 | 공식 블로그 사후 보고 "Public postmortem: manually adding subscribers failed". 6월 13일(UTC) 기능은 바꾸지 않고 계약을 엄격하게 하려고 선언되지 않은 필드가 든 API 요청을 거절하는 스키마 강화 변경을 내보냄. 자사 화면의 수동 구독자 추가 양식이 클라이언트 전용 필드 active 를 함께 보내고 있어 그 요청이 422 "Failed to create this subscriber: extra inputs are not permitted" 로 실패. 6월 14일 약 03:17 UTC 부터 6월 15일 약 14:30 UTC 까지(약 35시간) 작성자의 구독자 추가 시도 454건 실패, 가져오기와 앱의 나머지, 자연 구독은 무영향. 시험의 mock 이 실제 스키마가 거절하는 active 필드를 받아들여 코드 리뷰와 CI 가 놓침. 고객 지원 문의로 드러남(422 로깅이 너무 거칠어 적은 양의 경로를 못 잡음). 6월 15일 14:04 UTC 명시 필드 목록으로 요청을 만드는 수정 배포. 재발 방지로 경로, 필드별 자사 422 경보, 엄격한 mock 으로 화면 시험, 동작을 바꾸는 API 변경의 엔드포인트별 점진 배포 | [공식](https://buttondown.com/blog/incident-0029) |

### M9. 독이 든 메시지(poison message), query of death

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Google SRE 책 22장 | 특정 요청이 서버를 연달아 죽이는 "query of death" 설명 | [sre.google](https://sre.google/sre-book/addressing-cascading-failures/) |
| Cloudflare, 2023-01-24 | 오프셋이 전진하지 않는 Kafka 컨슈머를 감지해 재시작하는 설계 소개 | [공식](https://blog.cloudflare.com/intelligent-automatic-restarts-for-unhealthy-kafka-consumers/) |
| PostHog, 2026-07-23 | 독성 메시지 하나가 파티션 하나를 막음 | [2차](https://isdown.app/status/posthog-us/incidents/627216-realtime-destinations-delivery-delays) |

### M10. 컨슈머 리밸런싱 폭풍, 큐 적체

- 공신력 있는 공식 포스트모템은 찾지 못했다. 업체 사례 글 수준:
  [VGS](https://www.verygoodsecurity.com/blog/posts/solving-kafka-rebalancing-issues-a-case-study)

### M11. 헬스체크 오설정으로 인한 재시작 루프, 용량 제거

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Google SRE 책 22장 | 과부하 상태에서는 헬스체크 자체가 서비스를 망가뜨림 | [sre.google](https://sre.google/sre-book/addressing-cascading-failures/) |
| AWS, 2025-10-20 | NLB 헬스체크 실패로 정상 용량까지 제거 | [공식](https://aws.amazon.com/message/101925/) |
| PostHog, 2025-10-28 | CPU 압박 속에서 새 파드가 20초 안에 DB 풀을 초기화하지 못해 크래시 루프 | [공식](https://posthog.com/handbook/company/post-mortems/2025-10-21-feature-flags-recurring-outages.md) |

### M12. 메모리 누수와 GC

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Honeycomb, 2019-11-06 | 느린 누수가 모든 백엔드에서 같은 속도로 진행되어 수 분 간격으로 전부 죽음. 처음에는 ALB 문제로 오판. 세부(공식 보고): 수집(ingest) 워커의 메모리 누수로 약 20분짜리 오류 구간이 네 번 생겨 고객 텔레메트리의 1~3% 가 간헐적으로 거절됨. 누수는 "a slow memory leak that manifested over hours, which leaked at the same rate on each ingest backend" 라 백엔드들이 몇 분 차이로 함께 죽고 진행 중 요청이 실패, 새 요청은 건강한 백엔드를 찾지 못함. ALB 로그에는 'backend unreachable', 'backend timed out while processing'. SLO 소진 경보가 몇 분 안에 울렸지만 호출 경보가 아니었고, 최근 배포가 없었다는 이유로 ALB 를 의심(AWS 지원 요청까지 함). 다른 엔지니어가 재시작과 메모리 누수를 찾아 잘못된 커밋을 되돌리고 고친 릴리스를 배포해 메모리가 일정해지고 크래시가 멈춤. 재발 방지로 프로세스 크래시(panic, OOM) 비율을 진단 신호로 쓰고, 사용자 SLO 소진 경보를 호출 경보로 올림 | [공식](https://www.honeycomb.io/blog/incident-report-running-dry-on-memory-without-noticing) |
| Twitter | GC가 증폭 기전이 된 사례 (Huang 2022 §4) | [USENIX](https://www.usenix.org/conference/osdi22/presentation/huang-lexiang) |

### M13. 리소스 limit 오설정, 노드 과밀

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| PostHog, 2025-10 | 노드 대비 요청량이 커서 파드가 과밀 배치, CPU 90% 초과에도 경보 없음 | [공식](https://posthog.com/handbook/company/post-mortems/2025-10-21-feature-flags-recurring-outages.md) |
| Omio | CPU 스로틀링 | [k8s.af 목록(2차)](https://k8s.af/) |
| mybinder.org, 2022(보고 제목은 2022-01-27, 문서 파일 이름과 시간표 머리는 2022-06-02) | 운영팀 공개 사고 보고 "pod limit reached". 10:00(CET) 부터 prod hub 가 새 파드를 만들지 못해 mybinder.org 가 새 세션을 띄우지 못함. 로그의 오류: "exceeded quota: gke-resource-quotas, requested: pods=1, used: pods=15k, limited: pods=15k"(실제로는 15k 파드 근처도 쓰지 않았음). 보고는 원인을 GKE 의 리소스 할당량(gke-resource-quotas) 객체 버그로 적음. 21:00 사용자 제보로 알았고(약 11시간 동안 큰 장애인 줄 몰랐다고 적음), 21:06 로그에서 할당량 오류를 찾고, 21:24 그 resourcequota 객체를 지우자 파드 생성이 바로 돌아와 실행 성공률이 거의 100% 로 회복. 후속 조치는 가동 감시와 경보 개선. 요약 절은 "약 9시간 뒤 개입 없이 정상 운영"이라고도 적어 시간표와 어긋남 | [공식(운영팀 SRE 문서)](https://mybinder-sre.readthedocs.io/en/latest/incident-reports/2022-06-02-pod-limit.html) |

### M14. 외부 의존 서비스 장애

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Cloudflare, 2025-06-12 | Workers KV가 의존하는 외부 스토리지 장애로 KV 요청의 90.22% 실패 | [공식](https://blog.cloudflare.com/cloudflare-service-outage-june-12-2025/) |

### M15. 네트워크 손실, 노드 네트워크 단절, 파티션

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Datadog, 2023-03-08 | systemd 자동 보안 업데이트가 Cilium 경로를 지워 여러 리전에서 노드가 동시에 이탈 | [공식](https://www.datadoghq.com/blog/2023-03-08-multiregion-infrastructure-connectivity-issue/) |
| GitHub, 2018-10-21 | 43초 네트워크 단절 뒤 리전 간 DB 페일오버 | [공식](https://github.blog/2018-10-30-oct21-post-incident-analysis/) |
| Harness, 2024-09-01 | 상태 페이지 사고 "Harness cloud builds failing at initialise step for MAC users"(사후 보고 게시 2024-09-17). 9월 1일 17:00 UTC 방화벽 규칙을 좁힘: "We tightened a firewall rule for our Mac VM registry that was previously too permissive." 새 규칙이 레지스트리를 쓰는 구성 요소 하나의 NAT IP 를 빠뜨림("the new rule did not account for the NAT IP address of one of these components"). 그 구성 요소는 지속 소켓 연결을 유지해 연결이 다시 맺어지거나 재시작할 때까지 방화벽의 영향을 받지 않아 문제가 바로 드러나지 않음. 영향: 일부 고객의 macOS 호스팅 CI 파이프라인이 초기화 단계에서 실패. 탐지: 9월 4일 06:03 UTC 고객 보고. 완화: 9월 4일 08:39 UTC 방화벽 규칙을 다시 만들고 검증. 후속: 필요한 NAT IP 를 넣어 다시 좁히기, 방화벽 제한을 적용할 때 관련 서비스 재시작, 변경 시 연결을 비우고 다시 맺게 하기 | [공식](https://status.harness.io/incidents/bs6qp18g8l21) |
| Central 1, 2025-04-09 | 상태 페이지 사고 INC198965 "Brief Interac e-Transfer outage"(사후 보고 게시 2025-05-06). 4월 9일 12:50~12:54 PT(4분 미만) e-Transfer 서비스 중단: MemberDirect/Forge 디지털 뱅킹(3.4/3.5)과 API 서비스 사용자가 이체를 보내거나 받을 때 오류. 영향 범위 Payment Services, Digital Banking Services. 원인: 방화벽 오류("firewall error")가 UCP 시스템과 Interac 사이 연결을 잠깐 끊음. UCP 는 밴쿠버(VAHC) 망 연결로 Interac 과 통신하지 못했지만 응답은 유지했고, Interac 쪽에는 문제가 없었음. 네트워크 팀이 방화벽 오류로 추적. 완화: 방화벽 규칙을 바로잡았고 서비스는 개입 없이 자동 회복. 후속: 방화벽 규칙 변경에 대한 네트워크 거버넌스와 안전장치 강화(PTASK0010325 완료), 네트워크 계층 로깅 강화. 탐지 경로는 적혀 있지 않음 | [공식](https://status.central1.com/incidents/nr0l5bvnmwpn) |

### M16. DNS

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| AWS, 2025-10-19~20 | DynamoDB DNS 자동화의 경쟁 조건으로 빈 레코드 생성 | [공식](https://aws.amazon.com/message/101925/) |
| OpenAI, 2024-12-11 | 텔레메트리 배포가 K8s API 서버를 과부하시켰고 DNS가 그 API 서버에 의존. DNS 캐시 때문에 증상이 20분 늦게 드러남 | [공식](https://status.openai.com/incidents/ctrsv3lwd797) |
| Meta, 2021-10-04 | BGP 경로 철회 | [공식](https://engineering.fb.com/2021/10/05/networking-traffic/outage-details/) |
| Intercom(현 Fin), 2024-11-05 | 03:30~06:00 UTC(약 2.5시간) 미국 호스팅 지역. 앞선 장애의 후속 조치이자 가끔 생기던 DNS 지연을 고치려고 Linux 서버 이미지에 캐싱 resolver(unbound)를 더함. 이 이미지는 이미 많은 애플리케이션에 문제없이 배포된 상태였음. 미국 애플리케이션은 MySQL 앞의 ProxySQL 클러스터를 사설 DNS 영역으로 찾는데, unbound 는 호스트가 인터넷 DNS 에 닿을 수 있으면 사설 레코드를 해석하지 못함("the private DNS records were not resolvable"). 인터넷 DNS 질의가 막힌 스테이징과 사전 운영에서는 드러나지 않음. 서버 이미지는 자동으로 다시 빌드되고 애플리케이션 서버가 매주 교체되는데, 03:27 운영에 닿은 뒤 교체된 백그라운드 워커가 ProxySQL 엔드포인트를 해석하지 못해 Rails 프로세스를 띄우지 못하고 작업이 쌓임. 웹 서버는 교체 호스트가 health check 를 통과해야 넘겨받으므로 영향 없음. 03:44 경보, 04:48 이미지 롤아웃을 원인으로 식별하고 롤백 시작, 롤백이 배포 잠금에 걸려 05:46 재시도, 06:00 정상. 데이터 유실 없음. 재발 방지로 이미지 롤아웃 강화와 사설 DNS 레코드 사용 제거 | [공식](https://www.intercomstatus.com/us-hosting/incidents/01JBX9TND1N4J6PM95X6X5ZP0K/write-up) (현재 같은 경로의 [finstatus.com](https://www.finstatus.com/us-hosting/incidents/01JBX9TND1N4J6PM95X6X5ZP0K/write-up) 으로 넘겨짐) |
| Let's Encrypt, 2025-07-21 | 18:35 UTC~07-22 02:22 UTC 모든 데이터센터에서 ACME API 전면 중단. 내부 DNS resolver 클러스터 4대 중 마지막 한 대의 OS 업그레이드 뒤, 서비스 설정 스크립트의 플래그가 필요 없는 내부 전달자(forwarder)를 자동 설정해 클러스터의 다른 복제본을 전달 대상으로 고름. 복제본을 하나씩 올리는 동안 전달 대상이 사라진 복제본이기도 해서 마지막 업그레이드 뒤 모든 머신의 resolver 설정이 잘못됨. 질의는 막다른 곳이면 SERVFAIL, 무한 순환이면 시간 초과. DB 서버, MPIC 서버, 외부 CT 로그 이름 해석이 깨짐. 임시로 Boulder 워커의 /etc/hosts 에 DB 서버 주소를 적어 발급을 일부 되살리고, 잘못된 resolver 설정을 서버마다 지워 복구. 재발 방지로 데이터센터, 환경별 독립 DNS 클러스터와 소스 관리되는 설정 | [공식(커뮤니티 사고 보고)](https://community.letsencrypt.org/t/2025-07-21-complete-api-outage/240985) |

### M17. 시간 어긋남, 윤초

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Cloudflare, 2017-01-01 | 윤초로 시간 차가 음수가 되어 RRDNS가 panic | [공식](https://blog.cloudflare.com/how-and-why-the-leap-second-affected-cloudflare-dns/) |

### M18. 디스크 가득, 로그 폭주

- Spotify, 2013-04: 장애 경로의 과도한 로깅이 장애를 유지시킴 (Huang 2022 표 1).
- 대형 공식 사례는 드물고 소규모 업체 사례가 대부분이다.

### M19. 레이트 리밋 오설정

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| PostHog, 2025-10-24 | 리미터가 모든 트래픽을 로드밸런서 IP 하나에서 온 것으로 보고 요청의 97%를 429로 차단 | [공식](https://posthog.com/handbook/company/post-mortems/2025-10-21-feature-flags-recurring-outages.md) |

- 함께 볼 사례: M1 의 Google 2020-12-14. 리미터가 아니라 쿼터(한도) 값이 실제 사용량 아래로 줄어 정상 요청이 막혔다는 점에서 "한도 설정이 정상 요청을 차단"한 같은 계열이다.

### M20. 배치 잡과 온라인 트래픽 충돌

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Slack, 2025-11-01 | DB 잡이 DB CPU를 과다 사용 | [공식](https://slack-status.com/2025-11-01) |
| Slack, 2023-08-02 | 클러스터 이전으로 용량이 줄어든 상태에서 예약 잡이 몰림 | [공식](https://slack-status.com/2023-08/8ec13e4962a9bf43) |

### M21. 온라인 스키마 마이그레이션

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| GitHub, 2026-05-04 | 마이그레이션과 피크 트래픽이 겹쳐 DB 연결 용량 포화, 타임아웃 연쇄 | [공식](https://github.blog/news-insights/company-news/github-availability-report-may-2026/) |
| Buildkite, 2025-11-10 | 04:48 UTC DB 마이그레이션이 Pipelines annotations 테이블의 인덱스를 제거함. 대체 인덱스가 있었지만 고빈도 질의 일부가 그 인덱스를 타지 않았고, annotation 질의가 타임아웃되며 해당 DB들의 CPU 부하가 크게 늘어 Agent API, REST API, GraphQL 이 저하됨. 04:53 annotation 질의 타임아웃으로 탐지. 인덱스를 다시 만들어 복구했는데, 진행 중인 annotation 질의가 남은 샤드의 인덱스 생성을 막아 앱 계정의 테이블 권한을 잠시 회수하고 생성을 마침. 08:43 지연과 오류율 정상, 12:00 종료. 사후 보고가 꼽은 주원인은 인덱스를 제거한 마이그레이션과 검토 과정의 빈틈이고, 재발 방지로 인덱스 제거를 위험 마이그레이션 검사 대상에 넣어 미사용 확인을 요구, 위험 마이그레이션의 단계적 적용 | [공식](https://www.buildkitestatus.com/incidents/sv6phcn6xwwg) |
| Chargebee, 2018-03-02 | 배포 중 폐기 예정 열의 유일 제약을 제거했는데 그 열이 테이블의 인덱스 역할도 하고 있었음. 제거 직후 DB CPU 100%, 요청이 쌓여 타임아웃. 되돌리려면 인덱스 재생성에 메타 잠금이 필요했으나 실행 중인 질의 때문에 얻지 못해, 전체 서비스를 내린 뒤 인덱스를 다시 추가. 첫 오류부터 정상까지 19분(그중 전체 다운 9분). 원인은 인덱스 제거가 질의에 주는 영향을 재지 않은 것 | [공식](https://status.chargebee.com/incidents/pffpjnwyr92p) |
| Vapi, 2024-10-02 | 상태 페이지 시각(UTC) 16:15 API 성능 저하와 통화 타임아웃 보고, 16:38 DB CPU 급증에 대응해 DB 자원 확장(약 2분 완전 중단), 16:41 확장 뒤에도 CPU 최대, 16:59 병목 확인과 회복 시작, 17:00 API 복구(분석 기능 제외), 19:00 해결. 사후 보고: 늘어나는 부하로 DB CPU 가 오르자 `call` 테이블에 복합 인덱스를 더해 질의 성능을 높이려 했는데, 인덱스 생성이 UI 에서는 성공한 것처럼 보였지만 실제로는 실패해 `INVALID` 로 남았고, 이어 옛 단순 인덱스를 지워 가장 큰 테이블에 쓸 수 있는 인덱스가 없어짐("Human error on our end led us to being index-less on our biggest table `call`s"). DB CPU 100%, API 요청 타임아웃, 쿠버네티스가 건강하지 않은 파드를 재시작해 부하가 더 나빠짐. 일부 집계 질의를 끄는 것으로는 풀리지 않았고, 인덱스가 `INVALID` 임을 찾아 다시 만들어 복구. 재발 방지로 피크 시간 위험한 마이그레이션 금지, 변경 영향과 롤백 평가 절차 보강 | [공식](https://status.vapi.ai/incident/438296) |
| GitHub, 2026-07-24 | 월간 가용성 보고. 19:17~20:02 UTC(57분) 사용자가 웹, CLI, API 로 풀 리퀘스트를 만들지 못함(시도 113,930회, 사용자 50,904명, 평균 오류율 1.75%, 최대 2.25%). 기존 풀 리퀘스트와 다른 기능은 무영향이었고 PR 을 만드는 워크플로도 실패. 근본 원인은 풀 리퀘스트 데이터를 담은 Vitess keyspace 로의 백필 워크플로("The root cause was related to a backfill workflow into the Vitess keyspace hosting pull request data."): 워크플로를 취소하자 잘못 이해된 Vitess 코드 경로가 기반 표를 지워("The cancellation executed a misunderstood Vitess codepath that dropped the backing table to the target keyspace") 낡은 vschema 참조가 남음. 데이터베이스 변경을 되돌리자 PR 생성이 바로 재개됐고, 낡은 vschema 참조를 지워 마무리. 재발 방지로 인덱스 백필 운영 지침을 더하고 예상 밖 취소 동작을 문서화했으며, 사전 검증 강화, 더 안전한 내장 코드 경로, 하위 환경의 종단 시험을 계획. 탐지 경로는 적혀 있지 않음 | [공식](https://github.blog/news-insights/company-news/github-availability-report-july-2026/) |

### M22. 운영 명령이나 스크립트 실수, 조용한 데이터 오류

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Atlassian, 2022-04-05 | 앱 ID 대신 사이트 ID를 넘겨 883개 사이트 삭제 | [공식](https://www.atlassian.com/engineering/post-incident-review-april-2022-outage) |
| AWS S3, 2017-02-28 | 명령 오타로 서버를 너무 많이 제거. 세부(공식 보고): 9:37AM PST 권한 있는 S3 팀원이 정해진 플레이북으로 S3 과금 처리에 쓰이는 하위 시스템의 서버 몇 대를 빼려는 명령을 실행했는데, 명령의 입력 하나가 잘못 들어가 의도보다 많은 서버가 빠짐. 잘못 빠진 서버는 다른 두 하위 시스템(객체 메타데이터와 위치를 관리하는 index, 새 객체 저장소를 배정하는 placement)을 받치고 있었고, 큰 용량을 잃은 두 하위 시스템은 전체 재시작이 필요했음. 12:26PM index 가 GET, LIST, DELETE 를 다시 처리할 만큼 회복, 1:18PM index, 1:54PM placement 회복. 후속: 용량 제거 도구가 더 천천히 빼고, 어떤 하위 시스템이든 최소 필요 용량 아래로 내려가게 하는 제거는 막도록 고침 | [공식](https://aws.amazon.com/message/41926/) |
| Logto, 2023-12-17 | 공식 블로그 사후 보고 "Postmortem: Docker image not found". Logto cloud 와 core 서비스가 약 18분 중단(시간표는 UTC 03:56 사용 불가와 감시 탐지, 04:03 당직 확인, 04:10 최신 이미지로 두 서비스 새 배포, 04:15 사용 가능). 원인은 자동 GitHub 이미지 보존(retention) 작업이 운영 Docker 이미지 `logto`, `logto-cloud` 를 잘못 지운 것("The automated GitHub image retention workflow deleted the production images by mistake"). 작업의 의도는 "3일 지난 태그 없는 옛 이미지" 삭제였고, 운영 배포마다 새 이미지에 `prod` 태그를 옮겨 옛 이미지가 태그 없이 남는 방식이었다. 이미지를 buildx 로 다중 아키텍처로 빌드해 `prod` 를 포함한 태그는 매니페스트 목록에만 있고 아키텍처별 하위 이미지에는 태그가 없어, 작업이 그 하위 이미지를 지워 매니페스트 목록이 깨졌다. 그 결과 클라우드 서비스가 GitHub Container Registry 에서 이미지를 가져오지 못해("failed to fetch the image") 사용할 수 없게 됨. 보고는 그때 무엇이 이미지를 다시 가져오게 했는지(재시작, 확장, 배포)는 적지 않는다. 완화: 보존 작업을 멈추고 `prod` 태그의 새 이미지를 배포해 두 서비스가 이미지를 받아 회복. 기여 요인: 작업을 검토, 시험 없이 운영에 냄, 삭제 규칙을 정하기 전에 이미지의 태그와 매니페스트 목록 구조를 확인하지 않음. 교훈: 삭제 전 dry-run, 보존 정책을 신중히 정의 | [공식](https://blog.logto.io/postmortem-docker-image-not-found) |
| Resend, 2024-02-21 | 공식 블로그 사고 보고 "Incident report for February 21, 2024". 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션을 돌렸는데 그 명령이 운영 환경을 가리켜 운영의 모든 표를 지움("incorrectly pointed to the production environment instead, which dropped all tables in production"). 시간표(UTC): 04:50:00~04:56:27 데이터베이스 오프라인(5분 데이터 손실 구간), 04:56 마이그레이션 시작, 04:57 운영에서 표가 지워지는 것을 알아챔, 05:01 백업 복원 시작, 05:02 상태 페이지 게시, 11:02 첫 복원 완료(약 6시간), 11:33 첫 백업이 잘못된 시각 선택으로 실패했음을 확인, 11:48 복원을 빠르게 하려고 연산 증설, 12:05 더 오래된 백업에서 다시 복원, 17:01 두 번째 복원 완료, 17:02 API 요청 수락 재개, 17:05 대시보드 복구와 해결. 영향: 05:01~17:05(약 12시간) 모든 사용자가 메일 발송, API, 대시보드를 쓰지 못함("no API requests were being accepted and no data was being stored"), 마이그레이션 직전 5분의 기록 손실. 재발 방지: 운영 쓰기 권한 제한(사용자가 접근하는 역할이 운영에 쓰지 못하게), 로컬 개발 개선, DB 장애 중 발송 지속을 위한 중복, 재해 복구 시험 주기 강화 | [공식](https://resend.com/blog/incident-report-for-february-21-2024) |

### M23. 서킷브레이커 오설정

- 공식 포스트모템을 찾지 못했다.

## 3. 확인하지 못한 것

- VOID(Verica Open Incident Database) 보고서와 danluu/post-mortems 컬렉션은 원문을 확인하지 않았다.
- Microsoft Teams 2020, Azure Storage 2013 인증서 사례는 보도 기반이다. Azure AD 2021은 공식 RCA의 사본 링크다.
- PostHog 2026 poison message 사례는 집계 사이트를 통한 2차 출처다.
- Omio CPU 스로틀링은 k8s.af 목록에서만 확인했다.
- 서킷브레이커 오설정, 컨슈머 리밸런싱 폭풍, 디스크 가득은 공신력 있는 공식 포스트모템을 찾지 못했다.
- Piano 2024-03-20 RCA PDF 는 2026-10-09 에 직접 열면 docs.piano.io 첫 화면으로 돌려보내져(301) 원문을 내려받지 못했다. 위 내용은 같은 날 웹 검색 색인에 남은 그 PDF 의 본문에서 옮겼고, Internet Archive 는 그날 응답하지 않았다.
