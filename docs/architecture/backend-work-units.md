# Backend work units

Status: **implemented in source; not native-qualified; not consumer-accepted.**
Every unit below except ESA-QUAL-01 has an executable command, tested over
fake process boundaries. No image has been built and no Tart command has run
under this revision. `esa plan` marks each step `implemented: true` and
`native_qualified: false`. Decisions are recorded in
[ADR 0002](../adr/0002-esa-backends.md).

Each unit lists its inputs, outputs, refusal conditions and the evidence it
retains. A unit that discovers v1 needs a change amends
[manifest v1](../specs/manifest-v1.md), its validator and its tests together,
before any consumer accepts a v1 artifact.

## Shared interface

**Input:**

- a profile that passes `esa validate`;
- the files named by `esa plan` (`consumer_supplied_files`): the `flake.lock`, the customization module and, for tahoe-vanilla, the base's OCI manifest. Each must match its sha256;
- the ESA commit (`--esa-revision`). The caller asserts it and runs ESA from a clean checkout of that commit.

**Output:** a stopped, read-only Tart VM directory (`config.json`, `disk.img`,
`nvram.bin`) plus an artifact manifest that passes `esa validate` and `esa
verify --profile --payload-dir`.

**Refusals:** a backend refuses, and never substitutes or floats:

- a missing or mismatched consumer file;
- a lock whose pinned nodes differ from `inputs`;
- a module with path literals or `<...>` lookups;
- a non-exact disk capacity;
- an unpinned tool version;
- an existing destination;
- a failed seed check.

**Run directories:** `prepare` creates an absent run directory (mode 0700,
ownership marker) holding the isolated workspace and `state.json`. A run is
single-use: `construct` advances it from `prepared` to `constructing` and
then to `completed` or `failed`, and never replays.

**Evidence:**

- per-step logs (mode 0600, argv and environment key names, never environment values);
- tool versions;
- `esa-check-v1` reports and the host's `esa-evidence-v1` judgments, each bound to the recorded `disk.img`.

All of it stays in the run directory, outside the repository.
`validation[].evidence_sha256` references the evidence files.

## ESA-NIX-01: NixOS minimal system and raw disk

- **Commands:** `esa nixos prepare PROFILE --flake-lock F --module M --esa-revision REV --run-dir R`, then `esa nixos construct --run-dir R --nix PATH --build-store 'ssh-ng://USER@HOST?base64-ssh-public-host-key=PIN&ssh-key=KEY' [--ssh-agent-socket SOCK] --tart PATH --output SEED --artifact-output A`.
- **Recipe:**
  - `recipes/nixos/minimal.nix`: systemd-boot without EFI-variable writes, ext4 root `nixos`, vfat ESP, DHCP, no channel, immutable users, locked root, key-only sshd. Evaluation asserts that no account carries password material or authorized keys. Because the seed is deliberately key-free, it sets `users.allowNoPasswordLogin = true`, which only disables upstream's evaluation-time check that root or a wheel user has a permanent password or key; it grants no password or empty-password login, and access still depends on per-instance key injection;
  - `recipes/nixos/image.nix`: `make-disk-image`, `format = "raw"`, `partitionTableType = "efi"`, `copyChannel = false`, `diskSize` = `resources.disk` in MiB.
- **LKL:** the builder-only `cptofs` memory patch fails the build unless `mem=100M` occurs exactly once in `tools/lkl/cptofs.c`.
- **Build environment:** a caller-selected aarch64-linux store with the `kvm` system feature. Evaluating on a Mac is not a build. ESA passes `--option builders ""` and never starts or reconfigures a host builder.
- **Build-store SSH route:** Nix 2.31.5 runs the first `ssh` on `PATH` (the `--nix` directory, then `/usr/bin`) as `ssh HOST -x $NIX_SSHOPTS [-i KEY] -oUserKnownHostsFile=PIN_FILE …`. Without further options, ssh would use the invoking user's passwd-based configuration, default identities and agent, which a private `HOME` does not suppress. Before marking the run `constructing` or running any tool, ESA:
  - accepts only `ssh-ng://` or `ssh://` with a DNS or IPv4 host, and refuses a port (Nix pins the bare host while ssh looks up `[host]:port`), `localhost` (Nix runs it locally) and loopback addresses;
  - requires exactly one `base64-ssh-public-host-key`: canonical base64 of one `TYPE BASE64 [COMMENT]` public key line, with `+` written `%2B`. It accepts at most one `ssh-key` and refuses every other query parameter and any duplicate;
  - requires an explicit authentication route: the `ssh-key` file, `--ssh-agent-socket`, or both. The key file must be a private regular file with one link, owned by the caller, in a directory the caller or root owns and no one else can write, with no symlink in its path. ESA checks it with `lstat` and never opens it. The socket must be the caller's own and either private or in a private directory;
  - sets `NIX_SSHOPTS` itself to static options: `-F /dev/null`, batch mode, strict checking, no global known_hosts, no host-key updates, `IdentityFile=none` (so no default key files), publickey only, no passwords or keyboard-interactive, no forwarding, and either `IdentityAgent=none` or, only with a selected socket, `IdentityAgent=SSH_AUTH_SOCK` with `SSH_AUTH_SOCK` set to it. ESA sets no `UserKnownHostsFile`, because OpenSSH keeps the first value and that would displace Nix's pin. No caller or ambient `NIX_SSHOPTS` or `SSH_AUTH_SOCK` reaches Nix.

  A fake `nix` checks the composed ssh argv and a model of OpenSSH's option handling. How native Nix and OpenSSH behave together is not qualified.

## ESA-NIX-02: Tart VM directory and Linux seed preparation

- **Seal (inside the build):** a second derivation copies the disk and, in a throwaway build VM:
  - removes `/etc/machine-id`, `/var/lib/dbus/machine-id`, the SSH host keys, `/var/lib/systemd/random-seed` and the ESP `loader/random-seed`;
  - optimises the image's own store;
  - reports generations, the fallback loader `EFI/BOOT/BOOTAA64.EFI`, runner files, authorized keys, runner-named enabled units (counted at evaluation from the image's own `systemd.units`; a name guard, not a proof), password hashes, and the disk's bytes and sha256.

  The host judges every value. There is no garbage collection in the image, because `nixos-install` copies exactly the system closure.
- **Assembly:** run in an owned `TART_HOME`:
  1. `tart create --linux`;
  2. `tart set --cpu --memory --disk` (memory in MiB);
  3. the strict `tart-vm-dir-v1` and resource checks;
  4. a disk hash that must equal the streamed disk.
- **Boot path:** the ESP removable-media fallback loader with Tart's fresh `nvram.bin`; no NVRAM entry is registered and no network boot is configured.
- **Per-instance access:** `recipes/nixos/injection.nix` fills `/run/esa/authorized-keys/<user>` from the instance's read-only `esa-inject` directory share. Without a share there is no SSH access.
- **Guest agent:** `recipes/nixos/guest-agent.nix` runs a consumer-supplied `esa.guestAgent.package`. ESA ships none.

## ESA-MAC-01: Tahoe vanilla seed

- **Commands:**
  - `esa tahoe observe-base --digest D --oci-manifest M …`, the bootstrap-only observation route;
  - `esa tahoe prepare PROFILE …`;
  - `esa tahoe construct --run-dir R --oci-manifest M --tart PATH --ssh PATH --bootstrap-credential-file F --output SEED --artifact-output A`.
- **Base:** `tart clone` of `ghcr.io/cirruslabs/macos-tahoe-vanilla@DIGEST` into a newly created, empty owned `TART_HOME`. Before any `tart set`, boot or bootstrap login, `config.json` and `nvram.bin` must equal their OCI layer digests exactly, and `disk.img` must match the manifest's per-chunk uncompressed digests and total size. Exact matching is valid here: Tart 2.40.1 regenerates the MAC only when another local VM already uses it, which cannot happen in an empty home, and otherwise copies the files byte for byte. Any transformation is refused; there is no fallback.
- **Registry credentials:** a private `HOME` and `TART_HOME` do not prove that `tart clone` reads no credentials. Tart 2.40.1's registry client looks in the environment, the Docker configuration and then the Keychain, even for a public pull. ESA claims no credential-free pull, ships no wrapper and supplies no dummy credential. The operator must qualify the credential-access route of the `--tart` executable they pass.
- **Bootstrap host key:** trust on first use. `tart run` uses Tart's default shared NAT network, which the host and other NAT peers can reach. ESA accepts the first host key at the address `tart ip` reports and pins it only for the rest of the run. This is not an independently authenticated Tart-to-SSH identity.
- **Guest identity:** guest `sw_vers` must equal `macos.product_version` and `macos.build`. `macos.recipe` is inspected source, not proof of how the base was produced.
- **Inherited settings** (from the inspected vanilla recipe; documented and left unchanged, but not verified for the pinned base, which that recipe revision may not have produced):
  - an administrator account `admin` with a publicly known login;
  - autologin into the Aqua session;
  - Remote Login (SSH) and Screen Sharing enabled;
  - passwordless `sudo`;
  - Gatekeeper disabled, Safari remote automation enabled, sleep and screen lock off;
  - no Apple account.
- **Bootstrap credential policy:**
  - Only `inherited_default_account: true` is constructible.
  - The caller passes the bootstrap login as a private file, read only by `bin/esa-askpass`.
  - Rotating that console credential before construction is a separately documented human boundary and is not automated.
- **macOS identifier set:** `macAddress`, `ecid` (the platform machine identifier behind the hardware UUID and serial) and the SSH host keys. The seed keeps the base's `macAddress` and `ecid`; instances get fresh values only through `esa instance create`.
- **Must not:**
  - broaden host permissions;
  - add TCC grants, change Apple accounts or unlock Passwords;
  - remove the GUI session, applications or automation behaviour to shrink the image.

## ESA-MAC-02: Nix and nix-darwin layer

- **Install:** the guest fetches the release tarball derived from `inputs.nix_installer.version` and refuses it unless its sha256 equals `inputs.nix_installer.sha256`. It then runs the multi-user installer non-interactively with no channel.
- **Activate:**
  1. build `darwinConfigurations.esa-seed.system` from the uploaded workspace (`recipes/darwin/base.nix`, `guest-agent.nix` and the consumer module) without touching its lock;
  2. set it as the system profile;
  3. move the installer-era `/etc` shell and Nix files aside;
  4. activate as root.

  `esa.python.enable` provides Python from Nixpkgs instead of Apple's Command Line Tools.
- **Disk growth:** when `resources.disk` exceeds the base disk, the guest grows the APFS container and reports its size before and after. The host requires the container to absorb the disk growth to within 1 GiB, so a blocked or no-op resize fails.
- **Seal:** keep at most `nix_generations_retained` generations, then GC and optimise the guest store, and remove host keys, history and the run's guest files. macOS `sshd-keygen-wrapper` regenerates missing host keys on any later SSH connection, so the script then arms a finalizer and reports `finalizer=armed`:
  1. it writes a script and a one-shot plist (`RunAtLoad`, `LaunchOnlyOnce`, no `KeepAlive`) to a fresh root-only `/private/var/tmp/esa-seal-final.*` directory, and loads them with `launchctl bootstrap system`. Nothing goes in `LaunchDaemons`, and no enable or disable override is written;
  2. the job is not a child of the provisioning SSH session, so closing the session does not end it. It loads, as this boot's main `pf` ruleset (replacing the default anchors until the halt), one rule that drops new connection attempts to port 22, and accepts the rule only if `pfctl -s rules` shows it as port `22` or `ssh`. `/etc/pf.conf` and the sshd job are not changed;
  3. it waits up to 120 seconds for the provisioning connection's sshd process to exit, then stops any remaining `sshd*` or `ssh-keygen` process;
  4. it removes its own directory, then the host keys, checks that none remain, and runs `shutdown -h now`.

  Any failure in the script or the job exits without halting. The host refuses unless the report has `finalizer=armed` and `tart run` exits 0 within `--shutdown-seconds`; on refusal it kills `tart run` and removes the owned home. Both are fail-closed gates, not proof of the final state: report-time values and a clean VM stop do not show that the stopped disk holds no host keys. Clones boot with SSH enabled as before, because the `pf` rule is not persistent. Fake IO checks only the host's gates and the scripts' syntax and job shape; nothing in the guest has run.
- **Control socket residue:** every `tart run` binds a Unix socket `control.sock` in the VM directory. Tart 2.40.1 removes an old one only when the next run starts, and leaves the pathname behind at exit. After this run's own `tart run` exits 0, and before the strict scan and artifact recording, ESA unlinks that one name. All of these must hold:
  - the Tart home carries this run's ownership marker;
  - the VM directory is a real directory owned by this user;
  - Tart's `config.json` lock is free and there is no `state.vzvmsave`;
  - the entry is a Unix socket owned by this user with one link, and is the same inode before and after the lock check.

  Otherwise ESA refuses and removes nothing. A missing socket is a no-op. There is no removal after a non-zero exit or an unobserved halt, and none on a seed. The scan, `esa record` and payload verification still refuse `control.sock` and every other extra entry. Fake IO shows the order and the refusals; native Tart has not run this step.
- **Reproducibility claim:** pinned inputs, declared configuration and observed validation. ESA does not claim identical disk bytes.

## ESA-REC-01: `esa record`

- **Command:** `esa record PROFILE --vm-dir DIR --esa-revision REV --builder nix=V --builder tart=V [--evidence FILE]… [--extra-licenses FILE] --output A`.
- **Records:**
  - the builder versions;
  - the copied profile fields;
  - the payload files, `storage` and derived identities;
  - one `host_allocated_bytes` observation;
  - family `upstream_licenses` plus any extra rows.
- **Must:** produce manifests that pass `esa validate` unchanged. It takes `validation` entries only from evidence bound to this disk, and never invents boot, readiness or qualification results.

## Seed custody and instance lifecycle (shared by both backends)

- **`esa seed export` / `esa seed import`:** move a verified, stopped seed through a `.tvm` archive.
  - Each command uses a fresh owned `TART_HOME`.
  - Export refuses an existing archive path, because `tart export` would truncate it.
  - Import proves the destination name absent, then verifies the result byte for byte.
- **`esa instance create`:**
  1. verify the seed (`--expect-artifact-id` for a quick check, or `--full`);
  2. clone it into a new owned `TART_HOME`;
  3. apply only the declared host-side transformations;
  4. check that the config diff is bounded, NVRAM is unchanged and the disk size is unchanged.

  It prints the `tart run` arguments for the consumer.
- **`esa instance delete`:** removes only a home that `esa instance create` made (it carries an instance-home kind marker as well as the ownership marker) and that holds only that VM. Run directories and other ESA-owned trees are refused.

## ESA-QUAL-01: native qualification

- **Purpose:** on the supported host and Tart version, run each family through build, clone, boot and readiness, and measure the observation metrics with stated endpoints. This is separately authorized.
- **Status:** not started. A successful image build is not qualification, and qualification is not consumer acceptance.
- **Open native dependencies:**
  - an aarch64-linux `kvm` build store;
  - the native Nix 2.31.5 and OpenSSH pair honouring ESA's `NIX_SSHOPTS`: `-F /dev/null`, skipping `IdentityFile=none` entries, no fallback to default identities or an unselected agent, and strict checking against Nix's pin;
  - evaluation of the recipes against the real pins;
  - the LKL `cptofs` text;
  - Tart 2.40.1 runtime behaviour: `ip --wait`, plus `run --dir` and the virtiofs tag `com.apple.virtio-fs.automount`, which are confirmed in source only, and the `control.sock` left after a clean exit being removable under the residue conditions above;
  - fallback-loader boot timing;
  - the operator's Tart registry credential-access route for an anonymous ghcr.io pull;
  - a first clone into an empty home leaving `config.json` and `nvram.bin` byte-identical to their layers;
  - the Tahoe seal finalizer: the sshd process ancestry it waits on, `launchctl bootstrap` from `/private/var/tmp`, the `pf` rule and how `pfctl -s rules` prints it, the `pkill` patterns, Background Task Management or launchd records left on disk, a clean `tart run` exit after `shutdown -h now`, a stopped disk with no host keys, and SSH reachable on fresh clones;
  - vanilla SSH and `sudo`;
  - the Nix installer flags;
  - nix-darwin activation;
  - APFS growth;
  - consumer guest-agent packaging;
  - the attended Security, TCC and Passwords test after `--random-serial`.

## Consumer-side units (owned by consumers, not ESA)

- Profile and customization module, with pins, a declared unprivileged service account if needed, and any guest-agent package.
- Allowlist and receipt migration to ESA artifact identities, with positive and negative tests.
- Acceptance canary through the consumer's real path, including fresh-instance and cleanup proof.
