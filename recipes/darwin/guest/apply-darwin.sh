#!/bin/sh
# esa-guest-step: apply-darwin
# Usage: apply-darwin.sh WORKSPACE
# Build the pinned nix-darwin system from the uploaded workspace without
# touching its lock, set it as the system profile and activate it as root.
#
# This script arrives on the shell's stdin, so the body runs as one function
# with stdin from /dev/null: no command can consume the rest of the script.
set -eu
main() {
  ws=$1
  bin=/nix/var/nix/profiles/default/bin
  sys=$("$bin/nix" --extra-experimental-features 'nix-command flakes' build --no-link --print-out-paths \
    --no-update-lock-file --no-write-lock-file "path:$ws#darwinConfigurations.esa-seed.system")
  sudo -H "$bin/nix-env" -p /nix/var/nix/profiles/system --set "$sys"
  for f in /etc/bashrc /etc/zshrc /etc/nix/nix.conf; do
    if [ -f "$f" ] && [ ! -L "$f" ]; then
      sudo mv "$f" "$f.before-nix-darwin"
    fi
  done
  sudo -H "$sys/activate" >&2
  printf 'esa-check-v1\n'
  printf 'system=%s\n' "$sys"
  printf 'nix_version=%s\n' "$(/run/current-system/sw/bin/nix --version | sed 's/^nix (Nix) //')"
}
main "$@" </dev/null
