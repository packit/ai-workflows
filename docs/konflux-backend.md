# Konflux build backend

By default Ymir builds packages with Copr. Set `BUILD_BACKEND=konflux` to build
on Konflux (`kflux-rhel-p01`) instead. This document covers the extra
configuration that backend needs and how its service-account token is rotated.

## Environment

Set in `.secrets/mcp-gateway.env` (see `templates/mcp-gateway.env`):

| Variable | Example |
| -------- | ------- |
| `KONFLUX_API_URL` | `https://api.kflux-rhel-p01.fc38.p1.openshiftapps.com:6443` |
| `KUBEARCHIVE_API_URL` | `https://kubearchive-api-server-product-kubearchive.apps.kflux-rhel-p01.fc38.p1.openshiftapps.com` |
| `KONFLUX_PIPELINE_URL` | `https://gitlab.cee.redhat.com/rhel-on-konflux/rpmbuild-pipeline.git` |
| `GITLAB_TOKEN` | (already required) used for the per-build git-auth secret |

The namespace / service account / image repo defaults are baked into
`konflux.py`; override only for a different deployment via `KONFLUX_NAMESPACE`,
`KONFLUX_SERVICE_ACCOUNT`, `KONFLUX_IMAGE_REPO`.

## The SA token

Builds authenticate as `konflux-bot-0` in `ymir-tenant`. `kflux-rhel-p01`
**refuses long-lived service-account-token Secrets**, so this is a short-lived
(24h) token that must be refreshed.

`_load_config` reads the token **fresh per build** from the file named by
`KONFLUX_TOKEN_FILE`. `compose.yaml` wires this up:

- mounts `.secrets/konflux/token` (host) to `/home/mcp/konflux-token` (gateway)
- sets `KONFLUX_TOKEN_FILE=/home/mcp/konflux-token`

Because the file is read per build, **overwriting it rotates the token with no
gateway restart.**

### Refreshing locally

Local runs reuse the **same `konflux-bot-0` token as production** instead of
minting one: the in-cluster rotation CronJob already delivers a fresh token to
the `konflux-bot-0-token` Secret on the **gateway cluster**, so you just copy it
down. You build as `konflux-bot-0` (identical to prod) and only need read access
to that one Secret -- no konflux-tenant membership or token minting. (`konflux-
bot-0` cannot mint its own token anyway; see below.)

1. Log in to the **gateway** cluster (where the rotated Secret lives):
   ```bash
   oc login https://api.gpc.ocp-hub.prod.psi.redhat.com:6443
   ```
2. Pull the token into the file the gateway container reads:
   ```bash
   make konflux-token
   ```

`make konflux-token` runs `scripts/refresh-konflux-token.sh`, which verifies you
are logged in to the gateway cluster, then decodes
`secret/konflux-bot-0-token` (namespace `jotnar-ymir--jotnar-ymir`) into
`.secrets/konflux/token` (mode 0600). The next build picks it up. The delivered
token is rotated roughly every 12h, so re-run the command whenever a build
starts returning 401. Override the defaults with `GATEWAY_API_URL`,
`GATEWAY_NAMESPACE`, `SECRET_NAME`, or `OC` if your setup differs.

`konflux-bot-0` **cannot mint its own token** (platform RBAC forbids it);
production mints and rotates it with an in-cluster minter CronJob, which is
exactly the token you pull here.

## Production rotation

Automated via a CronJob in `kflux-rhel-p01/ymir-tenant`, defined in the Konflux
tenant GitOps repository (`konflux-release-data`).
