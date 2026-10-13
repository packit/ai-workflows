#!/usr/bin/env bash
# Refresh the local Konflux token used by the `konflux` build backend.
#
# The backend reads its bearer token fresh per build from .secrets/konflux/token
# (mounted into the gateway container). Rather than mint a token, local runs
# reuse the SAME konflux-bot-0 token that production uses: the in-cluster
# rotation CronJob delivers it to the `konflux-bot-0-token` Secret on the
# GATEWAY cluster, and this script copies that value into the local file. You
# therefore build as konflux-bot-0 (identical to prod) and only need read access
# to that one Secret -- no konflux-tenant membership or token minting required.
#
# Re-run this when a build starts returning 401 (the delivered token is rotated
# every ~12h; pull a fresh copy).
#
# Override the oc binary with OC=/path/to/oc if it is not on PATH.
set -euo pipefail

OC="${OC:-oc}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN_FILE="${KONFLUX_TOKEN_HOST:-$REPO_ROOT/.secrets/konflux/token}"
GATEWAY_API_URL="${GATEWAY_API_URL:-https://api.gpc.ocp-hub.prod.psi.redhat.com:6443}"
GATEWAY_NAMESPACE="${GATEWAY_NAMESPACE:-jotnar-ymir--jotnar-ymir}"
SECRET_NAME="${SECRET_NAME:-konflux-bot-0-token}"
SECRET_KEY="${SECRET_KEY:-token}"

command -v "$OC" >/dev/null 2>&1 || {
  echo "ERROR: '$OC' not found. Install oc or set OC=/path/to/oc." >&2; exit 1; }

if ! "$OC" whoami >/dev/null 2>&1; then
  echo "ERROR: not logged in to the gateway cluster. Run:" >&2
  echo "  $OC login $GATEWAY_API_URL" >&2
  exit 1
fi

server="$("$OC" whoami --show-server)"
if [[ -n "$GATEWAY_API_URL" && "$server" != "$GATEWAY_API_URL" ]]; then
  echo "ERROR: logged in to '$server' but the token lives on the gateway cluster." >&2
  echo "  $OC login $GATEWAY_API_URL" >&2
  exit 1
fi

token="$("$OC" -n "$GATEWAY_NAMESPACE" get secret "$SECRET_NAME" \
  -o "jsonpath={.data.$SECRET_KEY}" 2>/dev/null | base64 -d)" || {
  echo "ERROR: could not read $SECRET_NAME in $GATEWAY_NAMESPACE." >&2
  echo "       Check you have access and the rotation CronJob has run." >&2
  exit 1; }

if [[ -z "$token" ]]; then
  echo "ERROR: $SECRET_NAME/.data.$SECRET_KEY is empty (not rotated yet?)." >&2
  exit 1
fi

# A prior container run may have chown'd the file to the container UID (the
# compose mount uses :U). We own the directory, so replace the file wholesale.
mkdir -p "$(dirname "$TOKEN_FILE")"
rm -f "$TOKEN_FILE"
( umask 077; printf '%s' "$token" > "$TOKEN_FILE" )
chmod 600 "$TOKEN_FILE"
echo "Pulled konflux-bot-0 token ($(wc -c < "$TOKEN_FILE") bytes) from $server -> $TOKEN_FILE"
echo "The token is rotated ~every 12h; re-run 'make konflux-token' if a build 401s."
