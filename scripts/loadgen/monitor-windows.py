#!/usr/bin/env python3
"""k6 --out json 줄을 loadgen_monitor.py 정본에 30초 창씩 흘려 창마다 문서를 한 줄 JSON 으로 낸다.

scripts/test-loadgen-transfer-pairs.sh 가 모의(simulate-seed-balances.mjs, k6json)로 만든 표본을
상주 감시가 실제로 어떻게 세는지 보려고 쓴다. 창 경계에서 build() 를 부르므로(그 직전 30초) 창은
겹치지 않는다.

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
    builder = monitor.LiveDocumentBuilder({"domain": "commerce", "unit": "sim"}, business_step, read_step)
    window = datetime.timedelta(seconds=monitor.WINDOW_SEC)
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
                document = builder.build()
                if document is not None:
                    print(json.dumps(document, sort_keys=True))
                while stamp >= boundary:
                    boundary += window
            builder.consume(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
