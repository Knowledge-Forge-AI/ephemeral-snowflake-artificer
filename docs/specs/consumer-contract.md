# ESA consumer contract

Status: accepted for the foundation; updated for the implemented backends ([ADR 0002](../adr/0002-esa-backends.md)). Applies to `esa-manifest-v1`.

This document tells a consumer owner what ESA provides, what the consumer
owns, and how an accepted local seed is used without rebuilding it. It uses
two generic example consumers: `linux-ci` and `macos-gui`. Consumer-specific
pins, profiles, fixtures and admission rules stay in the consumer's own
repository. ESA never reads a consumer checkout, and a consumer needs only the
standard-library `esa` CLI, or its own reader of the published format, to
read an artifact.

## Division of ownership

| ESA owns | The consumer owns |
|---|---|
| Image recipes and backend construction ([backend work units](../architecture/backend-work-units.md)) | Its profile and customization module, its pins, and their revision |
| The manifest format, identity rules and refusal rules ([manifest v1](manifest-v1.md)) | Admission: which `artifact_id` or `payload.digest` it accepts, and why |
| Generic seed preparation and generic validation evidence | Consumer validation and acceptance tests, fixtures and assertions |
| Recording provenance and upstream licensing references | Runtime injection of credentials, source and fixtures into each instance |
| The seed custody and instance helper (`esa seed`, `esa instance`) | Instance lifecycle decisions: when to create, start, stop and delete, scoped to its own attempt |
| Generic guest-agent plumbing (`esa.guestAgent`) | Any guest-agent package, its source, checksum and licence |
| | Where its seeds are stored, and their visibility |

ESA is not a credential broker, a VM scheduler or a registry. Publishing
ESA's source does not select a public image registry. Artifact storage is
local or private unless a consumer's profile documentation states otherwise.

## What ESA guarantees, and what it does not

ESA guarantees that an artifact that passes `esa validate`:

- uses a supported family, `arm64` and `tart`, with every input pinned;
- carries the declared seed attestations;
- has an `artifact_id` and `payload.digest` derived exactly as specified.

ESA does **not** guarantee:

- admission or trust ("a digest is not admission");
- that declared `capabilities` are true;
- that an image boots on a given host or Tart version;
- that a payload is free of secrets beyond the seed attestations and their recorded evidence;
- any upstream licence grant.

## Lifecycle

1. **Profile.** The consumer writes a profile and keeps it in its own repository. `esa validate` and `esa plan --esa-revision <commit>` refuse unpinned or unsupported declarations before any build exists.
2. **Build.** `esa nixos prepare|construct` or `esa tahoe prepare|construct` produces a stopped, read-only Tart VM directory and an artifact manifest, using tool paths, a build store and a bootstrap credential file chosen by the caller (see [backend work units](../architecture/backend-work-units.md)). Construction is implemented but **not native-qualified**: no family has passed ESA-QUAL-01, and a successful build is neither qualification nor acceptance.
3. **Admission (once per artifact).** The consumer runs `esa verify ARTIFACT --profile PROFILE --payload-dir SEED_DIR` (a full hash) and its own acceptance tests. It then records a receipt in its own repository or private store. The receipt contains:
   - `schema` and `artifact_id`, and `payload.digest` if the consumer keys on it;
   - the profile `revision` and `content_sha256`;
   - the local Tart VM name and storage location, which are mutable labels and not identity;
   - the admission decision and a reference to its evidence.
4. **Per job (no rebuild, no registry, no source evaluation).** Before cloning, the consumer confirms that the named local seed still matches its receipt, using `esa verify ARTIFACT --payload-dir SEED_DIR --quick --expect-artifact-id ID` (or `--expect-payload-digest DIGEST`, whichever the receipt keys on). That command:
   - refuses unless the manifest's `artifact_id` (or `payload.digest`) equals the receipt's value, so a swapped or regenerated manifest is not accepted;
   - hashes `config.json` and `nvram.bin` in full;
   - size-checks `disk.img` without reading its contents.

   `--quick` refuses to run without an `--expect-*` value. A manifest that only agrees with itself proves nothing about admission.

   The consumer then clones the seed and never boots the seed itself. A consumer that needs stronger per-job assurance runs the full verify, at the cost of hashing the whole disk.
5. **Instance.** `esa instance create --artifact A --seed-dir SEED --tart PATH --tart-home NEW_HOME --name N (--expect-artifact-id ID | --full) [--inject-dir DIR]` does four things:
   - it verifies the seed;
   - it clones it into a new ESA-owned `TART_HOME` (APFS clone, or `--copy-mode sparse-copy`);
   - it applies the host-side identifier transformations the artifact declares: `tart set --random-mac`, plus `--random-serial` for `platform-machine-identifier`;
   - it refuses unless only those config keys changed.

   Tart's own clone or import does not guarantee fresh identifiers: it changes a MAC only on a collision. The guest generates machine-id, random seed and host keys on first boot; that generation is declared, not verified by ESA.

   The consumer starts the instance with the printed `tart run` arguments and injects per-attempt credentials and registration. For nixos-minimal, authorized keys go in `DIR/authorized_keys/<user>`, shared read-only as `esa-inject`. Without that share the instance has no SSH access. The consumer then runs the job and calls `esa instance delete` to remove the instance and its home. Nothing from a job flows back into the seed, and a transformed or booted instance is never verified as if it were the seed.
6. **Adoption and rollback.** Keep the previously accepted seed and receipt until the new one is accepted. A failed candidate leaves the working seed usable. Linux and macOS seeds are accepted independently.

## Example: Linux CI runner consumer (`linux-ci`)

See [`examples/profiles/linux-ci.profile.json`](../../examples/profiles/linux-ci.profile.json).

**ESA provides:**

- a `nixos-minimal` arm64 Tart seed with no registration material, no runner state and no runner service;
- `inherited_default_account: false`;
- fresh `machine-id`, SSH host keys and MAC address per clone.

**The consumer's private profile and module supply:**

- the toolchain it needs, declared as capability tokens (for example Git, Bash, CA certificates, SSH, transfer and checksum tools, jq, a Go toolchain, Node, a C toolchain and its verified runner executable);
- an unprivileged runner account with no password and no keys. `consumer_credentials_absent` permits such a declared service account; it excludes personal accounts and every credential;
- key-only SSH, through per-instance `esa-inject` keys;
- if its readiness method is `tart-guest-agent`, the exact agent package as `esa.guestAgent.package`.

**The consumer owns:**

- per-attempt runner registration and cleanup;
- its migration of OS, profile and receipt allowlists from the previous seed to the ESA artifact identity. It should key on `(schema, artifact_id)` and keep rejecting unapproved identities.

## Example: macOS GUI-automation consumer (`macos-gui`)

See [`examples/profiles/macos-gui.profile.json`](../../examples/profiles/macos-gui.profile.json).

**ESA provides:**

- a `tahoe-vanilla` macOS 26 seed with pinned Nix and nix-darwin configuration;
- declared GUI-session, application and automation capability tokens.

**The consumer owns:**

- fixtures, assertions, synthetic guest users and data;
- application and accessibility permission expectations;
- its artifact-admission rules. ESA never verifies TCC or accessibility grants.

**The vanilla base keeps the upstream recipe's publicly known administrator login.** The example therefore declares `inherited_default_account: true`, and that is the only constructible value. The consumer must run such seeds only on an isolated network, or rotate that credential at instance start.

- ESA uses the inherited login only during construction. It reads it from a caller-supplied private file through `bin/esa-askpass`, never from arguments, logs or manifests.
- Rotating that console credential before construction is a separately documented human boundary. ESA does not automate it or route around it over the network.
- Before a profile can pin `macos.product_version` and `macos.build`, `esa tahoe observe-base` boots the base once by digest, reports its `sw_vers`, and deletes everything it created.
- ESA does not automate native consent dialogs, TCC grants or the Passwords app. The attended Security, TCC and Passwords test stays with the consumer, and it must cover the effect of `--random-serial` (a new hardware UUID and serial) on Keychain behaviour.

**Apple's terms.** The macOS software license agreement, not ESA's licence, governs macOS use. It limits virtualization, for example the number of virtual instances and the requirement to run them on Apple-branded hardware. ESA's commercial licence conveys no Apple rights. Each consumer is responsible for its own compliance.

## Compatibility for consumer owners

- Readers must refuse an unknown `schema` value. They must not guess.
- v1 may still be amended until the first artifact is accepted by any consumer; after that it is frozen ([manifest v1, Compatibility](manifest-v1.md#compatibility)).
- If your receipt needs a field that v1 excludes from identity, or a field v1 lacks, raise it before the first acceptance.
