# 시나리오 재설계 트래커 (Phase 1: 설계 완성)

순서: ①설계완성 → ②배관완성 → ③실주입 스모크검증 → ④일괄 배치실행
상태 범례: ⬜미착수 / 🔧설계중 / 📝설계완료(gap있음) / ✅설계완료(gap없음)

## 신규 2차 (8, 07-24) — 커버리지 확장. 설계 완료 (coverage-matrix 잔여 빈칸 + fault-surface 미소진 표면)
| id | 시나리오 | 도메인 | 패턴 | ①② | ③ | ④ | 배관 gap |
|---|---|---|---|:--:|:--:|:--:|---|
| F20-R | order 통계/검색 슬로우쿼리 → 공유 PG 교차오염 | commerce | P8(신설:슬로우쿼리) | ✅ | 🟡 | 🟡 | slowquery.js 신설 + DB-side CPU/쿼리시간 query(교차오염 ground-truth) |
| F20-P | transfer stats trunc 풀스캔(인덱스 실재·무력화) → 이체 경합 | banking | P8 | ✅ | 🟡 | 🟡 | slowquery.js + hikari_pending(F19-P 공유) |
| F20-Q | 무제한 조회 unpaged + DATE() filesort → 힙(1Gi)·MySQL 압박 | food | P8 | ✅ | 🟡 | 🟡 | slowquery.js. 힙은 기존 container_memory로 관측 가능(3종 중 최선) |
| F21-Q | food order Tomcat 200 포화 (Hibernate 지연획득으로 Hikari 미보유 구간) | food | P2 | ✅ | 🟡 | 🟡 | active_requests query + create-heavy surge(λ≈50/s). 정밀 delay injector 부재=캘리브레이션 게이트 |
| F21-P | banking api Tomcat 200 포화 (DataSource 부재=유일 무풀 서비스) | banking | P2 | ✅ | 🟡 | 🟡 | active_requests + transfer-heavy surge(λ≈25/s, 총 250rps > max_rps 180) |
| F22-P | transfer Hikari(15) 자연 고갈 — 무관 계좌까지 전멸 (F01-P와 answer-key 분리) | banking | P1+P3 | ✅ | 🟡 | ❌ | **진짜 능력갭**: hot-account transfer-heavy surge 부재(현 banking 부하로 재현 불가). query는 F19-P/F18-P 공유 |
| F23-R | 재입고 배치(ReconciliationBatch) 정지 → 재고소진 → 409 조용마비 | commerce | P5류(배치) | ✅ | ❌ | 🟡 | **하드블로커=③**: inventory stock/RESTOCK rate/409 버킷 query 전무. ④는 env(interval-ms) stopgap 실재(검증용 한정), 정식=F19-Q 공유 훅 |
| F24-Q | restaurant 최상류 차단 → order 전량 502 (retry 3x 증폭 fan-in) | food | P4류(fan-in) | ✅ | 🟡 | 🟡 | F02-P 경로 재사용(30181 flood, 신규 executor 0) + cb=restaurant query. calibration만 |

요지: 8개 전부 ①②(코드앵커+정답) ✅ · 실소스 재검증 완료(드리프트: F20 dailyStats 라인, F19 service_name 4곳→정정됨). **7개는 배선/calibration, F22-P만 진짜 능력갭**(banking hot-account surge). 공유 훅 통합안: business.fault stub 승격 1건으로 F19-Q+F23-R 동시 해제(F23 sheet §4-B). 시트: design-F20-slowquery/F21-threads/F22-P/F23-R/F24-Q-sheet.md.

## 신규 (7) — 설계 완료. 4조건: ①코드앵커 ②정답 ③감별 ④주입수단
| id | 시나리오 | 도메인 | 패턴 | ①② | ③ | ④ | 배관 gap |
|---|---|---|---|:--:|:--:|:--:|---|
| F16-H | user-service fail-close → 전도메인 쓰기401 | commerce | P6 | ✅ | 🟡 | 🟡 | write/read status query + gateway CB open + surge step×status |
| F17-P | commerce→transfer 직행 무결성우회(FROZEN) | cross | P7 | ✅ | 🟡 | 🟡 | frozen_bypass/normal_reject rate + integrity_violation_count + dual-arm 스크립트 + ledger cleanup |
| F17-R | banking down → payment @Tx 전체 롤백 | cross | P7+P1 | ✅ | ✅ | ✅ | **승격 완료(07-24, bc2ac2b)** — 재설계 신규 1호, catalog 65·live 32. 러너 관측 allowlist 짝=runner 94bb225(109 배포됨). 109 testbed-services 동기화는 ③스모크 때 전체 일관 배포 |
| F18-P | outbox relay 정지 → 원장/알림 silent lag | banking·food | P5 | ✅ | ✅ | ✅ | **승격 완료(07-24)** — catalog 66·live 33. outbox count+banking kafka lag(runner 674c092)+transfer_2xx 배선, 라이브 env==baseline 검증 |
| F19-P | food 롱tx 풀고갈 (createOrder @Tx내 동기fanout) | food | P1 | ✅ | ✅ | ✅ | **승격 완료(07-24)** — hikari_pending(OTel semconv) 라이브 실증, catalog 68·live 35 |
| F19-Q | food 배차 배치정지 → capacity 소진 503 | food | P3 | ✅ | ❌ | ❌ | **진짜 injector 갭**: @Scheduled 정지 훅 없음. env하향은 §2-A 위배 |
| F19-S | food PG mock 지연 → payment 502 연쇄 | food | P4 | ✅ | ✅ | ✅ | **승격 완료(07-24)** — CB-open 게이트는 상태코드 rate 대체(부품2 폐기 판정 반영) |

요지: **7개 전부 ①②(코드앵커+정답) ✅** — 코드 근거는 탄탄. 남은 건 ③④(관측배선+injector계약)이고, 대부분 **공유 배관**이라 Phase 2에서 묶어 해결. F17-R은 사실상 즉시 승격 가능, F19-Q만 진짜 능력갭.

## Class B 신규 (10, 07-24) — APM 편중 해소 라운드. 인프라 fault-surface 3축 실측(fault-surface-infra-*.md) 기반
| id | 시나리오 | 도메인 | ①② | ③ | ④ | 판정 |
|---|---|---|:--:|:--:|:--:|---|
| F25-R | PG 연결슬롯 고갈(97 vs 풀합95, 전 8스키마 동시) | DPM | ✅ | ❌ | ❌ | **최심 능력갭**: bare 커넥션 injector+pg 접속수 query 둘 다 부재 |
| F25-H | PG OOMKill(512Mi) — 공유 DB blast | DPM·KCM | ✅ | ✅ | 🟡 | **승격 최선두**(관측 완비, executor kind 배선만) |
| F25-S | Kafka 브로커 SPOF(RF=1) — F04-R(consumer)과 available_replicas=0 vs lag 감별 | DPM·KCM | ✅ | 🟡 | 🟡 | 공유 배선 |
| F25-P | Oracle 단일 PDB 포화(FS-12 통합) — cpu-patch는 §2-A 위장 아님·proxy 🟡 | DPM·KCM | ✅ | 🟡 | 🟡 | 공유 배선 |
| F26-R | 노드 상실 blast radius(affinity無, 3도메인 동시) | KCM·SMS | ✅ | 🟡 | ❌ | k8s.node drain executor 갭(R·H 공유) |
| F26-H | DB PV 노드고정→영구 Pending(affinity conflict) | KCM | ✅ | ❌ | ❌ | 동일 갭+Pending사유 query |
| F26-P | hostPath 에이전트 디렉토리 소실(22앱 의존) | KCM·SMS | ✅ | 🟡 | ❌ | host.stress hostpath-disrupt mode 신설 |
| F26-Q | ephemeral 무제한→루트fs 포화→이웃 eviction | SMS·KCM | ✅ | ❌ | 🟡 | watermark 재타깃(배선)+DiskPressure query |
| F27-R | 워커 네트워크 지연(tc netem) — F12-H 상호 감별쌍, F12-R 승계 | APM·KCM·SMS | ✅ | 🟡 | ❌ | host tc executor 갭. **관측평면(br0) 접근 물리적 거부 allowlist 필수** |
| F27-P | NetworkPolicy 크로스도메인 파티션 — F12-P 승계, kube-router 강제 실측 | APM·KCM | ✅ | 🟡 | 🟡 | k8s.netpol 배선(즉시 실현 가능). probe 미차단 live검증 전 golden 금지 |

- **F28(WPM) = 전용 시나리오 없음 판정**(데드락/스레드릭 코드앵커 0 — grep 실증). WPM은 관측 능력: F21·F22·F01·F06-H·F20 6개 감별을 소거법→직접증거로 격상("동일증상 삼각" 해결). F09-H(GC)는 강화 안 됨(정직 음성). 복원 5게이트(G5=OTel 이중계측 충돌 검증, 실패 시 WPM축 보류) design-F28-wpm-sheet.md.
- **F12 재편**: F12-H KEEP 유지(F27-R 감별쌍) · F12-R(CUT)→F27-R 승계 · F12-P(blocked)→F27-P 승계 · F12-G 폐기 유지.
- **F13×3 = WebURL 계열 오배정 → 재설계 대기 강등**(wpmagent는 TTFB/connect 신호 안 줌). **NMS 관측 불가 확정**(SNMP 소스 0) — 네트워크 시나리오는 must_support에 NMS 금지, 소거법 감별.
- **F25 정정(중요)**: k8s.resource·k8s.patch·kafka.control 전부 Deployment 하드코딩 — StatefulSet 미들웨어에 allowlist만으론 불가. kafka.control은 consumer scale0 코드라 브로커 down 재현 불가 → executor kind 일반화 + statefulset_available_replicas가 공유 배선.
- **F26 운영 안전**: drain류는 3도메인 동시 타격 — 전용 클린윈도우·대상노드 라이브 확정(문서 매핑 스테일)·비파괴 drain만·recovery gate 전 namespace 검증·multi_namespace 캡처.

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
- 잔존 48 = KEEP 14 + FIX 27 + 신규 7. + 신규 2차 8(F20~F24) + **Class B 10(F25~F27, 07-24) = 총 66**. 전부 ①코드/인프라앵커 + ②정답 완료(또는 명확한 gap). F28=관측능력(카운트 제외), F13×3은 66에 포함하되 WebURL 재설계 대기.
- 남은 일 = ③④(관측배선+injector) = **Phase 2**.

## Phase 2 백로그 (③④ 배관) — 3버킷
### 버킷1 즉시/경량 (~13): KEEP14는 완료. F17-R(allowlist), F05×3·F09×3·F02-H·F07-H(정답구조화/재앵커,④✅), F03-P·F08-P(라벨만)
### 버킷2 공유 관측배선 (injector 실재, query 신설):
- status-class: loadgen write/read/food_create/transfer_2xx status rate + surge step×status 버킷 → F16-H·F19·F18-P·F04-H·food429 공유
- CB open: gateway_cb_open{user}, cb_open{pg} → F16-H·F19-S
- 무결성/적체: integrity_violation_count, outbox_unpublished_count(selector), hikari_pending, ledger_imbalance
- injector 계약확장(신규코드 아님): ~~mock.expectation food 일반화, k8s.probe/k8s.env allowlist, load.north_south surge 계약~~ **✅완료(07-24)**: F19-P/S(mock food /pay)·F17-R(probe banking transfer)·F18-P(env OUTBOX_RELAY_ENABLED)·load.north_south 4종 등록(executor+profiles.json 양쪽, validate 스모크 통과). 잔여: F17-P dual-arm 스크립트
- 대상: F16-H·F17-P·F18-P·F04-H·F04-P·F19-P/S·F06-P·F15-H/T2·F10×3(+좌표정정)
### 버킷3 진짜 능력갭 (신규 훅/executor/소스변경):
- F19-Q(dispatch @Scheduled 정지 훅) · F14-P(선별유실 injector) · F14-R(중복/유실 fault-proxy) · F15-P(co-residency 보장) · F15-T3/T4(timeline 결속 계약) · F13×3+F12(NMS/WPM 실 executor) · F03-H(합성 엔드포인트 제거)
- (07-24 추가) **스케줄러 정지 훅 통합**: business.fault stub(profiles.json, live_supported=false) 승격 → F19-Q+F23-R 동시 해제(계약 키잉={namespace,deployment,scheduler_method}, F23-R sheet §4-B) · **F22-P**(banking hot-account transfer-heavy surge 신설 — DB_POOL_MAX 하향은 §2-A 위배로 금지)
### 버킷2 추가분 (07-24 Class B 배선):
- executor kind 일반화 deploy→statefulset(F25-H/S/P 공유) + kubernetes.statefulset_available_replicas · kafka broker-down 모드 분기 · host.stress watermark 노드루트fs 재타깃(F26-Q) · k8s.netpol 프로파일+deny manifest(F27-P) · query 신설: pod_unschedulable_reason·volume_mount_failure·node_disk_pressure·pod_evicted_count·apm_cross_domain_edge_error_rate
- **WPM 복원 트랙**(관측 능력, F21·F22·F01 감별 격상): R1 manager_ip→119 · R2 이중 javaagent 부착 · R3 banking conf 신설 · 스모크 5게이트(G5=OTel 충돌 검증이 관문)
### 버킷3 추가분 (07-24 Class B 능력갭):
- k8s.node drain executor(F26-R/H 동시 해제, 비파괴 cordon+drain+uncordon만) · host.stress hostpath-disrupt mode(F26-P, rename+trap) · host tc executor network.host_tc(F27-R, **관측평면 vnet 거부 allowlist 필수**) · bare 커넥션 슬롯 injector+pg_connection_count query(F25-R, 최심)
### 버킷2 추가분 (07-24, 신규 2차 배선):
- slowquery.js 3도메인(F20 공유) + DB-side CPU/쿼리시간 query(F20-R ground-truth) · prometheus.http_server_active_requests(F21 공유) · cb=restaurant(F24-Q, F19-S cb=pg와 공유 신설) · inventory stock/RESTOCK rate/409 버킷 query 3종(F23-R 하드블로커) · surge 변형(create-heavy λ50/transfer-heavy λ25 — F21 캘리브레이션 게이트)

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
