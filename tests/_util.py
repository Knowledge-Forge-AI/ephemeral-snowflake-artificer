"""Shared helpers for ESA tests. Standard library only."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
BIN = os.path.join(REPO, "bin", "esa")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

EXAMPLES = {
    "linux-profile": "examples/profiles/linux-ci.profile.json",
    "macos-profile": "examples/profiles/macos-gui.profile.json",
    "linux-artifact": "examples/artifacts/linux-ci.artifact.json",
    "macos-artifact": "examples/artifacts/macos-gui.artifact.json",
    "payload-profile": "tests/fixtures/payload-test.profile.json",
    "payload-artifact": "tests/fixtures/payload-test.artifact.json",
}

# Identities computed by an independent generator that does not import esa
# (see docs/specs/manifest-v1.md, "Identity"). Literal on purpose.
EXPECTED = {
    "linux-profile": "sha256:2f06a99574972e4fc946cb0aec8b8a1f437048492f6c8016f4ff13ce8266e0a0",
    "macos-profile": "sha256:9f1cd7ea5a9cb72bcd48747bc0406f6c172e6263b7b5021d5fea6eca3f30902a",
    "linux-artifact": "esa-artifact-v1:sha256:5b4583aa5862fbf6d6a3f45a1afea49185fbd59bb03e1d5c493daa5aca200d59",
    "macos-artifact": "esa-artifact-v1:sha256:31dbb33dba5e9beb52a88897dc8f712acca994fcb25ede152470f69da3ea500e",
    "payload-artifact": "esa-artifact-v1:sha256:2b7181d82932a65f294bb5e64a8ff1eb6207fa2a5d9c763ef32ee1e71a4a6502",
}
LINUX_PAYLOAD_DIGEST = "sha256:68d2b88fe52bff42a9828a104af6daade28b763a7582a6a575fe9830ff7125bd"
TEST_PAYLOAD_DIGEST = "sha256:fc3889db8ddfed9101ed0060809259e24b5f3cceeb6aa6048a7864e6ca24243d"

# Payload fixture bytes; tests/fixtures/payload-test.artifact.json records
# their sizes and digests. disk.img is 1 GiB of zeros, written sparse.
PAYLOAD_CONFIG = b'{"synthetic":"esa-test-config"}\n'
PAYLOAD_NVRAM = bytes(range(256)) * 16
PAYLOAD_DISK_BYTES = 1 << 30

REV = "0123456789abcdef0123456789abcdef01234567"


def example_path(name: str) -> str:
    return os.path.join(REPO, EXAMPLES[name])


def load(name: str) -> dict:
    with open(example_path(name), "rb") as handle:
        return copy.deepcopy(json.loads(handle.read()))


def write_json(directory: str, name: str, obj) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle)
    return path


def write_bytes(directory: str, name: str, data: bytes) -> str:
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def run_cli(*args):
    """Run the real launcher; returns (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, BIN] + [str(a) for a in args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO,
    )
    return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")


def write_payload_dir(directory: str) -> str:
    payload = os.path.join(directory, "seed.tvm")
    os.mkdir(payload)
    write_bytes(payload, "config.json", PAYLOAD_CONFIG)
    write_bytes(payload, "nvram.bin", PAYLOAD_NVRAM)
    with open(os.path.join(payload, "disk.img"), "wb") as handle:
        handle.truncate(PAYLOAD_DISK_BYTES)
    return payload


def concat(*parts: str) -> str:
    """Assemble refused-material samples so the repository holds no literals."""
    return "".join(parts)
