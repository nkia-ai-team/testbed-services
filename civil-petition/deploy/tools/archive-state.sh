#!/bin/sh
# 녹화 preflight 의 보관 배치 상태 읽기(설계 §6-4, 검토 세션 Q22 덧붙임 2, 리뷰 rv-t2c M2). db01 에서 root 로
# 녹화마다 한 번 돌리고, 러너가 그 한 줄(last.json)로 주입 시작을 막는다(마지막 보관 ok 이후 48시간 넘음 또는
# 민원 행 수가 상한의 1.1배 넘음). 출력 꼴은 db-facts.sh 와 같다(rows.petition, last_archive_ok).
#
# db-facts.sh(T2 24시간 판정의 처음과 끝 두 번만)와 달리 가볍게 읽는다:
#   - 행 수는 count(*) 가 아니라 통계 뷰의 추정치 pg_stat_user_tables.n_live_tup 이다(표를 훑지 않고 민원 표에
#     잠금을 걸지 않는다. 기록과 1.1배 판정에는 추정치로 충분하다). 통계가 초기화된 직후(비정상 종료, 통계 초기화)에는
#     0 에 가까우므로 그때는 행 수 조건이 늦게 걸린다.
#   - 마지막 보관 ok 는 batch_run(밤마다 몇 줄 쌓이는 작은 표)의 max(finished_at) 다.
#   - 세션에서 lock_timeout 200ms, statement_timeout 1초. lock_timeout 이 deadlock_timeout(기본 1초)보다 짧아 잠금 대기
#     줄("still waiting for ...")은 log_lock_waits 와 무관하게 생기지 않는다(그 줄은 deadlock_timeout 을 넘겨 기다릴 때만
#     찍힌다). 이 읽기는 postgres(superuser) 로 돌므로 superuser 전용 설정인 log_lock_waits, log_min_duration_statement,
#     log_min_messages, log_min_error_statement 도 세션에서 꺼, 시간 초과로 실패해도 서버 로그에 오류 줄을 남기지 않는다.
#     실패하면 종료 코드가 0 이 아니고 러너 쪽 사실이 묵어 막힘(fail closed)이 된다. 읽은 뒤에는 log_min_messages 를
#     되돌려 종료 줄이 평소대로 남게 한다(실패한 경우만 종료 줄이 빠진다).
#   - 서버 로그에 남는 것은 log_connections, log_disconnections 의 접속 두 줄과 종료 한 줄(postgres@<DB>, 로컬 소켓)뿐이다.
#   - superuser 가 아닌 계정(앱 계정 등)은 log_lock_waits 를 세션에서 바꿀 수 없다(permission denied, PostgreSQL 16
#     로컬 시험). 그래서 이 읽기는 postgres 로 돈다. 그렇지 않더라도 lock_timeout 이 deadlock_timeout 보다 짧아 대기 줄은 안 생긴다.
# 쓰는 법: archive-state.sh <DB 이름>   (예: ssh db01 'sudo -n sh -s petition' < archive-state.sh > last.json)
set -eu
db="$1"
host=$(hostname)
at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
body=$(runuser -u postgres -- psql -X -Atq -v ON_ERROR_STOP=1 -d "$db" <<'SQL'
SET log_min_messages = panic;
SET log_min_error_statement = panic;
SET log_min_duration_statement = -1;
SET log_lock_waits = off;
SET lock_timeout = '200ms';
SET statement_timeout = '1s';
SELECT json_build_object(
    'rows', json_build_object(
        'petition', (SELECT n_live_tup FROM pg_stat_user_tables WHERE relname = 'petition' AND schemaname = 'public')),
    'rows_source', 'n_live_tup',
    'last_archive_ok', (
        SELECT to_char(max(finished_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        FROM batch_run WHERE job = 'archive' AND result = 'ok'));
RESET log_min_messages;
SQL
)
printf '{"host":"%s","at":"%s","db":%s}\n' "$host" "$at" "$body"
