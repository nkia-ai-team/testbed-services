# 스키마(같은 파일을 T 와 S 가 쓴다). 다시 돌려도 같다.
verify_artifact schema.sql
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d "$DB_NAME" \
    -v app_user="$DB_USER" -f "$BUNDLE/artifacts/schema.sql"
log "schema ok"
