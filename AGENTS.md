# Instructions for contributors and coding agents

These rules apply to every change in this repository.

## Scope and authority

- **Normative authority.** [docs/specs/manifest-v1.md](docs/specs/manifest-v1.md) is normative, and `src/esa/manifest.py` enforces it. Change both, plus their tests, in the same change. If you find them disagreeing, report it rather than picking one.
- **v1 is closed.** A new field, value or meaning is a schema change. Before any consumer accepts a v1 artifact, amend v1 in place. After that, add a new schema identifier.
- **Separate work units.** Backend construction belongs to the work units in [docs/architecture/backend-work-units.md](docs/architecture/backend-work-units.md). Never add a backend script that pretends to succeed, a stub that reports success, or a step marked implemented that is not. "Implemented" is not "native-qualified": keep those statements separate in code output and docs.
- **Construction safety.** Construction code takes every tool path, build store and credential file from command arguments. It runs argv lists with bounded timeouts and an explicit environment, uses only ESA-owned destinations that were absent, and never prunes, overwrites, uses the default `TART_HOME`, runs host `sudo`, or changes host Nix, services, profiles, TCC or Apple accounts. Credentials travel only as private file paths (`bin/esa-askpass`); never put a credential value in argv, environments, logs, manifests or seeds.

## Dependencies

- The CLI and tests use the Python standard library only, with a Python 3.9 floor: no `match`, no `tomllib`, no third-party imports. Do not add packaging metadata, runtime dependencies, frameworks or plugin systems without an accepted ADR.

## Tests

```zsh
python3 -m unittest discover -s tests
```

- Tests run the real CLI (`bin/esa`) and modules against synthetic files in a temporary directory.
- Write expected values literally. Do not recompute them with the implementation under test.
- Tests must never build images, evaluate or fetch Nix, run Tart or Packer, start VMs or containers, or probe the host. Construction tests run the real CLI against the fake `tart`, `nix` and `ssh` scripts in `tests/fakes/`, which stand in for those process boundaries. They prove dispatch, refusal and cleanup, not native behaviour.
- Nix evaluation of `recipes/` against real pins is a separate, explicitly selected check. Record it separately from the unit tests, and never as a build or qualification.
- Never capture a credential value in a test. Fakes compare a hash of what `bin/esa-askpass` returns.
- When an example changes, regenerate its identities with code independent of `src/esa`, and update the literal values in `tests/_util.py`.

## Content that must never enter the repository

- Credentials, tokens, registration files, runner identifiers, private keys, or SSH keys (including bootstrap public keys).
- Personal accounts, email addresses other than the project contact, or host-specific paths.
- Private consumer repository names, profiles, pins, fixtures or admission rules. Public examples use the generic names `linux-ci` and `macos-gui` and synthetic values.
- VM images, disks or other build outputs (see `.gitignore`).

Test code that needs refused-material samples assembles them from fragments, so the repository holds no secret-shaped literals.

## Legal

- `LICENSE` is the verbatim GNU AGPL text; never edit it.
- Pull requests must include: `I agree to the Ephemeral Snowflake Artificer contribution terms in CLA.md.`
- Preserve third-party notices. Before deriving from an upstream template, check its licence and add any required attribution to `NOTICE`.
