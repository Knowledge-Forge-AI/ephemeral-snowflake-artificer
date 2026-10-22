"""End-to-end NixOS and Tahoe construction through the real CLI over fake tools.

No Nix, Tart, ssh or VM runs: tests/fakes stand in for each process
boundary. These tests prove dispatch, refusal, cleanup and evidence binding,
not native behaviour.
"""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _util
import _fakes
from _util import run_cli
from esa import backend, cli, custody, proc, tahoe, tartvm
from esa.errors import CommandFailed, Refusal

OUT_PATH = "/nix/store/" + "a" * 32 + "-esa-seed-image"
DISK = 1 << 30


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class NixosConstructTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._shared = tempfile.TemporaryDirectory()
        cls.disk = os.path.join(cls._shared.name, "disk.img")
        _util.write_disk(cls.disk, DISK)
        cls.disk_sha = _fakes.sha256_file(cls.disk)

    @classmethod
    def tearDownClass(cls):
        cls._shared.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        for sub in ("tools", "inputs", "nar"):
            os.mkdir(os.path.join(self.tmp, sub))
        self.tools_dir = os.path.join(self.tmp, "tools")
        self.paths, self.profile = _fakes.nixos_inputs(os.path.join(self.tmp, "inputs"))
        self.nar = os.path.join(self.tmp, "nar")
        os.link(self.disk, os.path.join(self.nar, "disk.img"))
        self.report()
        self.tools = _fakes.install(self.tools_dir, {"out_path": OUT_PATH, "nar_dir": self.nar,
                                                     "sleep_pid": os.path.join(self.tmp, "sleep.pid")})
        self.key = _fakes.write_key(self.tmp)
        self.store = _fakes.store_uri(key=self.key)
        self.run_dir = os.path.join(self.tmp, "run")
        self.seed = os.path.join(self.tmp, "seed")
        self.artifact = os.path.join(self.tmp, "seed.artifact.json")
        code, _, err = run_cli("nixos", "prepare", self.paths["profile"], "--flake-lock", self.paths["lock"],
                               "--module", self.paths["module"], "--esa-revision", _util.REV,
                               "--run-dir", self.run_dir)
        self.assertEqual((code, err), (0, ""))

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, **overrides):
        path = os.path.join(self.nar, "esa-seed-check.txt")
        if os.path.exists(path):
            os.unlink(path)
        _util.write_bytes(self.nar, "esa-seed-check.txt", _fakes.nixos_seal_report(self.disk_sha, DISK, **overrides))

    def control(self, **changes):
        control = _fakes.read_control(self.tools_dir)
        control.update(changes)
        _fakes.write_control(self.tools_dir, control)

    def construct(self, *extra, store=None):
        return run_cli("nixos", "construct", "--run-dir", self.run_dir, "--nix", self.tools["nix"],
                       "--build-store", store or self.store, "--tart", self.tools["tart"],
                       "--output", self.seed, "--artifact-output", self.artifact, *extra)

    def state(self):
        with open(os.path.join(self.run_dir, "state.json")) as handle:
            return json.load(handle)

    def assert_nothing_produced(self):
        self.assertFalse(os.path.exists(self.seed))
        self.assertFalse(os.path.exists(self.artifact))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "tart-home")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "artifact.json")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "out", "disk.img")))
        self.assertEqual(self.state()["step"], "failed")

    def test_constructs_records_and_verifies(self):
        code, out, err = self.construct()
        self.assertEqual((code, err), (0, ""))
        summary = json.loads(out)
        self.assertEqual(summary["status"], "constructed-not-native-qualified")
        self.assertEqual(summary["validation"], [{"check": "seed.nixos-seal", "result": "pass"}])
        code, out, err = run_cli("verify", self.artifact, "--profile", self.paths["profile"],
                                 "--payload-dir", self.seed)
        self.assertEqual((code, err), (0, ""))
        with open(self.artifact) as handle:
            artifact = json.load(handle)
        self.assertEqual(artifact["builders"], [{"name": "nix", "version": "2.31.5"},
                                                {"name": "tart", "version": "2.40.1"}])
        self.assertEqual(artifact["payload"]["files"][1]["sha256"], self.disk_sha)
        self.assertEqual(artifact["esa_revision"], _util.REV)
        for name in ("config.json", "disk.img", "nvram.bin"):
            self.assertEqual(_fakes.mode(os.path.join(self.seed, name)), 0o444)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "tart-home")))
        self.assertEqual(self.state()["step"], "completed")
        nix_calls = [c["argv"] for c in _fakes.calls(self.tools_dir, "nix")]
        build = [a for a in nix_calls if "build" in a][0]
        self.assertEqual(build[build.index("--store") + 1], self.store)
        self.assertEqual(build[build.index("--option") + 1:build.index("--option") + 3], ["builders", ""])
        self.assertIn("--no-update-lock-file", build)
        self.assertNotIn("--out-link", build)
        for call in _fakes.calls(self.tools_dir):
            self.assertNotIn("SSH_AUTH_SOCK", call["env"])
            self.assertTrue(call["env"]["HOME"].startswith(self.run_dir))
        tart_cmds = [c["argv"][0] for c in _fakes.calls(self.tools_dir, "tart")]
        self.assertEqual(tart_cmds, ["--version", "create", "set"])
        code, _, err = self.construct()
        self.assertEqual(code, 1)  # outputs exist and a run is single-use

    def test_build_failure_produces_nothing(self):
        self.control(build_fail=True)
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("step nix-build: exited with status 1; log: ", err)
        self.assert_nothing_produced()
        self.assertEqual(os.listdir(os.path.join(self.run_dir, "evidence")), [])
        self.assertEqual(_fakes.calls(self.tools_dir, "tart"), [])

    def test_failed_seal_check_is_refused_and_kept_as_fail_evidence(self):
        self.report(machine_id="present")
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("seed.nixos-seal/machine_id: seal check failed", err)
        self.assert_nothing_produced()
        with open(os.path.join(self.run_dir, "evidence", "seed.nixos-seal.json")) as handle:
            doc = json.load(handle)
        self.assertEqual((doc["result"], doc["failures"]), ("fail", ["machine_id"]))
        self.assertEqual(_fakes.calls(self.tools_dir, "tart"), [])

    def test_report_disk_hash_must_match_streamed_disk(self):
        self.report(disk_sha256="sha256:" + "1" * 64)
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("seed.nixos-seal/disk_sha256", err)
        self.assert_nothing_produced()

    def test_unexpected_nar_content_refused(self):
        _util.write_bytes(self.nar, "extra", b"x")
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("nar: unexpected entry 'extra'", err)
        self.assert_nothing_produced()

    def test_timeout_kills_the_step(self):
        self.control(build_sleep=60)
        start = time.monotonic()
        code, _, err = self.construct("--max-step-seconds", "2")
        self.assertEqual(code, 1)
        self.assertIn("step nix-build: timed out after 2 seconds", err)
        self.assertLess(time.monotonic() - start, 30)
        with open(os.path.join(self.tmp, "sleep.pid")) as handle:
            pid = int(handle.read())
        time.sleep(0.2)
        self.assertFalse(_alive(pid))
        self.assert_nothing_produced()

    def test_replay_and_tamper_refused(self):
        _util.write_bytes(os.path.join(self.run_dir, "workspace"), "extra.nix", b"{}")
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("workspace/extra.nix: differs from the prepared workspace", err)
        os.unlink(os.path.join(self.run_dir, "workspace", "extra.nix"))
        self.control(build_fail=True)
        self.assertEqual(self.construct()[0], 1)
        self.control(build_fail=False)
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("is at step 'failed', not 'prepared'; a run is single-use", err)

    def test_store_must_be_explicit_remote(self):
        for uri in ("auto", "daemon", "local", "ssh-ng://h;rm", "file:///nix"):
            with self.subTest(uri=uri):
                code, _, err = run_cli("nixos", "construct", "--run-dir", self.run_dir, "--nix", self.tools["nix"],
                                       "--build-store", uri, "--tart", self.tools["tart"],
                                       "--output", self.seed, "--artifact-output", self.artifact)
                self.assertEqual(code, 1)
                self.assertIn("--build-store", err)
        self.assertEqual(self.state()["step"], "prepared")

    # The settings the fake nix's OpenSSH model derives for every store
    # connection when ESA's route is in force (see tests/fakes/nix.py).
    ISOLATED = {
        "config": "/dev/null",
        "user_known_hosts": "NIX_TMPDIR/host-key",
        "global_known_hosts": "/dev/null",
        "strict_host_key_checking": "yes",
        "batch_mode": "yes",
        "password_authentication": "no",
        "kbd_interactive_authentication": "no",
        "forward_agent": "no",
        "clear_all_forwardings": "yes",
        "permit_local_command": "yes",
    }

    def store_calls(self):
        rows = [c for c in _fakes.calls(self.tools_dir, "nix") if "--store" in c["argv"]]
        self.assertEqual([c["argv"][2:4] if c["argv"][2] == "store" else c["argv"][2:3] for c in rows],
                         [["store", "info"], ["build"], ["store", "dump-path"]])
        return rows

    def assert_route(self, identities, agent):
        for call in self.store_calls():
            ssh = call["ssh"]
            self.assertEqual(ssh["effective"], dict(self.ISOLATED, identities=identities, agent=agent))
            # Nix appends its own newline after the decoded line (a blank known_hosts line).
            self.assertEqual(ssh["pin_line"], "builder.invalid " + _fakes.host_key_line() + "\n")
            argv = ssh["argv"]
            self.assertEqual(argv[:5], ["ssh", "nixbuilder@builder.invalid", "-x", "-F", "/dev/null"])
            pinned = [a for a in argv if a.startswith("-oUserKnownHostsFile=")]
            self.assertEqual(pinned, ["-oUserKnownHostsFile=NIX_TMPDIR/host-key"])  # Nix's only
            if identities:
                self.assertLess(argv.index("-i"), argv.index(pinned[0]))
                self.assertLess(argv.index("-oIdentityFile=none"), argv.index("-i"))
        for call in _fakes.calls(self.tools_dir, "tart"):
            self.assertNotIn("SSH_AUTH_SOCK", call["env"])
            self.assertNotIn("NIX_SSHOPTS", call["env"])

    def assert_key_never_copied(self, *texts):
        marker = _fakes.key_marker()
        for text in texts:
            self.assertNotIn(marker.decode("ascii").strip(), text)
        for top in (self.run_dir, self.seed, self.tools_dir):
            for root, _, names in os.walk(top):
                for name in names:
                    path = os.path.join(root, name)
                    if os.path.islink(path):
                        continue
                    with open(path, "rb") as handle:
                        self.assertNotIn(marker.strip(), handle.read(), path)

    def test_key_route_uses_only_the_selected_key_and_pin(self):
        code, out, err = self.construct()
        self.assertEqual((code, err), (0, ""))
        self.assert_route([self.key], "none")
        for call in _fakes.calls(self.tools_dir, "nix"):
            self.assertNotIn("SSH_AUTH_SOCK", call["env"])
        self.assert_key_never_copied(out, err)

    def test_agent_route_uses_only_the_selected_agent(self):
        path, sock = _fakes.agent_socket(self.tmp)
        self.addCleanup(sock.close)
        code, _, err = self.construct("--ssh-agent-socket", path, store=_fakes.store_uri())
        self.assertEqual((code, err), (0, ""))
        self.assert_route([], path)
        for call in _fakes.calls(self.tools_dir, "nix"):
            self.assertEqual(call["env"]["SSH_AUTH_SOCK"], path)

    def test_key_and_agent_route(self):
        path, sock = _fakes.agent_socket(self.tmp)
        self.addCleanup(sock.close)
        code, _, err = self.construct("--ssh-agent-socket", path)
        self.assertEqual((code, err), (0, ""))
        self.assert_route([self.key], path)

    def test_ambient_ssh_environment_is_ignored(self):
        ambient = {"NIX_SSHOPTS": "-oUserKnownHostsFile=/dev/null -oStrictHostKeyChecking=no -i /x",
                   "SSH_AUTH_SOCK": os.path.join(self.tmp, "ambient.sock")}
        with mock.patch.dict(os.environ, ambient):
            code, _, err = self.construct()
        self.assertEqual((code, err), (0, ""))
        self.assert_route([self.key], "none")
        for call in _fakes.calls(self.tools_dir):
            self.assertNotIn("ambient.sock", json.dumps(call["env"]))
            self.assertNotIn("StrictHostKeyChecking=no", call["env"].get("NIX_SSHOPTS", ""))

    def refused(self, fragment, store=None, socket_path=None, pointer="--build-store"):
        extra = ("--ssh-agent-socket", socket_path) if socket_path is not None else ()
        code, _, err = self.construct(*extra, store=store)
        self.assertEqual(code, 1, err)
        if pointer:
            self.assertIn("esa: refused: %s: " % pointer, err)
        self.assertIn(fragment, err)
        self.assertEqual(_fakes.calls(self.tools_dir), [])
        self.assertEqual(self.state()["step"], "prepared")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "tart-home")))

    def test_malformed_store_route_refused_before_any_tool(self):
        good = _fakes.pin(_fakes.host_key_line())
        line = _fakes.host_key_line()
        pin_of = _fakes.pin
        blob31 = (struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 31) + bytes(31))
        short = "ssh-ed25519 " + base64.b64encode(blob31).decode("ascii") + "\n"
        rsa_named = "ssh-ed25519 " + base64.b64encode(struct.pack(">I", 7) + b"ssh-rsa" + bytes(8)).decode() + "\n"
        raw_plus = base64.b64encode(_fakes.host_key_line(_fakes.plus_comment()).encode()).decode()
        self.assertIn("+", raw_plus)
        key = self.key
        cases = [
            ("no query", "ssh-ng://nixbuilder@builder.invalid", "--build-store: must be ssh-ng://"),
            ("fragment", self.store + "#x", "--build-store: must be ssh-ng://"),
            ("no pin", "ssh-ng://builder.invalid?ssh-key=" + key, "must pin the builder's public host key"),
            ("two pins", self.store + "&base64-ssh-public-host-key=" + good, "given more than once"),
            ("two keys", self.store + "&ssh-key=" + key, "given more than once"),
            ("remote-program", self.store + "&remote-program=nix-daemon", "'remote-program' is not accepted"),
            ("compress", self.store + "&compress=true", "'compress' is not accepted"),
            ("empty item", self.store + "&", "must be NAME=VALUE"),
            ("empty value", self.store + "&ssh-key=", "must be NAME=VALUE"),
            ("raw plus", _fakes.store_uri(key=key, pin_value=raw_plus), "a raw '+' is ambiguous"),
            ("other escape", _fakes.store_uri(key=key, pin_value="%41" + good), "a raw '+' is ambiguous"),
            ("not canonical", _fakes.store_uri(key=key, pin_value=good + "A"), "must be canonical base64"),
            ("two lines", _fakes.store_uri(key=key, pin_value=pin_of(line + line)), "exactly one 'TYPE BASE64"),
            ("type", _fakes.store_uri(key=key, pin_value=pin_of(line.replace("ssh-ed25519 ", "ssh-dss ", 1))),
             "key type must be one of"),
            ("blob type", _fakes.store_uri(key=key, pin_value=pin_of(rsa_named)), "does not encode a ssh-ed25519"),
            ("blob size", _fakes.store_uri(key=key, pin_value=pin_of(short)), "exactly 51 bytes"),
            ("localhost", _fakes.store_uri("localhost", key), "must not be localhost"),
            ("LOCALHOST", _fakes.store_uri("LocalHost", key), "must not be localhost"),
            ("loopback", _fakes.store_uri("127.0.0.1", key), "loopback"),
            ("option host", _fakes.store_uri("-oProxyCommand=x", key), "DNS name or dotted IPv4"),
            ("port", _fakes.store_uri("builder.invalid:2222", key), "without a port"),
            ("no auth", _fakes.store_uri(), "names no explicit authentication"),
            ("relative key", _fakes.store_uri(key="store_key"), "--build-store/ssh-key: must be an absolute"),
            ("escaped key", _fakes.store_uri(key=key.replace("store_key", "store%5Fkey")),
             "--build-store/ssh-key: must be an absolute"),
            ("missing key", _fakes.store_uri(key=key + ".absent"), "--build-store/ssh-key: cannot be examined"),
        ]
        for name, store, fragment in cases:
            with self.subTest(name):
                self.refused(fragment, store, pointer=None)

    def test_unsafe_key_files_refused_before_any_tool(self):
        real = os.path.realpath(self.tmp)
        link = os.path.join(real, "key-link")
        os.symlink(self.key, link)
        os.mkdir(os.path.join(real, "keys"), 0o700)
        os.symlink(os.path.join(real, "keys"), os.path.join(real, "keys-link"))
        _fakes.write_private(os.path.join(real, "keys", "k"), b"x")
        readable = _fakes.write_key(self.tmp, "readable")
        os.chmod(readable, 0o640)
        linked = _fakes.write_key(self.tmp, "linked")
        os.link(linked, os.path.join(real, "second-name"))
        empty = _fakes.write_private(os.path.join(real, "empty"), b"")
        open_dir = os.path.join(real, "open")
        os.mkdir(open_dir)
        os.chmod(open_dir, 0o777)
        in_open = _fakes.write_key(open_dir)
        cases = [
            ("symlink", link, "no symlink in any component"),
            ("symlinked parent", os.path.join(real, "keys-link", "k"), "no symlink in any component"),
            ("group-readable", readable, "readable by its owner only"),
            ("hard-linked", linked, "exactly one hard link"),
            ("empty", empty, "must be between 1 and 16384 bytes"),
            ("open directory", in_open, "its directory must be owned"),
            ("directory", real, "must be a regular file"),
        ]
        for name, path, fragment in cases:
            with self.subTest(name):
                self.refused(fragment, _fakes.store_uri(key=path), pointer="--build-store/ssh-key")

    def test_unsafe_agent_sockets_refused_before_any_tool(self):
        real = os.path.realpath(self.tmp)
        shared = os.path.join(real, "shared")
        os.mkdir(shared)
        path, sock = _fakes.agent_socket(shared)
        self.addCleanup(sock.close)
        os.chmod(path, 0o666)
        os.chmod(shared, 0o755)
        private, sock2 = _fakes.agent_socket(self.tmp, "private.sock")
        self.addCleanup(sock2.close)
        os.symlink(private, os.path.join(real, "sock-link"))
        cases = [
            ("empty", "", "must be an absolute path"),
            ("relative", "agent.sock", "must be an absolute path"),
            ("control", real + "/a\nb", "without control characters"),
            ("too long", "/" + "a" * 120, "at most 103 bytes"),
            ("missing", os.path.join(real, "absent.sock"), "cannot be examined"),
            ("not a socket", self.key, "must be a socket owned by the current user"),
            ("symlink", os.path.join(real, "sock-link"), "no symlink in any component"),
            ("open", path, "unless its directory is private"),
        ]
        for name, socket_path, fragment in cases:
            with self.subTest(name):
                self.refused(fragment, self.store, socket_path, pointer="--ssh-agent-socket")

    def test_existing_output_refused_before_any_tool(self):
        os.mkdir(self.seed)
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("--output: already exists", err)
        self.assertEqual(_fakes.calls(self.tools_dir), [])
        self.assertEqual(self.state()["step"], "prepared")


class TahoeFixture(unittest.TestCase):
    SIZES = [536870912, 1000 ** 3 - 536870912]
    DISK = "1GB"

    @classmethod
    def setUpClass(cls):
        cls._shared = tempfile.TemporaryDirectory()
        base = os.path.join(cls._shared.name, "base")
        config = dict(_fakes.DARWIN_CONFIG)
        for key in ("os", "arch", "display", "diskFormat"):
            del config[key]  # an older upstream config that relies on Tart's decoding defaults
        cls.base = _fakes.vm_dir(cls._shared.name, "base", config, 1000 ** 3)
        cls.manifest = _fakes.oci_manifest(base, cls.SIZES)
        cls.digest = _fakes.sha256_bytes(cls.manifest)

    @classmethod
    def tearDownClass(cls):
        cls._shared.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        for sub in ("tools", "inputs"):
            os.mkdir(os.path.join(self.tmp, sub))
        self.tools_dir = os.path.join(self.tmp, "tools")
        self.paths, self.profile = _fakes.tahoe_inputs(os.path.join(self.tmp, "inputs"), self.digest, self.DISK)
        self.manifest_path = _util.write_bytes(self.tmp, "manifest.json", self.manifest)
        self.credential = _fakes.write_private(os.path.join(self.tmp, "bootstrap.cred"), _fakes.password() + b"\n")
        installer = self.profile["inputs"]["nix_installer"]
        macos = self.profile["inputs"]["macos"]
        self.guest = {
            "probe": "esa-check-v1\nproduct_version=%s\nbuild=%s\nuser=admin\narch=arm64\nsudo_nopasswd=yes\n"
                     % (macos["product_version"], macos["build"]),
            "install-nix": "esa-check-v1\ninstaller_sha256=%s\nnix_version=%s\n"
                           % (installer["sha256"], installer["version"]),
            "apply-darwin": "esa-check-v1\nsystem=/nix/store/%s-darwin-system-26.05.abc\nnix_version=2.31.5\n"
                            % ("b" * 32),
            "seal": "esa-check-v1\nssh_host_keys=0\nsystem_generations=1\nauthorized_keys_files=0\n"
                    "runner_files=0\nrunner_launchd_jobs=0\nautologin_user=admin\napple_account=absent\n"
                    "run_root=absent\nfinalizer=armed\n",
            "grow-disk": "esa-check-v1\ncontainer_bytes_before=900000000\ncontainer_bytes=1899000000\n",
        }
        self.tools = _fakes.install(self.tools_dir, {
            "base_ref": "ghcr.io/cirruslabs/macos-tahoe-vanilla@" + self.digest,
            "base_dir": self.base,
            "password_sha256": hashlib.sha256(_fakes.password()).hexdigest(),
            "halt_file": os.path.join(self.tmp, "halt"),
            "halt_writes_disk": True,
            "guest": self.guest,
        })
        self.run_dir = os.path.join(self.tmp, "run")
        self.seed = os.path.join(self.tmp, "seed")
        self.artifact = os.path.join(self.tmp, "seed.artifact.json")
        code, _, err = run_cli("tahoe", "prepare", self.paths["profile"], "--flake-lock", self.paths["lock"],
                               "--module", self.paths["module"], "--esa-revision", _util.REV,
                               "--run-dir", self.run_dir)
        self.assertEqual((code, err), (0, ""))

    def tearDown(self):
        self._tmp.cleanup()

    def construct_argv(self, manifest=None, shutdown="30"):
        return ["tahoe", "construct", "--run-dir", self.run_dir, "--tart", self.tools["tart"],
                "--ssh", self.tools["ssh"], "--oci-manifest", manifest or self.manifest_path,
                "--bootstrap-credential-file", self.credential, "--output", self.seed,
                "--artifact-output", self.artifact, "--boot-seconds", "30", "--shutdown-seconds", shutdown]

    def construct(self, manifest=None, shutdown="30"):
        return run_cli(*self.construct_argv(manifest, shutdown))

    def observe(self):
        return run_cli("tahoe", "observe-base", "--digest", self.digest, "--oci-manifest", self.manifest_path,
                       "--tart", self.tools["tart"], "--ssh", self.tools["ssh"],
                       "--bootstrap-credential-file", self.credential,
                       "--run-dir", os.path.join(self.tmp, "observe"), "--boot-seconds", "30")

    def set_guest(self, step, text):
        control = _fakes.read_control(self.tools_dir)
        control["guest"][step] = text
        _fakes.write_control(self.tools_dir, control)

    def control(self, **changes):
        control = _fakes.read_control(self.tools_dir)
        control.update(changes)
        _fakes.write_control(self.tools_dir, control)

    def base_copy(self, name):
        """An exact copy of the pinned base, served by the fake registry instead."""
        copy = os.path.join(self.tmp, name)
        os.mkdir(copy)
        for f in ("config.json", "nvram.bin"):
            shutil.copyfile(os.path.join(self.base, f), os.path.join(copy, f))
        _util.write_disk(os.path.join(copy, "disk.img"), 1000 ** 3)
        self.control(base_dir=copy)
        return copy

    def base_hashes(self, directory=None):
        return {f: _fakes.sha256_file(os.path.join(directory or self.base, f))
                for f in ("config.json", "nvram.bin", "disk.img")}

    def transformed_bases(self):
        """Pulled bases whose config or NVRAM alone differ from the pinned layers."""
        def config(copy):
            path = os.path.join(copy, "config.json")
            with open(path) as handle:
                cfg = json.load(handle)
            cfg["macAddress"] = "02:00:00:00:00:99"  # what a Tart MAC regeneration would do
            with open(path, "w") as handle:
                json.dump(cfg, handle, indent=2, sort_keys=True)

        def nvram(copy):
            with open(os.path.join(copy, "nvram.bin"), "r+b") as handle:
                handle.seek(100)
                handle.write(b"\xff")

        return (("config", config, "base/config.json: sha256 does not equal the OCI config layer digest"),
                ("nvram", nvram, "base/nvram.bin: sha256 does not equal the OCI NVRAM layer digest"))

    def assert_refused_at_ingress(self, err, fragment, before):
        self.assertIn(fragment, err)
        self.assertEqual([c["argv"][0] for c in _fakes.calls(self.tools_dir, "tart")], ["--version", "clone"])
        self.assertEqual(_fakes.calls(self.tools_dir, "ssh"), [])  # no boot, no bootstrap login
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "tools", "run.pid")))
        self.assertEqual(self.base_hashes(), before)

    def state(self):
        with open(os.path.join(self.run_dir, "state.json")) as handle:
            return json.load(handle)

    def assert_cleaned(self):
        self.assertFalse(os.path.exists(self.seed))
        self.assertFalse(os.path.exists(self.artifact))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "tart-home")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "private", "known_hosts")))


class TahoeConstructTest(TahoeFixture):
    def test_constructs_with_bound_evidence_and_no_credential_leak(self):
        code, out, err = self.construct()
        self.assertEqual((code, err), (0, ""))
        summary = json.loads(out)
        self.assertEqual([v["check"] for v in summary["validation"]],
                         ["base.ingress", "guest.nix-darwin", "guest.nix-install", "macos.sw-vers",
                          "seed.tahoe-seal"])
        self.assertTrue(all(v["result"] == "pass" for v in summary["validation"]))
        code, _, err = run_cli("verify", self.artifact, "--profile", self.paths["profile"], "--payload-dir", self.seed)
        self.assertEqual((code, err), (0, ""))
        with open(self.artifact) as handle:
            artifact = json.load(handle)
        self.assertEqual(artifact["builders"], [{"name": "nix", "version": "2.31.5"},
                                                {"name": "tart", "version": "2.40.1"}])
        self.assertNotEqual(artifact["payload"]["files"][1]["sha256"], _fakes.sha256_file(self.base + "/disk.img"))
        with open(os.path.join(self.run_dir, "evidence", "base.ingress.json")) as handle:
            ingress = json.load(handle)
        self.assertEqual(ingress["observed"]["base_disk_chunks_verified"], "2")
        self.assertEqual((ingress["result"], ingress["failures"]), ("pass", []))
        for key, name in (("base_config_sha256", "config.json"), ("base_nvram_sha256", "nvram.bin")):
            with open(os.path.join(self.base, name), "rb") as handle:
                self.assertEqual(ingress["observed"][key], "sha256:" + hashlib.sha256(handle.read()).hexdigest())
        self.assertEqual(ingress["binds"]["disk_sha256"], artifact["payload"]["files"][1]["sha256"])
        self.assertEqual(ingress["binds"]["base_digest"], self.digest)
        clone = [c["argv"] for c in _fakes.calls(self.tools_dir, "tart") if c["argv"][0] == "clone"][0]
        self.assertEqual(clone[1], "ghcr.io/cirruslabs/macos-tahoe-vanilla@" + self.digest)
        self.assert_cleaned_after_success()
        secret = _fakes.password()
        self.assertNotIn(secret.decode("ascii"), out + err)
        for root, _, names in os.walk(self.tmp):
            for name in names:
                path = os.path.join(root, name)
                if path == self.credential or os.path.islink(path):
                    continue
                with open(path, "rb") as handle:
                    self.assertNotIn(secret, handle.read(), path)
        for call in _fakes.calls(self.tools_dir, "ssh"):
            self.assertIn("PubkeyAuthentication=no", call["argv"])
            self.assertIn("IdentityAgent=none", call["argv"])
            self.assertEqual(call["env"]["ESA_ASKPASS_FILE"], self.credential)
            self.assertNotIn("SSH_AUTH_SOCK", call["env"])

    def assert_cleaned_after_success(self):
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "tart-home")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "private", "known_hosts")))

    def test_sw_vers_mismatch_refused_and_cleaned(self):
        self.set_guest("probe", self.guest["probe"].replace("build=25A362", "build=25B999"))
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("macos.sw-vers/build: guest check failed", err)
        self.assert_cleaned()
        with open(os.path.join(self.tmp, "tools", "run.pid")) as handle:
            pid = int(handle.read())
        time.sleep(0.2)
        self.assertFalse(_alive(pid))
        steps = [c["argv"] for c in _fakes.calls(self.tools_dir, "ssh")]
        self.assertFalse(any("umask 077" in a[-1] for a in steps))  # nothing uploaded or installed

    def test_failed_seal_refused(self):
        self.set_guest("seal", self.guest["seal"].replace("apple_account=absent", "apple_account=present"))
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("seed.tahoe-seal/apple_account: guest check failed", err)
        self.assert_cleaned()
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "evidence", "seed.tahoe-seal.failed.json")))

    def test_installer_checksum_must_match_pin(self):
        self.set_guest("install-nix", self.guest["install-nix"].replace(
            self.profile["inputs"]["nix_installer"]["sha256"], "sha256:" + "2" * 64))
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("guest.nix-install/installer_sha256", err)
        self.assert_cleaned()

    def test_manifest_must_match_pinned_digest(self):
        other = _util.write_bytes(self.tmp, "other.json", self.manifest + b" ")
        code, _, err = self.construct(other)
        self.assertEqual(code, 1)
        self.assertIn("--oci-manifest: sha256 does not equal the pinned base digest", err)
        self.assertEqual(_fakes.calls(self.tools_dir), [])

    def test_corrupt_base_chunk_refused_before_any_change(self):
        corrupt = self.base_copy("corrupt")
        with open(os.path.join(corrupt, "disk.img"), "r+b") as handle:
            handle.seek(600 << 20)
            handle.write(b"x")
        before = self.base_hashes()
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assert_refused_at_ingress(
            err, "base/disk.img: chunk 1 does not match its OCI uncompressed-content-digest", before)
        self.assert_cleaned()
        self.assertEqual(self.state()["step"], "failed")

    def construct_transformed(self, which):
        name, alter, fragment = self.transformed_bases()[which]
        served = self.base_copy(name)
        alter(served)
        before, served_before = self.base_hashes(), self.base_hashes(served)
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assert_refused_at_ingress(err, fragment, before)
        self.assertEqual(self.base_hashes(served), served_before)
        self.assert_cleaned()
        self.assertEqual(self.state()["step"], "failed")
        self.assertEqual(os.listdir(os.path.join(self.run_dir, "evidence")), [])

    def test_transformed_config_refused_before_any_change(self):
        self.construct_transformed(0)

    def test_transformed_nvram_refused_before_any_change(self):
        self.construct_transformed(1)

    def test_finalizer_not_armed_refused(self):
        self.set_guest("seal", self.guest["seal"].replace("finalizer=armed\n", ""))
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("seed.tahoe-seal/finalizer: guest check failed", err)
        self.assert_cleaned()

    def test_finalizer_setup_failure_refused(self):
        self.control(exit={"seal": 1})
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("step guest-seal: exited with status 1", err)
        self.assert_cleaned()
        self.assertEqual(self.state()["step"], "failed")

    def test_halt_not_observed_refused(self):
        self.control(seal_halts=False)
        start = time.monotonic()
        code, _, err = self.construct(shutdown="2")
        self.assertEqual(code, 1)
        self.assertIn("step tart-run: did not exit within 2 seconds", err)
        self.assertLess(time.monotonic() - start, 60)
        self.assert_cleaned()
        self.assertEqual(self.state()["step"], "failed")
        with open(os.path.join(self.tmp, "tools", "run.pid")) as handle:
            pid = int(handle.read())
        time.sleep(0.2)
        self.assertFalse(_alive(pid))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "artifact.json")))

    def test_unclean_vm_exit_refused(self):
        self.control(run_exit=1)
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("step tart-run: exited with status 1; a clean guest halt was not observed", err)
        self.assert_cleaned()
        self.assertEqual(self.state()["step"], "failed")

    def test_credential_file_must_be_private(self):
        os.chmod(self.credential, 0o644)
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("--bootstrap-credential-file: must not be accessible by group or others", err)
        self.assertEqual(_fakes.calls(self.tools_dir), [])

    def run_exits(self):
        return [c["control_sock_present"] for c in _fakes.calls(self.tools_dir, "tart-event")
                if c["event"] == "run-exit"]

    def test_stopped_control_socket_removed_and_verifiers_stay_strict(self):
        code, _, err = self.construct()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.run_exits(), [True])  # tart run left a genuine socket at exit
        self.assertEqual(sorted(os.listdir(self.seed)), ["config.json", "disk.img", "nvram.bin"])
        # Removal is not an allowance: a control.sock in a seed or VM directory is still refused.
        _fakes.residue_socket(self.seed)
        self.assertTrue(stat.S_ISSOCK(os.lstat(os.path.join(self.seed, "control.sock")).st_mode))
        code, _, err = run_cli("verify", self.artifact, "--profile", self.paths["profile"],
                               "--payload-dir", self.seed)
        self.assertEqual(code, 1)
        self.assertIn("--payload-dir/control.sock: unexpected entry in payload directory", err)
        code, _, err = run_cli("record", self.paths["profile"], "--vm-dir", self.seed, "--esa-revision", _util.REV,
                               "--builder", "nix=2.31.5", "--builder", "tart=2.40.1",
                               "--output", os.path.join(self.tmp, "again.artifact.json"))
        self.assertEqual(code, 1)
        self.assertIn("vm-dir/control.sock: unexpected entry in VM directory", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "again.artifact.json")))

    def test_missing_control_socket_is_a_noop(self):
        self.control(control_socket="none")
        code, _, err = self.construct()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.run_exits(), [False])
        self.assertEqual(sorted(os.listdir(self.seed)), ["config.json", "disk.img", "nvram.bin"])

    def construct_with_residue(self, kind):
        self.control(control_socket=kind)
        code, out, err = self.construct()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("vm-dir/control.sock: is not a Unix socket (a symlink, file or directory); not removed", err)
        self.assertEqual(self.run_exits(), [True])
        self.assert_cleaned()
        self.assertEqual(self.state()["step"], "failed")
        self.assertEqual(os.listdir(os.path.join(self.run_dir, "evidence")), [])

    def test_regular_file_control_sock_refused(self):
        self.construct_with_residue("file")

    def test_directory_control_sock_refused(self):
        self.construct_with_residue("dir")

    def test_symlinked_control_sock_refused(self):
        self.construct_with_residue("symlink")

    def test_other_vm_dir_extras_still_refused(self):
        self.control(run_extra=["extra.log"])
        code, out, err = self.construct()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("vm-dir/extra.log: unexpected entry in VM directory", err)
        self.assertNotIn("control.sock", err)  # the genuine socket was removed; the extra was not
        self.assert_cleaned()
        self.assertEqual(self.state()["step"], "failed")


class TahoeControlSocketOrderTest(TahoeFixture):
    """In-process construct over the fake tools, recording the order of the final steps."""

    def construct_recorded(self, shutdown="30"):
        events = []
        real_wait, real_remove = proc.Background.wait, tahoe.Session.remove_control_socket
        real_scan, real_finalize = tartvm.scan_vm_dir, backend.finalize

        def wait(handle, timeout):
            try:
                code = real_wait(handle, timeout)
            except CommandFailed:
                events.append(("wait", handle.step, "timeout"))
                raise
            events.append(("wait", handle.step, code))
            return code

        def remove(session):
            events.append(("remove", session.vm, sorted(os.listdir(session.vm))))
            removed = real_remove(session)
            events.append(("removed", removed, sorted(os.listdir(session.vm))))
            return removed

        def scan(directory, family, resources):
            events.append(("scan", directory, sorted(os.listdir(directory))))
            return real_scan(directory, family, resources)

        def finalize(run, vm_dir, artifact, seed_out, artifact_out):
            events.append(("finalize", vm_dir, seed_out))
            return real_finalize(run, vm_dir, artifact, seed_out, artifact_out)

        with mock.patch.object(proc.Background, "wait", wait), \
                mock.patch.object(tahoe.Session, "remove_control_socket", remove), \
                mock.patch.object(tahoe.tartvm, "scan_vm_dir", scan), \
                mock.patch.object(tahoe.backend, "finalize", finalize), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main(self.construct_argv(shutdown=shutdown))
        return code, err.getvalue(), events

    def test_removal_follows_clean_exit_and_precedes_scan_and_output(self):
        code, err, events = self.construct_recorded()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([e[0] for e in events], ["wait", "remove", "removed", "scan", "finalize"])
        self.assertEqual(events[0], ("wait", "tart-run", 0))
        vm = events[1][1]
        self.assertEqual(os.path.basename(os.path.dirname(vm)), "vms")
        self.assertEqual(os.path.basename(os.path.dirname(os.path.dirname(vm))), "tart-home")
        self.assertTrue(os.path.basename(vm).startswith("esa-mac-"))
        self.assertNotEqual(os.path.realpath(vm), os.path.realpath(self.seed))  # never the reusable seed
        self.assertEqual(events[1][2], ["config.json", "control.sock", "disk.img", "nvram.bin"])
        self.assertEqual(events[2], ("removed", True, ["config.json", "disk.img", "nvram.bin"]))
        self.assertEqual(events[3], ("scan", vm, ["config.json", "disk.img", "nvram.bin"]))
        self.assertEqual(events[4], ("finalize", vm, os.path.abspath(self.seed)))

    def test_no_removal_after_unclean_exit(self):
        self.control(run_exit=1)
        code, err, events = self.construct_recorded()
        self.assertEqual(code, 1)
        self.assertIn("step tart-run: exited with status 1; a clean guest halt was not observed", err)
        self.assertEqual(events, [("wait", "tart-run", 1)])
        self.assert_cleaned()

    def test_no_removal_when_halt_is_not_observed(self):
        self.control(seal_halts=False)
        code, err, events = self.construct_recorded(shutdown="2")
        self.assertEqual(code, 1)
        self.assertIn("step tart-run: did not exit within 2 seconds", err)
        self.assertEqual(events, [("wait", "tart-run", "timeout")])
        self.assert_cleaned()


LOCK_HOLDER = ("import fcntl, os, sys\n"
               "fd = os.open(sys.argv[1], os.O_RDWR)\n"
               "fcntl.lockf(fd, fcntl.LOCK_EX)\n"
               "print('locked', flush=True)\n"
               "sys.stdin.read()\n")


class ControlSocketRemovalTest(unittest.TestCase):
    """Session.remove_control_socket on synthetic owned VM directories; no Tart runs."""

    PAYLOAD = ["config.json", "disk.img", "nvram.bin"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        home = os.path.join(self.tmp, "h")
        token = custody.create_owned_dir(home, "test-home")
        os.mkdir(os.path.join(home, "vms"))
        self.vm = _fakes.vm_dir(os.path.join(home, "vms"), "v", _fakes.DARWIN_CONFIG, 1 << 20)
        self.session = tahoe.Session.__new__(tahoe.Session)  # only the fields the removal reads
        self.session.home, self.session.token, self.session.vm = home, token, self.vm
        self.session.vm_proc = self.exited(0)
        self.sock = os.path.join(self.vm, "control.sock")

    def exited(self, code):
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(%d)" % code])
        child.wait()
        return proc.Background("tart-run", child, None)

    def entries(self):
        return sorted(os.listdir(self.vm))

    def refused(self, fragment):
        before = self.entries()
        with self.assertRaises(Refusal) as ctx:
            self.session.remove_control_socket()
        self.assertIn(fragment, [m for _, m in ctx.exception.errors])
        self.assertEqual(self.entries(), before)

    def test_removes_the_genuine_residue(self):
        _fakes.residue_socket(self.vm)
        self.assertTrue(self.session.remove_control_socket())
        self.assertEqual(self.entries(), self.PAYLOAD)
        tartvm.list_vm_dir(self.vm)

    def test_missing_socket_is_a_noop(self):
        self.assertFalse(self.session.remove_control_socket())
        self.assertEqual(self.entries(), self.PAYLOAD)

    def test_wrong_types_refused(self):
        target = _fakes.residue_socket(self.tmp, "elsewhere.sock")
        makers = (("file", lambda: _util.write_bytes(self.vm, "control.sock", b"")),
                  ("dir", lambda: os.mkdir(self.sock)),
                  ("symlink to a socket", lambda: os.symlink(target, self.sock)))
        for name, make in makers:
            with self.subTest(name):
                make()
                self.refused("is not a Unix socket (a symlink, file or directory); not removed")
                (os.rmdir if name == "dir" else os.unlink)(self.sock)
        self.assertTrue(stat.S_ISSOCK(os.lstat(target).st_mode))

    def test_foreign_owner_refused(self):
        _fakes.residue_socket(self.vm)
        real_stat = os.stat

        def foreign(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path != "control.sock":
                return st
            return mock.Mock(st_mode=st.st_mode, st_uid=st.st_uid + 1, st_nlink=st.st_nlink,
                             st_dev=st.st_dev, st_ino=st.st_ino, st_ctime_ns=st.st_ctime_ns)

        with mock.patch.object(tahoe.os, "stat", foreign):
            self.refused("is not owned by the current user; not removed")

    def test_extra_link_refused(self):
        _fakes.residue_socket(self.vm)
        try:
            os.link(self.sock, os.path.join(self.tmp, "second-link"))
        except OSError as exc:
            self.skipTest("this filesystem does not hard-link sockets: %s" % exc.strerror)
        self.refused("has more than one link; not removed")

    def test_running_vm_refused(self):
        _fakes.residue_socket(self.vm)
        holder = subprocess.Popen([sys.executable, "-c", LOCK_HOLDER, os.path.join(self.vm, "config.json")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        def release():
            holder.stdin.close()
            holder.wait(30)
            holder.stdout.close()

        self.addCleanup(release)
        self.assertEqual(holder.stdout.readline(), b"locked\n")
        self.refused("config.json is locked; the VM appears to be running")

    def test_suspended_vm_refused(self):
        _fakes.residue_socket(self.vm)
        _util.write_bytes(self.vm, "state.vzvmsave", b"")
        self.refused("holds a suspended state (state.vzvmsave); a seed must be stopped")

    def test_replacement_during_stop_check_refused(self):
        def by_socket(_vm):
            _fakes.residue_socket(self.vm, "new.sock")
            os.rename(os.path.join(self.vm, "new.sock"), self.sock)

        def by_file(_vm):
            os.unlink(self.sock)
            _util.write_bytes(self.vm, "control.sock", b"")

        for replace, fragment in ((by_socket, "was replaced or changed during the stop check; not removed"),
                                  (by_file, "is not a Unix socket (a symlink, file or directory); not removed")):
            with self.subTest(replace.__name__):
                if os.path.lexists(self.sock):
                    os.unlink(self.sock)
                _fakes.residue_socket(self.vm)
                with mock.patch.object(tahoe.tartvm, "assert_stopped", replace):
                    with self.assertRaises(Refusal) as ctx:
                        self.session.remove_control_socket()
                self.assertIn(fragment, [m for _, m in ctx.exception.errors])
                self.assertTrue(os.path.lexists(self.sock))  # the replacement is left in place

    def test_only_after_this_runs_clean_exit(self):
        _fakes.residue_socket(self.vm)
        running = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)

        def stop():
            running.stdin.close()
            running.wait(30)

        self.addCleanup(stop)
        for handle in (None, self.exited(1), proc.Background("tart-run", running, None)):
            with self.subTest(handle):
                self.session.vm_proc = handle
                self.refused("this run's tart run has not exited with status 0; not removed")

    def test_foreign_home_refused(self):
        _fakes.residue_socket(self.vm)
        self.session.token = "0" * 32
        self.refused("the Tart home is not this run's owned home; not removed")

    def test_symlinked_vm_dir_refused(self):
        _fakes.residue_socket(self.vm)
        real = self.vm + "-real"
        os.rename(self.vm, real)
        os.symlink(real, self.vm)
        self.refused("must be a real directory, not a symlink or file")
        self.assertTrue(stat.S_ISSOCK(os.lstat(os.path.join(real, "control.sock")).st_mode))


class ObserveBaseTest(TahoeFixture):
    def test_observe_base_reports_and_deletes(self):
        run_dir = os.path.join(self.tmp, "observe")
        code, out, err = run_cli("tahoe", "observe-base", "--digest", self.digest, "--oci-manifest",
                                 self.manifest_path, "--tart", self.tools["tart"], "--ssh", self.tools["ssh"],
                                 "--bootstrap-credential-file", self.credential, "--run-dir", run_dir,
                                 "--boot-seconds", "30")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(report["profile_hint"], {"product_version": "26.0.1", "build": "25A362"})
        self.assertEqual(report["ingress"]["base_digest"], self.digest)
        self.assertFalse(os.path.exists(os.path.join(run_dir, "tart-home")))
        self.assertTrue(os.path.exists(os.path.join(run_dir, "observe-base.json")))
        tart_cmds = [c["argv"][0] for c in _fakes.calls(self.tools_dir, "tart")]
        self.assertNotIn("set", tart_cmds)

    def observe_transformed(self, which):
        name, alter, fragment = self.transformed_bases()[which]
        alter(self.base_copy(name))
        before = self.base_hashes()
        code, out, err = self.observe()
        self.assertEqual((code, out), (1, ""))
        self.assert_refused_at_ingress(err, fragment, before)
        run_dir = os.path.join(self.tmp, "observe")
        self.assertFalse(os.path.exists(os.path.join(run_dir, "tart-home")))
        self.assertFalse(os.path.exists(os.path.join(run_dir, "observe-base.json")))

    def test_observe_refuses_transformed_config(self):
        self.observe_transformed(0)

    def test_observe_refuses_transformed_nvram(self):
        self.observe_transformed(1)


PLIST_VALUE = r"<string>([^<]*)</string>|<(true|false)/>|(<array>(?:\s*<string>[^<]*</string>)*\s*</array>)"
PLIST_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
              '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>')
PLIST_TAIL = "</dict>\n</plist>\n"


def plist_dict(text):
    """Parse the one-level plist subset the finalizer job uses; refuse anything else."""
    if not text.startswith(PLIST_HEAD) or not text.endswith(PLIST_TAIL):
        raise ValueError("not a one-dict plist")
    body = text[len(PLIST_HEAD):-len(PLIST_TAIL)]
    item = r"\s*<key>([^<]*)</key>\s*(?:%s)" % PLIST_VALUE
    if not re.fullmatch(r"(?:%s)*\s*" % item, body):
        raise ValueError("unsupported plist content")
    out = {}
    for key, string, boolean, array in re.findall(item, body):
        if key in out:
            raise ValueError("duplicate key %s" % key)
        if array:
            out[key] = re.findall(r"<string>([^<]*)</string>", array)
        elif boolean:
            out[key] = boolean == "true"
        else:
            out[key] = string
    return out


class GuestScriptTest(unittest.TestCase):
    """Static checks only: no guest script runs on the host."""

    SCRIPTS = os.path.join(_util.REPO, "recipes", "darwin", "guest")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        with open(os.path.join(self.SCRIPTS, "seal.sh")) as handle:
            self.seal = handle.read()

    def heredoc(self, delimiter):
        match = re.search(r"<<'?%s'?\n(.*?)\n%s\n" % (delimiter, delimiter), self.seal, re.S)
        self.assertIsNotNone(match, delimiter)
        return match.group(1) + "\n"

    def syntax_ok(self, path):
        proc = subprocess.run(["/bin/sh", "-n", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""), path)

    def test_guest_scripts_parse(self):
        names = sorted(n for n in os.listdir(self.SCRIPTS) if n.endswith(".sh"))
        self.assertIn("seal.sh", names)
        for name in names:
            with self.subTest(name):
                self.syntax_ok(os.path.join(self.SCRIPTS, name))

    def test_finalizer_parses_and_removes_host_keys_after_its_own_files_then_halts(self):
        body = self.heredoc("FINALIZE")
        path = _util.write_bytes(self._tmp.name, "finalize.sh", body.encode("ascii"))
        self.syntax_ok(path)
        lines = [line.strip() for line in body.splitlines()]
        own = lines.index('rm -rf "$dir" || exit 1')
        keys = lines.index("rm -f /etc/ssh/ssh_host_* || exit 1")
        halt = lines.index("exec /sbin/shutdown -h now")
        self.assertLess(own, keys)
        self.assertLess(keys, halt)
        self.assertEqual(lines[halt + 1:], ["}", 'main "$@"'])  # halting is the last command

    def test_finalizer_job_is_a_one_shot_for_this_boot(self):
        directory = "/private/var/tmp/esa-seal-final.AbCd1234"
        text = self.heredoc("PLIST").replace("$dir", directory).replace("$conn", "4242")
        self.assertNotIn("$", text)
        self.assertEqual(plist_dict(text), {
            "Label": "esa.seal-finalizer",
            "ProgramArguments": ["/bin/sh", directory + "/finalize.sh", "4242", directory],
            "RunAtLoad": True,
            "KeepAlive": False,
            "LaunchOnlyOnce": True,
        })


class TahoeGrowTest(TahoeFixture):
    DISK = "2GB"

    def test_grown_container_passes(self):
        code, out, err = self.construct()
        self.assertEqual((code, err), (0, ""))
        checks = [v["check"] for v in json.loads(out)["validation"]]
        self.assertIn("guest.disk-grow", checks)
        resize = [c["argv"] for c in _fakes.calls(self.tools_dir, "tart") if c["argv"][0] == "set"][0]
        self.assertEqual(resize[-2:], ["--disk-size", "2"])

    def test_noop_resize_refused(self):
        self.set_guest("grow-disk", "esa-check-v1\ncontainer_bytes_before=900000000\ncontainer_bytes=900000000\n")
        code, _, err = self.construct()
        self.assertEqual(code, 1)
        self.assertIn("guest.disk-grow/container_bytes: guest check failed", err)
        self.assert_cleaned()
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "evidence", "guest.disk-grow.failed.json")))


if __name__ == "__main__":
    unittest.main()
