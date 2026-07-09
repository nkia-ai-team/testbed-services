# testbed-services

RCA 테스트베드용 MSA 서비스 monorepo. 각 서브폴더 = 한 도메인(domain) testbed.
NKIAAI-540 의 Ansible 플레이북이 본 레포 URL 을 `app_repo` 변수로 받아 K3s 위에 배포한다.

---

## 1. 레포 구조

```
testbed-services/
├── commerce/                         # 도메인 1 — 전자상거래, 5 service, PostgreSQL 16 (+ Redis Streams)
│   ├── {order,product,inventory,payment,notification}-service/   # Spring Boot 모듈
│   ├── shop-common/
│   ├── db/                           # init schemas + seed
│   ├── k8s/                          # K8s manifest + build-and-deploy.sh
│   └── pom.xml
├── food-delivery/                    # 도메인 2 — 음식배달, 5 service, MySQL 8.0 + Redis(비동기, notify-service)
│   ├── {order,restaurant,dispatch,payment,notify}-service/
│   ├── shop-common/
│   ├── db/
│   ├── k8s/
│   └── pom.xml
├── core-banking/                     # 도메인 3(신규) — 코어뱅킹, 4 service, MariaDB 11 + Redis Streams
│   ├── {api,account,transfer,ledger}-service/
│   ├── shop-common/
│   ├── db/
│   ├── k8s/
│   └── pom.xml
├── social-feed/                      # legacy — 신설계(core-banking)로 대체 예정. SNS, 4 service, MySQL 8.0
│   └── (구조는 위와 동일)
└── README.md (이 파일)
```

각 도메인 폴더는 **자기 완결적**이다 — 자체 manifest + 자체 build script. Ansible 은 `git clone` 후 **`<domain>/k8s/build-and-deploy.sh`** 만 호출한다.

commerce 는 기존 `plopvape-shop`(전자담배 쇼핑몰)을 커머스 일반 도메인으로 리네이밍한 것이다. social-feed 는 신설계상 폐기 예정이며, 그 자리를 core-banking(신규)이 대체한다.

---

## 2. 등록 가이드 — 새 도메인 추가

### 2-1. 필수 산출물

신규 도메인 `mydomain` 을 추가하려면 `mydomain/` 폴더 안에 최소한 아래 둘이 있어야 한다:

| 파일 | 역할 |
|------|------|
| `k8s/build-and-deploy.sh` | Docker 이미지 빌드 + K3s ctr import + `kubectl apply -f k8s/` 까지 책임지는 진입점. Ansible 이 `chdir=<domain>/` 에서 `bash k8s/build-and-deploy.sh` 호출. |
| `k8s/*.yaml` | Namespace, ConfigMap/Secret, Deployment, Service 등 K3s 리소스. **본 README §3 의 표준 hostPath 를 따를 것.** |

### 2-2. 권장 산출물

| 항목 | 비고 |
|------|------|
| `k8s/00-namespace.yaml` | Namespace 정의를 가장 먼저 적용되도록 `00-` prefix. |
| `k8s/01-secrets.yaml`, `02-configmaps.yaml` | 의존 리소스. |
| `k8s/10-*` (DB, `11-redis` 비동기 있으면), `20-*` (앱) | 적용 순서가 번호순이라 prefix 로 의존 순서 표현. |
| `README.md` (도메인 폴더 안) | 빌드 전제, 서비스 표, 시나리오 외부 링크 등. |

### 2-3. Naming

- **Namespace**: **`rca-testbed-<domain>`** 규약(commerce/food/banking). 도메인마다 namespace 를 분리해 cross-domain 호출을 FQDN 으로 명시한다. (과거 plopvape-shop 시절 `rca-testbed` 단일 namespace 를 썼던 것은 폐기된 규약이다 — 신규 도메인은 반드시 `rca-testbed-<domain>` 을 따를 것.)
- **Image tag**: `latest` 권장 (109/타 ARM 호스트에서 `imagePullPolicy: Never` + 로컬 ctr import 모델). 이미지명 규약: `<domain>-<svc>:latest`.
- **Container ports**: 8080부터 서비스별로 순차 부여 (Spring Boot 기본 8080).

### 2-4. lucida 태깅 규약 (golden 바인딩 계약)

모든 서비스는 아래 두 env 를 deployment 에 세팅해야 lucida-next 가 관측을 target 에 귀속시킨다.

| env | 값 | 비고 |
|-----|----|----|
| `OTEL_SERVICE_NAME` | `<domain>-<svc>` (예: `commerce-order`, `food-delivery-order`, `core-banking-transfer`) | 등록된 application target 의 `meta.service_name` 과 **정확일치**해야 APM trace golden signal 이 바인딩됨. |
| `OTEL_RESOURCE_ATTRIBUTES` | `lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=<domain>,lucida.target_id=${<SVC>_TARGET_ID}` | `POLESTAR_ORG_ID`/`<SVC>_TARGET_ID` 는 placeholder — `build-and-deploy.sh` 의 envsubst 가 배포 시점에 치환(org id·target UUID는 lucida 등록 후 발급받아 주입). |

DB/pod/host 의 `lucida.target_id` 는 collector(db_poll/kcm/sms)가 자기 등록정보로 emit 하므로 앱이 넣지 않는다. 앱이 책임지는 것은 `OTEL_SERVICE_NAME` 과 앱 JVM 메트릭용 `lucida.target_id` 뿐이다.

### 2-5. cross-domain 호출

- 현재 고정 경로 1개: **commerce-payment → core-banking-transfer**. `POST /api/transfers`, FQDN `testbed-transfer.rca-testbed-banking.svc.cluster.local:8082`.
- namespace 가 분리되어 있으므로 도메인 내부 URL(`service-config` configmap)과 별개로 cross-domain URL 을 추가해야 한다(commerce 의 `BANKING_TRANSFER_URL` 참고).
- RestClient + OTel javaagent 사용 시 W3C traceparent 가 자동 전파되어 같은 trace 로 이어진다.

---

## 3. 표준 hostPath 명세

NKIAAI-540 Ansible 플레이북이 호스트에 깔아두는 폴스타 에이전트 위치. **모든 도메인 manifest 는 이 경로를 hostPath 로 마운트해야 한다.**

| 에이전트 | 호스트 경로 | 주요 산출물 |
|---------|-----------|-----------|
| WPM | `/opt/polestar10/wpm/` | `wpmagent.jar`, `wpmagent-{service}.conf` (서비스별 1개) |
| APM (OTel) | `/opt/polestar10/apm/` | `opentelemetry-javaagent.jar` |
| KCM | (호스트 K3s 에이전트, manifest 무관) | — |
| SMS | `/opt/polestar10/sms/` (host systemd) | manifest 무관 |

> **WPM/APM 만 manifest hostPath 로 노출**. KCM·SMS·DPM·NMS 는 호스트 단에서 실행되므로 Pod 가 마운트할 필요 없음.

### 3-1. 표준 manifest snippet (Deployment)

```yaml
spec:
  template:
    spec:
      volumes:
        - name: apm-agent
          hostPath:
            path: /opt/polestar10/apm   # ← 표준 (NKIAAI-570)
            type: Directory
        - name: wpm-agent
          hostPath:
            path: /opt/polestar10/wpm   # ← 표준 (NKIAAI-570)
            type: Directory
      containers:
        - name: my-service
          volumeMounts:
            - name: apm-agent
              mountPath: /opt/apm        # 컨테이너 내부 경로 (자유)
              readOnly: true
            - name: wpm-agent
              mountPath: /opt/wpm        # 컨테이너 내부 경로 (자유)
              readOnly: true
          env:
            - name: JAVA_TOOL_OPTIONS
              value: >-
                -javaagent:/opt/apm/opentelemetry-javaagent.jar
                -javaagent:/opt/wpm/wpmagent.jar
                -Dwpm.config=/opt/wpm/wpmagent-<service>.conf
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "${OTLP_ENDPOINT}"   # envsubst placeholder — build-and-deploy.sh 가 deploy-time 치환. ansible 이 OTLP_ENDPOINT env 주입 (수동 실행 시 직접 export). hardcoded IP / Jinja 표현 사용 X.
            - name: OTEL_EXPORTER_OTLP_PROTOCOL
              value: "grpc"
            - name: OTEL_SERVICE_NAME
              value: "<domain>-<svc>"
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: "lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=<domain>,lucida.target_id=${<SVC>_TARGET_ID}"
```

### 3-2. WPM service-별 conf 명명 규칙

NKIAAI-540 의 `agent-wpm` 역할이 `testbed_services` 변수에 나열된 각 서비스마다 conf 파일을 생성한다 (`/opt/polestar10/wpm/wpmagent-<service>.conf`). manifest 의 `-Dwpm.config=...` 인자가 이 파일을 참조한다.

| 변수 | 기본값 | 비고 |
|-----|-------|------|
| `polestar10_wpm_collector_udp_port` | `31002` | 검증값 (109 wpmagent-*.conf 와 일치) |
| `polestar10_wpm_collector_tcp_port` | `31005` | 검증값 |

---

## 4. 빌드/배포 흐름 (참고)

```
[NKIAAI-540 Ansible 플레이북]
  └─ role: service-k8s
       ├─ git clone <app_repo>            # 본 레포 URL
       ├─ k8s/build-and-deploy.sh         # ← 각 도메인이 책임
       │     ├─ docker build (서비스 N개)
       │     ├─ docker save | k3s ctr images import
       │     └─ kubectl apply -f k8s/
       └─ kubectl rollout status (deploy + sts)
```

본 monorepo 변경 + push → Ansible 재실행으로 새 버전이 K3s 에 반영된다.

---

## 5. 관련 레포

| 레포 | 역할 |
|-----|------|
| [nkia-ai-team/claude-code-skills](https://github.com/nkia-ai-team/claude-code-skills) — `nkia-ai-tools/infra/testbed/` | NKIAAI-540 Ansible 플레이북 본체 (에이전트 6종 설치 + `service-k8s` 호출) |
| [nkia-ai-team/rca-scenario-runner](https://github.com/nkia-ai-team/rca-scenario-runner) | 장애 시나리오 실행 도구 (NKIAAI-498). **호스트 docker compose 단일 인스턴스로 운영** — testbed 외부에서 K3s API 를 호출해 시나리오 실행. testbed K3s 안 컴포넌트가 아니며 KCM/APM/WPM 모니터링 대상도 아니라 self-contained 통합 불필요 (NKIAAI-570 결정). |
| [nkia-ai-team/polestar-agents-binaries](https://github.com/nkia-ai-team/polestar-agents-binaries) | WPM/APM/SMS 에이전트 바이너리 GitHub Releases |

---

## 6. 변경 이력

- **2026-07-09**: 3도메인 재설계 — plopvape-shop → **commerce**(PostgreSQL 리네이밍), food-delivery **PostgreSQL→MySQL 전환 + Redis 비동기(notify-service 신규)**, **core-banking**(MariaDB, 신규) 추가. namespace 규약을 `rca-testbed-<domain>` 으로 통일하고 lucida 태깅(`OTEL_SERVICE_NAME`/`OTEL_RESOURCE_ATTRIBUTES`)을 전 도메인에 적용. social-feed 는 legacy 로 남기고 신규 배포 대상에서 제외.
- 본 레포 신설 (2026-04-29) 시점에 plopvape-shop 의 manifest hostPath 가 `/home/nkia/{wpm,apm}-agent/` 에서 **`/opt/polestar10/{wpm,apm}/`** 로 이전되었다(NKIAAI-570). 신규 도메인 등록 시 반드시 이 표준을 따를 것.

원본: [BangSungjoon/plopvape-shop](https://github.com/BangSungjoon/plopvape-shop) — 본 레포 신설 이후 archive 상태(실제 push 안 함). 본 레포가 canonical truth.
