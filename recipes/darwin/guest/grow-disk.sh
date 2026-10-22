#!/bin/sh
# esa-guest-step: grow-disk
# Grow the APFS container after `tart set --disk-size` enlarged disk.img and
# report its size before and after; the host requires actual growth.
# Sequence from Tart's disk-resizing FAQ; native qualification confirms it.
#
# This script arrives on the shell's stdin, so the body runs as one function
# with stdin from /dev/null: no command can consume the rest of the script.
set -eu
main() {
  size() { diskutil info disk0s2 | awk -F'[()]' '/Disk Size/ { split($2, a, " "); print a[1]; exit }'; }
  before=$(size)
  yes | sudo diskutil repairDisk disk0 >/dev/null
  sudo diskutil apfs resizeContainer disk0s2 0 >/dev/null
  printf 'esa-check-v1\n'
  printf 'container_bytes_before=%s\n' "$before"
  printf 'container_bytes=%s\n' "$(size)"
}
main "$@" </dev/null
