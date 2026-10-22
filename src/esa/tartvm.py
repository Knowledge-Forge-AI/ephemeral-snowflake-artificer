"""Tart standalone VM directories (``tart-vm-dir-v1``).

The layout and ``config.json`` keys follow Tart 2.40.1 (``VMConfig.swift``,
``VMDirectory.swift``): a standalone VM is exactly ``config.json``,
``disk.img`` and ``nvram.bin``; memory is in bytes; raw disks are sized in
decimal GB by Tart itself. Stacked, ASIF and content-store layouts are not
supported, and a suspended VM (``state.vzvmsave``) is refused.

Two readers exist. ``parse_config`` is strict and is used for anything ESA
records or compares. ``parse_ingress_config`` models the defaults Tart applies
when decoding an older upstream ``config.json`` and is used only to inspect a
pulled base before ESA lets Tart rewrite it.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat

from esa import manifest
from esa import strictjson
from esa.errors import Refusal, UsageError

PAYLOAD_PATHS = manifest.PAYLOAD_PATHS
CHUNK = 8 << 20
GPT_SIGNATURE = b"EFI PART"
GPT_OFFSET = 512

FAMILY_OS = {"nixos-minimal": "linux", "tahoe-vanilla": "darwin"}

_COMMON_REQUIRED = (
    "version", "os", "arch", "cpuCountMin", "cpuCount",
    "memorySizeMin", "memorySize", "macAddress", "display", "diskFormat",
)
_DARWIN_KEYS = ("ecid", "hardwareModel")
_OPTIONAL = ("displayRefit",)
# Defaults Tart's decoder applies to keys an older config may omit.
_INGRESS_DEFAULTS = {"os": "darwin", "arch": "arm64", "diskFormat": "raw"}

MAC_RE = r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}"
B64_RE = r"[A-Za-z0-9+/]{4,4096}={0,2}"


def _bad(pointer, message):
    return Refusal([("config.json" + pointer, message)])


def _int(value, pointer, low=0, high=manifest.MAX_BYTES_VALUE):
    if type(value) is not int or not low <= value <= high:
        raise _bad(pointer, "must be an integer between %d and %d" % (low, high))


def _display(value):
    if not isinstance(value, dict):
        raise _bad("/display", "must be an object")
    extra = set(value) - {"width", "height", "unit"}
    if extra or not {"width", "height"} <= set(value):
        raise _bad("/display", "must hold width, height and optional unit only")
    _int(value["width"], "/display/width", 1, 1 << 16)
    _int(value["height"], "/display/height", 1, 1 << 16)
    if "unit" in value and value["unit"] not in ("pt", "px"):
        raise _bad("/display/unit", "must be 'pt' or 'px'")


def _check(cfg: dict, ingress: bool) -> dict:
    if not isinstance(cfg, dict):
        raise _bad("", "must be a JSON object")
    cfg = dict(cfg)
    if ingress:
        for key, default in _INGRESS_DEFAULTS.items():
            cfg.setdefault(key, default)
        cfg.setdefault("display", {"width": 1024, "height": 768})
    os_name = cfg.get("os")
    if os_name not in ("linux", "darwin"):
        raise _bad("/os", "must be 'linux' or 'darwin'")
    required = _COMMON_REQUIRED + (_DARWIN_KEYS if os_name == "darwin" else ())
    allowed = set(required) | set(_OPTIONAL)
    for key in sorted(cfg):
        if key not in allowed:
            if key in _DARWIN_KEYS:
                raise _bad("/" + key, "is a macOS-only key in a linux config")
            raise _bad("/" + key, "unknown config.json key for Tart 2.40.1 standalone VMs")
    for key in required:
        if key not in cfg:
            raise _bad("/" + key, "required config.json key is missing")
    if cfg["version"] != 1 or type(cfg["version"]) is not int:
        raise _bad("/version", "must be 1")
    if cfg["arch"] != manifest.ARCH:
        raise _bad("/arch", "must be 'arm64'")
    if cfg["diskFormat"] != "raw":
        raise _bad("/diskFormat", "must be 'raw'; ASIF and stacked disks are not supported")
    for key in ("cpuCountMin", "cpuCount"):
        _int(cfg[key], "/" + key, 1, 1024)
    for key in ("memorySizeMin", "memorySize"):
        _int(cfg[key], "/" + key, 1)
    if not isinstance(cfg["macAddress"], str) or not re.fullmatch(MAC_RE, cfg["macAddress"]):
        raise _bad("/macAddress", "must be a colon-separated MAC address")
    _display(cfg["display"])
    if "displayRefit" in cfg and type(cfg["displayRefit"]) is not bool:
        raise _bad("/displayRefit", "must be a JSON boolean")
    if os_name == "darwin":
        for key in _DARWIN_KEYS:
            if not isinstance(cfg[key], str) or not re.fullmatch(B64_RE, cfg[key]):
                raise _bad("/" + key, "must be a base64 string")
    return cfg


def parse_config(data: bytes) -> dict:
    """Strict reader for a config.json Tart has written in full form."""
    try:
        doc = strictjson.loads(data)
    except strictjson.StrictJSONError as exc:
        raise _bad("", "not strict JSON: %s" % exc) from None
    return _check(doc, ingress=False)


def parse_ingress_config(data: bytes) -> dict:
    """Reader for a pulled upstream base, applying Tart's decoding defaults."""
    try:
        doc = strictjson.loads(data)
    except strictjson.StrictJSONError as exc:
        raise _bad("", "not strict JSON: %s" % exc) from None
    return _check(doc, ingress=True)


def compare_config(cfg: dict, family: str, resources: dict) -> list:
    """Errors where a parsed config disagrees with the profile."""
    errors = []
    want_os = FAMILY_OS[family]
    if cfg["os"] != want_os:
        errors.append(("config.json/os", "is %r but family %s requires %r" % (cfg["os"], family, want_os)))
    if cfg["cpuCount"] != resources["cpu_count"]:
        errors.append(("config.json/cpuCount", "does not equal resources.cpu_count"))
    if cfg["memorySize"] != manifest.parse_memory(resources["memory"]):
        errors.append(("config.json/memorySize", "does not equal resources.memory in bytes"))
    if cfg["os"] == "darwin":
        if cfg["cpuCount"] < cfg["cpuCountMin"]:
            errors.append(("config.json/cpuCount", "is below cpuCountMin"))
        if cfg["memorySize"] < cfg["memorySizeMin"]:
            errors.append(("config.json/memorySize", "is below memorySizeMin"))
    return errors


def has_gpt(path: str) -> bool:
    with open(path, "rb") as handle:
        handle.seek(GPT_OFFSET)
        return handle.read(len(GPT_SIGNATURE)) == GPT_SIGNATURE


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def hash_file(path: str, boundaries=None):
    """Stream sha256 over ``path``; refuse if the file changes meanwhile.

    ``boundaries`` is an optional list of chunk sizes; their digests are
    returned too (used to check a pulled disk against OCI layer digests); the
    caller compares the file size with their sum separately.
    Returns ``(sha256_tag, size, chunk_tags)``.
    """
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise Refusal([(path, "must be a regular file")])
    whole = hashlib.sha256()
    chunks = []
    with open(path, "rb") as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise Refusal([(os.path.basename(path), "changed while it was opened")])
        if boundaries is None:
            while True:
                block = handle.read(CHUNK)
                if not block:
                    break
                whole.update(block)
        else:
            for size in boundaries:
                part = hashlib.sha256()
                left = size
                while left:
                    block = handle.read(min(CHUNK, left))
                    if not block:
                        break
                    part.update(block)
                    whole.update(block)
                    left -= len(block)
                chunks.append("sha256:" + part.hexdigest())
            while True:
                block = handle.read(CHUNK)
                if not block:
                    break
                whole.update(block)
    if _identity(os.lstat(path)) != _identity(before):
        raise Refusal([(os.path.basename(path), "changed while it was being hashed")])
    return "sha256:" + whole.hexdigest(), before.st_size, chunks


def assert_stopped(directory: str) -> None:
    """Refuse a VM directory that a Tart process appears to hold open.

    Tart's running lock is a POSIX record lock on config.json (PIDLock). A
    shared lock attempt that fails means some process holds it. Success is
    evidence of "not running now", not a proof of custody.
    """
    if os.path.lexists(os.path.join(directory, "state.vzvmsave")):
        raise Refusal([("vm-dir", "holds a suspended state (state.vzvmsave); a seed must be stopped")])
    fd = os.open(os.path.join(directory, "config.json"), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise Refusal([("vm-dir", "config.json is locked; the VM appears to be running")]) from None
            raise
        fcntl.lockf(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def list_vm_dir(directory: str) -> dict:
    """``{name: lstat}`` for a real directory holding exactly the payload files."""
    try:
        st = os.lstat(directory)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (directory, exc.strerror or exc)) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refusal([("vm-dir", "must be a real directory, not a symlink or file")])
    names = sorted(os.listdir(directory))
    errors = [("vm-dir/" + n, "unexpected entry in VM directory") for n in names if n not in PAYLOAD_PATHS]
    out = {}
    for name in PAYLOAD_PATHS:
        if name not in names:
            errors.append(("vm-dir/" + name, "missing payload file"))
            continue
        entry = os.lstat(os.path.join(directory, name))
        if not stat.S_ISREG(entry.st_mode):
            errors.append(("vm-dir/" + name, "must be a regular file, not a symlink or other type"))
            continue
        out[name] = entry
    if errors:
        raise Refusal(errors)
    return out


class Scan:
    """Hashes and parsed config of one stopped VM directory."""

    def __init__(self, files, config, allocated, disk_sha256):
        self.files = files
        self.config = config
        self.host_allocated_bytes = allocated
        self.disk_sha256 = disk_sha256

    @property
    def sizes(self):
        return {f["path"]: f["size_bytes"] for f in self.files}


def scan_vm_dir(directory: str, family: str, resources: dict) -> Scan:
    """Validate a built VM directory against the profile and hash it in full."""
    entries = list_vm_dir(directory)
    assert_stopped(directory)
    with open(os.path.join(directory, "config.json"), "rb") as handle:
        config_bytes = handle.read(strictjson.MAX_BYTES + 1)
    config = parse_config(config_bytes)
    errors = compare_config(config, family, resources)
    disk_bytes = manifest.parse_disk(resources["disk"])
    disk = os.path.join(directory, "disk.img")
    if entries["disk.img"].st_size != disk_bytes:
        errors.append(("vm-dir/disk.img", "logical size %d does not equal resources.disk (%d bytes)"
                       % (entries["disk.img"].st_size, disk_bytes)))
    elif not has_gpt(disk):
        errors.append(("vm-dir/disk.img", "has no GPT header; a raw EFI/GPT disk is required"))
    if errors:
        raise Refusal(errors)
    files = []
    for name in PAYLOAD_PATHS:
        tag, size, _ = hash_file(os.path.join(directory, name))
        files.append({"path": name, "size_bytes": size, "sha256": tag})
    if files[0]["sha256"] != "sha256:" + hashlib.sha256(config_bytes).hexdigest():
        raise Refusal([("vm-dir/config.json", "changed between parsing and hashing")])
    allocated = sum(os.lstat(os.path.join(directory, n)).st_blocks * 512 for n in PAYLOAD_PATHS)
    return Scan(files, config, allocated, files[1]["sha256"])


def instance_diff(seed_cfg: dict, inst_cfg: dict, changed: tuple) -> list:
    """Errors unless exactly the ``changed`` keys differ, and each one does."""
    errors = []
    for key in sorted(set(seed_cfg) | set(inst_cfg)):
        before, after = seed_cfg.get(key), inst_cfg.get(key)
        if key in changed:
            if before == after:
                errors.append(("config.json/" + key, "was expected to change and did not"))
        elif before != after:
            errors.append(("config.json/" + key, "changed outside the selected instance transformations"))
    return errors
