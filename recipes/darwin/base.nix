# ESA tahoe-vanilla nix-darwin layer, applied inside the upstream vanilla
# guest. nix-darwin configures the installed Apple OS; it does not replace
# it. The Aqua session, the inherited (declared) bootstrap account, and the
# Passwords and automation behaviour of the base are left alone. ESA adds no
# TCC grants, no Apple account state and no authorized keys.
{ config, lib, pkgs, esaParams, ... }:
let
  keyChecks = name: u: {
    assertion = (u.openssh.authorizedKeys.keys or [ ]) == [ ]
      && (u.openssh.authorizedKeys.keyFiles or [ ]) == [ ];
    message = "esa: user ${name} declares authorized keys; inject them per instance instead";
  };
in
{
  options.esa.python.enable = lib.mkOption {
    type = lib.types.bool;
    default = false;
    description = "Provide python3 from the pinned Nixpkgs instead of installing Apple's Command Line Tools.";
  };

  config = {
    nixpkgs.hostPlatform = "aarch64-darwin";
    system.stateVersion = lib.mkDefault 6;
    system.primaryUser = lib.mkDefault esaParams.bootstrap_user;

    nix.channel.enable = false;
    nix.settings.experimental-features = [ "nix-command" "flakes" ];

    environment.systemPackages = lib.mkIf config.esa.python.enable [ pkgs.python3 ];

    assertions = lib.mapAttrsToList keyChecks config.users.users;
  };
}
