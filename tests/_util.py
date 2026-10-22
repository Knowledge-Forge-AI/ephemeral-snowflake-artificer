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
    "macos-profile": "sha256:de2392be873f0f30072bff3b2e9856e5ee19d50c0939c8f6ba61d24a10bfe7da",
    "linux-artifact": "esa-artifact-v1:sha256:5b4583aa5862fbf6d6a3f45a1afea49185fbd59bb03e1d5c493daa5aca200d59",
    "macos-artifact": "esa-artifact-v1:sha256:da154c9d39fb6f66a5d907fb7db16ab9a6565b630477b9e61ce40f2b22284899",
    "payload-artifact": "esa-artifact-v1:sha256:88c1005587e7c64f83d08170298f85d0454c48ea859f56dad40f1e0d638a639c",
}
LINUX_PAYLOAD_DIGEST = "sha256:68d2b88fe52bff42a9828a104af6daade28b763a7582a6a575fe9830ff7125bd"
TEST_PAYLOAD_DIGEST = "sha256:f632fbadea5047312c98f36b25b15fcc0ca841c597707ac8110b7234b3dc53bd"

# Payload fixture bytes; tests/fixtures/payload-test.artifact.json records
# their sizes and digests. config.json is a synthetic Tart 2.40.1 linux
# config; disk.img is 1 GiB, sparse, zero except a GPT signature at byte 512.
PAYLOAD_CONFIG = (
    b'{\n  "arch": "arm64",\n  "cpuCount": 4,\n  "cpuCountMin": 4,\n  "diskFormat": "raw",\n'
    b'  "display": {\n    "height": 768,\n    "width": 1024\n  },\n  "macAddress": "02:00:00:00:00:01",\n'
    b'  "memorySize": 8589934592,\n  "memorySizeMin": 4294967296,\n  "os": "linux",\n  "version": 1\n}\n'
)
PAYLOAD_NVRAM = bytes(range(256)) * 16
PAYLOAD_DISK_BYTES = 1 << 30
GPT_SIGNATURE = b"EFI PART"

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
    write_disk(os.path.join(payload, "disk.img"), PAYLOAD_DISK_BYTES)
    return payload


def write_disk(path: str, size: int, gpt: bool = True) -> None:
    """A sparse raw disk of ``size`` bytes, optionally carrying a GPT signature."""
    with open(path, "wb") as handle:
        handle.truncate(size)
        if gpt:
            handle.seek(512)
            handle.write(GPT_SIGNATURE)


def concat(*parts: str) -> str:
    """Assemble refused-material samples so the repository holds no literals."""
    return "".join(parts)
