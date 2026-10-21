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

## Build lifecycle

Rebase, backport, inherited backport, MR update, and MR consolidation builds
use a temporary branch on the package fork. The branch is deleted after the
build returns, including on failure. A validated change is pushed to the live
MR branch only during publication. Reruns reuse an existing open MR for that
branch instead of closing it before validation.

For agent validation, the gateway reads `ExclusiveArch` and `ExcludeArch` from
the generated SRPM and requests one compatible Konflux build architecture. It
prefers `x86_64`, then `ppc64le`, `s390x`, and `aarch64`. If none is compatible,
the tool reports an error before submitting a PipelineRun. Calls without an
SRPM retain the `x86_64` default.

The matching `build-platforms` label defaults to `linux-mxlarge/amd64` for
`x86_64`, `linux/ppc64le` for `ppc64le`, `linux/s390x` for `s390x`, and
`linux/arm64` for `aarch64`. The non-x86 labels follow the
[upstream RPM pipeline defaults](https://github.com/konflux-ci/rpmbuild-pipeline/blob/main/docs/parameters.md).
Since available labels depend on the Konflux cluster, set
`KONFLUX_BUILD_PLATFORM_X86_64`, `KONFLUX_BUILD_PLATFORM_PPC64LE`,
`KONFLUX_BUILD_PLATFORM_S390X`, or `KONFLUX_BUILD_PLATFORM_AARCH64` in the
gateway environment when a cluster uses a different label.

By default, each PipelineRun has a three-hour Tekton timeout. Ymir polls for
one additional minute to observe completion. If polling times out or the tool
call is cancelled, Ymir requests PipelineRun cancellation and waits for a
terminal status before deleting its per-build git-auth Secret. If cancellation
or status confirmation fails, Ymir keeps the Secret and logs its name for
cleanup after the run stops.
Cancellation during Secret creation waits for the creation request to finish,
then deletes the Secret if the request returned its name.

If Tekton reports a terminal `PipelineRunTimeout`, Ymir returns a build timeout
and deletes the Secret without requesting cancellation.

## The SA token

Builds authenticate as `konflux-bot-0` in `ymir-tenant`. `kflux-rhel-p01`
**refuses long-lived service-account-token Secrets**, so this is a short-lived
(24h) token that must be refreshed.

`_load_config` reads the token **fresh per build** from the file named by
`KONFLUX_TOKEN_FILE`. `compose.yaml` wires this up:

- mounts `.secrets/konflux/token` (host) to `/home/mcp/konflux-token` (gateway)
- sets `KONFLUX_TOKEN_FILE=/home/mcp/konflux-token`

Because Compose bind-mounts the file itself, `make konflux-token` updates the
existing file in place. The gateway reads the new contents on the next build
without a restart. Replacing the file would leave a running gateway attached to
the old inode.

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
`.secrets/konflux/token` (mode 0600). If Compose's `:U` mount changed the file's
host ownership, the script uses `podman unshare` to update the same file without
changing ownership. The next build picks it up. The delivered
token is rotated roughly every 12h, so re-run the command whenever a build
starts returning 401. Override the defaults with `GATEWAY_API_URL`,
`GATEWAY_NAMESPACE`, `SECRET_NAME`, or `OC` if your setup differs.

`konflux-bot-0` **cannot mint its own token** (platform RBAC forbids it);
production mints and rotates it with an in-cluster minter CronJob, which is
exactly the token you pull here.

## Production rotation

Automated via a CronJob in `kflux-rhel-p01/ymir-tenant`, defined in the Konflux
tenant GitOps repository (`konflux-release-data`).
