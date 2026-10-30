# 시나리오 화면 이야기(story_ko) 작성 규칙

러너 웹 화면의 시나리오 카드가 보여 주는 사람용 글이다. 정답지 `scripts/scenarios/registry/scenario-metadata.json` 의 `scenarios.<ID>.story_ko` 에 둔다.

- **채점 칸이 아니다.** `description` 등 설명 6칸은 채점 정답지이고 capture 가 녹화본 meta.json 에 싣는다. `story_ko` 는 화면 전용이라 capture 가 싣지 않는다. 이야기를 고치려고 채점 칸을 바꾸지 않는다.
- **모든 시나리오에 있어야 한다.** 정답지 시험(`tests/test_answer_keys.py`)이 칸이 없거나 모양이 틀리면 실패시킨다.
- 카드 머리에는 개요만 보이고, 나머지 넷은 "상세 보기" 안에 개요 다음 순서로 보인다.

## 모양

```json
"story_ko": {
  "overview": "한 문장",
  "injected_fault": "1~2문장",
  "propagation": [
    {"stage": "초기", "text": "1~2문장"},
    {"stage": "확산", "text": "1~2문장"}
  ],
  "root_cause": "1~2문장",
  "design_notes": ["주입: ...", "경로: ..."]
}
```

화면 제목은 순서대로 개요, 주입 장애, 전파 과정, 근본 원인, 설계 근거다.

## 칸별 기준

| 칸 | 길이 | 쓰는 것 |
|---|---|---|
| overview | 한 문장 | "무엇이 고장 나서 무엇이 어떻게 된다". 이 한 문장만 읽어도 이야기가 다 보여야 한다 |
| injected_fault | 1~2문장 | 무엇을, 얼마나 오래 망가뜨리는지만. 실행 세부(클라이언트 파드, 접속 이름 위장, 부하 rps, 램프업)는 design_notes 로 |
| propagation | 2~4단계, 단계마다 1~2문장 | 시간 순서. 단계 이름은 명사형(초기, 확산, 전면 장애, 지속, 회복 등). 시각(약 1분 뒤)과 사람이 관제 화면에서 볼 현상만. 기전 설명은 design_notes 로. 퍼지지 않는 장애면 억지로 단계를 늘리지 않는다 |
| root_cause | 1~2문장 | 근본 원인과, 오류가 크게 보이지만 피해자인 곳을 구분한다 |
| design_notes | 문자열 목록 | "주입: ...", "부하: ...", "경로: ...", "잠금 방식: ..." 처럼 머리말과 콜론. 전문 용어, 설정값, 실행 세부, 녹화 근거는 여기에만 둔다 |

## 표기

- 장비나 서비스가 처음 나올 때 `역할(기술 스택, 관제 대상 이름)` 으로 쓴다. 두 번째부터는 쉬운 이름만 쓴다. 예: "재고 DB(PostgreSQL, PostgreSQL-commerce)", "재고 서비스(Spring Boot, commerce-inventory)", "작업 노드(tb-w1)". 사람이 관제 제품의 장비 목록과 인시던트를 보고 이 시나리오와 이어 볼 수 있게 하려는 것이다.
- 관제 대상 이름은 관측 서버(119) 화면에 나오는 이름을 쓴다. 정답지 scoring 안의 이름(예: commerce-postgres)은 화면에 나오지 않으니 쓰지 않는다.
  - DB: PostgreSQL-commerce, Oracle-corebanking, MySQL-fooddelivery
  - 캐시: redis
  - 서비스: commerce-{gateway, order, cart, pricing, product, inventory, payment, shipping, notification, user}, core-banking-{api, account, transfer, ledger}, food-delivery-{order, restaurant, dispatch, payment, notify}
  - 노드: tb-w1, tb-w2, tb-w3, 클러스터: prod-k8s-01
- 기술 스택: 서비스는 Spring Boot, DB 는 PostgreSQL, Oracle, MySQL, 메시지는 Kafka, 캐시는 Redis, 은행 진입은 nginx. 관제 대상 이름이 없는 것은 `(Kafka)` 처럼 스택만 쓴다.
- 평서문("~다")과 쉬운 말을 쓴다. 예: "커넥션 풀 고갈"은 "DB 연결 10개가 다 찬다", "readiness 실패"는 "준비 안 됨 상태가 되어 트래픽에서 빠진다", "circuit breaker open"은 "차단기를 연다".
- 금지: 가운뎃점, 줄표, 비밀값. 명사구 뒤는 콜론, 나열은 쉼표로 쓴다.
- 다른 시나리오 ID 는 overview, injected_fault, propagation, root_cause 에 쓰지 않는다. 대조가 꼭 필요하면 design_notes 에만 쓴다.
- design_notes 에 강도 수치를 쓰면 정답지 시험이 계약 사다리와 대조한다. 계약과 다른 수치를 쓰지 않는다.

## 사실 근거

지어내지 않는다. 시각과 수치는 근거에 있는 것만 쓴다.

- **새 시나리오(녹화 전)**: 설계 시트, 정답지 설명 칸과 root_cause, must_support, must_rule_out, controller, profiles.json 의 scenario_parameters, 실행기. propagation 은 설계가 예상하는 모습으로 쓰고 시각은 설계 시트 §9 계산 범위로 쓴다.
- **녹화 뒤**: 검증 보고서(가장 최근 "녹화 가능" 판정)와 녹화본. 관측된 모습이 설계와 다르면 propagation 과 overview 를 **관측된 모습으로** 고친다. 정답지 채점 칸은 고치지 않는다(바꿀지는 사람이 정한다).
