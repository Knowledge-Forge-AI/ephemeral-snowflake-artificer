"""Minimal reader for Nix archive (NAR) streams from ``nix store dump-path``.

It accepts exactly one top-level directory whose entries are an expected
closed set of regular, non-executable files. Each file is written
exclusively into a new directory, leaving holes for zero blocks, while its
sha256 is computed. Symlinks, subdirectories, unexpected names, unsorted
entries and malformed framing are refused. Contents never reach memory as a
whole, so a multi-gigabyte disk image streams through.
"""

from __future__ import annotations

import hashlib
import os
import struct

from esa.errors import Refusal

MAGIC = b"nix-archive-1"
MAX_TOKEN = 4096
BLOCK = 1 << 20


def _bad(message):
    return Refusal([("nar", message)])


class _Reader:
    def __init__(self, stream):
        self.stream = stream

    def exact(self, n: int) -> bytes:
        parts = []
        while n:
            block = self.stream.read(min(n, BLOCK))
            if not block:
                raise _bad("truncated stream")
            parts.append(block)
            n -= len(block)
        return b"".join(parts)

    def u64(self) -> int:
        return struct.unpack("<Q", self.exact(8))[0]

    def pad(self, n: int) -> None:
        extra = (8 - n % 8) % 8
        if extra and self.exact(extra) != b"\0" * extra:
            raise _bad("non-zero padding")

    def token(self) -> bytes:
        n = self.u64()
        if n > MAX_TOKEN:
            raise _bad("framing string too long")
        data = self.exact(n)
        self.pad(n)
        return data

    def expect(self, *values: bytes) -> None:
        for value in values:
            if self.token() != value:
                raise _bad("unexpected framing; expected %r" % value.decode("ascii"))


def _write_contents(reader, path: str, size: int):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        left = size
        while left:
            block = reader.stream.read(min(BLOCK, left))
            if not block:
                raise _bad("truncated file contents")
            digest.update(block)
            if block.count(0) == len(block):
                os.lseek(fd, len(block), os.SEEK_CUR)
            else:
                os.write(fd, block)
            left -= len(block)
        os.ftruncate(fd, size)
    finally:
        os.close(fd)
    reader.pad(size)
    return "sha256:" + digest.hexdigest()


def extract(stream, dest: str, expected: dict) -> dict:
    """Extract into the new directory ``dest``.

    ``expected`` maps each required entry name to its maximum size. Returns
    ``{name: (size, sha256_tag)}``.
    """
    reader = _Reader(stream)
    reader.expect(MAGIC, b"(", b"type", b"directory")
    os.mkdir(dest, 0o700)
    found = {}
    previous = None
    while True:
        tag = reader.token()
        if tag == b")":
            break
        if tag != b"entry":
            raise _bad("expected a directory entry")
        reader.expect(b"(", b"name")
        raw = reader.token()
        name = raw.decode("ascii", "replace")
        if previous is not None and raw <= previous:
            raise _bad("entries are not strictly sorted")
        previous = raw
        if name not in expected:
            raise _bad("unexpected entry %r" % name)
        reader.expect(b"node", b"(", b"type")
        kind = reader.token()
        if kind != b"regular":
            raise _bad("entry %r is not a regular file" % name)
        field = reader.token()
        if field == b"executable":
            raise _bad("entry %r is executable" % name)
        if field != b"contents":
            raise _bad("expected contents for %r" % name)
        size = reader.u64()
        if size > expected[name]:
            raise _bad("entry %r exceeds %d bytes" % (name, expected[name]))
        found[name] = (size, _write_contents(reader, os.path.join(dest, name), size))
        reader.expect(b")", b")")
    if stream.read(1):
        raise _bad("trailing data after the archive")
    missing = sorted(set(expected) - set(found))
    if missing:
        raise _bad("missing entries %s" % ", ".join(missing))
    return found
