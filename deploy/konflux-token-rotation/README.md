# Konflux SA token rotation (`konflux-bot-0`)

The Konflux build backend authenticates to `kflux-rhel-p01` as the service
account `konflux-bot-0` in `ymir-tenant`. That cluster **refuses long-lived
service-account-token Secrets** (ValidatingAdmissionPolicy
`deny-serviceaccount-token-secrets.konflux-ci.dev`), so the token is a
short-lived (24h) bound token and **must be rotated**.

The gateway side is already rotation-ready: `_load_config` in
`ymir/tools/privileged/konflux.py` reads the token **fresh per build** from
`KONFLUX_TOKEN_FILE`, so a newly-written token file is picked up with no
restart. What remains is an automated **minter**.

## Why not just let the bot mint its own token?

It cannot. Verified live against `kflux-rhel-p01`:

- `konflux-bot-0` calling TokenRequest for itself returns **403**:
  `cannot create resource "serviceaccounts/token" ... in namespace "ymir-tenant"`.
- All **8** `konflux-*-bot-actions` ClusterRoles were inspected; **none** grants
  `serviceaccounts/token`.
- The admission policy
  `konflux-rbac-restrict-bindings-serviceaccounts-create.konflux-ci.dev`
  forbids binding a `konflux-bot-*` SA to any namespaced Role or to any
  ClusterRole outside the `konflux-*-bot-actions` family.

So the bot can never be granted self-mint. The minter must be a **different,
non-bot principal** that holds `create serviceaccounts/token` on
`konflux-bot-0`.

## Design

A dedicated **non-bot** SA `ymir-token-minter` (the `konflux-bot-*` naming
restriction does not apply to it) is granted a tiny namespaced Role that can
only mint a token for `konflux-bot-0`. A CronJob runs as that SA; its pod gets a
kubelet-projected identity automatically, so there is **no stored credential**
and **no refresh chain** that can lapse.

| File | Purpose |
| ---- | ------- |
| `service-account.yaml` | the `ymir-token-minter` SA |
| `role-konflux-bot-0-token-minter.yaml` | Role: `create serviceaccounts/token` on `konflux-bot-0` |
| `rolebinding-ymir-token-minter.yaml` | binds the minter SA to that Role |
| `cronjob-konflux-token-rotation.yaml` | mints a 24h token every 12h and delivers it |

## Where these go (NOT this repo's `openshift/`)

These resources live on **`kflux-rhel-p01`** (the only cluster where
`konflux-bot-0` exists and can be TokenRequested), managed by GitOps in the
**`konflux-release-data`** repo under
`tenants-config/cluster/kflux-rhel-p01/tenants/ymir-tenant/`. Add each file to
that directory's `kustomization.yaml`, run `./build-manifests.sh`, commit both
`cluster/` and `auto-generated/`, open an MR, and let Argo sync. See
`ymir-tenant-setup.md` for the full GitOps workflow and gotchas.

**Do not** apply these with this repo's `openshift/deploy-oc.sh` -- that targets
the `jotnar-ymir` deployment cluster, not `kflux-rhel-p01`.

## First-run verification (the one open risk)

The approach assumes the tenant permits a **non-bot SA bound to a namespaced
Role**. The gotcha-A admission policy only names `konflux-bot-*` subjects, but
the policy body could not be read with the bot's access, so confirm on first
deploy:

1. After Argo syncs, check the SA/Role/RoleBinding were admitted (no
   `SyncFailed` / `forbidden` on the RoleBinding).
2. Trigger the CronJob once:
   `oc create job --from=cronjob/konflux-token-rotation rotate-test -n ymir-tenant`
3. Read its logs: expect `Minted konflux-bot-0 token, length <N>` and
   `Delivered token to secret/konflux-bot-0-token`. A 403 here means the
   non-bot-SA + namespaced-Role path is also blocked and this needs a Konflux
   **platform** RBAC change instead (add `serviceaccounts/token`, restricted to
   its own name, into a `konflux-*-bot-actions` ClusterRole).
4. Confirm the token cap: decode the minted token's `exp-iat`. If the cluster
   clamps below 24h, shorten the CronJob `schedule` to ~cap/2.

## Delivery (still to decide for production)

The CronJob currently upserts `secret/konflux-bot-0-token` **in ymir-tenant**.
If the production gateway runs in a **different** cluster (`jotnar-ymir`, per
this repo's `openshift/`), that token must still cross clusters. Options, to be
chosen when prod is wired:

- **Push:** the CronJob writes directly into `jotnar-ymir` using a stored writer
  kubeconfig (reintroduces one long-lived cred -- scope it to patching a single
  Secret).
- **Pull:** a small job in `jotnar-ymir` reads `secret/konflux-bot-0-token` from
  `kflux-rhel-p01` and writes the local Secret the gateway mounts.

Either way the gateway consumes it the same way it does locally: a Secret
mounted as a **file** at `KONFLUX_TOKEN_FILE` (mounted secret volumes hot-reload
within ~1 min, so rotation lands without a gateway restart).

## Local development

Local dev uses **manual paste** for now (see `docs/konflux-backend.md`):
`compose.yaml` mounts `.secrets/konflux/token` into the gateway at
`/home/mcp/konflux-token` and sets `KONFLUX_TOKEN_FILE`. To rotate, overwrite
that one file; no restart needed.
