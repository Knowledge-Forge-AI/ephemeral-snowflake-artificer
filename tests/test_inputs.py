"""Consumer lock and module verification, and workspace preparation."""

import base64
import json
import os
import struct
import tempfile
import unittest

import _util
import _fakes
from _util import run_cli

from esa import nixinputs
from esa import nixos
from esa.errors import Refusal


class LockTest(unittest.TestCase):
    def setUp(self):
        self.profile = _util.load("payload-profile")
        pin = self.profile["inputs"]["nixpkgs"]
        self.node = _fakes.github_node("NixOS", "nixpkgs", pin["rev"], pin["nar_hash"], "nixos-26.05")

    def check(self, nodes=None, raw=None):
        data = raw if raw is not None else _fakes.lock_bytes(nodes or {"nixpkgs": self.node})
        self.profile["inputs"]["flake_lock_sha256"] = _fakes.sha256_bytes(data)
        return nixinputs.check_lock(data, self.profile)

    def refused(self, fragment, **kw):
        with self.assertRaises(Refusal) as ctx:
            self.check(**kw)
        self.assertTrue(any(fragment in p + ": " + m for p, m in ctx.exception.errors), ctx.exception.errors)

    def test_matching_lock(self):
        self.assertEqual(self.check()["nixpkgs"]["ref"], "nixos-26.05")

    def test_lock_hash_mismatch(self):
        data = _fakes.lock_bytes({"nixpkgs": self.node})
        self.profile["inputs"]["flake_lock_sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(Refusal) as ctx:
            nixinputs.check_lock(data, self.profile)
        self.assertIn("flake_lock_sha256", ctx.exception.errors[0][1])

    def test_malformed_lock(self):
        self.refused("not strict JSON", raw=b'{"nodes": {}, "nodes": {}}')
        self.refused("version 7", raw=json.dumps({"nodes": {}, "root": "root", "version": 6}).encode())

    def test_foreign_owner(self):
        node = dict(self.node, locked=dict(self.node["locked"], owner="someone"))
        self.refused("must lock github:NixOS/nixpkgs", nodes={"nixpkgs": node})

    def test_missing_and_extra_nodes(self):
        self.refused("exactly root, nixpkgs", nodes={"other": self.node})
        self.refused("exactly root, nixpkgs", nodes={"nixpkgs": self.node, "extra": self.node})

    def test_declared_input_mismatch(self):
        node = dict(self.node, locked=dict(self.node["locked"], rev="1" * 40))
        self.refused("locked rev differs", nodes={"nixpkgs": node})
        node = dict(self.node, locked=dict(self.node["locked"], narHash="sha256-" + "A" * 43 + "="))
        self.refused("locked narHash differs", nodes={"nixpkgs": node})

    def test_non_github_or_moving_original(self):
        node = dict(self.node, locked=dict(self.node["locked"], type="path"))
        self.refused("must lock github", nodes={"nixpkgs": node})
        node = dict(self.node, original=dict(self.node["original"], ref="refs/heads/x"))
        self.refused("simple branch or tag", nodes={"nixpkgs": node})


class ModuleTest(unittest.TestCase):
    def refusals(self, text):
        return nixinputs.lex_refusals(text)

    def test_self_contained_module_accepted(self):
        text = ('{ pkgs, lib, ... }:\n# ./not/a/path in a comment\n'
                '{ environment.etc."a/b".text = "./x ${pkgs.hello}/bin/hello";\n'
                "  x = ''\n    ./also/text ''${literal} ${toString 1}\n  '';\n  y = a // b; z = 6 / 3; }\n")
        self.assertEqual(self.refusals(text), [])

    def test_path_literals_refused(self):
        for text in ("{ imports = [ ./other.nix ]; }", "{ x = ../up; }", "{ x = /etc/passwd; }",
                     "{ x = ~/home; }", "{ x = a/b; }", '{ x = "${./interp}"; }', "{ x = /. + \"/etc\"; }"):
            with self.subTest(text=text):
                self.assertTrue(self.refusals(text))

    def test_search_path_refused(self):
        self.assertEqual(self.refusals("{ x = import <nixpkgs> {}; }")[0][1].split(";")[0],
                         "uses a <...> search-path lookup")

    def test_unterminated_refused(self):
        for text in ('{ x = "abc; }', "{ x = ''abc; }", "/* open", '{ x = "${a"; }'):
            with self.subTest(text=text), self.assertRaises(Refusal):
                self.refusals(text)


class BuildStoreRouteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key = _fakes.write_key(self._tmp.name)

    def line(self, key_type, *fields):
        blob = b"".join(struct.pack(">I", len(f)) + f for f in (key_type.encode("ascii"),) + fields)
        return "%s %s synthetic-host-key\n" % (key_type, base64.b64encode(blob).decode("ascii"))

    def test_key_route(self):
        route = nixos.parse_build_store(_fakes.store_uri(key=self.key))
        self.assertEqual((route.host, route.key_path, route.agent_socket), ("builder.invalid", self.key, None))
        self.assertEqual(route.pin_text, _fakes.host_key_line())

    def test_escaped_and_unescaped_base64_decode_alike(self):
        line = _fakes.host_key_line("x")
        raw = base64.b64encode(line.encode("ascii")).decode("ascii")
        self.assertTrue("/" in raw or "=" in raw)
        escaped = raw.replace("/", "%2f").replace("=", "%3D")
        for value in (raw, escaped):
            with self.subTest(value=value):
                route = nixos.parse_build_store(_fakes.store_uri(key=self.key, pin_value=value))
                self.assertEqual(route.pin_text, line)

    def test_other_host_key_types(self):
        for key_type, fields in (("ecdsa-sha2-nistp256", (b"nistp256", b"\x04" + bytes(64))),
                                 ("ssh-rsa", (b"\x01\x00\x01", b"\x00" + bytes(256)))):
            with self.subTest(key_type):
                line = self.line(key_type, *fields)
                route = nixos.parse_build_store(_fakes.store_uri("192.0.2.7", self.key, _fakes.pin(line)))
                self.assertEqual((route.host, route.pin_text), ("192.0.2.7", line))

    def test_agent_only_route_needs_a_socket(self):
        with self.assertRaises(Refusal) as ctx:
            nixos.parse_build_store(_fakes.store_uri())
        self.assertEqual(ctx.exception.errors[0][0], "--build-store")
        path, sock = _fakes.agent_socket(self._tmp.name)
        self.addCleanup(sock.close)
        route = nixos.parse_build_store(_fakes.store_uri(), path)
        self.assertEqual((route.key_path, route.agent_socket), (None, path))


class PrepareCLITest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.paths, self.profile = _fakes.nixos_inputs(self.tmp)
        self.run_dir = os.path.join(self.tmp, "run")

    def tearDown(self):
        self._tmp.cleanup()

    def prepare(self, *extra, module=True, family="nixos"):
        args = [family, "prepare", self.paths["profile"], "--flake-lock", self.paths["lock"],
                "--esa-revision", _util.REV, "--run-dir", self.run_dir]
        if module:
            args += ["--module", self.paths["module"]]
        return run_cli(*(args + list(extra)))

    def test_prepare_writes_isolated_workspace(self):
        code, out, err = self.prepare()
        self.assertEqual((code, err), (0, ""))
        summary = json.loads(out)
        self.assertIs(summary["executes"], False)
        self.assertEqual(sorted(summary["workspace_files"]), [
            "consumer.nix", "esa-params.json", "esa/nixos/guest-agent.nix", "esa/nixos/image.nix",
            "esa/nixos/injection.nix", "esa/nixos/minimal.nix", "flake.lock", "flake.nix"])
        self.assertEqual(_fakes.mode(self.run_dir), 0o700)
        ws = os.path.join(self.run_dir, "workspace")
        with open(os.path.join(ws, "flake.nix")) as handle:
            flake = handle.read()
        self.assertIn('inputs.nixpkgs = { type = "github"; owner = "NixOS"; repo = "nixpkgs"; '
                      'ref = "nixos-26.05"; };', flake)
        with open(os.path.join(ws, "esa-params.json")) as handle:
            params = json.load(handle)
        self.assertEqual(params["disk_mib"], 1024)
        with open(os.path.join(ws, "consumer.nix"), "rb") as handle:
            self.assertEqual(handle.read(), _fakes.MODULE)

    def test_existing_run_dir_refused(self):
        os.mkdir(self.run_dir)
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("--run-dir: already exists", err)

    def test_run_dir_in_source_tree_refused(self):
        self.run_dir = os.path.join(_util.REPO, "build-run-should-not-exist")
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("outside the ESA source tree", err)
        self.assertFalse(os.path.exists(self.run_dir))

    def test_module_hash_mismatch(self):
        _util.write_bytes(self.tmp, "consumer.nix", _fakes.MODULE + b"\n")
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("customization.sha256", err)
        self.assertFalse(os.path.exists(self.run_dir))

    def test_module_symlink_refused(self):
        os.rename(self.paths["module"], os.path.join(self.tmp, "real.nix"))
        os.symlink(os.path.join(self.tmp, "real.nix"), self.paths["module"])
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("--module: must be a regular file", err)

    def test_module_path_escape_refused(self):
        module = b"{ imports = [ ../../outside.nix ]; }\n"
        sub = os.path.join(self.tmp, "esc")
        os.mkdir(sub)
        self.paths, _ = _fakes.nixos_inputs(sub, module=module)
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("--module/line-1: contains a path literal", err)

    def test_missing_lock_is_io_error(self):
        os.unlink(self.paths["lock"])
        code, _, err = self.prepare()
        self.assertEqual(code, 2)

    def test_wrong_family_refused(self):
        code, _, err = self.prepare(family="tahoe")
        self.assertEqual(code, 1)
        self.assertIn("prepares tahoe-vanilla profiles", err)

    def test_tahoe_without_inherited_account_refused(self):
        sub = os.path.join(self.tmp, "mac")
        os.mkdir(sub)
        self.paths, _ = _fakes.tahoe_inputs(sub, "sha256:" + "1" * 64, account=False)
        code, _, err = self.prepare(family="tahoe")
        self.assertEqual(code, 1)
        self.assertIn("separately documented boundary", err)


if __name__ == "__main__":
    unittest.main()
