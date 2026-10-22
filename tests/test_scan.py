import unittest

from _util import concat
from esa import scan


class ScanTest(unittest.TestCase):
    def test_clean_strings(self):
        for text in (
            "registration_material_absent",
            "macos.app.passwords",
            "tool.go-1.25",
            "https://github.com/NixOS/nixpkgs/blob/master/COPYING",
            "ghcr.io/cirruslabs/macos-tahoe-vanilla",
            "sha256:" + "a" * 64,
            "password manager capability",
        ):
            with self.subTest(text=text):
                self.assertEqual(scan.scan_text(text), [])

    def test_refused_categories(self):
        cases = {
            concat("-----", "BEGIN RSA ", "PRIV", "ATE KEY-----"): "private key block",
            concat("ecdsa-sha2-", "nistp256 ", "AAAA", "E2VjZHNh"): "SSH public key",
            concat("github", "_pat_", "x"): "access token",
            concat("gl", "pat-", "x"): "access token",
            concat("AK", "IA", "ABCDEFGHIJKLMNOP"): "access token",
            concat("00000000-0000", "-0000-0000-000000000000"): "UUID-shaped identifier",
            concat(".", "runner"): "runner registration file",
            concat("/ho", "me/ci/work"): "host home path",
            concat("~", "/secrets"): "host home path",
            concat("ci", "@", "example.invalid"): "email address",
            concat("PASS", "WORD: hunter2"): "credential assignment",
            concat("registration", "_token = abc"): "credential assignment",
        }
        for text, category in cases.items():
            with self.subTest(category=category):
                self.assertIn(category, scan.scan_text(text))

    def test_document_pointers_escape_keys(self):
        findings = scan.scan_document({"a/b": [concat("x", "@", "example.invalid")]})
        self.assertEqual(findings, [("/a~1b/0", "refused material: email address")])


if __name__ == "__main__":
    unittest.main()
