# 큐 계열 모듈 미리 적재(설계 §2-4). 녹화 밖 정비 창에서만 돈다.
place_files kernel-modules
for module in $MODULES; do
    modprobe "$module"
    lsmod | grep -q "^${module} " || fail "$module 적재 안 됨"
done
log "modules ok: $MODULES"
