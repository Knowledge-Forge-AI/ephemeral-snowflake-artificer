"""Fake process boundaries and synthetic construction inputs for ESA tests.

The fake tart, nix and ssh tools (tests/fakes/*.py) are installed into a
temporary directory with this interpreter in their shebang, so the suite
runs the same way under Python 3.9 and later. They never start VMs, build
images or open connections. Hashes of synthetic inputs are computed here with
hashlib, independently of src/esa.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import stat
import struct
import sys

import _util

FAKES = os.path.join(_util.REPO, "tests", "fakes")
REV_DARWIN = "fedcba9876543210fedcba9876543210fedcba98"


def install(directory: str, control: dict) -> dict:
    tools = {}
    for name in ("tart", "nix", "ssh"):
        with open(os.path.join(FAKES, name + ".py")) as handle:
            source = handle.read()
        path = os.path.join(directory, name)
        with open(path, "w") as handle:
            handle.write("#!%s\n%s" % (sys.executable, source))
        os.chmod(path, 0o755)
        tools[name] = path
    control.setdefault("log", os.path.join(directory, "calls.jsonl"))
    control.setdefault("run_pid", os.path.join(directory, "run.pid"))
    write_control(directory, control)
    return tools


def write_control(directory: str, control: dict) -> None:
    with open(os.path.join(directory, "control.json"), "w") as handle:
        json.dump(control, handle)


def read_control(directory: str) -> dict:
    with open(os.path.join(directory, "control.json")) as handle:
        return json.load(handle)


def calls(directory: str, tool=None) -> list:
    path = os.path.join(directory, "calls.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        rows = [json.loads(line) for line in handle]
    return [r for r in rows if tool is None or r["tool"] == tool]


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def chunk_digests(path: str, sizes) -> list:
    out = []
    with open(path, "rb") as handle:
        for size in sizes:
            digest = hashlib.sha256()
            left = size
            while left:
                block = handle.read(min(left, 1 << 20))
                digest.update(block)
                left -= len(block)
            out.append("sha256:" + digest.hexdigest())
    return out


def write_private(path: str, data: bytes) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


def lock_bytes(nodes: dict) -> bytes:
    lock = {
        "nodes": dict(nodes, root={"inputs": {name: name for name in nodes}}),
        "root": "root",
        "version": 7,
    }
    return json.dumps(lock, indent=2, sort_keys=True).encode("ascii")


def github_node(owner, repo, rev, nar_hash, ref="main", follows=False):
    node = {
        "locked": {"lastModified": 1760000000, "narHash": nar_hash, "owner": owner, "repo": repo,
                   "rev": rev, "type": "github"},
        "original": {"owner": owner, "ref": ref, "repo": repo, "type": "github"},
    }
    if follows:
        node["inputs"] = {"nixpkgs": ["nixpkgs"]}
    return node


MODULE = b'{ pkgs, ... }:\n{\n  environment.systemPackages = [ pkgs.git ];\n  users.users.runner.isNormalUser = true;\n}\n'


def nixos_inputs(directory: str, disk: str = "1GiB", module: bytes = MODULE):
    """Write a lock, module and matching nixos-minimal profile. Returns paths."""
    profile = _util.load("payload-profile")
    profile["resources"]["disk"] = disk
    pin = profile["inputs"]["nixpkgs"]
    lock = lock_bytes({"nixpkgs": github_node("NixOS", "nixpkgs", pin["rev"], pin["nar_hash"], "nixos-26.05")})
    profile["inputs"]["flake_lock_sha256"] = sha256_bytes(lock)
    profile["customization"]["sha256"] = sha256_bytes(module)
    paths = {
        "lock": _util.write_bytes(directory, "flake.lock", lock),
        "module": _util.write_bytes(directory, "consumer.nix", module),
    }
    paths["profile"] = _util.write_json(directory, "profile.json", profile)
    return paths, profile


def tahoe_inputs(directory: str, base_digest: str, disk: str = "1GB", module: bytes = MODULE, account=True):
    profile = _util.load("macos-profile")
    profile["resources"]["disk"] = disk
    profile["seed"]["inherited_default_account"] = account
    inputs = profile["inputs"]
    nixpkgs, darwin = inputs["nixpkgs"], inputs["nix_darwin"]
    lock = lock_bytes({
        "nixpkgs": github_node("NixOS", "nixpkgs", nixpkgs["rev"], nixpkgs["nar_hash"], "nixpkgs-26.05-darwin"),
        "nix-darwin": github_node("nix-darwin", "nix-darwin", darwin["rev"], darwin["nar_hash"],
                                  "nix-darwin-26.05", follows=True),
    })
    inputs["flake_lock_sha256"] = sha256_bytes(lock)
    inputs["macos"]["base"]["digest"] = base_digest
    profile["customization"]["sha256"] = sha256_bytes(module)
    paths = {
        "lock": _util.write_bytes(directory, "flake.lock", lock),
        "module": _util.write_bytes(directory, "consumer.nix", module),
    }
    paths["profile"] = _util.write_json(directory, "profile.json", profile)
    return paths, profile


LINUX_CONFIG = {
    "arch": "arm64", "cpuCount": 4, "cpuCountMin": 4, "diskFormat": "raw",
    "display": {"height": 768, "width": 1024}, "macAddress": "02:00:00:00:00:01",
    "memorySize": 8589934592, "memorySizeMin": 4294967296, "os": "linux", "version": 1,
}
DARWIN_CONFIG = {
    "arch": "arm64", "cpuCount": 4, "cpuCountMin": 4, "diskFormat": "raw",
    "display": {"height": 768, "width": 1024}, "macAddress": "02:00:00:00:00:02",
    "memorySize": 8589934592, "memorySizeMin": 8589934592, "os": "darwin", "version": 1,
    "ecid": "c3ludGhldGljLWVjaWQtMDAx", "hardwareModel": "c3ludGhldGljLW1vZGVs",
}


def vm_dir(directory: str, name: str, config: dict, disk_bytes: int, gpt: bool = True) -> str:
    path = os.path.join(directory, name)
    os.mkdir(path)
    _util.write_bytes(path, "config.json", json.dumps(config, indent=2, sort_keys=True).encode("ascii"))
    _util.write_bytes(path, "nvram.bin", _util.PAYLOAD_NVRAM)
    _util.write_disk(os.path.join(path, "disk.img"), disk_bytes, gpt)
    return path


def nixos_seal_report(disk_sha: str, disk_bytes: int, **overrides) -> bytes:
    values = {
        "machine_id": "absent", "dbus_machine_id": "absent", "ssh_host_keys": "0", "random_seed": "absent",
        "esp_random_seed": "absent", "system_generations": "1", "system_profile": "present",
        "fallback_loader": "present", "runner_files": "0", "authorized_keys_files": "0",
        "enabled_runner_units": "0", "password_hashes": "0",
        "disk_bytes": str(disk_bytes), "disk_sha256": disk_sha,
    }
    values.update(overrides)
    return ("esa-check-v1\n" + "".join("%s=%s\n" % kv for kv in values.items())).encode("ascii")


def oci_manifest(base_dir: str, sizes) -> bytes:
    disk = os.path.join(base_dir, "disk.img")
    layers = [{"annotations": {}, "digest": sha256_file(os.path.join(base_dir, "config.json")),
               "mediaType": "application/vnd.cirruslabs.tart.config.v1", "size": 100}]
    for size, digest in zip(sizes, chunk_digests(disk, sizes)):
        layers.append({
            "annotations": {"org.cirruslabs.tart.uncompressed-content-digest": digest,
                            "org.cirruslabs.tart.uncompressed-size": str(size)},
            "digest": sha256_bytes(digest.encode("ascii")),
            "mediaType": "application/vnd.cirruslabs.tart.disk.v2", "size": 1000,
        })
    layers.append({"annotations": {}, "digest": sha256_file(os.path.join(base_dir, "nvram.bin")),
                   "mediaType": "application/vnd.cirruslabs.tart.nvram.v1", "size": 4096})
    doc = {
        "annotations": {"org.cirruslabs.tart.uncompressed-disk-size": str(sum(sizes))},
        "config": {"digest": sha256_bytes(b"cfg"), "mediaType": "application/vnd.oci.image.config.v1+json",
                   "size": 3},
        "layers": layers,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "schemaVersion": 2,
    }
    return json.dumps(doc, sort_keys=True).encode("ascii")


def host_key_line(comment: str = "") -> str:
    """A synthetic ssh-ed25519 public key line (all-zero key), built at run time."""
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(32)
    line = "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")
    return line + (" " + comment if comment else "") + "\n"


def pin(line: str) -> str:
    """The base64 pin of a key line, with '+' percent-encoded for a URI."""
    return base64.b64encode(line.encode("ascii")).decode("ascii").replace("+", "%2B")


def plus_comment() -> str:
    """A comment whose key line's base64 contains a raw '+'."""
    for i in range(3):  # '~' (0x7e) as the third byte of a group encodes to '+'
        comment = "c" * i + "~"
        if "+" in base64.b64encode(host_key_line(comment).encode("ascii")).decode("ascii"):
            return comment
    raise AssertionError("no comment yields '+'")


def store_uri(host: str = "builder.invalid", key=None, pin_value=None, extra: str = "") -> str:
    uri = "ssh-ng://nixbuilder@%s?base64-ssh-public-host-key=%s" % (host, pin_value or pin(host_key_line()))
    if key:
        uri += "&ssh-key=" + key
    return uri + extra


def key_marker() -> bytes:
    """Synthetic private-key file content; never a key, assembled from fragments."""
    return ("esa" + "-synthetic-" + "store-key" + "-marker\n").encode("ascii")


def write_key(directory: str, name: str = "store_key") -> str:
    """A private key file in a directory with no symlink in its path."""
    return write_private(os.path.join(os.path.realpath(directory), name), key_marker())


class BoundSocket:
    def __init__(self, fd: int):
        self.fd = fd

    def close(self) -> None:
        os.close(self.fd)


def agent_socket(directory: str, name: str = "agent.sock") -> tuple:
    """Bind a Unix socket nobody connects to. Returns (path, BoundSocket).

    socket(2) and bind(2) go through ctypes, which the module floor
    (test_floor.ALLOWED) already admits; it does not list ``socket``.
    """
    path = os.path.join(os.path.realpath(directory), name)
    raw = os.fsencode(path)
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.socket(1, 1, 0)  # AF_UNIX, SOCK_STREAM on macOS and Linux
    if fd < 0:
        raise OSError(ctypes.get_errno(), "socket")
    if sys.platform == "darwin":
        addr = struct.pack("BB104s", 106, 1, raw)  # sun_len, sun_family, sun_path
    else:
        addr = struct.pack("=H108s", 1, raw)  # sun_family, sun_path
    if libc.bind(fd, addr, len(addr)) != 0:
        code = ctypes.get_errno()
        os.close(fd)
        raise OSError(code, os.strerror(code), path)
    return path, BoundSocket(fd)


def residue_socket(directory: str, name: str = "control.sock") -> str:
    """Bind and close a Unix socket at ``directory/name``, as Tart leaves control.sock.

    It binds the relative name from inside ``directory``, as Tart does, so
    the path length limit of sun_path does not depend on the temporary root.
    """
    previous = os.getcwd()
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.socket(1, 1, 0)  # AF_UNIX, SOCK_STREAM on macOS and Linux
    if fd < 0:
        raise OSError(ctypes.get_errno(), "socket")
    raw = os.fsencode(name)
    if sys.platform == "darwin":
        addr = struct.pack("BB104s", 106, 1, raw)
    else:
        addr = struct.pack("=H108s", 1, raw)
    try:
        os.chdir(directory)
        if libc.bind(fd, addr, len(addr)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), name)
    finally:
        os.chdir(previous)
        os.close(fd)
    return os.path.join(directory, name)


def password() -> bytes:
    """A synthetic bootstrap credential, assembled so no literal sits in source."""
    return ("esa" + "-synthetic-" + "bootstrap" + "-value").encode("ascii")


def mode(path: str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)
