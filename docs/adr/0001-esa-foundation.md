# ADR 0001: ESA foundation

## Status

Accepted (2026-10-21). This records operator decisions; it does not claim
working images or onboarded consumers.

## Context

Ephemeral Snowflake Artificer (ESA) builds reusable Tart VM seeds that
orchestrated consumers clone into disposable instances. It has two
consumers: a Linux CI runner path and a macOS GUI-automation test path.

ESA needs:

- one small shared contract before either backend exists;
- a legal suite fit for an image builder whose payloads carry their own licences;
- a public source repository that private consumer configuration never enters.

## Decision

1. **Identity.** The project is "Ephemeral Snowflake Artificer (ESA)", stewarded by Samuel Leighton Lair. The public source repository is `Knowledge-Forge-AI/ephemeral-snowflake-artificer`. First-party legal and contribution contact is `sam.lair@knowledge-forge.ai`.
2. **Families.** ESA supports exactly two image families, both arm64 on Tart:
   - `nixos-minimal`, a minimal installed NixOS built from pinned Nixpkgs;
   - `tahoe-vanilla`, the upstream macOS 26 vanilla Tart image pinned by digest, with pinned Nix and nix-darwin configuration applied in the guest.

   Comparing NixOS with other Linux distributions is not reopened.
3. **One contract.** One closed JSON schema, `esa-manifest-v1`, covers consumer profiles and built artifacts ([manifest v1](../specs/manifest-v1.md)).
   - Identity is content-derived (`payload.digest`, `artifact_id`), never a mutable name or tag, and is distinct from variable observations.
   - A digest is not admission; consumers own admission ([consumer contract](../specs/consumer-contract.md)).
4. **No JSON Schema file in v1.** The normative specification plus the standard-library validator are the only authorities. A parallel JSON Schema would create a second authority. Enforcing it would require a schema engine, a dependency or framework that v1 does not need. `schemas/` stays unused until a later version decides otherwise.
5. **Standard-library Python CLI.** `bin/esa` (validate, identity, inspect, plan, verify) has no runtime or test dependencies, no plugin system and no framework. It refuses by default and has no override flags.
6. **Separate construction.** Backend construction is separate work, documented as [work units](../architecture/backend-work-units.md). The foundation ships no backend scripts or success stubs, and `esa plan` marks every step unimplemented.
7. **Licensing.**
   - The donor legal suite is adapted to ESA: AGPL-3.0-or-later (verbatim GNU text) plus a separate commercial offer, and a CLA in which contributors keep copyright and grant the steward copyright and narrow patent licences.
   - Payloads keep their upstream terms. The commercial offer conveys no Apple rights.
   - Provenance is carried by artifact manifests, not by product-specific configuration files.

## Consequences

- Consumers can validate, inspect and verify artifacts using only the standard-library CLI, and can check a local seed per job without rebuilding it or contacting a registry.
- v1 may still change, together with its validator and tests, until the first consumer accepts an artifact. After that, any change means a new schema identifier.
- A backend unit that finds the Tart file set, the `config.json` fields or the macOS identifier set differ from v1's assumptions must amend v1 first.
- Private consumer pins, profiles, fixtures and admission rules never enter this repository. The public examples use synthetic values and generic names.
