#!/bin/bash
# 보관 배치 SQL 을 진짜 PostgreSQL 16 에서 확인하는 수동 도구(기본 시험에 넣지 않는다. docker 가 필요하다).
# Java 시험(BatchServiceTest)은 JdbcTemplate 를 모의해 문장을 실행하지 않으므로, 문장의 뜻과 계획은 이것으로 본다.
#   - 일회 postgres:16 컨테이너에 schema.sql 두 번(멱등), seed.sql(규모 인자), VACUUM ANALYZE
#   - BatchService.MOVE_SQL 을 Java 소스에서 그대로 뽑아 1000, 1000, 500 건으로 실행
#   - 가장 오래된 2,500 건만 옮김, 처리 이력이 함께 옮겨지고 원표에 0, 답변과 첨부는 cascade 로 0,
#     남은 행이 옮긴 행보다 새것, 묶음 시간과 계획(처리 이력 색인을 타는지), count(*) 시간
#   - advisory lock: 한 세션이 잡고 있으면 다른 세션의 pg_try_advisory_lock 이 false
#   - 시드가 정상 상태 꼴인가(설계 §2-3): 행 수, 열린 건 1%, 그 가운데 처리 중 2~4%, 미래 시각 없음, 민원당 처리 이력
#     2.1~2.4, (규모 50만 이상) 접수 시각 폭(규모 / 하루 신청 121,824 일)과 KST 16시와 4시 신청 수 비 5~7,
#     그리고 기간 조건 읽기가 보는 행 수(참고 줄)
#   - 녹화 preflight 의 행 수 추정치 n_live_tup 이 count(*) 와 1% 안(VACUUM ANALYZE 뒤, 보관 이동 뒤)
# 끝에 컨테이너를 지운다. 104 같은 공용 서버에서는 무거운 잠금과 메모리 상한 아래에서 돌린다.
# 쓰는 법(저장소 뿌리에서): civil-petition/deploy/tools/archive-sql-check.sh [규모, 기본 2000000]
#   규모 200만은 시드에 약 6분이 걸린다. 빨리 문법만 볼 때는 20000.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
domain=$(cd "$here/../.." && pwd)
scale="${1:-2000000}"
name="archive-sql-check-$$"
source_file="$domain/petition-web/src/main/java/com/civilpetition/web/BatchService.java"
template=$(python3 -I - "$source_file" <<'PY'
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
body = re.search(r'MOVE_SQL = (.*?);\n', src, re.S).group(1)
print("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', body)))
PY
)
lock_key=$(sed -n 's/.*ARCHIVE_LOCK_KEY = \([0-9_]*\)L;.*/\1/p' "$source_file" | tr -d _)
[[ "$template" == *"LIMIT %d"* && -n "$lock_key" ]] || { echo "BatchService.java 에서 문장이나 잠금 열쇠를 못 찾음" >&2; exit 2; }
move() { printf '%s' "${template//%d/$1}"; }

report=$(mktemp)
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -f "$report"; }
trap cleanup EXIT
docker run -d --name "$name" --memory 1g --shm-size 256m -e POSTGRES_PASSWORD=check -e LANG=C.UTF-8 \
    -e POSTGRES_INITDB_ARGS="--locale=C.UTF-8" postgres:16 >/dev/null
for _ in $(seq 60); do docker exec "$name" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
sleep 2
psql_in() { docker exec -i "$name" psql -U postgres -X -v ON_ERROR_STOP=1 "$@"; }
psql_in -q -c "CREATE ROLE petition_app LOGIN"
psql_in -q -c "CREATE DATABASE petition OWNER petition_app"
for _ in 1 2; do psql_in -q -d petition -v app_user=petition_app < "$domain/db/schema.sql" 2>/dev/null; done
started=$(date +%s)
psql_in -q -d petition -v app_user=petition_app -v scale="$scale" < "$domain/db/seed.sql" >/dev/null
echo "seed scale=$scale seconds=$(( $(date +%s) - started ))"
psql_in -q -d petition -c "VACUUM ANALYZE"
psql_in -q -d petition -c "INSERT INTO petition_attachment (petition_id, file_name, size_bytes, created_at)
    SELECT petition_id, 'a.jpg', 1, now() FROM (SELECT petition_id FROM petition ORDER BY created_at, petition_id LIMIT 5) s"

psql_in -At -d petition <<SQL | tee "$report"
SELECT 'CHECK seed_rows', count(*) = $scale FROM petition;
SELECT 'CHECK seed_span_days', d BETWEEN e * 0.97 - 1.0 / 24 AND e * 1.03 + 1.0 / 24
  FROM (SELECT extract(epoch FROM max(created_at) - min(created_at)) / 86400 AS d, $scale / 121824.0 AS e FROM petition) x
  WHERE $scale >= 500000;
SELECT 'CHECK seed_open_one_percent', count(*) FILTER (WHERE status <> 'COMPLETED') = $scale / 100 FROM petition;
SELECT 'CHECK seed_in_progress_share', (count(*) FILTER (WHERE status = 'IN_PROGRESS'))::float8
       / nullif(count(*) FILTER (WHERE status <> 'COMPLETED'), 0) BETWEEN 0.02 AND 0.04 FROM petition;
SELECT 'CHECK seed_no_future_times', max(greatest(created_at, updated_at)) <= now() FROM petition;
SELECT 'CHECK seed_history_per_petition', (SELECT count(*) FROM petition_history)::float8 / $scale BETWEEN 2.1 AND 2.4;
SELECT 'CHECK seed_kst_peak_over_trough', r BETWEEN 5 AND 7
  FROM (SELECT (count(*) FILTER (WHERE extract(hour FROM created_at AT TIME ZONE 'Asia/Seoul') = 16))::float8
               / nullif(count(*) FILTER (WHERE extract(hour FROM created_at AT TIME ZONE 'Asia/Seoul') = 4), 0) AS r
        FROM petition) x WHERE $scale >= 500000;
SELECT 'INFO period_rows public_last_7d', count(*) FILTER (WHERE is_public AND created_at >= now() - interval '7 days'),
       'public_last_30d', count(*) FILTER (WHERE is_public AND created_at >= now() - interval '30 days'),
       'per_user_last_90d', round(count(*) FILTER (WHERE created_at >= now() - interval '90 days')::numeric / 49700, 1),
       'open', count(*) FILTER (WHERE status <> 'COMPLETED'),
       'history', (SELECT count(*) FROM petition_history) FROM petition;
SELECT 'CHECK n_live_tup_within_1pct_after_analyze', abs(n_live_tup - (SELECT count(*) FROM petition))::float8 / $scale < 0.01
  FROM pg_stat_user_tables WHERE relname = 'petition';
CREATE TEMP TABLE expect AS SELECT petition_id FROM petition ORDER BY created_at, petition_id LIMIT 2500;
CREATE TEMP TABLE expect_hist AS SELECT count(*) AS n FROM petition_history WHERE petition_id IN (SELECT petition_id FROM expect);
\\timing on
SELECT 'count', count(*) FROM petition;
\\timing off
EXPLAIN (COSTS OFF) $(move 1000);
\\timing on
$(move 1000);
$(move 1000);
$(move 500);
\\timing off
SELECT 'CHECK archived_are_oldest', (SELECT count(*) FROM petition_archive JOIN expect USING (petition_id)) = 2500
       AND (SELECT count(*) FROM petition_archive) = 2500;
SELECT 'CHECK history_moved', (SELECT count(*) FROM petition_history_archive) = (SELECT n FROM expect_hist);
SELECT 'CHECK history_left_zero', (SELECT count(*) FROM petition_history WHERE petition_id IN (SELECT petition_id FROM expect)) = 0;
SELECT 'CHECK answers_cascaded', (SELECT count(*) FROM petition_answer WHERE petition_id IN (SELECT petition_id FROM expect)) = 0;
SELECT 'CHECK attachments_cascaded', (SELECT count(*) FROM petition_attachment WHERE petition_id IN (SELECT petition_id FROM expect)) = 0;
SELECT 'CHECK rest_is_newer', (SELECT min(created_at) FROM petition) >= (SELECT max(created_at) FROM petition_archive);
SQL
sleep 2  # 통계는 세션이 쉴 때 공유 통계로 넘어간다(최소 1초)
psql_in -At -d petition -c "SELECT 'INFO n_live_tup', n_live_tup, 'count', (SELECT count(*) FROM petition) FROM pg_stat_user_tables WHERE relname = 'petition'" \
    -c "SELECT 'CHECK n_live_tup_within_1pct_after_moves', abs(n_live_tup - (SELECT count(*) FROM petition))::float8 / $scale < 0.01 FROM pg_stat_user_tables WHERE relname = 'petition'" \
    | tee -a "$report"

# advisory lock: 첫 세션이 잡고 쥐고 있는 동안 둘째 세션은 못 잡는다.
docker exec "$name" sh -c "psql -U postgres -X -d petition -At -c 'SELECT pg_advisory_lock($lock_key)' -c 'SELECT pg_sleep(5)' >/dev/null &"
sleep 2
held=$(psql_in -At -d petition -c "SELECT pg_try_advisory_lock($lock_key)")
echo "CHECK second_session_cannot_lock|$([[ "$held" == f ]] && echo t || echo f)" | tee -a "$report"
checks=$(grep -c '^CHECK ' "$report" || true)
failed=$(grep -c '^CHECK .*|f$' "$report" || true)
echo "checks=$checks failed=$failed"
# 7 은 보관 문장과 잠금, 시드 5(규모 50만 이상이면 접수 시각 폭과 KST 비를 더해 7), 2 는 n_live_tup.
expected=$(( scale >= 500000 ? 16 : 14 ))
[[ "$checks" == "$expected" && "$failed" == 0 ]]
