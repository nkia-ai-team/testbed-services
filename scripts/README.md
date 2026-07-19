# 평가 케이스 캡처 도구

`capture-eval-case.sh`는 시나리오 실행이 남긴 `t1`·`t2`를 받아 평가 케이스를
만든다. 스크립트 작성·`--dry-run`은 라이브 테스트베드에 부하나 장애를 주입하지
않는다.

시간창만 확인:

```bash
scripts/capture-eval-case.sh \
  --case-id case-f15-t2-staggered \
  --scenario-id cross-domain/f15-t2 \
  --t1 2026-07-15T01:00:00Z \
  --t2 2026-07-15T01:10:00Z \
  --case-label calibration \
  --output-root /tmp/eval-cases \
  --dry-run
```

`--case-label`은 `calibration`, `evaluation`, `failed` 중 하나이며 기본값은
`calibration`이다. `evaluation`만 점수용 케이스로 표시된다. 시각은 반드시 초 단위
UTC `Z` 형식이어야 한다. 정책 테스트는 원격 서비스나 dump를 호출하지 않는다.
`evaluation` 라벨은 임의 지정할 수 없으며, fixed profile 성공·cleanup·recovery·
비오염 상태를 담은 controller-owned `--run-result`가 필요하다. capture는 case,
scenario, t1/t2, 승인 profile을 결속하고 plan/script/catalog SHA-256을 실제 파일에서
다시 계산한다. live에서는 `/var/lib/lucida/scenario-runs/<run-id>/result.json`만 받는다.

```bash
scripts/test-capture-eval-case.sh
```

실제 캡처는 `t2+45m`까지 기다린 뒤 다음을 수행한다.

- VictoriaMetrics `[t1-2h,t2+45m]` export
- ClickHouse trace·log·event 시간 슬라이스와 `host_connections` 스냅샷
- PostgreSQL 전체 custom-format dump
- stream-anomaly global v1 `model.json`과 SHA-256 스냅샷
- `meta.json` 생성 후 staging 디렉터리를 최종 케이스 경로로 원자적 승격

라이브 실행에는 `PG_PASSWORD`가 필수다. 그 외 접속값과 모델 컨테이너는
`--help`에 나온 환경변수로 바꿀 수 있다. 기존 케이스 경로가 있으면 덮어쓰지 않고
실패한다. 별도 `golden.anomaly.json`은 생성하지 않는다.

## 64개 시나리오 진입점

`scenarios/bin/`에는 catalog의 64개 시나리오와 일대일 대응하는 실행 진입점이
있다. 모든 진입점은 기본적으로 부작용 없는 dry-run이며, 준비 상태·확인 토큰·
검증된 외부 runner 조건을 통과해야만 live 실행을 위임한다.

```bash
scripts/scenarios/test-scenarios.sh
scripts/scenarios/bin/f07-h-north-south-surge.sh
```
