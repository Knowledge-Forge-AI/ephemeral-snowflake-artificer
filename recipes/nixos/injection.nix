# ESA per-instance access injection for a key-free seed.
#
# Before sshd starts, read the instance's Tart directory share (read-only,
# `tart run --dir esa-inject:DIR:ro`) and copy
# esa-inject/authorized_keys/<user> into tmpfs /run/esa/authorized-keys/,
# which is sshd's only AuthorizedKeysFile (see ./minimal.nix). Nothing is
# written to the disk. Without a share there are no keys, so SSH stays
# closed: failure is closed, never open.
{ config, lib, pkgs, ... }:
{
  systemd.services.esa-inject = {
    description = "ESA per-instance authorized-keys injection";
    wantedBy = [ "multi-user.target" ];
    before = [ "sshd.service" "sshd.socket" ];
    after = [ "local-fs.target" ];
    path = [ pkgs.coreutils pkgs.util-linux ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      install -d -m 0755 -o root -g root /run/esa/authorized-keys
      mnt=$(mktemp -d /run/esa-inject.XXXXXX)
      if mount -t virtiofs -o ro com.apple.virtio-fs.automount "$mnt" 2>/dev/null; then
        src="$mnt/esa-inject/authorized_keys"
        if [ -d "$src" ]; then
          for f in "$src"/*; do
            [ -f "$f" ] && [ ! -L "$f" ] || continue
            u=$(basename "$f")
            case "$u" in
              root|*[!a-z0-9_-]*|[!a-z_]*) continue ;;
            esac
            if id -u "$u" >/dev/null 2>&1; then
              install -m 0644 -o root -g root "$f" "/run/esa/authorized-keys/$u"
            fi
          done
        fi
        umount "$mnt"
      fi
      rmdir "$mnt"
    '';
  };
}
