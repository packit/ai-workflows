# Testing Farm SSE Bridge

A FastAPI-based SSE bridge for Testing Farm requests.

## Overview

The Testing Farm SSE Bridge provides a Server-Sent Events (SSE) interface for Testing Farm requests, making it easier to integrate Testing Farm with other services.

## Features

- SSE endpoint for Testing Farm requests
- FastAPI-based REST API
- Configurable via environment variables
- Container-ready with multi-stage builds

## Quick Start

### Using Podman Compose

1. Copy the environment template:
   ```bash
   cp templates/testing-farm-sse-bridge.env .secrets/testing-farm-sse-bridge.env
   ```

2. Edit `.secrets/testing-farm-sse-bridge.env` and set your Testing Farm token.

3. Start the service:
   ```bash
   podman compose up testing-farm-sse-bridge
   ```

### Manual Setup

1. Install dependencies:
   ```bash
   pip install .
   ```

2. Set required environment variables:
   ```bash
   export TESTING_FARM_API_TOKEN=your-token-here
   export TESTING_FARM_API_URL=https://api.testing-farm.io
   ```

3. Run the service:
   ```bash
   testing-farm-sse-bridge --host=0.0.0.0 --port=10000
   ```

## Container Images

Pre-built container images are available on Quay.io:

```bash
podman pull quay.io/jotnar/testing-farm-sse-bridge:latest
```

### Building Locally

Build the production image:
```bash
podman build -t testing-farm-sse-bridge:latest -f Containerfile .
```

Build with debug tools:
```bash
podman build -t testing-farm-sse-bridge:debug -f Containerfile --target debug .
```

## Configuration

The service is configured via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| HOST | Server host | 0.0.0.0 |
| PORT | Server port | 10000 |
| LOG_LEVEL | Logging level (debug, info, warning, error) | info |
| TESTING_FARM_API_URL | Testing Farm API URL | https://api.testing-farm.io |
| TESTING_FARM_API_TOKEN | Testing Farm API token | Required |
| TESTING_FARM_POLL_INTERVAL | Poll interval in seconds | 5.0 |
| TESTING_FARM_TIMEOUT | Request timeout in seconds | 30.0 |

## Development

Install development dependencies:
```bash
pip install ".[dev]"
```

Run tests:
```bash
pytest
```

## License

MIT License - see LICENSE file for details.
