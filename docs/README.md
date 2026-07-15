---
title: 문서
status: Active
owner: project
last_reviewed: 2026-07-13
tags:
  - docs
summary: 테스트베드(testbed-services) 문서의 진입점과 주요 문서 목록.
---

# 문서

이 디렉터리는 RCA 평가 테스트베드(도메인 서비스·환경·시나리오)의 설계 문서
기준 위치입니다. 2026-07-13에 `../rca-agent-next/docs`에서 테스트베드 몫을
이전했습니다. RCA 에이전트 자체 설계 문서는 `../rca-agent-next/docs`에 남아
있습니다.

## 먼저 볼 문서

- [문서 관리 규칙](documentation-rules.md): 문서의 이름, 배치, 검토, 유지보수 기준.

## 테스트베드 정본 (이 저장소 소유)

- [테스트베드 환경 설계](spec-testbed-design.md):
  테스트베드 환경의 목적, 구성 원칙, 관측 계약, 운영 산출물, 격리 기준. (Draft)
- [테스트베드 서비스 설계](spec-testbed-services.md):
  도메인 서비스(commerce/food-delivery/core-banking)의 구성·계측·등록 대상. (Draft)
- [테스트베드 대폭 확장 + 지속 부하 (commerce 플래그십)](spec-testbed-expansion.md):
  production-like 확장 템플릿(복원성·Kafka·배치·스키마)과 상주 부하 생성기 설계.
  flagship-first(commerce)로 확정, 3도메인 구현 완료. (확정)
- [테스트베드 배포 runbook](runbook-testbed-deploy.md):
  109 kubeadm 클러스터 배포·검증 실측 절차 — 환경 실값(OTLP/org id), 신규 VM
  전제조건(StorageClass·OTel jar), 트러블슈팅. (Active)
- [관측 에이전트 설치 runbook](runbook-observability-agents.md):
  lucida-next(AP 118) 관측 4계층(SMS·APM·DPM·KCM) 설치·등록·검증 실측 절차 —
  비대칭 라우팅, DB 모니터링 계정, KCM arm64 크로스빌드 포함. (Active)
- [NMS 수집 소스 셋업 runbook](runbook-nms-source-setup.md):
  외부 서버 .57에 snmpd·trap·softflowd 셋업해 119 NMS 수신 경로 활성화 —
  G19·G20 전제 작업. (Planned — 서버 담당자 승인 대기)
- [테스트용 AP 서버 참고 정보](ref-testbed-ap-server.md):
  lucida-next AP(192.168.230.118) 접속 정보·포트맵·등록 자산 현황·검증
  엔드포인트 치트시트·알려진 제약. (Active)
- [시나리오 설계](spec-scenario-design.md):
  양성·음성 시나리오 카탈로그 구조, 설계 템플릿, 실행 시간 구조, golden 승격 기준,
  시나리오당 파일 레이아웃. (Draft)
- [시나리오 작성 규칙과 ground truth 형식](spec-scenario-authoring.md):
  service-spec.yaml을 채점 가능한 golden 레코드로 끌어올리는 작성 규칙과 계열별 패턴. (Draft)
- [부하 시나리오 규칙](spec-scenario-load.md):
  surge 주입 규칙(R0~R9), 시나리오 간 간격, RCA·이상감지 양축 커버리지,
  golden 이상감지 기대값과 사후 검증 패스. (Draft)

## 공용 문서 (rca-agent-next와 양쪽 복사 — 정본 동기화 주의)

- [평가·실험 설계](spec-evaluation.md):
  평가 축·채점식, golden 스키마, cause-domain 층화, baseline/ablation, 실험 절차. (Draft)
- [평가용 데이터 캡처·재생 설계](spec-eval-data-capture.md):
  사건 시간창을 파일로 떠서 케이스(`/data/eval-cases/`)로 저장하는 캡처
  파이프라인과 케이스 계약. 재생은 소비자 소유(§8 함정 참고). (Draft)
- [평가 케이스 복원 runbook](runbook-eval-case-restore.md):
  케이스 파일을 격리 일회용 DB 세트(VM/CH/PG)에 복원·검증·폐기하는
  소비자용 절차. (Draft)
- [Lucida Next 데이터 수집 참조](ref-lucida-next-data-collection.md):
  `../lucida-next`의 데이터 수집 대상, 경로, 스키마, 예시 데이터.
- [Lucida Next AI 기능 참조](ref-lucida-next-ai-features.md):
  `../lucida-next`의 Chat/RCA 제외 AI 기능, API, 런타임, 판단 로직.

> ⚠️ 공용 4개(평가 2 + lucida 참조 2)는 `rca-agent-next/docs`에도 동일 사본이 있습니다.
> 한쪽을 고치면 다른 쪽도 맞춰야 divergence를 막습니다.
