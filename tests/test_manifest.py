import unittest

import _util
from esa import manifest


class ManifestCase(unittest.TestCase):
    def assertValid(self, doc):
        self.assertEqual(manifest.validate(doc), [])

    def assertRefused(self, doc, pointer, fragment=""):
        errors = manifest.validate(doc)
        matches = [m for p, m in errors if p == pointer and fragment in m]
        self.assertTrue(matches, "expected %s %r in %r" % (pointer, fragment, errors))


class PositiveTest(ManifestCase):
    def test_examples_and_fixtures_valid(self):
        for name in _util.EXAMPLES:
            with self.subTest(name=name):
                self.assertValid(_util.load(name))

    def test_passwords_capability_and_seed_keys_not_mistaken_for_secrets(self):
        # The material scan must not trip on schema keys such as
        # registration_material_absent or on a Passwords capability token.
        doc = _util.load("macos-profile")
        self.assertIn("macos.app.passwords", doc["capabilities"]["guest"])
        self.assertIn("registration_material_absent", doc["seed"])
        self.assertValid(doc)

    def test_customization_none_with_null(self):
        doc = _util.load("linux-profile")
        doc["customization"] = {"kind": "none", "sha256": None}
        self.assertValid(doc)

    def test_tahoe_without_inherited_default_account(self):
        doc = _util.load("macos-profile")
        doc["seed"]["inherited_default_account"] = False
        self.assertValid(doc)


class SchemaAndTypeTest(ManifestCase):
    def test_missing_schema(self):
        doc = _util.load("linux-profile")
        del doc["schema"]
        self.assertRefused(doc, "/schema", "missing")

    def test_other_schema_version_not_reinterpreted(self):
        doc = _util.load("linux-profile")
        doc["schema"] = "esa-manifest-v2"
        self.assertEqual(manifest.validate(doc),
                         [("/schema", "unsupported schema; only 'esa-manifest-v1' is accepted")])

    def test_wrong_kind(self):
        doc = _util.load("linux-profile")
        doc["kind"] = "image"
        self.assertRefused(doc, "/kind", "must be one of")

    def test_unknown_top_level_and_nested_keys(self):
        doc = _util.load("linux-profile")
        doc["token"] = "x"
        self.assertRefused(doc, "/token", "unknown field")
        doc = _util.load("linux-profile")
        doc["resources"]["gpu"] = 1
        self.assertRefused(doc, "/resources/gpu", "unknown field")

    def test_profile_field_on_artifact(self):
        doc = _util.load("linux-artifact")
        doc["revision"] = 1
        self.assertRefused(doc, "/revision", "unknown field")

    def test_cpu_count_type_ambiguity(self):
        for bad in ("4", True, False, None, [4]):
            with self.subTest(value=bad):
                doc = _util.load("linux-profile")
                doc["resources"]["cpu_count"] = bad
                self.assertRefused(doc, "/resources/cpu_count", "integer")

    def test_cpu_count_range(self):
        for bad in (0, 65, -1):
            doc = _util.load("linux-profile")
            doc["resources"]["cpu_count"] = bad
            self.assertRefused(doc, "/resources/cpu_count", "between")

    def test_seed_boolean_ambiguity(self):
        for bad in ("true", 1, False, None):
            with self.subTest(value=bad):
                doc = _util.load("linux-profile")
                doc["seed"]["registration_material_absent"] = bad
                self.assertRefused(doc, "/seed/registration_material_absent", "JSON true")
        doc = _util.load("linux-profile")
        del doc["seed"]["ssh_host_keys_absent"]
        self.assertRefused(doc, "/seed/ssh_host_keys_absent", "missing")

    def test_inherited_default_account_strict_boolean(self):
        for bad in ("false", 0, None):
            doc = _util.load("macos-profile")
            doc["seed"]["inherited_default_account"] = bad
            self.assertRefused(doc, "/seed/inherited_default_account", "boolean")
        doc = _util.load("linux-profile")
        doc["seed"]["inherited_default_account"] = True
        self.assertRefused(doc, "/seed/inherited_default_account", "must be false")

    def test_null_required_field(self):
        doc = _util.load("linux-profile")
        doc["inputs"] = None
        self.assertRefused(doc, "/inputs", "object")

    def test_revision_boolean(self):
        doc = _util.load("linux-profile")
        doc["revision"] = True
        self.assertRefused(doc, "/revision", "integer")

    def test_non_ascii_string(self):
        doc = _util.load("linux-profile")
        doc["label"] = "linux-c" + chr(0xEF)
        self.assertRefused(doc, "/label", "printable ASCII")

    def test_capabilities_sorted_unique(self):
        doc = _util.load("linux-profile")
        doc["capabilities"]["guest"] = ["tool.jq", "tool.git"]
        self.assertRefused(doc, "/capabilities/guest", "sorted")
        doc["capabilities"]["guest"] = ["tool.git", "tool.git"]
        self.assertRefused(doc, "/capabilities/guest", "sorted")


class SupportTest(ManifestCase):
    def test_unsupported_arch(self):
        for bad in ("x86_64", "aarch64", "ARM64"):
            doc = _util.load("linux-profile")
            doc["arch"] = bad
            self.assertRefused(doc, "/arch", "arm64")

    def test_unsupported_family(self):
        doc = _util.load("linux-profile")
        doc["family"] = "debian"
        self.assertRefused(doc, "/family", "unsupported")

    def test_unsupported_platform(self):
        doc = _util.load("linux-profile")
        doc["platform"] = "vmware"
        self.assertRefused(doc, "/platform", "unsupported")

    def test_unsupported_tahoe_base_source(self):
        for repo in ("ghcr.io/cirruslabs/macos-tahoe-vanilla:latest",
                     "ghcr.io/example/macos-tahoe-vanilla",
                     "ghcr.io/cirruslabs/macos-tahoe-xcode"):
            doc = _util.load("macos-profile")
            doc["inputs"]["macos"]["base"]["repository"] = repo
            self.assertRefused(doc, "/inputs/macos/base/repository", "moving")

    def test_tahoe_base_kind(self):
        doc = _util.load("macos-profile")
        doc["inputs"]["macos"]["base"]["kind"] = "ipsw"
        self.assertRefused(doc, "/inputs/macos/base/kind")


class ConditionalTest(ManifestCase):
    def test_nixos_without_nixpkgs(self):
        doc = _util.load("linux-profile")
        del doc["inputs"]["nixpkgs"]
        self.assertRefused(doc, "/inputs/nixpkgs", "missing")

    def test_nixos_with_macos_block(self):
        doc = _util.load("linux-profile")
        doc["inputs"]["macos"] = _util.load("macos-profile")["inputs"]["macos"]
        self.assertRefused(doc, "/inputs/macos", "not permitted for family nixos-minimal")

    def test_nixos_with_nix_darwin(self):
        doc = _util.load("linux-profile")
        doc["inputs"]["nix_darwin"] = _util.load("macos-profile")["inputs"]["nix_darwin"]
        self.assertRefused(doc, "/inputs/nix_darwin", "not permitted")

    def test_tahoe_missing_os_fields(self):
        for key in ("build", "product_version", "base", "recipe"):
            with self.subTest(key=key):
                doc = _util.load("macos-profile")
                del doc["inputs"]["macos"][key]
                self.assertRefused(doc, "/inputs/macos/" + key, "missing")

    def test_tahoe_missing_nix_darwin(self):
        doc = _util.load("macos-profile")
        del doc["inputs"]["nix_darwin"]
        self.assertRefused(doc, "/inputs/nix_darwin", "missing")

    def test_tahoe_with_nixos_release(self):
        doc = _util.load("macos-profile")
        doc["inputs"]["nixos_release"] = "26.05"
        self.assertRefused(doc, "/inputs/nixos_release", "not permitted for family tahoe-vanilla")

    def test_customization_kind_family_mismatch(self):
        doc = _util.load("linux-profile")
        doc["customization"]["kind"] = "nix-darwin-module"
        self.assertRefused(doc, "/customization/kind", "nixos-minimal")
        doc = _util.load("macos-profile")
        doc["customization"]["kind"] = "nixos-module"
        self.assertRefused(doc, "/customization/kind", "tahoe-vanilla")

    def test_customization_none_with_digest(self):
        doc = _util.load("linux-profile")
        doc["customization"]["kind"] = "none"
        self.assertRefused(doc, "/customization/sha256", "null")

    def test_customization_module_without_digest(self):
        doc = _util.load("linux-profile")
        doc["customization"]["sha256"] = None
        self.assertRefused(doc, "/customization/sha256")

    def test_regenerate_set_per_family(self):
        doc = _util.load("linux-profile")
        doc["seed"]["regenerate_on_clone"] = ["mac-address", "ssh-host-keys"]
        self.assertRefused(doc, "/seed/regenerate_on_clone", "machine-id")
        doc = _util.load("linux-profile")
        doc["seed"]["regenerate_on_clone"].append("platform-machine-identifier")
        doc["seed"]["regenerate_on_clone"].sort()
        self.assertRefused(doc, "/seed/regenerate_on_clone/2", "must be one of")


class PinTest(ManifestCase):
    def test_moving_or_ambiguous_revisions(self):
        for bad in ("nixos-unstable", "main", "abc1234", "0123456789ABCDEF0123456789ABCDEF01234567"):
            with self.subTest(rev=bad):
                doc = _util.load("linux-profile")
                doc["inputs"]["nixpkgs"]["rev"] = bad
                self.assertRefused(doc, "/inputs/nixpkgs/rev", "moving or ambiguous")

    def test_recipe_http_and_moving_rev(self):
        doc = _util.load("macos-profile")
        doc["inputs"]["macos"]["recipe"]["repository"] = "http://github.com/cirruslabs/macos-image-templates"
        self.assertRefused(doc, "/inputs/macos/recipe/repository")
        doc = _util.load("macos-profile")
        doc["inputs"]["macos"]["recipe"]["rev"] = "main"
        self.assertRefused(doc, "/inputs/macos/recipe/rev", "moving")

    def test_base_without_digest(self):
        doc = _util.load("macos-profile")
        del doc["inputs"]["macos"]["base"]["digest"]
        self.assertRefused(doc, "/inputs/macos/base/digest", "missing")
        doc = _util.load("macos-profile")
        doc["inputs"]["macos"]["base"]["digest"] = "latest"
        self.assertRefused(doc, "/inputs/macos/base/digest", "sha256:")

    def test_nar_hash_and_lock_format(self):
        doc = _util.load("linux-profile")
        doc["inputs"]["nixpkgs"]["nar_hash"] = "sha256:" + "0" * 64
        self.assertRefused(doc, "/inputs/nixpkgs/nar_hash", "SRI")
        doc = _util.load("linux-profile")
        doc["inputs"]["flake_lock_sha256"] = "0" * 64
        self.assertRefused(doc, "/inputs/flake_lock_sha256", "sha256:")

    def test_builder_version_range(self):
        for bad in (">=1.2", "2.30", "2.30.0-rc1", "latest"):
            doc = _util.load("linux-artifact")
            doc["builders"][1]["version"] = bad
            self.assertRefused(doc, "/builders/1/version", "exact")

    def test_builders_required_and_sorted(self):
        doc = _util.load("linux-artifact")
        doc["builders"] = [doc["builders"][1]]
        self.assertRefused(doc, "/builders", "must include nix")
        doc = _util.load("linux-artifact")
        doc["builders"].reverse()
        self.assertRefused(doc, "/builders", "sorted")

    def test_macos_version_and_build(self):
        doc = _util.load("macos-profile")
        doc["inputs"]["macos"]["product_version"] = "15.6"
        self.assertRefused(doc, "/inputs/macos/product_version", "macOS 26")
        doc = _util.load("macos-profile")
        doc["inputs"]["macos"]["build"] = "24G84"
        self.assertRefused(doc, "/inputs/macos/build", "macOS 26")


class TahoeInputTest(ManifestCase):
    def test_nix_installer_required_and_exact(self):
        doc = _util.load("macos-profile")
        del doc["inputs"]["nix_installer"]
        self.assertRefused(doc, "/inputs/nix_installer", "required")
        doc = _util.load("macos-profile")
        doc["inputs"]["nix_installer"]["version"] = "2.31"
        self.assertRefused(doc, "/inputs/nix_installer/version", "MAJOR.MINOR.PATCH")
        doc = _util.load("macos-profile")
        doc["inputs"]["nix_installer"]["url"] = "https://releases.nixos.org/x"
        self.assertRefused(doc, "/inputs/nix_installer/url", "unknown")

    def test_nix_installer_foreign_to_nixos(self):
        doc = _util.load("linux-profile")
        doc["inputs"]["nix_installer"] = {"version": "2.31.5", "sha256": "sha256:" + "0" * 64}
        self.assertRefused(doc, "/inputs/nix_installer", "not permitted for family nixos-minimal")

    def test_installer_url_is_derived(self):
        self.assertEqual(manifest.nix_installer_url("2.31.5"),
                         "https://releases.nixos.org/nix/nix-2.31.5/nix-2.31.5-aarch64-darwin.tar.xz")

    def test_packer_is_not_a_builder(self):
        doc = _util.load("macos-artifact")
        doc["builders"][1] = {"name": "packer", "version": "1.14.2"}
        self.assertRefused(doc, "/builders/1/name")


class UnitTest(ManifestCase):
    def test_malformed_memory(self):
        for bad in ("8GB", "8 GiB", "8gib", "8.5GiB", "0GiB", "-1GiB", "8", 8, "256MiB", "1024GiB"):
            with self.subTest(value=bad):
                doc = _util.load("linux-profile")
                doc["resources"]["memory"] = bad
                self.assertRefused(doc, "/resources/memory")

    def test_malformed_disk(self):
        for bad in ("64GB", "64 GiB", "64gib", "0GiB", "64MiB", "1.5GiB", "4097GiB", 64):
            with self.subTest(value=bad):
                doc = _util.load("linux-profile")
                doc["resources"]["disk"] = bad
                self.assertRefused(doc, "/resources/disk")

    def test_disk_units_are_exact_bytes(self):
        self.assertEqual(manifest.parse_disk("64GiB"), 68719476736)
        self.assertEqual(manifest.parse_disk("50GB"), 50000000000)
        self.assertEqual(manifest.disk_in_units("80GB", 1000 ** 3), 80)
        with self.assertRaises(ValueError):
            manifest.disk_in_units("80GB", 1 << 20)

    def test_nixos_disk_must_be_whole_mib(self):
        doc = _util.load("linux-profile")
        doc["resources"]["disk"] = "64GB"
        self.assertRefused(doc, "/resources/disk", "not exactly representable in MiB")
        doc["resources"]["disk"] = "2048GB"
        self.assertValid(doc)

    def test_tahoe_disk_must_be_whole_decimal_gb(self):
        doc = _util.load("macos-profile")
        doc["resources"]["disk"] = "50GiB"
        self.assertRefused(doc, "/resources/disk", "decimal GB")
        doc["resources"]["disk"] = "50GB"
        self.assertValid(doc)
        doc["resources"]["disk"] = "4097GB"
        self.assertRefused(doc, "/resources/disk", "at most 4096")

    def test_storage_metric_types(self):
        for bad, fragment in ((-1, "between"), ("1024", "integer"), (True, "integer"), (None, "integer")):
            with self.subTest(value=bad):
                doc = _util.load("linux-artifact")
                doc["storage"]["payload_bytes"] = bad
                self.assertRefused(doc, "/storage/payload_bytes", fragment)

    def test_storage_inconsistent(self):
        doc = _util.load("linux-artifact")
        doc["storage"]["payload_bytes"] += 1
        self.assertRefused(doc, "/storage/payload_bytes", "sum")
        doc = _util.load("linux-artifact")
        doc["storage"]["disk_logical_bytes"] -= 1
        self.assertRefused(doc, "/storage/disk_logical_bytes", "resources.disk")

    def test_observation_metrics(self):
        doc = _util.load("linux-artifact")
        doc["observations"][0]["metrics"]["host_allocated_gib"] = 9
        self.assertRefused(doc, "/observations/0/metrics/host_allocated_gib", "unknown")
        doc = _util.load("linux-artifact")
        doc["observations"][0]["metrics"] = {}
        self.assertRefused(doc, "/observations/0/metrics", "at least one")
        doc = _util.load("linux-artifact")
        doc["observations"][0]["observed_at"] = "2026-02-30T00:00:00Z"
        self.assertRefused(doc, "/observations/0/observed_at", "calendar")


class PayloadTest(ManifestCase):
    def test_digest_must_match_files(self):
        doc = _util.load("linux-artifact")
        doc["payload"]["digest"] = "sha256:" + "0" * 64
        self.assertRefused(doc, "/payload/digest", "canonical digest")

    def test_file_set_closed(self):
        doc = _util.load("linux-artifact")
        del doc["payload"]["files"][2]
        self.assertRefused(doc, "/payload/files", "exactly")
        doc = _util.load("linux-artifact")
        doc["payload"]["files"][0]["path"] = "../config.json"
        self.assertRefused(doc, "/payload/files/0/path")

    def test_artifact_id_mismatch(self):
        doc = _util.load("linux-artifact")
        doc["artifact_id"] = "esa-artifact-v1:sha256:" + "0" * 64
        self.assertEqual(manifest.validate(doc),
                         [("/artifact_id", "does not match the identity derived from this manifest")])

    def test_mutated_identity_field_detected(self):
        doc = _util.load("linux-artifact")
        doc["esa_revision"] = "f" * 40
        self.assertRefused(doc, "/artifact_id", "does not match")


class MaterialTest(ManifestCase):
    SAMPLES = {
        "private key block": _util.concat("-----", "BEGIN OPENSSH ", "PRIV", "ATE KEY-----"),
        "SSH public key": _util.concat("ssh-", "ed25519 ", "AAAA", "C3NzaC1lZDI1NTE5"),
        "access token": _util.concat("gh", "p_", "abcdefghijklmnop"),
        "UUID-shaped identifier": _util.concat("123e4567-e89b", "-12d3-a456-426614174000"),
        "runner registration file": _util.concat("/var/lib/runner/", ".runner"),
        "host home path": _util.concat("/Us", "ers/someone/vm"),
        "email address": _util.concat("someone", "@", "example.invalid"),
        "credential assignment": _util.concat("tok", "en=abc"),
    }

    def test_material_refused_in_free_strings(self):
        for category, sample in self.SAMPLES.items():
            with self.subTest(category=category):
                doc = _util.load("linux-artifact")
                doc["upstream_licenses"][0]["reference"] = sample
                errors = manifest.validate(doc)
                self.assertIn(("/upstream_licenses/0/reference", "refused material: " + category), errors)

    def test_matched_text_not_echoed(self):
        doc = _util.load("linux-artifact")
        secret = self.SAMPLES["access token"]
        doc["validation"][0]["check"] = secret
        rendered = repr(manifest.validate(doc))
        self.assertNotIn(secret, rendered)

    def test_label_with_uuid(self):
        doc = _util.load("linux-profile")
        doc["label"] = self.SAMPLES["UUID-shaped identifier"]
        self.assertRefused(doc, "/label", "refused material")


if __name__ == "__main__":
    unittest.main()
