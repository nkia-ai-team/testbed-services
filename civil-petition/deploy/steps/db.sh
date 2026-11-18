# PostgreSQL 16 을 데이터 디스크에 둔다(설계 §2-2: 데이터 디스크를 OS 디스크와 따로).
# 디스크는 lsblk SERIAL 로 찾는다(disk_by_serial, lib.sh). 없거나 둘 이상이면 아무것도 바꾸지 않고 멈춘다.
disk=$(disk_by_serial "$DATA_DISK_SERIAL") || fail "데이터 디스크(serial $DATA_DISK_SERIAL)를 하나로 찾지 못함. 아무것도 바꾸지 않음"
# blkid 는 장치가 없을 때도 2 를 돌려주므로 블록 장치인지 먼저 본다.
[[ -b "$disk" ]] || fail "데이터 디스크가 블록 장치가 아님: $disk"
# 파일 시스템이나 파티션 표 같은 서명이 하나라도 있으면 포맷하지 않는다. blkid -p 가 "서명 없음"(2)이라 답할 때만 mkfs 하고,
# 그 밖의 실패(읽기 오류, 서명이 여럿이라 모호함 8 등)는 비어 있다는 뜻이 아니므로 멈춘다.
probe=0
blkid -p "$disk" >/dev/null 2>&1 || probe=$?
case "$probe" in
    0) log "데이터 디스크 $disk 에 서명이 있어 포맷하지 않음" ;;
    2) mkfs.ext4 -q -L "$DATA_DISK_SERIAL" "$disk" ;;
    *) fail "데이터 디스크 $disk 를 blkid 가 읽지 못함(rc $probe). 포맷하지 않음" ;;
esac
install -d -m 0755 "$DATA_MOUNT"
grep -q "^LABEL=${DATA_DISK_SERIAL} " /etc/fstab \
    || echo "LABEL=${DATA_DISK_SERIAL} ${DATA_MOUNT} ext4 defaults,noatime 0 2" >> /etc/fstab
mountpoint -q "$DATA_MOUNT" || mount "$DATA_MOUNT"
datadir="$DATA_MOUNT/16/main"
current=$(pg_lsclusters -h 2>/dev/null | awk '$1=="16" && $2=="main"{print $6}')
if [[ "$current" != "$datadir" ]]; then
    if [[ -n "$current" ]]; then
        pg_dropcluster --stop 16 main
    fi
    install -d -m 0700 -o postgres -g postgres "$DATA_MOUNT/16"
    # 로케일은 Ubuntu 클라우드 이미지 기본(C.UTF-8)을 명시해 T 와 S 가 같게 한다. glibc 언어별 정렬 규칙을 쓰면
    # 거의 같은 긴 본문의 비교가 매우 느려 시드 뒤 ANALYZE 가 수 분 걸렸다(2026-11-16 로컬 PostgreSQL 16 시험).
    pg_createcluster --locale C.UTF-8 16 main -d "$datadir" -- --data-checksums >/dev/null
fi
install -d -m 0750 -o postgres -g postgres "$DATA_MOUNT/backup"
place_files db
restart_enabled postgresql@16-main
# 막 다시 띄운 PostgreSQL 이 연결을 받을 때까지 다시 읽는다(settle, lib.sh).
settle "$SETTLE_SEC" "$SETTLE_EVERY" "PostgreSQL 응답 없음" runuser -u postgres -- psql -Atqc "select 1"
# 앱 계정과 DB. 비밀번호 문장이 서버 로그(log_statement=ddl)에 남지 않게 이 세션만 문장 기록을 끈다.
password=$(read_secret db_password)
[[ "$password" =~ ^[A-Za-z0-9]{16,}$ ]] || fail "DB 비밀번호는 영문, 숫자 16자 이상이어야 함(XML 속성과 SQL 따옴표 안전)"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q <<SQL
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SELECT format('CREATE ROLE %I LOGIN', '$DB_USER') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER') \gexec
ALTER ROLE "$DB_USER" PASSWORD '$password';
SELECT format('CREATE DATABASE %I OWNER %I', '$DB_NAME', '$DB_USER') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$DB_NAME') \gexec
SQL
unset password
log "db ok: $(runuser -u postgres -- psql -Atqc 'show data_directory')"
