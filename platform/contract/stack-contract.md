# Jasmin Stack Contract v0

What "deployable on Jasmin" means. Agents aim at this contract, the gate enforces it, and the renderer fills everything the user did not ask for with the defaults in §4. Change this file only through a reviewed PR: prompts, gates and the renderer all read it.

## 1. What an agent may produce

| File | Purpose |
|---|---|
| `Dockerfile`, `<service>.Dockerfile` | Container build for a service |
| `.dockerignore` | Keeps secrets, VCS data and build junk out of the context |
| `.jasmin/jasmin.yaml` | Workload spec (`schemas/jasmin.schema.json`) |

Nothing else. Kubernetes manifests, Terraform values, DNS records and certificates are rendered by the platform. The exact path rules are in `paths.yaml`.

## 2. Container rules

| ID | Rule | Gate layer |
|---|---|---|
| C1 | Builds with `docker buildx build --platform linux/amd64` from the declared context. | L2 build |
| C2 | Base images come from the allowlist in §5. The final stage is a slim, distroless or unprivileged variant. The platform resolves tags to digests. | L1 static |
| C3 | The final stage sets `USER` to a numeric non-root UID of 10000 or higher (default 65532). | L1, L4 |
| C4 | The process listens on `0.0.0.0` and on the port declared in `jasmin.yaml` (1024–65535). | L3 readiness |
| C5 | The declared health path answers HTTP 2xx or 3xx within 60 s of start, without auth and without side effects. The platform probe decides; a Dockerfile `HEALTHCHECK` is ignored. | L3 readiness |
| C6 | No secrets in the image, build args or context: no `.env*`, keys, tokens or credential files. `.dockerignore` excludes `.git`, `.env*`, `node_modules`, caches and build outputs. | L0, L4 |
| C7 | No CRITICAL vulnerability that has a fixed version. Fix it by moving to a newer base image or package version; ignore files are forbidden. | L4 conformance |
| C8 | Image size is at most 1 GiB compressed. | L4 |
| C9 | Build-time data generation (building a database from JSON, exporting frontend data, compiling assets) runs in a build stage; results are copied into the final stage read-only. | L2 |
| C10 | One main process per container. No `sshd`, no process supervisor unless the runtime requires it. | L1 |
| C11 | Each declared route answers from the running service, not only from a static placeholder. | L3 behavior |

## 3. Workload spec rules

- One entry in `services` per independently running process. A static frontend is its own service (served by an unprivileged web server image) unless another service already serves it.
- At least one service has a `route`. Route prefixes are distinct; `/` belongs to at most one service.
- `env` holds non-secret values only. Secret names go in `secrets`; the user supplies values through the platform's secure channel.
- Omit every field that the defaults in §4 cover. The spec records facts about the app and explicit user choices, nothing else.
- `resources` may only request what `catalog.yaml` offers.

## 4. Platform defaults (applied by the renderer)

The user never writes these. They follow the Kubernetes Pod Security Standards "restricted" profile and common PaaS defaults.

| Area | Default |
|---|---|
| Size S (default) | requests 100m CPU / 128Mi, limits 500m / 512Mi |
| Size M | requests 250m / 256Mi, limits 1 CPU / 1Gi |
| Size L | requests 500m / 512Mi, limits 2 CPU / 2Gi |
| Replicas | 1; a PodDisruptionBudget (minAvailable 1) when 2 or more |
| Probes | startup up to 60 s, then readiness and liveness HTTP GET on the health path |
| Security context | runAsNonRoot, UID from the image (65532 if unset), readOnlyRootFilesystem with an emptyDir at `/tmp`, allowPrivilegeEscalation false, all capabilities dropped, seccomp RuntimeDefault |
| Pod | no service account token, no hostPath, hostNetwork or hostPID |
| Namespace | `t-<tenant>-<app>` with ResourceQuota and LimitRange |
| Network | default deny both ways (cluster baseline). Ingress: gateway to routed ports, same app. Egress: DNS, same app, `egress` hosts on TCP 443. IMDS, kube-apiserver and SMTP always denied |
| Routing | HTTPRoute on `<app>-<random6>.<platform-domain>`, one path rule per `route`, TLS from the platform's wildcard certificate |
| Image | GHCR, referenced by digest |
| Rollout | RollingUpdate with maxUnavailable 0; smoke test on the public URL after sync; last known good (LKG) restore on failure |
| Database (when requested) | CloudNativePG `Cluster` per app (PostgreSQL 17). The app gets `DATABASE_URL` for a DML-only runtime role; only the migration Job gets `MIGRATION_DATABASE_URL` (owner). Never a superuser |
| Migrations (when `migrate.command` is set) | Argo CD Sync-phase hook Job at sync-wave 1 (database wave -1, app wave 2) with the app image, no retries, 5 min limit, failed Jobs kept as evidence; migrations must be idempotent because hooks rerun on every sync; the gate runs them first against an ephemeral `postgres:17` |
| Release strategy | RollingUpdate by default; `strategy: canary` or `bluegreen` renders an Argo Rollouts `Rollout` with Gateway API traffic splitting and a Prometheus AnalysisTemplate (5xx rate, p95 latency) |
| Logs | stdout and stderr only |

## 5. Base image allowlist

| Use | Images |
|---|---|
| Python | `python:3.12-slim`, `python:3.13-slim` |
| Node.js | `node:22-slim`, `node:24-slim` |
| Go, Rust (build stage) | `golang:1`, `rust:1` |
| Final stage, static binaries | `gcr.io/distroless/static-debian12`, `gcr.io/distroless/base-debian12` |
| Static web | `nginxinc/nginx-unprivileged:stable-alpine` |
| JVM | `eclipse-temurin:21-jre` |

## 6. Forbidden changes

- Editing, renaming or deleting tests, CI configuration, lockfiles, dependency manifests, policies, this contract, or agent instruction files (`paths.yaml`).
- Weakening a check: `|| true`, `exit 0` in commands, skipped tests, `--no-verify`, `.trivyignore`, lower scan severity, a health path that stays green while the app is down.
- Changing application source code (team decision D1 pending; forbidden until then).
- Fetching and executing remote scripts during the build (`curl … | sh`).
