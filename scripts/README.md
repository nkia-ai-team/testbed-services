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
- v4(연속 사이클): 원천 카탈로그에서 나머지 CH, PG 표를 찾아 더 담고 meta 에 제품 버전을
  적는다. 규칙은 `docs/spec-eval-data-capture.md` §2.3, 계획기는 `scenarios/ch-scope-plan.py`,
  `scenarios/pg-scope-export.py`

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

## 은행 Oracle 관리 (ADR 정리, 12GB 상한 감시)

`oracle-maintenance.sh`는 tb-ops root crontab 에서 돈다(설치 줄은 파일 머리 주석).

- `purge`: `testbed-oracle-0` 의 ADR 홈에서 trace, cdump, incident, hm 아래 보존 기한
  (`RETENTION_HOURS`, 기본 168시간)이 지난 파일을 지운다. alert 로그와 metadata 는 두고,
  리스너가 계속 덧붙이는 두 파일(listener.log, log.xml)은 `LISTENER_LOG_MAX_MB`(기본 256)를
  넘을 때만 비운다. 러너 API 가 실행 없음, 대기열 단계 idle, paused, completed 를 답할 때만
  돈다. 러너에 물을 수 없으면 건너뛴다(녹화 중 정리 금지).
- `check`: PDB 에서 Oracle 관리 계정이 아닌 계정의 세그먼트 합(사용자 데이터)을 재서
  `STATE_DIR/last.json` 에 남기고, 10.5 GiB(`CAPACITY_ALERT_BYTES`)를 넘으면
  `STATE_DIR/ALERT` 를 쓰고 종료 코드 3 을 낸다. 기준은 사용자 데이터이고 데이터파일 합은
  참고로만 적는다(사용자 결정 2026-11-09). 같은 점검이 은행 부하 이체 계좌 풀
  (`core-banking/loadgen/transfer-accounts.txt`, tb-ops 에는 `POOL_FILE` 로 설치)의 가장 낮은 잔액을
  하한 500,000(`POOL_BALANCE_FLOOR`) 과 견주고, 풀 계좌가 없어지거나 ACTIVE 가 아니어도 사유 한 줄을
  ALERT 에 쓴다.

```bash
scripts/test-oracle-maintenance.sh
```

## 은행 Oracle 데이터 전용 디스크

`oracle-data-disk.sh`는 tb-w2 에서 root 로 돈다(v4 E7, runbook §8-3). Oracle 볼륨(local-path 디렉터리)을
노드 루트 파일 시스템에서 전용 가상 디스크로 옮겨, 그 디스크가 가득 차거나 IO 가 포화돼도 노드
전체 DiskPressure 가 나지 않게 한다(kubelet 은 루트와 이미지 저장소 파일 시스템만 본다).
디스크 IO 한도는 하이퍼바이저(Proxmox 디스크 throttle)가 늘 걸고 있다. 노드 안에는 한도 설정이 없다.

- 새 디스크를 PV 가 가리키는 같은 경로에 마운트한다. PV, PVC, StatefulSet 은 바뀌지 않는다.
- `prepare <장치>`: 파티션, 서명이 없는 빈 디스크(20~64 GiB)에만 ext4(라벨 oradata, 예약 블록 0)를 만든다.
- `cutover <장치>`: Oracle 이 멈춘 상태(pmon 없음, subPath 마운트 없음)에서만 복사하고, 체크섬으로
  같음을 확인한 뒤 원래 디렉터리를 `<경로>.orig` 로 이름만 바꿔 남긴다. 빈 마운트 지점은 변경 불가
  표시를 해서 디스크가 안 붙은 채로는 Oracle 이 빈 디렉터리에 새 DB 를 만들지 못하고 실패한다.
  fstab 은 nofail 이라 디스크가 없어도 부팅은 막히지 않는다.
- `rollback [--to-cutover]`: 지금 데이터를 원래 디렉터리로 되가져와(기본) 되돌린다. 디스크의 데이터는 남는다.
- 데이터를 지우는 동작은 없다. `.orig` 와 디스크를 지우는 것은 사람 결정이다.

```bash
scripts/test-oracle-data-disk.sh
```
