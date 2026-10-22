"""Seed-check reports and the evidence files that artifact validation cites.

A builder or guest script prints an ``esa-check-v1`` report: the line
``esa-check-v1`` followed by unique ``key=value`` lines. The host never trusts
a script's own verdict; it judges each observed value against a closed
expectation table here. The judgment is written as an ``esa-evidence-v1``
JSON file whose sha256 becomes ``validation[].evidence_sha256``.

Every evidence file binds the exact ``disk.img`` it describes. ``esa record``
refuses evidence bound to any other disk, so stale or foreign evidence cannot
be attached to a payload. Evidence content is still the producer's report:
binding prevents reuse, it does not make a hand-written file true.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from esa import manifest
from esa import scan
from esa import strictjson
from esa.errors import Refusal

CHECK_HEADER = "esa-check-v1"
EVIDENCE_SCHEMA = "esa-evidence-v1"
KEY_RE = r"[a-z][a-z0-9_]{0,63}"
VALUE_RE = r"[A-Za-z0-9._:/+=,@-]{0,256}"
MAX_REPORT_BYTES = 64 << 10
MAX_LINES = 128
NONCE_RE = r"[0-9a-f]{32}"

BIND_KEYS = ("base_digest", "disk_sha256", "run_nonce")


def parse_report(data: bytes) -> dict:
    if len(data) > MAX_REPORT_BYTES:
        raise Refusal([("check-report", "exceeds %d bytes" % MAX_REPORT_BYTES)])
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Refusal([("check-report", "must be ASCII")]) from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != CHECK_HEADER:
        raise Refusal([("check-report", "must start with the line %r" % CHECK_HEADER)])
    if len(lines) > MAX_LINES:
        raise Refusal([("check-report", "has more than %d lines" % MAX_LINES)])
    out = {}
    for number, line in enumerate(lines[1:], start=2):
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(KEY_RE, key) or not re.fullmatch(VALUE_RE, value):
            raise Refusal([("check-report/line-%d" % number, "is not a key=value line in the closed character set")])
        if key in out:
            raise Refusal([("check-report/" + key, "duplicate key")])
        out[key] = value
    return out


# Predicates for expectation tables.

def equals(expected):
    return lambda v: v == expected


def int_between(low, high):
    def check(v):
        return re.fullmatch(r"0|[1-9][0-9]{0,18}", v) is not None and low <= int(v) <= high
    return check


def sha256_value(v):
    return re.fullmatch(manifest.SHA256_RE, v) is not None


def judge(observed: dict, table: dict) -> list:
    """Keys whose observed value fails, is missing, or is not expected."""
    failures = sorted(k for k, check in table.items() if k not in observed or not check(observed[k]))
    failures.extend(sorted("unexpected:" + k for k in observed if k not in table))
    return failures


def build(check: str, observed: dict, failures: list, binds: dict, producer: str) -> dict:
    for key in binds:
        if key not in BIND_KEYS:
            raise ValueError(key)
    return {
        "evidence_schema": EVIDENCE_SCHEMA,
        "check": check,
        "result": "fail" if failures else "pass",
        "failures": failures,
        "binds": dict(binds),
        "observed": dict(observed),
        "producer": producer,
    }


def encode(doc: dict) -> bytes:
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def write(path: str, doc: dict) -> str:
    """Write exclusively (mode 0600). Returns the file's sha256 tag."""
    data = encode(doc)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return _tag(data)


def _tag(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load(path: str):
    """Parse and check an evidence file. Returns ``(doc, sha256_tag)``."""
    with open(path, "rb") as handle:
        data = handle.read(strictjson.MAX_BYTES + 1)
    pointer = "evidence:" + os.path.basename(path)
    try:
        doc = strictjson.loads(data)
    except strictjson.StrictJSONError as exc:
        raise Refusal([(pointer, "not strict JSON: %s" % exc)]) from None
    keys = {"evidence_schema", "check", "result", "failures", "binds", "observed", "producer"}
    if set(doc) != keys or doc["evidence_schema"] != EVIDENCE_SCHEMA:
        raise Refusal([(pointer, "is not an %s document with exactly the defined fields" % EVIDENCE_SCHEMA)])
    errors = []
    if not isinstance(doc["check"], str) or len(doc["check"]) > manifest.TOKEN_MAX \
            or not re.fullmatch(manifest.TOKEN_RE, doc["check"]):
        errors.append((pointer + "/check", "must be a lowercase dotted token"))
    if doc["result"] not in ("pass", "fail"):
        errors.append((pointer + "/result", "must be 'pass' or 'fail'"))
    failures = doc["failures"]
    if not isinstance(failures, list) or not all(isinstance(f, str) for f in failures):
        errors.append((pointer + "/failures", "must be an array of strings"))
    elif (doc["result"] == "pass") != (not failures):
        errors.append((pointer + "/result", "contradicts failures"))
    binds = doc["binds"]
    if not isinstance(binds, dict) or "disk_sha256" not in binds or set(binds) - set(BIND_KEYS):
        errors.append((pointer + "/binds", "must bind disk_sha256 and only %s" % ", ".join(BIND_KEYS)))
    else:
        for key in ("base_digest", "disk_sha256"):
            if key in binds and not (isinstance(binds[key], str) and sha256_value(binds[key])):
                errors.append((pointer + "/binds/" + key, "must be a sha256 tag"))
        if "run_nonce" in binds and not (isinstance(binds["run_nonce"], str)
                                         and re.fullmatch(NONCE_RE, binds["run_nonce"])):
            errors.append((pointer + "/binds/run_nonce", "must be 32 lowercase hex digits"))
    observed = doc["observed"]
    if not isinstance(observed, dict) or not all(
            isinstance(k, str) and re.fullmatch(KEY_RE, k) and isinstance(v, str) and re.fullmatch(VALUE_RE, v)
            for k, v in observed.items()):
        errors.append((pointer + "/observed", "must map check keys to values in the closed character set"))
    if not isinstance(doc["producer"], str) or not re.fullmatch(r"[a-z][a-z0-9 .-]{0,63}", doc["producer"]):
        errors.append((pointer + "/producer", "must be a short lowercase producer name"))
    errors.extend((pointer + p, m) for p, m in scan.scan_document(doc))
    if errors:
        raise Refusal(errors)
    return doc, _tag(data)
