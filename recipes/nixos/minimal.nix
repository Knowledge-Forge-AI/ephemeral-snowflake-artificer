# ESA nixos-minimal: a minimal installed aarch64-linux system for Tart.
#
# Boots from its own EFI/GPT disk through systemd-boot, including the
# removable-media fallback loader, so no NVRAM boot entry and no network
# boot is needed. The seed carries no machine identity, host keys, accounts
# with password material or authorized keys; per-instance access arrives
# through ./injection.nix.
{ config, lib, pkgs, esaParams, ... }:
let
  lockedHash = h: h == null || h == "!" || h == "*" || lib.hasPrefix "!" h;
  userChecks = name: u: [
    {
      assertion = u.password == null && u.initialPassword == null && u.hashedPasswordFile == null
        && lockedHash u.hashedPassword && lockedHash u.initialHashedPassword;
      message = "esa: user ${name} declares password material; seeds carry none";
    }
    {
      assertion = u.openssh.authorizedKeys.keys == [ ] && u.openssh.authorizedKeys.keyFiles == [ ];
      message = "esa: user ${name} declares authorized keys; inject them per instance instead";
    }
  ];
in
{
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;
  boot.loader.timeout = lib.mkDefault 1;
  boot.initrd.availableKernelModules = [ "virtio_pci" "virtio_mmio" "virtio_blk" "virtio_net" "virtiofs" ];

  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/ESP";
    fsType = "vfat";
    options = [ "umask=0077" ];
  };

  networking.useDHCP = lib.mkDefault true;

  nix.channel.enable = false;
  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  documentation.enable = lib.mkDefault false;
  documentation.nixos.enable = lib.mkDefault false;

  users.mutableUsers = false;
  users.users.root.hashedPassword = "!";
  # The seed is key-free by design, so turn off only upstream's evaluation
  # guard that root or a wheel user has a permanent password or key. This
  # grants no login; SSH keys arrive per instance through ./injection.nix.
  users.allowNoPasswordLogin = true;

  services.openssh = {
    enable = lib.mkDefault true;
    authorizedKeysInHomedir = false;
    authorizedKeysFiles = lib.mkForce [ "/run/esa/authorized-keys/%u" ];
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  system.stateVersion = esaParams.nixos_release;

  assertions = lib.flatten (lib.mapAttrsToList userChecks config.users.users);
}
