# Generic guest-agent capability plumbing.
#
# ESA ships no agent and guesses no package or hash. A consumer whose
# readiness method is tart-guest-agent supplies `esa.guestAgent.package`
# (with its exact immutable source, checksum and licence) in its own module;
# ESA only runs it as a service. The agent is a runtime capability; ESA is
# not a broker and does not depend on it.
{ config, lib, pkgs, esaParams, ... }:
let
  cfg = config.esa.guestAgent;
in
{
  options.esa.guestAgent = {
    package = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = "Consumer-supplied guest agent package. ESA provides none.";
    };
    args = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Arguments passed to the package's main program.";
    };
  };

  config = lib.mkMerge [
    {
      assertions = [{
        assertion = esaParams.readiness_method != "tart-guest-agent" || cfg.package != null;
        message = "esa: readiness method tart-guest-agent needs esa.guestAgent.package from the consumer module";
      }];
    }
    (lib.mkIf (cfg.package != null) {
      systemd.services.esa-guest-agent = {
        description = "Consumer-supplied guest agent";
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          ExecStart = lib.escapeShellArgs ([ (lib.getExe cfg.package) ] ++ cfg.args);
          Restart = "always";
        };
      };
    })
  ];
}
