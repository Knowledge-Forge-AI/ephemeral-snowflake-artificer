"""Exceptions shared by the CLI and the backend modules.

Exit status mapping (see ``esa.cli``): ``Refusal`` and ``CommandFailed`` are
contract or construction refusals (1); ``UsageError`` is a usage or I/O
error (2).
"""

from __future__ import annotations


class Refusal(Exception):
    """A contract refusal carrying ``(pointer, message)`` errors."""

    def __init__(self, errors):
        super().__init__("contract refusal")
        self.errors = list(errors)


class UsageError(Exception):
    pass


class CommandFailed(Exception):
    """A bounded external step failed, timed out or produced refused output.

    ``log`` names the private per-step log; its contents are never echoed.
    """

    def __init__(self, step: str, reason: str, log: str = None):
        super().__init__("%s: %s" % (step, reason))
        self.step = step
        self.reason = reason
        self.log = log


def refuse(pointer: str, message: str):
    raise Refusal([(pointer, message)])
