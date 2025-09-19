import os
import shlex
import subprocess
import sys
from typing import List


def main(argv: List[str] | None = None) -> None:
	args = argv if argv is not None else sys.argv[1:]
	tag = os.environ.get("IMAGE_TAG", "testing-farm-sse-bridge:latest")
	port = os.environ.get("PORT", "10000")
	api_token = os.environ.get("TESTING_FARM_API_TOKEN", "")
	container_name = os.environ.get("CONTAINER_NAME", "testing-farm-sse-bridge")

	cmd = [
		"podman",
		"run",
		"--rm",
		"--name",
		container_name,
		"-p",
		f"{port}:{port}",
	]
	if api_token:
		cmd.extend(["-e", f"TESTING_FARM_API_TOKEN={api_token}"])
	# Allow custom extra env via RUN_ENV (comma-separated KEY=VALUE)
	run_env = os.environ.get("RUN_ENV", "")
	if run_env:
		for pair in run_env.split(","):
			if "=" in pair:
				cmd.extend(["-e", pair])
	cmd.append(tag)
	# Additional args go after the image name (e.g., override CMD)
	if args:
		cmd.extend(args)
	print("Running:", " ".join(shlex.quote(p) for p in cmd))
	subprocess.check_call(cmd)


if __name__ == "__main__":
	main()
