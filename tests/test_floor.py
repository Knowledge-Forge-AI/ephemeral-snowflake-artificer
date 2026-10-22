"""The Python 3.9 language floor and the standard-library-only rule."""

import ast
import os
import sys
import unittest

import _util

ALLOWED = {
    "__future__", "argparse", "ast", "base64", "copy", "ctypes", "datetime", "errno", "fcntl", "hashlib",
    "importlib", "io", "json", "os", "re", "secrets", "shlex", "shutil", "signal", "stat", "struct",
    "subprocess", "sys", "tarfile", "tempfile", "threading", "time", "unittest", "esa",
}


def sources():
    roots = [os.path.join(_util.REPO, "src", "esa"), os.path.join(_util.REPO, "tests")]
    files = [os.path.join(_util.REPO, "bin", "esa")]
    for root in roots:
        for current, _, names in os.walk(root):
            files += [os.path.join(current, n) for n in sorted(names) if n.endswith(".py")]
    return files


class FloorTest(unittest.TestCase):
    def test_parses_as_python_3_9(self):
        for path in sources():
            with open(path) as handle:
                text = handle.read()
            with self.subTest(path=os.path.relpath(path, _util.REPO)):
                tree = ast.parse(text, filename=path, feature_version=(3, 9))
                self.assertFalse([n for n in ast.walk(tree) if type(n).__name__ == "Match"])

    def test_imports_are_standard_library(self):
        local = {"_util", "_fakes"} | {n[:-3] for n in os.listdir(os.path.join(_util.REPO, "tests"))
                                         if n.startswith("test_")}
        for path in sources():
            with open(path) as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    with self.subTest(path=os.path.relpath(path, _util.REPO), module=name):
                        self.assertIn(name, ALLOWED | local)

    def test_running_interpreter_meets_floor(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 9))


if __name__ == "__main__":
    unittest.main()
