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

`oracle-maintenance.sh`는 tb-ops root crontab 에서 돈다. 크론 줄은 `oracle-maintenance.sh crontab` 이 `CHECK_PERIOD_HOURS`(기본 6)로 찍고, check 가 같은 값을 last.json 의 `check_period_hours` 로 남겨 러너가 오래됨 한도(주기 x 2 + 1시간)를 그 값으로 정한다.

- `purge`: `testbed-oracle-0` 의 ADR 홈에서 trace, cdump, incident, hm 아래 보존 기한
  (`RETENTION_HOURS`, 기본 168시간)이 지난 파일을 지운다. alert 로그와 metadata 는 두고,
  리스너가 계속 덧붙이는 두 파일(listener.log, log.xml)은 `LISTENER_LOG_MAX_MB`(기본 256)를
  넘을 때만 비운다. 러너 API 가 실행 없음, 대기열 단계 idle, paused, completed 를 답할 때만
  돈다. 러너에 물을 수 없으면 건너뛴다(녹화 중 정리 금지). 파드나 컨테이너에 닿지 못해 실패하면
  (`container not found` 등) 그 자리에서 `STATE_DIR/PURGE_FAILED`(시각과 사유 한 줄)를 쓰고, 러너
  readiness(`oracle_maintenance_check`)가 바로 false 가 된다(v4 ORAFIX, Q25 답 2. 11-16 은 13시간 신선도
  기한을 기다려 30시간 장애를 늦게 알렸다). 다음에 통과한 purge 가 지운다. 건너뛴 purge 는 그대로 둔다.
  손으로 지우려면 원인을 고친 뒤 `oracle-maintenance.sh purge` 를 한 번 돌린다.
- `check`: PDB 에서 Oracle 관리 계정이 아닌 계정의 세그먼트 합(사용자 데이터)을 재서
  `STATE_DIR/last.json` 에 남기고, 10.5 GiB(`CAPACITY_ALERT_BYTES`)를 넘으면
  `STATE_DIR/ALERT` 를 쓰고 종료 코드 3 을 낸다. 기준은 사용자 데이터이고 데이터파일 합은
  참고로만 적는다(사용자 결정 2026-11-09). 같은 점검이 은행 부하 이체 계좌 풀
  (`core-banking/loadgen/transfer-accounts.txt`, tb-ops 에는 `POOL_FILE` 로 설치)의 가장 낮은 잔액을
  하한 500,000(`POOL_BALANCE_FLOOR`) 과 견주고, 풀 계좌가 없어지거나 ACTIVE 가 아니어도 사유 한 줄을
  ALERT 에 쓴다. 활성 7개 계좌(ACC-1001~1007, 두 기준선이 짝 이체로 쓰고 F74-R 이 폴링)도 같은 방식으로
  하한 150,000(`SEED_BALANCE_FLOOR`) 과 견준다.
- `seed-plan`, `seed-restore`, `seed-undo 저널`: 7개 계좌를 init.sql 시작 잔액으로 되돌리는 이체 계획을
  보이고(읽기 전용), 녹화 밖에서만 그 이체를 transfer API 로 하며(원장 DEBIT, CREDIT 짝이 남아 대사 0
  유지, 끝에 대사 문장으로 확인), 저널로 되돌린다. 7개 합이 시작 합보다 적으면 `SEED_FUND_ACCOUNT` 를
  줘야 한다.

```bash
scripts/test-oracle-maintenance.sh
```

## 은행 Oracle 데이터 전용 디스크

`oracle-data-disk.sh`는 tb-w2 에서 root 로 돈다(v4 E7, runbook §8-3). Oracle 볼륨(local-path 디렉터리)을
노드 루트 파일 시스템에서 전용 가상 디스크로 옮겨, 그 디스크가 가득 차거나 IO 가 포화돼도 노드
전체 DiskPressure 가 나지 않게 한다(kubelet 은 루트와 이미지 저장소 파일 시스템만 본다).
디스크 IO 한도는 노드 안이 아니라 하이퍼바이저(Proxmox 디스크 throttle)에 건다. 값은 전환 뒤 한도 없이
`disk-io-rate.py` 로 초 단위 실측을 하고 정한다(1분 평균은 10분마다 도는 원장 대사의 순간 읽기를 감춘다).

- 새 디스크를 PV 가 가리키는 같은 경로에 마운트한다. PV, PVC, StatefulSet 은 바뀌지 않는다.
- `prepare <장치>`: 파티션, 서명이 없는 빈 디스크(20~64 GiB)에만 ext4(라벨 oradata, 예약 블록 0)를 만든다.
- `cutover <장치>`: Oracle 이 멈춘 상태(pmon 없음, subPath 마운트 없음)에서만 복사하고, 체크섬으로
  같음을 확인한 뒤 원래 디렉터리를 `<경로>.orig` 로 이름만 바꿔 남긴다. 빈 마운트 지점은 변경 불가
  표시를 해서 디스크가 안 붙은 채로는 Oracle 이 빈 디렉터리에 새 DB 를 만들지 못하고 실패한다.
  fstab 은 nofail 이라 디스크가 없어도 부팅은 막히지 않는다.
- fstrim 제외: cutover 가 쓰는 fstab 줄에 `X-fstrim.notrim` 이 들어간다(v4 ORAFIX, Q25 답 1). 이 디스크는
  하이퍼바이저 쓰기 한도(qm `mbps_wr`)가 걸려 있고 QEMU 는 discard 도 쓰기로 센다. 노드의 주 1회
  `fstrim.timer`(`fstrim --listed-in /etc/fstab:/proc/self/mountinfo`)가 11-16 10:20 KST 새 디스크의 빈 공간
  10.9 GiB 를 discard 로 보내 336초 동안 한도를 다 썼고 Oracle 이 멈췄다. ext4 는 trim 한 블록 그룹을
  메모리에만 기억하므로 재마운트나 재부팅 뒤 첫 fstrim 은 다시 빈 공간 전부를 보낸다. mount 는 `X-` 옵션을
  무시하므로 마운트는 그대로다.
- `fstab-notrim [--check]`: 이미 전환된 노드(옛 줄에 옵션이 없음)의 볼륨 줄에만 옵션을 더한다. 다른 필드와
  다른 줄은 그대로, 재마운트 없음(`systemctl daemon-reload` 만), Oracle 이 돌고 있어도 된다. 두 번 돌려도
  같다. `--check` 는 읽기만 한다: 0 제외됨, 1 제외 안 됨, 2 볼륨 줄 없음(전환 전). `check` 도 같은 줄을 낸다.
- `rollback [--to-cutover]`: 지금 데이터를 원래 디렉터리로 되가져와(기본) 되돌린다. 디스크의 데이터는 남는다.
  이름 바꾸기 뒤에 멈춘 전환(볼륨 경로가 없거나 비었고, fstab 이 디스크를 아직 안 적었거나 그 디스크가
  `.orig` 와 같음)도 되돌린다. 거절하는 상태: fstab 이 적은 디스크가 없음(전환 뒤 데이터가 그 디스크에
  있음, 붙이고 마운트), 그 디스크에 `.orig` 와 다른 데이터(마운트), 그 디스크를 읽을 수 없음, 볼륨 경로에
  자기 내용이 있음(손으로 확인).
- `check`: 위 상태를 따로 알린다. 볼륨 경로가 마운트 안 됐고 `.orig` 가 있을 때만 fstab 의 디스크를 읽기
  전용으로 잠깐 붙여 `.orig` 와 비교한다(13G 체크섬이라 몇 분 걸린다).
- 데이터를 지우는 동작은 없다. `.orig` 와 디스크를 지우는 것은 사람 결정이다.

```bash
scripts/test-oracle-data-disk.sh
```

`disk-io-rate.py` 는 104 에서 돈다. `collect` 는 ssh 하나로 Proxmox 호스트에서 그 디스크 LV 의 블록 통계
(`/sys/block/dm-N/stat`)를 1초마다 읽어 원시 줄을 쌓는다(게스트 안 프로세스 없음, VM 모니터 소켓 안 씀).
`report` 는 읽기, 쓰기 바이트율(MiB/s, qm `mbps_rd`, `mbps_wr` 에 같은 숫자)과 IOPS 의 p50, p99, p99.9, 최대와
기준 ②(p99.9 < 한도/2)만으로 본 한도를 낸다. 기준 ①은 읽기를 초 단위 대기열로 다시 흘려 본다: 초마다 들어온
바이트와 요청을 대기열 끝에 붙이고 그 초에 한도만큼 처리해, 어느 초에 들어온 읽기든 그 초 시작부터 2초
(`--max-wait`) 안에 다 끝나야 한다(지금은 그 초 안에 끝난다). 한도를 올리면 늦게 끝나는 초가 생기지 않고 읽기를
더하면 이르게 끝나는 초가 생기지 않으므로(둘 다 단조), report 가 이분 탐색으로 "두 기준을 다 지키는 가장 작은
읽기 한도"(바이트와 IOPS 각각)를 낸다. 예: 한 초 3000MiB/s 는 1500, 세 초 이어지면 2250. 드문 순간 봉우리(기동,
야간 작업, 대사 안의 한 초)를 따라 ② 만의 값보다 클 수 있다. 대기열은 QEMU throttle 처럼 한 줄이다: 요청은 들어온
차례대로 바이트 예산과 요청 수 예산을 둘 다 얻어야 나가므로, 한 초의 읽기는 max(바이트/바이트 한도, 요청/IOPS 한도)
초만큼 줄을 차지하고 앞 초가 다 나간 뒤에 시작한다. 두 최소 한도는 다른 쪽을 연 채 따로 찾은 값이라 **함께 걸면 더
엄할 수 있다**. report 가 둘을 함께 넣은 판정 줄("both smallest read limits together")을 내고, NO 면 둘을 같은 비율로
올려 다시 본다(한쪽만 올리면 그쪽을 두 배 가까이 올려야 할 수 있다). QEMU throttle 과 남은 차이 셋: 버스트 여유
(avg/10 이라 실제로는 한 초에 한도의 약 1.1배까지 찍힐 수 있고 모의가 약 0.1초 엄함), 한 초 안 순서(큰 읽기 뒤 작은
읽기가 같은 초에 오면 실제가 더 길 수 있어 두 한도가 같은 초에 함께 빠듯할 때만 모의가 낙관, 초 단위 통계로는 알 수
없어 문서로만 둠), 열린 고리 재생(Oracle 은 앞 읽기를 기다리므로 사이에 낀 가벼운 읽기는 실제가 더 짧음, 모의가 엄함).
한도를 고를 때는 이 줄을 본다. IOPS 는 호스트 블록 층 수라 qm `iops_rd`, `iops_wr` 로 옮길 때 QEMU 수와
한 번 비교한 비율로 나눈다. "read bursts" 줄은 자료를 읽는 참고일 뿐 판정에 쓰지 않는다.

한도나 평시를 재는 측정은 노드 주기 작업과 대조한다(v4 ORAFIX, Q25 답 1 ③). E7 의 24시간 실측(수, 목)과
G1 의 24시간 관찰은 월요일 주 1회 fstrim 을 보지 못했다. 측정을 끝낸 뒤 그 디스크를 쓰는 노드에서
`node-periodic-jobs.py` 를 측정 창으로 돌려 "outside the window" 에 나온 작업(주 1회, 월 1회 타이머와 크론)을
하나씩 본다. 그 작업이 이 디스크에 쓰거나 읽는지(fstrim 이면 `fstrim` 줄의 TRIMMED, 백업, scrub, 로그 정리)를
판단하고, 쓴다면 그 작업을 측정 창 안에서 한 번 돌려 다시 재거나, 그 디스크를 그 작업에서 빼거나(fstrim 은
`X-fstrim.notrim`), 한도를 그 작업의 최대치로 잡는다. 판단은 측정 결과 문서에 표로 남긴다.

```bash
# <vg>/<lv> 는 qm config <vmid> --current 에 나온 그 디스크의 볼륨 이름(짐작 금지)
scripts/disk-io-rate.py collect --host root@192.168.200.66 --volume <vg>/<lv> --seconds 86400 --out sdb.raw
scripts/disk-io-rate.py report sdb.raw --read-mbps <값> --write-mbps <값> --read-iops <값> --write-iops <값>
scripts/test-disk-io-rate.sh
```

## 노드 주기 작업 대조 (v4 ORAFIX)

`node-periodic-jobs.py` 는 노드에서 root 로 돌고 읽기만 한다(표준 라이브러리, 104 에서 표준 입력으로 흘려 돈다).
systemd 타이머(OnCalendar, 지연 RandomizedDelaySec + AccuracySec, 마지막과 다음 실행, 실행하는 명령), 크론
(`/etc/crontab`, `/etc/cron.d`, 사용자 crontab, run-parts 폴더), fstrim 이 trim 하는 파일 시스템(fstab 옵션과 장치의
discard 지원)을 한 표로 낸다. 측정 창을 주면 작업마다 창 안 실행이 있는지(`inside`, `outside`)를 낸다. 달력 타이머의
실행 시각은 `systemd-analyze calendar --base-time` 으로, 크론은 노드 시간대로 분마다 맞춰 본다. 부팅이나 직전
실행에 매인 타이머(`OnBootSec`, `OnUnitActiveSec`)는 `interval` 로 두고 짐작하지 않는다.

```bash
ssh -J root@192.168.200.66 nkia@<노드> \
  "sudo -n python3 - --window-start 2026-11-11T08:42:26Z --window-end 2026-11-12T08:42:25Z" \
  < scripts/node-periodic-jobs.py          # --json 이면 기계 판독용
scripts/test-node-periodic-jobs.sh
```

## 테스트베드 건강 점검 (v4 ORAFIX)

`testbed-health.py` 는 tb-ops 에서 10분마다 systemd 타이머로 돈다(녹화가 있든 없든). Kubernetes API 목록 한 번
(`kubectl get pods -A -o json`, tb-ops `/root/tb-kubeconfig`)과 tb-ops 안 러너 `/api/active` 만 읽고 관측 대상 VM
에는 ssh 하지 않는다. 테스트베드 네임스페이스(`rca-testbed-`)의 파드 중 Ready 아님(Succeeded 제외, 대기 사유와
함께)과 직전 실행 뒤 재시작 수 증가를 센다. 결과는 tb-ops `/var/lib/lucida/testbed-health/` 에 남는다.

- `status.line`: 한 줄, 실행마다 바뀜. 지휘 세션 상황판이 이것을 읽는다.
  `<KST 시각> testbed-health <OK|ALERT|UNKNOWN> pods <n> notready <n> restarts+<n> runner <idle|active 시나리오|unknown>`
  ALERT 이면 뒤에 ` | notready <ns>/<파드>(<사유>) ...`, ` | restarts <ns>/<파드>+<n> ...`. API 를 못 읽거나
  테스트베드 파드가 하나도 없으면 UNKNOWN 과 이유. 첫 실행은 `(first run)`. 시각이 20분보다 오래면 타이머가
  멈춘 것이다.
- `last.json`: 같은 내용 전체와 다음 실행이 견줄 재시작 수. `history.log`: 줄 전부, 최근 4,320줄(30일).
- 종료 코드 0 OK, 3 ALERT, 2 UNKNOWN(서비스 단위는 2, 3 도 성공으로 본다).
- 녹화 평시에 사건으로 보이지 않는 근거: 클러스터 감사 정책(tb-cp `/etc/kubernetes/audit/policy.yaml`)이 get,
  list, watch 를 기록하지 않는다. tb-ops 는 syslog 를 119 로 보내지 않는다(`setup-syslog-forwarding.sh` 대상에
  없음). 고정 분(매시 03, 13, ... 53분)에 지연 없이 돌아 평시부터 늘 있는 일이고 주입 시각과 짝을 이루지 않는다.

설치(tb-ops root, 저장소 사본 `/root/testbed-services` 갱신 뒤):

```bash
install -m 755 /root/testbed-services/scripts/testbed-health.py /opt/testbed/testbed-health.py
install -m 644 /root/testbed-services/scripts/systemd/testbed-health.service /etc/systemd/system/
install -m 644 /root/testbed-services/scripts/systemd/testbed-health.timer /etc/systemd/system/
systemctl daemon-reload
/usr/bin/python3 /opt/testbed/testbed-health.py; echo rc=$?     # 한 번 손으로: 첫 줄은 (first run)
systemctl enable --now testbed-health.timer
systemctl list-timers testbed-health.timer --no-pager            # 다음 실행 시각
cat /var/lib/lucida/testbed-health/status.line
# 되돌리기: systemctl disable --now testbed-health.timer; rm /etc/systemd/system/testbed-health.{service,timer}
#           systemctl daemon-reload; rm /opt/testbed/testbed-health.py (상태 폴더는 남겨도 된다)
scripts/test-testbed-health.sh
```

## 119 와 66 사이 WireGuard 터널 (v4 T0)

`vm-tunnel/` 은 SMS, NMS 전용 테스트베드(T)와 SMS 전용 테스트베드(S)가 같이 쓰는 터널 도구다.
`tb-wg.sh` 는 각 호스트(119, 66)에 `/usr/local/sbin/tb-wg` 로 설치되어 키 생성, 설정 작성, 규칙,
점검, 되돌리기를 하고, `t0-apply.sh` 는 제어 호스트에서 두 호스트를 ssh 로 다룬다(probe, apply,
verify, add-net, rollback). 키는 각 호스트의 `/etc/wireguard/wg0.key` 에만 있다. 절차와 적용 조건은
`docs/runbook-testbed-tunnel.md`.

```bash
scripts/test-vm-tunnel.sh
```
