#!/usr/bin/env bash
# Banking Oracle data on its own virtual disk (testbed v4 E7, runbook §8-3).
#
# Why: the Oracle volume is a local-path directory on the node's root file system. Filling it, or
# saturating its device, fills or saturates the node itself: kubelet's DiskPressure then evicts the
# entry pods first and the cause disappears behind a node-wide event (F10-R parked, 10-16 create run).
# On a disk of its own the database can run out of space or I/O while the node stays healthy;
# kubelet watches only the file systems of its root and of the image store.
#
# How: the new disk is mounted at the same path the PersistentVolume already names, so Kubernetes
# objects (PV, PVC, StatefulSet) stay exactly as they are. The original directory is renamed, not
# deleted: it is the cold copy to go back to. The empty mount point is made immutable, so if the disk
# is ever not mounted Oracle cannot start on an empty directory (it would create a new database);
# the pod fails instead, which is loud. The fstab line uses nofail so a missing disk never blocks boot.
#
# Run on the banking worker as root, Oracle stopped (StatefulSet scaled to 0) for cutover and
# rollback:
#   oracle-data-disk.sh check
#   oracle-data-disk.sh prepare <device>     # make the file system on an empty whole disk
#   oracle-data-disk.sh cutover <device>     # copy, verify, swap (Oracle must be stopped)
#   oracle-data-disk.sh rollback [--to-cutover]
#                                           # copy back the current data (default) or keep the
#                                           # copy taken at cutover, then restore the directory
# Nothing here deletes data: the original directory, the disk and its contents all stay.
set -Eeuo pipefail

PV_DIR=${PV_DIR:-/opt/local-path-provisioner/pvc-899a2f44-e397-4109-9e59-2bd6d7d5cd3a_rca-testbed-banking_oracledata-testbed-oracle-0}
ORIG_DIR=${ORIG_DIR:-$PV_DIR.orig}
LABEL=${LABEL:-oradata}
FSTAB=${FSTAB:-/etc/fstab}
STAGING=${STAGING:-/run/oradata-staging}
MIN_DISK_BYTES=${MIN_DISK_BYTES:-$((20 * 1024 * 1024 * 1024))}
MAX_DISK_BYTES=${MAX_DISK_BYTES:-$((64 * 1024 * 1024 * 1024))}
# Process name of the Oracle instance (the container runs in the node's process table).
PMON_PATTERN=${PMON_PATTERN:-ora_pmon_FREE}

die() { printf 'oracle-data-disk: %s\n' "$*" >&2; exit 1; }
say() { printf 'oracle-data-disk: %s\n' "$*"; }

whole_empty_disk() {
  local dev=$1 type size
  [[ -b "$dev" ]] || die "$dev is not a block device"
  type=$(lsblk -dn -o TYPE "$dev")
  [[ "$type" == disk ]] || die "$dev is a $type, not a whole disk"
  [[ -z "$(lsblk -n -o NAME "$dev" | tail -n +2)" ]] || die "$dev has partitions"
  size=$(lsblk -bdn -o SIZE "$dev")
  (( size >= MIN_DISK_BYTES && size <= MAX_DISK_BYTES )) || die "$dev is $size bytes, outside the expected range"
  [[ -z "$(findmnt -rn -S "$dev" -o TARGET)" ]] || die "$dev is mounted"
}

ours() { [[ "$(blkid -o value -s LABEL "$1" 2>/dev/null)" == "$LABEL" ]]; }

oracle_stopped() {
  if pgrep -f "$PMON_PATTERN" >/dev/null; then die "Oracle is running on this node ($PMON_PATTERN)"; fi
  # While the pod exists kubelet keeps subPath bind mounts into the volume: from the root file system
  # they show the volume path as their root, from the data disk they are a second mount of it.
  if findmnt -rn -o FSROOT | grep -Fq -- "$PV_DIR/"; then die "something is still bind-mounted from $PV_DIR"; fi
  if mountpoint -q "$PV_DIR"; then
    local dev; dev=$(findmnt -n -o SOURCE --target "$PV_DIR")
    if findmnt -rn -S "$dev" -o TARGET | grep -vFxq -- "$PV_DIR"; then die "$dev is still mounted elsewhere"; fi
  fi
}

fstab_line() { printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' "$1" "$PV_DIR"; }

# fstab is replaced, never edited in place: a node that stops halfway keeps the old or the new file.
fstab_rewrite() {  # fstab_rewrite add <uuid> | fstab_rewrite drop
  local tmp; tmp=$(mktemp "$FSTAB.XXXXXX")
  grep -vF " $PV_DIR " "$FSTAB" >"$tmp" || true
  [[ "$1" == add ]] && fstab_line "$2" >>"$tmp"
  chmod --reference="$FSTAB" "$tmp"
  sync "$tmp"
  mv -f "$tmp" "$FSTAB"
  sync "$(dirname "$FSTAB")"
}

# Same content, ownership and modes on both sides: a checksum pass that would change nothing.
same_tree() {
  local diff; diff=$(rsync -aHAXn --checksum --numeric-ids --delete --exclude=/lost+found --itemize-changes "$1"/ "$2"/)
  [[ -z "$diff" ]] || die "copy differs from its source: $diff"
}

# A cutover that stopped after the rename (the disk did not mount, or the node stopped): the volume
# path is an empty directory, the data is in the original copy and fstab may already name the disk.
interrupted() { ! mountpoint -q "$PV_DIR" && [[ -d "$ORIG_DIR" && -d "$PV_DIR" && -z "$(ls -A "$PV_DIR")" ]]; }

check() {
  say "volume directory: $PV_DIR"
  if mountpoint -q "$PV_DIR"; then
    say "mounted from $(findmnt -n -o SOURCE --target "$PV_DIR") ($(df -h --output=size,used,avail,pcent "$PV_DIR" | tail -1 | xargs))"
  elif interrupted; then
    say "INTERRUPTED cutover: $PV_DIR is empty and not mounted, the data is in $ORIG_DIR; Oracle must not start. Run rollback"
  elif [[ -d "$ORIG_DIR" ]]; then
    say "not a mount point but $PV_DIR is not empty while $ORIG_DIR exists: inspect by hand"
  else
    say "not a mount point (data on the node's root file system)"
  fi
  [[ -d "$ORIG_DIR" ]] && say "original copy kept at $ORIG_DIR"
  grep -F " $PV_DIR " "$FSTAB" >/dev/null && say "fstab: $(grep -F " $PV_DIR " "$FSTAB")"
  local dev; dev=$(blkid -L "$LABEL" 2>/dev/null || true)
  [[ -n "$dev" ]] && say "disk labelled $LABEL: $dev"
  return 0
}

prepare() {
  # Stable names (/dev/disk/by-id/...) are links; mount tables show the device itself.
  local dev; dev=$(readlink -f "${1:?device}")
  if ours "$dev"; then say "$dev already carries $LABEL"; return 0; fi
  whole_empty_disk "$dev"
  [[ -z "$(blkid -p -o value -s TYPE "$dev" 2>/dev/null)" ]] || die "$dev already has a file system or signature"
  # -m 0: no root-reserved blocks, so "full" means full for the oracle user too.
  mkfs.ext4 -q -L "$LABEL" -m 0 "$dev"
  say "made ext4 $LABEL on $dev (UUID $(blkid -o value -s UUID "$dev"))"
}

cutover() {
  local dev uuid
  dev=$(readlink -f "${1:?device}")
  ours "$dev" || die "$dev is not the prepared $LABEL disk"
  if mountpoint -q "$PV_DIR"; then
    [[ "$(findmnt -n -o SOURCE --target "$PV_DIR")" == "$dev" ]] || die "$PV_DIR is mounted from something else"
    say "already cut over"; return 0
  fi
  interrupted && die "an earlier cutover stopped after the rename; run rollback first"
  [[ -d "$PV_DIR" ]] || die "$PV_DIR does not exist"
  [[ ! -e "$ORIG_DIR" ]] || die "$ORIG_DIR already exists"
  oracle_stopped
  uuid=$(blkid -o value -s UUID "$dev")
  mkdir -p "$STAGING"
  mount "$dev" "$STAGING"
  trap 'mountpoint -q "$STAGING" && umount "$STAGING"' EXIT
  rsync -aHAX --sparse --numeric-ids "$PV_DIR"/ "$STAGING"/
  # rsync -a with trailing slashes gives the disk's root directory the volume directory's owner and
  # mode (root 0777); the check below compares the root directory too. Nothing is swapped unless the
  # copy is identical.
  same_tree "$PV_DIR" "$STAGING"
  umount "$STAGING"; trap - EXIT   # unmounting writes the copy out
  # From here on a failure leaves the interrupted state that check reports and rollback undoes.
  trap 'say "cutover stopped after the rename: run rollback before starting Oracle"' ERR
  mv -T "$PV_DIR" "$ORIG_DIR"
  mkdir "$PV_DIR"
  chattr +i "$PV_DIR"
  sync "$(dirname "$PV_DIR")"
  fstab_rewrite add "$uuid"
  systemctl daemon-reload
  mount "$PV_DIR"
  [[ "$(findmnt -n -o SOURCE --target "$PV_DIR")" == "$dev" ]] || die "$PV_DIR did not mount from $dev; run rollback"
  trap - ERR
  say "cut over: $PV_DIR now on $dev, original kept at $ORIG_DIR"
}

# Put the original directory back at the volume path (the mount point is already gone).
restore_dir() {
  fstab_rewrite drop
  systemctl daemon-reload
  chattr -i "$PV_DIR"
  rmdir "$PV_DIR"
  mv -T "$ORIG_DIR" "$PV_DIR"
  sync "$(dirname "$PV_DIR")"
}

rollback() {
  local mode=${1:-current} dev
  [[ "$mode" == current || "$mode" == --to-cutover ]] || die "unknown rollback mode $mode"
  [[ -d "$ORIG_DIR" ]] || die "no original copy at $ORIG_DIR"
  oracle_stopped
  if interrupted; then
    # The disk never took over, so the original copy is the current data whatever the mode.
    restore_dir
    say "rolled back an interrupted cutover: $PV_DIR is the original directory again"
    return 0
  fi
  mountpoint -q "$PV_DIR" || die "$PV_DIR is not mounted and not empty; nothing safe to do automatically"
  dev=$(findmnt -n -o SOURCE --target "$PV_DIR")
  if [[ "$mode" == current ]]; then
    # Bring the data written since the cutover back; the disk keeps its copy as well.
    rsync -aHAX --sparse --numeric-ids --delete --exclude=/lost+found "$PV_DIR"/ "$ORIG_DIR"/
    sync "$ORIG_DIR"
    same_tree "$PV_DIR" "$ORIG_DIR"
  fi
  umount "$PV_DIR"
  restore_dir
  say "rolled back ($mode): $PV_DIR is on the root file system again; $dev still holds its copy"
}

action=${1:-check}; shift || true
case "$action" in
  check) check ;;
  prepare) prepare "$@" ;;
  cutover) cutover "$@" ;;
  rollback) rollback "$@" ;;
  *) die "unknown action $action" ;;
esac
