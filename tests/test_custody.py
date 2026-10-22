"""Owned destinations, private inputs, copies, bounded processes and NAR/evidence parsing."""

import io
import os
import struct
import sys
import tempfile
import time
import unittest

import _util
import _fakes

from esa import custody
from esa import evidence
from esa import nar
from esa import proc
from esa.errors import CommandFailed, Refusal, UsageError


class CustodyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_owned_dir_and_scoped_removal(self):
        path = os.path.join(self.tmp, "owned")
        token = custody.create_owned_dir(path, "--x")
        sibling = _util.write_bytes(self.tmp, "sibling", b"keep")
        _util.write_bytes(path, "inside", b"x")
        with self.assertRaises(Refusal):
            custody.remove_owned(path, "0" * 32)
        self.assertTrue(os.path.exists(path))
        custody.remove_owned(path, token)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(sibling))

    def test_unmarked_or_symlinked_dir_not_removed(self):
        plain = os.path.join(self.tmp, "plain")
        os.mkdir(plain)
        with self.assertRaises(Refusal):
            custody.remove_owned(plain, "0" * 32)
        link = os.path.join(self.tmp, "link")
        os.symlink(plain, link)
        with self.assertRaises(Refusal):
            custody.remove_owned(link, "0" * 32)
        self.assertTrue(os.path.isdir(plain))

    def test_existing_destination_refused(self):
        for kind in ("file", "dir", "dangling"):
            path = os.path.join(self.tmp, kind)
            if kind == "file":
                _util.write_bytes(self.tmp, kind, b"")
            elif kind == "dir":
                os.mkdir(path)
            else:
                os.symlink(os.path.join(self.tmp, "nowhere"), path)
            with self.subTest(kind=kind), self.assertRaises(Refusal):
                custody.new_output(path, "--output")

    def test_source_tree_refused(self):
        with self.assertRaises(Refusal):
            custody.new_output(os.path.join(_util.REPO, "docs", "seed-out"), "--output")

    def test_private_file_checks(self):
        good = _fakes.write_private(os.path.join(self.tmp, "cred"), b"x")
        custody.check_private_file(good, "--c", 4096)
        loose = _util.write_bytes(self.tmp, "loose", b"x")
        os.chmod(loose, 0o640)
        with self.assertRaises(Refusal):
            custody.check_private_file(loose, "--c", 4096)
        link = os.path.join(self.tmp, "link")
        os.symlink(good, link)
        with self.assertRaises(Refusal):
            custody.check_private_file(link, "--c", 4096)
        big = _fakes.write_private(os.path.join(self.tmp, "big"), b"x" * 5000)
        with self.assertRaises(Refusal):
            custody.check_private_file(big, "--c", 4096)
        with self.assertRaises(UsageError):
            custody.check_private_file(os.path.join(self.tmp, "missing"), "--c", 4096)

    def test_private_dir_checks(self):
        path = os.path.join(self.tmp, "share")
        os.mkdir(path, 0o700)
        custody.check_private_dir(path, "--d")
        os.chmod(path, 0o755)
        with self.assertRaises(Refusal):
            custody.check_private_dir(path, "--d")

    def test_sparse_copy_preserves_bytes(self):
        src = os.path.join(self.tmp, "src.img")
        _util.write_disk(src, 64 << 20)
        dst = os.path.join(self.tmp, "dst.img")
        custody.sparse_copy(src, dst)
        self.assertEqual(_fakes.sha256_file(src), _fakes.sha256_file(dst))
        self.assertLess(os.lstat(dst).st_blocks * 512, 8 << 20)
        with self.assertRaises(OSError):
            custody.sparse_copy(src, dst)

    @unittest.skipUnless(sys.platform == "darwin", "APFS clonefile(2) only")
    def test_clonefile_refuses_existing(self):
        src = _util.write_bytes(self.tmp, "a", b"abc")
        dst = os.path.join(self.tmp, "b")
        custody.clone_file(src, dst)
        with open(dst, "rb") as handle:
            self.assertEqual(handle.read(), b"abc")
        with self.assertRaises(OSError):
            custody.clone_file(src, dst)


class ProcTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.logs = os.path.join(self.tmp, "logs")
        os.mkdir(self.logs)

    def tearDown(self):
        self._tmp.cleanup()

    def test_environment_is_only_what_is_passed(self):
        runner = proc.Runner(self.logs, {"PATH": proc.BASE_PATH, "ESA_ONLY": "v"}, 30)
        out = runner.run("env", [sys.executable, "-c", "import os,json;print(json.dumps(sorted(os.environ)))"],
                         capture=True)
        self.assertNotIn("HOME", out)
        self.assertIn("ESA_ONLY", out)
        log_path = [os.path.join(self.logs, n) for n in os.listdir(self.logs) if n.endswith(".log")][0]
        with open(log_path) as handle:
            log = handle.read()
        self.assertIn("ESA_ONLY", log)
        self.assertNotIn('"v"', log)  # values are never logged
        self.assertEqual(_fakes.mode(log_path), 0o600)

    def test_failure_and_timeout(self):
        runner = proc.Runner(self.logs, {}, 30)
        with self.assertRaises(CommandFailed) as ctx:
            runner.run("fails", [sys.executable, "-c", "raise SystemExit(3)"])
        self.assertIn("status 3", ctx.exception.reason)
        pidfile = os.path.join(self.tmp, "child.pid")
        script = ("import os,subprocess,sys,time;"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                  "open(%r,'w').write(str(p.pid));time.sleep(60)" % pidfile)
        start = time.monotonic()
        with self.assertRaises(CommandFailed) as ctx:
            runner.run("hangs", [sys.executable, "-c", script], timeout=2)
        self.assertIn("timed out", ctx.exception.reason)
        self.assertLess(time.monotonic() - start, 30)
        with open(pidfile) as handle:
            grandchild = int(handle.read())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild, 0)  # the whole process group was terminated

    def test_missing_tool(self):
        runner = proc.Runner(self.logs, {}, 30)
        with self.assertRaises(CommandFailed):
            runner.run("missing", [os.path.join(self.tmp, "no-such-tool")])


def _nar(entries, trailer=b""):
    out = io.BytesIO()

    def s(data):
        out.write(struct.pack("<Q", len(data)) + data + b"\0" * ((8 - len(data) % 8) % 8))

    for token in (b"nix-archive-1", b"(", b"type", b"directory"):
        s(token)
    for name, kind, payload in entries:
        for token in (b"entry", b"(", b"name", name, b"node", b"(", b"type", kind):
            s(token)
        if kind == b"regular":
            s(b"contents")
            s(payload)
        else:
            s(b"target")
            s(payload)
        s(b")")
        s(b")")
    s(b")")
    out.write(trailer)
    out.seek(0)
    return out


class NarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self._tmp.name, "out")

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_expected_files(self):
        disk = b"\0" * (2 << 20) + b"data"
        found = nar.extract(_nar([(b"disk.img", b"regular", disk), (b"report.txt", b"regular", b"r\n")]),
                            self.dest, {"disk.img": 1 << 30, "report.txt": 100})
        self.assertEqual(found["disk.img"], (len(disk), _fakes.sha256_bytes(disk)))
        self.assertEqual(_fakes.sha256_file(os.path.join(self.dest, "disk.img")), _fakes.sha256_bytes(disk))

    def test_refusals(self):
        cases = {
            "unexpected": _nar([(b"disk.img", b"regular", b"x"), (b"zzz", b"regular", b"x")]),
            "symlink": _nar([(b"disk.img", b"symlink", b"/etc/passwd")]),
            "missing": _nar([(b"disk.img", b"regular", b"x")]),
            "trailing": _nar([(b"disk.img", b"regular", b"x"), (b"report.txt", b"regular", b"r")], b"junk"),
            "unsorted": _nar([(b"report.txt", b"regular", b"r"), (b"disk.img", b"regular", b"x")]),
            "oversize": _nar([(b"disk.img", b"regular", b"x" * 200), (b"report.txt", b"regular", b"r")]),
        }
        truncated = _nar([(b"disk.img", b"regular", b"x" * 64)]).getvalue()[:-30]
        cases["truncated"] = io.BytesIO(truncated)
        for name, stream in cases.items():
            with self.subTest(case=name), self.assertRaises(Refusal):
                nar.extract(stream, self.dest + name, {"disk.img": 100, "report.txt": 100})


class EvidenceTest(unittest.TestCase):
    def test_report_parsing(self):
        self.assertEqual(evidence.parse_report(b"esa-check-v1\na=1\nb=sha256:00\n"), {"a": "1", "b": "sha256:00"})
        for bad in (b"a=1\n", b"esa-check-v1\na=1\na=2\n", b"esa-check-v1\nA=1\n", b"esa-check-v1\na=x y\n",
                    b"esa-check-v1\nnoequals\n", "esa-check-v1\na=é\n".encode("utf-8")):
            with self.subTest(bad=bad), self.assertRaises(Refusal):
                evidence.parse_report(bad)

    def test_judge_is_closed(self):
        table = {"a": evidence.equals("0"), "b": evidence.int_between(1, 2)}
        self.assertEqual(evidence.judge({"a": "0", "b": "2"}, table), [])
        self.assertEqual(evidence.judge({"a": "1", "b": "3"}, table), ["a", "b"])
        self.assertEqual(evidence.judge({"a": "0"}, table), ["b"])
        self.assertEqual(evidence.judge({"a": "0", "b": "1", "c": "x"}, table), ["unexpected:c"])
        self.assertEqual(evidence.judge({"a": "0", "b": "01"}, table), ["b"])


if __name__ == "__main__":
    unittest.main()
