"""Bootstrap SSH channel into a construction guest.

The bootstrap credential is never read by ESA and never appears in argv,
environment values, logs, manifests or the seed. ``ssh`` obtains it through
``bin/esa-askpass``, which re-checks and prints the private file named by
``ESA_ASKPASS_FILE``. User SSH configuration, agents and identities are
ignored. Guest steps are fixed ESA scripts sent on stdin.

Host-key trust is trust on first use, not an authenticated Tart-to-SSH
identity. ``tart run`` uses Tart's default shared NAT network, where the
host and other NAT peers can reach the guest; ESA connects to whatever
answers at the address ``tart ip`` reports and accepts the first host key it
sees (``StrictHostKeyChecking=accept-new``). The run-private known_hosts
only pins that key for the rest of the run, and it is deleted with the run's
private state. The bootstrap password is therefore sent to that first
responder.
"""

from __future__ import annotations

import os
import shlex
import tarfile
import time

from esa import custody
from esa import proc
from esa.errors import CommandFailed

ASKPASS = os.path.join(custody.REPO_ROOT, "bin", "esa-askpass")
SCRIPTS = os.path.join(custody.REPO_ROOT, "recipes", "darwin", "guest")
CREDENTIAL_MAX = 4096


class Guest:
    def __init__(self, runner: proc.Runner, ssh_path: str, user: str, host: str,
                 known_hosts: str, credential_file: str, private_home: str):
        self.runner = runner
        self.ssh = ssh_path
        self.user = user
        self.host = host
        self.known_hosts = known_hosts
        self.env = {
            "PATH": proc.BASE_PATH,
            "HOME": private_home,
            "LANG": "C",
            "LC_ALL": "C",
            "SSH_ASKPASS": ASKPASS,
            "SSH_ASKPASS_REQUIRE": "force",
            "ESA_ASKPASS_FILE": os.path.abspath(credential_file),
        }

    def argv(self, remote: str) -> list:
        options = [
            "BatchMode=no", "PubkeyAuthentication=no", "IdentityAgent=none", "IdentitiesOnly=yes",
            "IdentityFile=none", "PreferredAuthentications=keyboard-interactive,password",
            "NumberOfPasswordPrompts=1", "StrictHostKeyChecking=accept-new",
            "UserKnownHostsFile=" + self.known_hosts, "GlobalKnownHostsFile=/dev/null",
            "UpdateHostKeys=no", "ConnectTimeout=15", "ServerAliveInterval=15", "ServerAliveCountMax=4",
            "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes", "RequestTTY=no", "LogLevel=ERROR",
        ]
        argv = [self.ssh, "-F", "/dev/null"]
        for option in options:
            argv += ["-o", option]
        return argv + ["-l", self.user, self.host, remote]

    def wait_ready(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.runner.run("ssh-ready", self.argv("true"), timeout=60, env=self.env)
                return
            except CommandFailed:
                if time.monotonic() >= deadline:
                    raise CommandFailed("ssh-ready", "guest SSH did not accept the bootstrap login within "
                                                     "%d seconds" % timeout) from None
                time.sleep(5)

    def script(self, step: str, args=(), timeout: int = 600) -> bytes:
        """Run recipes/darwin/guest/<step>.sh with fixed arguments; return stdout."""
        path = os.path.join(SCRIPTS, step + ".sh")
        remote = " ".join(["/bin/sh", "-s", "--"] + [shlex.quote(a) for a in args])
        out = self.runner.run("guest-" + step, self.argv(remote), timeout=timeout, env=self.env,
                              stdin_path=path, capture=True)
        return out.encode("utf-8")

    def upload_dir(self, step: str, local_dir: str, tar_path: str, remote_root: str, timeout: int = 600) -> None:
        """Copy a directory to ``remote_root/<basename>`` as a stdin tar stream."""
        fd = os.open(tar_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
            tar.add(local_dir, arcname=os.path.basename(local_dir), filter=_anonymous)
        remote = "umask 077 && mkdir -p %s && tar -xf - -C %s" % (shlex.quote(remote_root), shlex.quote(remote_root))
        self.runner.run(step, self.argv(remote), timeout=timeout, env=self.env, stdin_path=tar_path)


def _anonymous(info: tarfile.TarInfo):
    if not (info.isfile() or info.isdir()):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info
