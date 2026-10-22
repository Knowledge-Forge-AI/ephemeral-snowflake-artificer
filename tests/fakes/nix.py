# Fake Nix CLI for ESA tests. Never evaluates or builds anything, and never
# runs ssh.
#
# `build` prints a configured store path (or fails or hangs on request);
# `store dump-path` serializes a prepared directory as a NAR on stdout,
# written here independently of ESA's reader.
#
# For an ssh:// or ssh-ng:// --store it logs the ssh argv that Nix 2.31.5
# would run (src/libstore/ssh.cc: "ssh HOST -x", shell-split NIX_SSHOPTS,
# "-i KEY", "-oUserKnownHostsFile=PIN_FILE", "-pPORT", the LocalCommand
# handshake) and the settings OpenSSH would then use. That second part is a
# MODEL of OpenSSH option handling (the first value of an option wins;
# IdentityFile and -i accumulate and "none" entries are skipped; without
# -F the user and system configs apply; with no identity OpenSSH adds the
# default key files of the passwd home). Tests over it prove consistency
# with the model, not native OpenSSH behaviour.
import base64
import json
import os
import re
import shlex
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "control.json")) as handle:
    CONTROL = json.load(handle)

PIN_FILE = "NIX_TMPDIR/host-key"


def unquote(text):
    """Percent-decoding as Boost.URL does by default: '+' stays '+'."""
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), text)


def store_ssh(uri):
    scheme, sep, rest = uri.partition("://")
    if not sep or scheme not in ("ssh", "ssh-ng"):
        return None
    authority, _, query = rest.partition("?")
    params = {}
    for item in query.split("&") if query else []:
        key, eq, value = item.partition("=")
        if eq:
            params.setdefault(unquote(key), unquote(value))  # std::map::emplace keeps the first
    port = None
    hostport = authority.rpartition("@")[2]
    if ":" in hostport:
        host, port = hostport.split(":", 1)
    else:
        host = hostport
    argv = ["ssh", authority.split(":")[0] if port else authority, "-x"]
    argv += shlex.split(os.environ.get("NIX_SSHOPTS", ""))
    if params.get("ssh-key"):
        argv += ["-i", params["ssh-key"]]
    pin_line = None
    if params.get("base64-ssh-public-host-key"):
        pin_line = host + " " + base64.b64decode(params["base64-ssh-public-host-key"]).decode("ascii") + "\n"
        argv.append("-oUserKnownHostsFile=" + PIN_FILE)
    if port:
        argv.append("-p" + port)
    argv += ["-oPermitLocalCommand=yes", "-oLocalCommand=echo started"]
    return {"argv": argv, "pin_line": pin_line, "effective": effective(argv[3:])}


def effective(args):
    first, identities, config = {}, [], None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-F", "-i", "-S", "-p"):
            value = args[i + 1]
            i += 2
        elif arg.startswith("-o"):
            value = None
            i += 1
        elif arg.startswith("-p"):
            i += 1
            continue
        else:
            raise SystemExit("fake nix: unmodelled ssh argument %r" % arg)
        if arg == "-F":
            config = value
        elif arg == "-i":
            identities.append(value)
        elif arg in ("-S", "-p"):
            continue
        else:
            key, _, val = arg[2:].partition("=")
            key = key.lower()
            if key == "identityfile":
                identities.append(val)
            else:
                first.setdefault(key, val)
    named = [x for x in identities if x.lower() != "none"]
    agent = first.get("identityagent", "SSH_AUTH_SOCK")
    if agent == "SSH_AUTH_SOCK":
        agent = os.environ.get("SSH_AUTH_SOCK", "none")
    elif agent.lower() == "none":
        agent = "none"
    return {
        "config": config or "user-and-system",
        "user_known_hosts": first.get("userknownhostsfile", "passwd-home"),
        "global_known_hosts": first.get("globalknownhostsfile", "system"),
        "strict_host_key_checking": first.get("stricthostkeychecking", "ask"),
        "batch_mode": first.get("batchmode", "no"),
        "identities": named if identities else "passwd-home-defaults",
        "agent": agent,
        "password_authentication": first.get("passwordauthentication", "yes"),
        "kbd_interactive_authentication": first.get("kbdinteractiveauthentication", "yes"),
        "forward_agent": first.get("forwardagent", "no"),
        "clear_all_forwardings": first.get("clearallforwardings", "no"),
        "permit_local_command": first.get("permitlocalcommand", "no"),
    }


record = {"tool": "nix", "argv": sys.argv[1:], "env": dict(os.environ)}
if "--store" in sys.argv:
    record["ssh"] = store_ssh(sys.argv[sys.argv.index("--store") + 1])
with open(CONTROL["log"], "a") as handle:
    handle.write(json.dumps(record) + "\n")

out = sys.stdout.buffer


def s(data):
    if isinstance(data, str):
        data = data.encode("ascii")
    out.write(struct.pack("<Q", len(data)))
    out.write(data)
    out.write(b"\0" * ((8 - len(data) % 8) % 8))


def nar(directory):
    s("nix-archive-1"); s("("); s("type"); s("directory")
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        s("entry"); s("("); s("name"); s(name); s("node"); s("(")
        if os.path.islink(path):
            s("type"); s("symlink"); s("target"); s(os.readlink(path))
        else:
            s("type"); s("regular")
            if os.access(path, os.X_OK):
                s("executable"); s("")
            size = os.path.getsize(path)
            s("contents")
            out.write(struct.pack("<Q", size))
            with open(path, "rb") as handle:
                while True:
                    block = handle.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
            out.write(b"\0" * ((8 - size % 8) % 8))
        s(")"); s(")")
    s(")")


args = sys.argv[1:]
while args and args[0] in ("--extra-experimental-features",):
    args = args[2:]
if args == ["--version"]:
    print(CONTROL.get("version", "nix (Nix) 2.31.5"))
elif args[:2] == ["store", "info"]:
    print("Store URL: %s" % args[-1])
elif args[:1] == ["build"]:
    if CONTROL.get("build_fail"):
        sys.stderr.write("error: builder for esa-seed-image failed\n")
        sys.exit(1)
    if CONTROL.get("build_sleep"):
        with open(CONTROL["sleep_pid"], "w") as handle:
            handle.write(str(os.getpid()))
        time.sleep(CONTROL["build_sleep"])
    print(CONTROL["out_path"])
elif args[:2] == ["store", "dump-path"]:
    nar(CONTROL["nar_dir"])
    out.write(CONTROL.get("nar_trailer", "").encode("ascii"))
else:
    sys.exit(64)
