"""Closed validation of ``esa-manifest-v1`` profiles and artifacts.

docs/specs/manifest-v1.md is the normative text; this module enforces it.
``validate`` returns a list of ``(json_pointer, message)`` errors; an empty
list means the document satisfies every v1 rule. There is no override and no
partial or draft mode.
"""

from __future__ import annotations

import datetime
import re

from esa import SCHEMA
from esa import identity
from esa import scan

KINDS = ("profile", "artifact")
FAMILIES = ("nixos-minimal", "tahoe-vanilla")
ARCH = "arm64"
PLATFORM = "tart"

MAX_STRING = 1024
MAX_URL = 512
MAX_BYTES_VALUE = (1 << 53) - 1

LABEL_RE = r"[a-z][a-z0-9-]{0,62}"
TOKEN_RE = r"[a-z][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*"
TOKEN_MAX = 64
REV_RE = r"[0-9a-f]{40}"
SHA256_RE = r"sha256:[0-9a-f]{64}"
NAR_HASH_RE = r"sha256-[A-Za-z0-9+/]{43}="
NIXOS_RELEASE_RE = r"[0-9]{2}\.(?:05|11)"
MACOS_VERSION_RE = r"26\.[0-9]{1,2}(?:\.[0-9]{1,2})?"
MACOS_BUILD_RE = r"25[A-Z][0-9]{1,4}[a-z]?"
MEMORY_RE = r"([1-9][0-9]{0,6})(MiB|GiB)"
DISK_RE = r"([1-9][0-9]{0,4})GiB"
BUILDER_VERSION_RE = r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}"
TIMESTAMP_RE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
REPORT_ID_RE = r"[a-z0-9][a-z0-9._-]{0,63}"
_LICENSE_TERM = r"(?:LicenseRef-[A-Za-z0-9.-]+|[A-Za-z0-9][A-Za-z0-9.+-]*)"
LICENSE_RE = _LICENSE_TERM + r"(?: (?:AND|OR|WITH) " + _LICENSE_TERM + r")*"
HTTPS_URL_RE = r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~%/+-]*)?"

MIB = 1 << 20
GIB = 1 << 30
MEMORY_MIN = 512 * MIB
MEMORY_MAX = 512 * GIB
DISK_MAX_GIB = 4096

TAHOE_BASE_REPOSITORY = "ghcr.io/cirruslabs/macos-tahoe-vanilla"
TAHOE_RECIPE_REPOSITORY = "https://github.com/cirruslabs/macos-image-templates"
TAHOE_RECIPE_PATH = "templates/vanilla-tahoe.pkr.hcl"

FAMILY_INPUTS = {
    "nixos-minimal": ("nixpkgs", "flake_lock_sha256", "nixos_release"),
    "tahoe-vanilla": ("nixpkgs", "nix_darwin", "flake_lock_sha256", "macos"),
}
ALL_INPUT_KEYS = frozenset(k for keys in FAMILY_INPUTS.values() for k in keys)

CUSTOMIZATION_KINDS = {
    "nixos-minimal": ("nixos-module", "none"),
    "tahoe-vanilla": ("nix-darwin-module", "none"),
}

REGENERATE_ALLOWED = {
    "nixos-minimal": ("hostname", "mac-address", "machine-id", "random-seed", "ssh-host-keys"),
    "tahoe-vanilla": ("hostname", "mac-address", "platform-machine-identifier", "ssh-host-keys"),
}
REGENERATE_REQUIRED = {
    "nixos-minimal": ("mac-address", "machine-id", "ssh-host-keys"),
    "tahoe-vanilla": ("mac-address", "ssh-host-keys"),
}

READINESS_METHODS = ("ssh", "tart-guest-agent")

BUILDER_NAMES = ("nix", "packer", "packer-plugin-tart", "tart")
BUILDERS_REQUIRED = ("nix", "tart")

PAYLOAD_FORMAT = "tart-vm-dir-v1"
PAYLOAD_PATHS = ("config.json", "disk.img", "nvram.bin")

OBSERVATION_METRICS = (
    "build_workspace_bytes",
    "clone_growth_bytes",
    "clone_to_ready_ms",
    "compressed_transfer_bytes",
    "guest_used_bytes",
    "host_allocated_bytes",
)

PROFILE_FIELDS = (
    "schema", "kind", "label", "family", "arch", "platform", "revision",
    "resources", "inputs", "customization", "capabilities", "readiness", "seed",
)
ARTIFACT_FIELDS = (
    "schema", "kind", "label", "family", "arch", "platform", "artifact_id",
    "esa_revision", "profile", "inputs", "resources", "customization",
    "capabilities", "readiness", "seed", "builders", "payload", "storage",
    "observations", "validation", "upstream_licenses",
)
# Profile fields an artifact copies verbatim; verify --profile compares them.
COPIED_FIELDS = (
    "family", "arch", "platform", "inputs", "resources", "customization",
    "capabilities", "readiness", "seed",
)

_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]*")


def _full(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value, flags=re.ASCII) is not None


def parse_memory(value: str) -> int:
    match = re.fullmatch(MEMORY_RE, value, flags=re.ASCII)
    if match is None:
        raise ValueError(value)
    return int(match.group(1)) * (GIB if match.group(2) == "GiB" else MIB)


def parse_disk(value: str) -> int:
    match = re.fullmatch(DISK_RE, value, flags=re.ASCII)
    if match is None:
        raise ValueError(value)
    return int(match.group(1)) * GIB


class _Checker:
    def __init__(self):
        self.errors = []

    def err(self, pointer, message):
        self.errors.append((pointer, message))

    # Primitive checks return True when the value is acceptable.

    def obj(self, value, pointer, required, optional=(), foreign=None):
        """Closed object. ``foreign`` maps keys that belong elsewhere to a reason."""
        if not isinstance(value, dict):
            self.err(pointer, "must be an object")
            return False
        ok = True
        for key in value:
            if key in required or key in optional:
                continue
            ok = False
            if foreign and key in foreign:
                self.err(pointer + "/" + key, foreign[key])
            else:
                self.err(pointer + "/" + key, "unknown field")
        for key in required:
            if key not in value:
                ok = False
                self.err(pointer + "/" + key, "required field is missing")
        return ok

    def string(self, value, pointer, pattern, what, max_len=MAX_STRING):
        if not isinstance(value, str):
            self.err(pointer, "must be a string (%s)" % what)
            return False
        if len(value) > max_len:
            self.err(pointer, "string longer than %d characters" % max_len)
            return False
        if not _full(pattern, value):
            self.err(pointer, "must be %s" % what)
            return False
        return True

    def const(self, value, pointer, expected, why=""):
        if value != expected or type(value) is not type(expected):
            suffix = "; " + why if why else ""
            self.err(pointer, "must be %r%s" % (expected, suffix))
            return False
        return True

    def enum(self, value, pointer, allowed, why=""):
        if not isinstance(value, str) or value not in allowed:
            suffix = "; " + why if why else ""
            self.err(pointer, "must be one of %s%s" % (", ".join(allowed), suffix))
            return False
        return True

    def integer(self, value, pointer, low, high):
        # bool is a subclass of int in Python; JSON true/false is not an integer.
        if type(value) is not int:
            self.err(pointer, "must be an integer")
            return False
        if not low <= value <= high:
            self.err(pointer, "must be between %d and %d" % (low, high))
            return False
        return True

    def boolean(self, value, pointer):
        if type(value) is not bool:
            self.err(pointer, "must be a JSON boolean")
            return False
        return True

    def true(self, value, pointer):
        if value is not True:
            self.err(pointer, "must be JSON true")
            return False
        return True

    def array(self, value, pointer, max_items, min_items=0):
        if not isinstance(value, list):
            self.err(pointer, "must be an array")
            return False
        if not min_items <= len(value) <= max_items:
            self.err(pointer, "must have between %d and %d items" % (min_items, max_items))
            return False
        return True

    def tokens(self, value, pointer, max_items, allowed=None):
        if not self.array(value, pointer, max_items):
            return False
        ok = True
        for i, item in enumerate(value):
            p = "%s/%d" % (pointer, i)
            if allowed is not None:
                ok = self.enum(item, p, allowed) and ok
            else:
                ok = self.string(item, p, TOKEN_RE, "a lowercase dotted token", TOKEN_MAX) and ok
        if ok and value != sorted(set(value)):
            self.err(pointer, "must be sorted and contain no duplicates")
            ok = False
        return ok

    def sha256(self, value, pointer):
        return self.string(value, pointer, SHA256_RE, "'sha256:' followed by 64 lowercase hex digits")

    def rev(self, value, pointer):
        return self.string(
            value, pointer, REV_RE,
            "a 40-character lowercase hex commit; branch, tag, channel or short revisions are moving or ambiguous",
        )


def _ascii_walk(c, doc, pointer=""):
    if isinstance(doc, dict):
        for key, value in doc.items():
            if not _PRINTABLE_ASCII.fullmatch(key):
                c.err(pointer, "keys must be printable ASCII")
            _ascii_walk(c, value, pointer + "/" + key)
    elif isinstance(doc, list):
        for i, value in enumerate(doc):
            _ascii_walk(c, value, "%s/%d" % (pointer, i))
    elif isinstance(doc, str):
        if not _PRINTABLE_ASCII.fullmatch(doc):
            c.err(pointer, "strings must be printable ASCII in esa-manifest-v1")


def _resources(c, value, pointer):
    if not c.obj(value, pointer, ("cpu_count", "memory", "disk")):
        return False
    ok = c.integer(value["cpu_count"], pointer + "/cpu_count", 1, 64)
    mem = value["memory"]
    if c.string(mem, pointer + "/memory", MEMORY_RE, "a whole number followed by MiB or GiB, e.g. '8GiB'"):
        if not MEMORY_MIN <= parse_memory(mem) <= MEMORY_MAX:
            c.err(pointer + "/memory", "must be between 512MiB and 512GiB")
            ok = False
    else:
        ok = False
    disk = value["disk"]
    if c.string(disk, pointer + "/disk", DISK_RE, "a whole number followed by GiB, e.g. '64GiB'"):
        if parse_disk(disk) > DISK_MAX_GIB * GIB:
            c.err(pointer + "/disk", "must be at most %dGiB" % DISK_MAX_GIB)
            ok = False
    else:
        ok = False
    return ok


def _locked_flake_input(c, value, pointer):
    if not c.obj(value, pointer, ("rev", "nar_hash")):
        return False
    ok = c.rev(value["rev"], pointer + "/rev")
    ok = c.string(
        value["nar_hash"], pointer + "/nar_hash", NAR_HASH_RE,
        "a Nix SRI hash 'sha256-' followed by 44 base64 characters",
    ) and ok
    return ok


def _inputs(c, value, pointer, family):
    required = FAMILY_INPUTS[family]
    foreign = {
        key: "not permitted for family %s" % family
        for key in ALL_INPUT_KEYS - set(required)
    }
    if not c.obj(value, pointer, required, foreign=foreign):
        return False
    ok = _locked_flake_input(c, value["nixpkgs"], pointer + "/nixpkgs")
    ok = c.sha256(value["flake_lock_sha256"], pointer + "/flake_lock_sha256") and ok
    if family == "nixos-minimal":
        ok = c.string(
            value["nixos_release"], pointer + "/nixos_release", NIXOS_RELEASE_RE,
            "a NixOS release such as '26.05'",
        ) and ok
        return ok
    ok = _locked_flake_input(c, value["nix_darwin"], pointer + "/nix_darwin") and ok
    return _macos(c, value["macos"], pointer + "/macos") and ok


def _macos(c, value, pointer):
    if not c.obj(value, pointer, ("product_version", "build", "base", "recipe")):
        return False
    ok = c.string(value["product_version"], pointer + "/product_version", MACOS_VERSION_RE,
                  "a macOS 26 product version such as '26.0.1'")
    ok = c.string(value["build"], pointer + "/build", MACOS_BUILD_RE,
                  "a macOS 26 build identifier such as '25A354'") and ok
    base = value["base"]
    bp = pointer + "/base"
    if c.obj(base, bp, ("kind", "repository", "digest")):
        ok = c.const(base["kind"], bp + "/kind", "oci") and ok
        ok = c.const(
            base["repository"], bp + "/repository", TAHOE_BASE_REPOSITORY,
            "tahoe-vanilla resolves only the upstream vanilla Tart image; tags are moving references",
        ) and ok
        ok = c.sha256(base["digest"], bp + "/digest") and ok
    else:
        ok = False
    recipe = value["recipe"]
    rp = pointer + "/recipe"
    if c.obj(recipe, rp, ("repository", "rev", "path")):
        ok = c.const(recipe["repository"], rp + "/repository", TAHOE_RECIPE_REPOSITORY) and ok
        ok = c.rev(recipe["rev"], rp + "/rev") and ok
        ok = c.const(recipe["path"], rp + "/path", TAHOE_RECIPE_PATH) and ok
    else:
        ok = False
    return ok


def _customization(c, value, pointer, family):
    if not c.obj(value, pointer, ("kind", "sha256")):
        return False
    kinds = CUSTOMIZATION_KINDS[family]
    if not c.enum(value["kind"], pointer + "/kind", kinds, "for family %s" % family):
        return False
    if value["kind"] == "none":
        if value["sha256"] is not None:
            c.err(pointer + "/sha256", "must be null when kind is 'none'")
            return False
        return True
    return c.sha256(value["sha256"], pointer + "/sha256")


def _capabilities(c, value, pointer):
    if not c.obj(value, pointer, ("guest", "host")):
        return False
    ok = c.tokens(value["guest"], pointer + "/guest", 32)
    return c.tokens(value["host"], pointer + "/host", 16) and ok


def _readiness(c, value, pointer):
    if not c.obj(value, pointer, ("method", "timeout_seconds", "consumer_check")):
        return False
    ok = c.enum(value["method"], pointer + "/method", READINESS_METHODS)
    ok = c.integer(value["timeout_seconds"], pointer + "/timeout_seconds", 1, 3600) and ok
    check = value["consumer_check"]
    if check is not None:
        ok = c.string(check, pointer + "/consumer_check", TOKEN_RE,
                      "a lowercase dotted token or null", TOKEN_MAX) and ok
    return ok


SEED_FIELDS = (
    "machine_identity_uninitialized",
    "ssh_host_keys_absent",
    "registration_material_absent",
    "consumer_credentials_absent",
    "build_residue_removed",
    "inherited_default_account",
    "nix_generations_retained",
    "regenerate_on_clone",
)


def _seed(c, value, pointer, family):
    if not c.obj(value, pointer, SEED_FIELDS):
        return False
    ok = True
    for key in SEED_FIELDS[:5]:
        ok = c.true(value[key], pointer + "/" + key) and ok
    account = value["inherited_default_account"]
    if c.boolean(account, pointer + "/inherited_default_account"):
        if family == "nixos-minimal" and account is not False:
            c.err(pointer + "/inherited_default_account", "must be false for family nixos-minimal")
            ok = False
    else:
        ok = False
    ok = c.integer(value["nix_generations_retained"], pointer + "/nix_generations_retained", 1, 8) and ok
    regen = value["regenerate_on_clone"]
    rp = pointer + "/regenerate_on_clone"
    if c.tokens(regen, rp, 8, REGENERATE_ALLOWED[family]):
        missing = [t for t in REGENERATE_REQUIRED[family] if t not in regen]
        if missing:
            c.err(rp, "must include %s for family %s" % (", ".join(missing), family))
            ok = False
    else:
        ok = False
    return ok


def _common(c, doc):
    """Check the fields shared by both kinds. Returns the family, or None."""
    c.string(doc["label"], "/label", LABEL_RE, "a lowercase label of at most 63 characters")
    c.const(doc["arch"], "/arch", ARCH, "no alias normalization; other architectures are unsupported")
    c.const(doc["platform"], "/platform", PLATFORM, "other platforms are unsupported")
    if c.enum(doc["family"], "/family", FAMILIES, "other image families are unsupported"):
        return doc["family"]
    return None


def _copied_body(c, doc, family):
    """Fields a profile declares and an artifact copies verbatim."""
    resources_ok = _resources(c, doc["resources"], "/resources")
    if family is not None:
        _inputs(c, doc["inputs"], "/inputs", family)
        _customization(c, doc["customization"], "/customization", family)
        _seed(c, doc["seed"], "/seed", family)
    _capabilities(c, doc["capabilities"], "/capabilities")
    _readiness(c, doc["readiness"], "/readiness")
    return resources_ok


def _builders(c, value, pointer):
    if not c.array(value, pointer, len(BUILDER_NAMES), 1):
        return False
    ok = True
    for i, item in enumerate(value):
        p = "%s/%d" % (pointer, i)
        if c.obj(item, p, ("name", "version")):
            ok = c.enum(item["name"], p + "/name", BUILDER_NAMES) and ok
            ok = c.string(item["version"], p + "/version", BUILDER_VERSION_RE,
                          "an exact MAJOR.MINOR.PATCH version; ranges are not pins") and ok
        else:
            ok = False
    if not ok:
        return False
    names = [item["name"] for item in value]
    if names != sorted(set(names)):
        c.err(pointer, "must be sorted by name and contain no duplicates")
        return False
    missing = [n for n in BUILDERS_REQUIRED if n not in names]
    if missing:
        c.err(pointer, "must include %s" % ", ".join(missing))
        return False
    return True


def _payload(c, value, pointer):
    """Returns ``{path: size_bytes}`` when the payload is valid, else None."""
    if not c.obj(value, pointer, ("format", "files", "digest")):
        return None
    ok = c.const(value["format"], pointer + "/format", PAYLOAD_FORMAT)
    ok = c.sha256(value["digest"], pointer + "/digest") and ok
    files = value["files"]
    fp = pointer + "/files"
    if not c.array(files, fp, len(PAYLOAD_PATHS), 1):
        return None
    sizes = {}
    files_ok = True
    for i, item in enumerate(files):
        p = "%s/%d" % (fp, i)
        if not c.obj(item, p, ("path", "size_bytes", "sha256")):
            files_ok = False
            continue
        files_ok = c.enum(item["path"], p + "/path", PAYLOAD_PATHS) and files_ok
        files_ok = c.integer(item["size_bytes"], p + "/size_bytes", 0, MAX_BYTES_VALUE) and files_ok
        files_ok = c.sha256(item["sha256"], p + "/sha256") and files_ok
        if isinstance(item.get("path"), str):
            sizes[item["path"]] = item.get("size_bytes")
    if not files_ok:
        return None
    paths = [item["path"] for item in files]
    if paths != sorted(set(paths)):
        c.err(fp, "must be sorted by path and contain no duplicates")
        return None
    if tuple(paths) != PAYLOAD_PATHS:
        c.err(fp, "must list exactly %s" % ", ".join(PAYLOAD_PATHS))
        return None
    if ok and value["digest"] != identity.payload_digest(files):
        c.err(pointer + "/digest", "does not match the canonical digest of payload.files")
        return None
    return sizes if ok else None


def _storage(c, value, pointer, sizes, disk_bytes):
    if not c.obj(value, pointer, ("disk_logical_bytes", "payload_bytes")):
        return
    ok = c.integer(value["disk_logical_bytes"], pointer + "/disk_logical_bytes", 0, MAX_BYTES_VALUE)
    ok = c.integer(value["payload_bytes"], pointer + "/payload_bytes", 0, MAX_BYTES_VALUE) and ok
    if not ok or sizes is None:
        return
    if disk_bytes is not None and value["disk_logical_bytes"] != disk_bytes:
        c.err(pointer + "/disk_logical_bytes", "must equal resources.disk in bytes (%d)" % disk_bytes)
    if value["disk_logical_bytes"] != sizes["disk.img"]:
        c.err(pointer + "/disk_logical_bytes", "must equal the logical size of payload disk.img")
    if value["payload_bytes"] != sum(sizes.values()):
        c.err(pointer + "/payload_bytes", "must equal the sum of payload.files size_bytes")


def _observations(c, value, pointer):
    if not c.array(value, pointer, 16):
        return
    seen = set()
    for i, item in enumerate(value):
        p = "%s/%d" % (pointer, i)
        if not c.obj(item, p, ("report_id", "observed_at", "metrics")):
            continue
        if c.string(item["report_id"], p + "/report_id", REPORT_ID_RE, "a lowercase report identifier"):
            if item["report_id"] in seen:
                c.err(p + "/report_id", "duplicate report_id")
            seen.add(item["report_id"])
        stamp = item["observed_at"]
        if c.string(stamp, p + "/observed_at", TIMESTAMP_RE, "a UTC timestamp YYYY-MM-DDTHH:MM:SSZ"):
            try:
                datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                c.err(p + "/observed_at", "is not a valid calendar time")
        metrics = item["metrics"]
        mp = p + "/metrics"
        if c.obj(metrics, mp, (), OBSERVATION_METRICS):
            if not metrics:
                c.err(mp, "must contain at least one metric")
            for key, number in metrics.items():
                c.integer(number, mp + "/" + key, 0, MAX_BYTES_VALUE)


def _validation(c, value, pointer):
    if not c.array(value, pointer, 32):
        return
    for i, item in enumerate(value):
        p = "%s/%d" % (pointer, i)
        if c.obj(item, p, ("check", "result", "evidence_sha256")):
            c.string(item["check"], p + "/check", TOKEN_RE, "a lowercase dotted token", TOKEN_MAX)
            c.enum(item["result"], p + "/result", ("pass", "fail"))
            c.sha256(item["evidence_sha256"], p + "/evidence_sha256")


def _upstream_licenses(c, value, pointer):
    if not c.array(value, pointer, 32, 1):
        return
    for i, item in enumerate(value):
        p = "%s/%d" % (pointer, i)
        if c.obj(item, p, ("component", "license", "reference")):
            c.string(item["component"], p + "/component", TOKEN_RE, "a lowercase dotted token", TOKEN_MAX)
            c.string(item["license"], p + "/license", LICENSE_RE,
                     "an SPDX identifier or expression, or LicenseRef-*")
            c.string(item["reference"], p + "/reference", HTTPS_URL_RE,
                     "an https URL without credentials, query or fragment", MAX_URL)


def _profile_ref(c, value, pointer):
    if not c.obj(value, pointer, ("label", "revision", "content_sha256")):
        return
    c.string(value["label"], pointer + "/label", LABEL_RE, "a lowercase label of at most 63 characters")
    c.integer(value["revision"], pointer + "/revision", 1, MAX_BYTES_VALUE)
    c.sha256(value["content_sha256"], pointer + "/content_sha256")


def validate(doc, check_artifact_id: bool = True) -> list:
    """Validate a parsed document. Returns a list of ``(pointer, message)``."""
    c = _Checker()
    if not isinstance(doc, dict):
        return [("", "top level must be a JSON object")]
    if "schema" not in doc:
        return [("/schema", "required field is missing")]
    if doc["schema"] != SCHEMA or not isinstance(doc["schema"], str):
        return [("/schema", "unsupported schema; only %r is accepted" % SCHEMA)]
    if not c.enum(doc.get("kind"), "/kind", KINDS):
        return c.errors
    fields = PROFILE_FIELDS if doc["kind"] == "profile" else ARTIFACT_FIELDS
    if not c.obj(doc, "", fields):
        return c.errors
    _ascii_walk(c, doc)
    family = _common(c, doc)
    resources_ok = _copied_body(c, doc, family)
    if doc["kind"] == "profile":
        c.integer(doc["revision"], "/revision", 1, MAX_BYTES_VALUE)
    else:
        c.rev(doc["esa_revision"], "/esa_revision")
        _profile_ref(c, doc["profile"], "/profile")
        _builders(c, doc["builders"], "/builders")
        sizes = _payload(c, doc["payload"], "/payload")
        disk_bytes = parse_disk(doc["resources"]["disk"]) if resources_ok else None
        _storage(c, doc["storage"], "/storage", sizes, disk_bytes)
        _observations(c, doc["observations"], "/observations")
        _validation(c, doc["validation"], "/validation")
        _upstream_licenses(c, doc["upstream_licenses"], "/upstream_licenses")
        artifact_id_ok = c.string(
            doc["artifact_id"], "/artifact_id", re.escape(identity.ARTIFACT_ID_PREFIX) + r"[0-9a-f]{64}",
            "'esa-artifact-v1:sha256:' followed by 64 lowercase hex digits",
        )
        if check_artifact_id and artifact_id_ok and not c.errors:
            if doc["artifact_id"] != identity.artifact_id(doc):
                c.err("/artifact_id", "does not match the identity derived from this manifest")
    c.errors.extend(scan.scan_document(doc))
    return c.errors


def compare_with_profile(artifact: dict, profile: dict) -> list:
    """Errors for a valid artifact that does not derive from a valid profile."""
    errors = []
    ref = artifact["profile"]
    if ref["content_sha256"] != identity.profile_content_hash(profile):
        errors.append(("/profile/content_sha256", "does not match the supplied profile's content hash"))
    if ref["revision"] != profile["revision"]:
        errors.append(("/profile/revision", "does not match the supplied profile's revision"))
    for field in COPIED_FIELDS:
        if artifact[field] != profile[field]:
            errors.append(("/" + field, "differs from the supplied profile"))
    return errors
