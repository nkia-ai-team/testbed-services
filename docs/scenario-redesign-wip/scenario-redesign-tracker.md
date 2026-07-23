# 시나리오 재설계 트래커 (Phase 1: 설계 완성)

순서: ①설계완성 → ②배관완성 → ③실주입 스모크검증 → ④일괄 배치실행
상태 범례: ⬜미착수 / 🔧설계중 / 📝설계완료(gap있음) / ✅설계완료(gap없음)

## 신규 (7) — 설계 완료. 4조건: ①코드앵커 ②정답 ③감별 ④주입수단
| id | 시나리오 | 도메인 | 패턴 | ①② | ③ | ④ | 배관 gap |
|---|---|---|---|:--:|:--:|:--:|---|
| F16-H | user-service fail-close → 전도메인 쓰기401 | commerce | P6 | ✅ | 🟡 | 🟡 | write/read status query + gateway CB open + surge step×status |
| F17-P | commerce→transfer 직행 무결성우회(FROZEN) | cross | P7 | ✅ | 🟡 | 🟡 | frozen_bypass/normal_reject rate + integrity_violation_count + dual-arm 스크립트 + ledger cleanup |
| F17-R | banking down → payment @Tx 전체 롤백 | cross | P7+P1 | ✅ | ✅ | 🟡 | **없음(기존 checkout_5xx로 관측)** — k8s.probe allowlist 확장뿐 |
| F18-P | outbox relay 정지 → 원장/알림 silent lag | banking·food | P5 | ✅ | 🟡 | 🟡 | outbox_unpublished_count DB selector + k8s.env 계약 + transfer_2xx query. (lag는 must_rule_out) |
| F19-P | food 롱tx 풀고갈 (createOrder @Tx내 동기fanout) | food | P1 | ✅ | 🟡 | 🟡 | mock.expectation food 일반화(1줄) + food_create_status + hikari_pending query |
| F19-Q | food 배차 배치정지 → capacity 소진 503 | food | P3 | ✅ | ❌ | ❌ | **진짜 injector 갭**: @Scheduled 정지 훅 없음. env하향은 §2-A 위배 |
| F19-S | food PG mock 지연 → payment 502 연쇄 | food | P4 | ✅ | 🟡 | 🟡 | mock.expectation food 일반화(공유) + circuitbreaker_open{cb=pg} + food_create_status |

요지: **7개 전부 ①②(코드앵커+정답) ✅** — 코드 근거는 탄탄. 남은 건 ③④(관측배선+injector계약)이고, 대부분 **공유 배관**이라 Phase 2에서 묶어 해결. F17-R은 사실상 즉시 승격 가능, F19-Q만 진짜 능력갭.

## FIX (30) — 처리 완료. 27 유지 / 3 CUT추가
### 정답 미작성 10 → 처리
- F02-H ✅정답(ClassB/disk, injector 실재) · F04-H ✅정답+unblock(k8s.env) · F04-P ✅정답(④🟡 rate injector) · F15-P ✅정답(merge,④✅) · F15-T3 ✅정답(MERGE 단일사슬) · F15-T4 ✅정답(SPLIT 순차2근본)
- F14-P ✅정답(④❌ 유실 injector 신설) · F14-R ✅정답(④❌ fault-proxy 신설)
- **F03-R → CUT** (실코드에 leak 경로 없음) · **F14-H → CUT** (잠재 correctness 결함 없음, 주입=§2-A위배)
### Class B 재라벨 12 → 처리
- F05×3(KCM)·F09×3(SMS) ✅ (정답 구조화만) · F10×3(disk) injector실재·**좌표정정 2건**+승격 · F13×3(NMS/WPM) **능력갭**
### food-429 착각 3 → 재정의(429→PG 502, F19-S 메커니즘 공유)
- F06-P·F15-H·F15-T2 정답 신작
### 앵커 얕음/env 5 → 처리
- **F02-R → CUT** (인덱스 드롭 물리적 무효과) · F03-P → ClassB/config 재라벨 · F08-P → ClassB/config 재라벨 · F07-H → 재앵커(order 풀10+5hop tx 한계) · F03-H → 재앵커(합성 sleep 제거=소스변경 필요)

## Phase 1 최종 (진짜장애 카탈로그)
- 64 → **CUT 23** (음성13 + 근거없음7 + FIX강등3: F02-R/F03-R/F14-H)
- 잔존 **48 = KEEP 14 + FIX 27 + 신규 7**. 전부 ①코드/인프라앵커 + ②정답 완료(또는 명확한 gap).
- 남은 일 = ③④(관측배선+injector) = **Phase 2**.

## Phase 2 백로그 (③④ 배관) — 3버킷
### 버킷1 즉시/경량 (~13): KEEP14는 완료. F17-R(allowlist), F05×3·F09×3·F02-H·F07-H(정답구조화/재앵커,④✅), F03-P·F08-P(라벨만)
### 버킷2 공유 관측배선 (injector 실재, query 신설):
- status-class: loadgen write/read/food_create/transfer_2xx status rate + surge step×status 버킷 → F16-H·F19·F18-P·F04-H·food429 공유
- CB open: gateway_cb_open{user}, cb_open{pg} → F16-H·F19-S
- 무결성/적체: integrity_violation_count, outbox_unpublished_count(selector), hikari_pending, ledger_imbalance
- injector 계약확장(신규코드 아님): mock.expectation food 일반화(1줄), k8s.probe/k8s.env allowlist, load.north_south entry_url/surge 계약, F17-P dual-arm 스크립트
- 대상: F16-H·F17-P·F18-P·F04-H·F04-P·F19-P/S·F06-P·F15-H/T2·F10×3(+좌표정정)
### 버킷3 진짜 능력갭 (신규 훅/executor/소스변경):
- F19-Q(dispatch @Scheduled 정지 훅) · F14-P(선별유실 injector) · F14-R(중복/유실 fault-proxy) · F15-P(co-residency 보장) · F15-T3/T4(timeline 결속 계약) · F13×3+F12(NMS/WPM 실 executor) · F03-H(합성 엔드포인트 제거)

## KEEP (14) — 그대로 골든
F01-R, F01-H, F01-P, F04-R, F06-R, F06-H, F07-P, F08-H, F08-G, F11-R, F12-H, F15-G, F15-R, F15-T1

## CUT (20)
음성13: F01-G,F02-G,F03-G,F04-G,F05-G,F06-G,F07-G,F09-G,F10-G,F11-G,F12-G,F13-G,F14-G
근거단절/수단없음7: F02-P, F07-R, F08-R, F11-H, F11-P, F12-R, F12-P

## 배관 gap 누적 (Phase 2 대상) — 신규 7개 기준, 대부분 공유
### A. 관측 배선 (진짜 병목, 공유)
- status-class 버킷: `loadgen.write/read_step_status_rate`(401), `loadgen.food_create_status_rate`(503), `loadgen.transfer_2xx_rate` + surge.js step×status 버킷팅 (401·food503은 checkout_5xx가 못 봄)
- CB open: `prometheus.gateway_circuitbreaker_open{cb=user}`, `prometheus.circuitbreaker_open{cb=pg}`
- 무결성/적체: `database.integrity_violation_count`, `database.outbox_unpublished_count`(DB selector), `prometheus.hikari_pending_connections`
### B. injector 계약 확장 (신규코드 아님)
- mock.expectation food namespace 일반화(-n guard 1줄) — F19-P/S 공유
- k8s.probe APPROVED_TARGETS 확장(transfer) — F17-R
- k8s.env 계약 확장(OUTBOX_RELAY_ENABLED) — F18-P
- load.north_south: entry_url 30282(F17-P) / banking·food surge 계약(F17-R/F18-P/F19)
- F17-P dual-arm frozen-bypass.js 스크립트 + ledger 정정 cleanup
### C. 진짜 능력갭 (신규 훅/executor 필요)
- F19-Q: dispatch @Scheduled 정지 제어 훅
- NMS/WPM 실 executor (네트워크 F12·프로브 F13) — Class B FIX 재라벨 대상과 연동
### D. answer-key 미작성분 (FIX 10 + 기타)
