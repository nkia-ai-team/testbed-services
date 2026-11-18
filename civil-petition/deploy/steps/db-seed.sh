# 시드(민원 200만, 처리 이력 약 450만, 사용자 5만, 부서 300). 비어 있을 때만 넣는다.
verify_artifact seed.sql
count=$(runuser -u postgres -- psql -Atq -d "$DB_NAME" -c "select count(*) from petition")
if [[ "$count" == 0 ]]; then
    # 묶음은 postgres 가 열 수 없는 곳에 있어 root 셸이 열어 표준 입력으로 넘긴다(db-schema 와 같다).
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d "$DB_NAME" -v app_user="$DB_USER" -f - < "$BUNDLE/artifacts/seed.sql"
fi
runuser -u postgres -- psql -Atq -d "$DB_NAME" -c \
    "select 'petition='||(select count(*) from petition)||' history='||(select count(*) from petition_history)||' users='||(select count(*) from app_user)||' departments='||(select count(*) from department)"
