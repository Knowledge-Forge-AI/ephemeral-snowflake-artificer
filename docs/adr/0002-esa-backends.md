# ADR 0002: NixOS and Tahoe construction, recording and seed custody

## Status

Accepted (2026-10-22) for implementation. Implemented in source with unit
tests over fake process boundaries. **Not native-qualified and not accepted
by any consumer.** No image has been built, no Tart command has run, and no
Nix expression in `recipes/` has been evaluated by this unit (see
Consequences).

## Context

[ADR 0001](0001-esa-foundation.md) fixed the manifest contract and left
construction to the [backend work units](../architecture/backend-work-units.md).
Pinned upstream source evidence was gathered for Tart 2.40.1 (tag commit
`05edeac562bae804ea4f88592cfbc98cc6791b5d`), for Nixpkgs `make-disk-image` and
`vmTools` at `5e2305d577ca00acbba631b05cb1094d172b29f3`, and for the upstream
Tahoe vanilla image manifest. No consumer had accepted a v1 artifact, so v1
could still be amended in place.

## Decision

1. **Direct construction, small surface.** ESA drives Nix, Tart and OpenSSH directly through bounded, logged argv steps (`esa.proc`). There is no Packer, no sshpass, no plugin framework and no Python dependency; the CLI stays standard-library Python 3.9+. Every tool path, build store and credential file is a caller-chosen command argument; none is committed.
2. **nixos-minimal.**
   - Nixpkgs `make-disk-image` builds a raw EFI/GPT disk of exactly `resources.disk` (in MiB) with systemd-boot and no copied channel. The seed boots through the ESP's removable-media fallback loader with Tart's fresh `nvram.bin`; ESA registers no NVRAM boot entry and configures no network boot. A pre-registered entry would need a construction boot and would put shared firmware state into the seed. Whether the firmware still tries the network first is measured by ESA-QUAL-01, not asserted.
   - A second derivation seals a copy inside a throwaway build VM: it removes the machine-id, the dbus machine-id, SSH host keys and both random seeds, and optimises the image's own store. It then writes an `esa-check-v1` report with the disk's size and sha256. There is no garbage collection inside the image: `nixos-install` copies exactly the system closure, and the GC roots and the single generation are kept.
   - The build runs on an explicit `--build-store` (`ssh-ng://` or `ssh://`) that must provide the `kvm` system feature. ESA passes `--option builders ""`, creates no out-links or host GC roots, and changes no host Nix configuration or builder. The disk streams back through `nix store dump-path` and a stdlib NAR reader (sparse, hashed, exclusive). The host judges the report and binds it to the streamed disk.
   - The build-store SSH route is explicit and pinned. Nix 2.31.5 (`src/libstore/ssh.cc`) places a shell-split `NIX_SSHOPTS` before its own `-i KEY` and pinned `-oUserKnownHostsFile`, and otherwise leaves ssh to the user's passwd-based configuration, identities and agent. ESA therefore requires exactly one canonical `base64-ssh-public-host-key` and an explicit `ssh-key` file and/or `--ssh-agent-socket`. It refuses every other query parameter, duplicates, ports, `localhost` and loopback hosts. It sets `NIX_SSHOPTS` to static options that ignore ssh configuration files and global known_hosts, require strict checking, allow only public-key authentication without prompts, forward nothing, and name no default identity or unselected agent. It deliberately sets no `UserKnownHostsFile`, which would displace Nix's pin. The key file is checked with `lstat` and never read. ESA adds no SSH configuration language and no ambient-host support.
   - The LKL `cptofs` memory patch is builder-only. It applies only to the image builder's package set, and the build fails unless the expected upstream text occurs exactly once.
   - Per-instance access: sshd's only `AuthorizedKeysFile` is tmpfs `/run/esa/authorized-keys/%u`. A boot unit fills it from the instance's read-only Tart directory share `esa-inject`. A seed without a share has no SSH access: it fails closed.
3. **tahoe-vanilla.**
   - The base is pulled by digest only into a newly created, empty owned `TART_HOME`. Integrity does not rest on Tart. The caller supplies the exact OCI manifest bytes, whose sha256 must equal the pinned digest. Before any `tart set`, boot or bootstrap login, ESA requires `config.json` and `nvram.bin` to equal their OCI layer digests and checks `disk.img` chunk by chunk against the manifest's uncompressed-content digests. Tart 2.40.1 `clone` regenerates the MAC only on a collision with an existing local VM, which an empty home rules out, and otherwise copies the files byte for byte, so any difference is refused with no transformation fallback.
   - ESA does not claim that a public pull reads no registry credentials. Tart 2.40.1 consults the environment, the Docker configuration and the Keychain even for anonymous bearer-token pulls, and a private `HOME` does not prove otherwise. The operator qualifies the credential-access route of the `--tart` executable; ESA ships no wrapper and no dummy credential.
   - Guest `sw_vers` must match the profile before any installation. `esa tahoe observe-base` is the bootstrap-only route that reports `sw_vers` for a base that cannot yet be pinned, then deletes everything.
   - Construction uses only the inherited, publicly documented upstream bootstrap account (`inherited_default_account: true`). Rotating that console credential first is a separately documented human boundary and is not automated; a profile declaring `false` is refused. The credential is read only by `bin/esa-askpass` from a private file (mode 0600). It never appears in argv, environment values, logs, manifests or the seed. Host keys use trust on first use in a run-private known_hosts, deleted with the run. Tart's default shared NAT network is reachable from the host and other NAT peers, so this is not an independently authenticated Tart-to-SSH identity: the first responder at the reported address receives the bootstrap login.
   - The guest installs the pinned Nix release after checking its tarball sha256 (`inputs.nix_installer`), then activates the pinned nix-darwin system plus the consumer module.
   - The seal removes host keys and residue and keeps at most the declared generations. Garbage collection and optimisation run inside the guest store only. Because macOS regenerates missing host keys on any SSH connection, the seal then arms a per-run, guest-only finalizer: a one-shot root launchd job bootstrapped from a private `/private/var/tmp` directory, with no `LaunchDaemons` file and no enable or disable override. It survives the closing of the provisioning session, drops new port-22 connection attempts with a runtime-only `pf` rule, waits for the provisioning connection to end, stops remaining sshd and ssh-keygen processes, removes its own files, removes the host keys last and halts. Any failure exits without halting. A launchd job was chosen over a background child of the SSH session, because session teardown could end such a child before it halts. The host judges the seal report, including `finalizer=armed`, requires `tart run` to exit 0 within `--shutdown-seconds`, and checks the config lock is free. These are fail-closed gates. They are not proof that the stopped disk holds no host keys; that is observed natively.
   - ESA writes no TCC grants, changes no Apple account, never unlocks Passwords and claims no byte-reproducible disk. The attended Security, TCC and Passwords test remains consumer-owned.
4. **Units and builders.** `resources.disk` takes `GB` (10⁹) or `GiB` (2³⁰) and must be exact in the family's tool unit. `builders` is exactly `nix` and `tart`; the Linux build store is private evidence, not a builder entry, because the output is content-addressed by its derivation.
5. **Recording.** `esa record` derives every identity from the profile, the declared builder versions and the hashed, stopped VM directory. Validation entries come only from `esa-evidence-v1` files bound to the recorded `disk.img`. Recording is not boot, readiness, qualification or admission.
6. **Seed custody and instances.**
   - Every Tart call uses an ESA-created `TART_HOME`, `TART_NO_AUTO_PRUNE=1` and a private `HOME`. Destinations must be absent, and cleanup removes only trees carrying ESA's ownership marker. There is no prune, `--overwrite`, host `sudo`, host service or profile change, and no write into a pre-existing VM root.
   - Export and import each use a fresh owned home, so names are provably absent and no MAC collision can occur. Import is verified byte for byte against the artifact.
   - `esa instance create` clones the verified seed into a new owned home, applies only the declared host-side transformations (`--random-mac`, `--random-serial`), and checks that the config diff is bounded to those keys.
7. **Guest agent.** ESA provides generic plumbing (`esa.guestAgent.package`, a systemd service or launchd job) and ships no agent package or hash. A consumer whose readiness is `tart-guest-agent` supplies the exact package, source, checksum and licence entry. The agent is a runtime capability; ESA is not a broker.
8. **Supply chain and licences.**
   - The selected dependency classes are Nix, Nixpkgs, nix-darwin, Tart and consumer-required system packages.
   - Tart and the Tart guest agent are under the Functional Source License 1.1, with their own future-licence terms. ESA does not redistribute or relicense them; an artifact lists an agent only through the consumer's `--extra-licenses`.
   - ESA derives no code from the MIT-licensed Cirrus Labs templates (copyright 2018 Cirrus Labs). It only documents their inherited settings, so `NOTICE` gains no entry. Copying template code would require the full MIT notice.
   - Apple macOS stays under Apple's licence agreement, and ESA claims no Apple rights.
9. **Runtime and rollback.** A failed run leaves no seed, no artifact and no pass evidence; its run directory keeps logs and state for review. A run directory is single-use. Consumers keep their previous seed and receipt until a new one is accepted.

## Consequences

- Both families have honest, executable construction entry points; `esa plan` reports `implemented: true, native_qualified: false`.
- The following native facts are untested and are the remaining dependencies for ESA-QUAL-01. The parent runs that qualification separately:
  - a caller-selected aarch64-linux store with `kvm`;
  - native Nix 2.31.5 and OpenSSH honouring the build-store route: `-F /dev/null`, `IdentityFile=none`, no default identity or unselected agent, and strict checking against Nix's pin;
  - evaluation of the recipes against the pinned Nixpkgs and nix-darwin;
  - the exact LKL `cptofs` text;
  - Tart 2.40.1 runtime behaviour, including `ip --wait`. The `--dir name:path:ro` syntax and the default virtiofs tag `com.apple.virtio-fs.automount` are confirmed in Tart source, but have not been run;
  - VZ fallback-loader boot timing;
  - an anonymous ghcr.io pull, during which Tart consults its own credential providers unless the operator's qualified route prevents it;
  - a first clone into an empty home leaving `config.json` and `nvram.bin` byte-identical to their layers;
  - the Tahoe seal finalizer in a real guest: the sshd ancestry, `launchctl bootstrap`, the `pf` rule and its `pfctl -s rules` rendering, the process patterns, launchd or Background Task Management records left on disk, a zero `tart run` exit after the halt, a stopped disk without host keys, and SSH on fresh clones;
  - vanilla bootstrap SSH and passwordless sudo;
  - the Nix installer flags at the pinned version;
  - nix-darwin activation;
  - APFS container growth;
  - the effect of `--random-serial` on the consumer's attended Keychain and Passwords test.
- Source tests use fake `tart`, `nix` and `ssh` processes. They prove dispatch, refusal, cleanup and evidence binding, not image behaviour. The fake `nix` composes ssh arguments as Nix 2.31.5 does and evaluates them with a model of OpenSSH option precedence. Tests over it show consistency with that model, not native OpenSSH behaviour. Guest scripts are only parsed (`sh -n`) and their launchd job shape checked; none runs.

## What would make this wrong

- `nix store dump-path` buffers a whole NAR in memory instead of streaming, or the build store needs about twice the disk size (image plus sealed copy) and that is not available.
- Tart refuses a VM directory ESA materializes under `vms/`, rewrites `config.json` keys not modelled here, or regenerates identifiers on import or set beyond the documented cases.
- The VZ firmware cannot boot the ESP fallback loader with a fresh `nvram.bin`.
- The upstream base disables SSH password login or needs a password for `sudo`, or the operator's qualified `--tart` route cannot pull the public base anonymously while denied access to every registry credential store.
- Nix places `NIX_SSHOPTS` after its own `-i` or pinned known_hosts option, reads further authority-bearing store parameters that the real route needs, or OpenSSH adds default identities despite `IdentityFile=none`.
- Tart rewrites `config.json` or `nvram.bin` on a first clone into an empty home.
- macOS refuses to bootstrap a one-shot job from `/private/var/tmp`, persists a record of it, keeps a long-running `sshd` that `pkill` cannot settle, regenerates host keys during `shutdown -h now`, or `tart run` exits non-zero after a clean guest halt.
- A consumer needs a seed without the inherited bootstrap account, or a guest agent that ESA must ship.
