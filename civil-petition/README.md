# civil-petition: 민원 접수, 처리 포털 (전통 3계층 테스트베드)

쿠버네티스와 OTel 없이 VM 위 systemd 로 도는 웹(Apache httpd 2.4), WAS 두 대(Tomcat 10.1, Java 17, WAR 하나),
DB(PostgreSQL 16) 와 계층 사이 가상 라우터 하나로 이루어진 테스트베드의 앱과 구성 코드다. 관제는 SMS 와 NMS 만이다.

- 설계: [docs/spec-testbed-vm-3tier.md](../docs/spec-testbed-vm-3tier.md)(T), [docs/spec-testbed-sms-only.md](../docs/spec-testbed-sms-only.md)(S, 같은 코드를 쓴다)
- 적용 순서: [docs/runbook-civil-petition-apply.md](../docs/runbook-civil-petition-apply.md)
- 시험: `python3 -m unittest scripts/scenarios/tests/test_civil_petition_deploy.py`, 앱은 `mvn -B package`

| 디렉터리 | 내용 |
|---|---|
| `petition-web/` | WAR 소스. 업무 경로 일곱(신청, 접수번호 조회, 내 민원 목록, 공개 검색, 담당자 처리, 통계, 로그인)과 배치 관리 경로 |
| `db/` | 스키마, 결정적 시드 |
| `loadgen/` | k6 상주 부하(기존 도메인과 같은 하루 모양, 여정 비중은 설계 §2-3) |
| `deploy/` | 인벤토리(T, S), 렌더러, 설정 틀, 설치 단계, 적용 스크립트, VM 명령 찍기, 라우터 도우미, 판정 도구 |

테스트베드별로 다른 것은 `deploy/inventory/*.json` 뿐이다. 설정 틀과 설치 단계 본문에 주소, 게스트 호스트 이름,
도메인 값을 쓰지 않는다(시험이 막는다).
