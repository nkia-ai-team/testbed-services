---
title: 은행 Oracle 스스로 회복 적용과 재현 runbook
status: Planned
owner: project
last_reviewed: 2026-11-17
tags:
  - runbook
  - testbed
  - oracle
summary: 은행 Oracle 이 liveness 로 죽은 뒤 사람 손 없이 다시 열리게 하는 매니페스트 변경(기동 전 낡은 IPC 정리, 종료 유예 300초)과 fstrim 제외를 적용하는 순서, 정비 창에서 IO 를 일부러 막아 회복을 한 번 재현하는 절차, 판정 기준, 되돌리기.
---

# 은행 Oracle 스스로 회복 적용과 재현 runbook

## 왜

2026-11-16 10:20 KST tb-w2 의 주 1회 fstrim 이 Oracle 전용 디스크(sdb)의 빈 공간 10.9 GiB 를 discard 로 보내
그 디스크의 쓰기 한도(Proxmox `mbps_wr=33`)를 336초 동안 다 썼다. redo 쓰기가 2 ~ 15초로 늘어 Oracle 이 멈췄고,
liveness(30초 주기, 5회)가 컨테이너를 멈췄다. 종료 유예 30초 안에 `shutdown immediate` 가 끝나지 못해 SIGKILL 로
끝났고, 죽은 인스턴스의 SGA 와 세마포어가 파드 IPC 이름공간에 남아 그 뒤 모든 재기동이 ORA-01012 로 1초 안에
실패했다. 파드를 지울 때(11-17 17:50 KST)까지 30시간 이어졌다. 근거: testbed-ops v4 `agent-out/ORA.md`, `APORA.md`.

검토 세션 답(questions.md Q25 답 1): 원인(fstrim)을 빼고, IO 가 막혀 SIGKILL 로 끝나더라도 재기동이 스스로 회복되게 한다.
종료 유예만으로는 IO 가 막힌 동안 `shutdown immediate` 도 redo 에서 멈출 수 있어 같은 영구 장애가 다시 나므로,
기동 전 낡은 IPC 정리가 필수이고 종료 유예 300초는 덧붙임이다.

## 바뀌는 것

| 무엇 | 어디 | 효과 |
|---|---|---|
| fstab 줄에 `X-fstrim.notrim` | tb-w2 `/etc/fstab` 의 sdb 줄(`scripts/oracle-data-disk.sh fstab-notrim`), 다음 전환부터는 `cutover` 가 직접 씀 | 주 1회 fstrim 이 sdb 를 건너뜀. 재마운트 없음 |
| 기동 전 IPC 정리 | `core-banking/k8s/10-oracle.yaml` 의 컨테이너 command 와 ConfigMap `oracle-ipc-clean` | 컨테이너가 다시 뜰 때 주인 없는 SGA, 세마포어, `/dev/shm/ora_*` 를 `sysresv -f` 로 지운 뒤 이미지 진입점을 그대로 exec |
| 종료 유예 300초 | 같은 매니페스트 `terminationGracePeriodSeconds: 300` | 진입점의 SIGTERM 처리(`shutdown immediate`)가 끝날 시간 |

정리 스크립트가 지우는 조건(셋 다 만족할 때만): oracle 사용자 소유 공유메모리가 있음, 그 세그먼트의 nattch 가 모두 0,
이 컨테이너에서 Oracle 배경 프로세스(`ora_pmon_` 등)가 보이지 않음. 지우지 못해도 기동을 막지 않는다(로그만 남김).
이 이미지에는 ipcs, ipcrm, ps 가 없어 판단은 `/proc/sysvipc/shm`, `/proc/<pid>/comm` 으로, 지우기는 Oracle 의 `sysresv` 로
한다. initContainer 는 새 파드 샌드박스(새 IPC 이름공간)에서만 돌아 컨테이너 재시작에는 쓸모가 없고, 이미지의
`/container-entrypoint-startdb.d` 는 DB 기동이 끝난 뒤에 돌아 기동 실패 앞에 끼어들 수 없어서 command 감싸기를 골랐다.

매니페스트의 1 ~ 106행 줄 번호는 정답지가 근거로 가리키므로 그대로다(시험
`BankingOracleSelfRecoveryTests.test_answer_key_line_anchors_still_point_at_the_same_lines`).

## 적용 순서

조건: 테스트베드 잠금(`locks/testbed`), 러너 `/api/active` false 와 대기열 정지, 적용 뒤 2시간 30분 규칙을 새로 센다.
fstrim 제외는 다음 fstrim(2026-11-23 월 09:23:56 KST) 전에 끝낸다.

1. tb-ops 저장소 사본 갱신: `/root/testbed-services` 를 main 반영본으로 맞춘다(종전 적용 절차와 같음).
2. tb-w2 fstrim 제외(Oracle 이 돌고 있어도 된다, 재마운트 없음):
   ```bash
   # tb-w2 root. 스크립트는 tb-ops 사본에서 넘긴다.
   cp /etc/fstab /root/fstab.bak-before-notrim-$(date +%Y%m%d)
   bash oracle-data-disk.sh fstab-notrim --check; echo rc=$?     # 기대 rc=1 (제외 안 됨)
   bash oracle-data-disk.sh fstab-notrim
   bash oracle-data-disk.sh fstab-notrim --check; echo rc=$?     # 기대 rc=0
   findmnt -n -o SOURCE,OPTIONS --target <PV 경로>               # 마운트 그대로(sdb, rw)
   fstrim --listed-in /etc/fstab:/proc/self/mountinfo --dry-run --verbose   # sdb 경로가 목록에 없어야 함
   ```
   되돌리기: `cp /root/fstab.bak-before-notrim-<날짜> /etc/fstab && systemctl daemon-reload`.
3. 매니페스트 적용(Oracle 파드가 한 번 다시 뜬다. 1 ~ 3분, 앱 연결 풀은 스스로 다시 붙는다. 11-17 복구 때 실측):
   ```bash
   # tb-ops root
   kubectl --kubeconfig /root/tb-kubeconfig apply -f /root/testbed-services/core-banking/k8s/10-oracle.yaml
   kubectl --kubeconfig /root/tb-kubeconfig -n rca-testbed-banking rollout status statefulset/testbed-oracle --timeout=15m
   kubectl --kubeconfig /root/tb-kubeconfig -n rca-testbed-banking logs testbed-oracle-0 | grep -E 'ipc-clean|READY TO USE|STARTUP FAILED'
   ```
   기대: `CONTAINER: ipc-clean: no shared memory or semaphores of uid 54321, nothing to clean`(새 샌드박스라 치울 것 없음), 그 뒤
   `DATABASE IS READY TO USE!`. 파드 spec 의 `terminationGracePeriodSeconds` 가 300. account, ledger, transfer 5분 안 1/1.
   `oracle-maintenance.sh check` rc 0. 재기동이 매시 23분 purge 와 겹쳤으면 tb-ops `/var/lib/lucida/oracle-capacity/PURGE_FAILED`
   가 있을 수 있다(두 번 다 닿지 못함). 있으면 `/opt/testbed/oracle-maintenance.sh purge` 를 한 번 돌려 지운다.
   적용 때 옛 파드는 옛 spec 의 유예(30초)로 끝나고, 적용 뒤의 파드 삭제는 최대 300초를 기다린다.
   되돌리기: 앞 커밋의 매니페스트를 apply(`git show <앞 커밋>:core-banking/k8s/10-oracle.yaml | kubectl apply -f -`) 뒤
   `kubectl delete configmap oracle-ipc-clean -n rca-testbed-banking`. 이것도 Oracle 파드를 한 번 다시 띄운다.

## 재현: IO 를 막아 liveness 가 죽인 뒤 사람 손 없이 다시 열리는지

정비 창에서 지휘 세션이 한 번 한다. 녹화 없음, 잠금, 이 절차 끝 시각부터 2시간 30분 규칙.
한도를 일부러 낮춰 redo 쓰기를 막는다. 66 변경 두 번(낮춤, 되돌림)이고 둘 다 시각을 적는다.

1. 앞 기록(읽기만): `kubectl get pod testbed-oracle-0 -o jsonpath='{.metadata.uid} {.status.containerStatuses[0].restartCount}'`,
   `qm config 103 --current | grep scsi1`(되돌릴 값), alert log 줄 수(tb-w2 `<PV 경로>/diag/rdbms/free/FREE/trace/alert_FREE.log`),
   tb-ops `cat /var/lib/lucida/testbed-health/status.line`.
2. 66 에서 sdb 쓰기 한도를 낮춘다(실행 중 VM 에 바로 걸림, 재기동 없음):
   `qm set 103 --scsi1 <1단계 scsi1 값에서 mbps_wr=1,iops_wr=5 로 바꾼 것>`. `qm config 103 --current` 로 확인,
   `qm pending 103` 에 남은 것이 없어야 한다.
3. 기다리며 본다(사람은 손대지 않는다): alert log 에 `log write elapsed`, `--ATTENTION--`, 파드 이벤트에
   `Liveness probe failed`(30초 간격 5회, 약 2분 30초), 그 뒤 `Killing`. 종료 유예 300초 안에 끝나면 정상 종료,
   못 끝나면 SIGKILL 이다. 두 경우 모두 이 재현의 대상이다(뒤의 판정에서 어느 쪽이었는지 적는다).
   liveness 로 죽일 때도 파드 종료 유예(300초)를 쓰므로, IO 가 막혀 shutdown immediate 가 멈추면 감지 약 150초에
   유예 최대 300초가 더해져 재시작까지 7분 넘게 걸릴 수 있다(종전 30초. Q25 답대로 이 상황에서는 유예가 회복을
   늦추기만 한다. liveness 만 짧게 하려면 `livenessProbe.terminationGracePeriodSeconds`, 클러스터 v1.31 지원, 지휘 세션 판단).
4. 컨테이너가 다시 시작된 것(재시작 수 +1)을 보면 바로 66 한도를 1단계 값으로 되돌린다:
   `qm set 103 --scsi1 <1단계 값 그대로>`. 되돌린 시각을 적는다. 한도가 낮은 채로는 인스턴스 복구가 끝나지 않는다.
   재시작이 15분 넘게 나지 않으면 한도를 되돌리고 멈춘다(재현 실패로 보고).
5. 사람 손 없이 기다린다(최대 20분): 파드 uid 가 1단계와 같아야 한다(파드를 지우지 않았다는 증거).

판정(모두 맞아야 충족):

| 항목 | 기대 |
|---|---|
| 파드 | 같은 uid, 재시작 수 1단계 + 1(또는 + n), 1/1 Ready |
| 컨테이너 로그 | SIGKILL 이었으면 `ipc-clean: leftover IPC of a stopped instance (...) removing with sysresv -f -l FREE` 와 `ipc-clean: done: no shared memory or semaphores of uid 54321 left`, 정상 종료였으면 `nothing to clean`. 어느 쪽이든 `DATABASE IS READY TO USE!`, `ORA-01012` 와 `STARTUP FAILED` 없음 |
| alert log 새 줄 | SIGKILL 이었으면 `Beginning crash recovery`, `Completed crash recovery`, `Completed: ALTER DATABASE OPEN` |
| 파드 IPC(tb-w2 `nsenter -t <pause PID> -i ipcs`) | 세그먼트의 만든 PID 가 새 인스턴스, nattch 0 보다 큼, 옛 1.59 GB 세그먼트가 두 벌 있지 않음(tb-w2 `Shmem` 약 1.55 GB) |
| 앱과 점검 | account, ledger, transfer 1/1, 은행 5xx 10분 0, `oracle-maintenance.sh check` rc 0, 원장 대사 imbalance 0 |
| 건강 점검 | `status.line` 이 ALERT(재시작 증가)를 낸 뒤 다음 줄이 OK |

실패하면(`LEFT after sysresv`, ORA-01012 반복): ORA.md 복구 A(파드 삭제)로 되돌리고 로그 전부를 결과에 남긴다.
파드 삭제도 종료 유예 300초를 기다린다. 인스턴스가 이미 죽어 기다릴 것이 없으면
`kubectl delete pod testbed-oracle-0 --grace-period=30` 로 줄인다(IO 가 멀쩡한 상태의 정상 종료는 30초 안에 끝난다).
재현이 매시 23분 purge 와 겹쳐 `PURGE_FAILED` 가 남았으면 `oracle-maintenance.sh purge` 를 한 번 돌려 지운다.
`sysresv` 가 남긴 것이 무엇인지(세그먼트, 세마포어)가 다음 판단의 근거다.

## 104 에서 먼저 확인한 것 (2026-11-17 19:36 ~ 19:37 KST, 리뷰 rv-orafix M3)

테스트베드와 같은 이미지(`gvenzl/oracle-free@sha256:fbbd3023...`, 23.26.2, tb-w2 파드의 imageID)로 104 docker 에서
파드 구조를 흉내 냈다: IPC 만 가진 컨테이너(`--ipc shareable`, 파드 샌드박스 몫)에 Oracle 컨테이너를
`--ipc container:` 로 붙이고 데이터는 같은 볼륨, `--memory 4g --cpus 2`, 무거운 작업 잠금과 MemoryMax=4G.

| 단계 | 결과 |
|---|---|
| 첫 기동 | 30초에 `DATABASE IS READY TO USE!`. 공유메모리 4개(1,593,835,520 바이트 포함, 키 0x793455d4), 세마포어 1(키 0x052c12f4), `/dev/shm/ora_793455d4_*` 2개, nattch 110, 55, 55, 55 |
| `docker kill -s KILL`(컨테이너 전체 SIGKILL) | 세그먼트 넷과 세마포어, `/dev/shm` 파일이 그대로 남고 nattch 0. 11-16 tb-w2 파드 IPC 와 같은 모양 |
| 정리 없이 다시 기동(11-16 재시작) | 5초 만에 끝, 종료 코드 57, `ORA-01081: cannot start already-running ORACLE - shut it down first`. 11-16 은 ORA-01012 였는데 둘 다 낡은 SGA 에 붙어 생기는 같은 영구 실패다 |
| 매니페스트와 같은 command 로 기동 | `ipc-clean: leftover IPC of a stopped instance (segments 0 1 2 3 semaphores 3 )`, sysresv 가 `Oracle Instance not alive for sid "FREE"` 로 판단해 세그먼트 넷, 세마포어, MGA 파일 둘을 지움(`Done removing ipc resources`), `ipc-clean: done`, 10초 만에 `DATABASE IS READY TO USE!`. 새 세그먼트(shmid 4 ~ 7)와 세마포어(semid 9)가 생김 |
| 다시 SIGKILL 하고 같은 command 로 기동 | 같은 정리(segments 4 5 6 7 semaphores 9), 10초 만에 OPEN. 되풀이해도 된다 |

이미지에 ipcs 가 없어 sysresv 가 `sh: /usr/bin/ipcs: No such file or directory` 를 찍지만 지우기에는 영향이 없다.
sysresv 출력 약 50줄이 컨테이너 로그에 `CONTAINER: ipc-clean: sysresv:` 로 남는다(정리할 때만).

## 확인하지 않은 것

- 테스트베드에서 liveness 가 실제로 죽이는 길(IO 포화, 종료 유예 300초, kubelet 재시작)은 위 재현으로 처음 본다.
  104 확인은 컨테이너 SIGKILL 과 재기동만 흉내 냈다.
