"""ESA-REC-01: record an artifact manifest for a built, stopped VM directory.

``record`` derives every identity-bearing value from the profile, the
declared builders and the hashed payload. ``validation`` entries come only
from evidence files bound to the recorded ``disk.img``; ``record`` itself
never creates a seed, boot, readiness or qualification result. A recorded
artifact is not admission and not native qualification.
"""

from __future__ import annotations

import datetime
import json
import os
import re

from esa import SCHEMA
from esa import evidence
from esa import identity
from esa import manifest
from esa import strictjson
from esa import tartvm
from esa.errors import Refusal, UsageError

NIXPKGS = "https://github.com/NixOS/nixpkgs"


def default_licenses(profile: dict) -> list:
    """Upstream licensing references for the payload, by family.

    These describe the payload's terms. ESA's own licence is not the payload
    licence, and no entry asserts that an inspected recipe produced a base.
    """
    inputs = profile["inputs"]
    rev = inputs["nixpkgs"]["rev"]
    rows = [
        ("nixpkgs", "MIT", "%s/blob/%s/COPYING" % (NIXPKGS, rev)),
        ("nixpkgs.packages", "LicenseRef-Nixpkgs-Package-Meta", "%s/tree/%s" % (NIXPKGS, rev)),
    ]
    if profile["family"] == "tahoe-vanilla":
        rows += [
            ("macos", "LicenseRef-Apple-macOS-SLA", "https://www.apple.com/legal/sla/"),
            ("macos-tahoe-vanilla", "LicenseRef-Upstream-Image-Terms",
             "https://github.com/cirruslabs/macos-image-templates"),
            ("nix", "LGPL-2.1-or-later",
             "https://github.com/NixOS/nix/blob/%s/COPYING" % inputs["nix_installer"]["version"]),
            ("nix-darwin", "MIT",
             "https://github.com/nix-darwin/nix-darwin/blob/%s/LICENSE" % inputs["nix_darwin"]["rev"]),
        ]
    return [{"component": c, "license": l, "reference": r} for c, l, r in sorted(rows)]


def load_extra_licenses(path: str) -> list:
    try:
        with open(path, "rb") as handle:
            data = handle.read(strictjson.MAX_BYTES + 1)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (path, exc.strerror or exc)) from None
    try:
        doc = strictjson.loads(b'{"rows":' + data + b"}")
    except strictjson.StrictJSONError as exc:
        raise Refusal([("--extra-licenses", "not a strict JSON array: %s" % exc)]) from None
    rows = doc["rows"]
    if not isinstance(rows, list):
        raise Refusal([("--extra-licenses", "must be a JSON array of {component, license, reference}")])
    return rows


def parse_builders(values: list) -> dict:
    out = {}
    for value in values:
        name, sep, version = value.partition("=")
        if not sep or name not in manifest.BUILDER_NAMES:
            raise Refusal([("--builder", "must be NAME=VERSION with NAME one of %s"
                            % ", ".join(manifest.BUILDER_NAMES))])
        if name in out:
            raise Refusal([("--builder", "names %s more than once" % name)])
        if not re.fullmatch(manifest.BUILDER_VERSION_RE, version):
            raise Refusal([("--builder", "%s version must be an exact MAJOR.MINOR.PATCH" % name)])
        out[name] = version
    missing = [n for n in manifest.BUILDERS_REQUIRED if n not in out]
    if missing:
        raise Refusal([("--builder", "must include %s" % ", ".join(missing))])
    return out


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def build_artifact(profile: dict, scan: tartvm.Scan, esa_revision: str, builders: dict,
                   evidence_paths=(), extra_licenses=(), now=None) -> dict:
    if not re.fullmatch(manifest.REV_RE, esa_revision):
        raise Refusal([("--esa-revision", "must be a 40-character lowercase hex commit")])
    validation = []
    for path in evidence_paths:
        doc, tag = evidence.load(path)
        if doc["binds"]["disk_sha256"] != scan.disk_sha256:
            raise Refusal([("evidence:" + os.path.basename(path),
                            "is bound to a different disk.img than the one being recorded")])
        validation.append({"check": doc["check"], "result": doc["result"], "evidence_sha256": tag})
    now = now or _utc_now()
    licenses = default_licenses(profile)
    seen = {row["component"] for row in licenses}
    for row in extra_licenses:
        if isinstance(row, dict) and row.get("component") in seen:
            raise Refusal([("--extra-licenses", "repeats component %r" % row.get("component"))])
        licenses.append(row)
    disk_bytes = manifest.parse_disk(profile["resources"]["disk"])
    files = scan.files
    artifact = {
        "schema": SCHEMA,
        "kind": "artifact",
        "label": profile["label"],
        "family": profile["family"],
        "arch": profile["arch"],
        "platform": profile["platform"],
        "artifact_id": "",
        "esa_revision": esa_revision,
        "profile": {
            "label": profile["label"],
            "revision": profile["revision"],
            "content_sha256": identity.profile_content_hash(profile),
        },
        "builders": [{"name": n, "version": builders[n]} for n in sorted(builders)],
        "payload": {
            "format": manifest.PAYLOAD_FORMAT,
            "files": files,
            "digest": identity.payload_digest(files),
        },
        "storage": {
            "disk_logical_bytes": disk_bytes,
            "payload_bytes": sum(f["size_bytes"] for f in files),
        },
        "observations": [{
            "report_id": "esa-record-" + now.strftime("%Y%m%dt%H%M%Sz"),
            "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metrics": {"host_allocated_bytes": scan.host_allocated_bytes},
        }],
        "validation": validation,
        "upstream_licenses": licenses,
    }
    for field in manifest.COPIED_FIELDS:
        artifact[field] = json.loads(json.dumps(profile[field]))
    artifact["artifact_id"] = identity.artifact_id(artifact)
    errors = manifest.validate(artifact) + manifest.compare_with_profile(artifact, profile)
    if errors:
        raise Refusal(errors)
    return {k: artifact[k] for k in manifest.ARTIFACT_FIELDS}


def encode(artifact: dict) -> bytes:
    return (json.dumps(artifact, indent=2, sort_keys=False, ensure_ascii=True) + "\n").encode("ascii")


def write_exclusive(path: str, data: bytes, mode: int = 0o644) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        os.unlink(path)
        raise
