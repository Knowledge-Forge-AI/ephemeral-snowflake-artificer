# Fake OpenSSH client for ESA tests. Never opens a connection.
#
# It answers ESA's fixed guest steps (identified by the
# `# esa-guest-step: NAME` line of the script on stdin) with canned
# esa-check-v1 reports from control.json. Like real ssh with
# SSH_ASKPASS_REQUIRE=force it asks $SSH_ASKPASS for the password, and it
# compares only a sha256 of the answer, so the credential is never logged.
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "control.json")) as handle:
    CONTROL = json.load(handle)

with open(CONTROL["log"], "a") as handle:
    handle.write(json.dumps({"tool": "ssh", "argv": sys.argv[1:], "env": dict(os.environ)}) + "\n")

env = os.environ
if env.get("SSH_ASKPASS_REQUIRE") != "force" or not env.get("SSH_ASKPASS"):
    sys.exit(255)
answer = subprocess.run([env["SSH_ASKPASS"], "admin@192.0.2.10's password: "],
                        stdout=subprocess.PIPE).stdout.rstrip(b"\n")
if hashlib.sha256(answer).hexdigest() != CONTROL["password_sha256"]:
    sys.stderr.write("Permission denied\n")
    sys.exit(255)

remote = sys.argv[-1]
if remote == "true":
    sys.exit(0)
data = sys.stdin.buffer.read()
if remote.startswith("umask 077"):
    sys.exit(0)
step = None
for line in data.decode("utf-8").splitlines():
    if line.startswith("# esa-guest-step: "):
        step = line.split(": ", 1)[1].strip()
reply = CONTROL["guest"].get(step)
if reply is None:
    sys.exit(97)
sys.stdout.write(reply)
# Stands in for the guest finalizer halting the VM after the session closes.
if step == "seal" and CONTROL.get("halt_file") and CONTROL.get("seal_halts", True):
    open(CONTROL["halt_file"], "w").close()
sys.exit(CONTROL.get("exit", {}).get(step, 0))
