#!/usr/bin/env python3
"""Run the trusted E2E controller with a temporary rootless Podman API socket."""

import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def cleanup(controller: str) -> None:
    subprocess.run(["podman", "rm", "--force", "--ignore", controller], check=False)
    for resource in ("pod", "volume"):
        ids = subprocess.check_output(
            ["podman", resource, "ls", "-q", "--filter", f"label=ymir-triage-suite={controller}"], text=True
        ).split()
        if ids:
            subprocess.run(["podman", resource, "rm", "--force", *ids], check=True)


def main() -> int:
    args = sys.argv[1:]
    at = args.index("triage-agent-e2e-tests")
    controller = f"ymir-triage-e2e-{uuid4().hex[:12]}"

    with tempfile.TemporaryDirectory(prefix="ymir-triage-") as directory:
        socket = Path(directory) / "podman.sock"
        with subprocess.Popen(
            ["podman", "system", "service", "--time=0", f"unix://{socket}"], stdout=subprocess.DEVNULL
        ) as server:
            try:
                for _ in range(100):
                    if socket.exists() and stat.S_ISSOCK(socket.stat().st_mode):
                        break
                    if server.poll() is not None:
                        raise RuntimeError("Podman API service failed to start")
                    time.sleep(0.1)
                else:
                    raise TimeoutError("Podman API socket did not become available")
                # The host directory is private. Only the trusted controller
                # container gets the socket bind mount, under its mapped UID.
                socket.chmod(0o666)
                flags = [
                    "--name",
                    controller,
                    "-v",
                    f"{socket}:/run/ymir-podman.sock",
                    "-e",
                    "YMIR_PODMAN_URL=unix:///run/ymir-podman.sock",
                    "-e",
                    f"YMIR_TRIAGE_CONTROLLER={controller}",
                ]
                return subprocess.run([*args[:at], *flags, *args[at:]], check=False).returncode
            finally:
                try:
                    cleanup(controller)
                finally:
                    server.terminate()


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
