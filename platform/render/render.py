#!/usr/bin/env python3
"""Render .jasmin/jasmin.yaml into an Argo CD app directory with platform defaults (stack-contract §4).

usage:
  render.py SPEC OUTDIR --tenant T --domain D [--image svc=ref ...] [--suffix abc123] [--storage-class gp3]
  render.py --self-test

Output (one Application per directory, discovered by the tenant ApplicationSet):
  01-guardrails.yaml, 00-db.yaml, 05-grants.yaml, 10-migrate.yaml, 20-app.yaml, 21-services.yaml, 22-route.yaml, 23-autoscaling.yaml,
  30-netpol.yaml, 90-smoke.yaml, meta.json
Network: the cluster baseline (gitops-template 30-network-baseline.yaml) denies everything; this adds per-app allows.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM))
from execution import pod_security, container_security
SIZES = {"S": (("100m", "128Mi"), ("500m", "512Mi")),
         "M": (("250m", "256Mi"), ("1", "1Gi")),
         "L": (("500m", "512Mi"), ("2", "2Gi"))}
GATEWAY = {"name": "traefik-gateway", "namespace": "kube-system", "sectionName": "web"}
TRAEFIK = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
           "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}
TENANT_RE = re.compile(r"[a-z0-9]{1,20}")   # no '-': namespace t-<tenant>-<app> can never collide across tenants
PULL_SECRET_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
APP_IMAGE_PULL_POLICY = "Always"  # Recheck registry access; unchanged digest layers remain cacheable.
PG_IMAGE = "ghcr.io/cloudnative-pg/postgresql:17"     # ponytail: platform images still use tags; pin separately before production.
PSQL_IMAGE = "postgres:17"
CURL_IMAGE = "curlimages/curl:8.16.0"
WAVE = "argocd.argoproj.io/sync-wave"
SYNC_OPTS = "argocd.argoproj.io/sync-options"
AUTOSCALING_LABEL = "railshot.dev/replica-owner"
# Trusted platform profile; users cannot raise these limits through the workload spec.
AUTOSCALING_PROFILES = {"bounded-v1": {"max_replicas": 5, "max_app_replicas": 15,
    "pods": 30, "requests.cpu": 4000, "requests.memory": 8192,
    "limits.cpu": 16000, "limits.memory": 24576}}  # CPU millicores, memory MiB


def meta(name, ns, wave=None, extra=None, labels=None):
    m = {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/managed-by": "railshot", **(labels or {})}}
    ann = dict(extra or {})
    if wave is not None:
        ann[WAVE] = str(wave)
    if ann:
        m["annotations"] = ann
    return m


def db_objects(app, ns, storage_class):
    cluster = f"{app}-db"
    keep = {SYNC_OPTS: "Prune=confirm,Delete=false"}
    docs = [{
        "apiVersion": "generators.external-secrets.io/v1alpha1", "kind": "Password",
        "metadata": meta("railshot-db-password", ns, -2),
        "spec": {"length": 32, "digits": 6, "symbols": 0, "noUpper": False, "allowRepeat": True},   # no symbols: safe inside a URI
    }, {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": meta(cluster, ns, -1, keep),
        "spec": {"instances": 1, "imageName": PG_IMAGE, "enableSuperuserAccess": False,
                 "storage": {"size": "1Gi", "storageClass": storage_class},
                 "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
                 "bootstrap": {"initdb": {"database": "app", "owner": "app"}}},
    }]
    for role, extra in (("rw", {}), ("ro", {"inRoles": ["pg_monitor"]})):
        secret = f"{app}-db-{role}"
        docs.append({
            "apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
            "metadata": meta(secret, ns, -1, keep),
            "spec": {"refreshInterval": "0",   # generate once; rotation is P2
                     "target": {"name": secret, "template": {"type": "kubernetes.io/basic-auth", "data": {
                         "username": f"{app}_{role}", "password": "{{ .password }}",
                         "uri": f"postgresql://{app}_{role}:{{{{ .password }}}}@{cluster}-rw.{ns}:5432/app"}}},
                     "dataFrom": [{"sourceRef": {"generatorRef": {
                         "apiVersion": "generators.external-secrets.io/v1alpha1", "kind": "Password",
                         "name": "railshot-db-password"}}}]},
        })
        docs.append({
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "DatabaseRole",
            "metadata": meta(f"{app}-{role}", ns, -1, keep),
            "spec": {"cluster": {"name": cluster}, "name": f"{app}_{role}", "login": True, "superuser": False,
                     "databaseRoleReclaimPolicy": "retain", "passwordSecret": {"name": secret}, **extra},
        })
    return docs


def resources(size):
    (rq_cpu, rq_mem), (lm_cpu, lm_mem) = SIZES[size]
    return {"requests": {"cpu": rq_cpu, "memory": rq_mem}, "limits": {"cpu": lm_cpu, "memory": lm_mem}}


def autoscaling_quota(spec, enable_keda, profile_name):
    """Validate the trusted admission switches and reserve the complete app envelope."""
    import jsonschema
    jsonschema.validate(spec, json.loads((PLATFORM / "schemas/jasmin.schema.json").read_text()))
    names = [s["name"] for s in spec["services"]]
    if len(names) != len(set(names)):
        raise ValueError("service names must be unique; duplicate workloads cannot share a replica owner")
    scaled = [s for s in spec["services"] if "autoscaling" in s]
    if not scaled:
        return None
    if not enable_keda:
        raise ValueError("autoscaling requires trusted --enable-keda after KEDA and Metrics Server readiness checks")
    if profile_name not in AUTOSCALING_PROFILES:
        raise ValueError("autoscaling requires an administrator-approved autoscaling profile")
    profile = AUTOSCALING_PROFILES[profile_name]
    for svc in scaled:
        cfg = svc["autoscaling"]
        if cfg["minReplicas"] > cfg["maxReplicas"]:
            raise ValueError(f"{svc['name']}: minReplicas exceeds maxReplicas")
        if cfg["maxReplicas"] > profile["max_replicas"]:
            raise ValueError(f"{svc['name']}: maxReplicas exceeds administrator profile")
    total = {key: 0 for key in ("pods", "requests.cpu", "requests.memory", "limits.cpu", "limits.memory")}
    def reserve(res, count=1):
        total["pods"] += count
        for field in ("requests", "limits"):
            cpu, mem = res[field]["cpu"], res[field]["memory"]
            total[f"{field}.cpu"] += count * (int(cpu[:-1]) if cpu.endswith("m") else int(cpu) * 1000)
            total[f"{field}.memory"] += count * (int(mem[:-2]) * (1024 if mem.endswith("Gi") else 1))
    steady = 0
    for svc in spec["services"]:
        replicas = svc.get("autoscaling", {}).get("maxReplicas", svc.get("replicas", 1))
        steady += replicas
        # Deployments reserve one rolling surge; static Rollouts reserve both revisions plus a surge.
        reserve(resources(svc.get("size", "S")), replicas + 1 if svc.get("strategy", "rolling") == "rolling" else replicas * 2 + 1)
    if steady > profile["max_app_replicas"]:
        raise ValueError("app replica total exceeds administrator autoscaling profile")
    reserve(resources("S"))  # PostSync smoke
    if "postgres" in spec.get("resources", {}):
        # Primary + transient DB bootstrap/replacement, grants, and each migration Job.
        reserve({"requests": {"cpu": "100m", "memory": "256Mi"},
                 "limits": {"cpu": "500m", "memory": "512Mi"}}, 2)
        reserve(resources("S"))
        for svc in spec["services"]:
            if svc.get("migrate"):
                reserve(resources(svc.get("size", "S")))
    for key, value in total.items():
        if value > profile[key]:
            raise ValueError(f"app {key} reservation {value} exceeds administrator profile {profile[key]}")
    return {key: str(value) + ("m" if key.endswith(".cpu") else "Mi" if key.endswith(".memory") else "")
            for key, value in total.items()}


def guardrails(ns, quota=None):
    """Per-namespace limits. LoadBalancer/NodePort 0 keeps tenants from opening node ports."""
    return [{"apiVersion": "v1", "kind": "ResourceQuota", "metadata": meta("railshot", ns, -3),
             "spec": {"hard": {"services.loadbalancers": "0", "services.nodeports": "0", "pods": "30",
                               "persistentvolumeclaims": "2", "requests.storage": "5Gi", **(quota or {})}}},
            {"apiVersion": "v1", "kind": "LimitRange", "metadata": meta("railshot", ns, -3),
             "spec": {"limits": [{"type": "Container", "max": {"cpu": "2", "memory": "2Gi"},
                                  "default": {"cpu": "500m", "memory": "256Mi"},
                                  "defaultRequest": {"cpu": "50m", "memory": "64Mi"}}]}}]


def network_policies(ns, routed_ports, egress_hosts):
    """Allows on top of the cluster default-deny: same app, gateway → routed ports, declared hosts on 443."""
    ingress = [{"from": [{"podSelector": {}}]}]
    if routed_ports:
        ingress.append({"from": [TRAEFIK], "ports": [{"protocol": "TCP", "port": p} for p in routed_ports]})
    docs = [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta("app", ns, -3),
             "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": ingress,
                      "egress": [{"to": [{"podSelector": {}}]}]}}]   # DNS comes from the baseline
    if egress_hosts:
        docs.append({"apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy", "metadata": meta("egress", ns, -3),
                     "spec": {"endpointSelector": {"matchExpressions": [{"key": "cnpg.io/cluster", "operator": "DoesNotExist"}]},
                              "egress": [{"toFQDNs": [{"matchPattern": h} if h.startswith("*.") else {"matchName": h}
                                                      for h in egress_hosts],
                                          "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}]}})
    return docs


def checked_image_pull_secret_name(value):
    """A trusted namespace-local reference, never a credential or uploaded setting."""
    if value is not None and (not isinstance(value, str) or not PULL_SECRET_RE.fullmatch(value)):
        raise ValueError("image pull secret name must be a DNS label of at most 63 characters")
    return value


def hook_job(name, ns, wave, image, command, env, uid=65532, res=None, *, image_pull_secret_name=None,
             image_pull_policy=None):
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": meta(name, ns, wave, {"argocd.argoproj.io/hook": "Sync",
                                              "argocd.argoproj.io/hook-delete-policy": "HookSucceeded"}),
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 300, "template": {"spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                **({"imagePullSecrets": [{"name": image_pull_secret_name}]} if image_pull_secret_name else {}),
                "securityContext": pod_security(uid),
                "containers": [{"name": "run", "image": image, "command": command, "env": env,
                                **({"imagePullPolicy": image_pull_policy} if image_pull_policy else {}),
                                "securityContext": container_security(), **({"resources": res} if res else {}),
                                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "tmp", "emptyDir": {}}]}}}}


def grants_job(app, ns):
    rw, ro = f'"{app}_rw"', f'"{app}_ro"'  # app is schema-validated; quote PostgreSQL identifiers
    sql = (f"GRANT USAGE ON SCHEMA public TO {rw}, {ro};"
           f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {rw};"
           f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {rw};"
           f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ro};"
           f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {rw};"
           f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {rw};"
           f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ro};")
    env = [{"name": "PGURI", "valueFrom": {"secretKeyRef": {"name": f"{app}-db-app", "key": "uri"}}}]
    # postgres image runs as uid 999; the command only needs psql
    return hook_job(f"{app}-grants", ns, 0, PSQL_IMAGE, ["psql", "$(PGURI)", "-v", "ON_ERROR_STOP=1", "-c", sql], env, uid=999, res=resources("S"))


def service_env(app, svc, has_db, *, migration=False):
    """One env contract for runtime and the same-image migration; only DB role changes."""
    env = [{"name": "PORT", "value": str(svc["port"])}]
    env += [{"name": k, "value": v} for k, v in sorted(svc.get("env", {}).items())]
    env += [{"name": s, "valueFrom": {"secretKeyRef": {"name": f"{app}-secrets", "key": s}}}
            for s in svc.get("secrets", []) if not (has_db and s == "DATABASE_URL")]
    if has_db:
        role = "app" if migration else "rw"
        for key in (["DATABASE_URL", "MIGRATION_DATABASE_URL"] if migration else ["DATABASE_URL"]):
            env.append({"name": key, "valueFrom": {"secretKeyRef": {"name": f"{app}-db-{role}", "key": "uri"}}})
    return env


def service_objects(app, ns, svc, image, has_db, *, image_pull_secret_name=None):
    name = f"{app}-{svc['name']}"
    health = svc.get("health", "/")
    probe = {"httpGet": {"path": health, "port": svc["port"]}}
    container = {"name": svc["name"], "image": image, "imagePullPolicy": APP_IMAGE_PULL_POLICY,
                 "ports": [{"containerPort": svc["port"], "name": "http"}],
                 "env": service_env(app, svc, has_db), "securityContext": container_security(),
                 "resources": resources(svc.get("size", "S")),
                 "startupProbe": {**probe, "periodSeconds": 2, "failureThreshold": 30},
                 "readinessProbe": {**probe, "periodSeconds": 5},
                 "livenessProbe": {**probe, "periodSeconds": 10, "failureThreshold": 3},
                 "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}
    if svc.get("command"):
        container["command"] = svc["command"]
    labels = {"app.kubernetes.io/name": name}
    template = {"metadata": {"labels": labels}, "spec": {
        "automountServiceAccountToken": False, "securityContext": pod_security(),
        **({"imagePullSecrets": [{"name": image_pull_secret_name}]} if image_pull_secret_name else {}),
        "containers": [container], "volumes": [{"name": "tmp", "emptyDir": {}}]}}
    replicas = svc.get("replicas", 1)
    workload = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(name, ns, 2, labels=labels),
                "spec": {"replicas": replicas, "selector": {"matchLabels": labels}, "template": template,
                         "strategy": {"rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}}}
    if "autoscaling" in svc:
        del workload["spec"]["replicas"]  # KEDA's HPA owns /scale; Argo ignores only this labeled workload.
        workload["metadata"]["labels"][AUTOSCALING_LABEL] = "keda"
    service = {"apiVersion": "v1", "kind": "Service", "metadata": meta(name, ns, 2, labels=labels),
               "spec": {"selector": labels, "ports": [{"name": "http", "port": 80, "targetPort": svc["port"]}]}}
    return workload, service


def scaled_object(app, ns, svc):
    cfg = svc["autoscaling"]
    return {"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject",
            "metadata": meta(svc_name(app, svc), ns, 3),
            "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": svc_name(app, svc)},
                     "minReplicaCount": cfg["minReplicas"], "maxReplicaCount": cfg["maxReplicas"],
                     "pollingInterval": 30, "cooldownPeriod": cfg.get("cooldownSeconds", 300),
                     "advanced": {"restoreToOriginalReplicaCount": False, "horizontalPodAutoscalerConfig": {
                         "behavior": {"scaleUp": {"stabilizationWindowSeconds": 30,
                                                 "policies": [{"type": "Pods", "value": 1, "periodSeconds": 60}]},
                                      "scaleDown": {"stabilizationWindowSeconds": cfg.get("scaleDownStabilizationSeconds", 300),
                                                    "policies": [{"type": "Pods", "value": 1, "periodSeconds": 60}]}}}},
                     "triggers": [{"type": metric, "metricType": "Utilization", "metadata": {"value": str(cfg[metric])}}
                                  for metric in ("cpu", "memory") if metric in cfg]}}


def render(spec, out, tenant, domain, images, suffix, storage_class, *, enable_keda=False, autoscaling_profile=None,
           https_gateway=None, image_pull_secret_name=None):
    if not TENANT_RE.fullmatch(tenant):
        raise ValueError(f"tenant must match {TENANT_RE.pattern}: {tenant!r}")
    checked_image_pull_secret_name(image_pull_secret_name)
    quota = autoscaling_quota(spec, enable_keda, autoscaling_profile)
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise ValueError("render output must be an empty directory; publish a fresh complete artifact")
    for svc in spec["services"]:
        if set(svc.get("env", {})) & {"PORT", "DATABASE_URL", "MIGRATION_DATABASE_URL"} or set(svc.get("secrets", [])) & {"PORT", "MIGRATION_DATABASE_URL"}:
            raise ValueError("PORT and database role bindings are platform-owned; use DATABASE_URL secret for an external DB")
    app = spec["app"]
    # This is administrator registration, never an uploaded .jasmin option.
    if https_gateway is not None and https_gateway != f"railshot-{tenant}-{app}":
        raise ValueError("HTTPS Gateway must be the registered tenant/application Gateway")
    ns = f"t-{tenant}-{app}"
    host = f"{app}-{suffix}.{domain}"
    has_db = "postgres" in spec.get("resources", {})
    out.mkdir(parents=True, exist_ok=True)
    files = {"01-guardrails.yaml": guardrails(ns, quota)}
    if has_db:
        files["00-db.yaml"] = db_objects(app, ns, storage_class)
        files["05-grants.yaml"] = [grants_job(app, ns)]
        migrations = [hook_job(f"{app}-{s['name']}-migrate", ns, 1, images[s["name"]], s["migrate"]["command"],
                               service_env(app, s, True, migration=True),
                               res=resources(s.get("size", "S")), image_pull_secret_name=image_pull_secret_name,
                               image_pull_policy=APP_IMAGE_PULL_POLICY)
                      for s in spec["services"] if s.get("migrate")]
        if migrations:
            files["10-migrate.yaml"] = migrations
    workloads, services, rules = [], [], []
    for s in spec["services"]:
        w, svc = service_objects(app, ns, s, images[s["name"]], has_db,
                                 image_pull_secret_name=image_pull_secret_name)
        workloads.append(w)
        services.append(svc)
        if s.get("route"):
            rules.append({"matches": [{"path": {"type": "PathPrefix", "value": s["route"]}}],
                          "backendRefs": [{"name": svc["metadata"]["name"], "port": 80}]})
    rules.sort(key=lambda r: -len(r["matches"][0]["path"]["value"]))   # longest prefix first
    files["20-app.yaml"] = workloads
    files["21-services.yaml"] = services
    # Include the scaler file even for an empty set in this fresh immutable artifact.
    files["23-autoscaling.yaml"] = [scaled_object(app, ns, s) for s in spec["services"] if "autoscaling" in s]
    parents = [GATEWAY]
    if https_gateway:
        parents.append({"name": https_gateway, "namespace": "kube-system", "sectionName": "websecure"})
    files["22-route.yaml"] = [{"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
                               "metadata": meta(app, ns, 2),
                               "spec": {"parentRefs": parents, "hostnames": [host], "rules": rules}}]
    files["30-netpol.yaml"] = network_policies(ns, sorted({s["port"] for s in spec["services"] if s.get("route")}),
                                               spec.get("egress", []))
    root = next((s for s in spec["services"] if s.get("route") == "/"), next(s for s in spec["services"] if s.get("route")))
    smoke_path = root.get("health", "/")
    files["90-smoke.yaml"] = [{"apiVersion": "batch/v1", "kind": "Job",
                               "metadata": meta(f"{app}-smoke", ns, None, {"argocd.argoproj.io/hook": "PostSync",
                                                                          "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation"}),
                               "spec": {"backoffLimit": 5, "activeDeadlineSeconds": 180, "template": {"spec": {
                                   "restartPolicy": "Never", "automountServiceAccountToken": False,
                                   "securityContext": pod_security(100),
                                   "containers": [{"name": "curl", "image": CURL_IMAGE, "securityContext": container_security(), "resources": resources("S"),
                                                   "args": ["-fsS", "--max-time", "10", "--retry", "5", "--retry-all-errors",
                                                            f"http://{svc_name(app, root)}.{ns}{smoke_path}"]}]}}}}]
    for fname, docs in files.items():
        (out / fname).write_text(yaml.safe_dump_all(docs, sort_keys=False))
    scheme = "https" if https_gateway else "http"
    info = {"app": app, "tenant": tenant, "namespace": ns, "host": host, "url": f"{scheme}://{host}{root['route']}",
            "probe_url": f"{scheme}://{host}{root['route']}",
            "files": sorted(files), "db": has_db,
            "manifest_sha256": hashlib.sha256("".join((out / f).read_text() for f in sorted(files)).encode()).hexdigest()}
    if quota:
        info["autoscaling"] = {"profile": autoscaling_profile, "quota": quota,
                               "max_replicas": {s["name"]: s.get("autoscaling", {}).get("maxReplicas", s.get("replicas", 1)) for s in spec["services"]}}
    (out / "meta.json").write_text(json.dumps(info, indent=2))
    return info


def svc_name(app, svc):
    return f"{app}-{svc['name']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--tenant", default="demo")
    ap.add_argument("--domain", default="127.0.0.1.sslip.io")
    ap.add_argument("--image", action="append", default=[], help="service=image@digest")
    ap.add_argument("--suffix", default=None)
    ap.add_argument("--storage-class", default="local-path")   # MVP single node: root EBS gp3 volume
    ap.add_argument("--enable-keda", action="store_true", help="trusted platform capability assertion after live readiness checks")
    ap.add_argument("--autoscaling-profile", choices=sorted(AUTOSCALING_PROFILES), help="administrator-approved capacity/budget profile")
    ap.add_argument("--https-gateway", help="trusted registered railshot-<tenant>-<app> Gateway; does not issue certificates")
    ap.add_argument("--image-pull-secret-name", help="trusted namespace-local Secret reference; rendering does not install or verify credentials")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import jsonschema
    spec = yaml.safe_load(Path(a.spec).read_text())
    jsonschema.validate(spec, json.loads((PLATFORM / "schemas/jasmin.schema.json").read_text()))
    images = dict(i.split("=", 1) for i in a.image)
    missing = [s["name"] for s in spec["services"] if s["name"] not in images]
    if missing:
        sys.exit(f"missing --image for: {missing}")
    suffix = a.suffix or hashlib.sha256(f"{a.tenant}/{spec['app']}".encode()).hexdigest()[:6]
    print(json.dumps(render(spec, Path(a.out), a.tenant, a.domain, images, suffix, a.storage_class,
                            enable_keda=a.enable_keda, autoscaling_profile=a.autoscaling_profile,
                            https_gateway=a.https_gateway, image_pull_secret_name=a.image_pull_secret_name), indent=2))


def self_test():
    import tempfile
    spec = {"apiVersion": "jasmin/v0", "app": "memo", "resources": {"postgres": {"size": "small"}},
            "services": [{"name": "api", "build": {"dockerfile": "Dockerfile"}, "port": 8000, "health": "/healthz",
                          "route": "/", "migrate": {"command": ["python", "migrate.py"]}, "env": {"MODE": "prod"}},
                         {"name": "admin", "build": {"dockerfile": "admin.Dockerfile"}, "port": 8080, "route": "/admin",
                          "strategy": "rolling"}]}
    with tempfile.TemporaryDirectory() as d:
        info = render(spec, Path(d), "t1", "example.test", {"api": "img/api@sha256:" + "a" * 64, "admin": "img/admin@sha256:" + "b" * 64}, "x1y2z3", "gp3")
        docs = [doc for f in info["files"] for doc in yaml.safe_load_all((Path(d) / f).read_text())]
        kinds = sorted(d_["kind"] for d_ in docs)
        assert kinds.count("DatabaseRole") == 2 and "Cluster" in kinds and kinds.count("Deployment") == 2 and "Rollout" not in kinds, kinds
        route = next(x for x in docs if x["kind"] == "HTTPRoute")
        assert route["spec"]["hostnames"] == ["memo-x1y2z3.example.test"]
        assert route["spec"]["rules"][0]["matches"][0]["path"]["value"] == "/admin"      # longest prefix first
        dep = next(x for x in docs if x["kind"] == "Deployment")
        c = dep["spec"]["template"]["spec"]["containers"][0]
        assert c["securityContext"]["readOnlyRootFilesystem"] and dep["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"]
        assert any(e["name"] == "DATABASE_URL" and e["valueFrom"]["secretKeyRef"]["name"] == "memo-db-rw" for e in c["env"])
        mig = next(x for x in docs if x["kind"] == "Job" and x["metadata"]["name"].endswith("-migrate"))
        assert mig["metadata"]["annotations"][WAVE] == "1"
        mig_env = {e["name"]: e for e in mig["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert mig_env["PORT"]["value"] == "8000" and mig_env["MODE"]["value"] == "prod"
        assert all(mig_env[key]["valueFrom"]["secretKeyRef"]["name"] == "memo-db-app" for key in ("DATABASE_URL", "MIGRATION_DATABASE_URL"))
        np = next(x for x in docs if x["kind"] == "NetworkPolicy")
        assert np["spec"]["ingress"][1]["ports"] == [{"protocol": "TCP", "port": 8000}, {"protocol": "TCP", "port": 8080}]
        assert "6443" not in json.dumps(np) and "CiliumNetworkPolicy" not in kinds       # no egress declared → none
        rq = next(x for x in docs if x["kind"] == "ResourceQuota")
        assert rq["spec"]["hard"]["services.loadbalancers"] == "0" == rq["spec"]["hard"]["services.nodeports"]
        assert mig["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "512Mi"
        cl = next(x for x in docs if x["kind"] == "Cluster")
        assert cl["metadata"]["annotations"][SYNC_OPTS] == "Prune=confirm,Delete=false" and not cl["spec"]["enableSuperuserAccess"]
    with tempfile.TemporaryDirectory() as d:
        info = render({**spec, "egress": ["api.example.com", "*.stripe.com"]}, Path(d), "t1", "example.test",
                      {"api": "i@sha256:" + "a" * 64, "admin": "i@sha256:" + "b" * 64}, "x", "gp3")
        cnp = [x for x in yaml.safe_load_all((Path(d) / "30-netpol.yaml").read_text()) if x["kind"] == "CiliumNetworkPolicy"][0]
        assert cnp["spec"]["egress"][0]["toFQDNs"] == [{"matchName": "api.example.com"}, {"matchPattern": "*.stripe.com"}]
    for bad in ("a-b", "", "x" * 21, "A1"):
        try:
            render(spec, Path("/nonexistent"), bad, "d", {}, "x", "gp3")
            raise AssertionError(f"tenant {bad!r} accepted")
        except ValueError:
            pass
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
