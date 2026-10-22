# Fake Tart 2.40.1 for ESA tests. Never runs a VM.
#
# Behaviour is driven by control.json next to this script. It mimics only
# the command shapes and on-disk effects ESA relies on: standalone VM
# directories under $TART_HOME/vms, config.json rewrites by `set`, a
# config.json record lock held while `run` is "running", the control.sock
# Unix socket `run` binds and leaves behind at exit, truncating `export`,
# replacing `import`. Every invocation is logged.
import base64
import ctypes
import fcntl
import json
import os
import shutil
import signal
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "control.json")) as handle:
    CONTROL = json.load(handle)


def log(record):
    with open(CONTROL["log"], "a") as handle:
        handle.write(json.dumps(record) + "\n")


log({"tool": "tart", "argv": sys.argv[1:], "env": dict(os.environ)})

if os.environ.get("TART_NO_AUTO_PRUNE") != "1" or not os.environ.get("TART_HOME"):
    sys.stderr.write("fake tart: refusing to run without an explicit TART_HOME and TART_NO_AUTO_PRUNE=1\n")
    sys.exit(9)
HOME = os.environ["TART_HOME"]
VMS = os.path.join(HOME, "vms")


def vm(name):
    return os.path.join(VMS, name)


def fail(step):
    if step in CONTROL.get("fail", []):
        sys.stderr.write("fake tart: injected failure in %s\n" % step)
        sys.exit(1)


def sparse_copy(src, dst):
    with open(src, "rb") as i, open(dst, "wb") as o:
        size = 0
        while True:
            block = i.read(1 << 20)
            if not block:
                break
            if block.count(0) == len(block):
                o.seek(len(block), 1)
            else:
                o.write(block)
            size += len(block)
        o.truncate(size)


def load(name):
    with open(os.path.join(vm(name), "config.json")) as handle:
        return json.load(handle)


def save(name, cfg):
    with open(os.path.join(vm(name), "config.json"), "w") as handle:
        handle.write(json.dumps(cfg, indent=2, sort_keys=True))


def bind_unix_socket(name):
    """bind(2) a relative AF_UNIX path in the cwd; returns the listening fd.

    Through ctypes, because the module floor does not admit ``socket``.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.socket(1, 1, 0)  # AF_UNIX, SOCK_STREAM on macOS and Linux
    if fd < 0:
        raise OSError(ctypes.get_errno(), "socket")
    raw = os.fsencode(name)
    if sys.platform == "darwin":
        addr = struct.pack("BB104s", 106, 1, raw)  # sun_len, sun_family, sun_path
    else:
        addr = struct.pack("=H108s", 1, raw)  # sun_family, sun_path
    if libc.bind(fd, addr, len(addr)) != 0 or libc.listen(fd, 1) != 0:
        code = ctypes.get_errno()
        os.close(fd)
        raise OSError(code, os.strerror(code), name)
    return fd


def control_residue(kind):
    """What `run` leaves at control.sock in the cwd; a genuine socket by default."""
    if kind == "socket":
        return bind_unix_socket("control.sock")
    if kind == "file":
        open("control.sock", "w").close()
    elif kind == "dir":
        os.mkdir("control.sock")
    elif kind == "symlink":
        os.close(bind_unix_socket("elsewhere.sock"))
        os.symlink(os.path.abspath("elsewhere.sock"), "control.sock")
    return None


def random_mac():
    raw = bytearray(os.urandom(6))
    raw[0] = (raw[0] | 0x02) & 0xFE
    return ":".join("%02x" % b for b in raw)


args = sys.argv[1:]
cmd = args[0] if args else ""
if cmd == "--version":
    print(CONTROL.get("version", "2.40.1"))
elif cmd == "create":
    fail("create")
    assert args[1] == "--linux" and args[2] == "--disk-size"
    name = args[4]
    os.makedirs(vm(name))
    save(name, {
        "version": 1, "os": "linux", "arch": "arm64", "cpuCountMin": 4, "cpuCount": 4,
        "memorySizeMin": 4294967296, "memorySize": 4294967296,
        "macAddress": CONTROL.get("create_mac", "02:00:00:00:00:aa"),
        "display": {"width": 1024, "height": 768}, "diskFormat": "raw",
    })
    with open(os.path.join(vm(name), "disk.img"), "wb") as handle:
        handle.truncate(int(args[3]) * 1000 ** 3)
    with open(os.path.join(vm(name), "nvram.bin"), "wb") as handle:
        handle.write(b"\x01" * 4096)
elif cmd == "set":
    fail("set")
    name = args[1]
    cfg = load(name)
    opts = args[2:]
    cfg.pop("displayRefit", None)  # Tart 2.40.1 writes displayRefit = nil unless a flag is given
    for key, default in (("os", "darwin"), ("arch", "arm64"), ("diskFormat", "raw"),
                         ("display", {"width": 1024, "height": 768})):
        cfg.setdefault(key, default)  # re-encoding writes the full form
    i = 0
    while i < len(opts):
        opt = opts[i]
        if opt == "--cpu":
            cfg["cpuCount"] = int(opts[i + 1]); i += 2
        elif opt == "--memory":
            cfg["memorySize"] = int(opts[i + 1]) * 1024 * 1024; i += 2
        elif opt == "--disk":
            tmp = os.path.join(vm(name), "disk.img.tmp")
            sparse_copy(opts[i + 1], tmp)
            os.replace(tmp, os.path.join(vm(name), "disk.img")); i += 2
        elif opt == "--disk-size":
            path = os.path.join(vm(name), "disk.img")
            size = int(opts[i + 1]) * 1000 ** 3
            if size < os.path.getsize(path):
                sys.exit(1)
            with open(path, "r+b") as handle:
                handle.truncate(size)
            i += 2
        elif opt == "--random-mac":
            cfg["macAddress"] = random_mac(); i += 1
        elif opt == "--random-serial":
            cfg["ecid"] = base64.b64encode(os.urandom(16)).decode("ascii"); i += 1
        elif opt == "--display-refit":
            cfg["displayRefit"] = True; i += 1
        elif opt == "--no-display-refit":
            cfg["displayRefit"] = False; i += 1
        else:
            sys.exit(64)
    cfg.update(CONTROL.get("set_extra", {}))
    save(name, cfg)
elif cmd == "clone":
    fail("clone")
    if args[1] != CONTROL["base_ref"]:
        sys.stderr.write("fake tart: unexpected clone source\n")
        sys.exit(1)
    name = args[2]
    # Tart 2.40.1 Clone: a new MAC only if a local VM already uses the
    # source's MAC (VMStorageLocal.hasVMsWithMACAddress); otherwise
    # VMDirectory.clone copies config, NVRAM and disk byte for byte.
    with open(os.path.join(CONTROL["base_dir"], "config.json")) as handle:
        source_mac = json.load(handle)["macAddress"]
    collision = any(load(other).get("macAddress") == source_mac
                    for other in (os.listdir(VMS) if os.path.isdir(VMS) else []))
    os.makedirs(vm(name))
    for f in ("config.json", "disk.img", "nvram.bin"):
        sparse_copy(os.path.join(CONTROL["base_dir"], f), os.path.join(vm(name), f))
    if collision:
        cfg = load(name)
        cfg["macAddress"] = random_mac()
        save(name, cfg)
elif cmd == "run":
    name = args[-1]
    fd = os.open(os.path.join(vm(name), "config.json"), os.O_RDWR)
    fcntl.lockf(fd, fcntl.LOCK_EX)
    with open(CONTROL["run_pid"], "w") as handle:
        handle.write(str(os.getpid()))
    # Tart 2.40.1 Run/ControlSocket: chdir into the VM directory (for the
    # 104-byte sun_path limit), remove residue of a previous run, bind
    # control.sock. Closing at exit leaves the pathname in place.
    os.chdir(vm(name))
    if os.path.lexists("control.sock"):
        os.unlink("control.sock")
    control_fd = control_residue(CONTROL.get("control_socket", "socket"))
    for extra in CONTROL.get("run_extra", []):
        open(extra, "w").close()
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    halt = CONTROL.get("halt_file")
    while not stop["now"] and not (halt and os.path.exists(halt)):
        time.sleep(0.05)
    if halt and os.path.exists(halt) and CONTROL.get("halt_writes_disk"):
        with open(os.path.join(vm(name), "disk.img"), "r+b") as handle:
            handle.seek(4096)
            handle.write(b"guest-written")
    # Recorded at exit, so tests can show nothing removed the socket while the VM ran.
    log({"tool": "tart-event", "event": "run-exit", "control_sock_present": os.path.lexists("control.sock")})
    if control_fd is not None:
        os.close(control_fd)
    os.close(fd)
    sys.exit(CONTROL.get("run_exit", 0))
elif cmd == "ip":
    fail("ip")
    print(CONTROL.get("ip", "192.0.2.10"))
elif cmd == "delete":
    fail("delete")
    shutil.rmtree(vm(args[1]))
elif cmd == "export":
    fail("export")
    # Stand-in for AppleArchive: config and NVRAM in full, disk as non-zero blocks.
    src = vm(args[1])
    with open(src + "/config.json", "rb") as handle:
        config = handle.read()
    with open(src + "/nvram.bin", "rb") as handle:
        nvram = handle.read()
    blocks = []
    with open(src + "/disk.img", "rb") as handle:
        offset = 0
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            if block.count(0) != len(block):
                blocks.append([offset, base64.b64encode(block).decode("ascii")])
            offset += len(block)
    with open(args[2], "w") as handle:  # truncates an existing file, as Tart does
        json.dump({"config": base64.b64encode(config).decode("ascii"),
                   "nvram": base64.b64encode(nvram).decode("ascii"),
                   "disk_size": offset, "blocks": blocks}, handle)
elif cmd == "import":
    fail("import")
    name = args[2]
    with open(args[1]) as handle:
        archive = json.load(handle)
    if os.path.exists(vm(name)):
        shutil.rmtree(vm(name))  # Tart replaces an existing destination
    os.makedirs(vm(name))
    with open(vm(name) + "/config.json", "wb") as handle:
        handle.write(base64.b64decode(archive["config"]))
    with open(vm(name) + "/nvram.bin", "wb") as handle:
        handle.write(base64.b64decode(archive["nvram"]))
    with open(vm(name) + "/disk.img", "wb") as handle:
        handle.truncate(archive["disk_size"])
        for offset, data in archive["blocks"]:
            handle.seek(offset)
            handle.write(base64.b64decode(data))
    if CONTROL.get("import_mutate_mac"):
        cfg = load(name)
        cfg["macAddress"] = random_mac()
        save(name, cfg)
else:
    sys.exit(64)
