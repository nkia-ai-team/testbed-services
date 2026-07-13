# testbed-services

RCA 테스트베드용 MSA 서비스 monorepo. 각 서브폴더 = 한 도메인(domain) testbed.
lucida-next / OpenTelemetry 기반으로 관측되는 Spring Boot 마이크로서비스 모음이며, 각 도메인은 kubeadm 클러스터 위에 배포되어 RCA 시나리오 검증 대상이 된다.
인프라(배포 자동화)가 본 레포 URL 을 `app_repo` 로 받아 클러스터에 배포하고, 각 도메인 폴더의 `k8s/build-and-deploy.sh` 를 진입점으로 호출한다.

---

## 1. 레포 구조

```
testbed-services/
├── commerce/                         # 도메인 1(플래그십) — 전자상거래, 10 service + api-gateway, PostgreSQL 16 + Kafka + Redis(cart 캐시)
│   ├── api-gateway/                  # BFF: 라우팅·토큰검증·집계
│   ├── {order,product,inventory,payment,notification,user,cart,pricing,shipping}-service/
│   ├── commerce-common/              # 공유 DTO + outbox 템플릿
│   ├── loadgen/                      # k6 상주 부하 (diurnal)
│   ├── db/                           # init schemas + 대량 시드 (주문 50k, 90일 diurnal)
│   ├── k8s/                          # K8s manifest + build-and-deploy.sh
│   └── pom.xml
├── food-delivery/                    # 도메인 2 — 음식배달, 5 service, MySQL 8.0 + Kafka
│   ├── {order,restaurant,dispatch,payment,notify}-service/
│   ├── shop-common/                  # 공유 DTO + outbox(제네릭 베이스)
│   ├── loadgen/
│   ├── db/                           # 대량 시드 (주문 20k)
│   ├── k8s/
│   └── pom.xml
├── core-banking/                     # 도메인 3 — 코어뱅킹, 4 service, Oracle 23ai Free + Kafka
│   ├── {api,account,transfer,ledger}-service/
│   ├── shop-common/
│   ├── loadgen/
│   ├── db/                           # 대량 시드 (이체 20k·원장 39k)
│   ├── k8s/
│   └── pom.xml
├── social-feed/                      # legacy — 신설계(core-banking)로 대체 예정. SNS, 4 service, MySQL 8.0
│   └── (구조는 위와 동일)
└── README.md (이 파일)
```

각 도메인 폴더는 **자기 완결적**이다 — 자체 manifest + 자체 build script. 인프라는 `git clone` 후 **`<domain>/k8s/build-and-deploy.sh`** 만 호출한다.

commerce 는 기존 전자담배 쇼핑몰 예제 프로젝트를 커머스 일반 도메인으로 리네이밍한 것이다. social-feed 는 신설계상 폐기 예정이며, 그 자리를 core-banking(신규)이 대체한다.

---

## 2. 레포 규약

각 도메인 폴더가 따르는 구조·명명 규약. (아래는 현재 레포의 상태를 설명한다.)

### 2-1. 도메인 폴더 구성

각 도메인 폴더는 다음으로 구성된다:

| 파일 | 역할 |
|------|------|
| `k8s/build-and-deploy.sh` | Docker 이미지 빌드 + K3s ctr import + `kubectl apply -f k8s/` 까지 책임지는 진입점. 인프라가 `chdir=<domain>/` 에서 `bash k8s/build-and-deploy.sh` 를 호출한다. |
| `k8s/*.yaml` | Namespace, ConfigMap/Secret, Deployment, Service 등 K3s 리소스. |

`k8s/` 안의 manifest 는 적용 순서를 파일명 prefix 로 표현한다:

| 파일 | 비고 |
|------|------|
| `k8s/00-namespace.yaml` | Namespace 정의를 가장 먼저 적용되도록 `00-` prefix. |
| `k8s/01-secrets.yaml`, `02-configmaps.yaml` | 의존 리소스. |
| `k8s/10-*` (DB, `11-redis` 비동기 있으면), `20-*` (앱) | 번호순으로 적용되어 의존 순서를 표현. |
| `README.md` (도메인 폴더 안) | 빌드 전제, 서비스 표, 시나리오 등. |

### 2-2. 명명 규약

- **Namespace**: **`rca-testbed-<domain>`** (commerce/food/banking). 도메인마다 namespace 를 분리해 cross-domain 호출을 FQDN 으로 명시한다.
- **Image tag**: `<domain>-<svc>:latest`. ARM 호스트에서 `imagePullPolicy: Never` + 로컬 ctr import 모델을 쓴다.
- **Container ports**: 8080부터 서비스별로 순차 부여 (Spring Boot 기본 8080).

### 2-3. lucida 태깅 규약 (golden 바인딩 계약)

모든 서비스는 아래 두 env 를 deployment 에 세팅해 lucida-next 가 관측을 target 에 귀속시키도록 되어 있다.

| env | 값 | 비고 |
|-----|----|----|
| `OTEL_SERVICE_NAME` | `<domain>-<svc>` (예: `commerce-order`, `food-delivery-order`, `core-banking-transfer`) | 등록된 application target 의 `meta.service_name` 과 **정확일치**해야 APM trace golden signal 이 바인딩됨. |
| `OTEL_RESOURCE_ATTRIBUTES` | `lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=<domain>,lucida.target_id=${<SVC>_TARGET_ID}` | `POLESTAR_ORG_ID`/`<SVC>_TARGET_ID` 는 placeholder — `build-and-deploy.sh` 의 envsubst 가 배포 시점에 치환(org id·target UUID는 lucida 등록 후 발급받아 주입). |

DB/pod/host 의 `lucida.target_id` 는 collector(db_poll/kcm/sms)가 자기 등록정보로 emit 하므로 앱이 넣지 않는다. 앱이 책임지는 것은 `OTEL_SERVICE_NAME` 과 앱 JVM 메트릭용 `lucida.target_id` 뿐이다.

### 2-4. cross-domain 호출

- 현재 고정 경로 1개: **commerce-payment → core-banking-transfer**. `POST /api/transfers`, FQDN `testbed-transfer.rca-testbed-banking.svc.cluster.local:8082`.
- namespace 가 분리되어 있으므로 도메인 내부 URL(`service-config` configmap)과 별개로 cross-domain URL 이 설정되어 있다(commerce 의 `BANKING_TRANSFER_URL` 참고).
- RestClient + OTel javaagent 사용 시 W3C traceparent 가 자동 전파되어 같은 trace 로 이어진다.

---

## 3. 표준 hostPath 명세

lucida-next 용 OTel javaagent 는 이미지에 굽지 않고, 인프라가 호스트에 공급한 jar 을 hostPath 로 마운트해 `JAVA_TOOL_OPTIONS` 로 주입한다. 모든 도메인 manifest 가 이 경로를 hostPath 로 마운트한다.

| 산출물 | 호스트 경로 | 비고 |
|--------|-----------|------|
| OTel javaagent (`opentelemetry-javaagent.jar`) | `/opt/polestar10/apm/` | 인프라가 hostPath 로 공급하는 디렉토리. Pod 가 read-only 로 마운트. |

> 그 외 host-level collector(DB/pod/host 관측)는 호스트 단에서 실행되므로 Pod 가 마운트할 필요 없다.

### 3-1. 표준 manifest snippet (Deployment)

```yaml
spec:
  template:
    spec:
      volumes:
        - name: apm-agent
          hostPath:
            path: /opt/polestar10/apm   # 인프라가 OTel javaagent 를 공급하는 디렉토리
            type: Directory
      containers:
        - name: my-service
          volumeMounts:
            - name: apm-agent
              mountPath: /opt/apm        # 컨테이너 내부 경로 (자유)
              readOnly: true
          env:
            - name: JAVA_TOOL_OPTIONS
              value: >-
                -javaagent:/opt/apm/opentelemetry-javaagent.jar
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "${OTLP_ENDPOINT}"   # envsubst placeholder — build-and-deploy.sh 가 deploy-time 치환. 인프라가 OTLP_ENDPOINT env 주입 (수동 실행 시 직접 export). hardcoded IP / Jinja 표현 사용 X.
            - name: OTEL_EXPORTER_OTLP_PROTOCOL
              value: "grpc"
            - name: OTEL_SERVICE_NAME
              value: "<domain>-<svc>"
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: "lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=<domain>,lucida.target_id=${<SVC>_TARGET_ID}"
```

---

## 4. 빌드/배포 흐름 (참고)

```
[인프라 배포 자동화]
  └─ service-k8s 단계
       ├─ git clone <app_repo>            # 본 레포 URL
       ├─ k8s/build-and-deploy.sh         # ← 각 도메인이 책임
       │     ├─ docker build (서비스 N개)
       │     ├─ docker save | k3s ctr images import
       │     └─ kubectl apply -f k8s/
       └─ kubectl rollout status (deploy + sts)
```

본 monorepo 변경 + push → 인프라 재배포로 새 버전이 클러스터에 반영된다.

### 4-1. 배포 대상 인프라 (2026-07-09)

현재 배포 대상은 호스트 `192.168.200.109`(ARM64 GB10 / Ubuntu 24.04) 위
KVM/libvirt 게스트 VM들로 구성된 **kubeadm 단일 클러스터**다. 게스트는 libvirt
NAT(`192.168.122.0/24`). 도메인 워크로드는 worker 노드에 `domain` 라벨로
배치된다.

| VM | IP | 사양 | 역할 |
|----|----|------|------|
| tb-cp | 192.168.122.77 | 2vCPU/4GB | kubeadm control-plane |
| tb-w1 | 192.168.122.184 | 4vCPU/12GB | worker · `domain=commerce` |
| tb-w2 | 192.168.122.11 | 4vCPU/12GB | worker · `domain=food-delivery` |
| tb-w3 | 192.168.122.14 | 4vCPU/12GB | worker · `domain=core-banking` |
| tb-runner | 192.168.122.206 | 2vCPU/4GB | 조종석(부하/장애 주입/mock, 클러스터 외부) |

앱 이미지는 arm64로 빌드한다(`eclipse-temurin` 멀티아치). 각 worker는 SMS
호스트 풀 + KCM 노드 역할도 겸한다. 관측 평면(lucida-next)은 109 밖 별도 AP
서버다.

---

## 5. 관련 레포

| 레포 | 역할 |
|-----|------|
| [nkia-ai-team/rca-scenario-runner](https://github.com/nkia-ai-team/rca-scenario-runner) | 장애 시나리오 실행 도구. **호스트 docker compose 단일 인스턴스로 운영** — testbed 외부에서 K3s API 를 호출해 시나리오를 실행한다. testbed K3s 안 컴포넌트가 아니며 관측 대상도 아니라 self-contained 통합은 불필요하다. |

---

## 6. 변경 이력

- **2026-07-13**: **대폭 확장(spec-testbed-expansion.md) 구현 완료** — ① commerce 플래그십:
  5→10 서비스+api-gateway(BFF), checkout 5-hop 오케스트레이션, Kafka+outbox 백본(Redis Streams
  대체), Resilience4j 전면, 상주 배치 4종, 25테이블+주문 50k diurnal 시드, §7 read API, k6
  loadgen 상주. ② food-delivery·core-banking 에 템플릿 복제(각자 Kafka·배치·read API·대량
  시드·loadgen). ③ core-banking **MariaDB→Oracle 23ai Free 전환**(cross-domain 계약 불변).
  ④ cross-domain 2경로(결제 동기 + 정산 배치). ⑤ 배포 스크립트 kubeadm 정정, DB init
  ConfigMap `--from-file` 생성 전환. 도메인별 상세는 각 폴더 README 참조.
- **2026-07-09**: 3도메인 재설계 — 전자담배 쇼핑몰 예제 프로젝트 → **commerce**(PostgreSQL 리네이밍), food-delivery **PostgreSQL→MySQL 전환 + Redis 비동기(notify-service 신규)**, **core-banking**(MariaDB, 신규) 추가. namespace 규약을 `rca-testbed-<domain>` 으로 통일하고 lucida 태깅(`OTEL_SERVICE_NAME`/`OTEL_RESOURCE_ATTRIBUTES`)을 전 도메인에 적용. social-feed 는 legacy 로 남기고 신규 배포 대상에서 제외.
- **2026-07-09**: WPM/Polestar10 계측 제거, 관측을 OTel(lucida-next) 단일 경로로 일원화.

원본: [BangSungjoon/plopvape-shop](https://github.com/BangSungjoon/plopvape-shop) — 본 레포 신설 이후 archive 상태(실제 push 안 함). 본 레포가 canonical truth.
