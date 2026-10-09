#!/usr/bin/env python3
"""Check the init entrypoint in built agent images without a runtime --init."""

import argparse
import json
import subprocess
import time
from uuid import uuid4

REAP_PROBE = r"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

pid1_exe = Path('/proc/1/exe').resolve().name
pid1_args = Path('/proc/1/cmdline').read_bytes().split(b'\x00')
# Under architecture emulation, /proc reports QEMU as the executable.
emulated_catatonit = (pid1_exe.startswith('qemu-')
                     and pid1_args[1:2] == [b'/usr/bin/catatonit']
                     and Path('/proc/1/comm').read_text().strip() == 'catatonit')
assert pid1_exe == 'catatonit' or emulated_catatonit, 'PID 1 is not catatonit'
assert os.getppid() == 1, 'Python is not the direct child of init'
assert os.geteuid() != 0, 'Probe must run as a non-root user'

launcher = '''
import json
import os
import time

children = []
for _ in range(10):
    pid = os.fork()
    if pid == 0:
        time.sleep(.05)
        os._exit(0)
    children.append(pid)
print(json.dumps(children), flush=True)
os._exit(0)
'''

for _ in range(10):
    result = subprocess.run([sys.executable, '-c', launcher],
                            capture_output=True, text=True, check=True)
    children = json.loads(result.stdout)
    deadline = time.monotonic() + 10
    while any(Path(f'/proc/{pid}').exists() for pid in children):
        if time.monotonic() >= deadline:
            raise AssertionError('Orphaned children were not reaped: ' + str(children))
        time.sleep(.01)

print(f'PID 1 is catatonit; UID {os.geteuid()}; 100 orphaned children reaped')
"""

SIGNAL_PROBE = """
import signal
import sys

def stop(signum, frame):
    print('received ' + signal.Signals(signum).name, flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
print('ready', flush=True)
while True:
    signal.pause()
"""

IMPORT_PROBE = """
import importlib

for agent in ('backport', 'rebase', 'rebuild', 'mr_consolidation', 'triage', 'reproducer'):
    importlib.import_module(f'ymir.agents.{agent}_agent')
print('Agent modules imported successfully')
"""


def run(engine: str, *args: str, expected_exit: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run([engine, *args], capture_output=True, text=True, timeout=60)
    if result.returncode != expected_exit:
        raise RuntimeError(
            f"{engine} {args[0]} exited {result.returncode}, expected {expected_exit}:\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def check_signal(engine: str, image: str, signame: str) -> None:
    name = f"ymir-init-test-{uuid4().hex}"
    created = False
    try:
        run(
            engine,
            "run",
            "--detach",
            "--name",
            name,
            "--network=none",
            image,
            "python3",
            "-u",
            "-c",
            SIGNAL_PROBE,
        )
        created = True
        deadline = time.monotonic() + 20
        while True:
            logs = run(engine, "logs", name)
            if "ready" in logs.stdout.splitlines():
                break
            state = json.loads(run(engine, "inspect", "--format", "{{json .State}}", name).stdout)
            if not state["Running"]:
                raise RuntimeError(
                    f"{image}: signal probe stopped before becoming ready "
                    f"(status {state['Status']}, exit {state['ExitCode']}):\n{logs.stdout}{logs.stderr}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"{image}: signal probe did not become ready within 20 seconds:\n"
                    f"{logs.stdout}{logs.stderr}"
                )
            time.sleep(0.1)
        run(engine, "kill", "--signal", signame, name)
        exit_code = run(engine, "wait", name).stdout.strip()
        if exit_code != "0":
            raise RuntimeError(f"{image}: {signame} shutdown exited {exit_code}")
        logs = run(engine, "logs", name).stdout.splitlines()
        if f"received {signame}" not in logs:
            raise RuntimeError(f"{image}: {signame} did not reach Python")
    finally:
        if created:
            run(engine, "rm", "--force", name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman", help="Container engine executable")
    parser.add_argument("images", nargs="*", default=["beeai-agent:c9s", "beeai-agent:c10s"])
    args = parser.parse_args()

    for image in args.images:
        for user_args in ([], ["--user", "12345:0"]):
            result = run(
                args.engine, "run", "--rm", "--network=none", *user_args, image, "python3", "-c", REAP_PROBE
            )
            print(f"{image}: {result.stdout.strip()}", flush=True)
        for signame in ("SIGTERM", "SIGINT"):
            check_signal(args.engine, image, signame)
        run(
            args.engine,
            "run",
            "--rm",
            "--network=none",
            image,
            "python3",
            "-c",
            "raise SystemExit(23)",
            expected_exit=23,
        )
        print(f"{image}: signals forwarded and exit code preserved", flush=True)
        result = run(args.engine, "run", "--rm", "--network=none", image, "python3", "-c", IMPORT_PROBE)
        print(f"{image}: {result.stdout.strip()}", flush=True)


if __name__ == "__main__":
    main()
