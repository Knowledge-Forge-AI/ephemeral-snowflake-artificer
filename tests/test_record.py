"""ESA-REC-01: `esa record` over synthetic stopped VM directories."""

import fcntl
import json
import os
import tempfile
import unittest

import _util
import _fakes
from _util import run_cli

from esa import evidence

FIXTURE_REV = "f9821898394a60a9bb0d9f73eecb1be3452ff6ec"


class RecordTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.vm = _util.write_payload_dir(self.tmp)
        self.profile = _util.example_path("payload-profile")

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, *extra, vm=None, output=None):
        output = output or os.path.join(self.tmp, "artifact.json")
        return run_cli("record", self.profile, "--vm-dir", vm or self.vm, "--esa-revision", FIXTURE_REV,
                       "--builder", "nix=2.31.2", "--builder", "tart=2.30.0", "--output", output, *extra)

    def evidence(self, name, disk_sha, result="pass"):
        failures = [] if result == "pass" else ["machine_id"]
        doc = evidence.build("seed.nixos-seal", {"machine_id": "absent"}, failures,
                             {"disk_sha256": disk_sha}, "esa test")
        path = os.path.join(self.tmp, name)
        evidence.write(path, doc)
        return path

    def test_records_the_fixture_identity(self):
        code, out, err = self.record()
        self.assertEqual((code, err), (0, ""))
        summary = json.loads(out)
        self.assertEqual(summary["artifact_id"], _util.EXPECTED["payload-artifact"])
        self.assertEqual(summary["payload_digest"], _util.TEST_PAYLOAD_DIGEST)
        self.assertIs(summary["native_qualified"], False)
        artifact = os.path.join(self.tmp, "artifact.json")
        code, _, err = run_cli("verify", artifact, "--profile", self.profile, "--payload-dir", self.vm)
        self.assertEqual((code, err), (0, ""))
        with open(artifact) as handle:
            doc = json.load(handle)
        self.assertEqual(doc["validation"], [])
        self.assertEqual(len(doc["observations"]), 1)
        self.assertRegex(doc["observations"][0]["report_id"], r"^esa-record-[0-9]{8}t[0-9]{6}z$")
        self.assertEqual(set(doc["observations"][0]["metrics"]), {"host_allocated_bytes"})

    def test_existing_output_refused(self):
        existing = _util.write_bytes(self.tmp, "artifact.json", b"keep")
        code, _, err = self.record(output=existing)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        with open(existing, "rb") as handle:
            self.assertEqual(handle.read(), b"keep")

    def test_extra_file_refused(self):
        _util.write_bytes(self.vm, "state.vzvmsave", b"x")
        code, _, err = self.record()
        self.assertEqual(code, 1)
        self.assertIn("vm-dir/state.vzvmsave: unexpected entry", err)

    def test_symlinked_payload_refused(self):
        os.rename(os.path.join(self.vm, "nvram.bin"), os.path.join(self.tmp, "nvram.bin"))
        os.symlink(os.path.join(self.tmp, "nvram.bin"), os.path.join(self.vm, "nvram.bin"))
        code, _, err = self.record()
        self.assertEqual(code, 1)
        self.assertIn("nvram.bin: must be a regular file", err)

    def test_config_drift_refused(self):
        config = dict(_fakes.LINUX_CONFIG, cpuCount=2)
        vm = _fakes.vm_dir(self.tmp, "drift", config, 1 << 30)
        code, _, err = self.record(vm=vm)
        self.assertEqual(code, 1)
        self.assertIn("config.json/cpuCount: does not equal resources.cpu_count", err)

    def test_foreign_family_config_refused(self):
        vm = _fakes.vm_dir(self.tmp, "mac", dict(_fakes.DARWIN_CONFIG, memorySizeMin=1), 1 << 30)
        code, _, err = self.record(vm=vm)
        self.assertEqual(code, 1)
        self.assertIn("config.json/os", err)

    def test_disk_without_gpt_refused(self):
        vm = _fakes.vm_dir(self.tmp, "nogpt", _fakes.LINUX_CONFIG, 1 << 30, gpt=False)
        code, _, err = self.record(vm=vm)
        self.assertEqual(code, 1)
        self.assertIn("no GPT header", err)

    def test_decimal_disk_size_is_not_binary(self):
        vm = _fakes.vm_dir(self.tmp, "decimal", _fakes.LINUX_CONFIG, 1000 ** 3)
        code, _, err = self.record(vm=vm)
        self.assertEqual(code, 1)
        self.assertIn("logical size 1000000000 does not equal resources.disk (1073741824 bytes)", err)

    def test_running_vm_refused(self):
        fd = os.open(os.path.join(self.vm, "config.json"), os.O_RDWR)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX)
            code, _, err = self.record()
        finally:
            os.close(fd)
        self.assertEqual(code, 1)
        self.assertIn("appears to be running", err)

    def test_bound_evidence_recorded_with_its_own_result(self):
        disk = _fakes.sha256_file(os.path.join(self.vm, "disk.img"))
        passing = self.evidence("pass.json", disk)
        failing = self.evidence("fail.json", disk, result="fail")
        code, out, err = self.record("--evidence", passing, "--evidence", failing)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["validation"], [
            {"check": "seed.nixos-seal", "result": "pass"},
            {"check": "seed.nixos-seal", "result": "fail"},
        ])
        with open(os.path.join(self.tmp, "artifact.json")) as handle:
            doc = json.load(handle)
        self.assertEqual(doc["validation"][0]["evidence_sha256"], _fakes.sha256_file(passing))
        self.assertEqual(doc["artifact_id"], _util.EXPECTED["payload-artifact"])

    def test_evidence_bound_to_another_disk_refused(self):
        other = self.evidence("other.json", "sha256:" + "0" * 64)
        code, _, err = self.record("--evidence", other)
        self.assertEqual(code, 1)
        self.assertIn("bound to a different disk.img", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "artifact.json")))

    def test_unbound_or_forged_evidence_refused(self):
        path = _util.write_json(self.tmp, "forged.json", {"check": "seed.nixos-seal", "result": "pass"})
        code, _, err = self.record("--evidence", path)
        self.assertEqual(code, 1)
        self.assertIn("esa-evidence-v1", err)

    def test_evidence_with_refused_material_rejected(self):
        disk = _fakes.sha256_file(os.path.join(self.vm, "disk.img"))
        doc = evidence.build("seed.nixos-seal", {"note": _util.concat("gh", "p_", "A" * 20)}, [],
                             {"disk_sha256": disk}, "esa test")
        path = os.path.join(self.tmp, "secret.json")
        evidence.write(path, doc)
        code, _, err = self.record("--evidence", path)
        self.assertEqual(code, 1)
        self.assertIn("refused material: access token", err)
        self.assertNotIn("AAAA", err)

    def test_builders_must_be_exact_and_complete(self):
        output = os.path.join(self.tmp, "a.json")
        code, _, err = run_cli("record", self.profile, "--vm-dir", self.vm, "--esa-revision", FIXTURE_REV,
                               "--builder", "nix=2.31", "--builder", "tart=2.30.0", "--output", output)
        self.assertEqual(code, 1)
        self.assertIn("exact MAJOR.MINOR.PATCH", err)
        code, _, err = run_cli("record", self.profile, "--vm-dir", self.vm, "--esa-revision", FIXTURE_REV,
                               "--builder", "nix=2.31.2", "--builder", "packer=1.14.2", "--output", output)
        self.assertEqual(code, 1)
        self.assertIn("--builder", err)

    def test_extra_licenses_validated(self):
        rows = _util.write_bytes(self.tmp, "lic.json", json.dumps([
            {"component": "tart-guest-agent", "license": "LicenseRef-FSL-1.1-ALv2",
             "reference": "https://github.com/cirruslabs/tart-guest-agent"}]).encode("ascii"))
        code, _, err = self.record("--extra-licenses", rows)
        self.assertEqual((code, err), (0, ""))
        bad = _util.write_bytes(self.tmp, "bad.json", json.dumps([
            {"component": "x", "license": "MIT", "reference": "http://example.invalid/"}]).encode("ascii"))
        code, _, err = self.record("--extra-licenses", bad, output=os.path.join(self.tmp, "b.json"))
        self.assertEqual(code, 1)
        self.assertIn("/upstream_licenses/2/reference", err)


if __name__ == "__main__":
    unittest.main()
