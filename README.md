# Ephemeral Snowflake Artificer

**Ephemeral Snowflake Artificer (ESA)** builds reusable virtual-machine image
seeds for orchestration. A consumer accepts a seed once. For each job or test
it clones that seed into a disposable instance, runs the job, and deletes the
instance.

ESA owns:

- image recipes and pinned build inputs;
- backend-specific construction and seed preparation;
- generic validation;
- artifact manifests and the documented consumption contract.

Consumers own their profiles, credentials, admission decisions and instance
lifecycles.

## Current status

**Foundation: contract only.** This revision contains:

- the versioned manifest and consumption contract, `esa-manifest-v1`;
- a standard-library Python CLI that enforces the contract;
- synthetic examples and tests;
- the plan for the backend work units.

It does **not** build, fetch or distribute images. No backend is implemented,
no image has been qualified, and no consumer has been onboarded.

| Family | Platform | Status |
|---|---|---|
| `nixos-minimal`: minimal installed NixOS, aarch64-linux, from pinned Nixpkgs | Tart, arm64 | contract defined; backend not implemented |
| `tahoe-vanilla`: upstream macOS 26 vanilla Tart image by digest, plus pinned Nix and nix-darwin | Tart, arm64 | contract defined; backend not implemented |

## Using the CLI

The CLI needs Python 3.9 or later and nothing else.

```zsh
bin/esa validate examples/profiles/linux-ci.profile.json
bin/esa identity examples/artifacts/linux-ci.artifact.json
bin/esa inspect  examples/artifacts/macos-gui.artifact.json
bin/esa plan     examples/profiles/linux-ci.profile.json --esa-revision <40-hex ESA commit>
bin/esa verify   ARTIFACT.json --profile PROFILE.json --payload-dir SEED_DIR
bin/esa verify   ARTIFACT.json --payload-dir SEED_DIR --quick --expect-artifact-id ID
```

| Command | Purpose |
|---|---|
| `validate` | Checks every v1 rule for a profile or artifact. Missing, unpinned or moving inputs, unsupported families, hash mismatches and secret-like material are refused. There is no override. |
| `identity` | Prints the profile content hash, or an artifact's recorded `artifact_id` after checking its derivation. Recording new artifacts is a later work unit. |
| `inspect` | Prints a deterministic summary, including `"admission": "not-determined-by-esa"`. |
| `plan` | Describes the declarative build plan for a profile. Every step is marked `implemented: false`; nothing is executed. |
| `verify` | Checks an artifact against its profile and/or a local seed directory, and optionally against the `artifact_id` or `payload.digest` in a consumer's receipt (required with `--quick`). It needs no registry, no rebuild and no source evaluation. |

Exit status is `0` for success, `1` for a contract refusal (one
`pointer: message` line per error on stderr) and `2` for a usage or I/O error.

You can also run the CLI as `PYTHONPATH=src python3 -m esa …`.

## Documentation

- [ADR 0001: ESA foundation](docs/adr/0001-esa-foundation.md)
- [Manifest v1 specification](docs/specs/manifest-v1.md): field semantics, units, identity, compatibility
- [Consumer contract](docs/specs/consumer-contract.md): ownership, admission receipts, per-job use
- [Backend work units](docs/architecture/backend-work-units.md): the next NixOS and macOS units
- [Examples](examples/README.md): synthetic only

## Development

```zsh
python3 -m unittest discover -s tests
```

The tests use only the standard library and synthetic files. See
[AGENTS.md](AGENTS.md) for contributor and agent rules.

## License

Ephemeral Snowflake Artificer is licensed under the GNU Affero General Public
License v3.0 or later. See [LICENSE](LICENSE).

Commercial licenses are available for proprietary terms, including
closed-source embedding, private SaaS deployments, OEM use, support,
warranty, indemnity, and custom commercial terms. See
[COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

**That license covers ESA's own recipes, generators, manifest and consumption
contracts, validation tooling, documentation, and other ESA-authored material.
It does not relicense the operating systems, packages, base images, or other
payloads that ESA builds into an image.** Each payload remains governed by
its upstream terms. Apple macOS remains governed by Apple's software license
agreement, and an ESA commercial license conveys no Apple rights. ESA artifact
manifests carry provenance and upstream licensing references.

Contributors retain copyright in their contributions and agree to the terms in
[CLA.md](CLA.md). Every pull request must include the exact acknowledgment
described in [CONTRIBUTING.md](CONTRIBUTING.md).

See [LICENSE](LICENSE), [NOTICE](NOTICE),
[COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md),
[CONTRIBUTING.md](CONTRIBUTING.md), and [CLA.md](CLA.md).
