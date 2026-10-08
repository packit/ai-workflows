# Encryption in Transit — Compliance Documentation

This document describes how the Ymir service deployment meets the Red Hat
encryption-in-transit requirements for data classified as Red Hat Internal
or Red Hat Restricted (PII).

## Requirement Summary

All classified data must be encrypted in transit with secure protocols
(TLS with PFS ciphers, HSTS enforcement). Internet-facing services must
enforce HTTPS with automatic HTTP-to-HTTPS redirect. Alternative secure
protocols (SSH, VPN, network-level isolation) are acceptable where TLS
is not feasible.

## External Traffic (Internet-Facing Routes)

All externally exposed OpenShift Routes enforce:

- **TLS termination** — edge or reencrypt, depending on whether the
  backend carries classified data directly
- **HTTP-to-HTTPS redirect** — `insecureEdgeTerminationPolicy: Redirect`
  on every Route
- **HSTS** — `haproxy.router.openshift.io/hsts_header` annotation with
  `max-age=31536000;includeSubDomains;preload` on every Route

### Routes and their TLS mode

| Route | Host | TLS Mode | Backend |
|-------|------|----------|---------|
| api | (auto-generated) | edge | API server (stateless) |
| phoenix | phoenix-jotnar-ymir--.../ocp-hub | edge | Phoenix trace UI |
| redis-commander | (auto-generated) | edge | Redis Commander UI |
| trace-server | trace-server-jotnar-ymir--.../ocp-hub | reencrypt | Trace server (stores classified trace data) |
| trace-server-cname | ymir.redhat.com | reencrypt | Trace server (custom domain) |

The trace-server Routes use `reencrypt` because the trace server stores
classified span data in an embedded SQLite database. This ensures that
traffic is encrypted on both the client-to-router and router-to-pod segments.

## Database and Cache Traffic

### PostgreSQL (phoenix-db)

The Phoenix database connection uses TLS with server authentication. The
client validates the PostgreSQL server certificate, but PostgreSQL does not
require a client certificate:

- The `phoenix-db` Service is annotated with
  `service.beta.openshift.io/serving-cert-secret-name: phoenix-db-tls`,
  which causes the OpenShift service-ca operator to auto-generate and
  rotate a TLS certificate for the database service
- PostgreSQL is configured with `ssl = on` using the service-ca certificate
- The Phoenix application connects with `sslmode=verify-full` and validates
  the server certificate against the OpenShift service-CA bundle mounted
  from a `service-ca-bundle` ConfigMap (annotated with
  `service.beta.openshift.io/inject-cabundle: "true"`) at
  `/etc/pki/service-ca/service-ca.crt`

### Valkey (Redis-compatible cache)

All agent-to-Valkey and internal tool connections use TLS:

- The `valkey` Service is annotated with
  `service.beta.openshift.io/serving-cert-secret-name: valkey-tls`
- Valkey is configured with `--tls-port 6379 --port 0` (TLS-only, no
  plaintext port)
- Valkey TLS client authentication is disabled (`--tls-auth-clients no`)
  since internal consumers authenticate via the Redis URL, not mTLS
- All consumers connect via the `rediss://` URI scheme (TLS-enabled Redis
  protocol)
- The service-CA trust bundle is mounted from a `service-ca-bundle`
  ConfigMap into all Redis-consuming pods at `/etc/pki/service-ca/`
- The Python Redis client verifies the server certificate against this
  CA bundle automatically

## Certificate Lifecycle and Pod Restarts

OpenShift owns the service certificates and the service CA:

- **Service CA** — valid for 26 months, automatically rotated when less than
  13 months remain. After rotation there is a 13-month grace period during
  which the original CA is still valid, but all pods that trust it must be
  restarted to pick up the new CA bundle.
- **Serving certificates** — valid for approximately two years and replaced
  automatically near expiration. Workloads that load certificates at startup
  (PostgreSQL, Valkey, trace-server) must be restarted after the Secret is
  replaced.

### Annual Rotation Procedure

A recurring Jira issue must be created to perform the following procedure
once a year. This proactive rotation avoids relying on automatic expiry
detection and ensures all workloads pick up fresh certificates well within
the grace period.

**Step 1 — Rotate the serving certificates** by deleting each Secret. The
service-ca controller regenerates them automatically:

```
oc delete secret phoenix-db-tls valkey-tls otel-collector-tls
```

**Step 2 — Restart the TLS endpoints** that load certificates at startup.
Restart servers before their clients:

```
oc rollout restart deployment/phoenix-db deployment/valkey deployment/otel-collector
oc rollout status deployment/phoenix-db
oc rollout status deployment/valkey
oc rollout status deployment/otel-collector
```

**Step 3 — Restart all client deployments** that mount the
`service-ca-bundle` ConfigMap, so they trust the new CA bundle:

```
oc rollout restart deployment/api
oc rollout restart deployment/phoenix
oc rollout restart deployment/redis-commander
oc rollout restart deployment/backport-agent-c9s deployment/backport-agent-c10s
oc rollout restart deployment/rebase-agent-c9s deployment/rebase-agent-c10s
oc rollout restart deployment/rebuild-agent-c9s deployment/rebuild-agent-c10s
oc rollout restart deployment/mr-consolidation-agent-c9s deployment/mr-consolidation-agent-c10s
oc rollout restart deployment/triage-agent deployment/reproducer-agent
```

**Step 4 — Verify connectivity.** Confirm that each restarted deployment
reaches Ready and that TLS connections succeed:

```
for secret in phoenix-db-tls valkey-tls otel-collector-tls; do
  oc get secret "$secret" -o jsonpath="${secret}: expiry={.metadata.annotations.service\\.beta\\.openshift\\.io/expiry} version={.metadata.resourceVersion}{‘\\n’}"
done
```

CronJob pods always start with the current ConfigMap, so no manual restart
is required for completed CronJob pods.

### Monitoring

The annual rotation is tracked as a recurring Jira issue assigned to the
team. The issue should be scheduled approximately 12 months after the
previous rotation — well within the 13-month grace period.

To manually rotate the service CA itself (not normally required):

```
oc delete secret/signing-key -n openshift-service-ca
```

This triggers re-generation of all serving certificates and the CA bundle.
All pods must be restarted afterward using Steps 2–4 above.

The public Route certificate is a separate concern. OpenShift manages the
default Route certificate, while the `ymir.redhat.com` custom Route continues
to use the separately supplied `ymir-cname-tls` Secret. Service certificate
rotation should not produce a browser warning; a warning or a visible outage
usually indicates an incorrectly configured Route certificate or a workload
that was not restarted after rotation.

## Internal HTTP Services (SDN Isolation)

The following internal services communicate over HTTP within the cluster:

| Service | Port | Consumers | External Route? |
|---------|------|-----------|-----------------|
| otel-collector (OTLP receiver) | 4318 | All agents, cronjobs | No |
| mcp-gateway (MCP/SSE) | 8000 | Agent pods | No |
| otel-collector → phoenix | 6006 | OTel collector exporter | No (phoenix has its own Route) |

These services are **not exposed externally** (no Routes) and operate
within the isolated OpenShift namespace `jotnar-ymir--jotnar-ymir`.

### Network isolation controls

1. **TenantEgress deny-all** — A `TenantEgress` resource
   (`openshift/tenant-egress.yml`) enforces a default-deny egress policy,
   allowing outbound traffic only to explicitly listed destinations
2. **OVN-Kubernetes SDN** — The OpenShift SDN provides network-level
   isolation between namespaces. Pod-to-pod traffic within the namespace
   does not traverse any external network segment
3. **No external ingress** — These services have no Routes and are only
   reachable from pods within the same namespace

This qualifies as an "alternative secure technology" under the policy's
allowance for VPN or network-level isolation where TLS is not
feasible or preferable for stateless relay services.

## PFS Cipher Compliance

The OpenShift router uses the cluster-wide TLS security profile. The
default profile on OpenShift 4.x is `Intermediate`, which includes only
cipher suites with Perfect Forward Secrecy (PFS) — ECDHE and DHE key
exchange algorithms.

To verify the active TLS profile:

```
oc get ingresscontroller default -n openshift-ingress-operator \
  -o jsonpath='{.spec.tlsSecurityProfile}'
```

To verify PFS ciphers are in use on a specific Route:

```
openssl s_client -connect <route-host>:443 2>/dev/null | grep -E 'Cipher|Protocol'
```

## Verification Checklist

- [ ] All Routes return `Strict-Transport-Security` header
- [ ] HTTP requests to all Routes are redirected to HTTPS
- [ ] PostgreSQL connections use SSL (`SHOW ssl` returns `on`)
- [ ] Valkey accepts only TLS connections (`valkey-cli --tls ping`)
- [ ] Trace-server Routes use `reencrypt` termination
- [ ] Cluster TLS profile enforces PFS ciphers
