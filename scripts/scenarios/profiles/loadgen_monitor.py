#!/usr/bin/env python3
"""k6 샘플 스트림 -> 라이브 관측 문서.

라이브 요약 계약의 **정본**이다. 두 소비자가 이 파일 하나를 쓴다.

- 시나리오 경로: ``load_north_south_executor.py``가 이 파일 내용을 읽어 원격 스크립트에
  주입한다(ssh stdin 한 번으로 전송되는 구조를 유지한다).
- baseline 경로: tb-runner에 배포된 이 파일을 상주 유닛이 직접 실행한다.

사본을 두지 않는 이유는 2026-07-29 F06-P다 — 손으로 관리하던 두 번째 allowlist가
정본과 갈라져 시나리오의 유일한 성공 조건이 매 tick 실패했다.

두 가지 입력 모드가 있다.

``tail``   정규 파일을 seek/tell로 따라간다. 시나리오 경로의 기존(검증된) 동작.
``stream`` FIFO를 순차로 읽는다. baseline 상주 유닛용 — 1시간 run을 무한 반복하는
           유닛에서 ``--out json=<파일>``은 샘플 파일이 시간당 수 GB로 자란다.
           FIFO는 소비 즉시 사라지고, writer(k6)가 매시 재시작하면 EOF에서 재개방한다.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import os
import sys
import time

WINDOW_SEC = 30.0
PUBLISH_INTERVAL_SEC = 1.0
# 창 기록(--window-log)의 고정 창 길이. 평시 안정 판정(설계 spec-testbed-vm-3tier.md Q3 의 "5분 창")과 같다.
WINDOW_LOG_SEC = 300


class LiveDocumentBuilder:
    """30초 창 위에서 k6 포인트를 누적해 라이브 문서를 만든다.

    계산은 시나리오 monitor에서 그대로 옮겨온 것이다 — 이 변경은 *누가 언제 문서를
    쓰는가*만 바꾸고 지표 정의는 건드리지 않는다.
    """

    def __init__(
        self,
        identity: dict[str, str],
        business_step: str,
        read_step: str = "",
        http_summary: bool = False,
        window_log: "WindowLog | None" = None,
    ) -> None:
        self.identity = identity
        self.business_step = business_step
        self.read_step = read_step
        # 전체 요청 요약(--http-summary, 2026-11-16 v4 T2). 켜야만 도는 선택 기능이라 끈 도메인의 문서와
        # 동작은 그대로다. 켜면 단계와 무관하게 모든 요청의 5xx, 응답 없음(시간 초과와 연결 실패), p95 를
        # 같은 30초 창으로 싣는다(SMS, NMS 전용 테스트베드 설계 §5 요약 파일, §6-3 사용자 비정상 비율).
        self.http_summary = http_summary
        self.window_log = window_log
        self.http_results: collections.deque = collections.deque()
        self.http_durations: collections.deque = collections.deque()
        self.iterations: collections.deque = collections.deque()
        self.checkout_results: collections.deque = collections.deque()
        self.read_results: collections.deque = collections.deque()
        # (시각, check 이름, 통과 여부). business, read 두 단계 밖의 여정도 스크립트가 check() 로 적은
        # 기대(상태 코드)를 어기면 보이게 한다(아래 build 의 check 필드 설명).
        self.check_results: collections.deque = collections.deque()
        self.entry_status: int | None = None
        self.last_stamp: datetime.datetime | None = None

    def consume(self, line: str) -> bool:
        """k6 json 한 줄을 흡수한다. 창 안에 반영됐으면 True."""
        if (
            '"iterations"' not in line
            and '"http_reqs"' not in line
            and '"checks"' not in line
            and not ((self.http_summary or self.window_log is not None) and '"http_req_duration"' in line)
        ):
            return False
        try:
            point = json.loads(line)
            if point.get("type") != "Point":
                return False
            data = point.get("data", {})
            observed = data.get("time")
            if not observed:
                return False
            stamp = datetime.datetime.fromisoformat(observed.replace("Z", "+00:00"))
            metric = point.get("metric")
            tags = data.get("tags", {})
            if self.http_summary or self.window_log is not None:
                if metric == "http_req_duration":
                    value = float(data.get("value"))
                    if self.http_summary:
                        self.http_durations.append((stamp, value))
                    if self.window_log is not None:
                        self.window_log.add_duration(stamp, value)
                    self.last_stamp = stamp
                    return True
                if metric == "http_reqs":
                    raw = tags.get("status")
                    status = int(raw) if raw and str(raw).isdigit() else 0
                    error_code = str(tags.get("error_code") or "")
                    if self.http_summary:
                        self.http_results.append((stamp, status, error_code))
                    if self.window_log is not None:
                        self.window_log.add_request(stamp, status, error_code)
                    self.last_stamp = stamp
                    # 아래 단계별 집계는 그대로 이어서 한다(같은 줄이 사업, 조회 단계에도 들어간다).
            if metric == "iterations":
                if self.window_log is not None:
                    self.window_log.add_iteration(stamp)
                self.iterations.append(stamp)
            elif metric == "http_reqs" and tags.get("step") == self.business_step:
                raw = tags.get("status")
                self.entry_status = int(raw) if raw and str(raw).isdigit() else 0
                self.checkout_results.append((stamp, self.entry_status))
            elif metric == "http_reqs" and self.read_step and tags.get("step") == self.read_step:
                raw = tags.get("status")
                self.read_results.append((stamp, int(raw) if raw and str(raw).isdigit() else 0))
            elif metric == "checks" and tags.get("check"):
                self.check_results.append((stamp, str(tags["check"]), data.get("value") == 1))
            elif metric == "http_reqs" and (self.http_summary or self.window_log is not None):
                pass  # 단계 밖 요청: 위 전체 요약에만 들어갔다
            else:
                return False
            self.last_stamp = stamp
            return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def build(self) -> dict | None:
        if self.last_stamp is None:
            return None
        cutoff = self.last_stamp - datetime.timedelta(seconds=WINDOW_SEC)
        while self.iterations and self.iterations[0] < cutoff:
            self.iterations.popleft()
        while self.checkout_results and self.checkout_results[0][0] < cutoff:
            self.checkout_results.popleft()
        while self.read_results and self.read_results[0][0] < cutoff:
            self.read_results.popleft()
        while self.check_results and self.check_results[0][0] < cutoff:
            self.check_results.popleft()
        while self.http_results and self.http_results[0][0] < cutoff:
            self.http_results.popleft()
        while self.http_durations and self.http_durations[0][0] < cutoff:
            self.http_durations.popleft()
        span = (
            max(1.0, min(WINDOW_SEC, (self.iterations[-1] - self.iterations[0]).total_seconds()))
            if len(self.iterations) > 1
            else 1.0
        )
        # 요청이 한 건도 없는 창은 비율 필드를 **싣지 않는다**.
        #
        # 종전에는 `... if checkout_count else 0.0`으로 0을 발행했다. 비율의
        # 분모가 0이면 비율은 정의되지 않는데, 0.0은 "오류가 없었다"로 읽힌다 —
        # 측정 실패를 정상이라는 값으로 접는 것이다. 2026-08-07 러너에서 같은
        # 병을 두 번 고쳤다(APM 백분위 게이지의 표본 없는 창, clickhouse 오류율의
        # `if(count()=0, 0, ...)`). 그 전수 점검이 러너 안에만 닿아서 이 세 번째
        # 인스턴스는 밖에 남아 있었다.
        #
        # 해가 큰 이유는 소비자가 전부 감별자이기 때문이다. F03-P·F19-P·F20-Q·
        # F06-P·F10-H·F14-P·F16-H는 이 비율들을 must_rule_out으로 읽는다. 서비스가
        # 죽어 요청이 한 건도 못 나간 창이 "정상"으로 답하면, 배제됐어야 할 교란
        # 요인이 배제된 것처럼 통과한다.
        #
        # 필드를 생략하면 러너의 `_loadgen_observation`이 `document.get(field)`에서
        # None을 받아 `LiveProbeError`를 던지고 그대로 unusable이 된다. 러너는 한
        # 줄도 바꿀 필요가 없다.
        #
        # `achieved_rps`는 여기 해당하지 않는다 — 분모가 요청 수가 아니라 시간이고,
        # 0은 "부하가 전달되지 않았다"는 **진짜 측정값**이다. 여러 시나리오가
        # `achieved_rps < 15`를 must_rule_out으로 읽으므로 이걸 부재로 바꾸면
        # 감별자가 발화해야 할 때 침묵한다. 정확히 반대 방향의 결함이 된다.
        checkout_count = len(self.checkout_results)
        read_count = len(self.read_results)
        document = {
            "achieved_rps": len(self.iterations) / span,
            "entry_status": self.entry_status,
            # 비율이 몇 개의 요청 위에서 계산됐는지 — 게이트가 표본 수를 알아야
            # 최소 표본 가드를 걸 수 있다. 2026-08-07 실측: 대조 팔의 창당 요청이
            # 2~15건이라 p≈0.3에서 표준오차가 ~0.20이었다. 그 분모가 문서에 없어서
            # 소비자는 0.33이 1/3인지 100/300인지 구분할 수 없었다.
            "checkout_count": checkout_count,
            "business_ok": self.entry_status in {200, 400, 409},
            "observed_at": self.last_stamp.astimezone(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        if checkout_count:
            business_2xx_rate = (
                sum(200 <= status <= 299 for _, status in self.checkout_results) / checkout_count
            )
            business_4xx_rate = (
                sum(400 <= status <= 499 for _, status in self.checkout_results) / checkout_count
            )
            business_5xx_rate = (
                sum(status >= 500 for _, status in self.checkout_results) / checkout_count
            )
            # F23-R decisive evidence: 409 (stock-exhausted) is a subset of the
            # 4xx bucket that business_nonok_rate can't isolate from other 4xx
            # causes (e.g. coupon validation) - tracked separately here.
            business_409_rate = (
                sum(status == 409 for _, status in self.checkout_results) / checkout_count
            )
            # F06-P decisive evidence: 429 (downstream rate limit) is another 4xx
            # subset that business_nonok_rate cannot isolate. Without it a partial
            # 429 outage is indistinguishable from validation rejects, and
            # business_5xx_rate is blind to it entirely - the app propagates the
            # downstream status verbatim rather than promoting it to 5xx.
            business_429_rate = (
                sum(status == 429 for _, status in self.checkout_results) / checkout_count
            )
            document.update(
                {
                    # checkout_5xx_rate is kept for backward compatibility
                    # (pre-existing query_id/consumer contract);
                    # business_5xx_rate is its replacement value.
                    "checkout_5xx_rate": business_5xx_rate,
                    "business_2xx_rate": business_2xx_rate,
                    "business_4xx_rate": business_4xx_rate,
                    "business_5xx_rate": business_5xx_rate,
                    "business_409_rate": business_409_rate,
                    "business_429_rate": business_429_rate,
                    "business_nonok_rate": business_4xx_rate + business_5xx_rate,
                }
            )
        if self.read_step:
            document["read_count"] = read_count
            if read_count:
                document["read_2xx_rate"] = (
                    sum(200 <= status <= 299 for _, status in self.read_results) / read_count
                )
                document["read_nonok_rate"] = (
                    sum(status >= 400 or status == 0 for _, status in self.read_results)
                    / read_count
                )
        # check 필드(2026-11-11 v4 E5c). business, read 는 러너 관측 계약(profiles.json
        # domain_profiles)에 묶인 두 단계뿐이라, 다른 여정의 비정상은 문서 어디에도 안 잡혔다.
        # commerce 장바구니 쓰기가 토큰 없이 나가 늘 401 을 받던 것(평시 분당 약 58건)이 그렇게
        # 넉 달 숨어 있었다. 스크립트의 check() 는 단계마다 기대 상태를 이미 적고 있으므로(상품
        # 상세의 404 처럼 정상인 4xx 는 통과로 적혀 있다) 그 통과 여부를 그대로 센다. 새 단계도
        # check 만 달면 따로 등록하지 않아도 잡힌다. business, read 필드와 그 의미는 바꾸지 않는다.
        # 분모가 0 인 창은 위 비율들처럼 비율과 이름별 수를 싣지 않는다.
        check_count = len(self.check_results)
        document["check_count"] = check_count
        if check_count:
            failed: dict[str, int] = {}
            for _, name, passed in self.check_results:
                if not passed:
                    failed[name] = failed.get(name, 0) + 1
            document["check_failed_rate"] = sum(failed.values()) / check_count
            document["check_failed"] = dict(sorted(failed.items()))
        if self.http_summary:
            # 분모가 0 인 창은 위 비율들처럼 비율과 p95 를 싣지 않고 분모만 싣는다.
            http_count = len(self.http_results)
            document["http_count"] = http_count
            if http_count:
                server_errors = sum(status >= 500 for _, status, _ in self.http_results)
                no_response = sum(status == 0 for _, status, _ in self.http_results)
                timeouts = sum(code == K6_REQUEST_TIMEOUT for _, _, code in self.http_results)
                document["http_5xx_rate"] = server_errors / http_count
                document["http_timeout_rate"] = timeouts / http_count
                document["http_failed_rate"] = no_response / http_count
                document["http_abnormal_rate"] = (server_errors + no_response) / http_count
            if self.http_durations:
                document["http_p95_ms"] = percentile([value for _, value in self.http_durations], 0.95)
        document.update(self.identity)
        return document


# k6 가 응답 시간 초과에 다는 error_code 태그 값(k6 문서의 오류 코드 표, 1050 request timeout).
K6_REQUEST_TIMEOUT = "1050"


def percentile(values: list[float], fraction: float) -> float:
    """가장 가까운 순위 방식 백분위(표본 n 에서 ceil(fraction*n) 번째)."""
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


class WindowLog:
    """고정 5분 창마다 한 줄씩 요청 수, 5xx, 응답 없음, 시간 초과, p95 를 JSON 줄로 덧붙인다.

    평시 24시간 안정(Q3: 전체 비율과 "5분 창의 99%") 판정의 입력이다. 라이브 문서는 30초 창 하나만
    덮어쓰므로 지난 창을 되짚을 수 없다. 창 경계는 UTC 기원 기준 300초 배수로 고정해 다시 돌려도
    같은 창 이름이 나온다. 창이 바뀔 때 지난 창을 쓴다. 부하가 멈춘 창은 줄이 없고, 판정기는 그것을
    빠진 창으로 센다. 지난 창에 속하는 늦은 점은 지금 창에 넣는다(k6 json 출력은 거의 시각순이다).
    """

    def __init__(self, path: str, window_sec: int = WINDOW_LOG_SEC) -> None:
        self.path = path
        self.window_sec = window_sec
        self.current: int | None = None
        self._reset()

    def _reset(self) -> None:
        self.requests = 0
        self.server_errors = 0
        self.no_response = 0
        self.timeouts = 0
        self.iterations = 0
        self.durations: list[float] = []

    def _bucket(self, stamp: datetime.datetime) -> int:
        epoch = int(stamp.timestamp())
        return epoch - epoch % self.window_sec

    def _advance(self, stamp: datetime.datetime) -> None:
        bucket = self._bucket(stamp)
        if self.current is None:
            self.current = bucket
        elif bucket > self.current:
            self.flush()
            self.current = bucket

    def add_request(self, stamp: datetime.datetime, status: int, error_code: str) -> None:
        self._advance(stamp)
        self.requests += 1
        self.server_errors += status >= 500
        self.no_response += status == 0
        self.timeouts += error_code == K6_REQUEST_TIMEOUT

    def add_duration(self, stamp: datetime.datetime, value: float) -> None:
        self._advance(stamp)
        self.durations.append(value)

    def add_iteration(self, stamp: datetime.datetime) -> None:
        self._advance(stamp)
        self.iterations += 1

    def flush(self) -> None:
        if self.current is None or (self.requests == 0 and self.iterations == 0):
            self._reset()
            return
        record = {
            "window_start": datetime.datetime.fromtimestamp(self.current, datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "window_sec": self.window_sec,
            "iterations": self.iterations,
            "http_count": self.requests,
            "http_5xx": self.server_errors,
            "http_no_response": self.no_response,
            "http_timeout": self.timeouts,
        }
        if self.durations:
            record["http_p95_ms"] = percentile(self.durations, 0.95)
        with open(self.path, "a", encoding="utf-8") as target:
            target.write(json.dumps(record, sort_keys=True) + "\n")
        self._reset()


def publish(document: dict, output: str) -> None:
    temporary = output + ".tmp"
    with open(temporary, "w", encoding="utf-8") as target:
        json.dump(document, target, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, output)


def run_tail(builder: LiveDocumentBuilder, source: str, output: str) -> None:
    """정규 파일을 따라간다(시나리오 경로).

    라이브 문서는 파싱한 줄마다가 아니라 드레인 주기마다 한 번만 다시 쓴다: 높은 도착률에서
    줄마다 fsync하면 파서가 k6 json 출력을 못 따라가 수 분 뒤처지고, 그러면 observed_at이
    30초 신선도 계약에 걸린다(F07-H run 158b449c, 80rps, 07-19).
    """
    position = 0
    while True:
        parsed_any = False
        try:
            with open(source, encoding="utf-8") as stream:
                stream.seek(position)
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    position = stream.tell()
                    parsed_any |= builder.consume(line)
        except FileNotFoundError:
            pass
        if parsed_any:
            document = builder.build()
            if document is not None:
                publish(document, output)
        time.sleep(PUBLISH_INTERVAL_SEC)


def run_stream(builder: LiveDocumentBuilder, source: str, output: str) -> None:
    """FIFO를 순차로 읽는다(baseline 경로).

    seek/tell은 FIFO에서 동작하지 않으므로 tail 모드를 쓸 수 없다. writer(k6)가 매시
    재시작하며 EOF를 남기므로, EOF에서 종료하지 않고 재개방해 상주한다.
    """
    last_publish = 0.0
    while True:
        try:
            with open(source, encoding="utf-8") as stream:
                for line in stream:
                    if not builder.consume(line):
                        continue
                    now = time.monotonic()
                    if now - last_publish < PUBLISH_INTERVAL_SEC:
                        continue
                    document = builder.build()
                    if document is not None:
                        publish(document, output)
                        last_publish = now
        except FileNotFoundError:
            pass
        # writer가 닫혔다(k6 사이클 종료). 다음 writer를 기다리며 재개방한다.
        time.sleep(PUBLISH_INTERVAL_SEC)


def build_identity(args: argparse.Namespace) -> dict[str, str]:
    if args.scenario_id:
        return {
            "scenario_id": args.scenario_id,
            "scenario_tag": f"scenario_id={args.scenario_id}",
        }
    return {"domain": args.domain, "unit": args.unit}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="k6 json 샘플 경로(파일 또는 FIFO)")
    parser.add_argument("--output", required=True, help="라이브 문서를 쓸 경로")
    parser.add_argument("--business-step", required=True)
    parser.add_argument("--read-step", default="")
    parser.add_argument("--mode", choices=["tail", "stream"], default="tail")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--scenario-id", help="시나리오 모드 신원")
    identity.add_argument("--domain", help="baseline 모드 신원")
    parser.add_argument("--unit", default="", help="baseline 모드의 systemd 유닛 이름")
    parser.add_argument("--http-summary", action="store_true", help="모든 요청의 5xx, 응답 없음, p95 를 싣는다")
    parser.add_argument("--window-log", default="", help="고정 5분 창 요약을 덧붙일 JSON 줄 파일")
    args = parser.parse_args(argv)
    if args.domain and not args.unit:
        parser.error("--domain requires --unit")
    builder = LiveDocumentBuilder(
        build_identity(args),
        args.business_step,
        args.read_step,
        http_summary=args.http_summary,
        window_log=WindowLog(args.window_log) if args.window_log else None,
    )
    runner = run_tail if args.mode == "tail" else run_stream
    runner(builder, args.source, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
