#!/bin/bash
# 보관 배치 SQL 을 진짜 PostgreSQL 16 에서 확인하는 수동 도구(기본 시험에 넣지 않는다. docker 가 필요하다).
# Java 시험(BatchServiceTest)은 JdbcTemplate 를 모의해 문장을 실행하지 않으므로, 문장의 뜻과 계획은 이것으로 본다.
#   - 일회 postgres:16 컨테이너에 schema.sql 두 번(멱등), seed.sql(규모 인자), VACUUM ANALYZE
#   - BatchService.MOVE_SQL 을 Java 소스에서 그대로 뽑아 1000, 1000, 500 건으로 실행
#   - 가장 오래된 2,500 건만 옮김, 처리 이력이 함께 옮겨지고 원표에 0, 답변과 첨부는 cascade 로 0,
#     남은 행이 옮긴 행보다 새것, 묶음 시간과 계획(처리 이력 색인을 타는지), count(*) 시간
#   - advisory lock: 한 세션이 잡고 있으면 다른 세션의 pg_try_advisory_lock 이 false
# 끝에 컨테이너를 지운다. 104 같은 공용 서버에서는 무거운 잠금과 메모리 상한 아래에서 돌린다.
# 쓰는 법(저장소 뿌리에서): civil-petition/deploy/tools/archive-sql-check.sh [규모, 기본 2000000]
#   규모 200만은 시드에 약 5분이 걸린다. 빨리 문법만 볼 때는 20000.
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

# advisory lock: 첫 세션이 잡고 쥐고 있는 동안 둘째 세션은 못 잡는다.
docker exec "$name" sh -c "psql -U postgres -X -d petition -At -c 'SELECT pg_advisory_lock($lock_key)' -c 'SELECT pg_sleep(5)' >/dev/null &"
sleep 2
held=$(psql_in -At -d petition -c "SELECT pg_try_advisory_lock($lock_key)")
echo "CHECK second_session_cannot_lock|$([[ "$held" == f ]] && echo t || echo f)" | tee -a "$report"
checks=$(grep -c '^CHECK ' "$report" || true)
failed=$(grep -c '^CHECK .*|f$' "$report" || true)
echo "checks=$checks failed=$failed"
[[ "$checks" == 7 && "$failed" == 0 ]]
