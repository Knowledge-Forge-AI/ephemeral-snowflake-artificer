# ESA nixos-minimal raw disk image and seed seal, built on a caller-selected
# aarch64-linux store with the `kvm` system feature.
#
# esa-raw-image: Nixpkgs make-disk-image, EFI/GPT, raw, no copied channel,
#   systemd-boot installed, diskSize exactly params.disk_mib MiB.
# esa-seed-image: a copy of that disk, sealed inside a throwaway build VM:
#   machine-id, dbus machine-id, SSH host keys and both random seeds are
#   removed, the image's own store is optimised, and an esa-check-v1 report
#   is written next to disk.img with the disk's size and sha256. ESA judges
#   that report on the host; the derivation does not decide pass or fail.
#
# The LKL patch is builder-only: it raises cptofs's library-kernel memory so
# a realistic closure can be copied, and fails the build unless the upstream
# text occurs exactly once. It never enters the seed's closure.
{ pkgs, lib, nixos, params }:
let
  lklMem = toString params.lkl_cptofs_mem_mib;
  imagePkgs = pkgs.extend (final: prev: {
    lkl = prev.lkl.overrideAttrs (old: {
      postPatch = (old.postPatch or "") + ''
        n=$(grep -o 'mem=100M' tools/lkl/cptofs.c | wc -l | tr -d ' ')
        if [ "$n" != 1 ]; then
          echo "esa: expected exactly one 'mem=100M' in tools/lkl/cptofs.c, found $n" >&2
          exit 1
        fi
        substituteInPlace tools/lkl/cptofs.c --replace-fail 'mem=100M' 'mem=${lklMem}M'
      '';
    });
  });

  # Runner-named system units the image's configuration enables, counted at
  # evaluation from the same config the image is built from. The unit files
  # in the image are absolute /nix/store symlinks that do not resolve inside
  # the seal VM, so walking them there would not observe them.
  runnerUnits = lib.length (lib.filter (n: lib.hasInfix "runner" (lib.toLower n))
    (lib.attrNames (lib.filterAttrs (_: u: u.enable) nixos.config.systemd.units)));

  raw = import "${pkgs.path}/nixos/lib/make-disk-image.nix" {
    pkgs = imagePkgs;
    inherit lib;
    inherit (nixos) config;
    name = "esa-raw-image";
    baseName = "disk";
    format = "raw";
    partitionTableType = "efi";
    diskSize = params.disk_mib;
    copyChannel = false;
    installBootLoader = true;
    touchEFIVars = false;
    memSize = params.image_mem_mib;
  };

  seal = imagePkgs.vmTools.runInLinuxVM (imagePkgs.runCommand "esa-seed-image" {
    buildInputs = with imagePkgs; [ coreutils findutils gawk util-linux e2fsprogs dosfstools nix ];
    memSize = params.image_mem_mib;
    preVM = ''
      cp --sparse=always ${raw}/disk.img disk.img
      chmod u+w disk.img
      diskImage=$PWD/disk.img
    '';
    postVM = ''
      mkdir -p $out
      mv disk.img $out/disk.img
      {
        cat xchg/esa-seed-check.txt
        echo "disk_bytes=$(stat -c %s $out/disk.img)"
        echo "disk_sha256=sha256:$(sha256sum $out/disk.img | cut -d' ' -f1)"
      } > $out/esa-seed-check.txt
    '';
  } ''
    set -eu
    export HOME=/tmp
    mkdir -p /mnt
    mount /dev/vda2 /mnt
    mkdir -p /mnt/boot
    mount /dev/vda1 /mnt/boot

    rm -f /mnt/etc/machine-id /mnt/var/lib/dbus/machine-id
    rm -f /mnt/etc/ssh/ssh_host_*
    rm -f /mnt/var/lib/systemd/random-seed /mnt/boot/loader/random-seed
    nix-store --store 'local?root=/mnt' --optimise

    present() { if [ -e "$1" ] || [ -L "$1" ]; then echo present; else echo absent; fi; }
    count() { wc -l | tr -d ' '; }
    {
      echo esa-check-v1
      echo "machine_id=$(present /mnt/etc/machine-id)"
      echo "dbus_machine_id=$(present /mnt/var/lib/dbus/machine-id)"
      echo "ssh_host_keys=$(find /mnt/etc/ssh -maxdepth 1 -name 'ssh_host_*' 2>/dev/null | count)"
      echo "random_seed=$(present /mnt/var/lib/systemd/random-seed)"
      echo "esp_random_seed=$(present /mnt/boot/loader/random-seed)"
      echo "system_generations=$(find /mnt/nix/var/nix/profiles -maxdepth 1 -name 'system-*-link' | count)"
      echo "system_profile=$(present /mnt/nix/var/nix/profiles/system)"
      echo "fallback_loader=$(present /mnt/boot/EFI/BOOT/BOOTAA64.EFI)"
      echo "runner_files=$(find /mnt/home /mnt/root /mnt/var/lib /mnt/etc \( -name .runner -o -name .credentials -o -name .credentials_rsaparams \) 2>/dev/null | count)"
      echo "authorized_keys_files=$( { find /mnt/home /mnt/root -name 'authorized_keys*' 2>/dev/null; find /mnt/etc/ssh/authorized_keys.d -type f 2>/dev/null; } | count)"
      echo "enabled_runner_units=${toString runnerUnits}"
      if [ -f /mnt/etc/shadow ]; then
        echo "password_hashes=$(awk -F: '$2 != "" && $2 !~ /^[!*]/ { n++ } END { print n + 0 }' /mnt/etc/shadow)"
      else
        echo "password_hashes=missing"
      fi
    } > /tmp/xchg/esa-seed-check.txt
    umount /mnt/boot
    umount /mnt
  '');
in
{
  esa-raw-image = raw;
  esa-seed-image = seal;
}
