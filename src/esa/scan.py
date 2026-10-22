"""Refuse secret-like, registration-like and host-specific material.

This is a guard against accidents in manifests and examples, not a security
boundary: it cannot prove that a payload is free of secrets. Findings name
the JSON pointer and category only; matched text is never echoed.

Several patterns are assembled from fragments so that repository hygiene
searches do not match this file.
"""

from __future__ import annotations

import re

_PEM = "-" * 5 + "BEGIN"
_PATTERNS = (
    ("private key block", re.compile(re.escape(_PEM) + r"[A-Z0-9 ]*" + "PRIVATE" + r" KEY")),
    (
        "SSH public key",
        re.compile(
            r"\b(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-nistp[0-9]+|sk-ssh-ed25519@openssh\.com)"
            r"\s+" + "AAAA"
        ),
    ),
    ("access token", re.compile(r"\b(?:gh[pousr])_[A-Za-z0-9]{8,}")),
    ("access token", re.compile(r"github" + r"_pat_")),
    ("access token", re.compile(r"glpat" + r"-")),
    ("access token", re.compile(r"\bxox[abprs]" + r"-")),
    ("access token", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "UUID-shaped identifier",
        re.compile(
            r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
        ),
    ),
    ("runner registration file", re.compile(r"(?:^|[/\s])\.runner\b")),
    ("host home path", re.compile(r"(?:/" + "Users/|/home/|/root/|~/)")),
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    (
        "credential assignment",
        re.compile(
            r"(?i)\b(?:token|password|passwd|secret|api[_-]?key|registration[_-]?token)\s*[=:]\s*\S"
        ),
    ),
)


def scan_text(text: str) -> list:
    """Return the sorted, de-duplicated categories that ``text`` matches."""
    return sorted({name for name, pattern in _PATTERNS if pattern.search(text)})


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def scan_document(doc, pointer: str = "") -> list:
    """Return ``(json_pointer, message)`` for every refused string value."""
    findings = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            findings.extend(scan_document(value, pointer + "/" + _escape(str(key))))
    elif isinstance(doc, list):
        for index, value in enumerate(doc):
            findings.extend(scan_document(value, "%s/%d" % (pointer, index)))
    elif isinstance(doc, str):
        for category in scan_text(doc):
            findings.append((pointer, "refused material: %s" % category))
    return findings
