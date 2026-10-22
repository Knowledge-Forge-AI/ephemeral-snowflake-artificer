"""tahoe-vanilla construction: upstream base by digest, guest Nix and nix-darwin.

Ingress integrity does not rest on Tart. The caller supplies the base's OCI
manifest bytes, whose sha256 must equal ``inputs.macos.base.digest``. Before
Tart may change anything, and before any boot or bootstrap login, ESA
requires the pulled ``config.json`` and ``nvram.bin`` to equal their OCI
layer digests exactly, and ``disk.img`` to match the manifest's
``uncompressed-content-digest`` annotations chunk by chunk and in total size.
Exact matching holds because the clone lands in a newly created, empty owned
``TART_HOME``: Tart 2.40.1 regenerates the MAC address only when another
local VM already uses it (``Clone.swift``, ``VMStorageLocal``), and otherwise
copies the three files byte for byte (``VMDirectory.clone``). Any other
transformation is refused; there is no fallback.

Guest work uses the inherited, publicly documented upstream bootstrap
account over Tart's default shared NAT network (see guest.py). ESA adds no
TCC grants, no Apple account changes and no Passwords unlock, and makes no
claim of a byte-reproducible disk. Guest ``sw_vers`` must match the profile
before any change is made.

The seal arms a one-shot root launchd job inside the guest
(``recipes/darwin/guest/seal.sh``) that outlives the provisioning SSH
session, blocks new SSH connections for that boot, removes its own files and
then the host keys, and halts. The host requires the report's
``finalizer=armed`` and a zero exit of ``tart run`` within
``--shutdown-seconds``. Both are fail-closed gates: report-time values and a
clean VM stop are not proof that the stopped disk holds no host keys, which
native qualification must observe.

Every ``tart run`` binds a Unix socket ``control.sock`` in the VM directory
(``VMDirectory.controlSocketURL``, ``Run.swift``). Tart 2.40.1 removes a
previous one only when the next run starts (``ControlSocket.init``), and
closing it at exit leaves the pathname behind. After this run's own
``tart run`` exits 0, and before the strict scan, ESA removes that one name
under the conditions in ``remove_control_socket``. The scan and every
artifact verifier still refuse ``control.sock`` and any other extra entry.
"""

from __future__ import annotations

import os
import re
import stat

from esa import backend
from esa import custody
from esa import evidence
from esa import guest as guestmod
from esa import manifest
from esa import nixinputs
from esa import proc
from esa import record
from esa import strictjson
from esa import tart as tartmod
from esa import tartvm
from esa.errors import CommandFailed, Refusal
from esa.run import Run

FAMILY = "tahoe-vanilla"
DISK_MEDIA = "application/vnd.cirruslabs.tart.disk.v2"
CONFIG_MEDIA = "application/vnd.cirruslabs.tart.config.v1"
NVRAM_MEDIA = "application/vnd.cirruslabs.tart.nvram.v1"
ANNOT_SIZE = "org.cirruslabs.tart.uncompressed-size"
ANNOT_DIGEST = "org.cirruslabs.tart.uncompressed-content-digest"
ANNOT_DISK = "org.cirruslabs.tart.uncompressed-disk-size"
SYSTEM_RE = r"/nix/store/[0-9a-z]{32}-darwin-system-[A-Za-z0-9._+-]{1,64}"
USER_RE = r"[a-z_][a-z0-9_-]{0,31}|none"
# The APFS container must absorb the disk growth up to this much, which covers
# partition alignment and GPT overhead. A no-op resize (for example one blocked
# by a container after disk0s2) fails.
GROW_SLACK_BYTES = 1 << 30
CONTROL_SOCKET = "control.sock"


class BaseLayout:
    def __init__(self, digest, chunks, disk_bytes, config_digest, nvram_digest):
        self.digest = digest
        self.chunks = chunks
        self.disk_bytes = disk_bytes
        self.config_digest = config_digest
        self.nvram_digest = nvram_digest


def parse_oci_manifest(data: bytes, digest: str) -> BaseLayout:
    """Refuse unless ``data`` is the exact manifest for ``digest`` with a raw standalone disk."""
    if nixinputs.sha256_tag(data) != digest:
        raise Refusal([("--oci-manifest", "sha256 does not equal the pinned base digest")])
    try:
        doc = strictjson.loads(data)
    except strictjson.StrictJSONError as exc:
        raise Refusal([("--oci-manifest", "not strict JSON: %s" % exc)]) from None
    if doc.get("mediaType") != "application/vnd.oci.image.manifest.v1+json" or doc.get("schemaVersion") != 2:
        raise Refusal([("--oci-manifest", "is not an OCI image manifest v1")])
    layers = doc.get("layers")
    if not isinstance(layers, list) or not layers:
        raise Refusal([("--oci-manifest/layers", "must be a non-empty array")])
    chunks, configs, nvrams = [], [], []
    for i, layer in enumerate(layers):
        pointer = "--oci-manifest/layers/%d" % i
        if not isinstance(layer, dict) or not isinstance(layer.get("digest"), str) \
                or not re.fullmatch(manifest.SHA256_RE, layer["digest"]):
            raise Refusal([(pointer, "must carry a sha256 digest")])
        media = layer.get("mediaType")
        annotations = layer.get("annotations") or {}
        if media == DISK_MEDIA:
            size, content = annotations.get(ANNOT_SIZE), annotations.get(ANNOT_DIGEST)
            if not (isinstance(size, str) and re.fullmatch(r"[1-9][0-9]{0,15}", size)
                    and isinstance(content, str) and re.fullmatch(manifest.SHA256_RE, content)):
                raise Refusal([(pointer, "disk layer lacks exact uncompressed size and digest annotations")])
            chunks.append((int(size), content))
        elif media == CONFIG_MEDIA:
            configs.append(layer["digest"])
        elif media == NVRAM_MEDIA:
            nvrams.append(layer["digest"])
        else:
            raise Refusal([(pointer, "media type is not part of a standalone raw Tart VM")])
    if len(configs) != 1 or len(nvrams) != 1 or not chunks:
        raise Refusal([("--oci-manifest/layers", "must hold one config, one NVRAM and at least one disk layer")])
    total = sum(size for size, _ in chunks)
    declared = (doc.get("annotations") or {}).get(ANNOT_DISK)
    if declared != str(total):
        raise Refusal([("--oci-manifest/annotations", "uncompressed disk size does not equal the sum of disk layers")])
    return BaseLayout(digest, chunks, total, configs[0], nvrams[0])


def verify_ingress(vm: str, layout: BaseLayout) -> dict:
    """Exact ingress check of a pulled base before any transformation.

    Every value returned is a passed check: a mismatch raises a refusal.
    """
    entries = tartvm.list_vm_dir(vm)
    tartvm.assert_stopped(vm)
    config_sha, _, _ = tartvm.hash_file(os.path.join(vm, "config.json"))
    if config_sha != layout.config_digest:
        raise Refusal([("base/config.json", "sha256 does not equal the OCI config layer digest; "
                                            "Tart transformed or replaced the pulled config")])
    nvram_sha, _, _ = tartvm.hash_file(os.path.join(vm, "nvram.bin"))
    if nvram_sha != layout.nvram_digest:
        raise Refusal([("base/nvram.bin", "sha256 does not equal the OCI NVRAM layer digest")])
    with open(os.path.join(vm, "config.json"), "rb") as handle:
        config_bytes = handle.read(strictjson.MAX_BYTES + 1)
    config = tartvm.parse_ingress_config(config_bytes)
    if config["os"] != "darwin":
        raise Refusal([("base/config.json/os", "the vanilla base must be a darwin VM")])
    if entries["disk.img"].st_size != layout.disk_bytes:
        raise Refusal([("base/disk.img", "size does not equal the manifest's uncompressed disk size")])
    disk_sha, _, chunk_tags = tartvm.hash_file(os.path.join(vm, "disk.img"), [s for s, _ in layout.chunks])
    bad = [i for i, (tag, (_, want)) in enumerate(zip(chunk_tags, layout.chunks)) if tag != want]
    if bad:
        raise Refusal([("base/disk.img", "chunk %d does not match its OCI uncompressed-content-digest" % bad[0])])
    return {
        "base_digest": layout.digest,
        "base_disk_bytes": str(layout.disk_bytes),
        "base_disk_sha256": disk_sha,
        "base_disk_chunks_verified": str(len(layout.chunks)),
        "base_config_sha256": config_sha,
        "base_nvram_sha256": nvram_sha,
    }


def probe_table(product_version=None, build=None) -> dict:
    version_re = manifest.MACOS_VERSION_RE if product_version is None else re.escape(product_version)
    build_re = manifest.MACOS_BUILD_RE if build is None else re.escape(build)
    return {
        "product_version": lambda v: re.fullmatch(version_re, v) is not None,
        "build": lambda v: re.fullmatch(build_re, v) is not None,
        "user": evidence.equals(backend.BOOTSTRAP_USER),
        "arch": evidence.equals("arm64"),
        "sudo_nopasswd": evidence.equals("yes"),
    }


def _judged(raw: bytes, table: dict):
    report = evidence.parse_report(raw)
    failures = evidence.judge(report, table)
    return report, failures


def _judged_growth(raw: bytes, target: int, base_disk: int):
    """The container grew by the disk growth, within GROW_SLACK_BYTES, and fits the disk."""
    report, failures = _judged(raw, {"container_bytes_before": evidence.int_between(1, base_disk),
                                     "container_bytes": evidence.int_between(1, target)})
    if not failures:
        grown = int(report["container_bytes"]) - int(report["container_bytes_before"])
        if grown < max(1, (target - base_disk) - GROW_SLACK_BYTES):
            failures.append("container_bytes")
    return report, failures


class Session:
    """One owned base VM: pulled by digest, ingress-verified, optionally booted."""

    def __init__(self, run, args, layout, timeout):
        tart_path = tartmod.tool_path(args.tart, "--tart")
        self.ssh = tartmod.tool_path(args.ssh, "--ssh")
        self.run = run
        self.layout = layout
        self.runner = proc.Runner(run.sub("logs"), {}, timeout)
        self.home, self.token = backend.owned_home(run)
        self.tart = tartmod.Tart(self.runner, tart_path, self.home, run.private_home, run.tmp, timeout)
        self.tart_version = None
        self.credential = args.bootstrap_credential_file
        self.name = custody.rand_name("esa-mac-")
        self.vm = self.tart.vm_dir(self.name)
        self.vm_proc = None
        self.guest = None
        self.known_hosts = run.sub("private", "known_hosts")

    def pull(self, pull_seconds: int) -> dict:
        self.tart_version = self.tart.version()
        self.tart.clone("%s@%s" % (manifest.TAHOE_BASE_REPOSITORY, self.layout.digest), self.name, pull_seconds)
        return verify_ingress(self.vm, self.layout)

    def boot(self, boot_seconds: int) -> None:
        self.vm_proc = self.tart.run(self.name)
        ip = self.tart.ip(self.name, boot_seconds)
        self.guest = guestmod.Guest(self.runner, self.ssh, backend.BOOTSTRAP_USER, ip,
                                    self.known_hosts, self.credential, self.run.private_home)
        self.guest.wait_ready(boot_seconds)

    def remove_control_socket(self) -> bool:
        """Remove the control socket this session's stopped ``tart run`` left behind.

        Only the name ``control.sock`` in this run's VM directory is touched,
        and only when all of these hold: this session's ``tart run`` has
        exited with status 0; the Tart home still carries this run's
        ownership marker; the VM directory is a real directory owned by this
        user; Tart's running lock is free and no suspended state exists; and
        the entry is a Unix socket owned by this user with one link, the same
        inode before and after that lock check. Anything else is refused and
        left in place. A missing socket is a no-op. Returns whether a socket
        was removed.
        """
        pointer = "vm-dir/" + CONTROL_SOCKET
        if self.vm_proc is None or self.vm_proc.proc.poll() != 0:
            raise Refusal([(pointer, "this run's tart run has not exited with status 0; not removed")])
        if custody.read_marker(self.home) != self.token:
            raise Refusal([(pointer, "the Tart home is not this run's owned home; not removed")])
        try:
            fd = os.open(self.vm, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise Refusal([("vm-dir", "must be a real directory, not a symlink or file")]) from None
        try:
            st = os.fstat(fd)
            path_st = os.lstat(self.vm)
            if st.st_uid != os.geteuid() or (path_st.st_dev, path_st.st_ino) != (st.st_dev, st.st_ino):
                raise Refusal([("vm-dir", "is not this run's VM directory owned by this user; not removed")])
            before = _control_socket(fd, pointer)
            if before is None:
                return False
            tartvm.assert_stopped(self.vm)
            if _control_socket(fd, pointer) != before:
                raise Refusal([(pointer, "was replaced or changed during the stop check; not removed")])
            # unlinkat without AT_REMOVEDIR never removes a directory.
            os.unlink(CONTROL_SOCKET, dir_fd=fd)
        finally:
            os.close(fd)
        return True

    def close(self) -> None:
        self.runner.kill_all()
        if os.path.lexists(self.known_hosts):
            os.unlink(self.known_hosts)
        custody.remove_owned(self.home, self.token)


def _control_socket(dir_fd: int, pointer: str):
    """Identity of a removable ``control.sock`` under ``dir_fd``, None if absent; else refuse."""
    try:
        st = os.stat(CONTROL_SOCKET, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(st.st_mode):
        raise Refusal([(pointer, "is not a Unix socket (a symlink, file or directory); not removed")])
    if st.st_uid != os.geteuid():
        raise Refusal([(pointer, "is not owned by the current user; not removed")])
    if st.st_nlink != 1:
        raise Refusal([(pointer, "has more than one link; not removed")])
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_ctime_ns)


def _credential(args) -> None:
    custody.check_private_file(args.bootstrap_credential_file, "--bootstrap-credential-file",
                               guestmod.CREDENTIAL_MAX)


def _layout(path: str, digest: str) -> BaseLayout:
    return parse_oci_manifest(nixinputs.read_input(path, "--oci-manifest", strictjson.MAX_BYTES), digest)


def observe_base(args) -> int:
    """Bootstrap-only route: describe a base before a profile can pin it.

    Pull by digest, verify ingress, boot, read sw_vers, then stop and delete
    everything owned. Nothing is installed and nothing is retained but logs
    and the report.
    """
    if not re.fullmatch(manifest.SHA256_RE, args.digest):
        raise Refusal([("--digest", "must be 'sha256:' followed by 64 lowercase hex digits")])
    layout = _layout(args.oci_manifest, args.digest)
    _credential(args)
    tartmod.tool_path(args.tart, "--tart")
    tartmod.tool_path(args.ssh, "--ssh")
    run = Run.create(args.run_dir)
    session = Session(run, args, layout, args.max_step_seconds)
    try:
        ingress = session.pull(args.pull_seconds)
        session.boot(args.boot_seconds)
        raw = session.guest.script("probe", timeout=120)
        report, failures = _judged(raw, probe_table())
    finally:
        session.close()
    out = {
        "observe_schema": "esa-observe-base-v1",
        "base": {"repository": manifest.TAHOE_BASE_REPOSITORY, "digest": args.digest},
        "ingress": ingress,
        "guest": report,
        "failures": failures,
        "profile_hint": {"product_version": report.get("product_version"), "build": report.get("build")},
        "note": "observation of one boot; not qualification and not a profile",
    }
    record.write_exclusive(run.sub("observe-base.json"), record.encode(out), 0o600)
    backend.emit(out)
    if failures:
        raise Refusal([("probe/" + k, "unexpected guest observation") for k in failures])
    return 0


def prepare(args) -> int:
    return backend.prepare(args, FAMILY)


def seal_table(keep: int) -> dict:
    return {
        "ssh_host_keys": evidence.equals("0"),
        "system_generations": evidence.int_between(1, keep),
        "authorized_keys_files": evidence.equals("0"),
        "runner_files": evidence.equals("0"),
        "runner_launchd_jobs": evidence.equals("0"),
        "autologin_user": lambda v: re.fullmatch(USER_RE, v) is not None,
        "apple_account": evidence.equals("absent"),
        "run_root": evidence.equals("absent"),
        "finalizer": evidence.equals("armed"),
    }


def construct(args) -> int:
    run, state = backend.open_prepared(args.run_dir, FAMILY)
    profile = state["profile"]
    inputs = profile["inputs"]
    layout = _layout(args.oci_manifest, inputs["macos"]["base"]["digest"])
    _credential(args)
    resources = profile["resources"]
    target = manifest.parse_disk(resources["disk"])
    if target < layout.disk_bytes:
        raise Refusal([("/resources/disk", "is smaller than the base disk (%d bytes); disks are never shrunk"
                        % layout.disk_bytes)])
    seed_out, artifact_out = backend.outputs(run, args.output, args.artifact_output)
    tartmod.tool_path(args.tart, "--tart")
    tartmod.tool_path(args.ssh, "--ssh")
    state = run.begin(state, "prepared", "constructing")
    session = Session(run, args, layout, args.max_step_seconds)
    reports = {}
    try:
        reports["base.ingress"] = (session.pull(args.pull_seconds), [])
        options = ["--cpu", str(resources["cpu_count"]),
                   "--memory", str(manifest.parse_memory(resources["memory"]) // manifest.MIB)]
        if target > layout.disk_bytes:
            options += ["--disk-size", str(manifest.disk_in_units(resources["disk"], manifest.GB))]
        session.tart.set(session.name, options)
        session.boot(args.boot_seconds)
        g = session.guest
        macos = inputs["macos"]
        reports["macos.sw-vers"] = _judged(g.script("probe", timeout=120),
                                           probe_table(macos["product_version"], macos["build"]))
        _stop_on_failure(reports, "macos.sw-vers")
        if target > layout.disk_bytes:
            reports["guest.disk-grow"] = _judged_growth(g.script("grow-disk", timeout=900),
                                                        target, layout.disk_bytes)
            _stop_on_failure(reports, "guest.disk-grow")
        root = "/private/tmp/esa-" + state["run_nonce"][:16]
        g.upload_dir("guest-upload", run.sub("workspace"), run.sub("private", "workspace.tar"), root)
        installer = inputs["nix_installer"]
        reports["guest.nix-install"] = _judged(
            g.script("install-nix", [installer["version"], installer["sha256"][7:]], timeout=1800),
            {"installer_sha256": evidence.equals(installer["sha256"]),
             "nix_version": evidence.equals(installer["version"])})
        _stop_on_failure(reports, "guest.nix-install")
        reports["guest.nix-darwin"] = _judged(
            g.script("apply-darwin", [root + "/workspace"], timeout=args.max_step_seconds),
            {"system": lambda v: re.fullmatch(SYSTEM_RE, v) is not None,
             "nix_version": lambda v: re.fullmatch(manifest.BUILDER_VERSION_RE, v) is not None})
        _stop_on_failure(reports, "guest.nix-darwin")
        keep = profile["seed"]["nix_generations_retained"]
        reports["seed.tahoe-seal"] = _judged(
            g.script("seal", [backend.BOOTSTRAP_USER, str(keep), root], timeout=args.max_step_seconds),
            seal_table(keep))
        _stop_on_failure(reports, "seed.tahoe-seal")
        # The guest finalizer halts the VM; a timeout or non-zero exit means no
        # clean halt was observed, and the run is refused.
        code = session.vm_proc.wait(args.shutdown_seconds)
        if code != 0:
            raise CommandFailed("tart-run", "exited with status %d; a clean guest halt was not observed" % code,
                                session.vm_proc.log)
        session.remove_control_socket()
        scan = tartvm.scan_vm_dir(session.vm, FAMILY, resources)
        binds = {"disk_sha256": scan.disk_sha256, "base_digest": layout.digest, "run_nonce": state["run_nonce"]}
        paths = []
        for check in sorted(reports):
            path = run.sub("evidence", check + ".json")
            evidence.write(path, evidence.build(check, reports[check][0], reports[check][1], binds,
                                                "esa tahoe construct"))
            paths.append(path)
        guest_nix = reports["guest.nix-darwin"][0]["nix_version"]
        artifact = record.build_artifact(profile, scan, state["esa_revision"],
                                         {"nix": guest_nix, "tart": session.tart_version}, paths)
        backend.finalize(run, session.vm, artifact, seed_out, artifact_out)
    except BaseException as exc:
        # Failed guest reports are kept for review, unbound: there is no seed to bind.
        for check, (report, failures) in reports.items():
            if failures:
                record.write_exclusive(run.sub("evidence", check + ".failed.json"), evidence.encode(
                    {"check": check, "failures": failures, "observed": report}), 0o600)
        state["step"] = "failed"
        state["failure"] = getattr(exc, "step", type(exc).__name__)
        run.write_state(state)
        raise
    finally:
        session.close()
    state["step"] = "completed"
    state["artifact_id"] = artifact["artifact_id"]
    run.write_state(state)
    backend.emit(backend.summary(artifact, seed_out, artifact_out))
    return 0


def _stop_on_failure(reports: dict, check: str) -> None:
    failures = reports[check][1]
    if failures:
        raise Refusal([(check + "/" + k, "guest check failed") for k in failures])

