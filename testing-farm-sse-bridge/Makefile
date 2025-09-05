.PHONY: build test clean run

# Default target
all: build

# Build container image
build:
	podman build -t testing-farm-sse-bridge:latest -f Containerfile .

# Build debug container image
build-debug:
	podman build -t testing-farm-sse-bridge:debug -f Containerfile --target debug .

# Run tests
test:
	python3 -m pytest

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info/ __pycache__/ .pytest_cache/

# Run locally with uv
run:
	uv pip install -e .
	testing-farm-sse-bridge --host=0.0.0.0 --port=10000 --log-level=debug

# Run container
run-container:
	podman run --rm -p 10000:10000 \
		--env-file ../.secrets/testing-farm-sse-bridge.env \
		testing-farm-sse-bridge:latest

# Run debug container
run-container-debug:
	podman run --rm -it -p 10000:10000 \
		--env-file ../.secrets/testing-farm-sse-bridge.env \
		testing-farm-sse-bridge:debug
