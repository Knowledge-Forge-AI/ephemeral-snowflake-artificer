#!/bin/sh
# esa-guest-step: seal
# Usage: seal.sh BOOTSTRAP_USER GENERATIONS RUN_ROOT
# Remove build residue and host keys, keep GENERATIONS system generations,
# collect garbage and optimise inside the guest store only, arm a one-shot
# finalizer, then report what is left.
#
# The finalizer is a root launchd job bootstrapped from a private run
# directory under /private/var/tmp, not a child of this SSH session, so
# closing the session cannot end it. It is loaded for this boot only: no
# LaunchDaemons file, no enable/disable override, no change to /etc/pf.conf
# or to the sshd job. It blocks new SSH connection attempts with a runtime pf
# rule, waits for this connection to close, stops any remaining sshd or
# ssh-keygen process, removes its own files, removes the host keys last and
# halts. Any failure exits without halting, so the host times out and
# refuses. The report describes the guest at report time; whether the
# stopped disk holds host keys is observed outside this script.
#
# This script arrives on the shell's stdin, so the body runs as one function
# with stdin from /dev/null: no command can consume the rest of the script.
set -eu

# The PID of this connection's per-connection sshd: the first ancestor whose
# parent is launchd (PID 1).
connection_pid() {
  pid=$$
  i=0
  while [ "$i" -lt 16 ]; do
    ppid=$(ps -o ppid= -p "$pid" | tr -d ' ')
    case $ppid in '' | *[!0-9]*) return 1 ;; esac
    if [ "$ppid" -eq 1 ]; then
      case $(ps -o comm= -p "$pid") in *sshd*) printf '%s\n' "$pid"; return 0 ;; esac
      return 1
    fi
    pid=$ppid
    i=$((i + 1))
  done
  return 1
}

arm_finalizer() {
  conn=$(connection_pid) || { printf 'esa seal: no sshd connection process found\n' >&2; return 1; }
  dir=$(sudo mktemp -d /private/var/tmp/esa-seal-final.XXXXXXXX)
  case $dir in
    /private/var/tmp/esa-seal-final.*[!A-Za-z0-9/._-]* | *..*) return 1 ;;
    /private/var/tmp/esa-seal-final.?*) ;;
    *) return 1 ;;
  esac
  sudo tee "$dir/finalize.sh" >/dev/null <<'FINALIZE'
#!/bin/sh
# ESA seal finalizer, run once by launchd as root. Arguments: the PID of the
# provisioning connection's sshd and this run directory. Every failure exits
# without halting.
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
main() {
  conn=$1
  dir=$2
  [ "$(id -u)" -eq 0 ] || exit 1
  case $conn in '' | *[!0-9]*) exit 1 ;; esac
  case $dir in /private/var/tmp/esa-seal-final.?*) ;; *) exit 1 ;; esac
  # This boot only: drop new connection attempts (SYN without ACK) to port
  # 22. The open provisioning session carries no SYN and is unaffected.
  printf 'block drop in quick proto tcp from any to any port 22 flags S/SA\n' | pfctl -q -f - || exit 1
  pfctl -q -E 2>/dev/null || exit 1
  # pfctl prints a known port by its service name unless asked for numbers.
  pfctl -s rules 2>/dev/null | grep -Eq 'port = (22|ssh)( |$)' || exit 1
  waited=0
  while kill -0 "$conn" 2>/dev/null; do
    [ "$waited" -lt 120 ] || exit 1
    sleep 1
    waited=$((waited + 1))
  done
  # Stop any sshd, sshd-session, sshd-auth, sshd-keygen-wrapper (comm is cut
  # to 16 characters) or ssh-keygen process. pgrep and pkill exit 1 when
  # nothing matches, which is the expected case.
  tries=0
  while :; do
    pgrep '^ssh(d|-keygen)' >/dev/null
    case $? in 1) break ;; 0) ;; *) exit 1 ;; esac
    [ "$tries" -lt 5 ] || exit 1
    pkill -9 '^ssh(d|-keygen)'
    [ "$?" -le 1 ] || exit 1
    tries=$((tries + 1))
    sleep 1
  done
  rm -rf "$dir" || exit 1
  [ ! -e "$dir" ] || exit 1
  rm -f /etc/ssh/ssh_host_* || exit 1
  for key in /etc/ssh/ssh_host_*; do
    [ ! -e "$key" ] || exit 1
  done
  sync
  exec /sbin/shutdown -h now
}
main "$@"
FINALIZE
  sudo tee "$dir/job.plist" >/dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>esa.seal-finalizer</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$dir/finalize.sh</string>
    <string>$conn</string>
    <string>$dir</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>LaunchOnlyOnce</key>
  <true/>
</dict>
</plist>
PLIST
  sudo chmod 0600 "$dir/finalize.sh"
  sudo chmod 0644 "$dir/job.plist"
  sudo /bin/sh -n "$dir/finalize.sh"
  sudo plutil -lint "$dir/job.plist" >/dev/null
  sudo launchctl bootstrap system "$dir/job.plist"
  sudo launchctl print system/esa.seal-finalizer >/dev/null
}

main() {
  user=$1
  keep=$2
  root=$3
  bin=/nix/var/nix/profiles/default/bin
  rm -rf "$root"
  sudo -H "$bin/nix-env" -p /nix/var/nix/profiles/system --delete-generations "+$keep" >&2
  sudo -H "$bin/nix-store" --gc >&2
  sudo -H "$bin/nix-store" --optimise >&2
  sudo rm -f /etc/ssh/ssh_host_*
  home=$(dscl . -read "/Users/$user" NFSHomeDirectory | awk '{ print $2 }')
  rm -f "$home/.ssh/known_hosts" "$home/.zsh_history" "$home/.bash_history"
  rm -rf "$home/.zsh_sessions" "$home/.cache/nix"
  sudo rm -rf /var/root/.cache/nix
  arm_finalizer
  count() { wc -l | tr -d ' '; }
  printf 'esa-check-v1\n'
  printf 'ssh_host_keys=%s\n' "$(find /etc/ssh -maxdepth 1 -name 'ssh_host_*' | count)"
  printf 'system_generations=%s\n' "$(sudo -H "$bin/nix-env" -p /nix/var/nix/profiles/system --list-generations | count)"
  printf 'authorized_keys_files=%s\n' "$(sudo find /Users /var/root -maxdepth 3 -name 'authorized_keys*' 2>/dev/null | count)"
  printf 'runner_files=%s\n' "$(sudo find /Users /var/root -maxdepth 4 \( -name .runner -o -name .credentials -o -name .credentials_rsaparams \) 2>/dev/null | count)"
  printf 'runner_launchd_jobs=%s\n' "$(ls /Library/LaunchDaemons /Library/LaunchAgents "$home/Library/LaunchAgents" 2>/dev/null | grep -Ei 'runner' | count)"
  printf 'autologin_user=%s\n' "$(sudo defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || echo none)"
  accounts="$home/Library/Preferences/MobileMeAccounts.plist"
  if [ -f "$accounts" ] && /usr/libexec/PlistBuddy -c 'Print :Accounts:0' "$accounts" >/dev/null 2>&1; then
    printf 'apple_account=present\n'
  else
    printf 'apple_account=absent\n'
  fi
  if [ -e "$root" ]; then printf 'run_root=present\n'; else printf 'run_root=absent\n'; fi
  printf 'finalizer=armed\n'
}
main "$@" </dev/null
