#!/bin/sh
# esa-guest-step: probe
# Report the guest OS identity and bootstrap privileges. Changes nothing.
set -eu
printf 'esa-check-v1\n'
printf 'product_version=%s\n' "$(sw_vers -productVersion)"
printf 'build=%s\n' "$(sw_vers -buildVersion)"
printf 'user=%s\n' "$(id -un)"
printf 'arch=%s\n' "$(uname -m)"
if sudo -n true 2>/dev/null; then
  printf 'sudo_nopasswd=yes\n'
else
  printf 'sudo_nopasswd=no\n'
fi
