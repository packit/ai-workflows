import os
import shlex
import subprocess
import sys
from typing import List


def main(argv: List[str] | None = None) -> None:
	args = argv if argv is not None else sys.argv[1:]
	tag = os.environ.get("IMAGE_TAG", "testing-farm-sse-bridge:latest")
	containerfile = os.environ.get("CONTAINERFILE", "Containerfile")
	context = os.environ.get("BUILD_CONTEXT", ".")
	cmd = [
		"podman",
		"build",
		"-t",
		tag,
		"-f",
		containerfile,
		context,
	]
	if args:
		cmd.extend(args)
	print("Running:", " ".join(shlex.quote(p) for p in cmd))
	subprocess.check_call(cmd)


if __name__ == "__main__":
	main()
