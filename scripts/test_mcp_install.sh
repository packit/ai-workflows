#!/bin/bash
# Smoke test: verify that ymir-common and ymir-tools can be pip-installed
# from the local source tree and that both MCP gateways start without
# import errors.
#
# This replicates the installation path described in skills_installation.md
# and guards against broken transitive imports that would prevent the
# servers from starting for end users.

set -euo pipefail

VENV_DIR=$(mktemp -d)/mcp-install-test

cleanup() { rm -rf "$(dirname "$VENV_DIR")"; }
trap cleanup EXIT

echo "==> Creating isolated venv at ${VENV_DIR}"
python3 -m venv --system-site-packages "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "==> Installing ymir-common"
pip install --no-cache-dir ./ymir/common

echo "==> Installing ymir-tools"
pip install --no-cache-dir ./ymir/tools

echo "==> Verifying gateway module imports"
python -c "from ymir.tools.privileged.gateway import main; print('  privileged gateway: OK')"
python -c "from ymir.tools.unprivileged.gateway import main; print('  unprivileged gateway: OK')"

echo "==> Verifying console-script entry points"
command -v ymir-privileged-gateway
command -v ymir-unprivileged-gateway

echo "==> Starting privileged gateway (expecting clean startup)"
MCP_TRANSPORT=stdio timeout 5 ymir-privileged-gateway &
PID_PRIV=$!

echo "==> Starting unprivileged gateway (expecting clean startup)"
MCP_TRANSPORT=stdio timeout 5 ymir-unprivileged-gateway &
PID_UNPRIV=$!

# Expected exit codes:
#   0   — server started, ran through init, and exited because stdin
#          was closed (background process gets no tty); proves startup
#          succeeded.
#   124 — timeout killed the still-running process; also proves startup
#          succeeded.
# Any other code means the gateway crashed (e.g. import error).
FAIL=0

RC_PRIV=0;   wait $PID_PRIV   || RC_PRIV=$?
RC_UNPRIV=0; wait $PID_UNPRIV || RC_UNPRIV=$?

if [ "$RC_PRIV" -ne 0 ] && [ "$RC_PRIV" -ne 124 ]; then
    echo "FAIL: privileged gateway exited with code $RC_PRIV"
    FAIL=1
fi
if [ "$RC_UNPRIV" -ne 0 ] && [ "$RC_UNPRIV" -ne 124 ]; then
    echo "FAIL: unprivileged gateway exited with code $RC_UNPRIV"
    FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
    echo "MCP gateway installation smoke test FAILED."
    exit 1
fi

echo "MCP gateway installation smoke test passed."
