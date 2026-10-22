"""Seed export/import and instance create/delete over a fake Tart."""

import json
import os
import tempfile
import unittest

import _util
import _fakes
from _util import run_cli


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        os.mkdir(os.path.join(self.tmp, "tools"))
        self.tools_dir = os.path.join(self.tmp, "tools")
        self.tools = _fakes.install(self.tools_dir, {})
        self.seed = _util.write_payload_dir(self.tmp)
        self.artifact = _util.example_path("payload-artifact")
        self.artifact_id = _util.EXPECTED["payload-artifact"]

    def tearDown(self):
        self._tmp.cleanup()

    def control(self, **changes):
        control = _fakes.read_control(self.tools_dir)
        control.update(changes)
        _fakes.write_control(self.tools_dir, control)

    def config(self, directory):
        with open(os.path.join(directory, "config.json")) as handle:
            return json.load(handle)


class InstanceTest(LifecycleFixture):
    def create(self, *extra, seed=None, artifact=None, home=None, check=None):
        home = home or os.path.join(self.tmp, "inst-home")
        check = check or ["--expect-artifact-id", self.artifact_id]
        return run_cli("instance", "create", "--artifact", artifact or self.artifact, "--seed-dir", seed or self.seed,
                       "--tart", self.tools["tart"], "--tart-home", home, "--name", "job-1", *(list(check) + list(extra)))

    def test_create_transforms_only_declared_identifiers_and_delete(self):
        code, out, err = self.create()
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        home = os.path.join(self.tmp, "inst-home")
        self.assertEqual(result["host_transformations"], ["mac-address"])
        self.assertEqual(result["guest_first_boot"], ["machine-id", "random-seed", "ssh-host-keys"])
        self.assertIs(result["guest_first_boot_verified"], False)
        self.assertEqual(result["tart_run"]["env"], {"TART_HOME": home, "TART_NO_AUTO_PRUNE": "1"})
        inst = os.path.join(home, "vms", "job-1")
        before, after = self.config(self.seed), self.config(inst)
        self.assertNotEqual(before["macAddress"], after["macAddress"])
        self.assertEqual({k: v for k, v in before.items() if k != "macAddress"},
                         {k: v for k, v in after.items() if k != "macAddress"})
        set_call = [c for c in _fakes.calls(self.tools_dir, "tart") if c["argv"][0] == "set"][0]
        self.assertEqual(set_call["argv"], ["set", "job-1", "--random-mac"])
        self.assertEqual(set_call["env"]["TART_HOME"], home)
        code, _, err = run_cli("instance", "delete", "--tart", self.tools["tart"], "--tart-home", home,
                               "--name", "job-1")
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(os.path.exists(home))
        self.assertTrue(os.path.exists(os.path.join(self.seed, "disk.img")))

    def test_darwin_serial_and_display_refit_preserved(self):
        profile = _util.load("macos-profile")
        profile["resources"]["disk"] = "1GB"
        profile_path = _util.write_json(self.tmp, "mac.profile.json", profile)
        seed = _fakes.vm_dir(self.tmp, "macseed", dict(_fakes.DARWIN_CONFIG, displayRefit=True), 1000 ** 3)
        artifact = os.path.join(self.tmp, "mac.artifact.json")
        code, _, err = run_cli("record", profile_path, "--vm-dir", seed, "--esa-revision", _util.REV,
                               "--builder", "nix=2.31.5", "--builder", "tart=2.40.1", "--output", artifact)
        self.assertEqual((code, err), (0, ""))
        code, out, err = self.create(seed=seed, artifact=artifact, check=["--full"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["host_transformations"], ["mac-address", "platform-machine-identifier"])
        before = self.config(seed)
        after = self.config(os.path.join(self.tmp, "inst-home", "vms", "job-1"))
        self.assertNotEqual(before["ecid"], after["ecid"])
        self.assertNotEqual(before["macAddress"], after["macAddress"])
        self.assertEqual(before["hardwareModel"], after["hardwareModel"])
        self.assertIs(after["displayRefit"], True)

    def test_unbounded_transformation_refused_and_cleaned(self):
        self.control(set_extra={"cpuCount": 2})
        code, _, err = self.create()
        self.assertEqual(code, 1)
        self.assertIn("config.json/cpuCount: changed outside the selected instance transformations", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "inst-home")))

    def test_seed_mismatch_refused_before_any_tart_call(self):
        _util.write_bytes(self.seed, "nvram.bin", bytes(4096))
        code, _, err = self.create()
        self.assertEqual(code, 1)
        self.assertIn("nvram.bin: sha256 does not match", err)
        self.assertEqual(_fakes.calls(self.tools_dir), [])
        code, _, err = self.create(check=["--expect-artifact-id", "esa-artifact-v1:sha256:" + "0" * 64])
        self.assertEqual(code, 1)
        self.assertIn("does not match --expect-artifact-id", err)

    def test_seed_check_mode_is_required(self):
        code, _, err = run_cli("instance", "create", "--artifact", self.artifact, "--seed-dir", self.seed,
                               "--tart", self.tools["tart"], "--tart-home", os.path.join(self.tmp, "h"),
                               "--name", "job-1")
        self.assertEqual(code, 2)

    def test_existing_home_and_loose_inject_dir_refused(self):
        home = os.path.join(self.tmp, "existing")
        os.mkdir(home)
        code, _, err = self.create(home=home)
        self.assertEqual(code, 1)
        self.assertIn("--tart-home: already exists", err)
        inject = os.path.join(self.tmp, "inject")
        os.mkdir(inject, 0o755)
        os.chmod(inject, 0o755)
        code, _, err = self.create("--inject-dir", inject)
        self.assertEqual(code, 1)
        self.assertIn("--inject-dir: must not be accessible", err)
        colon = os.path.join(self.tmp, "a:b")
        os.mkdir(colon, 0o700)
        code, _, err = self.create("--inject-dir", colon)
        self.assertEqual(code, 1)
        self.assertIn("must not contain ':'", err)
        os.chmod(inject, 0o700)
        code, out, err = self.create("--inject-dir", inject)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["tart_run"]["args"], ["run", "--dir", "esa-inject:%s:ro" % inject, "job-1"])

    def test_delete_refuses_unowned_or_shared_home(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(os.path.join(plain, "vms", "job-1"))
        code, _, err = run_cli("instance", "delete", "--tart", self.tools["tart"], "--tart-home", plain,
                               "--name", "job-1")
        self.assertEqual(code, 1)
        self.assertIn("not an ESA-owned instance home", err)
        self.assertTrue(os.path.isdir(os.path.join(plain, "vms", "job-1")))
        self.assertEqual(self.create()[0], 0)
        home = os.path.join(self.tmp, "inst-home")
        os.mkdir(os.path.join(home, "vms", "someone-else"))
        code, _, err = run_cli("instance", "delete", "--tart", self.tools["tart"], "--tart-home", home,
                               "--name", "job-1")
        self.assertEqual(code, 1)
        self.assertIn("holds VMs other than --name", err)
        self.assertTrue(os.path.isdir(os.path.join(home, "vms", "job-1")))

    def test_delete_refuses_other_esa_owned_trees(self):
        # A run directory carries a valid ownership marker but is not an instance home.
        run = os.path.join(self.tmp, "run")
        os.makedirs(os.path.join(run, "evidence"))
        _util.write_bytes(run, ".esa-owned", b"0123456789abcdef0123456789abcdef\n")
        _util.write_bytes(os.path.join(run, "evidence"), "kept.json", b"{}")
        code, _, err = run_cli("instance", "delete", "--tart", self.tools["tart"], "--tart-home", run,
                               "--name", "job-1")
        self.assertEqual(code, 1)
        self.assertIn("not an ESA-owned instance home", err)
        self.assertTrue(os.path.isfile(os.path.join(run, "evidence", "kept.json")))
        self.assertEqual(_fakes.calls(self.tools_dir), [])


class SeedArchiveTest(LifecycleFixture):
    def export(self, output, run="exp-run"):
        return run_cli("seed", "export", "--artifact", self.artifact, "--seed-dir", self.seed, "--tart",
                       self.tools["tart"], "--run-dir", os.path.join(self.tmp, run), "--output", output,
                       "--expect-artifact-id", self.artifact_id)

    def imp(self, tvm, output, run="imp-run"):
        return run_cli("seed", "import", "--artifact", self.artifact, "--tvm", tvm, "--tart", self.tools["tart"],
                       "--run-dir", os.path.join(self.tmp, run), "--output", output)

    def test_export_import_roundtrip_is_exactly_verified(self):
        tvm = os.path.join(self.tmp, "archive.tvm")
        code, out, err = self.export(tvm)
        self.assertEqual((code, err), (0, ""))
        exported = json.loads(out)
        self.assertEqual(exported["compressed_transfer_bytes"], os.path.getsize(tvm))
        self.assertEqual(exported["archive_sha256"], _fakes.sha256_file(tvm))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "exp-run", "tart-home")))
        out_seed = os.path.join(self.tmp, "imported")
        code, out, err = self.imp(tvm, out_seed)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["verified"], "payload-full")
        code, _, err = run_cli("verify", self.artifact, "--payload-dir", out_seed)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(_fakes.mode(os.path.join(out_seed, "disk.img")), 0o444)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "imp-run", "tart-home")))

    def test_export_refuses_existing_archive(self):
        tvm = _util.write_bytes(self.tmp, "archive.tvm", b"keep")
        code, _, err = self.export(tvm)
        self.assertEqual(code, 1)
        self.assertIn("--output: already exists", err)
        with open(tvm, "rb") as handle:
            self.assertEqual(handle.read(), b"keep")
        self.assertEqual(_fakes.calls(self.tools_dir, "tart"), [])

    def test_import_with_changed_identity_refused(self):
        tvm = os.path.join(self.tmp, "archive.tvm")
        self.assertEqual(self.export(tvm)[0], 0)
        self.control(import_mutate_mac=True)
        out_seed = os.path.join(self.tmp, "imported")
        code, _, err = self.imp(tvm, out_seed)
        self.assertEqual(code, 1)
        self.assertIn("esa: refused: imported/config.json: ", err)
        self.assertFalse(os.path.exists(out_seed))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "imp-run", "tart-home")))

    def test_failed_export_removes_its_partial_archive(self):
        self.control(fail=["export"])
        tvm = os.path.join(self.tmp, "archive.tvm")
        code, _, err = self.export(tvm)
        self.assertEqual(code, 1)
        self.assertIn("step tart-export", err)
        self.assertFalse(os.path.exists(tvm))


if __name__ == "__main__":
    unittest.main()
