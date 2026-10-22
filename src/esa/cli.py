"""``esa`` command-line interface.

Exit status: 0 success, 1 contract refusal, 2 usage or I/O error.
Nothing here builds, fetches, evaluates Nix, or runs Tart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys

from esa import __version__
from esa import identity
from esa import manifest
from esa import strictjson

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

ADMISSION = "not-determined-by-esa"
CHUNK = 1 << 20

CONSTRUCTION = {
    "status": "unavailable",
    "reason": "backend construction work units are not implemented in this ESA revision",
}

PLAN_STEPS = {
    "nixos-minimal": (
        ("resolve-inputs", "ESA-NIX-01",
         "Refuse unless the consumer flake.lock matches flake_lock_sha256 and its nixpkgs node matches inputs.nixpkgs."),
        ("evaluate-system", "ESA-NIX-01",
         "Evaluate the ESA recipe plus the self-contained consumer module for aarch64-linux in isolation."),
        ("build-disk", "ESA-NIX-01",
         "Produce an EFI/GPT raw disk of resources.disk logical capacity."),
        ("prepare-seed", "ESA-NIX-02",
         "Apply the seed policy: retained generations, GC, store optimisation, identifier removal."),
        ("assemble-vm-dir", "ESA-NIX-02",
         "Assemble a tart-vm-dir-v1 directory with resources.cpu_count and resources.memory."),
        ("record-manifest", "ESA-REC-01",
         "Hash the VM directory and emit the artifact manifest."),
    ),
    "tahoe-vanilla": (
        ("resolve-base", "ESA-MAC-01",
         "Obtain inputs.macos.base by digest only and confirm product_version and build."),
        ("declare-inherited-settings", "ESA-MAC-01",
         "Record the upstream vanilla recipe settings relevant to the profile at inputs.macos.recipe.rev."),
        ("apply-nix-darwin", "ESA-MAC-02",
         "Install pinned Nix and apply the pinned nix-darwin configuration plus the consumer module."),
        ("prepare-seed", "ESA-MAC-01",
         "Apply the seed policy: identifier removal, credential and residue removal, retained generations."),
        ("set-resources", "ESA-MAC-01",
         "Set resources.cpu_count, resources.memory and resources.disk on the VM."),
        ("record-manifest", "ESA-REC-01",
         "Hash the VM directory and emit the artifact manifest."),
    ),
}


class Refusal(Exception):
    def __init__(self, errors):
        super().__init__("contract refusal")
        self.errors = errors


class UsageError(Exception):
    pass


def _pointer(p: str) -> str:
    # Pointers carry document key names; escape anything a terminal could act on.
    return "".join(ch if " " <= ch <= "~" else "\\u%04x" % ord(ch) for ch in p) or "/"


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _load_valid(path: str, expect_kind=None, check_artifact_id=True) -> dict:
    try:
        doc = strictjson.load_path(path)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (path, exc.strerror or exc)) from None
    except strictjson.StrictJSONError as exc:
        raise Refusal([("", str(exc))]) from None
    errors = manifest.validate(doc, check_artifact_id=check_artifact_id)
    if errors:
        raise Refusal(errors)
    if expect_kind is not None and doc["kind"] != expect_kind:
        raise Refusal([("/kind", "this command requires a %s manifest" % expect_kind)])
    return doc


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
        "builder_versions": "pinned by the backend work unit; not selected by this plan",
        "seed": profile["seed"],
        "output": {"payload_format": manifest.PAYLOAD_FORMAT, "files": list(manifest.PAYLOAD_PATHS)},
        "steps": [
            {"id": step, "work_unit": unit, "description": text, "implemented": False}
            for step, unit, text in PLAN_STEPS[family]
        ],
        "construction": dict(CONSTRUCTION),
        "admission": ADMISSION,
    }
    _emit(plan)
    return EXIT_OK


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _verify_payload(artifact: dict, directory: str, quick: bool) -> list:
    try:
        st = os.lstat(directory)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (directory, exc.strerror or exc)) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return [("--payload-dir", "must be a real directory, not a symlink or file")]
    try:
        return _compare_payload(artifact, directory, quick)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (exc.filename or directory, exc.strerror or exc)) from None


def _compare_payload(artifact: dict, directory: str, quick: bool) -> list:
    expected = {f["path"]: f for f in artifact["payload"]["files"]}
    errors = []
    with os.scandir(directory) as entries:
        present = {entry.name: entry for entry in entries}
    for name in sorted(set(present) - set(expected)):
        errors.append(("--payload-dir/" + name, "unexpected entry in payload directory"))
    for name in sorted(expected):
        pointer = "--payload-dir/" + name
        entry = present.get(name)
        if entry is None:
            errors.append((pointer, "missing payload file"))
            continue
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            errors.append((pointer, "must be a regular file, not a symlink or other type"))
            continue
        size = entry.stat(follow_symlinks=False).st_size
        if size != expected[name]["size_bytes"]:
            errors.append((pointer, "size %d does not match manifest size_bytes %d"
                           % (size, expected[name]["size_bytes"])))
            continue
        if quick and name == "disk.img":
            continue
        if _sha256_file(os.path.join(directory, name)) != expected[name]["sha256"]:
            errors.append((pointer, "sha256 does not match manifest"))
    return errors


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
        errors.extend(_verify_payload(artifact, args.payload_dir, args.quick))
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


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("esa: usage error: %s\n" % message)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="esa", description="Ephemeral Snowflake Artificer manifest contract tool.")
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
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Refusal as exc:
        for pointer, message in exc.errors:
            sys.stderr.write("esa: refused: %s: %s\n" % (_pointer(pointer), message))
        return EXIT_REFUSED
    except UsageError as exc:
        sys.stderr.write("esa: error: %s\n" % exc)
        return EXIT_USAGE
