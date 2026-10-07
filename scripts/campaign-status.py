#!/usr/bin/env python3
"""캡처 캠페인 일일 점검 — .104 에서 실행.

케이스별 라벨/적격/인시던트 수를 표로 내고, 다음을 표시한다:
  - 재캡처 후보: case_label != evaluation, 또는 evaluation 인데 incidents == 0
    (격리 채점 rca-runonce 는 인시던트를 입력으로 요구한다)
  - 아카이브 누락: 109 스테이징에는 있는데 정본(.104 /data/eval-cases)에 없는 케이스
    (capture-eval-case.sh 의 아카이브 전송은 best-effort 라 실패해도 캡처는 성공 처리됨)
  - 큐 상태(phase/진행/실패/정지)와 디스크(109 /data, 119 루트 — imageGC 임계 85%)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ARCHIVE_ROOT = "/data/eval-cases"
STAGING_HOST = "root@192.168.200.109"
AP_HOST = "ydkim@192.168.230.119"


def ssh(host: str, cmd: str, via: str | None = None) -> str:
    if via:
        cmd = f"ssh -i /root/.ssh/tb_key -o BatchMode=yes -o ConnectTimeout=10 {host} {json.dumps(cmd)}"
        host = via
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", host, cmd], capture_output=True, text=True, timeout=180)
    return r.stdout.strip()


def case_row(meta_path: str) -> dict:
    m = json.load(open(meta_path))
    pt = {t["table"]: t["rows"] for t in m.get("postgres_tables", [])}
    return {
        "label": m.get("case_label"),
        "eligible": m.get("evaluation_eligible"),
        "incidents": pt.get("incidents"),
        "ch_tables": len(m.get("clickhouse_tables", [])),
        "t2": m.get("t2", "")[5:16].replace("T", " "),
        "preflight": (m.get("preflight") or {}).get("verdict"),
    }


def main() -> int:
    local = sorted(n for n in os.listdir(ARCHIVE_ROOT) if n.startswith("case-") and os.path.isdir(os.path.join(ARCHIVE_ROOT, n)))
    staging = ssh(STAGING_HOST, "ls -d /data/eval-cases/case-* 2>/dev/null | xargs -rn1 basename").split()

    print(f"== 케이스 (정본 .104: {len(local)} / 스테이징 109: {len(staging)}) ==")
    recapture: list[str] = []
    for n in local:
        r = case_row(os.path.join(ARCHIVE_ROOT, n, "meta.json"))
        flag = ""
        if r["label"] != "evaluation" or not r["eligible"]:
            flag = "  <-- 재캡처 후보 (비평가 라벨)"
            recapture.append(n)
        elif not r["incidents"]:
            flag = "  <-- 재캡처 후보 (인시던트 0)"
            recapture.append(n)
        print(f"  {n:34} {r['label']:<11} elig={str(r['eligible']):5} inc={str(r['incidents']):>3} CH={r['ch_tables']} pre={r['preflight']} t2={r['t2']}Z{flag}")
    missing = [n for n in staging if n not in local]
    if missing:
        print(f"  !! 아카이브 누락 (109 에만 있음): {missing}")

    q = ssh(STAGING_HOST, "docker exec scenario-runner python3 -c \"import urllib.request,json;d=json.load(urllib.request.urlopen('http://localhost:8091/api/live-queue'));print(d['phase'],'|',d.get('current_scenario_id'),'|',d.get('next_index'),'/',len(d['scenario_ids']),'|',d.get('reason') or '-','| skipped',d.get('skipped_scenario_ids'))\"")
    print(f"== 큐 == {q}")

    def df_summary(line: str) -> str:
        f = line.split()
        return f"{f[4]} used, {f[3]} free" if len(f) >= 5 else "?"

    # 중첩 ssh 에서는 awk 의 $N 이 바깥 셸에 먹히므로 원문을 받아 파이썬에서 자른다.
    d109 = df_summary(ssh(STAGING_HOST, "df -h /data | tail -1"))
    d119 = df_summary(ssh(AP_HOST, "df -h / | tail -1", via=STAGING_HOST))
    print(f"== 디스크 == 109 /data: {d109} | 119 /: {d119}  (119 는 85% 에서 imageGC)")

    # AI 판정 건강도 — 2026-08-21 에 LLM 게이트웨이가 HTTPS→HTTP 로 바뀌어 판정기가 15시간
    # 눈먼 채 캠페인 5케이스가 인시던트 0 으로 찍힌 사고의 재발 감지. LLM 판정이 최근 1h
    # 에 0 이거나 operator 에 unavailable 오류가 쌓이면 즉시 경고.
    sql = ("SELECT count(*) FILTER (WHERE trigger='llm'), count(*), "
           "(SELECT count(*) FROM incidents WHERE created_at > now() - interval '6 hour') "
           "FROM incident_judge_decisions WHERE updated_at > now() - interval '1 hour'")
    psql = f"sudo -n kubectl exec -n polestar pg-1 -c postgres -- psql -U postgres -d lucida -tAc \"{sql}\""
    row = ssh(AP_HOST, psql, via=STAGING_HOST).split("|")
    llm_1h, total_1h, inc_6h = (row + ["?", "?", "?"])[:3]
    unav = ssh(AP_HOST, "sudo -n kubectl logs -n polestar deploy/ai-operator --since=1h 2>&1 | grep -ac 'llm temporarily unavailable'", via=STAGING_HOST)
    warn = "  <-- !! LLM 판정 0건: 게이트웨이/설정 점검" if llm_1h.strip() == "0" else ""
    print(f"== AI 판정 == 최근 1h LLM 판정 {llm_1h}/{total_1h} | 최근 6h 인시던트 {inc_6h} | operator LLM 불통 오류 1h {unav}{warn}")

    print(f"== 요약 == 정본 {len(local)}건, 평가용 {sum(1 for n in local if n not in recapture)}건, 재캡처 후보 {len(recapture)}건, 아카이브 누락 {len(missing)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
