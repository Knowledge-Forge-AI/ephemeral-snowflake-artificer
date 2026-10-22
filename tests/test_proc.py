"""Process-group termination over a scripted process boundary.

Only ``Popen``, ``os.killpg`` for one synthetic pid, and ``proc``'s own clock
(``proc.time``) and timer (``proc.threading``) are replaced: each test scripts
the exact boundary calls it expects and their outcomes, and any call beyond
the script fails. The real ``_kill_group``, ``Background`` and ``Runner``
control flow runs. No process is started, no signal is sent and nothing
really sleeps.
"""

import errno
import io
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import _util  # noqa: F401  (puts src on sys.path)
from esa import proc
from esa.errors import CommandFailed

PID = 4242
LOG = "/esa-synthetic/logs/001-tart-run.log"  # named in errors only; never opened
UNRESOLVED_GROUP = "process group remained after its leader exited; termination unresolved"
UNRESOLVED_KILL = "did not exit within 10 seconds of SIGKILL; termination unresolved"
PROBE = (("killpg", 4242, 0), None)
SLEEP = (("sleep", 0.25), None)


def eperm():
    return PermissionError(errno.EPERM, "Operation not permitted")


def esrch():
    return ProcessLookupError(errno.ESRCH, "No such process")


def timeout():
    return subprocess.TimeoutExpired(["tart"], 10)


class Boundary:
    """An ordered script of (call, outcome[, seconds]) at the process boundary.

    An outcome that is an exception is raised; anything else is returned.
    The boundary is also ``proc``'s clock: each call moves it forward by its
    optional third element, by default the timeout of a ``wait`` that times
    out, the argument of a ``sleep``, and 0 otherwise.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.real_killpg = os.killpg
        self.now = 1000.0

    def step(self, call):
        self.calls.append(call)
        if not self.script:
            raise AssertionError("unscripted boundary call %r" % (call,))
        entry = self.script.pop(0)
        expected, outcome = entry[0], entry[1]
        if call != expected:
            raise AssertionError("boundary call %r, expected %r" % (call, expected))
        if len(entry) > 2:
            self.now += entry[2]
        elif call[0] == "sleep" or (call[0] == "wait" and isinstance(outcome, subprocess.TimeoutExpired)):
            self.now += call[1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def killpg(self, pid, sig):
        if pid != PID:
            return self.real_killpg(pid, sig)  # not this test's synthetic group
        return self.step(("killpg", pid, sig))

    # The proc.time surface.

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.step(("sleep", seconds))


class FakeProcess:
    """The Popen surface proc uses, answered from the script."""

    pid = PID

    def __init__(self, boundary):
        self.boundary = boundary
        self.returncode = None
        self.stdout = io.BytesIO()

    def poll(self):
        return self.boundary.step(("poll",))

    def wait(self, timeout=None):
        self.returncode = self.boundary.step(("wait", timeout))
        return self.returncode


class KillGroupTest(unittest.TestCase):
    def kill(self, script):
        """Run the real Background.kill against the script; returns (boundary, raised exception or None)."""
        boundary = Boundary(script)
        raised = None
        with mock.patch.object(proc.os, "killpg", boundary.killpg), mock.patch.object(proc, "time", boundary):
            try:
                result = proc.Background("tart-run", FakeProcess(boundary), LOG).kill()
            except AssertionError:
                raise
            except Exception as exc:  # the test inspects exactly what escaped
                raised = exc
            else:
                self.assertIsNone(result)
        self.assertEqual(boundary.script, [], "scripted boundary calls were not made")
        return boundary, raised

    def assert_completes(self, script):
        boundary, raised = self.kill(script)
        self.assertIsNone(raised)
        return boundary.calls

    # Preserved behaviour. A reaped leader is no longer taken as proof that its
    # group is gone (finding F1): each success now ends with a signal-0 probe
    # answered ESRCH, which delivers nothing.

    def test_already_exited_sends_nothing(self):
        self.assertEqual(self.assert_completes([
            (("poll",), 0),
            (("killpg", 4242, 0), esrch()),
        ]), [("poll",), ("killpg", 4242, 0)])

    def test_term_then_exit(self):
        self.assertEqual(self.assert_completes([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), -15),
            (("killpg", 4242, 0), esrch()),
        ]), [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10), ("killpg", 4242, 0)])

    def test_term_timeout_then_kill(self):
        self.assertEqual(self.assert_completes([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), None),
            (("wait", 10), -9),
            (("killpg", 4242, 0), esrch()),
        ]), [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
             ("killpg", 4242, signal.SIGKILL), ("wait", 10), ("killpg", 4242, 0)])

    def test_group_gone_at_term(self):
        self.assertEqual(self.assert_completes([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), esrch()),
        ]), [("poll",), ("killpg", 4242, signal.SIGTERM)])

    def test_group_gone_at_kill(self):
        self.assertEqual(self.assert_completes([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), esrch()),
        ]), [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10), ("killpg", 4242, signal.SIGKILL)])

    def test_both_graces_exhausted_is_unresolved(self):
        # Formerly test_pre_existing_silent_return_after_both_graces, which pinned
        # finding F2: a leader surviving both graces was returned from silently,
        # and callers then deleted owned state. The boundary calls are unchanged;
        # the silent return is a bug, corrected here to a visible failure. This is
        # not a weakened expectation.
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), None),
            (("wait", 10), timeout()),
        ])
        self.assertIs(type(raised), proc.TerminationUnresolved)
        self.assertIsInstance(raised, CommandFailed)
        self.assertEqual((raised.step, raised.reason, raised.log), ("tart-run", UNRESOLVED_KILL, LOG))
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, signal.SIGKILL), ("wait", 10)])

    # A reaped leader with its group still present (finding F1): only signal 0
    # follows, within the grace already running, and only ESRCH clears.

    def assert_unresolved_group(self, script, calls):
        boundary, raised = self.kill(script)
        self.assertIs(type(raised), proc.TerminationUnresolved)
        self.assertEqual((raised.step, raised.reason, raised.log), ("tart-run", UNRESOLVED_GROUP, LOG))
        self.assertEqual(boundary.calls, calls)

    def test_reaped_on_term_with_group_present_is_unresolved_without_kill(self):
        self.assert_unresolved_group([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), -15, 9.5),
            PROBE, SLEEP, PROBE, SLEEP, PROBE,
        ], [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
            ("killpg", 4242, 0), ("sleep", 0.25), ("killpg", 4242, 0), ("sleep", 0.25), ("killpg", 4242, 0)])

    def test_reaped_on_kill_with_group_present_is_unresolved(self):
        self.assert_unresolved_group([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), None),
            (("wait", 10), -9, 9.75),
            PROBE, SLEEP, PROBE,
        ], [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10), ("killpg", 4242, signal.SIGKILL),
            ("wait", 10), ("killpg", 4242, 0), ("sleep", 0.25), ("killpg", 4242, 0)])

    def test_already_exited_with_group_present_sends_no_destructive_signal(self):
        # One grace of probes, 0.25 s apart: 41 probes and 40 sleeps, then unresolved.
        self.assert_unresolved_group(
            [(("poll",), 0)] + [PROBE, SLEEP] * 40 + [PROBE],
            [("poll",)] + [("killpg", 4242, 0), ("sleep", 0.25)] * 40 + [("killpg", 4242, 0)])

    def test_only_absence_clears_a_reaped_leaders_group(self):
        self.assertEqual(self.assert_completes([
            (("poll",), 0),
            (("killpg", 4242, 0), eperm()),
            SLEEP,
            PROBE,
            SLEEP,
            (("killpg", 4242, 0), esrch()),
        ]), [("poll",), ("killpg", 4242, 0), ("sleep", 0.25), ("killpg", 4242, 0), ("sleep", 0.25),
             ("killpg", 4242, 0)])

    def test_probe_refusal_persisting_to_the_deadline_is_raised_unchanged(self):
        last = eperm()
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), -15, 9.5),
            (("killpg", 4242, 0), eperm()),
            SLEEP,
            (("killpg", 4242, 0), eperm()),
            SLEEP,
            (("killpg", 4242, 0), last),
        ])
        self.assertIs(raised, last)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, 0), ("sleep", 0.25), ("killpg", 4242, 0),
                                          ("sleep", 0.25), ("killpg", 4242, 0)])

    def test_other_probe_error_after_a_reaped_leader_raised_at_once(self):
        for name, error in (("EINVAL", OSError(errno.EINVAL, "Invalid argument")),
                            ("EACCES", PermissionError(errno.EACCES, "Permission denied"))):
            with self.subTest(name):
                boundary, raised = self.kill([
                    (("poll",), 0),
                    (("killpg", 4242, 0), error),
                ])
                self.assertIs(raised, error)
                self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, 0)])

    # The terminal-group EPERM: benign only once the leader is reaped and the group is gone.

    def test_eperm_at_term_then_reaped_and_group_gone_completes(self):
        self.assertEqual(self.assert_completes([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), eperm()),
            (("wait", 10), 0),
            (("killpg", 4242, 0), esrch()),
        ]), [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10), ("killpg", 4242, 0)])

    def test_eperm_at_kill_then_reaped_and_group_gone_completes(self):
        self.assertEqual(self.assert_completes([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), eperm()),
            (("wait", 10), -9),
            (("killpg", 4242, 0), esrch()),
        ]), [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
             ("killpg", 4242, signal.SIGKILL), ("wait", 10), ("killpg", 4242, 0)])

    def test_eperm_with_leader_still_live_raises_the_original(self):
        refused = eperm()
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), refused),
            (("wait", 10), timeout()),
        ])
        self.assertIs(raised, refused)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10)])

    def test_eperm_at_kill_with_leader_still_live_raises_the_original(self):
        refused = eperm()
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), refused),
            (("wait", 10), timeout()),
        ])
        self.assertIs(raised, refused)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, signal.SIGKILL), ("wait", 10)])

    def test_eperm_with_reaped_leader_but_group_present_raises_the_original(self):
        refused = eperm()
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), refused),
            (("wait", 10), 0),
            (("killpg", 4242, 0), None),
        ])
        self.assertIs(raised, refused)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, 0)])

    def test_eperm_at_kill_with_reaped_leader_but_group_present_raises_the_original(self):
        refused = eperm()
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), refused),
            (("wait", 10), -9),
            (("killpg", 4242, 0), None),
        ])
        self.assertIs(raised, refused)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, signal.SIGKILL), ("wait", 10), ("killpg", 4242, 0)])

    def test_eperm_with_reaped_leader_and_unsignalable_group_raises(self):
        refused, probed = eperm(), eperm()
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), refused),
            (("wait", 10), 0),
            (("killpg", 4242, 0), probed),
        ])
        self.assertIs(raised, probed)
        self.assertIs(raised.__context__, refused)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, 0)])

    # Other errors are unchanged: raised at once, with no wait or probe.

    def test_other_errors_raised_unchanged(self):
        for name, error in (("EINVAL", OSError(errno.EINVAL, "Invalid argument")),
                            ("EACCES", PermissionError(errno.EACCES, "Permission denied"))):
            with self.subTest(name):
                boundary, raised = self.kill([
                    (("poll",), None),
                    (("killpg", 4242, signal.SIGTERM), error),
                ])
                self.assertIs(raised, error)
                self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM)])

    def test_other_probe_error_raised_unchanged(self):
        error = OSError(errno.EINVAL, "Invalid argument")
        boundary, raised = self.kill([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), eperm()),
            (("wait", 10), 0),
            (("killpg", 4242, 0), error),
        ])
        self.assertIs(raised, error)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, 0)])


class KillAllTest(unittest.TestCase):
    """Runner.background then Runner.kill_all, through the real Runner and Background."""

    def kill_all(self, script):
        boundary = Boundary(script)
        real_popen = proc.subprocess.Popen

        def popen(argv, *args, **kwargs):
            if argv != ["tart", "run", "--no-graphics", "esa-mac-x"]:
                return real_popen(argv, *args, **kwargs)  # not this test's synthetic process
            return FakeProcess(boundary)

        with tempfile.TemporaryDirectory() as logs:
            runner = proc.Runner(logs, {}, 30)
            with mock.patch.object(proc.subprocess, "Popen", popen):
                handle = runner.background("tart-run", ["tart", "run", "--no-graphics", "esa-mac-x"])
            self.assertEqual(handle.log, os.path.join(logs, "001-tart-run.log"))
            with mock.patch.object(proc.os, "killpg", boundary.killpg), mock.patch.object(proc, "time", boundary):
                try:
                    runner.kill_all()
                except AssertionError:
                    raise
                except Exception as exc:
                    self.runner = runner
                    return boundary, exc
                finally:
                    self.assertEqual(boundary.script, [], "scripted boundary calls were not made")
        return boundary, None

    def test_benign_terminal_group_completes(self):
        boundary, raised = self.kill_all([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), eperm()),
            (("wait", 10), 0),
            (("killpg", 4242, 0), esrch()),
        ])
        self.assertIsNone(raised)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10),
                                          ("killpg", 4242, 0)])

    def test_live_group_permission_error_propagates(self):
        refused = eperm()
        boundary, raised = self.kill_all([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), refused),
            (("wait", 10), timeout()),
        ])
        self.assertIs(raised, refused)
        self.assertEqual(boundary.calls, [("poll",), ("killpg", 4242, signal.SIGTERM), ("wait", 10)])

    def test_unresolved_background_termination_is_latched(self):
        boundary, raised = self.kill_all([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), None),
            (("wait", 10), timeout()),
        ])
        self.assertIs(type(raised), proc.TerminationUnresolved)
        self.assertEqual((raised.step, raised.reason), ("tart-run", UNRESOLVED_KILL))
        self.assertIs(self.runner.unresolved, raised)
        with self.assertRaises(proc.TerminationUnresolved) as refused:
            self.runner.background("tart-run", ["tart", "run", "--no-graphics", "esa-mac-y"])
        self.assertIs(refused.exception, raised)


class RunnerTest(unittest.TestCase):
    """Runner.run and Runner.stream over the scripted boundary, through the real Runner."""

    ARGV = ["tart", "set", "esa-x", "--random-mac"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = self._tmp.name
        self.runner = proc.Runner(self.logs, {}, 30)
        self.started = []
        self.timers = []

    def tearDown(self):
        self._tmp.cleanup()

    def boundary(self, script):
        self.bound = Boundary(script)
        real_popen = proc.subprocess.Popen

        def popen(argv, *args, **kwargs):
            if argv != self.ARGV:
                return real_popen(argv, *args, **kwargs)  # not this test's synthetic process
            self.started.append(list(argv))
            return FakeProcess(self.bound)

        test = self

        class Timer:
            """proc's stream timer; it fires only when the test says so."""

            def __init__(self, interval, function):
                self.interval, self.function, self.firing = interval, function, False
                test.timers.append(self)

            def start(self):
                pass

            def cancel(self):
                pass  # like threading.Timer, cancel cannot stop a timer that is already firing

            def join(self):
                if self.firing:
                    self.firing = False
                    self.function()  # a timer already firing finishes before join returns

        patches = [mock.patch.object(proc.subprocess, "Popen", popen),
                   mock.patch.object(proc.os, "killpg", self.bound.killpg),
                   mock.patch.object(proc, "time", self.bound),
                   mock.patch.object(proc, "threading", mock.NonCallableMock(
                       spec=["Timer", "Event"], Timer=Timer, Event=threading.Event))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return self.bound

    def call(self, method, *args, **kwargs):
        try:
            return getattr(self.runner, method)(*args, **kwargs), None
        except AssertionError:
            raise
        except Exception as exc:  # the test inspects exactly what escaped
            return None, exc
        finally:
            self.assertEqual(self.bound.script, [], "scripted boundary calls were not made")

    def unresolved_run(self):
        self.boundary([
            (("wait", 30), timeout()),
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), None),
            (("wait", 10), timeout()),
        ])
        _, raised = self.call("run", "tart-set", self.ARGV)
        return raised

    def test_run_timeout_with_unresolved_termination(self):
        raised = self.unresolved_run()
        self.assertIs(type(raised), proc.TerminationUnresolved)
        self.assertEqual((raised.step, raised.reason, raised.log),
                         ("tart-set", UNRESOLVED_KILL, os.path.join(self.logs, "001-tart-set.log")))
        self.assertIs(self.runner.unresolved, raised)
        with self.assertRaises(proc.TerminationUnresolved) as again:
            self.runner.kill_all()
        self.assertIs(again.exception, raised)

    def test_no_step_starts_after_an_unresolved_termination(self):
        raised = self.unresolved_run()
        for method, args in (("run", ("tart-set", self.ARGV)),
                             ("stream", ("tart-set", self.ARGV, lambda stream: None)),
                             ("background", ("tart-set", self.ARGV))):
            with self.subTest(method):
                with self.assertRaises(proc.TerminationUnresolved) as refused:
                    getattr(self.runner, method)(*args)
                self.assertIs(refused.exception, raised)
        self.assertEqual(self.started, [self.ARGV])
        self.assertEqual(sorted(os.listdir(self.logs)), ["001-tart-set.log"])

    def test_run_timeout_with_known_clear_group_is_an_ordinary_timeout(self):
        self.boundary([
            (("wait", 30), timeout()),
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), -15),
            (("killpg", 4242, 0), esrch()),
        ])
        _, raised = self.call("run", "tart-set", self.ARGV)
        self.assertIs(type(raised), CommandFailed)
        self.assertEqual((raised.step, raised.reason, raised.log),
                         ("tart-set", "timed out after 30 seconds", os.path.join(self.logs, "001-tart-set.log")))
        self.assertIsNone(self.runner.unresolved)
        self.assertIsNone(self.runner.kill_all())

    def test_run_live_leader_permission_error_is_latched_unchanged(self):
        refused = eperm()
        self.boundary([
            (("wait", 30), timeout()),
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), refused),
            (("wait", 10), timeout()),
        ])
        _, raised = self.call("run", "tart-set", self.ARGV)
        self.assertIs(raised, refused)
        self.assertIs(self.runner.unresolved, refused)
        with self.assertRaises(PermissionError) as again:
            self.runner.kill_all()
        self.assertIs(again.exception, refused)

    def test_stream_success_and_ordinary_consumer_failure(self):
        self.boundary([(("wait", 30), 0)])
        result, raised = self.call("stream", "tart-set", self.ARGV, lambda stream: "consumed")
        self.assertEqual((result, raised), ("consumed", None))
        failure = ValueError("truncated")

        def consume(stream):
            raise failure

        self.boundary([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), -15),
            (("killpg", 4242, 0), esrch()),
        ])
        _, raised = self.call("stream", "tart-set", self.ARGV, consume)
        self.assertIs(raised, failure)
        self.assertIsNone(self.runner.kill_all())

    def test_stream_timer_unresolved_after_consumer_returns(self):
        # The timer fires as the consumer returns; the leader is already reaped
        # but its group stays. stream must not return or report a plain timeout
        # before that termination is settled.
        self.boundary([(("wait", 30), -9), (("poll",), -9)] + [PROBE, SLEEP] * 40 + [PROBE])

        def consume(stream):
            self.timers[0].firing = True
            return "consumed"

        _, raised = self.call("stream", "tart-set", self.ARGV, consume)
        self.assertIs(type(raised), proc.TerminationUnresolved)
        self.assertEqual((raised.step, raised.reason), ("tart-set", UNRESOLVED_GROUP))
        with self.assertRaises(proc.TerminationUnresolved) as again:
            self.runner.kill_all()
        self.assertIs(again.exception, raised)

    def test_stream_timer_unresolved_then_consumer_fails(self):
        # The timer owns the termination; the consumer's failure must not start
        # a second one (any extra boundary call fails the script).
        self.boundary([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), timeout()),
            (("killpg", 4242, signal.SIGKILL), None),
            (("wait", 10), timeout()),
        ])

        def consume(stream):
            self.timers[0].firing = True
            raise ValueError("truncated")

        _, raised = self.call("stream", "tart-set", self.ARGV, consume)
        self.assertIs(type(raised), proc.TerminationUnresolved)
        self.assertEqual((raised.step, raised.reason), ("tart-set", UNRESOLVED_KILL))
        self.assertIs(self.runner.unresolved, raised)

    def test_stream_timer_known_clear_is_an_ordinary_timeout(self):
        self.boundary([
            (("poll",), None),
            (("killpg", 4242, signal.SIGTERM), None),
            (("wait", 10), -15),
            (("killpg", 4242, 0), esrch()),
        ])

        def consume(stream):
            self.timers[0].firing = True
            raise ValueError("truncated")

        _, raised = self.call("stream", "tart-set", self.ARGV, consume, timeout=30)
        self.assertIs(type(raised), CommandFailed)
        self.assertEqual((raised.step, raised.reason), ("tart-set", "timed out after 30 seconds"))
        self.assertIsNone(self.runner.kill_all())


if __name__ == "__main__":
    unittest.main()
