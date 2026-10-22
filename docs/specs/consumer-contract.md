# ESA consumer contract

Status: accepted for the foundation. Applies to `esa-manifest-v1`.

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
| | Instance lifecycle: clone, start, stop, delete, scoped to its own attempt |
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
2. **Build.** A future ESA backend produces a Tart VM directory and an artifact manifest. No backend is implemented yet.
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
5. **Instance.** The clone gets fresh identifiers, as listed in `seed.regenerate_on_clone`. The consumer injects per-attempt credentials and registration, runs the job, and deletes the clone and any retained diagnostics it created. Nothing from a job flows back into the seed.
6. **Adoption and rollback.** Keep the previously accepted seed and receipt until the new one is accepted. A failed candidate leaves the working seed usable. Linux and macOS seeds are accepted independently.

## Example: Linux CI runner consumer (`linux-ci`)

See [`examples/profiles/linux-ci.profile.json`](../../examples/profiles/linux-ci.profile.json).

**ESA provides:**

- a `nixos-minimal` arm64 Tart seed with no registration material, no runner state and no runner service;
- `inherited_default_account: false`;
- fresh `machine-id`, SSH host keys and MAC address per clone.

**The consumer's private profile and module supply:**

- the toolchain it needs, declared as capability tokens (for example Git, Bash, CA certificates, SSH, transfer and checksum tools, jq, a Go toolchain, Node, a C toolchain and its verified runner executable);
- an unprivileged runner account;
- key-only SSH after bootstrap.

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

**The vanilla base keeps the upstream recipe's publicly known administrator login.** The example therefore declares `inherited_default_account: true`. The consumer must run such seeds only on an isolated network, or rotate that credential at instance start.

**Apple's terms.** The macOS software license agreement, not ESA's licence, governs macOS use. It limits virtualization, for example the number of virtual instances and the requirement to run them on Apple-branded hardware. ESA's commercial licence conveys no Apple rights. Each consumer is responsible for its own compliance.

## Compatibility for consumer owners

- Readers must refuse an unknown `schema` value. They must not guess.
- v1 may still be amended until the first artifact is accepted by any consumer; after that it is frozen ([manifest v1, Compatibility](manifest-v1.md#compatibility)).
- If your receipt needs a field that v1 excludes from identity, or a field v1 lacks, raise it before the first acceptance.
