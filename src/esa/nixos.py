"""nixos-minimal construction: pinned Nix build, host-judged seal, Tart assembly.

The disk is built on a caller-selected aarch64-linux store (``--build-store``,
which must provide the ``kvm`` system feature). ESA disables ambient remote
builders, writes no host GC roots or out-links, and never changes the host
Nix configuration. The built disk and its seal report stream back through
``nix store dump-path`` into the run directory; the host judges the report,
binds it to the streamed disk's sha256, and only then assembles a Tart VM
directory in an owned ``TART_HOME`` and records the artifact.

Build-store SSH route. Nix 2.31.5 (``src/libstore/ssh.cc``) runs the first
``ssh`` on ``PATH`` (the ``--nix`` directory, then ``/usr/bin``) as
``ssh HOST -x $NIX_SSHOPTS [-i KEY] -oUserKnownHostsFile=PIN ...``, with
``NIX_SSHOPTS`` shell-split, the ``ssh-key`` query value as ``KEY`` and a
private known_hosts file holding the decoded ``base64-ssh-public-host-key``.
Otherwise ssh would use the invoking user's passwd-based configuration,
identities and agent, which a private ``HOME`` does not suppress. ESA
therefore:

- accepts only ``ssh-ng://`` or ``ssh://`` with a DNS or IPv4 host, no port
  (Nix pins the bare host while ssh looks up ``[host]:port``), no
  ``localhost`` (Nix runs that locally) and no loopback address;
- requires exactly one canonical single-line public host-key pin and at most
  one ``ssh-key``, and refuses every other query parameter and any duplicate;
- requires an explicit authentication route: the private ``ssh-key`` file
  (checked by ``lstat`` only, never opened), the ``--ssh-agent-socket``, or
  both. Without a selected socket no agent is reachable;
- sets ``NIX_SSHOPTS`` itself to static options (``SSH_OPTS``) that ignore
  user and system ssh configuration and global known_hosts, require strict
  checking against Nix's pin, forbid passwords, prompts and forwarding, and
  name no default identity. ESA sets no ``UserKnownHostsFile``: OpenSSH keeps
  the first value, which would displace Nix's pin. No caller or ambient
  ``NIX_SSHOPTS`` or ``SSH_AUTH_SOCK`` reaches Nix.

These are checked over fake process boundaries only; how the native Nix and
OpenSSH pair behaves is part of native qualification.
"""

from __future__ import annotations

import base64
import os
import re
import stat
import struct

from esa import backend
from esa import custody
from esa import evidence
from esa import manifest
from esa import nar
from esa import proc
from esa import record
from esa import tart as tartmod
from esa import tartvm
from esa.errors import CommandFailed, Refusal

FAMILY = "nixos-minimal"
NIX_FLAGS = ["--extra-experimental-features", "nix-command flakes"]
STORE_RE = r"(ssh-ng|ssh)://(?:([A-Za-z0-9_][A-Za-z0-9._-]{0,31})@)?([^?#]{1,253})\?([^#]{1,8192})"
LABEL_RE = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
IPV4_RE = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}"
PIN_PARAM = "base64-ssh-public-host-key"
KEY_PARAM = "ssh-key"
# Raw '+' is refused: whether a URI decoder reads it as '+' or a space is a
# convention, so the pin must spell it %2B.
PIN_VALUE_RE = r"(?:[A-Za-z0-9/=]|%2[BbFf]|%3[Dd]){4,8192}"
KEY_PATH_RE = r"/[A-Za-z0-9._/-]{1,1023}"
HOST_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
ED25519_BLOB_BYTES = 51
KEY_FILE_MAX = 16384
SOCKET_PATH_MAX = 103  # sun_path holds 104 bytes on macOS, including the NUL
OUT_RE = r"/nix/store/[0-9a-z]{32}-esa-seed-image"
NIX_VERSION_RE = r"nix \(Nix\) ([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})"
REPORT_MAX = 64 << 10

# NIX_SSHOPTS, shell-split by Nix and placed before its own -i and pinned
# -oUserKnownHostsFile. No token holds whitespace, quotes or backslashes, so
# any splitting yields the same argv. PermitLocalCommand, LocalCommand,
# ControlMaster and ControlPath are left to Nix, which relies on them.
SSH_OPTS = (
    "-F", "/dev/null",
    "-oBatchMode=yes",
    "-oStrictHostKeyChecking=yes",
    "-oGlobalKnownHostsFile=/dev/null",
    "-oUpdateHostKeys=no",
    "-oCheckHostIP=no",
    "-oIdentityFile=none",  # a non-empty identity list: OpenSSH adds no default key files
    "-oAddKeysToAgent=no",
    "-oPreferredAuthentications=publickey",
    "-oPasswordAuthentication=no",
    "-oKbdInteractiveAuthentication=no",
    "-oHostbasedAuthentication=no",
    "-oGSSAPIAuthentication=no",
    "-oForwardAgent=no",
    "-oForwardX11=no",
    "-oClearAllForwardings=yes",
)
SSH_OPTS_NO_AGENT = ("-oIdentityAgent=none", "-oIdentitiesOnly=yes")
SSH_OPTS_AGENT = ("-oIdentityAgent=SSH_AUTH_SOCK", "-oIdentitiesOnly=no")


class StoreRoute:
    """A validated build-store URI and its explicit authentication route."""

    def __init__(self, uri, host, pin_text, key_path, agent_socket):
        self.uri = uri
        self.host = host
        self.pin_text = pin_text
        self.key_path = key_path
        self.agent_socket = agent_socket


def _store_refusal(message: str, pointer: str = "--build-store") -> Refusal:
    return Refusal([(pointer, message)])


def _check_host(host: str) -> None:
    if re.fullmatch(IPV4_RE, host):
        if host.startswith("127.") or host == "0.0.0.0":
            raise _store_refusal("host must not be a loopback or unspecified address")
        return
    labels = host.split(".")
    if not all(re.fullmatch(LABEL_RE, label) for label in labels) or re.fullmatch(r"[0-9.]+", host):
        raise _store_refusal("host must be a DNS name or dotted IPv4 address, without a port")
    if labels[-1].lower() == "localhost":
        raise _store_refusal("host must not be localhost; Nix runs a 'localhost' store locally, not over SSH")


def _ssh_string(data: bytes, offset: int):
    if offset + 4 > len(data):
        return None, offset
    (size,) = struct.unpack(">I", data[offset:offset + 4])
    end = offset + 4 + size
    if end > len(data):
        return None, offset
    return data[offset + 4:end], end


def _canonical_b64(text: str):
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if not raw or base64.b64encode(raw).decode("ascii") != text:
        return None
    return raw


def parse_pin(value: str) -> str:
    """Decode the raw query value of the host-key pin; return the decoded key line."""
    pointer = "--build-store/" + PIN_PARAM
    if not re.fullmatch(PIN_VALUE_RE, value):
        raise _store_refusal("must be base64 with '+', '/' and '=' optionally percent-encoded; "
                             "a raw '+' is ambiguous, write %2B", pointer)
    text = re.sub(r"%(2[BbFf]|3[Dd])", lambda m: chr(int(m.group(1), 16)), value)
    raw = _canonical_b64(text)
    if raw is None:
        raise _store_refusal("must be canonical base64", pointer)
    try:
        line = raw.decode("ascii")
    except UnicodeDecodeError:
        raise _store_refusal("must decode to one ASCII public key line", pointer) from None
    match = re.fullmatch(r"([a-z0-9-]{1,32}) ([A-Za-z0-9+/=]{1,4096})(?: ([!-~]{1,256}))?\n?", line)
    if match is None:
        raise _store_refusal("must decode to exactly one 'TYPE BASE64 [COMMENT]' public key line", pointer)
    key_type, blob_text = match.group(1), match.group(2)
    if key_type not in HOST_KEY_TYPES:
        raise _store_refusal("key type must be one of %s" % ", ".join(HOST_KEY_TYPES), pointer)
    blob = _canonical_b64(blob_text)
    named, end = _ssh_string(blob or b"", 0)
    if blob is None or named != key_type.encode("ascii") or end == len(blob):
        raise _store_refusal("key blob does not encode a %s public key" % key_type, pointer)
    if key_type == "ssh-ed25519" and len(blob) != ED25519_BLOB_BYTES:
        raise _store_refusal("an ssh-ed25519 key blob must be exactly %d bytes" % ED25519_BLOB_BYTES, pointer)
    return line


def check_key_file(path: str) -> None:
    """The selected private key: a private regular file. Never opened or read."""
    pointer = "--build-store/" + KEY_PARAM
    if not re.fullmatch(KEY_PATH_RE, path):
        raise _store_refusal("must be an absolute path of letters, digits, '.', '_', '-' and '/'", pointer)
    if os.path.normpath(path) != path or os.path.realpath(path) != path:
        raise _store_refusal("must be a normalized path with no symlink in any component", pointer)
    try:
        st = os.lstat(path)
        parent = os.lstat(os.path.dirname(path))
    except OSError as exc:
        raise _store_refusal("cannot be examined: %s" % (exc.strerror or exc), pointer) from None
    uid = os.geteuid()
    if not stat.S_ISREG(st.st_mode):
        raise _store_refusal("must be a regular file, not a symlink or other type", pointer)
    if st.st_uid != uid:
        raise _store_refusal("must be owned by the current user", pointer)
    if st.st_mode & 0o077 or not st.st_mode & stat.S_IRUSR:
        raise _store_refusal("must be readable by its owner only (mode 0600 or 0400)", pointer)
    if st.st_nlink != 1:
        raise _store_refusal("must have exactly one hard link", pointer)
    if not 1 <= st.st_size <= KEY_FILE_MAX:
        raise _store_refusal("must be between 1 and %d bytes" % KEY_FILE_MAX, pointer)
    if parent.st_uid not in (uid, 0) or parent.st_mode & 0o022:
        raise _store_refusal("its directory must be owned by the current user or root and not writable "
                             "by group or others", pointer)


def check_agent_socket(path: str) -> None:
    """The explicitly selected agent socket. ESA never connects to it."""
    pointer = "--ssh-agent-socket"
    if not os.path.isabs(path) or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise _store_refusal("must be an absolute path without control characters", pointer)
    if len(os.fsencode(path)) > SOCKET_PATH_MAX:
        raise _store_refusal("must be at most %d bytes" % SOCKET_PATH_MAX, pointer)
    if os.path.normpath(path) != path or os.path.realpath(path) != path:
        raise _store_refusal("must be a normalized path with no symlink in any component", pointer)
    try:
        st = os.lstat(path)
        parent = os.lstat(os.path.dirname(path))
    except OSError as exc:
        raise _store_refusal("cannot be examined: %s" % (exc.strerror or exc), pointer) from None
    uid = os.geteuid()
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != uid:
        raise _store_refusal("must be a socket owned by the current user", pointer)
    private_parent = parent.st_uid == uid and not parent.st_mode & 0o077
    if st.st_mode & 0o077 and not private_parent:
        raise _store_refusal("must not be accessible by group or others unless its directory is private "
                             "to the current user", pointer)


def parse_build_store(uri: str, agent_socket=None) -> StoreRoute:
    """Refuse unless ``uri`` and the agent choice form one explicit, pinned SSH route."""
    match = re.fullmatch(STORE_RE, uri)
    if match is None:
        raise _store_refusal("must be ssh-ng://[USER@]HOST?%s=PIN[&%s=PATH] for an aarch64-linux builder "
                             "with the kvm system feature" % (PIN_PARAM, KEY_PARAM))
    host, query = match.group(3), match.group(4)
    _check_host(host)
    values = {}
    for item in query.split("&"):
        name, sep, value = item.partition("=")
        if not sep or not value:
            raise _store_refusal("query item %r must be NAME=VALUE with a non-empty value" % name[:64])
        if name not in (PIN_PARAM, KEY_PARAM):
            raise _store_refusal("query parameter %r is not accepted; only %s and %s are"
                                 % (name[:64], PIN_PARAM, KEY_PARAM))
        if name in values:
            raise _store_refusal("query parameter %s is given more than once" % name)
        values[name] = value
    if PIN_PARAM not in values:
        raise _store_refusal("must pin the builder's public host key with %s" % PIN_PARAM)
    pin_text = parse_pin(values[PIN_PARAM])
    key_path = values.get(KEY_PARAM)
    if key_path is None and agent_socket is None:
        raise _store_refusal("names no explicit authentication: give %s=PATH, --ssh-agent-socket, or both"
                             % KEY_PARAM)
    if key_path is not None:
        check_key_file(key_path)
    if agent_socket is not None:
        check_agent_socket(agent_socket)
    return StoreRoute(uri, host, pin_text, key_path, agent_socket)


def nix_version(text: str) -> str:
    match = re.fullmatch(NIX_VERSION_RE, text.strip())
    if match is None:
        raise CommandFailed("nix-version", "did not report an exact MAJOR.MINOR.PATCH Nix version")
    return match.group(1)


def ssh_opts(route: StoreRoute) -> str:
    agent = SSH_OPTS_AGENT if route.agent_socket is not None else SSH_OPTS_NO_AGENT
    return " ".join(SSH_OPTS + agent)


def nix_env(run, nix_path: str, route: StoreRoute) -> dict:
    """The whole environment for Nix: nothing is inherited from the caller."""
    private = run.private_home
    env = {
        "PATH": os.path.dirname(nix_path) + ":" + proc.BASE_PATH,
        "HOME": private,
        "TMPDIR": run.tmp,
        "XDG_CONFIG_HOME": os.path.join(private, ".config"),
        "XDG_CACHE_HOME": os.path.join(private, ".cache"),
        "XDG_DATA_HOME": os.path.join(private, ".local", "share"),
        "XDG_STATE_HOME": os.path.join(private, ".local", "state"),
        "LANG": "C",
        "LC_ALL": "C",
        "NIX_SSHOPTS": ssh_opts(route),
    }
    if route.agent_socket is not None:
        env["SSH_AUTH_SOCK"] = route.agent_socket
    return env


def seal_table(profile: dict, disk_bytes: int, disk_sha256: str) -> dict:
    keep = profile["seed"]["nix_generations_retained"]
    return {
        "machine_id": evidence.equals("absent"),
        "dbus_machine_id": evidence.equals("absent"),
        "ssh_host_keys": evidence.equals("0"),
        "random_seed": evidence.equals("absent"),
        "esp_random_seed": evidence.equals("absent"),
        "system_generations": evidence.int_between(1, keep),
        "system_profile": evidence.equals("present"),
        "fallback_loader": evidence.equals("present"),
        "runner_files": evidence.equals("0"),
        "authorized_keys_files": evidence.equals("0"),
        "enabled_runner_units": evidence.equals("0"),
        "password_hashes": lambda v: v in ("0", "missing"),
        "disk_bytes": evidence.equals(str(disk_bytes)),
        "disk_sha256": evidence.equals(disk_sha256),
    }


def prepare(args) -> int:
    return backend.prepare(args, FAMILY)


def construct(args) -> int:
    run, state = backend.open_prepared(args.run_dir, FAMILY)
    profile = state["profile"]
    nix = tartmod.tool_path(args.nix, "--nix")
    tart_path = tartmod.tool_path(args.tart, "--tart")
    route = parse_build_store(args.build_store, args.ssh_agent_socket)
    seed_out, artifact_out = backend.outputs(run, args.output, args.artifact_output)
    state = run.begin(state, "prepared", "constructing")
    runner = proc.Runner(run.sub("logs"), {}, args.max_step_seconds)
    env = nix_env(run, nix, route)
    store = route.uri
    resources = profile["resources"]
    disk_bytes = manifest.parse_disk(resources["disk"])
    home = token = None
    try:
        nix_v = nix_version(runner.run("nix-version", [nix, "--version"], timeout=60, env=env, capture=True))
        runner.run("nix-store-info", [nix] + NIX_FLAGS + ["store", "info", "--store", store],
                   timeout=300, env=env)
        out = runner.run("nix-build", [nix] + NIX_FLAGS + [
            "build", "--eval-store", "auto", "--store", store,
            "--option", "builders", "",
            "--no-update-lock-file", "--no-write-lock-file", "--no-link", "--print-out-paths",
            "path:%s#packages.aarch64-linux.esa-seed-image" % run.sub("workspace"),
        ], env=env, capture=True).strip()
        if not re.fullmatch(OUT_RE, out):
            raise CommandFailed("nix-build", "did not print exactly one esa-seed-image store path")
        limits = {"disk.img": disk_bytes, "esa-seed-check.txt": REPORT_MAX}
        found = runner.stream(
            "nix-dump-path", [nix] + NIX_FLAGS + ["store", "dump-path", "--store", store, out],
            lambda stream: nar.extract(stream, run.sub("out"), limits), env=env)
        with open(run.sub("out", "esa-seed-check.txt"), "rb") as handle:
            report = evidence.parse_report(handle.read())
        disk_sha = found["disk.img"][1]
        failures = evidence.judge(report, seal_table(profile, disk_bytes, disk_sha))
        doc = evidence.build("seed.nixos-seal", report, failures,
                             {"disk_sha256": disk_sha, "run_nonce": state["run_nonce"]}, "esa nixos construct")
        seal_path = run.sub("evidence", "seed.nixos-seal.json")
        evidence.write(seal_path, doc)
        if failures:
            raise Refusal([("seed.nixos-seal/" + k, "seal check failed") for k in failures])

        home, token = backend.owned_home(run)
        tart = tartmod.Tart(runner, tart_path, home, run.private_home, run.tmp, args.max_step_seconds)
        tart_v = tart.version()
        name = custody.rand_name("esa-asm-")
        tart.create_linux(name)
        memory_mib = manifest.parse_memory(resources["memory"]) // manifest.MIB
        tart.set(name, ["--cpu", str(resources["cpu_count"]), "--memory", str(memory_mib),
                        "--disk", run.sub("out", "disk.img")])
        os.unlink(run.sub("out", "disk.img"))
        vm = tart.vm_dir(name)
        scan = tartvm.scan_vm_dir(vm, FAMILY, resources)
        if scan.disk_sha256 != disk_sha:
            raise Refusal([("vm-dir/disk.img", "differs from the disk streamed from the build store")])
        artifact = record.build_artifact(profile, scan, state["esa_revision"],
                                         {"nix": nix_v, "tart": tart_v}, [seal_path])
        backend.finalize(run, vm, artifact, seed_out, artifact_out)
        custody.remove_owned(home, token)
    except BaseException as exc:
        runner.kill_all()
        if token:
            custody.remove_owned(home, token)
        streamed = run.sub("out", "disk.img")
        if os.path.lexists(streamed):
            os.unlink(streamed)  # no unrecorded payload outlives a failed run
        state["step"] = "failed"
        state["failure"] = getattr(exc, "step", type(exc).__name__)
        run.write_state(state)
        raise
    state["step"] = "completed"
    state["artifact_id"] = artifact["artifact_id"]
    run.write_state(state)
    backend.emit(backend.summary(artifact, seed_out, artifact_out))
    return 0
