# ESA manifest v1 (`esa-manifest-v1`)

Status: accepted for the foundation and amended in place by the backend units ([ADR 0002](../adr/0002-esa-backends.md)). Not frozen; see [Compatibility](#compatibility).

This document is the normative definition of the ESA manifest. `esa validate`
([`src/esa/manifest.py`](../../src/esa/manifest.py)) enforces it. If the two
disagree, report the discrepancy. Neither is silently authoritative, and v1
has no separate JSON Schema file
([ADR 0001](../adr/0001-esa-foundation.md)).

## Two kinds, one schema

| Kind | Owner | Purpose |
|---|---|---|
| `profile` | consumer | Declares what an image must be built from and what it must provide. It lives in the consumer's repository and may stay private. |
| `artifact` | ESA build (`esa record`, [ESA-REC-01](../architecture/backend-work-units.md)) | Describes one built Tart VM directory: its exact payload, its provenance and the profile it was built from. |

Both kinds share the schema identifier `esa-manifest-v1` and are closed:

- an unknown field is refused, at every level;
- a missing required field is refused;
- `null` is accepted only where a field below says so.

## Encoding

- **File format.** UTF-8 JSON, at most 1 MiB, with a JSON object at the top level.
- **Refused encodings.** A byte-order mark, duplicate keys, `NaN`, `Infinity`, trailing data, any non-integer number such as `4.0` or `1e3`, and integers with more than 19 digits are all refused.
- **Strings.** Every string and key must be printable ASCII (0x20–0x7E), and strings are at most 1024 characters (URLs at most 512). Non-ASCII text is not needed by any v1 field, and the restriction keeps the canonical form trivial for other implementations.
- **Booleans are not integers.** JSON `true` is never accepted as an integer, and `1` or `"true"` is never accepted as a boolean.
- **Hash spelling.** Every digest is written `sha256:` followed by 64 lowercase hex digits. The one exception is a Nix `nar_hash`, which keeps the SRI form `sha256-<44 base64 chars>` exactly as `flake.lock` records it.
- **Tokens.** Where a field says *token*, the value matches `^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$` and is at most 64 characters.

## Common fields

| Field | Rule | Meaning |
|---|---|---|
| `schema` | `"esa-manifest-v1"` | Any other value is refused before anything else is read. |
| `kind` | `"profile"` or `"artifact"` | |
| `label` | `^[a-z][a-z0-9-]{0,62}$` | A mutable display name. It is **not identity-bearing**, so renaming changes no hash. |
| `family` | `"nixos-minimal"` or `"tahoe-vanilla"` | The only supported image families. |
| `arch` | `"arm64"` | No aliases: `aarch64`, `x86_64` and `ARM64` are refused. |
| `platform` | `"tart"` | |

### Families

- **`nixos-minimal`** is a minimal *installed* NixOS aarch64-linux system that ESA constructs directly from pinned Nixpkgs. It is not the NixOS installer ISO and not a container image. ESA selects no registry source for Linux, so any published Tart image, upstream or otherwise, is outside this family.
- **`tahoe-vanilla`** is the upstream Cirrus Labs `macos-tahoe-vanilla` Tart image for macOS 26, resolved to an immutable digest. Pinned Nix and nix-darwin configuration is applied inside the guest. nix-darwin configures the installed Apple OS; it does not replace it.
  - The upstream recipe is automation-oriented: it creates an administrator account, enables SSH and changes several session and security settings. So "vanilla" does not mean untouched Apple defaults. See `seed.inherited_default_account`.

## Profile fields

A profile has the common fields plus `revision`, `resources`, `inputs`,
`customization`, `capabilities`, `readiness` and `seed`.

| Field | Rule | Meaning |
|---|---|---|
| `revision` | integer ≥ 1 | A consumer-owned counter. Bump it whenever the profile changes. |
| `resources.cpu_count` | integer 1–64 | The virtual CPU count the VM is configured with. |
| `resources.memory` | `^[1-9][0-9]{0,6}(MiB\|GiB)$`, 512 MiB – 512 GiB | Guest RAM in binary units. `8GB`, `8 GiB`, `8gib` and `8.5GiB` are refused. |
| `resources.disk` | `^[1-9][0-9]{0,4}(GB\|GiB)$`, numeric part 1 – 4096 | The virtual disk *capacity* presented to the guest, in exact bytes: `GB` is 10⁹ bytes and `GiB` is 2³⁰ bytes. This is a logical size, not host allocation. The capacity must be a whole number of the unit the family's construction tool takes, and is refused rather than rounded otherwise: **nixos-minimal** needs whole MiB (Nix `make-disk-image` `diskSize`), so any `GiB` value or a `GB` value that is a multiple of 2048; **tahoe-vanilla** needs whole decimal GB (`tart set --disk-size`, which Tart multiplies by 1000³), so only `GB`. A tahoe-vanilla disk smaller than the upstream base disk is refused at construction; disks are grown, never shrunk. |

### `inputs` (family-conditional, all pinned)

| Field | Families | Rule |
|---|---|---|
| `nixpkgs.rev` | both | A 40-character lowercase hex commit. Branch, channel, tag and short revisions are refused as moving or ambiguous. |
| `nixpkgs.nar_hash` | both | The Nix SRI hash of the locked source. |
| `flake_lock_sha256` | both | The sha256 of the consumer-supplied `flake.lock` bytes. |
| `nixos_release` | nixos-minimal only | `^[0-9]{2}\.(05\|11)$`, for example `26.05`. The OS version declared by the profile. |
| `nix_darwin.rev`, `nix_darwin.nar_hash` | tahoe-vanilla only | The locked nix-darwin input, using the same rules as `nixpkgs`. |
| `macos.product_version` | tahoe-vanilla only | `^26\.[0-9]{1,2}(\.[0-9]{1,2})?$`. |
| `macos.build` | tahoe-vanilla only | `^25[A-Z][0-9]{1,4}[a-z]?$`. macOS 26 builds carry the Darwin 25 prefix. |
| `macos.base` | tahoe-vanilla only | `{kind: "oci", repository: "ghcr.io/cirruslabs/macos-tahoe-vanilla", digest: sha256}`. A tag such as `:latest` and any other repository are refused. |
| `macos.recipe` | tahoe-vanilla only | `{repository: "https://github.com/cirruslabs/macos-image-templates", rev: 40-hex, path: "templates/vanilla-tahoe.pkr.hcl"}`. An independently pinned upstream recipe that ESA inspected for the inherited settings it documents. It is **not** an attestation that this recipe produced `macos.base`, and it does not establish the base's exact OS build; only guest `sw_vers` observations do. |
| `nix_installer.version`, `nix_installer.sha256` | tahoe-vanilla only | The exact Nix release (`MAJOR.MINOR.PATCH`) installed in the guest, and the sha256 of its `aarch64-darwin` release tarball. The URL is derived, never declared: `https://releases.nixos.org/nix/nix-V/nix-V-aarch64-darwin.tar.xz`. The guest refuses the tarball unless its sha256 matches. |

Any key that belongs to the other family is refused as "not permitted for family …".

**Pin authority.** The profile's pins are authoritative for the artifact.

- At build time the backend refuses a `flake.lock` whose sha256 differs from `flake_lock_sha256`, or whose `nixpkgs` (and for tahoe, `nix-darwin`) node differs from `inputs`. The lock must be version 7 and hold exactly the root and those nodes, each locked as `github:NixOS/nixpkgs` or `github:nix-darwin/nix-darwin`; for tahoe, `nix-darwin`'s `nixpkgs` must follow the root `nixpkgs`.
- ESA backend units may publish pin sets they have tested. A consumer adopts one by copying it into the profile. ESA never substitutes its own pins silently.

### `customization`

`{kind, sha256}`:

- `kind` is `nixos-module` (nixos-minimal), `nix-darwin-module` (tahoe-vanilla) or `none`.
- `sha256` is the digest of the consumer-held module file. It is `null` exactly when `kind` is `none`.

The module must be **self-contained**. It may use only the pinned flake inputs, ESA's recipe options, and fixed-output fetches whose hashes are written in the module itself (so the module digest covers them). It may not contain path literals or `<...>` lookups. The backend refuses those before evaluating, then evaluates the module as a pure flake in a workspace that holds only generated files, so any other access fails the build rather than escaping the digest. ESA never needs the module's path or contents in a manifest.

### `capabilities`

`{guest: [token…≤32], host: [token…≤16]}`. Both lists are sorted and unique.

- `guest` lists what the guest is declared to provide.
- `host` lists what the host must provide to run it.

These are **declared claims**. ESA does not verify them. Evidence of verification lives only in an artifact's `validation` entries, and acceptance belongs to the consumer.

### `readiness`

`{method, timeout_seconds, consumer_check}`:

- `method` is `ssh` or `tart-guest-agent`: how the guest is first reachable after a clone boots.
- `timeout_seconds` is 1–3600.
- `consumer_check` is a token naming the consumer's own readiness check, or `null`. It is opaque to ESA.

`clone_to_ready_ms` observations are measured to the endpoint named here.

### `seed`

These are recipe attestations about the reusable seed, not proofs. Each is checked by the backend unit and recorded as `validation` evidence.

| Field | Rule | Meaning |
|---|---|---|
| `machine_identity_uninitialized` | `true` | Construction added no instance identity. On Linux, `/etc/machine-id`, the dbus machine-id and both random seeds are absent. On macOS, the identifier set is the config `macAddress`, the config `ecid` (the Virtualization platform machine identifier from which the guest hardware UUID and serial derive) and the SSH host keys; `hardwareModel` is not an identity. The seed's `macAddress` and `ecid` are inherited from the upstream base and are seed-scoped, not unique: fresh values come only from the per-instance transformation (see `regenerate_on_clone`). |
| `ssh_host_keys_absent` | `true` | No SSH host keys are present; the guest generates them on first boot. |
| `registration_material_absent` | `true` | No CI or service registration: no tokens, no `.runner` files, no runner UUIDs, no enabled runner services. |
| `consumer_credentials_absent` | `true` | No consumer credentials and no personal accounts: no keys, passwords, tokens, authorized keys or registration, and no Apple account sign-in. Declared unprivileged service accounts, such as a generic runner account with no password and no keys, are permitted. No authorization or key state is baked into the seed; the consumer injects it per instance. |
| `build_residue_removed` | `true` | The build-only residue retention policy was applied. |
| `inherited_default_account` | strict boolean | `true` means the image keeps a publicly known upstream login, such as the vanilla recipe's administrator account. It must be `false` for nixos-minimal. Consumers must treat a `true` seed as reachable only on an isolated network, or rotate the credential at instance start. |
| `nix_generations_retained` | integer 1–8 | The maximum number of system generations kept in the seed. A freshly built nixos-minimal image always has one. |
| `regenerate_on_clone` | sorted closed tokens | Identifiers that each instance gets fresh. nixos-minimal allows `hostname`, `mac-address`, `machine-id`, `random-seed` and `ssh-host-keys`, and requires `mac-address`, `machine-id` and `ssh-host-keys`. tahoe-vanilla allows `hostname`, `mac-address`, `platform-machine-identifier` and `ssh-host-keys`, and requires `mac-address` and `ssh-host-keys`. |

Fresh values come from ESA's deliberate instance transformation or from first-boot generation in the guest, never from `tart clone` or `tart import` alone. Those regenerate a MAC address only when another VM in the same `TART_HOME` already uses it, and clone does not when the source is suspended. `esa instance create` maps `mac-address` to `tart set --random-mac` and `platform-machine-identifier` to `tart set --random-serial` (a new `ecid`, the same `hardwareModel`), then checks that only those config keys changed. `machine-id`, `random-seed`, `ssh-host-keys` and `hostname` are generated by the guest on first boot. A tahoe-vanilla profile that omits `platform-machine-identifier` gives every instance the base's hardware UUID and serial.

Any `false`, `"true"`, `1` or missing value where `true` is required is refused. There is no override.

## Artifact fields

An artifact has the common fields plus the following. `inputs`, `resources`,
`customization`, `capabilities`, `readiness` and `seed` are copied verbatim
from the profile, so a consumer can inspect an artifact without the private
profile. `esa verify --profile` checks the copy, together with `family`,
`arch` and `platform`.

| Field | Rule | Meaning |
|---|---|---|
| `artifact_id` | `esa-artifact-v1:sha256:<64 hex>` | Must equal the derived identity (see [Identity](#identity)). |
| `esa_revision` | 40 lowercase hex | The ESA commit whose recipes produced the artifact. |
| `profile` | `{label, revision, content_sha256}` | The exact profile. `label` is informational only. |
| `builders` | exactly `nix` and `tart`, each `{name, version}`, sorted by name | `version` is an exact `MAJOR.MINOR.PATCH`, and ranges are refused. `tart` is the host Tart that assembled the VM directory. `nix` is the Nix that built the system: for nixos-minimal, the host Nix client that drove the build (the caller-selected build store, its version and features are private evidence, not identity, because the output is content-addressed by its derivation); for tahoe-vanilla, the guest Nix reported after nix-darwin activation (which may differ from `inputs.nix_installer.version`, because nix-darwin manages Nix from the pinned Nixpkgs). Packer is not used. |
| `payload.format` | `"tart-vm-dir-v1"` | A standalone Tart VM directory (see [Tart VM directory](#tart-vm-directory-tart-vm-dir-v1)). |
| `payload.files` | exactly `config.json`, `disk.img`, `nvram.bin`, sorted, each `{path, size_bytes, sha256}` | `size_bytes` is the file's logical size (`st_size`), so a sparse `disk.img` reports its full capacity. |
| `payload.digest` | sha256 | Must equal the sha256 of the canonical JSON of `payload.files`. Validation recomputes it. |
| `storage.disk_logical_bytes` | integer bytes | The virtual disk capacity. Must equal `resources.disk` in bytes and the `disk.img` `size_bytes`. |
| `storage.payload_bytes` | integer bytes | The sum of `payload.files[].size_bytes`, as logical bytes. It is not host allocation. |
| `observations` | ≤16 `{report_id, observed_at, metrics}`, `report_id` unique | Variable measurements, see below. |
| `validation` | ≤32 `{check: token, result: pass\|fail, evidence_sha256}` | References to bounded validation results held elsewhere, as `esa-evidence-v1` files. `esa record` takes `check` and `result` only from evidence files that bind the recorded `disk.img` sha256, so evidence from another payload is refused; binding prevents reuse, it does not make a report true. These are claims about one build and do not imply boot, readiness, native qualification or admission. |
| `upstream_licenses` | 1–32 `{component: token, license, reference}` | `license` is an SPDX identifier or expression, or a `LicenseRef-*`. `reference` is an https URL with no credentials, query or fragment. These describe the payload's terms; ESA's own licence is not the payload licence. |

### Observation metrics

`observed_at` is a UTC timestamp `YYYY-MM-DDTHH:MM:SSZ`. `metrics` holds at
least one of the following non-negative integers. The unit is part of each
name.

| Metric | Meaning |
|---|---|
| `host_allocated_bytes` | Host storage actually occupied by the seed directory. This depends on the filesystem: sparse files and APFS clones can make it change. |
| `guest_used_bytes` | Space used inside the guest filesystem. |
| `clone_growth_bytes` | Extra host storage a disposable clone consumed during one job. |
| `compressed_transfer_bytes` | Size of the compressed form used to distribute the seed. |
| `build_workspace_bytes` | Peak host cache and workspace used outside the retained seed. |
| `clone_to_ready_ms` | Time from clone start to the `readiness` endpoint. |

Virtual disk capacity is `storage.disk_logical_bytes`, which is not an
observation. An observation is identified by its `report_id` and time. It is
**never** part of artifact identity.

## Identity

**Canonical JSON.** The canonical form of a value is
`json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`
encoded as ASCII. In other words:

- object keys are sorted by code point;
- there is no insignificant whitespace;
- there are no floats;
- all strings are printable ASCII.

**Golden vector.** The canonical bytes of
`[{"path":"config.json","sha256":"sha256:000…000","size_bytes":2}]` (with 64
zeros) are exactly
`[{"path":"config.json","sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","size_bytes":2}]`.
Their digest is
`sha256:a8f30ee0f6cae65dbce836376128027542732ca34c94d6161ff69854f7da7a35`.

| Identity | Derivation | Meaning |
|---|---|---|
| Profile content hash | sha256 of the canonical profile with `label` removed | Exactly this profile content. |
| `payload.digest` | sha256 of the canonical `payload.files` | Exactly these VM-directory bytes. |
| `artifact_id` | `esa-artifact-v1:sha256:` + the sha256 of the canonical object holding `schema`, `family`, `arch`, `platform`, `esa_revision`, `profile` (without `label`), `inputs`, `resources`, `customization`, `capabilities`, `readiness`, `seed`, `builders` and `payload` | These bytes, built from this declared provenance. |

The fields excluded from `artifact_id` are `label`, `artifact_id`, `storage`
(which is derived), `observations`, `validation` and `upstream_licenses`.
`esa inspect` therefore marks its `validation` summary
`"identity_bearing": false`. A consumer that relies on validation results or
licensing references must copy them into its own receipt.

`esa identity` derives the profile content hash, and for an artifact prints the
recorded `artifact_id` only after checking it against the derivation. `esa
record` produces new artifact manifests and their identities from a stopped
VM directory.

Identical payload bytes recorded with a different ESA revision or profile get
the same `payload.digest` but a different `artifact_id`. A consumer may key
admission on either value, and must say which.

A stable identity is never a local Tart VM name or a registry tag. Both are
mutable labels.

**A digest is not admission.** An `artifact_id` or digest means "these exact
bytes and declarations". It does not mean trusted, approved, qualified or
admitted. ESA has no admission field, and `esa inspect` reports
`"admission": "not-determined-by-esa"`. Admission is a consumer decision,
recorded in the consumer's own receipt.

## Qualification and refusal

- **Contract-unqualified.** A document that fails any rule above is refused by every command, with exit status 1. Examples are a missing, unpinned or moving input, an unsupported family, architecture, platform or base source, a missing conditional OS field, a malformed unit, a false seed attestation, a hash mismatch, or refused material. There is no draft mode, no `--force` and no partial acceptance.
- **Backend-unqualified.** In this ESA revision both families have implemented construction commands, but neither has passed native qualification (ESA-QUAL-01). `esa plan` marks each step `implemented: true` and `native_qualified: false`, with `construction.status: "implemented-not-native-qualified"`. `esa plan` itself executes nothing.
- **Consumer-unqualified.** Whether an artifact satisfies a consumer is decided by the consumer's acceptance. ESA does not determine it.

## Refused material

`esa validate` scans every string value and refuses:

- private-key blocks;
- SSH public keys;
- common access-token prefixes;
- UUID-shaped identifiers, such as runner UUIDs;
- `.runner` files;
- host home paths (the macOS and Linux user home roots, `root`'s home, and `~`-relative paths);
- email addresses;
- credential assignments such as `token=` or `password:`.

Scan findings name the JSON pointer and category, never the matched text.
Other refusals do not echo string values either, but every pointer contains
the document's own key names, with non-printable characters escaped. This is
a guard against accidents in manifests and examples, not a security boundary.
It cannot prove that a VM payload is free of secrets; that is what the seed
attestations and their validation evidence are for.

## Payload verification

`esa verify ARTIFACT --payload-dir DIR`:

- requires `DIR` to be a real directory containing exactly the manifest's files, as regular files (no symlinks) with matching sizes;
- streams sha256 over each file, refusing a file that changes while it is hashed;
- parses `config.json` strictly (see below) and compares its OS, CPU and memory with the artifact's family and `resources`;
- checks the GPT header of `disk.img`;
- with `--quick`, size-checks `disk.img` only (no GPT read) and hashes and parses the other files in full;
- with `--expect-artifact-id ID` or `--expect-payload-digest DIGEST`, refuses unless the manifest carries exactly that value. `--quick` requires one of them, because a manifest that is merely self-consistent can be swapped or regenerated alongside a modified seed.

An unreadable payload directory or file is an I/O error (exit status 2), not a
contract refusal.

Verification applies to the **stored seed directory**, not to a clone or a
running instance. Tart writes per-VM values such as the MAC address into a
clone's `config.json`, and a booted guest changes its disk.

## Tart VM directory (`tart-vm-dir-v1`)

Confirmed against Tart 2.40.1 source (`VMConfig.swift`, `VMDirectory.swift`,
`Commands/Set.swift`):

- A standalone VM directory is exactly `config.json`, `disk.img` and `nvram.bin`; Linux guests use `nvram.bin` as their EFI variable store. A suspended VM (`state.vzvmsave`), stacked or ASIF disks (`overlay.asif`, `diskFormat: "asif"`) and content-store records (`manifest.json`) are not `tart-vm-dir-v1` and are refused.
- `disk.img` is a raw image, `diskFormat: "raw"`, whose logical size is the disk capacity, with a GPT header.
- `config.json` holds exactly `version` (1), `os` (`linux` for nixos-minimal, `darwin` for tahoe-vanilla), `arch` (`arm64`), `cpuCountMin`, `cpuCount`, `memorySizeMin`, `memorySize` (bytes), `macAddress`, `display` (`{width, height, unit?}`), `diskFormat` and the optional `displayRefit`; darwin configs also hold `ecid` and `hardwareModel` (base64), and linux configs never do. Any other key is refused.
- `cpuCount` must equal `resources.cpu_count` and `memorySize` must equal `resources.memory` in bytes; on darwin both must also be at least their minimums.
- A running VM holds a POSIX record lock on `config.json`; `esa record` refuses a directory whose lock is held. That check shows "not running now", not exclusive custody.

## Compatibility

- v1 is closed. A new field, a new allowed value or a changed meaning requires a new schema identifier. Readers refuse unknown identifiers and never reinterpret a document.
- Until a consumer accepts its first v1 artifact, v1 may still be amended in place, together with this document, the validator and its tests. The backend units amended it to fix disk units, the Tart VM-directory and `config.json` rules, the macOS identifier set, the Nix installer input and the builder set.
- After that first acceptance, v1 is frozen. ESA then keeps a v1 validator for as long as any accepted v1 artifact remains in use.

## Known gaps (to resolve before freezing v1)

- Consumer customization self-containment is enforced only at build time, by early refusal of path literals and pure flake evaluation of an isolated workspace.
- Native qualification (ESA-QUAL-01) is pending for both families. The rules above are confirmed against Tart source, not against an executed Tart.

## What would make this specification wrong

- Tart VM directories need a file outside `config.json`, `disk.img` and `nvram.bin`, Linux guests do not use `nvram.bin`, or an executed Tart 2.40.1 writes `config.json` keys other than those listed above.
- A consumer needs a tahoe-vanilla disk in GiB, a nixos-minimal disk that is not a whole MiB, or a seed with no inherited bootstrap account.
- A consumer's receipt or allowlist needs a field that is excluded from identity, such as `validation`, to be identity-bearing.
- The upstream vanilla image is not published as `ghcr.io/cirruslabs/macos-tahoe-vanilla`, or a supported macOS 26 build does not start with `25`.
- A consumer needs a non-arm64 guest, an OCI base for Linux, or a base other than the upstream vanilla image for macOS.
- A consumer must keep its profile pins secret even from the artifact. The artifact copies `inputs`.
- Builders report versions that are not `MAJOR.MINOR.PATCH`.
