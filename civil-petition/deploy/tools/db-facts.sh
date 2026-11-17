#!/bin/sh
# DB 서버의 평시 정상 상태 사실을 JSON 한 줄로 낸다(설계 §10 T2 평시 24시간: 주 표 행 수가 하루 동안 평평한가,
# 보관 배치가 돌았는가). 24시간 창의 처음과 끝에 db01 에서 root 로 한 번씩 돌려
# stability_check.py --db-before, --db-after 에 준다. 읽기만 한다.
# count(*) 로 정확히 세므로 T2 24시간 판정의 처음과 끝 두 번에만 쓴다. 녹화 preflight 의 보관 배치 상태는 표를
# 훑지 않는 가벼운 읽기 archive-state.sh 가 맡는다(설계 §6-4, 리뷰 rv-t2c M2). last_archive_ok 는 두 도구가 같다.
# 쓰는 법: db-facts.sh <DB 이름>   (예: ssh db01 'sudo -n sh -s petition' < db-facts.sh >> db-before.jsonl)
# 이 점검 읽기는 세션에서 느린 문장 기준(1초)을 꺼 느린 문장 줄은 서버 로그에 남지 않는다. 접속과 종료 줄
# (log_connections, log_disconnections, postgres@<DB> 로컬 소켓)은 남는다. 24시간 판정의 처음과 끝 시각의 운영자 흔적이다.
set -eu
db="$1"
host=$(hostname)
at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
body=$(runuser -u postgres -- psql -X -Atq -v ON_ERROR_STOP=1 -d "$db" <<'SQL'
SET log_min_duration_statement = -1;
SELECT json_build_object(
    'rows', json_build_object(
        'petition', (SELECT count(*) FROM petition),
        'petition_history', (SELECT count(*) FROM petition_history)),
    'archive_runs', COALESCE((
        SELECT json_agg(json_build_object(
                   'finished_at', to_char(finished_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                   'rows', rows_affected, 'result', result) ORDER BY finished_at)
        FROM batch_run WHERE job = 'archive' AND finished_at > now() - interval '26 hours'), '[]'::json),
    'last_archive_ok', (
        SELECT to_char(max(finished_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        FROM batch_run WHERE job = 'archive' AND result = 'ok'));
SQL
)
printf '{"host":"%s","at":"%s","db":%s}\n' "$host" "$at" "$body"
