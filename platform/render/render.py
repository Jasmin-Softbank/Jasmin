#!/usr/bin/env python3
"""Render .jasmin/jasmin.yaml into an Argo CD app directory with platform defaults (stack-contract §4).

usage:
  render.py SPEC OUTDIR --tenant T --domain D [--image svc=ref ...] [--suffix abc123] [--storage-class gp3]
  render.py --self-test

Output (one Application per directory, discovered by the tenant ApplicationSet):
  00-db.yaml, 05-grants.yaml, 10-migrate.yaml, 20-app.yaml, 21-services.yaml, 22-route.yaml,
  30-netpol.yaml, 90-smoke.yaml, meta.json
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

PLATFORM = Path(__file__).resolve().parents[1]
SIZES = {"S": (("100m", "128Mi"), ("500m", "512Mi")),
         "M": (("250m", "256Mi"), ("1", "1Gi")),
         "L": (("500m", "512Mi"), ("2", "2Gi"))}
GATEWAY = {"name": "traefik-gateway", "namespace": "kube-system", "sectionName": "web"}
ALLOWED_NS = [GATEWAY["namespace"], "cnpg-system", "monitoring"]   # ingress into tenant pods
PG_IMAGE = "ghcr.io/cloudnative-pg/postgresql:17"     # ponytail: tag here; the pipeline pins the digest
PSQL_IMAGE = "postgres:17"
CURL_IMAGE = "curlimages/curl:8.16.0"
WAVE = "argocd.argoproj.io/sync-wave"
SYNC_OPTS = "argocd.argoproj.io/sync-options"


def meta(name, ns, wave=None, extra=None, labels=None):
    m = {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/managed-by": "railshot", **(labels or {})}}
    ann = dict(extra or {})
    if wave is not None:
        ann[WAVE] = str(wave)
    if ann:
        m["annotations"] = ann
    return m


def pod_security(uid=65532):
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"}}


def container_security():
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


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


def hook_job(name, ns, wave, image, command, env, uid=65532):
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": meta(name, ns, wave, {"argocd.argoproj.io/hook": "Sync",
                                              "argocd.argoproj.io/hook-delete-policy": "HookSucceeded"}),
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 300, "template": {"spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": pod_security(uid),
                "containers": [{"name": "run", "image": image, "command": command, "env": env,
                                "securityContext": container_security(),
                                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "tmp", "emptyDir": {}}]}}}}


def grants_job(app, ns):
    sql = (f"GRANT USAGE ON SCHEMA public TO {app}_rw, {app}_ro;"
           f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {app}_rw;"
           f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {app}_rw;"
           f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {app}_ro;"
           f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {app}_rw;"
           f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {app}_rw;"
           f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {app}_ro;")
    env = [{"name": "PGURI", "valueFrom": {"secretKeyRef": {"name": f"{app}-db-app", "key": "uri"}}}]
    # postgres image runs as uid 999; the command only needs psql
    return hook_job(f"{app}-grants", ns, 0, PSQL_IMAGE, ["psql", "$(PGURI)", "-v", "ON_ERROR_STOP=1", "-c", sql], env, uid=999)


def service_objects(app, ns, svc, image, has_db):
    name = f"{app}-{svc['name']}"
    (rq_cpu, rq_mem), (lm_cpu, lm_mem) = SIZES[svc.get("size", "S")]
    health = svc.get("health", "/")
    env = [{"name": "PORT", "value": str(svc["port"])}]
    env += [{"name": k, "value": v} for k, v in sorted(svc.get("env", {}).items())]
    env += [{"name": s, "valueFrom": {"secretKeyRef": {"name": f"{app}-secrets", "key": s}}} for s in svc.get("secrets", [])]
    if has_db:
        env.append({"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": f"{app}-db-rw", "key": "uri"}}})
    probe = {"httpGet": {"path": health, "port": svc["port"]}}
    container = {"name": svc["name"], "image": image, "ports": [{"containerPort": svc["port"], "name": "http"}],
                 "env": env, "securityContext": container_security(),
                 "resources": {"requests": {"cpu": rq_cpu, "memory": rq_mem}, "limits": {"cpu": lm_cpu, "memory": lm_mem}},
                 "startupProbe": {**probe, "periodSeconds": 2, "failureThreshold": 30},
                 "readinessProbe": {**probe, "periodSeconds": 5},
                 "livenessProbe": {**probe, "periodSeconds": 10, "failureThreshold": 3},
                 "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}
    if svc.get("command"):
        container["command"] = svc["command"]
    labels = {"app.kubernetes.io/name": name}
    template = {"metadata": {"labels": labels}, "spec": {
        "automountServiceAccountToken": False, "securityContext": pod_security(),
        "containers": [container], "volumes": [{"name": "tmp", "emptyDir": {}}]}}
    strategy = svc.get("strategy", "rolling")
    replicas = svc.get("replicas", 1)
    if strategy == "rolling":
        workload = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(name, ns, 2, labels=labels),
                    "spec": {"replicas": replicas, "selector": {"matchLabels": labels}, "template": template,
                             "strategy": {"rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}}}
    else:  # ponytail: Rollout skeleton; traffic routing + AnalysisTemplate wiring is P1 (PRD §8)
        steps = [{"setWeight": 20}, {"pause": {"duration": "60s"}}, {"setWeight": 50}, {"pause": {"duration": "60s"}}]
        workload = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Rollout", "metadata": meta(name, ns, 2, labels=labels),
                    "spec": {"replicas": replicas, "selector": {"matchLabels": labels}, "template": template,
                             "strategy": {"canary": {"steps": steps}} if strategy == "canary"
                             else {"blueGreen": {"activeService": name, "autoPromotionEnabled": False}}}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": meta(name, ns, 2, labels=labels),
               "spec": {"selector": labels, "ports": [{"name": "http", "port": 80, "targetPort": svc["port"]}]}}
    return workload, service


def render(spec, out, tenant, domain, images, suffix, storage_class):
    app = spec["app"]
    ns = f"t-{tenant}-{app}"
    host = f"{app}-{suffix}.{domain}"
    has_db = "postgres" in spec.get("resources", {})
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    if has_db:
        files["00-db.yaml"] = db_objects(app, ns, storage_class)
        files["05-grants.yaml"] = [grants_job(app, ns)]
        migrations = [hook_job(f"{app}-{s['name']}-migrate", ns, 1, images[s["name"]], s["migrate"]["command"],
                               [{"name": "MIGRATION_DATABASE_URL",
                                 "valueFrom": {"secretKeyRef": {"name": f"{app}-db-app", "key": "uri"}}}])
                      for s in spec["services"] if s.get("migrate")]
        if migrations:
            files["10-migrate.yaml"] = migrations
    workloads, services, rules = [], [], []
    for s in spec["services"]:
        w, svc = service_objects(app, ns, s, images[s["name"]], has_db)
        workloads.append(w)
        services.append(svc)
        if s.get("route"):
            rules.append({"matches": [{"path": {"type": "PathPrefix", "value": s["route"]}}],
                          "backendRefs": [{"name": svc["metadata"]["name"], "port": 80}]})
    rules.sort(key=lambda r: -len(r["matches"][0]["path"]["value"]))   # longest prefix first
    files["20-app.yaml"] = workloads
    files["21-services.yaml"] = services
    files["22-route.yaml"] = [{"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
                               "metadata": meta(app, ns, 2),
                               "spec": {"parentRefs": [GATEWAY], "hostnames": [host], "rules": rules}}]
    files["30-netpol.yaml"] = [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                                "metadata": meta("default", ns),
                                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                                         "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": n}}} for n in ALLOWED_NS]
                                                      + [{"podSelector": {}}]}],
                                         "egress": [{"to": [{"podSelector": {}}]},
                                                    {"ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                                                    {"ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 6443}]}]}}]
    root = next((s for s in spec["services"] if s.get("route") == "/"), next(s for s in spec["services"] if s.get("route")))
    smoke_path = root.get("health", "/")
    files["90-smoke.yaml"] = [{"apiVersion": "batch/v1", "kind": "Job",
                               "metadata": meta(f"{app}-smoke", ns, None, {"argocd.argoproj.io/hook": "PostSync",
                                                                          "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation"}),
                               "spec": {"backoffLimit": 5, "activeDeadlineSeconds": 180, "template": {"spec": {
                                   "restartPolicy": "Never", "automountServiceAccountToken": False,
                                   "securityContext": pod_security(100),
                                   "containers": [{"name": "curl", "image": CURL_IMAGE, "securityContext": container_security(),
                                                   "args": ["-fsS", "--max-time", "10", "--retry", "5", "--retry-all-errors",
                                                            f"http://{svc_name(app, root)}.{ns}{smoke_path}"]}]}}}}]
    for fname, docs in files.items():
        (out / fname).write_text(yaml.safe_dump_all(docs, sort_keys=False))
    info = {"app": app, "tenant": tenant, "namespace": ns, "host": host, "url": f"http://{host}",
            "files": sorted(files), "db": has_db,
            "manifest_sha256": hashlib.sha256("".join((out / f).read_text() for f in sorted(files)).encode()).hexdigest()}
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
    print(json.dumps(render(spec, Path(a.out), a.tenant, a.domain, images, suffix, a.storage_class), indent=2))


def self_test():
    import tempfile
    spec = {"apiVersion": "jasmin/v0", "app": "memo", "resources": {"postgres": {"size": "small"}},
            "services": [{"name": "api", "build": {"dockerfile": "Dockerfile"}, "port": 8000, "health": "/healthz",
                          "route": "/", "migrate": {"command": ["python", "migrate.py"]}, "env": {"MODE": "prod"}},
                         {"name": "admin", "build": {"dockerfile": "admin.Dockerfile"}, "port": 8080, "route": "/admin",
                          "strategy": "canary"}]}
    with tempfile.TemporaryDirectory() as d:
        info = render(spec, Path(d), "t1", "example.test", {"api": "img/api@sha256:" + "a" * 64, "admin": "img/admin@sha256:" + "b" * 64}, "x1y2z3", "gp3")
        docs = [doc for f in info["files"] for doc in yaml.safe_load_all((Path(d) / f).read_text())]
        kinds = sorted(d_["kind"] for d_ in docs)
        assert kinds.count("DatabaseRole") == 2 and "Cluster" in kinds and "Rollout" in kinds and "Deployment" in kinds, kinds
        route = next(x for x in docs if x["kind"] == "HTTPRoute")
        assert route["spec"]["hostnames"] == ["memo-x1y2z3.example.test"]
        assert route["spec"]["rules"][0]["matches"][0]["path"]["value"] == "/admin"      # longest prefix first
        dep = next(x for x in docs if x["kind"] == "Deployment")
        c = dep["spec"]["template"]["spec"]["containers"][0]
        assert c["securityContext"]["readOnlyRootFilesystem"] and dep["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"]
        assert any(e["name"] == "DATABASE_URL" and e["valueFrom"]["secretKeyRef"]["name"] == "memo-db-rw" for e in c["env"])
        mig = next(x for x in docs if x["kind"] == "Job" and x["metadata"]["name"].endswith("-migrate"))
        assert mig["metadata"]["annotations"][WAVE] == "1"
        assert all(e["valueFrom"]["secretKeyRef"]["name"] == "memo-db-app" for e in mig["spec"]["template"]["spec"]["containers"][0]["env"])
        cl = next(x for x in docs if x["kind"] == "Cluster")
        assert cl["metadata"]["annotations"][SYNC_OPTS] == "Prune=confirm,Delete=false" and not cl["spec"]["enableSuperuserAccess"]
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
