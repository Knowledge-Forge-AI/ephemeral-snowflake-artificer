"""ESA-owned run directories.

A run directory is created absent-only (mode 0700, ownership marker) and
holds ``state.json``, per-step logs, evidence, a private empty ``HOME`` and
scratch space. It is retained after success and failure so the outcome can
be reviewed; ESA removes only the owned sub-homes (such as a ``TART_HOME``)
it created inside it. A run is single-use: a construction step that started
can never be replayed from the same run directory.
"""

from __future__ import annotations

import json
import os

from esa import custody
from esa import strictjson
from esa.errors import Refusal, UsageError

RUN_SCHEMA = "esa-run-v1"


class Run:
    def __init__(self, path: str, token: str):
        self.path = os.path.abspath(path)
        self.token = token

    @classmethod
    def create(cls, path: str, pointer: str = "--run-dir") -> "Run":
        token = custody.create_owned_dir(path, pointer)
        run = cls(path, token)
        for sub in ("logs", "evidence", "private", "private/home", "tmp"):
            os.mkdir(run.sub(sub), 0o700)
        return run

    @classmethod
    def open(cls, path: str) -> "Run":
        token = custody.read_marker(path)
        if token is None:
            raise Refusal([("--run-dir", "is not an ESA-owned run directory")])
        custody.check_private_dir(path, "--run-dir")
        return cls(path, token)

    def sub(self, *parts) -> str:
        return os.path.join(self.path, *parts)

    @property
    def private_home(self) -> str:
        return self.sub("private", "home")

    @property
    def tmp(self) -> str:
        return self.sub("tmp")

    def write_state(self, doc: dict) -> None:
        doc = dict(doc)
        doc["run_schema"] = RUN_SCHEMA
        doc["owner_nonce"] = self.token
        data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
        tmp = self.sub(".state.json.%s" % custody.nonce())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.sub("state.json"))

    def read_state(self, family: str) -> dict:
        try:
            with open(self.sub("state.json"), "rb") as handle:
                doc = strictjson.loads(handle.read(strictjson.MAX_BYTES + 1))
        except OSError as exc:
            raise UsageError("--run-dir: cannot read state.json: %s" % (exc.strerror or exc)) from None
        except strictjson.StrictJSONError as exc:
            raise Refusal([("--run-dir/state.json", str(exc))]) from None
        if doc.get("run_schema") != RUN_SCHEMA or doc.get("owner_nonce") != self.token:
            raise Refusal([("--run-dir/state.json", "is not this run's ESA state")])
        if doc.get("family") != family:
            raise Refusal([("--run-dir/state.json", "was prepared for another family")])
        return doc

    def begin(self, doc: dict, expected_step: str, step: str) -> dict:
        """Advance the one-shot step marker; refuse a replay."""
        if doc.get("step") != expected_step:
            raise Refusal([("--run-dir", "is at step %r, not %r; a run is single-use"
                            % (doc.get("step"), expected_step))])
        doc = dict(doc)
        doc["step"] = step
        self.write_state(doc)
        return doc
