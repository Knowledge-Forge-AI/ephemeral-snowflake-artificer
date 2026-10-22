"""Real CLI contract tests: the launcher runs in a subprocess on synthetic files."""

import json
import os
import subprocess
import tempfile
import unittest

import _util
from _util import run_cli


class CLITestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="esa-test-")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def mutated(self, name, mutate, filename="doc.json"):
        doc = _util.load(name)
        mutate(doc)
        return _util.write_json(self.tmp, filename, doc)

    def assertRefusedCLI(self, args, fragment):
        code, out, err = run_cli(*args)
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertIn(fragment, err)
        return err


class PositiveCLITest(CLITestCase):
    def test_validate_examples(self):
        for name in _util.EXAMPLES:
            with self.subTest(name=name):
                code, out, err = run_cli("validate", _util.example_path(name))
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.startswith("ok: "))

    def test_identity_examples(self):
        for name, expected in _util.EXPECTED.items():
            with self.subTest(name=name):
                code, out, err = run_cli("identity", _util.example_path(name))
                self.assertEqual((code, out, err), (0, expected + "\n", ""))

    def test_inspect_profile(self):
        code, out, _ = run_cli("inspect", _util.example_path("linux-profile"))
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["admission"], "not-determined-by-esa")
        self.assertEqual(summary["label"], {"value": "linux-ci", "mutable": True, "identity_bearing": False})
        self.assertEqual(summary["identity"], {"profile_content_sha256": _util.EXPECTED["linux-profile"]})
        self.assertEqual(summary["construction"]["status"], "unavailable")
        self.assertIs(summary["capabilities_verified_by_esa"], False)

    def test_inspect_artifact(self):
        code, out, _ = run_cli("inspect", _util.example_path("linux-artifact"))
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["identity"], {
            "artifact_id": _util.EXPECTED["linux-artifact"],
            "payload_digest": _util.LINUX_PAYLOAD_DIGEST,
        })
        self.assertEqual(summary["admission"], "not-determined-by-esa")
        self.assertIs(summary["validation"]["identity_bearing"], False)
        self.assertTrue(summary["validation"]["checks"])

    def test_verify_matching_profiles(self):
        for family in ("linux", "macos"):
            with self.subTest(family=family):
                code, out, err = run_cli(
                    "verify", _util.example_path(family + "-artifact"),
                    "--profile", _util.example_path(family + "-profile"))
                self.assertEqual((code, err), (0, ""))
                result = json.loads(out)
                self.assertIs(result["verified"], True)
                self.assertEqual(result["checks"], ["manifest", "profile"])
                self.assertEqual(result["disk_img"], "not-checked")

    def test_version(self):
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("esa "))

    def test_launcher_executes_directly(self):
        self.assertTrue(os.access(_util.BIN, os.X_OK), "bin/esa must be executable")
        proc = subprocess.run([_util.BIN, "validate", _util.example_path("linux-profile")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=_util.REPO)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertTrue(proc.stdout.startswith(b"ok: profile "))


class RefusalCLITest(CLITestCase):
    def test_malformed_json_is_refusal(self):
        path = _util.write_bytes(self.tmp, "dup.json", b'{"schema": "esa-manifest-v1", "schema": "x"}')
        self.assertRefusedCLI(["validate", path], "duplicate key")

    def test_float_resource_refused(self):
        with open(_util.example_path("linux-profile"), "rb") as handle:
            data = handle.read()
        self.assertIn(b'"cpu_count": 4,', data)
        path = _util.write_bytes(self.tmp, "float.json", data.replace(b'"cpu_count": 4,', b'"cpu_count": 4.0,'))
        self.assertRefusedCLI(["validate", path], "non-integer")

    def test_unsupported_family_refused(self):
        path = self.mutated("linux-profile", lambda d: d.__setitem__("family", "debian"))
        self.assertRefusedCLI(["validate", path], "/family: must be one of")

    def test_identity_refuses_invalid(self):
        path = self.mutated("linux-artifact", lambda d: d.__setitem__("esa_revision", "f" * 40))
        self.assertRefusedCLI(["identity", path], "/artifact_id: does not match")

    def test_inspect_refuses_secret_material(self):
        sample = _util.concat("gh", "o_", "abcdefghijklmnop")
        path = self.mutated("macos-artifact",
                            lambda d: d["validation"][0].__setitem__("check", sample))
        err = self.assertRefusedCLI(["inspect", path], "refused material: access token")
        self.assertNotIn(sample, err)

    def test_unsupported_schema_value_not_echoed(self):
        sample = _util.concat("gh", "p_", "abcdefghijklmnop")
        path = self.mutated("linux-profile", lambda d: d.__setitem__("schema", sample))
        err = self.assertRefusedCLI(["validate", path], "/schema: unsupported schema")
        self.assertNotIn(sample, err)

    def test_control_characters_in_pointer_escaped(self):
        path = self.mutated("linux-profile", lambda d: d["resources"].__setitem__("x\x1b[2J", 1))
        err = self.assertRefusedCLI(["validate", path], "/resources/x\\u001b[2J: unknown field")
        self.assertNotIn("\x1b", err)


class UsageCLITest(CLITestCase):
    def test_unknown_command(self):
        code, _, err = run_cli("build", _util.example_path("linux-profile"))
        self.assertEqual(code, 2)
        self.assertIn("usage error", err)

    def test_no_command(self):
        self.assertEqual(run_cli()[0], 2)

    def test_missing_file(self):
        code, out, err = run_cli("validate", os.path.join(self.tmp, "absent.json"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot read", err)

    def test_quick_requires_payload_dir(self):
        code, _, err = run_cli("verify", _util.example_path("linux-artifact"), "--quick")
        self.assertEqual(code, 2)
        self.assertIn("--quick requires --payload-dir", err)

    def test_quick_requires_receipt_identity(self):
        code, out, err = run_cli("verify", _util.example_path("linux-artifact"),
                                 "--payload-dir", self.tmp, "--quick")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--quick requires --expect-artifact-id or --expect-payload-digest", err)


class PlanCLITest(CLITestCase):
    def test_plan_both_profiles(self):
        for family, units in (("linux", {"ESA-NIX-01", "ESA-NIX-02", "ESA-REC-01"}),
                              ("macos", {"ESA-MAC-01", "ESA-MAC-02", "ESA-REC-01"})):
            with self.subTest(family=family):
                code, out, err = run_cli("plan", _util.example_path(family + "-profile"),
                                         "--esa-revision", _util.REV)
                self.assertEqual((code, err), (0, ""))
                plan = json.loads(out)
                self.assertEqual(plan["plan_schema"], "esa-plan-v1")
                self.assertIs(plan["executes"], False)
                self.assertEqual(plan["construction"]["status"], "unavailable")
                self.assertEqual(plan["profile"]["content_sha256"], _util.EXPECTED[family + "-profile"])
                self.assertTrue(plan["steps"])
                self.assertTrue(all(step["implemented"] is False for step in plan["steps"]))
                self.assertEqual({step["work_unit"] for step in plan["steps"]}, units)
                self.assertNotIn("success", out.lower())

    def test_plan_resources_in_bytes(self):
        _, out, _ = run_cli("plan", _util.example_path("linux-profile"), "--esa-revision", _util.REV)
        self.assertEqual(json.loads(out)["resources"],
                         {"cpu_count": 4, "memory_bytes": 8589934592, "disk_bytes": 68719476736})

    def test_plan_consumer_supplied_files(self):
        _, out, _ = run_cli("plan", _util.example_path("linux-profile"), "--esa-revision", _util.REV)
        roles = [entry["role"] for entry in json.loads(out)["consumer_supplied_files"]]
        self.assertEqual(roles, ["flake-lock", "nixos-module"])

    def test_plan_deterministic(self):
        args = ("plan", _util.example_path("macos-profile"), "--esa-revision", _util.REV)
        self.assertEqual(run_cli(*args), run_cli(*args))

    def test_plan_requires_revision(self):
        code, _, err = run_cli("plan", _util.example_path("linux-profile"))
        self.assertEqual(code, 2)
        self.assertIn("--esa-revision", err)

    def test_plan_refuses_moving_revision(self):
        for rev in ("main", "HEAD", "0123abc", _util.REV.upper()):
            with self.subTest(rev=rev):
                self.assertRefusedCLI(
                    ["plan", _util.example_path("linux-profile"), "--esa-revision", rev],
                    "--esa-revision: must be a 40-character")

    def test_plan_refuses_artifact(self):
        self.assertRefusedCLI(
            ["plan", _util.example_path("linux-artifact"), "--esa-revision", _util.REV],
            "/kind: this command requires a profile manifest")

    def test_plan_refuses_unpinned_profile(self):
        path = self.mutated("linux-profile",
                            lambda d: d["inputs"]["nixpkgs"].__setitem__("rev", "nixos-unstable"))
        self.assertRefusedCLI(["plan", path, "--esa-revision", _util.REV], "moving or ambiguous")

    def test_plan_refuses_unqualified_seed(self):
        path = self.mutated("linux-profile",
                            lambda d: d["seed"].__setitem__("consumer_credentials_absent", False))
        self.assertRefusedCLI(["plan", path, "--esa-revision", _util.REV], "must be JSON true")


class VerifyProfileCLITest(CLITestCase):
    def test_profile_hash_mismatch(self):
        profile = self.mutated("linux-profile", lambda d: d["capabilities"]["guest"].pop(), "p.json")
        self.assertRefusedCLI(["verify", _util.example_path("linux-artifact"), "--profile", profile],
                              "/profile/content_sha256")

    def test_profile_revision_mismatch(self):
        profile = self.mutated("linux-profile", lambda d: d.__setitem__("revision", 2), "p.json")
        err = self.assertRefusedCLI(["verify", _util.example_path("linux-artifact"), "--profile", profile],
                                    "/profile/revision")
        self.assertIn("/profile/content_sha256", err)

    def test_renamed_profile_still_matches(self):
        profile = self.mutated("linux-profile", lambda d: d.__setitem__("label", "renamed"), "p.json")
        code, _, err = run_cli("verify", _util.example_path("linux-artifact"), "--profile", profile)
        self.assertEqual((code, err), (0, ""))

    def test_wrong_family_profile(self):
        err = self.assertRefusedCLI(
            ["verify", _util.example_path("linux-artifact"), "--profile", _util.example_path("macos-profile")],
            "/family: differs from the supplied profile")
        self.assertIn("/inputs: differs", err)

    def test_profile_argument_must_be_profile(self):
        self.assertRefusedCLI(
            ["verify", _util.example_path("linux-artifact"), "--profile", _util.example_path("linux-artifact")],
            "requires a profile manifest")


class VerifyPayloadCLITest(CLITestCase):
    ARTIFACT = _util.example_path("payload-artifact")

    def setUp(self):
        super().setUp()
        self.payload = _util.write_payload_dir(self.tmp)

    def verify(self, *extra):
        return run_cli("verify", self.ARTIFACT, "--payload-dir", self.payload,
                       "--expect-artifact-id", _util.EXPECTED["payload-artifact"], *extra)

    def test_full_verify_passes(self):
        code, out, err = run_cli("verify", self.ARTIFACT, "--payload-dir", self.payload,
                                 "--profile", _util.example_path("payload-profile"))
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["checks"], ["manifest", "profile", "payload-full"])
        self.assertEqual(result["disk_img"], "sha256")
        self.assertEqual(result["artifact_id"], _util.EXPECTED["payload-artifact"])

    def test_quick_verify_passes(self):
        code, out, err = self.verify("--quick")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["disk_img"], "size-only")
        self.assertEqual(result["checks"], ["manifest", "expected-artifact-id", "payload-quick"])

    def test_quick_verify_by_payload_digest(self):
        code, out, err = run_cli("verify", self.ARTIFACT, "--payload-dir", self.payload, "--quick",
                                 "--expect-payload-digest", _util.TEST_PAYLOAD_DIGEST)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["checks"], ["manifest", "expected-payload-digest", "payload-quick"])

    def test_receipt_artifact_id_mismatch(self):
        # A different, self-consistent manifest is not the one the receipt admitted.
        code, out, err = run_cli("verify", self.ARTIFACT, "--payload-dir", self.payload, "--quick",
                                 "--expect-artifact-id", _util.EXPECTED["linux-artifact"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("/artifact_id: does not match --expect-artifact-id", err)

    def test_receipt_payload_digest_mismatch(self):
        code, out, err = run_cli("verify", self.ARTIFACT, "--payload-dir", self.payload, "--quick",
                                 "--expect-payload-digest", _util.LINUX_PAYLOAD_DIGEST)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("/payload/digest: does not match --expect-payload-digest", err)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_unreadable_payload_file_is_io_error(self):
        config = os.path.join(self.payload, "config.json")
        os.chmod(config, 0)
        try:
            code, out, err = self.verify("--quick")
        finally:
            os.chmod(config, 0o600)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("config.json: cannot read", err)
        self.assertNotIn("Traceback", err)

    def test_flipped_disk_byte_fails_full_only(self):
        with open(os.path.join(self.payload, "disk.img"), "r+b") as handle:
            handle.seek(4096)
            handle.write(b"\x01")
        self.assertEqual(self.verify("--quick")[0], 0)
        code, _, err = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("disk.img: sha256 does not match", err)

    def test_flipped_config_byte_fails_quick(self):
        _util.write_bytes(self.payload, "config.json", _util.PAYLOAD_CONFIG.replace(b"test", b"tesT"))
        code, _, err = self.verify("--quick")
        self.assertEqual(code, 1)
        self.assertIn("config.json: sha256 does not match", err)

    def test_size_mismatch(self):
        _util.write_bytes(self.payload, "nvram.bin", _util.PAYLOAD_NVRAM + b"\0")
        code, _, err = self.verify("--quick")
        self.assertEqual(code, 1)
        self.assertIn("nvram.bin: size", err)

    def test_extra_file(self):
        _util.write_bytes(self.payload, ".DS_Store", b"x")
        code, _, err = self.verify("--quick")
        self.assertEqual(code, 1)
        self.assertIn(".DS_Store: unexpected entry", err)

    def test_missing_file(self):
        os.remove(os.path.join(self.payload, "nvram.bin"))
        code, _, err = self.verify("--quick")
        self.assertEqual(code, 1)
        self.assertIn("nvram.bin: missing payload file", err)

    def test_symlinked_file(self):
        target = _util.write_bytes(self.tmp, "real-config.json", _util.PAYLOAD_CONFIG)
        os.remove(os.path.join(self.payload, "config.json"))
        os.symlink(target, os.path.join(self.payload, "config.json"))
        code, _, err = self.verify("--quick")
        self.assertEqual(code, 1)
        self.assertIn("config.json: must be a regular file", err)

    def test_symlinked_directory(self):
        link = os.path.join(self.tmp, "link.tvm")
        os.symlink(self.payload, link)
        code, _, err = run_cli("verify", self.ARTIFACT, "--payload-dir", link, "--quick",
                               "--expect-payload-digest", _util.TEST_PAYLOAD_DIGEST)
        self.assertEqual(code, 1)
        self.assertIn("must be a real directory", err)

    def test_missing_directory_is_io_error(self):
        code, _, err = run_cli("verify", self.ARTIFACT, "--payload-dir", os.path.join(self.tmp, "absent"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)


if __name__ == "__main__":
    unittest.main()
