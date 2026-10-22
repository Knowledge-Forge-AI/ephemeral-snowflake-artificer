"""Bounded execution of explicit external tools.

Every step runs an argv list (never a shell) with an environment built only
from what the caller passes, stdin from /dev/null unless a file is given, its
own process group, a timeout, and private per-step logs (mode 0600) in the
run directory. A timeout terminates the whole process group. A non-zero exit
or timeout raises ``CommandFailed``; nothing is retried.

A termination counts as complete only once the process group is shown gone
(``ESRCH``); a reaped leader alone is not enough. Otherwise it raises
``TerminationUnresolved`` (or the permission error that stopped it), the
``Runner`` keeps that error, starts no further step, and ``kill_all`` raises it
again, so callers keep every owned path the group may still use.

Arguments and environments must never carry credential values: credentials
travel only as private file paths that a helper reads (see bin/esa-askpass).
"""

from __future__ import annotations

import errno
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
_PROBE_INTERVAL = 0.25  # between group probes, within a grace already running


class TerminationUnresolved(CommandFailed):
    """A step's process group was not shown gone; keep everything it may use."""


def _open_log(path: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def _kill_group(proc, step, log, grace=GRACE_SECONDS):
    # Signals go to the group only while its leader is unreaped: only then is
    # the group id still certainly ours. After the reap, only signal 0 follows.
    if proc.poll() is not None:
        _require_gone(proc.pid, time.monotonic() + grace, step, log)
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
        deadline = time.monotonic() + wait
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            # Darwin refuses a group whose only member is the exited, unreaped
            # leader. That is benign only once the leader is reaped within the
            # grace and the group is then gone; otherwise the refusal stands.
            if exc.errno == errno.EPERM and _reaped_and_group_gone(proc, wait):
                return
            raise
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        _require_gone(proc.pid, deadline, step, log)
        return
    raise TerminationUnresolved(step, "did not exit within %d seconds of SIGKILL; termination unresolved"
                                % grace, log)


def _require_gone(pgid, deadline, step, log):
    """Return once the group is gone (ESRCH); raise if that is not shown by the deadline."""
    while True:
        refused = None
        try:
            os.killpg(pgid, 0)  # signal 0 delivers nothing; it only tests for the group
        except ProcessLookupError:
            return
        except PermissionError as exc:
            if exc.errno != errno.EPERM:
                raise
            refused = exc  # an exited, unreaped or unsignalable member: not absence
        left = deadline - time.monotonic()
        if left <= 0:
            if refused is not None:
                raise refused
            raise TerminationUnresolved(step, "process group remained after its leader exited; "
                                              "termination unresolved", log)
        time.sleep(min(_PROBE_INTERVAL, left))


def _reaped_and_group_gone(proc, grace) -> bool:
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return False
    try:
        os.killpg(proc.pid, 0)  # signal 0 delivers nothing; it only tests for the group
    except ProcessLookupError:
        return True
    return False


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
        _kill_group(self.proc, self.step, self.log)


class Runner:
    def __init__(self, log_dir: str, base_env: dict, default_timeout: int):
        self.log_dir = log_dir
        self.base_env = dict(base_env)
        self.default_timeout = default_timeout
        self._seq = 0
        self._background = []
        self.unresolved = None  # the first termination not shown complete

    def _terminate(self, kill, *args):
        try:
            kill(*args)
        except BaseException as exc:
            if self.unresolved is None:
                self.unresolved = exc
            raise

    def _refuse_after_unresolved(self):
        if self.unresolved is not None:
            raise self.unresolved

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
        self._refuse_after_unresolved()
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
                    self._terminate(_kill_group, proc, step, log_path)
                    raise CommandFailed(step, "timed out after %d seconds" % timeout, log_path) from None
                except BaseException:
                    self._terminate(_kill_group, proc, step, log_path)
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
        self._refuse_after_unresolved()
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
                try:
                    self._terminate(_kill_group, proc, step, log_path)
                except BaseException:
                    pass  # kept in self.unresolved and raised once the timer is joined

            def timed_out():
                return self.unresolved or CommandFailed(step, "timed out after %d seconds" % timeout, log_path)

            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            try:
                try:
                    result = consume(proc.stdout)
                except BaseException:
                    # A timer that has fired owns the termination; wait for it rather than start another.
                    timer.cancel()
                    timer.join()
                    if fired.is_set():
                        raise timed_out() from None
                    self._terminate(_kill_group, proc, step, log_path)
                    raise
                code = proc.wait(timeout=max(1, timeout))
            finally:
                timer.cancel()
                timer.join()  # bounded by _kill_group's own grace
                proc.stdout.close()
        if fired.is_set():
            raise timed_out()
        if code != 0:
            raise CommandFailed(step, "exited with status %d" % code, log_path)
        return result

    def background(self, step, argv, env=None):
        self._refuse_after_unresolved()
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
        """Terminate every background group, then raise the first unresolved
        termination of this runner, if any. Delete owned state only after this
        returns."""
        for handle in self._background:
            self._terminate(handle.kill)
        self._refuse_after_unresolved()


def poll_until(predicate, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
