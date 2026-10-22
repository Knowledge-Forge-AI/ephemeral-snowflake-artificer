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

**Construction implemented; not native-qualified; no consumer onboarded.**
This revision contains:

- the versioned manifest and consumption contract, `esa-manifest-v1`;
- a standard-library Python CLI that enforces the contract;
- construction commands for both families, `esa record`, and a seed custody and instance helper;
- generic Nix recipes under `recipes/`;
- synthetic examples, and tests that use fake `tart`, `nix` and `ssh` processes.

No image has been built or qualified with this revision. A successful build
is not native qualification (ESA-QUAL-01), and qualification is not consumer
acceptance. See [ADR 0002](docs/adr/0002-esa-backends.md) for the open native
dependencies.

| Family | Platform | Status |
|---|---|---|
| `nixos-minimal`: minimal installed NixOS, aarch64-linux, constructed from pinned Nixpkgs | Tart, arm64 | implemented; not native-qualified |
| `tahoe-vanilla`: upstream macOS 26 vanilla Tart image by digest, plus pinned Nix and nix-darwin | Tart, arm64 | implemented; not native-qualified |

## Using the CLI

The CLI needs Python 3.9 or later and nothing else. Construction and custody
commands additionally run the Nix, Tart and OpenSSH executables whose paths
you pass explicitly.

```zsh
bin/esa validate examples/profiles/linux-ci.profile.json
bin/esa identity examples/artifacts/linux-ci.artifact.json
bin/esa inspect  examples/artifacts/macos-gui.artifact.json
bin/esa plan     examples/profiles/linux-ci.profile.json --esa-revision <40-hex ESA commit>
bin/esa verify   ARTIFACT.json --profile PROFILE.json --payload-dir SEED_DIR
bin/esa verify   ARTIFACT.json --payload-dir SEED_DIR --quick --expect-artifact-id ID

# Construction (explicit tool paths, an absent private run directory)
bin/esa nixos prepare PROFILE.json --flake-lock flake.lock --module consumer.nix --esa-revision REV --run-dir RUN
bin/esa nixos construct --run-dir RUN --nix NIX --tart TART \
  --build-store 'ssh-ng://USER@BUILDER?base64-ssh-public-host-key=PIN&ssh-key=/abs/private/key' \
  [--ssh-agent-socket SOCK] --output SEED_DIR --artifact-output ARTIFACT.json
bin/esa tahoe observe-base --digest sha256:… --oci-manifest MANIFEST.json --tart TART --ssh SSH \
  --bootstrap-credential-file CRED --run-dir RUN
bin/esa tahoe prepare PROFILE.json --flake-lock flake.lock --module consumer.nix --esa-revision REV --run-dir RUN
bin/esa tahoe construct --run-dir RUN --oci-manifest MANIFEST.json --tart TART --ssh SSH \
  --bootstrap-credential-file CRED --output SEED_DIR --artifact-output ARTIFACT.json
bin/esa record PROFILE.json --vm-dir VM_DIR --esa-revision REV --builder nix=V --builder tart=V \
  [--evidence FILE]… --output ARTIFACT.json

# Seed custody and instances
bin/esa seed export --artifact ARTIFACT.json --seed-dir SEED_DIR --tart TART --run-dir RUN --output SEED.tvm --full
bin/esa seed import --artifact ARTIFACT.json --tvm SEED.tvm --tart TART --run-dir RUN --output SEED_DIR
bin/esa instance create --artifact ARTIFACT.json --seed-dir SEED_DIR --tart TART --tart-home NEW_HOME \
  --name job-1 --expect-artifact-id ID [--inject-dir DIR]
bin/esa instance delete --tart TART --tart-home NEW_HOME --name job-1
```

| Command | Purpose |
|---|---|
| `validate` | Checks every v1 rule for a profile or artifact. Missing, unpinned or moving inputs, unsupported families, hash mismatches and secret-like material are refused. There is no override. |
| `identity` | Prints the profile content hash, or an artifact's recorded `artifact_id` after checking its derivation. Recording new artifacts is a later work unit. |
| `inspect` | Prints a deterministic summary, including `"admission": "not-determined-by-esa"`. |
| `plan` | Describes the build plan for a profile, naming the command for each step (`implemented: true`, `native_qualified: false`). It executes nothing. |
| `verify` | Checks an artifact against its profile and/or a local seed directory, including the Tart `config.json` and GPT header, and optionally against the `artifact_id` or `payload.digest` in a consumer's receipt (required with `--quick`). It needs no registry, no rebuild and no source evaluation. |
| `nixos`, `tahoe` | `prepare` verifies the lock and module and writes an isolated workspace without running tools. `construct` builds, seals and assembles a stopped seed and records its artifact; `tahoe observe-base` reports a base's `sw_vers` before a profile can pin it. |
| `record` | Hashes a built, stopped Tart VM directory and writes its artifact manifest. Validation entries come only from evidence bound to that disk. |
| `seed`, `instance` | Export and import verified seeds, and create or delete per-instance clones, each in an ESA-owned `TART_HOME` with no auto-prune and no overwrite. |

Exit status is `0` for success, `1` for a contract or construction refusal
(one `pointer: message` line per error on stderr, or the failed step and the
path of its private log) and `2` for a usage or I/O error. Construction never
reads credentials into its arguments or logs, never prunes, never overwrites
and never changes host services, profiles, TCC or Apple accounts.

The NixOS build store must pin its SSH host key (`PIN` is the base64 of one
public key line, with `+` written `%2B`) and name its authentication
explicitly: a private `ssh-key` file that ESA never reads, an agent socket,
or both. ESA sets `NIX_SSHOPTS` itself so that ssh is meant to ignore your
SSH configuration, default keys and any agent you did not select; that
Nix and OpenSSH honour this natively is not yet qualified. Tahoe
construction pulls a public base with the `--tart` you pass; a private `HOME`
does not stop Tart from consulting the Docker configuration or Keychain for
registry credentials, so qualify that executable's credential access
yourself. See the [backend work units](docs/architecture/backend-work-units.md).

You can also run the CLI as `PYTHONPATH=src python3 -m esa …`.

## Documentation

- [ADR 0001: ESA foundation](docs/adr/0001-esa-foundation.md)
- [Manifest v1 specification](docs/specs/manifest-v1.md): field semantics, units, identity, compatibility
- [Consumer contract](docs/specs/consumer-contract.md): ownership, admission receipts, per-job use
- [ADR 0002: backends](docs/adr/0002-esa-backends.md): construction, recording, custody, supply chain
- [Backend work units](docs/architecture/backend-work-units.md): what each unit implements and what remains native
- [Examples](examples/README.md): synthetic only

## Development

```zsh
python3 -m unittest discover -s tests
```

The tests use only the standard library, synthetic files and fake process
boundaries; they never run Nix, Tart, ssh or a VM. See
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
