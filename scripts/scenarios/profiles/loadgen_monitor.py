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
import os
import sys
import time

WINDOW_SEC = 30.0
PUBLISH_INTERVAL_SEC = 1.0


class LiveDocumentBuilder:
    """30초 창 위에서 k6 포인트를 누적해 라이브 문서를 만든다.

    계산은 시나리오 monitor에서 그대로 옮겨온 것이다 — 이 변경은 *누가 언제 문서를
    쓰는가*만 바꾸고 지표 정의는 건드리지 않는다.
    """

    def __init__(self, identity: dict[str, str], business_step: str, read_step: str = "") -> None:
        self.identity = identity
        self.business_step = business_step
        self.read_step = read_step
        self.iterations: collections.deque = collections.deque()
        self.checkout_results: collections.deque = collections.deque()
        self.read_results: collections.deque = collections.deque()
        self.entry_status: int | None = None
        self.last_stamp: datetime.datetime | None = None

    def consume(self, line: str) -> bool:
        """k6 json 한 줄을 흡수한다. 창 안에 반영됐으면 True."""
        if '"iterations"' not in line and '"http_reqs"' not in line:
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
            if metric == "iterations":
                self.iterations.append(stamp)
            elif metric == "http_reqs" and tags.get("step") == self.business_step:
                raw = tags.get("status")
                self.entry_status = int(raw) if raw and str(raw).isdigit() else 0
                self.checkout_results.append((stamp, self.entry_status))
            elif metric == "http_reqs" and self.read_step and tags.get("step") == self.read_step:
                raw = tags.get("status")
                self.read_results.append((stamp, int(raw) if raw and str(raw).isdigit() else 0))
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
        document.update(self.identity)
        return document


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
    args = parser.parse_args(argv)
    if args.domain and not args.unit:
        parser.error("--domain requires --unit")
    builder = LiveDocumentBuilder(build_identity(args), args.business_step, args.read_step)
    runner = run_tail if args.mode == "tail" else run_stream
    runner(builder, args.source, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
