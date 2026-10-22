"""Verify a stored seed directory against an artifact manifest.

Verification applies to the stored, stopped seed, never to a clone or a
running instance. Besides exact sizes and hashes it applies the semantic
``config.json`` check (family OS, CPU and memory against the artifact's
copied resources) and, for a full check, the GPT header of ``disk.img``.
"""

from __future__ import annotations

import os
import stat

from esa import strictjson
from esa import tartvm
from esa.errors import Refusal, UsageError


def verify(artifact: dict, directory: str, quick: bool) -> list:
    try:
        st = os.lstat(directory)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (directory, exc.strerror or exc)) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return [("--payload-dir", "must be a real directory, not a symlink or file")]
    try:
        return _compare(artifact, directory, quick)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (exc.filename or directory, exc.strerror or exc)) from None


def _compare(artifact: dict, directory: str, quick: bool) -> list:
    expected = {f["path"]: f for f in artifact["payload"]["files"]}
    errors = []
    with os.scandir(directory) as entries:
        present = {entry.name: entry for entry in entries}
    for name in sorted(set(present) - set(expected)):
        errors.append(("--payload-dir/" + name, "unexpected entry in payload directory"))
    for name in sorted(expected):
        pointer = "--payload-dir/" + name
        entry = present.get(name)
        if entry is None:
            errors.append((pointer, "missing payload file"))
            continue
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            errors.append((pointer, "must be a regular file, not a symlink or other type"))
            continue
        size = entry.stat(follow_symlinks=False).st_size
        if size != expected[name]["size_bytes"]:
            errors.append((pointer, "size %d does not match manifest size_bytes %d"
                           % (size, expected[name]["size_bytes"])))
            continue
        if quick and name == "disk.img":
            continue
        try:
            tag, _, _ = tartvm.hash_file(os.path.join(directory, name))
        except Refusal as exc:
            errors.extend(exc.errors)
            continue
        if tag != expected[name]["sha256"]:
            errors.append((pointer, "sha256 does not match manifest"))
    if errors:
        return errors
    with open(os.path.join(directory, "config.json"), "rb") as handle:
        data = handle.read(strictjson.MAX_BYTES + 1)
    try:
        config = tartvm.parse_config(data)
    except Refusal as exc:
        return [("--payload-dir/" + p, m) for p, m in exc.errors]
    errors = [("--payload-dir/" + p, m)
              for p, m in tartvm.compare_config(config, artifact["family"], artifact["resources"])]
    if not quick and not tartvm.has_gpt(os.path.join(directory, "disk.img")):
        errors.append(("--payload-dir/disk.img", "has no GPT header"))
    return errors


def require(artifact: dict, directory: str, quick: bool, expect_artifact_id=None) -> None:
    """Refuse unless the seed matches; ``quick`` needs the receipt's artifact_id."""
    if quick and not expect_artifact_id:
        raise UsageError("a quick seed check requires --expect-artifact-id from the consumer's receipt")
    errors = []
    if expect_artifact_id and artifact["artifact_id"] != expect_artifact_id:
        errors.append(("/artifact_id", "does not match --expect-artifact-id"))
    errors.extend(verify(artifact, directory, quick))
    if errors:
        raise Refusal(errors)
