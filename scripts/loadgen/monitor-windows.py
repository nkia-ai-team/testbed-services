#!/usr/bin/env python3
"""k6 --out json 줄을 loadgen_monitor.py 정본에 30초 묶음씩 흘려 묶음마다 문서를 한 줄 JSON 으로 낸다.

scripts/test-loadgen-transfer-pairs.sh 가 모의(simulate-seed-balances.mjs, k6json)로 만든 표본을
상주 감시가 실제로 어떻게 세는지 보려고 쓴다. 첫 표본 시각부터 30초씩 자른 묶음마다 새
LiveDocumentBuilder 에 그 묶음 표본만 넣고 build() 하므로 묶음은 겹치지 않고(한 표본은 한 묶음에만),
마지막 묶음까지 내므로 묶음 합이 표본 수와 같다. 감시의 30초 창은 cutoff 와 같은 시각의 표본을
남기므로(cutoff 미만만 버림) 한 builder 를 이어 쓰며 경계마다 build() 하면 경계 시각 표본이 두 창에
들어간다(2026-11-11 E5c 리뷰 1).

    monitor-windows.py <k6 json 파일> <business_step> [read_step]
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path

PROFILES = Path(__file__).resolve().parent.parent / "scenarios" / "profiles"
spec = importlib.util.spec_from_file_location("loadgen_monitor", PROFILES / "loadgen_monitor.py")
assert spec and spec.loader
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def main(path: str, business_step: str, read_step: str = "") -> int:
    window = datetime.timedelta(seconds=monitor.WINDOW_SEC)

    def fresh() -> "monitor.LiveDocumentBuilder":
        return monitor.LiveDocumentBuilder({"domain": "commerce", "unit": "sim"}, business_step, read_step)

    def emit(builder: "monitor.LiveDocumentBuilder") -> None:
        document = builder.build()
        if document is not None:
            print(json.dumps(document, sort_keys=True))

    builder = fresh()
    boundary = None
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            observed = json.loads(line).get("data", {}).get("time")
            if not observed:
                continue
            stamp = datetime.datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if boundary is None:
                boundary = stamp + window
            if stamp >= boundary:
                emit(builder)
                builder = fresh()
                while stamp >= boundary:
                    boundary += window
            builder.consume(line)
    emit(builder)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
