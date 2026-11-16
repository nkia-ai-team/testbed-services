# 야간 배치 타이머(이 WAS 하나에서만, 설계 §2-2).
place_files was-batch
systemctl daemon-reload
systemctl enable --now petition-stats.timer petition-archive.timer >/dev/null
log "batch timers ok"
