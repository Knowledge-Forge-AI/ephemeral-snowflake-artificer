"""Tart invocation scoped to one ESA-owned ``TART_HOME``.

Every call passes an explicit Tart path, an owned ``TART_HOME`` that ESA
created for this run, ``TART_NO_AUTO_PRUNE=1`` and a private empty ``HOME``.
ESA never uses the default Tart home, never prunes, never passes
``--overwrite`` and never runs Tart with credentials in its arguments.
Command shapes follow Tart 2.40.1 (``Create``, ``Set``, ``Clone``, ``Export``,
``Import``); ``--memory`` is MiB and ``--disk-size`` is decimal GB.

Registry credentials. A private ``HOME`` and ``TART_HOME`` and an
environment without ``TART_REGISTRY_*`` variables do not show that Tart reads
no credentials. Tart 2.40.1's OCI registry client looks for credentials in
the environment, then the Docker configuration, then the macOS Keychain, even
for a public pull that only needs an anonymous bearer token; a private
``HOME`` is not shown to prevent the Keychain lookup. ESA makes no claim that ``tart clone`` of
a public base is credential-free. The operator chooses the ``--tart``
executable and must qualify its credential-access route (for example by
running it under their own sandbox that denies credential stores). ESA ships
no such wrapper and supplies no dummy credential.
"""

from __future__ import annotations

import os
import re

from esa import manifest
from esa import proc
from esa.errors import CommandFailed, UsageError

NAME_RE = r"[a-z][a-z0-9-]{0,62}"
IPV4_RE = r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}"


def tool_path(path: str, pointer: str) -> str:
    """An explicit, absolute, executable tool path chosen by the caller."""
    if not os.path.isabs(path):
        raise UsageError("%s must be an absolute path" % pointer)
    real = os.path.realpath(path)
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        raise UsageError("%s is not an executable file" % pointer)
    return path


def env(home: str, private_home: str, tmp: str) -> dict:
    return {
        "PATH": proc.BASE_PATH,
        "HOME": private_home,
        "TMPDIR": tmp,
        "TART_HOME": home,
        "TART_NO_AUTO_PRUNE": "1",
        "LANG": "C",
        "LC_ALL": "C",
    }


class Tart:
    def __init__(self, runner: proc.Runner, path: str, home: str, private_home: str, tmp: str,
                 timeout: int = 1800):
        self.runner = runner
        self.path = path
        self.home = home
        self.env = env(home, private_home, tmp)
        self.timeout = timeout

    def vm_dir(self, name: str) -> str:
        return os.path.join(self.home, "vms", name)

    def _run(self, step, args, timeout=None, capture=False):
        for arg in args:
            if not isinstance(arg, str):
                raise TypeError(arg)
        return self.runner.run(step, [self.path] + list(args), timeout=timeout or self.timeout,
                               env=self.env, capture=capture)

    def version(self) -> str:
        out = self._run("tart-version", ["--version"], timeout=60, capture=True).strip()
        if not re.fullmatch(manifest.BUILDER_VERSION_RE, out):
            raise CommandFailed("tart-version", "did not print an exact MAJOR.MINOR.PATCH version")
        return out

    def create_linux(self, name: str) -> None:
        self._run("tart-create", ["create", "--linux", "--disk-size", "1", name])

    def set(self, name: str, options) -> None:
        self._run("tart-set", ["set", name] + list(options))

    def clone(self, source: str, name: str, timeout: int) -> None:
        self._run("tart-clone", ["clone", source, name], timeout=timeout)

    def run(self, name: str, extra=()) -> proc.Background:
        return self.runner.background("tart-run", [self.path, "run", "--no-graphics"] + list(extra) + [name],
                                      env=self.env)

    def ip(self, name: str, wait_seconds: int) -> str:
        out = self._run("tart-ip", ["ip", name, "--wait", str(wait_seconds)],
                        timeout=wait_seconds + 60, capture=True).strip()
        if not re.fullmatch(IPV4_RE, out):
            raise CommandFailed("tart-ip", "did not print one IPv4 address")
        return out

    def delete(self, name: str) -> None:
        self._run("tart-delete", ["delete", name], timeout=300)

    def export(self, name: str, path: str, timeout: int) -> None:
        self._run("tart-export", ["export", name, path], timeout=timeout)

    def import_(self, path: str, name: str, timeout: int) -> None:
        self._run("tart-import", ["import", path, name], timeout=timeout)
