# 패키지 설치와 예정 없는 갱신 끄기(설계 §2-4). 판 고정은 lib.sh 의 apt_install.
export DEBIAN_FRONTEND=noninteractive
common="chrony rsyslog nftables python3 curl jq ca-certificates iproute2 logrotate"
case "$HOST_ROLE" in
    router)  role="snmpd snmp conntrack tcpdump" ;;
    web)     role="apache2" ;;
    was)     role="openjdk-17-jre-headless tomcat10 libpostgresql-jdbc-java" ;;
    db)      role="postgresql-16 postgresql-client-16" ;;
    loadgen) role="" ;;
    *) fail "모르는 역할 $HOST_ROLE" ;;
esac
apt-get update -q
# openjdk 17 을 먼저 깔아 tomcat10 이 기본 JRE(21)를 끌어오지 않게 한다.
if [[ "$HOST_ROLE" == was ]]; then
    apt_install openjdk-17-jre-headless
fi
# shellcheck disable=SC2086
apt_install $common $role
apt-get purge -y -q unattended-upgrades || true
place_files os-base
for unit in apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service \
            snapd.service snapd.socket snapd.seeded.service; do
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    systemctl mask "$unit" >/dev/null 2>&1 || true
done
touch /etc/cloud/cloud-init.disabled
install -d -m 0755 /var/lib/petition-deploy
dpkg-query -W -f '${Package} ${Version}\n' | sort > /var/lib/petition-deploy/packages.txt
log "packages: $(wc -l < /var/lib/petition-deploy/packages.txt) recorded"
