# Generic guest-agent capability plumbing for macOS (launchd).
#
# ESA ships no agent and guesses no package or hash. A consumer whose
# readiness method is tart-guest-agent supplies `esa.guestAgent.package`
# with its exact source, checksum and licence; ESA only declares the job.
{ config, lib, pkgs, esaParams, ... }:
let
  cfg = config.esa.guestAgent;
  job = {
    serviceConfig = {
      ProgramArguments = [ (lib.getExe cfg.package) ] ++ cfg.args;
      RunAtLoad = true;
      KeepAlive = true;
    };
  };
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
    scope = lib.mkOption {
      type = lib.types.enum [ "daemon" "agent" ];
      default = "daemon";
      description = "Run as a system launchd daemon or as a per-user agent in the Aqua session.";
    };
  };

  config = lib.mkMerge [
    {
      assertions = [{
        assertion = esaParams.readiness_method != "tart-guest-agent" || cfg.package != null;
        message = "esa: readiness method tart-guest-agent needs esa.guestAgent.package from the consumer module";
      }];
    }
    (lib.mkIf (cfg.package != null && cfg.scope == "daemon") { launchd.daemons.esa-guest-agent = job; })
    (lib.mkIf (cfg.package != null && cfg.scope == "agent") { launchd.user.agents.esa-guest-agent = job; })
  ];
}
