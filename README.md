# testbed-services

RCA 테스트베드용 MSA 서비스 monorepo. 각 서브폴더 = 한 종류의 testbed (예: `plopvape-shop`).
NKIAAI-540 의 Ansible 플레이북이 본 레포 URL 을 `app_repo` 변수로 받아 K3s 위에 배포한다.

---

## 1. 레포 구조

```
testbed-services/
├── plopvape-shop/                    # testbed 1 — 5 service e-commerce
│   ├── {order,product,inventory,payment,notification}-service/   # Spring Boot 모듈
│   ├── shop-common/
│   ├── db/                           # init schemas + seed
│   ├── k8s/                          # K8s manifest + build-and-deploy.sh
│   └── pom.xml
├── <future-testbed-name>/
└── README.md (이 파일)
```

각 testbed 폴더는 **자기 완결적**이다 — 자체 manifest + 자체 build script. Ansible 은 `git clone` 후 **`<testbed>/k8s/build-and-deploy.sh`** 만 호출한다.

---

## 2. 등록 가이드 — 새 testbed 추가

### 2-1. 필수 산출물

신규 testbed `mytestbed` 를 추가하려면 `mytestbed/` 폴더 안에 최소한 아래 둘이 있어야 한다:

| 파일 | 역할 |
|------|------|
| `k8s/build-and-deploy.sh` | Docker 이미지 빌드 + K3s ctr import + `kubectl apply -f k8s/` 까지 책임지는 진입점. Ansible 이 `chdir=<testbed>/` 에서 `bash k8s/build-and-deploy.sh` 호출. |
| `k8s/*.yaml` | Namespace, ConfigMap/Secret, Deployment, Service 등 K3s 리소스. **본 README §3 의 표준 hostPath 를 따를 것.** |

### 2-2. 권장 산출물

| 항목 | 비고 |
|------|------|
| `k8s/00-namespace.yaml` | Namespace 정의를 가장 먼저 적용되도록 `00-` prefix. |
| `k8s/01-secrets.yaml`, `02-configmaps.yaml` | 의존 리소스. |
| `k8s/10-*` (DB), `20-*` (앱) | 적용 순서가 알파벳순이라 prefix 로 의존 순서 표현. |
| `README.md` (testbed 폴더 안) | 빌드 전제, 시나리오 외부 링크 등. |

### 2-3. Naming

- **Namespace**: 짧고 일관된 이름. plopvape-shop 은 `rca-testbed` 사용. 동일 클러스터에 여러 testbed 띄울 거면 testbed 별로 분리.
- **Image tag**: `latest` 권장 (109/타 ARM 호스트에서 `imagePullPolicy: Never` + 로컬 ctr import 모델).
- **Container ports**: 8080 (Spring Boot 기본).

---

## 3. 표준 hostPath 명세

NKIAAI-540 Ansible 플레이북이 호스트에 깔아두는 폴스타 에이전트 위치. **모든 testbed manifest 는 이 경로를 hostPath 로 마운트해야 한다.**

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
              value: "my-service"
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
       ├─ k8s/build-and-deploy.sh         # ← 각 testbed 가 책임
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

## 6. 변경 이력 — 호스트 경로 표준 (NKIAAI-570)

본 레포 신설 (2026-04-29) 시점에 plopvape-shop 의 manifest hostPath 가 `/home/nkia/{wpm,apm}-agent/` 에서 **`/opt/polestar10/{wpm,apm}/`** 로 이전되었다. 신규 testbed 등록 시 반드시 새 표준을 따를 것.

원본: [BangSungjoon/plopvape-shop](https://github.com/BangSungjoon/plopvape-shop) — 본 레포 신설 이후 archive 상태(실제 push 안 함). 본 레포가 canonical truth.
