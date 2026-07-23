# Phase 2 관측 계층 2블로커 — 구현 스펙 (프로토타입 수준, 실적용 금지)

작성: 2026-07-23 · 근거: repo `scripts/scenarios/` 실측 + 러너(GB10 `scenario-runner` 컨테이너) `/app/backend/app/live_probes.py` 실측
스코프: repo = `/home/ydkim/project-2025/testbed-services/` · 러너 = 비-git 배포본(patch 보존)

---

## 0. 확정된 배관 사실 (코드 실측)

**생산자(repo)**: `load_north_south_executor.py` heredoc monitor(134~211행)만이 `-live.json`을 만든다. surge.js 3종은 이미 `tags:{journey,step}` + k6 자동 `status` 태그를 붙인다 — **surge.js 3종 전부 무변경 가능**.

**소비자(러너)**: `_loadgen_observation`(live_probes.py 429~443행)이 SSH로 tb-runner(192.168.122.206) `-live.json`을 cat(`_loadgen_live_document` 1007행) → **query_id 하드코딩 allowlist**(430행 `{"loadgen.achieved_rps","loadgen.checkout_5xx_rate"}`) + **field map**(435~438행)으로 스칼라 추출. freshness ≤30s(1039행), scenario_tag 일치(1033행) 검증. **queries.json 선언만으론 절대 동작 안 함 — 러너 allowlist+map에 없으면 `LiveProbeError`.**

현재 monitor가 쓰는 필드(194~202행): `scenario_id, scenario_tag, achieved_rps, entry_status, checkout_5xx_rate, business_ok, observed_at`.
monitor는 `http_reqs` 중 `tags.step == business_step` **단일 step**만 `checkout_results`(stamp,status)에 축적(171~174행), rate는 `>=500` **단일 버킷**(190~193행).

`business_step`(domain_profile, profiles.json): commerce=`checkout`, food=`create`, banking=`transfer`. 3도메인 surge.js 모두 해당 step 태그 실재 확인.

---

## 블로커 1 — 상태코드 관측 end-to-end (step×status-class 버킷)

### 판정: **YES (repo monitor 확장 + 러너 patch 1건)**

### 1a. 생산자(repo) — `scripts/scenarios/profiles/load_north_south_executor.py` (heredoc monitor)

현재→목표 (monitor는 이미 (stamp,status) 튜플 보유 → 산술만 추가, 성능 영향 없음):

| 지점(행) | 현재 | 목표 |
|---|---|---|
| 190~193 | `checkout_5xx_rate = sum(status>=500)/len` 단일 버킷 | window 내 `business_step` 샘플을 3-class로: `business_2xx_rate`(200–299), `business_4xx_rate`(400–499), `business_5xx_rate`(500–599), `business_nonok_rate`(4xx+5xx). 각 = class수/len, len=0이면 0.0 |
| 194~202 (document) | 기존 7필드 | 위 4필드 추가. `checkout_5xx_rate`는 `business_5xx_rate` 값으로 **유지**(하위호환·기존 query 불변) |
| 140 (argv unpack) | `source, output, scenario_id, business_step = sys.argv[1:]` | `read_step` 5번째 인자 추가(read_step 없는 도메인은 빈 문자열) |
| 171~174 (append) | step==business_step만 append | step==read_step 샘플을 별도 deque `read_results`에도 append |
| (신규) | — | `read_2xx/4xx/5xx/nonok_rate` 동일 산출, read_step 빈값이면 필드 생략 or null |

**모든 필드는 도메인-불가지(domain-agnostic).** food의 `business_step=create`, banking의 `business_step=transfer`라 `business_*_rate`가 그대로 create/transfer 버킷이 된다 → 도메인 구분은 **러너 query_id 이름**이 담당(아래 1c).

`read_step` 배선(read_step_status_rate 필요할 때만):
- `profiles.json > load.north_south > domain_profiles.<url>`에 `read_step` 키 추가: commerce=`list`, food=`menu`, banking=`get`(태그 실재 확인).
- `build_invocation`(load_north_south_executor.py 260~263행): argv에 `domain_profile["read_step"]` 추가.
- heredoc arg parse(94행): `read_step="${15}"`.
- monitor 기동(218행): `python3 "$monitor" "$samples" "$live" "$scenario_id" "$business_step" "$read_step"`.

**최소안**: read_step를 빼면 domain_profile·argv·94·218행 무변경 → `business_*_rate`만 산출. 요청 4 query_id 중 3개(write_step/food_create/transfer_2xx)를 커버. `read_step_status_rate`만 미지원. (권고: 최소안 먼저, read_step는 실제로 read-path 시나리오가 생길 때 증분.)

### 1b. query 선언(repo) — `scripts/scenarios/registry/queries.json`

`queries` dict에 4개 신규 키 추가(기존 loadgen 항목 스키마와 동일: adapter=`loadgen_summary`, value_type=`number`, freshness_sec=30):

```
"loadgen.write_step_status_rate":  {selector:"business.nonok.rate"}
"loadgen.read_step_status_rate":   {selector:"read.nonok.rate"}
"loadgen.food_create_status_rate": {selector:"business.5xx.rate"}
"loadgen.transfer_2xx_rate":       {selector:"business.2xx.rate"}
```
(selector는 선언용 라벨 — 실제 field 매핑은 러너가 소유. 기존 `checkout.5xx.rate` 라벨 관례 그대로.)

### 1c. 소비자(러너 patch) — `live_probes.py::_loadgen_observation` (429~443행)

| 지점(행) | 변경 |
|---|---|
| 430~432 (allowlist set) | 4 query_id 추가: `loadgen.write_step_status_rate, loadgen.read_step_status_rate, loadgen.food_create_status_rate, loadgen.transfer_2xx_rate` |
| 435~438 (field map) | 매핑 추가 → `write_step_status_rate:"business_nonok_rate"`, `read_step_status_rate:"read_nonok_rate"`, `food_create_status_rate:"business_5xx_rate"`, `transfer_2xx_rate:"business_2xx_rate"` |
| 442~443 (range check) | 현 `field=="checkout_5xx_rate"` 하드코딩 → **모든 rate 필드 [0,1] 검증**으로 일반화(`field.endswith("_rate")` and value>1 → raise) |

query_id→field 진단 의미:
| query_id | field | 상승 조건 | 대상 시나리오 |
|---|---|---|---|
| write_step_status_rate | business_nonok_rate | checkout 401(fail-close) 또는 5xx | F16-H(401), F19-S(502) |
| read_step_status_rate | read_nonok_rate | read-path 저하 | (read-path 결함) |
| food_create_status_rate | business_5xx_rate | order create 503(courier 포화) | food 503 |
| transfer_2xx_rate | business_2xx_rate | transfer 2xx **하락**(계좌 FROZEN 등) | banking |

### 1d. runner-patch 규약

**형식(README 확정): 대상 파일 전체 root 스냅샷 patch**(정본 부재로 최소 diff 기준선 없음). 신규 patch = `scripts/runner-patches/2026-07-23-status-class-observation.patch`, 수정된 `backend/app/live_probes.py`(+queries 소비 무관) 스냅샷 1파일.
- 생성: 109 컨테이너에서 `live_probes.py` 편집 → 체크아웃 트리에 복사 → `git add` → `git format-patch`(빈 트리 기준) 또는 기존 v2 patch에 이어붙임.
- 적용: `git apply --3way scripts/runner-patches/2026-07-23-status-class-observation.patch` (대상 파일 존재 시). 컨테이너 반영은 `docker cp`/재기동.
- 롤백: `git apply -R <patch>` 또는 직전 스냅샷(2026-07-20-v2)의 live_probes.py로 복원.

### 1e. 적용 순서
1. repo: monitor 확장(1a 최소안) + queries.json 4항(1b) → 커밋.
2. 러너: live_probes.py 3지점 편집(1c) → 컨테이너 반영 → **동일 편집을 patch 스냅샷으로 보존**(1d).
3. controllers.json `observations[]`에 신규 query_id 배선(시나리오별, 별도 작업).

### 1f. 리스크
- (중) `business_nonok_rate` composite가 4xx·5xx를 합침 → 401과 502를 하나로 봄. 세분 필요 시 러너 map을 `business_4xx_rate`/`business_5xx_rate`로 분리(필드는 이미 다 emit).
- (하) read_step 미배선 시 read_step_status_rate 미지원(최소안 명시적 제약).
- (하) monitor drain 성능: (stamp,status) 튜플 재사용이라 신규 fsync 없음 → F07-H 교훈 무관.
- (하) repo↔러너 2트랙 드리프트: field명이 monitor(repo)와 map(러너)에 이중 존재 → 이름 불일치 시 `LiveProbeError`. patch 규약이 유일 보루.

---

## 블로커 2 — food 503 (3-URL "문제")

### 판정: **YES — 단, 계획서 리스크2는 부정확. 라우팅 문제 아니라 볼륨 문제.**

### 2a. 실측 정정
- food surge.js는 **이미 ORDER_URL/api/orders에 POST(step:'create')** 한다(132~136행, `orderJourneyBounded`).
- executor는 `--env RESTAURANT_URL=<entry_url 30181>` **단일 주입**(load_north_south_executor.py 214행). 그러나 `ORDER_URL`(기본 30180)·`DISPATCH_URL`(기본 30182)은 **k6 default가 곧 정확한 NodePort** → order create 경로는 **오늘도 구동됨**.
- `business_step=create` → monitor가 이미 step=='create' 필터. **블로커1 버킷을 얹으면 `loadgen.food_create_status_rate`는 즉시 값을 낸다.**
- 진짜 제약: `orderJourneyBounded`가 **10% 가중 + menu 실패 시 early-return** → create 표본 희박, courier pool 포화(503)가 좀처럼 안 터짐.

### 2b. 가장 작은 변경 (503을 신뢰성 있게 만들려면)
계약 다중URL 확장 **불필요**(defaults 정확). 대신 order 볼륨을 올리는 신규 스크립트 1개:

| 조각 | 파일 | 성격 |
|---|---|---|
| order-heavy 스크립트 | 신규 `food-delivery/loadgen/order-surge.js` | surge.js 복제 후 여정 가중을 order-create 중심으로(bounded 제거, create ≥70%). RESTAURANT_URL은 menu 조회용(executor가 주입), ORDER_URL default 30180 사용. step 태그 `create` 유지 |
| script 등록 | `profiles.json > load.north_south.parameter_contract.allowed_script_paths` | `/opt/loadgen/food-delivery/order-surge.js` 추가 |
| domain_profile | (무변경) entry_url=30181 프로필의 `business_step=create` 그대로 재사용 |
| tb-runner 배치 | 신규 스크립트를 tb-runner `/opt/loadgen/food-delivery/`에 배포(baseline과 동일 경로 관례) |

- **repo-only**(러너 무관). 관측은 블로커1의 `business_5xx_rate`→`loadgen.food_create_status_rate` 매핑에 종속.
- 대안(더 작음, 신뢰성↓): 신규 스크립트 없이 기존 10% create 표본만으로 `food_create_status_rate` 값을 내되 503은 드묾 → **관측 배관은 통하나 신호(503)가 약함**. 스크립트 추가가 "503을 실제로 만드는" 최소 정공법.
- 비권고: 기존 surge.js의 order 가중 상향 — browsing-surge 계열 시나리오의 원인 프로파일을 오염(스크립트 헤더 §8.1-2 의도 파괴).

### 2c. 리스크
- (하) 신규 스크립트가 food baseline 여정·시드 계약(restaurant id 1–3·16–35, menu.available 필터) 준수해야 — surge.js 그대로 상속하면 안전.
- (중) courier pool 포화 임계 RPS 미실측 → order-create 가중·TARGET_RPS 튜닝 필요(503 재현 검증은 실행 단계).

---

## 산출 요약 (parent 반환용)

- **블로커1 = YES** (repo monitor 확장 + queries.json + 러너 patch 1건).
- **블로커2 = YES** (repo-only 신규 스크립트 1개; 계약 다중URL 확장 불요 — order 경로는 이미 default로 구동, 볼륨만 부족).
- repo 변경 파일: `scripts/scenarios/profiles/load_north_south_executor.py`(monitor), `scripts/scenarios/registry/queries.json`(4항), `scripts/scenarios/registry/profiles.json`(read_step domain_profile[선택] + food order-surge script_path), 신규 `food-delivery/loadgen/order-surge.js`.
- 러너 patch 대상: `backend/app/live_probes.py::_loadgen_observation` 430~432(allowlist)·435~438(field map)·442~443(range check).
- runner-patch 규약: **대상 파일 전체 root 스냅샷 patch**를 `scripts/runner-patches/2026-07-23-*.patch`로 보존, `git apply --3way`로 재적용/`-R`로 롤백.
- 미해결 잔여: read_step_status_rate는 domain_profile.read_step 배선 시에만(최소안 제외); business_nonok composite 4xx/5xx 분리 여부는 controller 배선 시 결정; food 503 재현 임계 RPS는 실행-단계 튜닝 필요; controllers.json observations[] 배선은 별도.
