"""Bounded execution of explicit external tools.

Every step runs an argv list (never a shell) with an environment built only
from what the caller passes, stdin from /dev/null unless a file is given, its
own process group, a timeout, and private per-step logs (mode 0600) in the
run directory. A timeout terminates the whole process group. A non-zero exit
or timeout raises ``CommandFailed``; nothing is retried.

Arguments and environments must never carry credential values: credentials
travel only as private file paths that a helper reads (see bin/esa-askpass).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time

from esa.errors import CommandFailed

GRACE_SECONDS = 10
MAX_CAPTURE = 1 << 20
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _open_log(path: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def _kill_group(proc, grace=GRACE_SECONDS):
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


class Background:
    """A long-running owned process such as ``tart run``."""

    def __init__(self, step, proc, log_path):
        self.step = step
        self.proc = proc
        self.log = log_path

    def running(self) -> bool:
        return self.proc.poll() is None

    def wait(self, timeout: float) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CommandFailed(self.step, "did not exit within %d seconds" % timeout, self.log) from None

    def kill(self):
        _kill_group(self.proc)


class Runner:
    def __init__(self, log_dir: str, base_env: dict, default_timeout: int):
        self.log_dir = log_dir
        self.base_env = dict(base_env)
        self.default_timeout = default_timeout
        self._seq = 0
        self._background = []

    def _paths(self, step: str):
        self._seq += 1
        stem = os.path.join(self.log_dir, "%03d-%s" % (self._seq, step))
        return stem + ".log", stem + ".stdout"

    def _env(self, env):
        merged = dict(self.base_env)
        merged.update(env or {})
        return merged

    def _header(self, log, argv, env):
        record = {"argv": list(argv), "env_keys": sorted(env)}
        log.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        log.flush()

    def run(self, step, argv, timeout=None, env=None, stdin_path=None, capture=False, cwd=None):
        """Run to completion. Returns captured stdout text, or None."""
        timeout = timeout or self.default_timeout
        log_path, out_path = self._paths(step)
        env = self._env(env)
        with _open_log(log_path) as log:
            self._header(log, argv, env)
            out = _open_log(out_path) if capture else log
            stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
            try:
                try:
                    proc = subprocess.Popen(
                        list(argv), stdin=stdin, stdout=out, stderr=log, env=env, cwd=cwd,
                        start_new_session=True, close_fds=True,
                    )
                except OSError as exc:
                    raise CommandFailed(step, "could not start: %s" % (exc.strerror or exc), log_path) from None
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_group(proc)
                    raise CommandFailed(step, "timed out after %d seconds" % timeout, log_path) from None
                except BaseException:
                    _kill_group(proc)
                    raise
            finally:
                if stdin_path:
                    stdin.close()
                if capture:
                    out.close()
        if code != 0:
            raise CommandFailed(step, "exited with status %d" % code, log_path)
        if not capture:
            return None
        with open(out_path, "rb") as handle:
            data = handle.read(MAX_CAPTURE + 1)
        if len(data) > MAX_CAPTURE:
            raise CommandFailed(step, "produced more than %d bytes of output" % MAX_CAPTURE, log_path)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise CommandFailed(step, "produced non-UTF-8 output", log_path) from None

    def stream(self, step, argv, consume, timeout=None, env=None):
        """Run with stdout piped into ``consume(fileobj)``; returns its result."""
        timeout = timeout or self.default_timeout
        log_path, _ = self._paths(step)
        env = self._env(env)
        with _open_log(log_path) as log:
            self._header(log, argv, env)
            try:
                proc = subprocess.Popen(
                    list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                    env=env, start_new_session=True, close_fds=True,
                )
            except OSError as exc:
                raise CommandFailed(step, "could not start: %s" % (exc.strerror or exc), log_path) from None
            fired = threading.Event()

            def expire():
                fired.set()
                _kill_group(proc)

            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            try:
                try:
                    result = consume(proc.stdout)
                except BaseException:
                    _kill_group(proc)
                    if fired.is_set():
                        raise CommandFailed(step, "timed out after %d seconds" % timeout, log_path) from None
                    raise
                code = proc.wait(timeout=max(1, timeout))
            finally:
                timer.cancel()
                proc.stdout.close()
        if fired.is_set():
            raise CommandFailed(step, "timed out after %d seconds" % timeout, log_path)
        if code != 0:
            raise CommandFailed(step, "exited with status %d" % code, log_path)
        return result

    def background(self, step, argv, env=None):
        log_path, _ = self._paths(step)
        env = self._env(env)
        log = _open_log(log_path)
        try:
            self._header(log, argv, env)
            proc = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            raise CommandFailed(step, "could not start: %s" % (exc.strerror or exc), log_path) from None
        finally:
            log.close()
        handle = Background(step, proc, log_path)
        self._background.append(handle)
        return handle

    def kill_all(self):
        for handle in self._background:
            handle.kill()


def poll_until(predicate, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
