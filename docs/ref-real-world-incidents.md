---
title: 실제 운영 장애 사례와 원인 분포 참고 자료
status: Active
owner: project
last_reviewed: 2026-10-07
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

- 조사일: 2026-10-07
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
| Google Cloud, 2025-06-12 | 빈 필드가 든 정책 데이터가 전역으로 배포되어 널 포인터로 Service Control이 다운. 해당 코드 경로에 기능 플래그가 없었음. 재시작한 작업들이 무작위 지수 백오프 없이 Spanner로 몰림 | [공식](https://status.cloud.google.com/incidents/ow5i3PPK96RduMcb1SsW) |
| Cloudflare, 2025-11-18 | DB 권한 변경 뒤 피처 파일이 2배로 커져 200개 한도를 넘었고 프록시가 panic. 5분마다 정상 파일과 불량 파일이 번갈아 생성됨 | [공식](https://blog.cloudflare.com/18-november-2025-outage/) |
| Facebook, 2010-09-23 | 잘못된 설정값 때문에 모든 클라이언트가 캐시를 버리고 DB를 다시 조회하는 되먹임이 생김 | [공식](https://engineering.fb.com/2010/09/23/uncategorized/more-details-on-today-s-outage/) |
| Cloudflare, 2019-07-02 | WAF 규칙의 정규식 하나가 CPU를 100%까지 사용 | [공식](https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019/) |

### M2. 버그 있는 버전 배포와 롤아웃, 롤백 부작용

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Stripe, 2019-07-10 | DB 새 버전의 페일오버 결함으로 1차 장애. 롤백한 버전이 최근 설정 변경과 충돌해 2차 장애 | [공식](https://stripe.com/rcas/2019-07-10) |
| Knight Capital, 2012-08-01 | 기능 플래그를 재사용했는데 서버 1대에만 새 코드가 배포되지 않음 | [SEC 문서](https://www.sec.gov/litigation/admin/2013/34-70694.pdf) |

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

### M8. 데이터 형식 불일치 (버전 비호환)

- Liu 2019: Azure 버그 장애의 21%. Ghosh 2022: 버그 가운데 하위 호환성 문제 14.6%.
- 개별 공개 사례는 이번 조사에서 따로 찾지 않았다.

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
| Honeycomb, 2019-11-06 | 느린 누수가 모든 백엔드에서 같은 속도로 진행되어 수 분 간격으로 전부 죽음. 처음에는 ALB 문제로 오판 | [공식](https://www.honeycomb.io/blog/incident-report-running-dry-on-memory-without-noticing) |
| Twitter | GC가 증폭 기전이 된 사례 (Huang 2022 §4) | [USENIX](https://www.usenix.org/conference/osdi22/presentation/huang-lexiang) |

### M13. 리소스 limit 오설정, 노드 과밀

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| PostHog, 2025-10 | 노드 대비 요청량이 커서 파드가 과밀 배치, CPU 90% 초과에도 경보 없음 | [공식](https://posthog.com/handbook/company/post-mortems/2025-10-21-feature-flags-recurring-outages.md) |
| Omio | CPU 스로틀링 | [k8s.af 목록(2차)](https://k8s.af/) |

### M14. 외부 의존 서비스 장애

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Cloudflare, 2025-06-12 | Workers KV가 의존하는 외부 스토리지 장애로 KV 요청의 90.22% 실패 | [공식](https://blog.cloudflare.com/cloudflare-service-outage-june-12-2025/) |

### M15. 네트워크 손실, 노드 네트워크 단절, 파티션

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Datadog, 2023-03-08 | systemd 자동 보안 업데이트가 Cilium 경로를 지워 여러 리전에서 노드가 동시에 이탈 | [공식](https://www.datadoghq.com/blog/2023-03-08-multiregion-infrastructure-connectivity-issue/) |
| GitHub, 2018-10-21 | 43초 네트워크 단절 뒤 리전 간 DB 페일오버 | [공식](https://github.blog/2018-10-30-oct21-post-incident-analysis/) |

### M16. DNS

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| AWS, 2025-10-19~20 | DynamoDB DNS 자동화의 경쟁 조건으로 빈 레코드 생성 | [공식](https://aws.amazon.com/message/101925/) |
| OpenAI, 2024-12-11 | 텔레메트리 배포가 K8s API 서버를 과부하시켰고 DNS가 그 API 서버에 의존. DNS 캐시 때문에 증상이 20분 늦게 드러남 | [공식](https://status.openai.com/incidents/ctrsv3lwd797) |
| Meta, 2021-10-04 | BGP 경로 철회 | [공식](https://engineering.fb.com/2021/10/05/networking-traffic/outage-details/) |

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

### M20. 배치 잡과 온라인 트래픽 충돌

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Slack, 2025-11-01 | DB 잡이 DB CPU를 과다 사용 | [공식](https://slack-status.com/2025-11-01) |
| Slack, 2023-08-02 | 클러스터 이전으로 용량이 줄어든 상태에서 예약 잡이 몰림 | [공식](https://slack-status.com/2023-08/8ec13e4962a9bf43) |

### M21. 온라인 스키마 마이그레이션

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| GitHub, 2026-05-04 | 마이그레이션과 피크 트래픽이 겹쳐 DB 연결 용량 포화, 타임아웃 연쇄 | [공식](https://github.blog/news-insights/company-news/github-availability-report-may-2026/) |

### M22. 운영 명령이나 스크립트 실수, 조용한 데이터 오류

| 사례 | 무엇이 일어났나 | 출처 |
|---|---|---|
| Atlassian, 2022-04-05 | 앱 ID 대신 사이트 ID를 넘겨 883개 사이트 삭제 | [공식](https://www.atlassian.com/engineering/post-incident-review-april-2022-outage) |
| AWS S3, 2017-02-28 | 명령 오타로 서버를 너무 많이 제거 | [공식](https://aws.amazon.com/message/41926/) |

### M23. 서킷브레이커 오설정

- 공식 포스트모템을 찾지 못했다.

## 3. 확인하지 못한 것

- VOID(Verica Open Incident Database) 보고서와 danluu/post-mortems 컬렉션은 원문을 확인하지 않았다.
- Microsoft Teams 2020, Azure Storage 2013 인증서 사례는 보도 기반이다. Azure AD 2021은 공식 RCA의 사본 링크다.
- PostHog 2026 poison message 사례는 집계 사이트를 통한 2차 출처다.
- Omio CPU 스로틀링은 k8s.af 목록에서만 확인했다.
- 서킷브레이커 오설정, 컨슈머 리밸런싱 폭풍, 디스크 가득은 공신력 있는 공식 포스트모템을 찾지 못했다.
