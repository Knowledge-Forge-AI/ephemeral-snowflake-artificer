"""Seed custody and per-instance lifecycle helpers shared by both backends.

- ``seed export`` / ``seed import`` move a verified, stopped seed through a
  Tart ``.tvm`` archive using a fresh owned ``TART_HOME`` each time, so the
  destination name is provably absent and no local MAC collision exists.
  Import is followed by exact payload verification against the artifact.
- ``instance create`` verifies the immutable seed, materializes it into a
  new owned ``TART_HOME`` by APFS clone (or an explicit sparse copy), then
  applies only the deliberate host-side identifier transformations the
  artifact declares and checks the semantic config diff is bounded to them.
- ``instance delete`` removes only an ESA-owned instance home.

Tart clone or import alone does not guarantee fresh identifiers: Tart
regenerates a MAC only on a local collision. ESA therefore transforms
explicitly (``tart set --random-mac`` / ``--random-serial``). A transformed or
booted instance is never hashed as if it were the seed.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys

from esa import custody
from esa.loader import load_valid
from esa import payload
from esa import proc
from esa import strictjson
from esa import tart as tartmod
from esa import tartvm
from esa.errors import Refusal, UsageError
from esa.run import Run

ADMISSION = "not-determined-by-esa"
HOST_TRANSFORMS = {
    "mac-address": ("--random-mac", "macAddress"),
    "platform-machine-identifier": ("--random-serial", "ecid"),
}
# Only `instance create` writes this; `instance delete` refuses any other
# ESA-owned directory (a run directory, a seed export home).
KIND_MARKER = ".esa-kind"
INSTANCE_HOME = b"esa-instance-home-v1\n"


def _is_instance_home(home: str) -> bool:
    path = os.path.join(home, KIND_MARKER)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        return False
    with open(path, "rb") as handle:
        return handle.read(64) == INSTANCE_HOME


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _read_config(directory: str) -> dict:
    with open(os.path.join(directory, "config.json"), "rb") as handle:
        return tartvm.parse_config(handle.read(strictjson.MAX_BYTES + 1))


def _seal_seed_files(directory: str) -> None:
    for name in tartvm.PAYLOAD_PATHS:
        os.chmod(os.path.join(directory, name), 0o444)


def _owned_home(path: str, pointer: str, kind: bytes = b""):
    token = custody.create_owned_dir(path, pointer)
    if kind:
        fd = os.open(os.path.join(path, KIND_MARKER), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(kind)
    for sub in ("vms", "esa-logs", "esa-private", "esa-tmp"):
        os.mkdir(os.path.join(path, sub), 0o700)
    return token


def _tart(path, home, timeout):
    runner = proc.Runner(os.path.join(home, "esa-logs"), {}, timeout)
    return runner, tartmod.Tart(runner, path, home, os.path.join(home, "esa-private"),
                                os.path.join(home, "esa-tmp"), timeout)


def seed_export(args) -> int:
    artifact = load_valid(args.artifact, expect_kind="artifact")
    tool = tartmod.tool_path(args.tart, "--tart")
    payload.require(artifact, args.seed_dir, not args.full, args.expect_artifact_id)
    output = custody.new_output(args.output, "--output")
    run = Run.create(args.run_dir)
    home = run.sub("tart-home")
    token = _owned_home(home, "--run-dir/tart-home")
    runner, tart = _tart(tool, home, args.max_step_seconds)
    name = custody.rand_name("esa-exp-")
    created = False
    try:
        os.mkdir(tart.vm_dir(name), 0o700)
        custody.copy_vm_files(args.seed_dir, tart.vm_dir(name), tartvm.PAYLOAD_PATHS, args.copy_mode)
        custody.require_absent(output, "--output")
        created = True
        tart.export(name, output, args.max_step_seconds)
        size = os.lstat(output).st_size
        digest, _, _ = tartvm.hash_file(output)
    except BaseException:
        runner.kill_all()
        if created and os.path.lexists(output):
            os.unlink(output)
        raise
    finally:
        custody.remove_owned(home, token)
    _emit({
        "export_schema": "esa-export-v1",
        "artifact_id": artifact["artifact_id"],
        "archive": output,
        "archive_sha256": digest,
        "compressed_transfer_bytes": size,
        "note": "compressed_transfer_bytes is an observation; it is not part of artifact identity",
    })
    return 0


def seed_import(args) -> int:
    artifact = load_valid(args.artifact, expect_kind="artifact")
    tool = tartmod.tool_path(args.tart, "--tart")
    if args.expect_archive_sha256:
        digest, _, _ = tartvm.hash_file(args.tvm)
        if digest != args.expect_archive_sha256:
            raise Refusal([("--tvm", "sha256 does not match --expect-archive-sha256")])
    output = custody.new_output(args.output, "--output")
    run = Run.create(args.run_dir)
    home = run.sub("tart-home")
    token = _owned_home(home, "--run-dir/tart-home")
    runner, tart = _tart(tool, home, args.max_step_seconds)
    name = custody.rand_name("esa-imp-")
    try:
        tart.import_(os.path.abspath(args.tvm), name, args.max_step_seconds)
        vm = tart.vm_dir(name)
        tartvm.list_vm_dir(vm)
        tartvm.assert_stopped(vm)
        errors = payload.verify(artifact, vm, quick=False)
        if errors:
            raise Refusal([(p.replace("--payload-dir", "imported"), m) for p, m in errors])
        custody.require_absent(output, "--output")
        if not custody.same_device(vm, os.path.dirname(output)):
            raise Refusal([("--output", "must be on the same volume as --run-dir")])
        os.rename(vm, output)
        _seal_seed_files(output)
    except BaseException:
        runner.kill_all()
        raise
    finally:
        custody.remove_owned(home, token)
    _emit({
        "import_schema": "esa-import-v1",
        "artifact_id": artifact["artifact_id"],
        "seed_dir": output,
        "verified": "payload-full",
        "admission": ADMISSION,
    })
    return 0


def instance_create(args) -> int:
    artifact = load_valid(args.artifact, expect_kind="artifact")
    tool = tartmod.tool_path(args.tart, "--tart")
    if not re.fullmatch(tartmod.NAME_RE, args.name):
        raise UsageError("--name must match %s" % tartmod.NAME_RE)
    payload.require(artifact, args.seed_dir, not args.full, args.expect_artifact_id)
    if args.inject_dir:
        custody.check_private_dir(args.inject_dir, "--inject-dir")
        if re.search(r"[:,]", os.path.abspath(args.inject_dir)):
            # Tart parses --dir as name:path:options, splitting on ':' and ','.
            raise Refusal([("--inject-dir", "path must not contain ':' or ','")])
    seed_cfg = _read_config(args.seed_dir)
    home = os.path.abspath(args.tart_home)
    token = _owned_home(home, "--tart-home", INSTANCE_HOME)
    runner, tart = _tart(tool, home, args.max_step_seconds)
    regen = artifact["seed"]["regenerate_on_clone"]
    host = [t for t in regen if t in HOST_TRANSFORMS]
    options = [HOST_TRANSFORMS[t][0] for t in host]
    changed = tuple(HOST_TRANSFORMS[t][1] for t in host)
    if "displayRefit" in seed_cfg:
        # tart set rewrites displayRefit to "unset" unless told; keep the seed's value.
        options.append("--display-refit" if seed_cfg["displayRefit"] else "--no-display-refit")
    vm = tart.vm_dir(args.name)
    try:
        os.mkdir(vm, 0o700)
        custody.copy_vm_files(args.seed_dir, vm, tartvm.PAYLOAD_PATHS, args.copy_mode)
        if options:
            tart.set(args.name, options)
        tartvm.list_vm_dir(vm)
        errors = tartvm.instance_diff(seed_cfg, _read_config(vm), changed)
        expected = {f["path"]: f for f in artifact["payload"]["files"]}
        nvram, _, _ = tartvm.hash_file(os.path.join(vm, "nvram.bin"))
        if nvram != expected["nvram.bin"]["sha256"]:
            errors.append(("instance/nvram.bin", "changed during instance transformation"))
        if os.lstat(os.path.join(vm, "disk.img")).st_size != expected["disk.img"]["size_bytes"]:
            errors.append(("instance/disk.img", "size changed during instance transformation"))
        if errors:
            raise Refusal(errors)
    except BaseException:
        runner.kill_all()
        custody.remove_owned(home, token)
        raise
    run_args = ["run"]
    if args.inject_dir:
        run_args += ["--dir", "esa-inject:%s:ro" % os.path.abspath(args.inject_dir)]
    run_args.append(args.name)
    _emit({
        "instance_schema": "esa-instance-v1",
        "artifact_id": artifact["artifact_id"],
        "name": args.name,
        "tart_home": home,
        "seed_check": "payload-full" if args.full else "payload-quick",
        "host_transformations": host,
        "guest_first_boot": [t for t in regen if t not in HOST_TRANSFORMS],
        "guest_first_boot_verified": False,
        "verified": {"config_changed_keys": sorted(changed), "nvram": "unchanged", "disk_size": "unchanged"},
        "tart_run": {"env": {"TART_HOME": home, "TART_NO_AUTO_PRUNE": "1"}, "args": run_args},
        "readiness": artifact["readiness"],
        "admission": ADMISSION,
        "native_qualified": False,
    })
    return 0


def instance_delete(args) -> int:
    tool = tartmod.tool_path(args.tart, "--tart")
    home = os.path.abspath(args.tart_home)
    token = custody.read_marker(home)
    if token is None or not _is_instance_home(home):
        raise Refusal([("--tart-home", "is not an ESA-owned instance home; nothing removed")])
    vms = os.path.join(home, "vms")
    present = sorted(os.listdir(vms)) if os.path.isdir(vms) else []
    if set(present) - {args.name}:
        raise Refusal([("--tart-home", "holds VMs other than --name; nothing removed")])
    runner, tart = _tart(tool, home, args.max_step_seconds)
    if args.name in present:
        st = os.lstat(os.path.join(vms, args.name))
        if not stat.S_ISDIR(st.st_mode):
            raise Refusal([("--name", "is not a VM directory; nothing removed")])
        tart.delete(args.name)
    custody.remove_owned(home, token)
    _emit({"instance_schema": "esa-instance-v1", "deleted": args.name, "tart_home": home})
    return 0

