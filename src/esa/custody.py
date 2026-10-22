"""Owned paths, private inputs and scoped cleanup.

ESA only creates destinations that are absent, and only deletes trees it
created itself: each owned directory holds an ``.esa-owned`` marker with a
random nonce, and removal checks that nonce first. There is no prune, no
overwrite and no write into a pre-existing VM root. Paths inside the ESA
source tree are refused so build state never lands in the repository.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import shutil
import stat
import sys

from esa.errors import Refusal, UsageError

MARKER = ".esa-owned"
REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CLONE_NOFOLLOW = 0x0001
BLOCK = 1 << 20


def nonce() -> str:
    return secrets.token_hex(16)


def rand_name(prefix: str) -> str:
    return prefix + secrets.token_hex(6)


def _real_parent(path: str) -> str:
    return os.path.realpath(os.path.dirname(os.path.abspath(path)))


def refuse_in_source(path: str, pointer: str) -> None:
    target = os.path.join(_real_parent(path), os.path.basename(os.path.abspath(path)))
    if target == REPO_ROOT or target.startswith(REPO_ROOT + os.sep):
        raise Refusal([(pointer, "must be outside the ESA source tree")])


def require_absent(path: str, pointer: str) -> None:
    if os.path.lexists(path):
        raise Refusal([(pointer, "already exists; ESA never overwrites or reuses a destination")])
    parent = os.path.dirname(os.path.abspath(path))
    try:
        st = os.stat(parent)
    except OSError:
        raise Refusal([(pointer, "parent directory does not exist")]) from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refusal([(pointer, "parent is not a directory")])


def new_output(path: str, pointer: str) -> str:
    """Check a caller-chosen output path: absent and outside the source tree."""
    refuse_in_source(path, pointer)
    require_absent(path, pointer)
    return os.path.abspath(path)


def create_owned_dir(path: str, pointer: str) -> str:
    """Create ``path`` (mode 0700) with an ownership marker. Returns the nonce."""
    new_output(path, pointer)
    os.mkdir(path, 0o700)
    token = nonce()
    fd = os.open(os.path.join(path, MARKER), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


def read_marker(path: str):
    marker = os.path.join(path, MARKER)
    try:
        st = os.lstat(marker)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        return None
    with open(marker) as handle:
        return handle.read(64).strip()


def remove_owned(path: str, token: str) -> None:
    """Remove a tree ESA created, after re-checking its marker."""
    if not os.path.lexists(path):
        return
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refusal([(path, "is not an ESA-owned directory; not removed")])
    if read_marker(path) != token:
        raise Refusal([(path, "ownership marker does not match; not removed")])
    _make_writable(path)
    shutil.rmtree(path)


def _make_writable(path: str) -> None:
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(root, name)
            st = os.lstat(full)
            if not stat.S_ISLNK(st.st_mode) and not st.st_mode & stat.S_IWUSR:
                os.chmod(full, stat.S_IMODE(st.st_mode) | stat.S_IWUSR)


def check_private_file(path: str, pointer: str, max_bytes: int) -> None:
    """A regular file owned by this user, unreadable by others, bounded."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (pointer, exc.strerror or exc)) from None
    if not stat.S_ISREG(st.st_mode):
        raise Refusal([(pointer, "must be a regular file, not a symlink or other type")])
    if st.st_uid != os.geteuid():
        raise Refusal([(pointer, "must be owned by the current user")])
    if st.st_mode & 0o077:
        raise Refusal([(pointer, "must not be accessible by group or others (mode 0600 or stricter)")])
    if st.st_size > max_bytes or st.st_size == 0:
        raise Refusal([(pointer, "must be between 1 and %d bytes" % max_bytes)])


def check_private_dir(path: str, pointer: str) -> None:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (pointer, exc.strerror or exc)) from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refusal([(pointer, "must be a real directory, not a symlink or file")])
    if st.st_uid != os.geteuid():
        raise Refusal([(pointer, "must be owned by the current user")])
    if st.st_mode & 0o077:
        raise Refusal([(pointer, "must not be accessible by group or others (mode 0700)")])


def clone_file(src: str, dst: str) -> None:
    """APFS clonefile(2); refuses an existing destination (EEXIST)."""
    if sys.platform != "darwin":
        raise UsageError("clone copy mode needs APFS clonefile(2); use --copy-mode sparse-copy")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.clonefile(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), dst)


def sparse_copy(src: str, dst: str) -> None:
    """Copy bytes exclusively, leaving holes where the source reads as zeros."""
    out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with open(src, "rb") as handle:
            size = 0
            while True:
                block = handle.read(BLOCK)
                if not block:
                    break
                if block.count(0) == len(block):
                    os.lseek(out, len(block), os.SEEK_CUR)
                else:
                    os.write(out, block)
                size += len(block)
        os.ftruncate(out, size)
    finally:
        os.close(out)


def copy_vm_files(src_dir: str, dst_dir: str, names, mode: str) -> None:
    """Materialize payload files into a new directory by clone or sparse copy."""
    if mode not in ("clone", "sparse-copy"):
        raise UsageError("--copy-mode must be clone or sparse-copy")
    for name in names:
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        try:
            if mode == "clone":
                clone_file(src, dst)
            else:
                sparse_copy(src, dst)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise UsageError("clone across volumes is impossible; use --copy-mode sparse-copy") from None
            raise
        os.chmod(dst, 0o600)


def same_device(a: str, b: str) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev
