# PostgreSQL 16 을 데이터 디스크에 둔다(설계 §2-2: 데이터 디스크를 OS 디스크와 따로).
disk="/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_${DATA_DISK_SERIAL}"
[[ -b "$disk" ]] || fail "데이터 디스크 없음: $disk"
if ! blkid "$disk" >/dev/null 2>&1; then
    mkfs.ext4 -q -L "$DATA_DISK_SERIAL" "$disk"
fi
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
