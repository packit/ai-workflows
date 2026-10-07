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

The Phoenix database connection uses TLS with mutual authentication:

- The `phoenix-db` Service is annotated with
  `service.beta.openshift.io/serving-cert-secret-name: phoenix-db-tls`,
  which causes the OpenShift service-ca operator to auto-generate and
  rotate a TLS certificate for the database service
- PostgreSQL is configured with `ssl = on` using the service-ca certificate
- The Phoenix application connects with `sslmode=verify-full` and validates
  the server certificate against the service-ca root at
  `/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt`

### Valkey (Redis-compatible cache)

All agent-to-Valkey and internal tool connections use TLS:

- The `valkey` Service is annotated with
  `service.beta.openshift.io/serving-cert-secret-name: valkey-tls`
- Valkey is configured with `--tls-port 6379 --port 0` (TLS-only, no
  plaintext port)
- All consumers connect via the `rediss://` URI scheme (TLS-enabled Redis
  protocol)
- The service-ca root certificate is used for server verification

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
