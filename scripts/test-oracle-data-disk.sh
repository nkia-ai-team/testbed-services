#!/usr/bin/env bash
# Side-effect-free tests for oracle-data-disk.sh. The mount table is a file: mount and umount are fakes
# that swap the mount point for a symlink to a directory standing in for the disk, and findmnt,
# mountpoint, blkid, lsblk, mkfs.ext4, chattr, systemctl and pgrep answer from that table and from
# FAKE_* variables. rsync, mv and mkdir are real, so the copy, the verification and the rename run
# for real on temporary directories.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOL="$SCRIPT_DIR/oracle-data-disk.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

DEV=$(find /dev -maxdepth 1 -type b | head -1)
[[ -n "$DEV" ]] || fail "this host has no block device to stand for the disk"
mkdir -p "$TMP_ROOT/bin"
export FAKE_DEV="$DEV" FAKE_DISK="$TMP_ROOT/disk" FAKE_TABLE="$TMP_ROOT/mounts" FAKE_LOG="$TMP_ROOT/calls"
export PV_DIR="$TMP_ROOT/lp/pvc-x_rca-testbed-banking_oracledata-testbed-oracle-0"
export ORIG_DIR="$PV_DIR.orig" FSTAB="$TMP_ROOT/fstab" STAGING="$TMP_ROOT/staging"

# rsync is real; FAKE_CORRUPT names a file to change right after the first copying (not dry-run)
# call, standing for a copy that went wrong.
REAL_RSYNC=$(command -v rsync)
cat >"$TMP_ROOT/bin/rsync" <<SH
#!/usr/bin/env bash
case " \$* " in *" -aHAXn "*) [[ -n "\${FAKE_DRYRUN_FAIL:-}" ]] && exit 23 ;; esac   # fails with no output
"$REAL_RSYNC" "\$@" || exit \$?
case " \$* " in *" -aHAXn "*) exit 0 ;; esac
if [[ -n "\${FAKE_CORRUPT:-}" && ! -e "\$FAKE_LOG.corrupted" ]]; then
  printf 'x' >>"\$FAKE_CORRUPT"; : >"\$FAKE_LOG.corrupted"
fi
SH
cat >"$TMP_ROOT/bin/mount" <<'SH'
#!/usr/bin/env bash
printf 'mount %s\n' "$*" >>"$FAKE_LOG"
if [[ "$1" == -o && "$2" == ro ]]; then [[ -n "${FAKE_RO_FAIL:-}" ]] && exit 32; shift 2; fi
if [[ $# -eq 1 ]]; then [[ -n "${FAKE_MOUNT_FAIL:-}" ]] && exit 32
  target=$1; src=$(awk -v t="$1" '$2 == t {print $1}' "$FSTAB")
  [[ "$src" == UUID=fake-uuid ]] || exit 32; src=$FAKE_DEV
else src=$1; target=$2; fi
[[ "$src" == "$FAKE_DEV" ]] || exit 32
rmdir "$target" || exit 32
ln -s "$FAKE_DISK" "$target"
printf '%s %s\n' "$src" "$target" >>"$FAKE_TABLE"
SH
cat >"$TMP_ROOT/bin/umount" <<'SH'
#!/usr/bin/env bash
printf 'umount %s\n' "$*" >>"$FAKE_LOG"
[[ -L "$1" ]] || exit 32
rm "$1"; mkdir "$1"
grep -v " $1\$" "$FAKE_TABLE" >"$FAKE_TABLE.n" || true; mv "$FAKE_TABLE.n" "$FAKE_TABLE"
SH
cat >"$TMP_ROOT/bin/mountpoint" <<'SH'
#!/usr/bin/env bash
[[ "$1" == -q ]] && shift
awk -v t="$1" '$2 == t {f=1} END {exit !f}' "$FAKE_TABLE"
SH
cat >"$TMP_ROOT/bin/findmnt" <<'SH'
#!/usr/bin/env bash
args=" $* "
if [[ "$args" == *" -o FSROOT "* ]]; then printf '/\n%s' "${FAKE_FSROOTS:-}"; [[ -n "${FAKE_FSROOTS:-}" ]] && echo; exit 0; fi
if [[ "$args" == *" --target "* ]]; then t=${args##* --target }; t=${t%% *}; awk -v t="$t" '$2 == t {print $1}' "$FAKE_TABLE"; exit 0; fi
if [[ "$args" == *" -S "* ]]; then s=${args##* -S }; s=${s%% *}; awk -v s="$s" '$1 == s {print $2}' "$FAKE_TABLE"; printf '%s' "${FAKE_EXTRA_TARGETS:-}"; [[ -n "${FAKE_EXTRA_TARGETS:-}" ]] && echo; exit 0; fi
exit 1
SH
cat >"$TMP_ROOT/bin/blkid" <<'SH'
#!/usr/bin/env bash
args=" $* "
labelled() { [[ -f "$FAKE_DISK.label" ]]; }
if [[ "$args" == *" -L "* ]]; then labelled && echo "$FAKE_DEV"; exit 0; fi
if [[ "$args" == *" -U fake-uuid "* ]]; then labelled && echo "$FAKE_DEV"; exit 0; fi
if [[ "$args" == *" -U "* ]]; then exit 2; fi
if [[ "$args" == *" -s LABEL "* ]]; then labelled && cat "$FAKE_DISK.label"; exit 0; fi
if [[ "$args" == *" -s UUID "* ]]; then labelled && echo fake-uuid; exit 0; fi
if [[ "$args" == *" -s TYPE "* ]]; then { labelled && echo ext4; } || printf '%s' "${FAKE_SIGNATURE:-}"; exit 0; fi
exit 2
SH
cat >"$TMP_ROOT/bin/lsblk" <<'SH'
#!/usr/bin/env bash
args=" $* "
if [[ "$args" == *" TYPE "* ]]; then echo "${FAKE_TYPE:-disk}"; exit 0; fi
if [[ "$args" == *" SIZE "* ]]; then echo "${FAKE_SIZE:-25769803776}"; exit 0; fi
if [[ "$args" == *" NAME "* ]]; then echo sdb; [[ -n "${FAKE_PARTITIONED:-}" ]] && echo sdb1; exit 0; fi
exit 1
SH
cat >"$TMP_ROOT/bin/mkfs.ext4" <<'SH'
#!/usr/bin/env bash
printf 'mkfs.ext4 %s\n' "$*" >>"$FAKE_LOG"
[[ " $* " == *" -m 0 "* && " $* " == *" -L oradata "* ]] || exit 9
mkdir -p "$FAKE_DISK"; echo oradata >"$FAKE_DISK.label"; mkdir -p "$FAKE_DISK/lost+found"
SH
for name in chattr systemctl chown; do
  printf '#!/usr/bin/env bash\nprintf "%s %%s\\n" "$*" >>"$FAKE_LOG"\n' "$name" >"$TMP_ROOT/bin/$name"
done
cat >"$TMP_ROOT/bin/pgrep" <<'SH'
#!/usr/bin/env bash
[[ -n "${FAKE_PMON:-}" ]]
SH
chmod +x "$TMP_ROOT"/bin/*
export PATH="$TMP_ROOT/bin:$PATH"

reset() {
  rm -rf "$TMP_ROOT/lp" "$FAKE_DISK" "$FAKE_DISK.label" "$STAGING" "$TMP_ROOT/by-id-link"; : >"$FAKE_TABLE"; : >"$FAKE_LOG"
  rm -f "$FAKE_LOG.corrupted"
  printf 'LABEL=cloudimg-rootfs / ext4 discard 0 1\n' >"$FSTAB"
  unset FAKE_PMON FAKE_FSROOTS FAKE_EXTRA_TARGETS FAKE_SIGNATURE FAKE_PARTITIONED FAKE_SIZE FAKE_TYPE FAKE_CORRUPT FAKE_MOUNT_FAIL FAKE_RO_FAIL FAKE_DRYRUN_FAIL
  mkdir -p "$PV_DIR/FREE/FREEPDB1" "$PV_DIR/diag/rdbms" "$PV_DIR/dbconfig/FREE" "$FAKE_DISK"
  chmod 0777 "$PV_DIR"
  head -c 300000 /dev/urandom >"$PV_DIR/FREE/FREEPDB1/users01.dbf"
  truncate -s 4M "$PV_DIR/FREE/temp01.dbf"   # sparse, as Oracle temp files are
  printf 'alert\n' >"$PV_DIR/diag/rdbms/alert.log"
  ln -s ../FREE "$PV_DIR/dbconfig/FREE/link"
}
expect_fail() { local what=$1; shift; if "$@" >/dev/null 2>&1; then fail "$what: expected a refusal"; fi; }
tree_sum() { (cd "$1" && find . -path ./lost+found -prune -o -print0 | sort -z | xargs -0 stat -c '%n %a %F' && find . -type f -print0 | sort -z | xargs -0 sha256sum); }

# prepare: only an empty whole disk of the expected size, once.
reset; rm -rf "$FAKE_DISK"
FAKE_PARTITIONED=1 expect_fail "partitioned disk" bash "$TOOL" prepare "$DEV"
FAKE_SIGNATURE=xfs expect_fail "disk with a signature" bash "$TOOL" prepare "$DEV"
FAKE_SIZE=$((8 * 1024 * 1024 * 1024)) expect_fail "disk too small" bash "$TOOL" prepare "$DEV"
FAKE_TYPE=part expect_fail "a partition" bash "$TOOL" prepare "$DEV"
expect_fail "not a block device" bash "$TOOL" prepare "$TMP_ROOT/fstab"
bash "$TOOL" prepare "$DEV" >/dev/null
grep -q 'mkfs.ext4 -q -L oradata -m 0' "$FAKE_LOG" || fail "mkfs must label oradata with no reserved blocks"
[[ "$(bash "$TOOL" prepare "$DEV")" == *"already carries oradata"* ]] || fail "prepare is not idempotent"
[[ $(grep -c '^mkfs.ext4' "$FAKE_LOG") -eq 1 ]] || fail "prepare made the file system twice"
echo "[PASS] prepare refuses partitions, signatures, sizes and non-disks and runs once"

# cutover refuses while Oracle or its subPath mounts are alive, and on an unprepared disk.
reset; bash "$TOOL" prepare "$DEV" >/dev/null
FAKE_PMON=1 expect_fail "Oracle running" bash "$TOOL" cutover "$DEV"
FAKE_FSROOTS="$PV_DIR/diag" expect_fail "subPath bind mount alive" bash "$TOOL" cutover "$DEV"
[[ -d "$PV_DIR" && ! -L "$PV_DIR" && ! -e "$ORIG_DIR" ]] || fail "a refused cutover changed the directory"
rm "$FAKE_DISK.label"; expect_fail "unprepared disk" bash "$TOOL" cutover "$DEV"
echo "[PASS] cutover refuses a running Oracle, live subPath mounts and an unprepared disk"

# cutover: identical copy, original renamed and untouched, immutable mount point, one fstab line.
reset; bash "$TOOL" prepare "$DEV" >/dev/null
before=$(tree_sum "$PV_DIR")
ln -s "$DEV" "$TMP_ROOT/by-id-link"   # the procedure names the disk by its stable /dev/disk/by-id link
bash "$TOOL" cutover "$TMP_ROOT/by-id-link" >/dev/null
[[ "$(tree_sum "$FAKE_DISK")" == "$before" ]] || fail "the disk does not hold an identical copy"
[[ "$(tree_sum "$ORIG_DIR")" == "$before" ]] || fail "the original copy changed"
[[ -L "$PV_DIR" && "$(readlink "$PV_DIR")" == "$FAKE_DISK" ]] || fail "the volume path is not mounted from the disk"
[[ "$(stat -c %a "$FAKE_DISK")" == 777 ]] || fail "the disk's root directory did not get the volume directory's mode"
[[ $(du -k "$FAKE_DISK/FREE/temp01.dbf" | cut -f1) -lt 1024 ]] || fail "the sparse temp file was filled in"
grep -q "^chattr +i $PV_DIR\$" "$FAKE_LOG" || fail "the empty mount point is not made immutable"
grep -q '^systemctl daemon-reload' "$FAKE_LOG" || fail "systemd was not told about the new fstab line"
[[ $(grep -c " $PV_DIR " "$FSTAB") -eq 1 ]] || fail "expected exactly one fstab line"
grep -qx "UUID=fake-uuid $PV_DIR ext4 defaults,nofail 0 2" "$FSTAB" || fail "fstab line: $(cat "$FSTAB")"
[[ ! -L "$STAGING" ]] || fail "the staging mount was left behind"
[[ "$(bash "$TOOL" cutover "$DEV")" == *"already cut over"* ]] || fail "cutover is not idempotent"
report=$(bash "$TOOL" check)
grep -q "original copy kept at $ORIG_DIR" <<<"$report" || fail "check does not report the original copy"
grep -q "mounted from $DEV" <<<"$report" || fail "check does not report the mount"
echo "[PASS] cutover copies identically, keeps the original and mounts the disk at the volume path"

# rollback refuses while Oracle runs or the disk is still bound into a pod.
FAKE_PMON=1 expect_fail "rollback with Oracle running" bash "$TOOL" rollback
FAKE_EXTRA_TARGETS=/var/lib/kubelet/pods/p/volume-subpaths/x/oracle/1 expect_fail "rollback with a subPath mount" bash "$TOOL" rollback
expect_fail "unknown rollback mode" bash "$TOOL" rollback --sideways

# rollback (default): data written after the cutover comes back, the disk keeps its copy.
printf 'after\n' >"$PV_DIR/FREE/new.log"
rm "$PV_DIR/diag/rdbms/alert.log"   # removed after the cutover: must be gone from the restored copy too
bash "$TOOL" rollback >/dev/null
[[ ! -e "$PV_DIR/diag/rdbms/alert.log" ]] || fail "rollback kept a file deleted after the cutover"
[[ -d "$PV_DIR" && ! -L "$PV_DIR" && ! -e "$ORIG_DIR" ]] || fail "rollback did not restore the directory"
[[ "$(cat "$PV_DIR/FREE/new.log")" == after ]] || fail "rollback lost data written after the cutover"
[[ -f "$FAKE_DISK/FREE/new.log" ]] || fail "rollback emptied the disk"
! grep -q " $PV_DIR " "$FSTAB" || fail "rollback left the fstab line"
grep -q '^LABEL=cloudimg-rootfs' "$FSTAB" || fail "rollback touched other fstab lines"
grep -q "^chattr -i $PV_DIR\$" "$FAKE_LOG" || fail "rollback did not lift the immutable flag"
echo "[PASS] rollback brings current data back and keeps the disk"

# rollback --to-cutover: back to the copy taken at cutover.
reset; bash "$TOOL" prepare "$DEV" >/dev/null; before=$(tree_sum "$PV_DIR")
bash "$TOOL" cutover "$DEV" >/dev/null
printf 'after\n' >"$PV_DIR/FREE/new.log"
bash "$TOOL" rollback --to-cutover >/dev/null
[[ "$(tree_sum "$PV_DIR")" == "$before" ]] || fail "--to-cutover did not return the cutover copy"
echo "[PASS] rollback --to-cutover returns the copy taken at cutover"

# A copy that differs from the original stops the cutover before anything is renamed.
reset; bash "$TOOL" prepare "$DEV" >/dev/null; before=$(tree_sum "$PV_DIR"); cp "$FSTAB" "$TMP_ROOT/fstab.before"
FAKE_CORRUPT="$FAKE_DISK/FREE/FREEPDB1/users01.dbf" expect_fail "differing copy" bash "$TOOL" cutover "$DEV"
[[ -d "$PV_DIR" && ! -L "$PV_DIR" && ! -e "$ORIG_DIR" ]] || fail "a differing copy still renamed the original"
[[ "$(tree_sum "$PV_DIR")" == "$before" ]] || fail "a differing copy changed the original"
cmp -s "$FSTAB" "$TMP_ROOT/fstab.before" || fail "a differing copy touched fstab"
[[ ! -s "$FAKE_TABLE" ]] || fail "a differing copy left something mounted: $(cat "$FAKE_TABLE")"
echo "[PASS] cutover refuses a copy that differs and leaves everything as it was"

# The disk does not mount after the rename: check says so, cutover will not run again, rollback undoes it.
reset; bash "$TOOL" prepare "$DEV" >/dev/null; before=$(tree_sum "$PV_DIR")
FAKE_MOUNT_FAIL=1 expect_fail "disk that does not mount" bash "$TOOL" cutover "$DEV"
[[ -d "$ORIG_DIR" && -d "$PV_DIR" && ! -L "$PV_DIR" && -z "$(ls -A "$PV_DIR")" ]] || fail "not the interrupted state"
[[ "$(bash "$TOOL" check)" == *"INTERRUPTED cutover"* ]] || fail "check does not report the interrupted cutover"
expect_fail "cutover over an interrupted one" bash "$TOOL" cutover "$DEV"
FAKE_PMON=1 expect_fail "rollback of an interrupted cutover with Oracle running" bash "$TOOL" rollback
bash "$TOOL" rollback >/dev/null || fail "rollback could not undo the interrupted cutover"
[[ "$(tree_sum "$PV_DIR")" == "$before" && ! -e "$ORIG_DIR" ]] || fail "rollback did not bring the original back"
! grep -q " $PV_DIR " "$FSTAB" || fail "rollback of the interrupted cutover left the fstab line"
grep -q "^chattr -i $PV_DIR\$" "$FAKE_LOG" || fail "rollback did not lift the immutable flag"
echo "[PASS] an interrupted cutover is reported by check and undone by rollback"

# A rollback copy that differs stops before the disk is unmounted.
reset; bash "$TOOL" prepare "$DEV" >/dev/null; bash "$TOOL" cutover "$DEV" >/dev/null
FAKE_CORRUPT="$ORIG_DIR/FREE/FREEPDB1/users01.dbf" expect_fail "differing rollback copy" bash "$TOOL" rollback
[[ -L "$PV_DIR" ]] || fail "a differing rollback copy still unmounted the disk"
grep -q " $PV_DIR " "$FSTAB" || fail "a differing rollback copy removed the fstab line"
echo "[PASS] rollback refuses a copy back that differs and stays on the disk"

# (a) After a finished cutover and writes, the node boots without the disk: not an interrupted cutover.
reset; bash "$TOOL" prepare "$DEV" >/dev/null; bash "$TOOL" cutover "$DEV" >/dev/null
printf 'after\n' >"$PV_DIR/FREE/new.log"
umount "$PV_DIR"; mv "$FAKE_DISK.label" "$TMP_ROOT/label.away"   # disk gone: blkid finds no such UUID
report=$(bash "$TOOL" check)
[[ "$report" != *INTERRUPTED* && "$report" == *"DISK NOT ATTACHED"* ]] || fail "check must not call a missing disk an interrupted cutover: $report"
expect_fail "rollback without the disk" bash "$TOOL" rollback
expect_fail "rollback --to-cutover without the disk" bash "$TOOL" rollback --to-cutover
[[ -d "$PV_DIR" && -z "$(ls -A "$PV_DIR")" && -d "$ORIG_DIR" ]] || fail "a refused rollback changed the directories"
[[ $(grep -c " $PV_DIR " "$FSTAB") -eq 1 ]] || fail "a refused rollback touched fstab"
echo "[PASS] a missing disk after a finished cutover is reported and rollback refuses"

# (b) Same, but the disk is attached and holds data written after the cutover: refuse, say mount.
mv "$TMP_ROOT/label.away" "$FAKE_DISK.label"
[[ "$(bash "$TOOL" check)" == *"holds data written after the cutover"* ]] || fail "check must tell to mount the disk"
expect_fail "rollback over newer data on the disk" bash "$TOOL" rollback
[[ -d "$PV_DIR" && -z "$(ls -A "$PV_DIR")" && -d "$ORIG_DIR" && ! -s "$FAKE_TABLE" ]] || fail "a refused rollback changed the state"
[[ $(grep -c " $PV_DIR " "$FSTAB") -eq 1 ]] || fail "a refused rollback touched fstab"
mount "$PV_DIR"; [[ -f "$PV_DIR/FREE/new.log" ]] || fail "mounting the disk must bring the newer data back"
echo "[PASS] a disk with newer data than the original copy is not rolled back; mounting it restores the volume"

# The cutover stopped between the rename and mkdir: the volume path is missing, fstab untouched.
reset; bash "$TOOL" prepare "$DEV" >/dev/null; before=$(tree_sum "$PV_DIR")
mv -T "$PV_DIR" "$ORIG_DIR"
[[ "$(bash "$TOOL" check)" == *"INTERRUPTED cutover"* ]] || fail "check must report a missing volume path with an original copy"
expect_fail "cutover over a stopped one" bash "$TOOL" cutover "$DEV"
bash "$TOOL" rollback >/dev/null || fail "rollback must undo a cutover stopped before mkdir"
[[ "$(tree_sum "$PV_DIR")" == "$before" && ! -e "$ORIG_DIR" ]] || fail "rollback did not bring the original back"
echo "[PASS] a cutover stopped before the mount point existed is reported and undone"

# A disk that cannot be compared (read-only mount refused, or the comparison fails without output) is
# neither an interrupted cutover nor newer data: check says so, rollback refuses, nothing changes.
# The look at the disk is read-only.
interrupted_with_disk() {  # the cutover stopped after fstab named the disk, the disk holds the original copy
  reset; bash "$TOOL" prepare "$DEV" >/dev/null; before=$(tree_sum "$PV_DIR")
  FAKE_MOUNT_FAIL=1 expect_fail "disk that does not mount" bash "$TOOL" cutover "$DEV"
  : >"$FAKE_LOG"
}
interrupted_with_disk
report=$(bash "$TOOL" check)
[[ "$report" == *"INTERRUPTED cutover"* ]] || fail "precondition: $report"
mounts=$(grep '^mount ' "$FAKE_LOG")
[[ -n "$mounts" ]] && ! grep -v "^mount -o ro $DEV " <<<"$mounts" | grep -q . || fail "check may mount the disk read-only only: $mounts"
for knob in FAKE_RO_FAIL FAKE_DRYRUN_FAIL; do
  interrupted_with_disk
  report=$(env "$knob=1" bash "$TOOL" check)
  [[ "$report" == *"COULD NOT READ"* && "$report" != *INTERRUPTED* && "$report" != *"written after"* ]] \
    || fail "$knob: an unreadable disk must be reported as such: $report"
  env "$knob=1" bash "$TOOL" rollback >/dev/null 2>&1 && fail "$knob: rollback must refuse an unreadable disk"
  [[ -d "$ORIG_DIR" && -d "$PV_DIR" && -z "$(ls -A "$PV_DIR")" && ! -s "$FAKE_TABLE" ]] || fail "$knob: a refused rollback changed the state"
  [[ $(grep -c " $PV_DIR " "$FSTAB") -eq 1 ]] || fail "$knob: a refused rollback touched fstab"
done
echo "[PASS] the disk is only looked at read-only, and a disk that cannot be compared is not rolled back"

# Content of its own at the volume path next to an original copy: hands off.
reset; bash "$TOOL" prepare "$DEV" >/dev/null
mv -T "$PV_DIR" "$ORIG_DIR"; mkdir "$PV_DIR"; printf 'new database?\n' >"$PV_DIR/stray"
[[ "$(bash "$TOOL" check)" == *"inspect by hand"* ]] || fail "check must hand a non-empty volume path to a person"
expect_fail "rollback over content of its own" bash "$TOOL" rollback
[[ -f "$PV_DIR/stray" && -d "$ORIG_DIR" ]] || fail "a refused rollback changed the directories"
echo "[PASS] a volume path with its own content next to an original copy is left to a person"
