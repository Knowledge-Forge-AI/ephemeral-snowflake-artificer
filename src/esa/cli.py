"""``esa`` command-line interface.

Exit status: 0 success, 1 contract or construction refusal, 2 usage or I/O
error. ``validate``, ``identity``, ``inspect``, ``plan`` and ``verify`` never
run external tools. ``record`` hashes a local VM directory. The ``nixos``,
``tahoe``, ``seed`` and ``instance`` commands run only the explicit tool paths
their caller passes, with bounded, logged steps (see esa.proc).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from esa import __version__
from esa import identity
from esa import manifest
from esa import payload
from esa.errors import CommandFailed, Refusal, UsageError
from esa.loader import load_valid as _load_valid

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

ADMISSION = "not-determined-by-esa"

CONSTRUCTION = {
    "status": "implemented-not-native-qualified",
    "reason": "construction commands exist in this ESA revision; no family has passed native "
              "qualification (ESA-QUAL-01), and qualification is not consumer acceptance",
}

PLAN_STEPS = {
    "nixos-minimal": (
        ("resolve-inputs", "ESA-NIX-01", "esa nixos prepare",
         "Refuse unless the consumer flake.lock matches flake_lock_sha256 and its nixpkgs node matches "
         "inputs.nixpkgs; write an isolated workspace."),
        ("evaluate-system", "ESA-NIX-01", "esa nixos construct",
         "Evaluate the ESA recipe plus the self-contained consumer module for aarch64-linux as a pure flake."),
        ("build-disk", "ESA-NIX-01", "esa nixos construct",
         "Build an EFI/GPT raw disk of exactly resources.disk on the caller-selected kvm store, reached "
         "over SSH with a pinned host key and only the explicitly selected key file or agent."),
        ("prepare-seed", "ESA-NIX-02", "esa nixos construct",
         "Remove identifiers and optimise the image store in a build VM; judge the seal report on the host."),
        ("assemble-vm-dir", "ESA-NIX-02", "esa nixos construct",
         "Assemble a tart-vm-dir-v1 directory with resources.cpu_count and resources.memory in an owned "
         "TART_HOME."),
        ("record-manifest", "ESA-REC-01", "esa record",
         "Hash the stopped VM directory and emit the artifact manifest with payload-bound evidence."),
    ),
    "tahoe-vanilla": (
        ("resolve-base", "ESA-MAC-01", "esa tahoe construct",
         "Pull inputs.macos.base by digest only into an empty owned TART_HOME, require config.json and "
         "nvram.bin to equal their OCI layer digests and disk.img its chunk digests, and confirm "
         "product_version and build in the guest."),
        ("declare-inherited-settings", "ESA-MAC-01", "documented",
         "The inherited upstream account, autologin, SSH and sudo settings are declared in the consumer "
         "contract and by seed.inherited_default_account."),
        ("set-resources", "ESA-MAC-01", "esa tahoe construct",
         "Set resources.cpu_count and resources.memory; grow, never shrink, the disk in decimal GB."),
        ("apply-nix-darwin", "ESA-MAC-02", "esa tahoe construct",
         "Install the pinned Nix release (sha256-checked) and apply the pinned nix-darwin configuration "
         "plus the consumer module."),
        ("prepare-seed", "ESA-MAC-01", "esa tahoe construct",
         "Remove residue, keep the declared generations, arm a one-shot guest finalizer that blocks new SSH "
         "connections, removes host keys last and halts; judge the seal report on the host, require a "
         "clean VM exit (a fail-closed gate, not proof of the final disk) and confirm stopped custody."),
        ("record-manifest", "ESA-REC-01", "esa record",
         "Hash the stopped VM directory and emit the artifact manifest with payload-bound evidence."),
    ),
}


def _pointer(p: str) -> str:
    # Pointers carry document key names; escape anything a terminal could act on.
    return "".join(ch if " " <= ch <= "~" else "\\u%04x" % ord(ch) for ch in p) or "/"


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _identities(doc: dict) -> dict:
    if doc["kind"] == "profile":
        return {"profile_content_sha256": identity.profile_content_hash(doc)}
    return {
        "artifact_id": doc["artifact_id"],
        "payload_digest": doc["payload"]["digest"],
    }


def cmd_validate(args) -> int:
    doc = _load_valid(args.file)
    sys.stdout.write("ok: %s %s %s\n" % (doc["kind"], doc["family"], doc["label"]))
    return EXIT_OK


def cmd_identity(args) -> int:
    doc = _load_valid(args.file)
    if doc["kind"] == "profile":
        sys.stdout.write(identity.profile_content_hash(doc) + "\n")
    else:
        sys.stdout.write(doc["artifact_id"] + "\n")
    return EXIT_OK


def cmd_inspect(args) -> int:
    doc = _load_valid(args.file)
    out = {
        "inspect_schema": "esa-inspect-v1",
        "schema": doc["schema"],
        "kind": doc["kind"],
        "family": doc["family"],
        "arch": doc["arch"],
        "platform": doc["platform"],
        "label": {"value": doc["label"], "mutable": True, "identity_bearing": False},
        "identity": _identities(doc),
        "admission": ADMISSION,
        "capabilities_verified_by_esa": False,
    }
    if doc["kind"] == "profile":
        out["revision"] = doc["revision"]
        out["construction"] = dict(CONSTRUCTION)
    else:
        out["esa_revision"] = doc["esa_revision"]
        out["profile"] = {
            "revision": doc["profile"]["revision"],
            "content_sha256": doc["profile"]["content_sha256"],
        }
        out["validation"] = {
            "identity_bearing": False,
            "checks": [{"check": v["check"], "result": v["result"]} for v in doc["validation"]],
        }
    _emit(out)
    return EXIT_OK


def cmd_plan(args) -> int:
    rev = args.esa_revision
    if not re.fullmatch(manifest.REV_RE, rev, flags=re.ASCII):
        raise Refusal([(
            "--esa-revision",
            "must be a 40-character lowercase hex commit; branch, tag or short revisions are moving or ambiguous",
        )])
    profile = _load_valid(args.profile, expect_kind="profile")
    family = profile["family"]
    supplied = [{"role": "flake-lock", "sha256": profile["inputs"]["flake_lock_sha256"]}]
    custom = profile["customization"]
    if custom["kind"] != "none":
        supplied.append({"role": custom["kind"], "sha256": custom["sha256"]})
    if family == "tahoe-vanilla":
        supplied.append({"role": "oci-manifest", "sha256": profile["inputs"]["macos"]["base"]["digest"]})
    plan = {
        "plan_schema": "esa-plan-v1",
        "executes": False,
        "esa_revision": rev,
        "profile": {
            "label": profile["label"],
            "revision": profile["revision"],
            "content_sha256": identity.profile_content_hash(profile),
        },
        "family": family,
        "arch": profile["arch"],
        "platform": profile["platform"],
        "nix_system": "aarch64-linux" if family == "nixos-minimal" else "aarch64-darwin",
        "inputs": profile["inputs"],
        "resources": {
            "cpu_count": profile["resources"]["cpu_count"],
            "memory_bytes": manifest.parse_memory(profile["resources"]["memory"]),
            "disk_bytes": manifest.parse_disk(profile["resources"]["disk"]),
        },
        "consumer_supplied_files": supplied,
        "builders_required": list(manifest.BUILDERS_REQUIRED),
        "builder_versions": "recorded from the tools that actually ran; not selected by this plan",
        "seed": profile["seed"],
        "output": {"payload_format": manifest.PAYLOAD_FORMAT, "files": list(manifest.PAYLOAD_PATHS)},
        "steps": [
            {"id": step, "work_unit": unit, "command": command, "description": text,
             "implemented": True, "native_qualified": False}
            for step, unit, command, text in PLAN_STEPS[family]
        ],
        "construction": dict(CONSTRUCTION),
        "admission": ADMISSION,
    }
    _emit(plan)
    return EXIT_OK


def cmd_verify(args) -> int:
    if args.quick and not args.payload_dir:
        raise UsageError("--quick requires --payload-dir")
    if args.quick and not (args.expect_artifact_id or args.expect_payload_digest):
        raise UsageError("--quick requires --expect-artifact-id or --expect-payload-digest from the receipt")
    artifact = _load_valid(args.artifact, expect_kind="artifact")
    checks = ["manifest"]
    errors = []
    if args.expect_artifact_id:
        if artifact["artifact_id"] != args.expect_artifact_id:
            errors.append(("/artifact_id", "does not match --expect-artifact-id"))
        checks.append("expected-artifact-id")
    if args.expect_payload_digest:
        if artifact["payload"]["digest"] != args.expect_payload_digest:
            errors.append(("/payload/digest", "does not match --expect-payload-digest"))
        checks.append("expected-payload-digest")
    if args.profile:
        profile = _load_valid(args.profile, expect_kind="profile")
        errors.extend(manifest.compare_with_profile(artifact, profile))
        checks.append("profile")
    if args.payload_dir:
        errors.extend(payload.verify(artifact, args.payload_dir, args.quick))
        checks.append("payload-quick" if args.quick else "payload-full")
    if errors:
        raise Refusal(errors)
    _emit({
        "verify_schema": "esa-verify-v1",
        "verified": True,
        "checks": checks,
        "disk_img": ("size-only" if args.quick else "sha256") if args.payload_dir else "not-checked",
        "artifact_id": artifact["artifact_id"],
        "payload_digest": artifact["payload"]["digest"],
        "admission": ADMISSION,
    })
    return EXIT_OK


def cmd_record(args) -> int:
    from esa import record
    from esa import tartvm
    from esa import custody
    if not re.fullmatch(manifest.REV_RE, args.esa_revision, flags=re.ASCII):
        raise Refusal([("--esa-revision", "must be a 40-character lowercase hex commit")])
    profile = _load_valid(args.profile, expect_kind="profile")
    builders = record.parse_builders(args.builder)
    output = custody.new_output(args.output, "--output")
    extra = record.load_extra_licenses(args.extra_licenses) if args.extra_licenses else []
    try:
        scan = tartvm.scan_vm_dir(args.vm_dir, profile["family"], profile["resources"])
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (exc.filename or args.vm_dir, exc.strerror or exc)) from None
    artifact = record.build_artifact(profile, scan, args.esa_revision, builders, args.evidence, extra)
    record.write_exclusive(output, record.encode(artifact))
    _emit({
        "record_schema": "esa-record-v1",
        "artifact": output,
        "artifact_id": artifact["artifact_id"],
        "payload_digest": artifact["payload"]["digest"],
        "validation": [{"check": v["check"], "result": v["result"]} for v in artifact["validation"]],
        "native_qualified": False,
        "admission": ADMISSION,
    })
    return EXIT_OK


def _dispatch(module: str, func: str):
    def run(args):
        import importlib
        return getattr(importlib.import_module("esa." + module), func)(args)
    return run


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if not 1 <= value <= 86400 * 2:
        raise argparse.ArgumentTypeError("must be between 1 and 172800 seconds")
    return value


def _seed_check(p) -> None:
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--expect-artifact-id", metavar="ID",
                       help="quick seed check against the artifact_id in the consumer's receipt")
    group.add_argument("--full", action="store_true", help="hash every seed file, including disk.img")


def _add_backends(sub) -> None:
    p = sub.add_parser("record", help="record an artifact manifest for a built, stopped Tart VM directory")
    p.add_argument("profile")
    p.add_argument("--vm-dir", required=True, metavar="DIR")
    p.add_argument("--esa-revision", required=True, metavar="REV")
    p.add_argument("--builder", action="append", default=[], required=True, metavar="NAME=VERSION")
    p.add_argument("--evidence", action="append", default=[], metavar="FILE",
                   help="esa-evidence-v1 file bound to this disk.img; repeatable")
    p.add_argument("--extra-licenses", metavar="FILE",
                   help="JSON array of additional {component, license, reference} rows")
    p.add_argument("--output", required=True, metavar="FILE")
    p.set_defaults(func=cmd_record)

    actions = {}
    for family, module in (("nixos", "nixos"), ("tahoe", "tahoe")):
        fam = sub.add_parser(family, help="%s construction" % ("nixos-minimal" if family == "nixos"
                                                               else "tahoe-vanilla"))
        fsub = fam.add_subparsers(dest="action", metavar="ACTION", parser_class=_Parser)
        fsub.required = True
        actions[family] = fsub
        p = fsub.add_parser("prepare", help="verify consumer inputs and write an isolated workspace")
        p.add_argument("profile")
        p.add_argument("--flake-lock", required=True, metavar="FILE")
        p.add_argument("--module", metavar="FILE")
        p.add_argument("--esa-revision", required=True, metavar="REV")
        p.add_argument("--run-dir", required=True, metavar="DIR", help="absent; created mode 0700")
        p.set_defaults(func=_dispatch(module, "prepare"))
        p = fsub.add_parser("construct", help="build, seal, assemble and record a seed")
        p.add_argument("--run-dir", required=True, metavar="DIR")
        p.add_argument("--tart", required=True, metavar="PATH")
        p.add_argument("--output", required=True, metavar="SEED_DIR")
        p.add_argument("--artifact-output", required=True, metavar="FILE")
        p.add_argument("--max-step-seconds", type=_positive, default=14400)
        if family == "nixos":
            p.add_argument("--nix", required=True, metavar="PATH")
            p.add_argument("--build-store", required=True, metavar="URI",
                           help="ssh-ng:// or ssh://[USER@]HOST?base64-ssh-public-host-key=PIN[&ssh-key=PATH] of an "
                                "aarch64-linux builder with the kvm feature; PIN (required, '+' written "
                                "%%2B) is the base64 of one public host-key line, PATH a private key file "
                                "ESA never reads; no port and no other query parameter")
            p.add_argument("--ssh-agent-socket", metavar="PATH",
                           help="the only SSH agent the build-store connection may use; without it no "
                                "agent is used. ssh-key, this socket, or both must be given")
        else:
            _tahoe_session(p)
        p.set_defaults(func=_dispatch(module, "construct"))
    p = actions["tahoe"].add_parser("observe-base", help="bootstrap-only: boot a base by digest and report sw_vers")
    p.add_argument("--digest", required=True, metavar="SHA256")
    p.add_argument("--tart", required=True, metavar="PATH")
    p.add_argument("--run-dir", required=True, metavar="DIR")
    p.add_argument("--max-step-seconds", type=_positive, default=3600)
    _tahoe_session(p)
    p.set_defaults(func=_dispatch("tahoe", "observe_base"))

    seed = sub.add_parser("seed", help="seed custody: export or import a verified stopped seed")
    ssub = seed.add_subparsers(dest="action", metavar="ACTION", parser_class=_Parser)
    ssub.required = True
    p = ssub.add_parser("export", help="export a verified seed to an absent .tvm path")
    p.add_argument("--artifact", required=True)
    p.add_argument("--seed-dir", required=True)
    p.add_argument("--tart", required=True, metavar="PATH")
    p.add_argument("--run-dir", required=True, metavar="DIR")
    p.add_argument("--output", required=True, metavar="FILE.tvm")
    p.add_argument("--copy-mode", choices=("clone", "sparse-copy"), default="clone")
    p.add_argument("--max-step-seconds", type=_positive, default=7200)
    _seed_check(p)
    p.set_defaults(func=_dispatch("lifecycle", "seed_export"))
    p = ssub.add_parser("import", help="import a .tvm into an absent seed directory and verify it exactly")
    p.add_argument("--artifact", required=True)
    p.add_argument("--tvm", required=True, metavar="FILE.tvm")
    p.add_argument("--expect-archive-sha256", metavar="SHA256")
    p.add_argument("--tart", required=True, metavar="PATH")
    p.add_argument("--run-dir", required=True, metavar="DIR")
    p.add_argument("--output", required=True, metavar="SEED_DIR")
    p.add_argument("--max-step-seconds", type=_positive, default=7200)
    p.set_defaults(func=_dispatch("lifecycle", "seed_import"))

    inst = sub.add_parser("instance", help="per-instance lifecycle helper over an owned TART_HOME")
    isub = inst.add_subparsers(dest="action", metavar="ACTION", parser_class=_Parser)
    isub.required = True
    p = isub.add_parser("create", help="clone a verified seed into a new owned TART_HOME and transform it")
    p.add_argument("--artifact", required=True)
    p.add_argument("--seed-dir", required=True)
    p.add_argument("--tart", required=True, metavar="PATH")
    p.add_argument("--tart-home", required=True, metavar="DIR", help="absent; created mode 0700")
    p.add_argument("--name", required=True)
    p.add_argument("--inject-dir", metavar="DIR", help="private per-instance share for esa-inject")
    p.add_argument("--copy-mode", choices=("clone", "sparse-copy"), default="clone")
    p.add_argument("--max-step-seconds", type=_positive, default=600)
    _seed_check(p)
    p.set_defaults(func=_dispatch("lifecycle", "instance_create"))
    p = isub.add_parser("delete", help="delete an instance and its owned TART_HOME")
    p.add_argument("--tart", required=True, metavar="PATH")
    p.add_argument("--tart-home", required=True, metavar="DIR")
    p.add_argument("--name", required=True)
    p.add_argument("--max-step-seconds", type=_positive, default=600)
    p.set_defaults(func=_dispatch("lifecycle", "instance_delete"))


def _tahoe_session(p) -> None:
    p.add_argument("--oci-manifest", required=True, metavar="FILE",
                   help="exact OCI manifest bytes of the base; sha256 must equal the digest")
    p.add_argument("--ssh", required=True, metavar="PATH")
    p.add_argument("--bootstrap-credential-file", required=True, metavar="FILE",
                   help="private file (mode 0600) holding the bootstrap login; read only by bin/esa-askpass")
    p.add_argument("--pull-seconds", type=_positive, default=14400)
    p.add_argument("--boot-seconds", type=_positive, default=900)
    p.add_argument("--shutdown-seconds", type=_positive, default=900)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("esa: usage error: %s\n" % message)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="esa", description="Ephemeral Snowflake Artificer: manifests, construction and seed custody.")
    parser.add_argument("--version", action="version", version="esa " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)
    sub.required = True

    p = sub.add_parser("validate", help="validate a profile or artifact manifest")
    p.add_argument("file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("identity", help="print the profile content hash or artifact_id")
    p.add_argument("file")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("inspect", help="print a deterministic summary")
    p.add_argument("file")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("plan", help="describe the declarative build plan for a profile")
    p.add_argument("profile")
    p.add_argument("--esa-revision", required=True, metavar="REV")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("verify", help="verify an artifact against a profile and/or local payload")
    p.add_argument("artifact")
    p.add_argument("--profile", metavar="PROFILE")
    p.add_argument("--payload-dir", metavar="DIR")
    p.add_argument("--expect-artifact-id", metavar="ID",
                   help="refuse unless the artifact_id equals ID (from the consumer's receipt)")
    p.add_argument("--expect-payload-digest", metavar="DIGEST",
                   help="refuse unless payload.digest equals DIGEST (from the consumer's receipt)")
    p.add_argument("--quick", action="store_true",
                   help="hash every payload file except disk.img, which is size-checked only; "
                        "requires an --expect-* value")
    p.set_defaults(func=cmd_verify)
    _add_backends(sub)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Refusal as exc:
        for pointer, message in exc.errors:
            sys.stderr.write("esa: refused: %s: %s\n" % (_pointer(pointer), message))
        return EXIT_REFUSED
    except CommandFailed as exc:
        where = "; log: %s" % exc.log if exc.log else ""
        sys.stderr.write("esa: refused: step %s: %s%s\n" % (exc.step, exc.reason, where))
        return EXIT_REFUSED
    except UsageError as exc:
        sys.stderr.write("esa: error: %s\n" % exc)
        return EXIT_USAGE
    except OSError as exc:
        sys.stderr.write("esa: error: %s: %s\n" % (exc.filename or "I/O", exc.strerror or exc))
        return EXIT_USAGE
