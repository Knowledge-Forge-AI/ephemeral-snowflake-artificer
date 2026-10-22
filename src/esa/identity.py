"""Canonical encoding and the identities derived from ESA manifests.

See docs/specs/manifest-v1.md, "Identity". Callers must validate a document
before deriving identity from it; these functions do not validate.
"""

from __future__ import annotations

import copy
import hashlib
import json

ARTIFACT_ID_PREFIX = "esa-artifact-v1:sha256:"

# Fields of an artifact manifest that artifact_id binds. Everything else
# (label, artifact_id itself, storage, observations, validation and
# upstream_licenses) is descriptive or derived and excluded.
ARTIFACT_IDENTITY_FIELDS = (
    "schema",
    "family",
    "arch",
    "platform",
    "esa_revision",
    "profile",
    "inputs",
    "resources",
    "customization",
    "capabilities",
    "readiness",
    "seed",
    "builders",
    "payload",
)


def canonical_bytes(obj) -> bytes:
    """Sorted keys, no insignificant whitespace, ASCII-only escapes."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def sha256_tag(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def profile_content_hash(profile: dict) -> str:
    """Content hash of a profile; the mutable ``label`` is excluded."""
    subset = {k: v for k, v in profile.items() if k != "label"}
    return sha256_tag(canonical_bytes(subset))


def payload_digest(files: list) -> str:
    return sha256_tag(canonical_bytes(files))


def artifact_identity_subset(artifact: dict) -> dict:
    subset = {k: copy.deepcopy(artifact[k]) for k in ARTIFACT_IDENTITY_FIELDS}
    subset["profile"].pop("label", None)
    return subset


def artifact_id(artifact: dict) -> str:
    digest = hashlib.sha256(canonical_bytes(artifact_identity_subset(artifact)))
    return ARTIFACT_ID_PREFIX + digest.hexdigest()
