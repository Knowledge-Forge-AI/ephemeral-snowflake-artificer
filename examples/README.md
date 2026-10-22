# Examples

**Every value in these files is synthetic.**

- Revisions, `nar_hash` values, digests, sizes, builder versions, the Nix installer version and checksum, macOS versions and builds, and observations were derived from fixed placeholder strings. They are not real pins and refer to no existing payload, commit or image.
- Do not copy them into a real profile.

| File | Kind | Family |
|---|---|---|
| `profiles/linux-ci.profile.json` | profile | `nixos-minimal`: generic Linux CI runner consumer |
| `profiles/macos-gui.profile.json` | profile | `tahoe-vanilla`: generic macOS GUI-automation consumer |
| `artifacts/linux-ci.artifact.json` | artifact | built from `linux-ci`, revision 1 |
| `artifacts/macos-gui.artifact.json` | artifact | built from `macos-gui`, revision 1 |

The names `linux-ci` and `macos-gui` are generic. Real consumer profiles,
pins and customization modules live in the consumers' own repositories.

Each example passes `bin/esa validate`, and each artifact passes `bin/esa verify
ARTIFACT --profile PROFILE` against its profile. `bin/esa plan` accepts both
profiles. Their identities were computed independently of `src/esa`, and
`tests/_util.py` records them as literal values.
