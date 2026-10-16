---
title: F80-R 설계 시트 (banking 워커의 Oracle 데이터 파일 일상 백업이 노드 메모리를 거의 다 써 banking 파드가 축출되거나 OOM 으로 죽음)
status: Draft
owner: project
last_reviewed: 2026-10-16
tags:
  - scenario
  - design
  - node
  - memory
summary: banking 워커 tb-w2 에서 Oracle 볼륨의 일상 백업(systemd 일시 유닛)이 가장 큰 데이터 파일과 나머지 볼륨을 두 흐름으로 나란히 zstd -12 --long=31 로 압축한다. 흐름마다 2 GiB 장거리 창이 차며 합계 약 4.6 GiB 를 쥐어 노드 여유(2.2~2.8 GiB)를 넘고, 파드 밖 작업이라 한도도 축출도 없어 노드가 파드를 내준다(kubelet 이 요청량을 넘긴 Oracle 을 축출하거나 커널이 KCM 노드 에이전트와 banking 자바 컨테이너를 OOM 으로 죽임). 배포도 설정 변경도 없고 원인 흔적은 호스트 지표와 로그에만 남는다. 원본은 GitHub 2026-09-20(일상 저장소 유지보수 작업이 아주 큰 저장소에서 저장 서버 메모리를 거의 다 써 Git 작업을 처리하지 못함).
---

# F80-R 설계 시트

## 1. 요약

banking 워커 tb-w2 에는 banking 앱 다섯(nginx, api, account, transfer, ledger), Kafka, Oracle 이 있고, Oracle 데이터는 같은 노드의 local-path 볼륨(PVC `oracledata-testbed-oracle-0`)에 있다. 운영자의 일상 백업이 그 볼륨의 데이터 디렉터리(FREE, 합계 9,303,835,648 바이트)를 두 흐름으로 나란히 압축한다: 가장 큰 `FREEPDB1/users01.dbf`(5,369,765,888 바이트)는 혼자, 나머지(약 3.7 GiB)는 `tar` 하나로, 둘 다 `zstd -q -12 --long=31 -T1`. 데이터 파일 하나가 나머지보다 큰 백업에서 흔한 나눔이다. 예약 백업이 도는 방식대로 파드 밖 systemd 일시 유닛(`oradata-backup.service`, Nice 10, IO 우선순위 idle)이다.

zstd 장거리 모드의 창(2^31 = 2 GiB)은 입력이 지나가며 차므로 흐름마다 상주 메모리가 대상 크기를 따라 커지고, 입력을 2 GiB 넘게 읽으면 약 2.3 GiB 에서 멈춘다(실제 파일 실측 2,395,520 kB, 2,404,096 kB, §9). 두 흐름이 합계 약 4.6 GiB 를 쥐는데 tb-w2 여유는 2,257~2,904 MiB(66 이전 뒤 모든 1분 표본, 스왑 없음)라 작업이 여유를 넘는다. preflight 는 여유가 작업보다 1 GiB 이상 작을 때(≤ 3,584 MiB)만 주입한다. 작업은 어느 파드 cgroup 에도 없어 메모리 한도가 없고 kubelet 이 축출할 수도 없다. 그래서 노드는 파드 쪽에서 무너진다: kubelet 이 memory.available<100Mi 에서 요청량을 가장 많이 넘긴 파드를 축출하는데 그것이 Oracle 이고(작업 집합 약 2.8 GiB, 요청 2Gi), 증가가 kubelet 주기보다 빠른 구간에는 커널 OOM killer 가 oom 점수가 높은 프로세스(KCM 노드 에이전트, banking 자바 컨테이너)를 죽인다. 어느 쪽이든 이체 경로(api → account → transfer → Oracle)가 끊겨 이체와 거래 내역이 502, commerce 정산 이체가 실패한다. 압박이 이어지는 동안 다시 뜬 파드도 같은 메모리 부족을 만난다.

배포, 설정 변경, 부하 변화는 없다. 앱 추적에는 하류 실패만 남고, 원인은 호스트 데이터에만 있다: SMS 메모리 사용률, 프로세스 목록의 zstd 둘(각 약 2.3 GiB)과 tar(기가바이트 읽기), 유닛을 띄운 sudo 와 systemd 시작 줄, 노드 수준의 SystemOOM 이나 메모리 축출(빠진 파드는 자기 한도 아래). 사용자 요청("APM 없이도 원인을 찾아야 하는 서버 자원 장애, 계기는 배포나 설정 변경이 아닌 것")에 맞춘 후보다.

비유: 은행 지점 금고실 옆 작은 사무실에서 야간 장부 복사 담당이 장부 전체를 펼쳐 놓고 베끼기 시작했다. 장부가 커져 펼친 종이가 사무실 바닥을 거의 다 덮자, 창구 직원들이 앉을 자리가 없어 하나둘 밖으로 밀려나고(가장 넓게 앉아 있던 금고 담당이 먼저), 고객 송금이 멈춘다. 창구 쪽 기록에는 "직원이 자리에 없었다"만 남고, 이유는 사무실 바닥을 본 사람만 안다.

## 2. 원본 사례

- 기업: GitHub
- 날짜: 2026-09-20 21:46~22:24 UTC
- 링크: [공식 상태 페이지 "Incident with Pull Requests"](https://www.githubstatus.com/incidents/f6yrxnz5f7bs)
- 요약(출처가 말한 것만): Pull Requests 저하. merge commit 과 test merge commit 생성이 최대 약 4분 늦어졌고, 그 commit 뒤에 시작하는 pull request 이벤트 Actions 실행 일부도 늦어졌다(잃은 commit 없음). 원인은 "a routine repository maintenance job for an unusually large repository consuming nearly all of the memory" 로 Git 저장 서버 한 대가 merge commit 생성에 필요한 Git 작업을 처리하지 못한 것. "We mitigated the incident by removing the affected server from service at 22:18 UTC", 쌓인 merge commit 은 6분 안에 생성. 후속은 유지보수 작업 하나가 쓸 수 있는 메모리에 상한을 두어 저장소 하나가 서버를 고갈시키지 못하게 하는 것과 저장 서버 상태 감시, 경보 개선. 서버 이름, 작업 종류의 세부, 탐지 경로는 적혀 있지 않다. `ref-real-world-incidents.md` M20 에 더했다.
- 같은 계열(같은 M20 에 함께 더함, 이번 후보 목록에서 다른 부품으로 시도): GitLab.com 2021-01-16 production#3338(DB 볼륨 스냅숏이 IO 지연 급증을 일으켜 장애 조치), Open Telekom Cloud 2019-03-30(감시 에이전트의 예약 작업이 시각 계산 버그로 곧바로 다시 돌며 프로세스가 늘어 CPU 포화). 셋 다 "서비스 옆에서 도는 일상 작업이 같은 서버의 공유 자원을 다 써 서비스가 무너짐"이고 다 쓴 자원(메모리, 디스크 IO, CPU)이 다르다.
- 현실 비중: Google 포스트모템 트리거의 '용량 관리 5%', '성능 저하 누적 5%', Ghosh 2022 '인프라와 용량 12%', 장부 묶음 A(용량 5~12%). 이 테스트베드의 정답이 노드인 시나리오 가운데 노드 메모리를 다 쓴 원인이 실제 운영 작업(이름 있는 프로세스와 시작 기록)인 것은 없었다(F05-P 는 이름 없는 메모리 점유).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 일상 저장소 유지보수 작업이 아주 큰 저장소를 대상으로 돎 | 일상 Oracle 데이터 파일 백업이 2 GiB 를 넘는 데이터 파일(users01.dbf 5.4 GiB)을 대상으로 돎(systemd 유닛 시작) |
| 원인이 된 결함 | 유지보수 작업 하나가 쓸 수 있는 메모리에 상한이 없어 대상 크기만큼 메모리를 씀(후속 조치가 그 상한) | 백업 압축(zstd --long=31 두 흐름)의 메모리가 대상 크기를 따라 흐름마다 약 2.3 GiB, 합계 약 4.6 GiB 까지 크고, 파드 밖 유닛이라 상한(MemoryMax, 파드 한도)이 없음 |
| 전파 경로 | 서버 메모리가 거의 다 쓰여 그 서버가 Git 작업을 처리하지 못함 | 노드 메모리가 다 쓰여 노드가 파드를 내줌(kubelet 이 Oracle 축출, 또는 커널 전역 OOM 이 KCM 노드 에이전트와 자바 컨테이너를 죽임) → 이체 경로 끊김 |
| 사용자 증상 | merge commit 생성 최대 약 4분 지연, 관련 Actions 지연 | banking 이체와 거래 내역 502, Oracle 이 빠지면 잔액 조회도 실패, commerce checkout 정산 실패 |
| 원본의 탐지 경로 | 적혀 있지 않음(후속으로 저장 서버 상태 감시와 경보 개선) | SMS 노드 메모리와 프로세스 목록(zstd 둘, tar), syslog 의 유닛 시작, KCM 노드 SystemOOM 이나 메모리 축출, APM 오류율 |
| 완화와 복구 | 그 서버를 서비스에서 뺌(22:18 UTC), 쌓인 작업 6분 안에 처리 | 백업 유닛을 멈추고(cleanup) 축출되거나 죽은 파드가 다시 뜸 |

기전은 원본과 같다: "서비스와 같은 서버에서 도는 일상 유지보수 작업이 대상이 커서 서버 메모리를 거의 다 쓰고, 그 서버가 맡은 일을 처리하지 못하며, 작업을 치우면 회복한다". 바꾼 것 셋: ① 작업 종류. 원본은 Git 저장소 유지보수(세부 미공개)이고 이 테스트베드에는 Git 저장소가 없어, 같은 서버에서 대상 크기에 비례해 메모리를 쓰는 일상 작업으로 DB 볼륨 백업 압축을 쓴다(zstd 장거리 모드의 창이 대상 크기를 따름, 실측). ② "처리하지 못함"의 모양. 원본 서버는 Git 프로세스가 일을 못 했고, 쿠버네티스 노드에서는 메모리가 마르면 kubelet 축출이나 커널 OOM 이 파드를 내보내는 것이 같은 결과의 모양이다. ③ 크기. 이 노드는 원본 서버보다 작아 작업이 '거의 다'가 아니라 확실히 넘도록 두 흐름으로 돌리고, preflight 가 여유가 작업보다 1 GiB 이상 작을 때만 주입한다(1회차 평가 지적: 한 흐름 2,339 MiB 는 여유의 82~106% 라 피해가 그때그때 메모리 상태에 달렸다). 원본의 대상 크기(아주 큰 저장소)에 해당하는 것은 2 GiB 창을 넘는 대상(users01.dbf 와 나머지 볼륨) 둘이다.

## 3. 숫자 근거 (2026-10-16, `scenario-stats.py`, 정식 + 후보 합계 116, 이 후보 전)

| 축 | 값 |
|---|---|
| 서비스 | 쇼핑몰 39, **은행 38**, 음식배달 39 → 은행이 가장 적어 은행에서 찾는다(원칙 2, 돌아가며 만들기) |
| 묶음 | J 20(17%), D 17(14%), P 14(12%), G 13(11%), A 9(7.8%), L 9, B 7, C 6, E 4, F 4, K 3, O 3, H 2, I 2, Q 2, M 1, N 0. 이 후보는 **A(9→10, 117 중 8.5%)** |
| 정답 위치 | 은행 이체 서비스 11(9%), 배달 서비스 8, 주문 서비스 8, **노드, 디스크 7(6%)**, 외부 결제 의존 6, … 이 후보는 **노드, 디스크(7→8, 6.8%)**. 결제 경로 합계 14(12.1%) 그대로 |
| 부품 지도 | 은행 oracle 9, account 11, transfer 13, api 5, ledger 2, kafka 0, nginx 0. 인프라 층 '노드 자원'에서 메모리가 정답인 시나리오는 F05-P(쇼핑몰) 하나 |

이 후보를 고른 이유:

- 사용자 요청이 "호스트 지표와 로그에만 원인 흔적이 남는 서버 자원 장애(디스크 IO, CPU, 메모리, 파일 시스템), 계기는 배포나 설정 변경이 아님"이고, 은행이 가장 적다. 은행 노드 tb-w2 의 네 자원을 모두 후보로 올려 숫자와 실측으로 줄 세웠고(아래 목록), 피해까지 이어지는 것은 메모리뿐이었다.
- 묶음 A(8.5%)와 정답 위치 노드, 디스크(6.8%) 모두 20% 상한에서 멀다. 장부로는 F05-P(쇼핑몰 노드 메모리 점유)와 같은 노드 메모리 계열이라 R 관계에 가깝다: 노드와 도메인, 원본, 점유한 프로그램의 정체(이름 있는 일상 백업 유닛과 백업 도구), 먼저 빠지는 대상(DB)이 다르다(§8).
- 증상이 겹치고 정답이 다른 짝이 있다(관계 H, §8): F25-H(DB 컨테이너 자신의 한도 OOM), F35-R(DB 계정 잠김), F70-R, F63-R(서비스 자신의 힙, 컨테이너 한도). 관제 AI 가 "DB 가 빠졌으니 DB", "컨테이너가 죽었으니 그 서비스"로 찍으면 부분 점수에 그치고, 호스트 메모리와 프로세스 목록까지 내려가야 정답에 닿는다.
- rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본과 같은 주입이 함께 있는 것은 없다. rejected 2026-10-12(반복 68) 의 같은 원본(GitHub 2026-09-20)은 작업을 **Oracle 세션**(PGA 한도로 그 세션만 끊김)이나 **노드 Job 파드**(BestEffort 라 먼저 축출, OOM 됨)로 놓아 막혔다. 이 후보는 작업을 파드 밖 호스트 프로세스로 놓아 그 두 벽에 걸리지 않는다(kubelet 이 축출할 수 없고, oom_score_adj 0 이라 파드보다 늦게 고름, §9). 주입 수단도 다르다(새 실행기 host.job).

### 3단계 후보 목록 (실제 기전 × 부품, 순서는 숫자 순: 은행 먼저)

| 순위 | 후보 | 원본 사례 | 부품 | 결과 |
|---|---|---|---|---|
| 1 | Oracle 데이터 파일 일상 백업(tar, zstd --long=31)이 tb-w2 메모리를 거의 다 씀 | GitHub 2026-09-20 공식 상태 페이지 | 노드 메모리(노드, 디스크 7), 은행 | **채택(F80-R)** |
| 2 | Oracle 볼륨 백업, 스냅숏 복사가 tb-w2 디스크 IO 를 포화 | GitLab.com 2021-01-16 production#3338(공식 운영 이슈), Litium 2019-07-03 상태 페이지 | Oracle 디스크(노드, 디스크), 은행 | 버림(원칙 9): 2026-10-16 tb-w2 실측(sda, 66 vmdata = 하드웨어 RAID SSD)에서 버퍼 쓰기 12 GB 가 약 14초에 끝났고(약 0.9 GB/s), 그동안 8k 동기 쓰기 p99 가 2.2 → 20.6ms, p99.9 4.0 → 30.8ms 로만 올랐다. 8.7 GiB 복사는 20초 남짓이면 끝나고 Oracle 커밋이 수십 ms 로는 5xx 가 나지 않는다. F10-P(같은 Oracle, fio 6000 IOPS)의 피해 실측은 109 의 다른 저장 장치(vda)에서였다 |
| 3 | 노드 감시 에이전트의 예약 작업이 시각 계산 버그로 곧바로 다시 돌며 프로세스(lspci)가 늘어 CPU 포화, 헬스 체크 실패 | Open Telekom Cloud 2019-03-30 공식 원인 분석 | 노드 CPU, 은행 | 버림(원칙 9, 7): F15-P 계측(tb-w2 에 cpu 워커 8/16/32)에서 transfer p95 중앙값 249/295/290ms 로 약 300ms 에서 포화, account 무반응, 5xx 없음. 호스트 프로세스는 system.slice(cpu.weight 100)라 kubepods.slice(157)가 4코어 중 2.4코어 이상을 지키고 banking 수요는 약 0.7코어다. 원본처럼 상한 없이 늘리면 노드가 응답을 잃어(원본은 SSH 도 막힘) 중단 조건(entry 0, 노드 NotReady)에 먼저 닿는다 |
| 4 | 로그 폭주나 회전 실패로 tb-w2 루트 파일 시스템 가득 참, Oracle 쓰기 실패 | Adafruit 2016-06-07 공식 블로그(logrotate 가 돌지 않아 로그 약 20GB, 디스크 가득) | 루트 파일 시스템(노드, 디스크), 은행 | 버림(컨트롤러 필수 중단 조건, v4 대기): Oracle 데이터가 루트 디스크(96G, 79G 여유)라 가득 차기 전에 kubelet DiskPressure(nodefs 10%)가 banking 파드를 모두 축출하고, 축출로 쓰이지 않게 된 ctr import 앱 이미지가 이미지 GC(85%)로 지워지면 레지스트리가 없어 되돌릴 수 없다(F52-R 의 고장 모양). 사용자 결정(2026-10-16 이전 계획 §8)으로 v4 에서 tb-w2 에 Oracle 전용 디스크를 달면 풀린다 |
| 5 | tb-w2 inode 고갈 | GitLab.com 2021-04 production#4197, Iowa Environmental Mesonet 2019-12-16 | 루트 파일 시스템, 은행 | 버림(원칙 7, 위와 같은 벽): GitLab 사례는 고객 영향이 없던 선제 완화, kubelet nodefs.inodesFree<5% 도 DiskPressure 축출이라 4번과 같은 벽 |
| 6 | 주기 fstrim 이 discard 를 몰아 IO 정지 | Nuvotex 블로그, Red Hat KB(구독, 미검증) | Oracle 디스크, 은행 | 버림(원칙 1): 공식 사후 보고 없음(웹 검색 1회). ext4 는 마운트 뒤 한 번 다듬은 블록 묶음을 다시 다듬지 않아 같은 강도를 되풀이할 수도 없다 |
| 7 | 노드 로그 수집 에이전트의 메모리 증가가 SystemOOM 을 부름 | Blue Matador 2019-06-15 블로그, DFDS PM2024-001(공개 사후 보고) | 노드 메모리, 은행 | 버림(원칙 1, 7): Blue Matador 는 사용자 영향 없음, DFDS 는 원인이 에이전트와 개발 컨테이너로 섞이고 사용자 영향을 적지 않음. 채택 후보와 같은 노드 메모리라 더 분명한 원본(1번)으로 대신한다 |
| 8 | 원인 미상 메모리 소비로 OOM killer 가 DB 를 죽임 | Miraheze 2016-12 사고 기록 | Oracle(DB 인스턴스), 은행 | 버림(원칙 1): 출처가 메모리를 쓴 주체를 밝히지 못함("Investigate how the OOM killer was triggered" 가 미결) |
| 9 | 하이퍼바이저 스냅숏(Proxmox thin 스냅숏)의 쓰기 시 복사로 DB 디스크 지연 | GitLab.com 2021-01-16 production#3338 | 66 호스트 저장소, 세 도메인 공통 | 버림(원칙 9, 2): 66 vmdata 를 세 워커가 함께 써 피해가 도메인을 가르지 못하고(정답 위치 모호), RAID SSD 에서 64K 쓰기 시 복사로 Oracle 커밋이 5xx 까지 갈 계산이 서지 않음 |
| 10 | 같은 백업 메모리 기전을 음식배달 tb-w3(MySQL), 쇼핑몰 tb-w1(PostgreSQL)에 | GitHub 2026-09-20 | 노드 메모리, 음식배달·쇼핑몰 | 이번엔 고르지 않음(원칙 2 서비스 순서: 은행 38 이 가장 적음). 쇼핑몰 노드 메모리는 F05-P 가 이미 있다 |

## 4. 인과 사슬 (코드와 인프라 위치)

1. **작업 시작**(계기): tb-w2 `sudo systemd-run --unit=oradata-backup --description="Oracle datafile backup" --collect --property=Nice=10 --property=IOSchedulingClass=idle --property=RuntimeMaxSec=600 /bin/sh -c 'mkdir -p /var/backups/oradata && cd /opt/local-path-provisioner/pvc-899a2f44-…_rca-testbed-banking_oracledata-testbed-oracle-0 && { tar -cf - --exclude=FREE/FREEPDB1/users01.dbf FREE | zstd -q -12 --long=31 -T1 -o /var/backups/oradata/oradata-<날짜>.tar.zst & zstd -q -12 --long=31 -T1 -o /var/backups/oradata/users01-<날짜>.dbf.zst FREE/FREEPDB1/users01.dbf; wait; }'`. PV 디렉터리는 매번 PVC 에서 찾는다(`scripts/scenarios/profiles/host_job_executor.py` `datadir()`, PV 가 한 번 2c369013 → 899a2f44 로 다시 만들어졌다). 명령 모양은 104 에서 작은 표본으로 돌려 두 출력이 나오고 tar 에서 users01.dbf 가 빠지는 것을 확인했다.
2. **메모리 증가**: zstd 1.5.5 장거리 모드 창 2^31. 흐름마다 입력이 지나가며 창이 차 상주 메모리가 약 2.3 GiB 까지 큼(§9 실측). 유닛은 `system.slice` 아래라 MemoryMax 가 없고(systemd 기본), 파드가 아니라 kubelet 축출 대상이 아니다.
3. **노드 메모리 고갈**: tb-w2 MemTotal 8,131,780 kB(7,941 MiB), 66 이전 뒤 여유 2,257~2,904 MiB, 스왑 없음, `vm.overcommit_memory=1`. kubelet 축출 기본값(memory.available<100Mi, `/var/lib/kubelet/config.yaml` 에 축출 설정 없음, evictionPressureTransitionPeriod 0s).
4. **노드가 파드를 내줌**:
   - kubelet 축출: 요청량을 넘긴 파드를 넘긴 양 순으로 고른다. 2026-10-16 cgroup 실측에서 Oracle(`kubepods-burstable-pod85163790…`) memory.current 4,226,756,608, inactive_file 1,428,115,456 → 작업 집합 약 2.8 GiB 대 요청 2Gi(`core-banking/k8s/10-oracle.yaml:43-49`). 다른 banking 파드는 요청 아래(transfer 520MB/512Mi, account 491MB, ledger 440MB, api 315MB, Kafka 648MB/768Mi). 그래서 Oracle 이 먼저다. 축출하면 Oracle 의 익명, 공유(SGA 약 1.5 GiB), 캐시 메모리가 함께 풀린다.
   - 커널 전역 OOM: 증가가 kubelet 주기(10초)보다 빨라 커널이 먼저 메모리를 다 쓰면 커널이 고른다. 점수는 대략 oom_score_adj + RSS/총메모리×1000 이고, 버스터블 파드의 oom_score_adj 는 1000 − 1000×요청/용량이다. 그래서 KCM 노드 에이전트(kcm-node-agent-j7sj4, 요청 50Mi, 작업 집합 약 66MB, 점수 약 1000)와 banking 자바 서비스(요청 512Mi, 약 936 + 40~66 = 976~1002)가 먼저이고, Kafka(약 901 + 80), nginx(약 992, RSS 수 MB), Oracle 프로세스(약 742 + 공유 메모리 몫)가 뒤, 호스트 작업 zstd(0 + 약 300)가 마지막이다. F05-P 의 SystemOOM 도 kcm-agent 와 java 가 희생자였다.
5. **피해**: Oracle 이 빠지면 account, transfer 의 DB 연결이 실패하고, 자바 서비스가 죽으면 이체 경로가 끊긴다. 어느 쪽이든 api 가 이체를 502 로 답하고(`core-banking/api-service/.../client/AccountClient.java:28-58`, `core-banking/account-service/.../client/TransferClient.java:28-58`), commerce-payment 의 정산 이체가 실패해(`commerce/payment-service/.../client/BankingTransferClient.java:37-58`) commerce checkout 이 실패한다. 압박이 이어지는 동안 다시 뜬 파드도 메모리가 모자란다.
6. **끝**: 나머지 흐름이 약 183초, users01 흐름이 약 420초에 끝난다(RuntimeMaxSec 600 이 상한). cleanup 이 유닛을 멈추고 출력 파일 둘과 디렉터리를 지운다.

## 5. 원인 규정 (원칙 5, 6)

- **근본(`root_cause.target_id`)**: `tb-w2`(target_kind node). 결함 보유 대상은 그 노드의 일상 백업 작업이다: 대상 크기에 비례해 메모리를 쓰는 압축을 상한 없이 서비스 노드에서 돌린다. Oracle 과 자바 서비스는 평소와 같은 메모리로 정당한 요청을 처리했을 뿐이다(원칙 6: C 를 낸 B 가 아니라 B 를 일으킨 A). 원본이 지목한 층위('유지보수 작업이 서버 메모리를 거의 다 씀')와 같다.
- **계기(`trigger_target_id`)**: 비움. 작업 시작과 메모리 고갈이 같은 노드다.
- **부분 점수(`scoring.partial`)**: `banking-oracle`, `testbed-oracle`(빠진 DB 만 본 답), `core-banking-transfer`, `core-banking-account`, `core-banking-api`(죽거나 502 를 낸 서비스만 본 답).
- **코드 위치(`code_anchor`)**: Oracle 의 노드 고정과 요청, 한도(축출 순서의 근거), 자바 서비스의 요청, 한도(OOM 순서의 근거), 502 전파 경로(TransferClient, AccountClient, BankingTransferClient). 정답이 코드라는 뜻이 아니라 피해 경로의 위치다.
- 채점 입도는 F15-P, F52-R, F44-P 처럼 노드 이름이다. 관제 AI 에게 zstd 의 장거리 창 같은 도구 내부를 요구하지 않는다(원칙 5): 요구하는 결론은 "tb-w2 에서 백업 작업(zstd, tar)이 노드 메모리를 다 써 banking 파드가 축출, OOM 으로 빠졌다"이고, 모두 호스트 데이터로 읽힌다.

## 6. 감지, 피해 판정, RCA 증거 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | banking api, account 오류율과 502 급증, ERROR 로그 급증, KCM 파드 재시작이나 축출, SMS 노드 메모리 사용률 급등 | 별도 규칙 없이 이벤트, 인시던트 생성(녹화에서 확인) |
| 피해 판정(러너) | 동반 부하 이체(step transfer) 5xx 비율(loadgen business_5xx_rate) | 평시 0, 고장 중 0.5 이상(3틱) |
| RCA 증거 | SMS 메모리, 프로세스 목록(zstd 둘, tar), syslog 의 sudo systemd-run 과 유닛 시작, KCM 노드 SystemOOM 이나 노드 메모리 축출, 빠진 파드가 자기 한도 아래였다는 KCM 파드 메모리 | §7 표 |

## 7. 관측 근거 표 (119 실데이터, 평시 조회 2026-10-16)

핵심 증거는 SMS 호스트 지표, syslog, KCM 이벤트와 파드 지표다. 표본 데이터(트레이스 10%, APM 지표)는 피해의 모양에만 쓰고 원인 증명에 쓰지 않는다.

| 증거 | 119 위치 | 조회와 결과(평시) |
|---|---|---|
| 노드 메모리 | VM `sms.memory.utilization`, `sms.memory.total_size`{host_name="192.168.200.137"} | total_size 가 10-13 까지 11.65 GiB(109 의 VM), 10-15 부터 7.76 GiB(66 의 VM). 66 이전 뒤(2026-10-15 03:00Z~) `total_size × (1 − utilization/100)` 1분 표본 2,004개: 최소 2,257, p05 2,631, p50 2,700, p95 2,780, 최대 2,904 MiB. 고장 중 100% 근처 기대 |
| 범인 프로세스와 크기 | VM `sms.process.memory_rss_size{host_name="192.168.200.137", name=…, pid=…}`(프로세스 66개) | `topk(8, …)` 1위 ora_dbw0_free 1,039MB, ora_scmn_free 877MB, java 636MB. 고장 중 name=zstd 둘이 각 약 2.3 GiB 로 상위 기대 |
| 대상 읽기 | VM `sms.process.io_read_bytes`(name 별), `sms.disk.read_bytes{device="sda"}` | `topk(3, …)` oracle_142156_f 351,881,203 등 수집 중. 고장 중 name=tar 기가바이트 단위 기대 |
| 계기(유닛 시작) | CH `lucida.syslog_local` hostname='tb-w2', app_name in ('sudo','systemd') | 1일 동안 systemd 1,695줄(예: 'Starting sysstat-collect.service - system activity accounting tool...' 142줄), sudo 'nkia : PWD=/home/nkia ; USER=root ; COMMAND=/usr/bin/ctr -n k8s.io images ls -q' 105줄(10일). 같은 경로로 'COMMAND=/usr/bin/systemd-run --unit=oradata-backup …' 와 'Started oradata-backup.service - Oracle datafile backup.' 기대 |
| 같은 계기의 로그 감시 사본 | CH `lucida.lucida_logs_local` service_name='lucida_sms_agent', log_attributes['file_path'] in ('/var/log/syslog','/var/log/auth.log') | tb-w2 syslog 2,720줄, auth.log 283줄(3일) 수집 중 |
| 노드 수준 메모리 부족 기록 | CH `lucida.kcm_events_local` reason='SystemOOM'(object_kind Node) | 선례: tb-w1 2026-09-12 06:11 'System OOM encountered, victim process: java, pid: 847575', 'victim process: kcm-agent'(F05-P). 고장 중 같은 꼴 기대 |
| 노드 메모리 축출 | CH `lucida.kcm_events_local` reason in ('EvictionThresholdMet','Evicted') | 같은 경로의 선례는 ephemeral-storage 뿐(tb-w3 2026-08-22 'Attempting to reclaim ephemeral-storage'), 메모리 축출 선례 0건. 축출 경로로 가면 'Attempting to reclaim memory' 와 testbed-oracle-0 Evicted 기대(녹화에서 확인) |
| 빠진 파드가 자기 한도 아래 | VM `kcm.pod.mem_usage`, `kcm.pod.mem_limit`, `kcm.pod.container_restart_count`, `kcm.pod.container_oom_killed` | 수집 중(파드 단위). 고장 중 빠진 파드의 사용량 < 한도 기대. KCM 노드 에이전트가 희생되면 그 사이 KCM 지표가 비므로 SMS 가 노드 메모리 증거를 맡는다 |
| 커널 OOM 줄(있으면) | CH `lucida.syslog_local` app_name='kernel' | 90일 'Out of memory%' 0건. F05-P 의 SystemOOM 때도 커널 줄 없이 syslog 가 끊겼다. 컨테이너 한도 OOM 줄('Memory cgroup out of memory: Killed process … (java)')은 tb-w1, tb-w3 에 있다. 전역 OOM 줄은 남으면 보조 증거일 뿐 필수가 아니다 |
| 피해 | CH `lucida.lucida_logs_local` core-banking-api 32,983줄/일, core-banking-account 23,118줄/일(전수), APM 서버 스팬 | 고장 중 이체 502, Oracle 축출 시 JDBC 연결 실패 로그 기대 |

녹화 검증에서 확인할 것: ① zstd 둘이 프로세스 목록 상위에 잡혔는지, ② 축출과 전역 OOM 중 어느 쪽이 났는지(KCM SystemOOM 이나 Evicted), ③ KCM 노드 에이전트가 희생되었는지(F05-P 처럼 kcm-agent 가 먼저 죽으면 그동안 KCM 파드, 노드 지표와 이벤트 일부가 빈다. 이 시나리오의 메모리 증거는 SMS 호스트 에이전트가 맡고, SMS 에이전트는 호스트 프로세스라 oom 점수가 낮아 뒤에 고른다), ④ syslog 가 끊기지 않았는지(끊겼다면 유닛 시작 줄이 끊기기 전에 들어왔는지).

## 8. 감별 (must_support, must_rule_out, contrast_with)

정답지 `scenario-metadata.json` F80-R 에 그대로 적었다. 가르는 근거만 모은다.

| 짝 | 겹치는 증상 | 가르는 관측 근거 |
|---|---|---|
| F05-P | 노드 메모리 고갈, SystemOOM | 다른 노드(tb-w1, 쇼핑몰). F05-P 의 점유 프로세스는 SMS 에 name=python3 로 잡히고 계기 흔적은 syslog sudo 'bash -s -- run F05-P memhog …' 다. F80-R 은 이름 있는 일상 작업(systemd 유닛 oradata-backup 시작, 'Oracle datafile backup')이고, 점유 프로세스의 정체가 백업 도구(zstd 둘, tar)이며 Oracle 데이터 파일 읽기가 함께 보이고, 먼저 빠지는 것이 DB(Oracle) 다 |
| F15-P | 같은 노드 tb-w2 정답 | F15-P 는 CPU+메모리 동시 압박에 축출 없이 느려짐. F80-R 은 CPU 는 2코어 남짓이고 메모리가 마르며 축출이나 OOM, 5xx |
| F25-H | DB 가 빠져 도메인 전체 실패 | F25-H 는 DB 컨테이너 자신의 한도 OOMKill. F80-R 의 Oracle 은 한도(4Gi) 아래에서 노드 메모리 부족으로 빠진다(노드 SystemOOM 이나 노드 메모리 축출) |
| F35-R | banking DB 접속 실패 | F35-R 은 Oracle 이 떠 있고 ORA-28000. F80-R 은 Oracle 파드가 빠지거나 앱 컨테이너가 죽음 |
| F70-R, F63-R | banking 자바 서비스가 죽고 502 | F70-R 은 힙 고갈(OutOfMemoryError, GC 로그), F63-R 은 컨테이너 한도. F80-R 의 자바 컨테이너는 한도 아래에서 노드 수준 OOM 이나 축출 |
| F10-P, F10-H | 같은 노드 자원 계열, 디스크 IO | 백업이 디스크를 읽지만 IO 우선순위 idle 이고 디스크 대기 시간이 피해를 설명할 만큼 오르지 않음(§3 후보 2 실측) |

## 9. 러너 판정 조건, 강도, 부하 계산 (원칙 9)

**작업의 메모리와 속도(실측, 2026-10-16 66 tb-ops, zstd 1.5.5 -12 --long=31 -T1)**

| 입력 | 최대 RSS | 걸린 시간 | 속도 |
|---|---|---|---|
| 실제 users01.dbf 앞 2,600 MiB(tb-w2 에서 읽어 옴) | 2,395,520 kB(2,339 MiB) | 3분 35초 | 12.7 MB/s |
| 실제 나머지 디렉터리 tar(users01.dbf 제외, 3,934,085,120 바이트) | 2,404,096 kB(2,348 MiB) | 3분 3초 | 21.5 MB/s |
| 참고: /dev/urandom 2,600 MiB | 2,439,168 kB | 39초 | |
| 참고: tar(/usr, /var/lib/docker) 앞 2,500 MiB | 2,425,856 kB | 2분 8초 | 약 20 MB/s |

(1회차 평가 지적 반영: 앞서 시트가 "약 2.4 GiB"로 적은 2,439,168 kB 는 2,382 MiB = 2.33 GiB 였고, 속도 실측도 Oracle 데이터 파일이 아니었다. 위 두 줄이 실제 파일 실측이다.)

**노드의 여유**: tb-w2 지표는 2026-10-13 까지 109 의 VM(MemTotal 11.65 GiB)이고 10-15 부터 66 의 VM(7.76 GiB)이다. 1회차 평가의 7일 분포(중앙 48.3%)는 두 VM 이 섞인 값이라, 66 이전 뒤만 본다: 1분 표본 2,004개에서 여유 최소 2,257, p05 2,631, p50 2,700, p95 2,780, 최대 2,904 MiB. kubelet 이 보는 available(MemFree + Inactive(file))은 2026-10-16 약 2.1 GiB 이고, 압박 중에는 커널이 파일 캐시를 회수하므로 대략 "여유 − 작업"으로 움직인다.

**시간에 따른 작업 크기와 넘침**(여유 p50 2,700 MiB 기준, 두 흐름 합계는 창이 차기 전 초당 약 33 MiB 증가):

| 시작 뒤 | users01 흐름 | 나머지 흐름 | 합계 | 여유 − 합계(kubelet available 근사) |
|---|---|---|---|---|
| 60초 | 약 730 MiB | 약 1,230 MiB | 약 1,960 MiB | 약 +740 MiB |
| 85초 | 약 1,030 | 약 1,740 | 약 2,770 | 약 −70(문턱 100Mi 아래, 축출 시작) |
| 100초 | 약 1,210 | 약 2,348(창 참) | 약 3,560 | 약 −860 |
| 170~183초 | 약 2,339(창 참) | 약 2,348(끝나기 직전) | 약 4,687 | 약 −1,990 |
| 183~420초 | 약 2,339 | 끝 | 약 2,339 | 약 +360(혼자서는 여유의 87%) |

preflight 문턱: 여유 ≤ 3,584 MiB(작업 최대 4,687 MiB 보다 1 GiB 이상 작음)일 때만 주입하므로 어떤 허용 상태에서도 작업이 여유를 1.1 GiB 이상 넘는다. 여유 ≥ 1,536 MiB 도 요구해 이미 압박 중인 노드에는 넣지 않는다. 66 이전 뒤 모든 표본(2,257~2,904)이 이 띠 안이다. 109 때처럼 노드 메모리가 커지면(여유 약 4 GiB) preflight 가 주입을 거부한다(운에 맡기지 않고 막는다).

**피해 계산**: 약 85초부터 kubelet available 이 100Mi 아래로 내려가고, 합계가 더 커지는 동안(약 183초까지) 노드는 Oracle 축출(약 4 GiB 회수)이나 커널 OOM(KCM 노드 에이전트, 자바 컨테이너 하나당 0.3~0.6 GiB)으로 메모리를 내준다. 넘침이 최대 약 2 GiB 라 축출이면 Oracle 하나로 충분하고, 커널 OOM 이면 KCM 에이전트와 자바 컨테이너 몇 개가 필요하다. 축출된 Oracle 은 StatefulSet 이 곧 다시 만들지만 약 183초까지는 메모리가 모자라 다시 빠지거나 기동하지 못하고, 그 뒤 기동과 인스턴스 복구가 붙는다. 자바 컨테이너는 재기동에 95~130초(66 실측, host.image 주석)가 걸린다. 그래서 이체 실패는 약 85초부터 약 4~5분까지 이어지고, 동반 부하 이체 5xx 비율은 0.5 를 넘는다.

**압박이 짧을 때**: 압축 속도가 실측보다 빠르면(예: 나머지 흐름이 두 배 빠름) 넘침 구간이 짧아진다. 그래도 넘침이 시작되는 순간 파드 하나 이상이 빠지고, 빠진 파드의 회복(Oracle 기동과 복구, 자바 기동 95~130초)이 판정 창을 덮는다. 넘침이 아예 없을 만큼 빠르려면 나머지 흐름이 users01 흐름이 1 GiB 남짓을 읽기 전에 끝나야 하는데(약 50 MB/s 이상), 실제 파일 실측 21.5 MB/s 와 거리가 멀다. 그래도 피해가 모자라면 검증 하네스가 보강을 내고 운영 하네스가 조정한다.

**노드 붕괴 위험**: F05-P 는 메모리 점유가 여유 바로 아래(91%)에서 머물며 캐시를 갉아먹는 동안 노드가 응답을 잃은 적이 있다(7번 중 2번). 이 작업은 여유를 확실히 넘어 kubelet 축출이나 커널 OOM 이 결정적으로 메모리를 되찾게 하고, 축출 대상(Oracle)이 넘침보다 큰 메모리를 돌려준다. nginx(요청 64Mi)는 OOM 순서에서 KCM 에이전트, transfer, account 다음 무리라 넘침 약 2 GiB 로는 대개 그 앞에서 멈추고, 죽어도 재기동이 몇 초라 entry 0 이 2틱 이어질 가능성은 낮다. 노드 NotReady 는 중단 조건이다.

**판정 조건(컨트롤러 `F80-R`)**

- 주입: host.job 1단 고정(approved-fixed-f80-r), min_hold 3m, settle 30s, timeout 10m. 동반 부하 load.north_south core-banking transfer-heavy-surge.js 5rps(경사 1m, 유지 13m, 내림 15s). max_injection_duration 20m.
- success: 동반 부하 이체 5xx 비율 ≥ 0.5, 3틱 연속.
- must_rule_out: 동반 부하가 끊김(achieved_rps < 1.25, 2틱). oracle-down 배제는 두지 않는다(Oracle 축출이 예상 전파).
- abort: entry_status == 0(필수), tb-w2 NotReady, 2틱.
- recovery: target_health 200(형식), transfer 파드 준비, transfer 가용 1, Oracle 준비, 기준선 이체 5xx < 0.05, 2틱, 15m.
- preflight: 도구(tar, zstd, systemd-run), Oracle 파드가 tb-w2, users01.dbf ≥ 2 GiB, /var/backups 여유 ≥ 20 GiB(kubelet 디스크 문턱과 멀리), MemAvailable 1,536~3,584 MiB, banking 정상, 유닛이 없고 출력 디렉터리가 없음. 실제 66 에서 preflight 와 recovery 를 읽기만으로 돌려 둘 다 통과했다(2026-10-16, 문턱을 더한 뒤에도 다시 돌려 통과).
- cleanup: 유닛 멈춤, 그 유닛이 쓴 `/var/backups/oradata/*.zst` 와 디렉터리만 삭제(preflight 가 디렉터리 없음을 확인), banking Deployment 다섯과 Oracle StatefulSet 준비 대기(최대 420초).
- 안전: RuntimeMaxSec 600 이 러너가 cleanup 을 못 하더라도 작업을 끝낸다. 출력은 루트 파일 시스템 여유 79G 중 약 2.0 GB(실측 압축률 users01 3.6:1, 나머지 7.6:1)다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2026-09-20 공식 상태 페이지)와 요소별 대응표가 있고 기전이 같다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다: 묶음 A 10(8.5%), 정답 위치 노드, 디스크 8(6.8%), 결제 경로 14(12.0%), 은행 38→39. 어느 축도 20% 아래(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점으로 확인됐다: tb-w2 systemd 일시 유닛과 Oracle PV 디렉터리, kubelet 축출 설정, cgroup 메모리 실측, 피해 경로 코드 위치(§4) (G1)
- [x] 근본 원인의 흔적이 119 에 쌓이는 경로를 평시 조회로 확인했다: SMS 메모리와 프로세스(VM), syslog sudo, systemd(CH), KCM SystemOOM(CH)(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: SMS 호스트 지표, syslog, KCM 이벤트와 파드 지표, 전수 로그(§7) (원칙 3)
- [x] 계기의 흔적이 조회됐다: 유닛을 띄운 sudo 명령 줄과 systemd 유닛 시작 줄이 같은 경로로 평시 수집됨. 인공 지연을 쓰지 않는다(§7) (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 노드 메모리, 범인 프로세스, 노드 수준 OOM, 축출 기록. 도구 내부나 코드 결함 추론을 요구하지 않는다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다: 근본 tb-w2, 계기 비움, 부분 점수 Oracle 과 하류 서비스(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 실행기 인자, 원격 명령, 유닛 이름, 파일 이름 어디에도 없다(실행기 테스트가 확인). 동반 부하의 scenario_tag 는 다른 시나리오와 같이 tb-runner 의 k6 태그에만 있다 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 날 계산이 있다: 실제 파일 실측으로 작업 최대 약 4,687 MiB 대 66 여유 2,257~2,904 MiB, preflight 문턱으로 넘침 1.1 GiB 이상 보장, 동반 부하 5rps(상한 180 안)(§9) (원칙 9)
