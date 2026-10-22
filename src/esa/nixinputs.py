"""Consumer input verification and isolated build-workspace generation.

The build workspace is a directory holding only generated files: a flake
rendered from validated values, the consumer's exact ``flake.lock`` and
module bytes, ESA's recipe files and a parameter file. It is evaluated as a
pure ``path:`` flake, so the module can reach nothing outside those files,
the pinned inputs and hash-pinned fixed-output fetches whose hashes live in
the module bytes themselves. The lexer below is an early refusal of path
literals and ``<...>`` lookups, not the isolation boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from esa import custody
from esa import strictjson
from esa.errors import Refusal, UsageError

LOCK_VERSION = 7
MAX_MODULE_BYTES = 256 << 10
REF_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
RECIPES = os.path.join(custody.REPO_ROOT, "recipes")

FAMILY_LOCK = {
    "nixos-minimal": {"nixpkgs": ("NixOS", "nixpkgs", "nixpkgs")},
    "tahoe-vanilla": {
        "nixpkgs": ("NixOS", "nixpkgs", "nixpkgs"),
        "nix-darwin": ("nix-darwin", "nix-darwin", "nix_darwin"),
    },
}
RECIPE_FILES = {
    "nixos-minimal": ("nixos/minimal.nix", "nixos/injection.nix", "nixos/guest-agent.nix", "nixos/image.nix"),
    "tahoe-vanilla": ("darwin/base.nix", "darwin/guest-agent.nix"),
}
GUEST_SCRIPTS = ("probe.sh", "grow-disk.sh", "install-nix.sh", "apply-darwin.sh", "seal.sh")
EMPTY_MODULE = b"{ ... }:\n{\n}\n"


def sha256_tag(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_input(path: str, pointer: str, max_bytes: int) -> bytes:
    """Read a consumer file: a regular file (not a symlink) of bounded size."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (pointer, exc.strerror or exc)) from None
    if not stat.S_ISREG(st.st_mode):
        raise Refusal([(pointer, "must be a regular file, not a symlink or other type")])
    if st.st_size > max_bytes:
        raise Refusal([(pointer, "exceeds %d bytes" % max_bytes)])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise Refusal([(pointer, "exceeds %d bytes" % max_bytes)])
    return data


def _node_error(name, message):
    return Refusal([("flake.lock/nodes/" + name, message)])


def check_lock(data: bytes, profile: dict) -> dict:
    """Refuse a lock that differs from the profile pins. Returns ``{name: original}``."""
    inputs = profile["inputs"]
    if sha256_tag(data) != inputs["flake_lock_sha256"]:
        raise Refusal([("--flake-lock", "sha256 does not match inputs.flake_lock_sha256")])
    try:
        lock = strictjson.loads(data)
    except strictjson.StrictJSONError as exc:
        raise Refusal([("--flake-lock", "not strict JSON: %s" % exc)]) from None
    if set(lock) != {"nodes", "root", "version"} or lock["version"] != LOCK_VERSION or lock["root"] != "root":
        raise Refusal([("flake.lock", "must be a version %d lock with nodes and root 'root' only" % LOCK_VERSION)])
    expected = FAMILY_LOCK[profile["family"]]
    nodes = lock["nodes"]
    if not isinstance(nodes, dict) or set(nodes) != {"root"} | set(expected):
        raise Refusal([("flake.lock/nodes", "must hold exactly root, %s" % ", ".join(sorted(expected)))])
    root = nodes["root"]
    if root != {"inputs": {n: n for n in expected}}:
        raise _node_error("root", "root inputs must be exactly %s" % ", ".join(sorted(expected)))
    originals = {}
    for name, (owner, repo, key) in sorted(expected.items()):
        node = nodes[name]
        allowed = {"locked", "original", "inputs"} if name == "nix-darwin" else {"locked", "original"}
        if not isinstance(node, dict) or set(node) - allowed or not {"locked", "original"} <= set(node):
            raise _node_error(name, "has unexpected fields")
        if name == "nix-darwin" and node.get("inputs") != {"nixpkgs": ["nixpkgs"]}:
            raise _node_error(name, "must make nixpkgs follow the root nixpkgs input")
        locked = node["locked"]
        if not isinstance(locked, dict) or set(locked) != {"lastModified", "narHash", "owner", "repo", "rev", "type"}:
            raise _node_error(name, "locked must hold exactly lastModified, narHash, owner, repo, rev and type")
        if (locked["type"], locked["owner"], locked["repo"]) != ("github", owner, repo):
            raise _node_error(name, "must lock github:%s/%s" % (owner, repo))
        if type(locked["lastModified"]) is not int:
            raise _node_error(name, "lastModified must be an integer")
        pin = inputs[key]
        if locked["rev"] != pin["rev"]:
            raise _node_error(name, "locked rev differs from inputs.%s.rev" % key)
        if locked["narHash"] != pin["nar_hash"]:
            raise _node_error(name, "locked narHash differs from inputs.%s.nar_hash" % key)
        original = node["original"]
        if not isinstance(original, dict) or set(original) - {"owner", "repo", "type", "ref", "rev"} \
                or (original.get("type"), original.get("owner"), original.get("repo")) != ("github", owner, repo):
            raise _node_error(name, "original must be github:%s/%s with optional ref or rev" % (owner, repo))
        if "ref" in original and not (isinstance(original["ref"], str) and re.fullmatch(REF_RE, original["ref"])):
            raise _node_error(name, "original ref must be a simple branch or tag name")
        if "rev" in original and original["rev"] != pin["rev"]:
            raise _node_error(name, "original rev differs from the pinned rev")
        originals[name] = original
    return originals


def check_module(data: bytes, profile: dict) -> bytes:
    custom = profile["customization"]
    if sha256_tag(data) != custom["sha256"]:
        raise Refusal([("--module", "sha256 does not match customization.sha256")])
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal([("--module", "must be UTF-8 text")]) from None
    errors = lex_refusals(text)
    if errors:
        raise Refusal(errors)
    return data


_PATH = re.compile(r"(?<![A-Za-z0-9._+/~-])(?:~|[A-Za-z0-9._+-]*)(?:/[A-Za-z0-9._+-]+)+/?")
_SEARCH_PATH = re.compile(r"<[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*>")


def code_mask(text: str) -> str:
    """Return ``text`` with strings and comments blanked; interpolations stay code.

    Raises Refusal on an unterminated string, comment or interpolation.
    """
    out = list(text)
    stack = []  # entries: "dq", "ind" (string modes) or an int brace depth (code)
    depth = 0
    i, n = 0, len(text)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    mode = "code"
    while i < n:
        ch = text[i]
        if mode == "code":
            if ch == "#":
                end = text.find("\n", i)
                end = n if end < 0 else end
                blank(i, end)
                i = end
                continue
            if text.startswith("/*", i):
                end = text.find("*/", i + 2)
                if end < 0:
                    raise Refusal([("--module", "unterminated comment")])
                blank(i, end + 2)
                i = end + 2
                continue
            if ch == '"':
                stack.append(depth)
                depth = 0
                mode = "dq"
                blank(i, i + 1)
                i += 1
                continue
            if text.startswith("''", i):
                stack.append(depth)
                depth = 0
                mode = "ind"
                blank(i, i + 2)
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    if not stack or not isinstance(stack[-1], str):
                        raise Refusal([("--module", "unbalanced closing brace")])
                    mode = stack.pop()
                    blank(i, i + 1)
                    i += 1
                    continue
                depth -= 1
            i += 1
            continue
        if mode == "dq":
            if ch == "\\":
                blank(i, min(i + 2, n))
                i += 2
                continue
            if text.startswith("${", i):
                stack.append("dq")
                blank(i, i + 2)
                i += 2
                mode = "code"
                continue
            blank(i, i + 1)
            i += 1
            if ch == '"':
                depth = stack.pop()
                mode = "code"
            continue
        # indented string
        if text.startswith("'''", i) or text.startswith("''$", i):
            blank(i, i + 3)
            i += 3
            continue
        if text.startswith("''\\", i):
            blank(i, min(i + 4, n))
            i += 4
            continue
        if text.startswith("''", i):
            blank(i, i + 2)
            i += 2
            depth = stack.pop()
            mode = "code"
            continue
        if text.startswith("${", i):
            stack.append("ind")
            blank(i, i + 2)
            i += 2
            mode = "code"
            continue
        blank(i, i + 1)
        i += 1
    if mode != "code" or stack:
        raise Refusal([("--module", "unterminated string or interpolation")])
    return "".join(out)


def lex_refusals(text: str) -> list:
    code = code_mask(text)
    errors = []
    for match in _SEARCH_PATH.finditer(code):
        line = code.count("\n", 0, match.start()) + 1
        errors.append(("--module/line-%d" % line, "uses a <...> search-path lookup; only pinned inputs are allowed"))
    for match in _PATH.finditer(code):
        line = code.count("\n", 0, match.start()) + 1
        errors.append(("--module/line-%d" % line,
                       "contains a path literal; a self-contained module may not import or read paths"))
    return errors


def _nix_input(original: dict, extra: str = "") -> str:
    fields = ['type = "github";', 'owner = "%s";' % original["owner"], 'repo = "%s";' % original["repo"]]
    for key in ("ref", "rev"):
        if key in original:
            fields.append('%s = "%s";' % (key, original[key]))
    return "{ " + " ".join(fields) + extra + " }"


FLAKE_NIXOS = """# Generated by ESA for one construction run. Do not edit.
{
  inputs.nixpkgs = %(nixpkgs)s;
  outputs = { self, nixpkgs }:
    let
      params = builtins.fromJSON (builtins.readFile ./esa-params.json);
      system = "aarch64-linux";
      lib = nixpkgs.lib;
      nixos = lib.nixosSystem {
        inherit system;
        specialArgs = { esaParams = params; };
        modules = [
          ./esa/nixos/minimal.nix
          ./esa/nixos/injection.nix
          ./esa/nixos/guest-agent.nix
          ./consumer.nix
        ];
      };
    in {
      nixosConfigurations.esa-seed = nixos;
      packages.${system} = import ./esa/nixos/image.nix {
        inherit lib nixos params;
        pkgs = nixpkgs.legacyPackages.${system};
      };
    };
}
"""

FLAKE_DARWIN = """# Generated by ESA for one construction run. Do not edit.
{
  inputs.nixpkgs = %(nixpkgs)s;
  inputs.nix-darwin = %(nix_darwin)s;
  outputs = { self, nixpkgs, nix-darwin }:
    let
      params = builtins.fromJSON (builtins.readFile ./esa-params.json);
    in {
      darwinConfigurations.esa-seed = nix-darwin.lib.darwinSystem {
        specialArgs = { esaParams = params; };
        modules = [
          ./esa/darwin/base.nix
          ./esa/darwin/guest-agent.nix
          ./consumer.nix
        ];
      };
    };
}
"""


def render_flake(family: str, originals: dict) -> bytes:
    if family == "nixos-minimal":
        text = FLAKE_NIXOS % {"nixpkgs": _nix_input(originals["nixpkgs"])}
    else:
        text = FLAKE_DARWIN % {
            "nixpkgs": _nix_input(originals["nixpkgs"]),
            "nix_darwin": _nix_input(originals["nix-darwin"], ' inputs.nixpkgs.follows = "nixpkgs";'),
        }
    return text.encode("ascii")


def recipe_bytes(relative: str) -> bytes:
    path = os.path.join(RECIPES, relative)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise UsageError("ESA recipe %s is not a regular file" % relative)
    with open(path, "rb") as handle:
        return handle.read()


def workspace_files(profile: dict, lock: bytes, module: bytes, originals: dict, params: dict) -> dict:
    family = profile["family"]
    files = {
        "flake.nix": render_flake(family, originals),
        "flake.lock": lock,
        "consumer.nix": module,
        "esa-params.json": (json.dumps(params, sort_keys=True, indent=2) + "\n").encode("ascii"),
    }
    for relative in RECIPE_FILES[family]:
        files["esa/" + relative] = recipe_bytes(relative)
    return files


def write_tree(root: str, files: dict) -> dict:
    """Write ``files`` exclusively under a new ``root``. Returns ``{path: sha256}``."""
    os.mkdir(root, 0o700)
    hashes = {}
    for relative in sorted(files):
        path = os.path.join(root, *relative.split("/"))
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(files[relative])
        hashes[relative] = sha256_tag(files[relative])
    return hashes


def verify_tree(root: str, hashes: dict) -> None:
    """Refuse a workspace that gained, lost or changed any file."""
    found = {}
    for current, dirs, names in os.walk(root):
        for name in dirs + names:
            full = os.path.join(current, name)
            st = os.lstat(full)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise Refusal([("workspace/" + rel, "is not a regular file")])
            with open(full, "rb") as handle:
                found[rel] = sha256_tag(handle.read())
    if found != hashes:
        changed = sorted(set(found) ^ set(hashes) | {k for k in found if k in hashes and found[k] != hashes[k]})
        raise Refusal([("workspace/" + k, "differs from the prepared workspace") for k in changed])
