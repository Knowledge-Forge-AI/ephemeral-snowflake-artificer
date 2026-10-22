# Backend work units

Status: planned. **None of these units is implemented.** The foundation
delivers only the manifest contract, the CLI and documentation. `esa plan`
names these units in every step and marks each step `implemented: false`.

Each unit must be authorized and reviewed on its own. A unit lists its
inputs, outputs, refusal conditions and the evidence it must retain. A unit
that discovers v1 needs a change amends [manifest v1](../specs/manifest-v1.md),
its validator and its tests together, before any consumer accepts a v1
artifact.

The Linux and macOS tracks are independent once the contract exists.

## Shared interface

**Input:**

- a profile that passes `esa validate`;
- the consumer-supplied files named by `esa plan` (`consumer_supplied_files`), each matching its sha256;
- the ESA commit (`--esa-revision`).

**Output:** a Tart VM directory (`config.json`, `disk.img`, `nvram.bin`) plus
an artifact manifest that passes `esa validate` and `esa verify --profile`.

**Refusals:** a backend must refuse, never substitute or float:

- a missing or mismatched consumer file;
- a lock whose pinned nodes differ from `inputs`;
- an unpinned tool;
- a failed seed attestation.

**Evidence:** command logs, tool versions and seed-check results, retained
outside the repository. `validation[].evidence_sha256` references them.

## ESA-NIX-01: NixOS minimal system and raw disk

- **Purpose:** build an aarch64-linux EFI/GPT raw disk of `resources.disk` capacity from a declarative flake. The flake combines ESA's minimal system module with the consumer's self-contained `nixos-module`, evaluated in isolation against the profile's `flake.lock`.
- **Interface sketch:**
  - an ESA flake output parameterized by the profile (`nixos_release`, resources);
  - the consumer module, by digest;
  - output: `disk.img` (raw, sparse).
- **Lessons carried from the reference NixOS VMware/VZ recipes:**
  - `make-disk-image` with `partitionTableType = "efi"` and systemd-boot, and no copied channel;
  - define the disk size once and reuse it for the image and its provenance;
  - make-disk-image's `cptofs` copies the root through LKL, whose library kernel memory was hard-coded small enough to fail on a realistic closure. Patch only the image builder's copy, and fail the build if the upstream line changes;
  - the image derivation requires the `kvm` system feature. Evaluating on a Mac is not enough: an aarch64-linux builder with working `/dev/kvm` is required, such as a dedicated VZ Linux builder with nested virtualization (macOS 15+ on M3 or newer);
  - sparse logical capacity is not host allocation. Record both.
- **Decides:**
  - the tested Nix and Nixpkgs pin set offered to consumers;
  - whether the Linux build environment becomes a `builders` entry (manifest known gap).
- **Must not:** copy personal guest configuration, bootstrap SSH keys or user accounts from any reference recipe.

## ESA-NIX-02: Tart VM directory and Linux seed preparation

- **Purpose:** assemble a Tart VM directory around the ESA-NIX-01 disk with `resources.cpu_count` and `resources.memory`, and apply the seed policy.
- **Seed policy:**
  - remove `/etc/machine-id`, the dbus machine-id, SSH host keys and the systemd random seed before the filesystem is final;
  - retain exactly `nix_generations_retained` generations;
  - remove obsolete GC roots while keeping the required runtime roots;
  - collect garbage;
  - optimise the store;
  - prepare free space for sparse compaction.
- **Must confirm against the supported Tart version** (and amend v1 if wrong):
  - the VM directory file set (`tart-vm-dir-v1`);
  - that Linux guests use `nvram.bin` and boot via EFI;
  - the `config.json` CPU and memory fields;
  - that `tart clone` regenerates the MAC address.
- **Must verify:**
  - that a fresh clone boots from the registered EFI disk entry, with no network-boot fallback. The reference measurements found a firmware network-boot fallback that cost about a minute until the disk loader was registered;
  - that two successive clones share no machine-id, host keys or job data.
- **Defines:** how a consumer injects per-instance access into a key-free seed (for example a guest agent or a first-boot mechanism). Readiness must not depend on a key baked into the seed.

## ESA-MAC-01: Tahoe vanilla seed

- **Purpose:** produce a macOS 26 seed from `inputs.macos.base` (the upstream vanilla image, fetched by digest only) at `resources`.
- **Steps:**
  - confirm `product_version` and `build` inside the guest;
  - record the upstream recipe's inherited session and security settings that matter to the profile, at `inputs.macos.recipe.rev`;
  - check the upstream repository's licence and add any required NOTICE entry before deriving from its templates.
- **Defines:**
  - the bootstrap credential policy. Credentials are supplied by the consumer at construction or instance time and never stored in the repository or manifests;
  - whether the inherited administrator login is kept (`inherited_default_account: true`) or rotated;
  - the macOS identifier set behind `machine_identity_uninitialized` and `platform-machine-identifier`, and how `tart clone` treats it.
- **Must not:** broaden host permissions, or remove tested guest behaviour (GUI session, applications, automation permissions) to shrink the image.

## ESA-MAC-02: Nix and nix-darwin layer

- **Purpose:** install pinned Nix in the guest and apply the pinned nix-darwin configuration plus the consumer's self-contained `nix-darwin-module`.
- **Reproducibility claim:** stated in terms of pinned inputs, declared configuration and observed validation. Never claim identical disk bytes unless that is independently demonstrated.

## ESA-REC-01: `esa record`

- **Purpose:** add a command that hashes a built VM directory and emits the artifact manifest. It records:
  - the builder versions;
  - the copied profile fields;
  - the payload files, `storage` and derived identities;
  - an initial `observations` entry;
  - `upstream_licenses`.
- **Must:** produce manifests that pass `esa validate` unchanged, and never invent validation results.

## ESA-QUAL-01: native qualification

- **Purpose:** on the supported host and Tart version, build, clone, boot and reach readiness for each family, and measure the observation metrics with stated endpoints. This is separately authorized.
- **Status:** a successful image build is not qualification, and qualification is not consumer acceptance.

## Consumer-side units (owned by consumers, not ESA)

- Profile and customization module, with pins.
- Allowlist and receipt migration to ESA artifact identities, with positive and negative tests.
- Acceptance canary through the consumer's real path, including fresh-clone and cleanup proof.
