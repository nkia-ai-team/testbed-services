---
title: 테스트베드 배포 runbook (109 kubeadm)
status: Active
owner: project
last_reviewed: 2026-07-13
tags:
  - runbook
  - testbed
  - deploy
summary: 109 KVM 클러스터에 3도메인 테스트베드를 배포·검증하는 실측 절차. 환경 실값, 신규 VM 전제조건, 트러블슈팅 포함.
---

# 테스트베드 배포 runbook (109 kubeadm)

> 2026-07-13 commerce 실배포로 검증된 절차다. 여기 적힌 값은 전부 실값이며,
> 인프라 상세(VM 인벤토리·브리지·br_netfilter)는 `spec-testbed-design.md` §5 참조.

## 1. 환경 실값 (배포에 필요한 전부)

| 항목 | 값 | 근거/출처 |
|---|---|---|
| 빌드·오케스트레이션 호스트 | `192.168.200.109` (root, 워크스테이션 ssh 키 등록됨) | ARM64 GB10, docker 있음, ansible 없음 |
| kubeconfig | `109:/root/tb-kubeconfig` | kubeadm v1.31 클러스터(tb-cp + tb-w1/w2/w3) |
| VM ssh | `ssh -i /root/.ssh/tb_key nkia@192.168.122.<NAT-ip>` (NOPASSWD sudo) | 인벤토리 `109:/root/tb-inventory.txt` |
| 워커 NAT IP | w1=`.184`(commerce) w2=`.11`(food) w3=`.14`(banking) | 노드 라벨 `domain=` 적용됨 |
| OTLP 수집 경로 | manifest 가 downward API 로 **노드 로컬** `http://$(status.hostIP):4317` 구성 — 각 VM의 OTel 에이전트(lucida-otel-supervisor, OpAMP 관리)가 수신 | **VM에 OTel 에이전트 설치 필수**(미설치 시 앱은 정상, 텔레메트리만 유실). AP collector 직접 전송(`230.104:4317`)은 에이전트 설치 전 임시 우회였음. ※ 6565는 구 Polestar10(범위 밖) |
| `POLESTAR_ORG_ID` | `69731678b56620b247fb279a` | lucida ClickHouse `otel_traces_local`의 실사용 organizationId (변경 시 재확인: §5 쿼리) |
| deploy env 파일 | `109:/root/deploy-env.sh` | 위 값 + IMPORT_SSH_NODES/KEY export 모음 |
| `IMPORT_SSH_NODES` | `"nkia@192.168.122.184 nkia@192.168.122.11 nkia@192.168.122.14"` | manifest에 nodeSelector가 없어 **3개 워커 전부**에 이미지 필요 (§6 미결) |
| `IMPORT_SSH_KEY` | `/root/.ssh/tb_key` | |

## 2. 신규 VM/클러스터 전제조건 (한 번만, 재생성 시 반복)

2026-07-13 배포에서 실제로 빠져 있어 pod가 못 뜬 항목들:

1. **StorageClass**: kubeadm에는 기본 프로비저너가 없어 PVC(postgres/kafka/oracle)가
   전부 Pending이 된다.
   ```bash
   kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml
   kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
   ```
   K8s 1.31은 기본 SC를 기존 PVC에 소급 적용하므로 Pending PVC도 자동 회복된다.
2. **OTel javaagent hostPath**: 모든 앱 manifest가 `/opt/polestar10/apm`(hostPath,
   type: Directory)을 마운트한다. 각 워커 VM에 jar를 배포해야 한다(원본은 109에 있음):
   ```bash
   for ip in 192.168.122.184 192.168.122.11 192.168.122.14; do
     ssh -i /root/.ssh/tb_key nkia@$ip "sudo mkdir -p /opt/polestar10/apm"
     scp -i /root/.ssh/tb_key /opt/polestar10/apm/opentelemetry-javaagent.jar nkia@$ip:/tmp/otel.jar
     ssh -i /root/.ssh/tb_key nkia@$ip "sudo mv /tmp/otel.jar /opt/polestar10/apm/"
   done
   ```
   누락 증상: pod가 `ContainerCreating`에 고착, describe에 `FailedMount ... /opt/polestar10/apm is not a directory`.
3. **호스트 109 브리지 포워딩**: `tb-br0-forward.service`+`.timer`가 enabled인지 확인
   (`systemctl is-active tb-br0-forward.timer`). 없으면 AP↔VM(200.135~139) 통신이 죽는다
   — 원인·해법은 `spec-testbed-design.md` §5.2.

## 3. 배포 절차 (도메인당 ~15분)

```bash
# 0) 코드 동기화 (워크스테이션에서)
rsync -az --delete --exclude target/ --exclude .git/ \
  ~/project-2025/testbed-services/ root@192.168.200.109:/root/testbed-services/

# 1) 109에서 도메인별 실행 (nohup — ssh 끊겨도 지속)
ssh root@192.168.200.109
source /root/deploy-env.sh
cd /root/testbed-services/<domain> && nohup bash k8s/build-and-deploy.sh > /root/deploy-<domain>.log 2>&1 &
```

스크립트가 하는 일: ①이미지 ARM 빌드(도메인당 5~10개) → ②`IMPORT_SSH_NODES`의 각
워커 containerd로 ssh 임포트 → ②.5 DB init ConfigMap을 `db/*.sql`에서 `--from-file`
생성 → ③envsubst+`kubectl apply` → ④rollout 대기(180s 타임아웃 — 첫 배포는 워밍업이
느려 timed out이 떠도 이후 Ready가 되면 정상, §4로 확인).

주의: Oracle(core-banking) 첫 기동은 이미지 pull(~2GB)+DB 초기화로 수 분 걸린다
(startupProbe가 15s×60으로 잡혀 있음).

## 4. 배포 후 검증 (commerce 기준, 도메인별 동일 패턴)

```bash
export KUBECONFIG=/root/tb-kubeconfig
kubectl -n rca-testbed-<domain> get pods            # 전부 1/1 Running
kubectl -n rca-testbed-<domain> logs deploy/testbed-loadgen --tail=5   # Request Failed 없어야 함
curl -s -o /dev/null -w "%{http_code}" http://192.168.122.184:30080/api/products?size=3  # 200 (commerce nginx NodePort)
```

OTLP 유입 확인 (워크스테이션에서):
```bash
docker exec lucida-clickhouse clickhouse-client -q "
  SELECT resource_attributes['lucida.groupId'] grp, resource_attributes['service.name'] svc, count() spans
  FROM lucida.otel_traces_local WHERE timestamp > now() - INTERVAL 15 MINUTE
  GROUP BY grp, svc ORDER BY spans DESC LIMIT 20 FORMAT TSV"
```
기대: 도메인 전 서비스가 `grp=<domain>`으로 span을 적재.

## 5. 트러블슈팅 (전부 실제 발생 이력)

| 증상 | 원인 | 조치 |
|---|---|---|
| PVC Pending, pod `unbound PersistentVolumeClaims` | StorageClass 없음 | §2-1 |
| `FailedMount ... /opt/polestar10/apm` | otel jar hostPath 미준비 | §2-2 |
| Kafka pod 기동 실패 `nonroutable meta-address 0.0.0.0` | advertised.listeners에 CONTROLLER 누락 | 수정 커밋 f745fb3 반영 확인 |
| gateway 프록시 응답 전부 거부 `too many transfer encodings` | 하류 Transfer-Encoding 헤더 재전파 | 수정 커밋 dd485ca 반영 확인 |
| banking 앱 `ORA-00942 ... "BANKING"."<table>" does not exist` | gvenzl initdb.d 가 **SYSDBA 로 실행**돼 테이블이 SYS 소유로 생성됨 (Oracle 로그에 ORA-04089 동반) | init.sql/seed-all.sql 헤더의 `ALTER SESSION SET CONTAINER/CURRENT_SCHEMA` 유지(f6834a0). 재초기화는 ConfigMap 재생성 후 oracle pod+PVC 삭제(§2-1의 SC가 재프로비저닝) |
| loadgen `dial i/o timeout` (배포 직후 수 분) | 서비스 워밍업 중 | 정상 — Ready 후 자동 회복 |
| org id를 모름/바뀜 | — | §4 ClickHouse 쿼리로 실사용 organizationId 역추출 |
| 원격 프로세스 정리 시 ssh 세션이 끊김 | `pkill -f build-and-deploy`가 자기 명령줄과 매치 | `pkill -f "[b]uild-and-deploy"` 패턴 사용 |

## 6. 미결

- **nodeSelector 미적용**: 설계는 "워커=도메인 전용"(노드 라벨 존재)이나 manifest에
  nodeSelector가 없어 pod가 아무 워커에나 뜬다. 그래서 현재는 이미지를 3워커 전부에
  임포트한다. manifest에 `nodeSelector: {domain: <domain>}` 추가 시 도메인별 장애
  격리(VM 단위 CPU throttle 등)가 성립하고 임포트도 해당 워커 1대로 줄일 수 있다.
- DB StatefulSet이 local-path PV를 쓰므로 pod가 노드를 옮기면 데이터가 새로 시작된다
  (테스트베드 성격상 허용, 단 장기 캡처 중 재스케줄은 주의).
