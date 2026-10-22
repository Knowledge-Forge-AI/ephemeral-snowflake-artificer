#!/bin/sh
# esa-guest-step: install-nix
# Usage: install-nix.sh VERSION SHA256HEX
# Fetch the pinned Nix release tarball, refuse it unless its sha256 matches
# the profile pin, and run its multi-user installer non-interactively.
#
# This script arrives on the shell's stdin, so the body runs as one function
# with stdin from /dev/null: no command can consume the rest of the script.
set -eu
main() {
  version=$1
  want=$2
  work=$(mktemp -d /private/tmp/esa-nix.XXXXXX)
  url="https://releases.nixos.org/nix/nix-$version/nix-$version-aarch64-darwin.tar.xz"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 -o "$work/nix.tar.xz" "$url"
  got=$(shasum -a 256 "$work/nix.tar.xz" | cut -d' ' -f1)
  if [ "$got" != "$want" ]; then
    echo "esa: nix installer sha256 does not match the profile pin" >&2
    rm -rf "$work"
    exit 3
  fi
  tar -xJf "$work/nix.tar.xz" -C "$work"
  "$work/nix-$version-aarch64-darwin/install" --daemon --yes --no-channel-add --no-modify-profile >&2
  rm -rf "$work"
  printf 'esa-check-v1\n'
  printf 'installer_sha256=sha256:%s\n' "$got"
  printf 'nix_version=%s\n' "$(/nix/var/nix/profiles/default/bin/nix --version | sed 's/^nix (Nix) //')"
}
main "$@" </dev/null
