place_files db-backup
systemctl daemon-reload
systemctl enable --now pg-backup.timer pg-table-stats.timer >/dev/null
# 표 통계 사본은 30분 고정 주기로 평시부터 늘 돈다(설계 §6-4). 설치 때 한 번 돌려 첫 파일을 만든다.
systemctl start pg-table-stats.service
log "backup timer: $(systemctl show -p NextElapseUSecRealtime --value pg-backup.timer), table stats: $(cat /var/lib/pg-table-stats/last.json)"
