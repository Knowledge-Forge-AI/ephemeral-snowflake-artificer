"""Strict JSON loading for ESA manifests.

Refuses input that ordinary ``json.loads`` would accept but that makes a
manifest ambiguous: a byte-order mark, duplicate keys, NaN or Infinity, any
float, oversized integers, trailing data, and a non-object top level.
"""

from __future__ import annotations

import json

MAX_BYTES = 1 << 20
MAX_INT_DIGITS = 19


class StrictJSONError(ValueError):
    """The bytes are not a strict ESA JSON document."""


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise StrictJSONError("duplicate key %r" % key)
        out[key] = value
    return out


def _constant(name):
    raise StrictJSONError("non-finite number %s is not permitted" % name)


def _float(text):
    raise StrictJSONError("non-integer number %s is not permitted" % text)


def _int(text):
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise StrictJSONError("integer with more than %d digits" % MAX_INT_DIGITS)
    return int(text)


def loads(data: bytes) -> dict:
    if not isinstance(data, bytes):
        raise TypeError("loads() requires bytes")
    if len(data) > MAX_BYTES:
        raise StrictJSONError("document exceeds %d bytes" % MAX_BYTES)
    if data.startswith(b"\xef\xbb\xbf"):
        raise StrictJSONError("byte-order mark is not permitted")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJSONError("not UTF-8: %s" % exc) from None
    try:
        doc = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
            parse_int=_int,
        )
    except json.JSONDecodeError as exc:
        raise StrictJSONError("invalid JSON: %s" % exc) from None
    if not isinstance(doc, dict):
        raise StrictJSONError("top level must be a JSON object")
    return doc


def load_path(path: str) -> dict:
    """Read and parse ``path``. ``OSError`` propagates to the caller."""
    with open(path, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    return loads(data)
