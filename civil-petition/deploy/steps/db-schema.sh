# 스키마(같은 파일을 T 와 S 가 쓴다). 다시 돌려도 같다.
verify_artifact schema.sql
# 묶음은 root 만 들어가는 곳(/root/petition-deploy, 700)에 있어 postgres 가 파일을 열 수 없다. root 셸이 열어 표준 입력으로 넘긴다.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d "$DB_NAME" \
    -v app_user="$DB_USER" -f - < "$BUNDLE/artifacts/schema.sql"
log "schema ok"
