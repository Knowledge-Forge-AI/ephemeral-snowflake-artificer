"""Steps shared by the NixOS and Tahoe construction drivers."""

from __future__ import annotations

import json
import os
import re
import sys

from esa import custody
from esa import manifest
from esa import nixinputs
from esa import record
from esa import tartvm
from esa.errors import Refusal, UsageError
from esa.loader import load_valid
from esa.run import Run

BOOTSTRAP_USER = "admin"  # the upstream vanilla recipe's public automation account
LKL_CPTOFS_MEM_MIB = 2048
IMAGE_MEM_MIB = 2048
ADMISSION = "not-determined-by-esa"


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def check_revision(rev: str) -> None:
    if not re.fullmatch(manifest.REV_RE, rev):
        raise Refusal([("--esa-revision", "must be a 40-character lowercase hex commit; "
                                          "branch, tag or short revisions are moving or ambiguous")])


def refuse_unconstructible(profile: dict) -> None:
    if profile["family"] == "tahoe-vanilla" and profile["seed"]["inherited_default_account"] is not True:
        raise Refusal([("/seed/inherited_default_account",
                        "ESA constructs tahoe-vanilla seeds only from the inherited upstream account; "
                        "bootstrap-only console credential rotation is a separately documented boundary, "
                        "not automated")])


def params_for(profile: dict) -> dict:
    common = {
        "readiness_method": profile["readiness"]["method"],
        "nix_generations_retained": profile["seed"]["nix_generations_retained"],
    }
    if profile["family"] == "nixos-minimal":
        common.update({
            "disk_mib": manifest.disk_in_units(profile["resources"]["disk"], manifest.MIB),
            "nixos_release": profile["inputs"]["nixos_release"],
            "lkl_cptofs_mem_mib": LKL_CPTOFS_MEM_MIB,
            "image_mem_mib": IMAGE_MEM_MIB,
        })
    else:
        common["bootstrap_user"] = BOOTSTRAP_USER
    return common


def prepare(args, family: str) -> int:
    """Verify consumer inputs and write an isolated build workspace. Runs no tools."""
    check_revision(args.esa_revision)
    profile = load_valid(args.profile, expect_kind="profile")
    if profile["family"] != family:
        raise Refusal([("/family", "this command prepares %s profiles" % family)])
    refuse_unconstructible(profile)
    lock = nixinputs.read_input(args.flake_lock, "--flake-lock", 1 << 20)
    originals = nixinputs.check_lock(lock, profile)
    if profile["customization"]["kind"] == "none":
        if args.module:
            raise Refusal([("--module", "customization.kind is 'none'; no module is accepted")])
        module = nixinputs.EMPTY_MODULE
    else:
        if not args.module:
            raise UsageError("--module is required for customization.kind %s" % profile["customization"]["kind"])
        module = nixinputs.check_module(
            nixinputs.read_input(args.module, "--module", nixinputs.MAX_MODULE_BYTES), profile)
    files = nixinputs.workspace_files(profile, lock, module, originals, params_for(profile))
    run = Run.create(args.run_dir)
    hashes = nixinputs.write_tree(run.sub("workspace"), files)
    run.write_state({
        "family": family,
        "step": "prepared",
        "profile": profile,
        "esa_revision": args.esa_revision,
        "run_nonce": custody.nonce(),
        "workspace": hashes,
    })
    emit({
        "run_schema": "esa-run-v1",
        "run_dir": run.path,
        "family": family,
        "step": "prepared",
        "workspace_files": hashes,
        "executes": False,
    })
    return 0


def open_prepared(run_dir: str, family: str):
    run = Run.open(run_dir)
    state = run.read_state(family)
    errors = manifest.validate(state.get("profile"))
    if errors:
        raise Refusal([("--run-dir/state.json/profile" + p, m) for p, m in errors])
    refuse_unconstructible(state["profile"])
    nixinputs.verify_tree(run.sub("workspace"), state["workspace"])
    return run, state


def outputs(run: Run, seed_out: str, artifact_out: str):
    seed_out = custody.new_output(seed_out, "--output")
    artifact_out = custody.new_output(artifact_out, "--artifact-output")
    if not custody.same_device(run.path, os.path.dirname(seed_out)):
        raise Refusal([("--output", "must be on the same volume as --run-dir; seeds are moved, never copied")])
    return seed_out, artifact_out


def owned_home(run: Run):
    home = run.sub("tart-home")
    token = custody.create_owned_dir(home, "--run-dir/tart-home")
    os.mkdir(os.path.join(home, "vms"), 0o700)
    return home, token


def finalize(run: Run, vm_dir: str, artifact: dict, seed_out: str, artifact_out: str) -> None:
    """Move the stopped seed into place, make it read-only, write the artifact."""
    data = record.encode(artifact)
    record.write_exclusive(run.sub("artifact.json"), data)
    custody.require_absent(seed_out, "--output")
    os.rename(vm_dir, seed_out)
    try:
        for name in tartvm.PAYLOAD_PATHS:
            os.chmod(os.path.join(seed_out, name), 0o444)
        record.write_exclusive(artifact_out, data)
    except BaseException:
        for name in tartvm.PAYLOAD_PATHS:
            os.chmod(os.path.join(seed_out, name), 0o600)
        os.rename(seed_out, vm_dir)
        raise


def summary(artifact: dict, seed_out: str, artifact_out: str) -> dict:
    return {
        "construction_schema": "esa-construction-v1",
        "status": "constructed-not-native-qualified",
        "artifact_id": artifact["artifact_id"],
        "payload_digest": artifact["payload"]["digest"],
        "seed_dir": seed_out,
        "artifact": artifact_out,
        "validation": [{"check": v["check"], "result": v["result"]} for v in artifact["validation"]],
        "native_qualified": False,
        "admission": ADMISSION,
    }
