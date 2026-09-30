#!/usr/bin/env python3
"""Deterministic gate: L0 patch policy, L1 static, L2 build, L3 readiness, L4 conformance.

usage:
  gate.py WORKSPACE RUN [--layers L0,L1,L2,L3,L4]
  gate.py --self-test

Writes RUN/verdict.json and, on failure, RUN/failure.txt (input for the fixer).
The workspace is a git repo whose HEAD is the imported source; agent changes are uncommitted.
L2–L4 need Docker; without it they report "blocked" and the verdict is not ok.
"""
import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM / "runner"))
from run_agent import path_ok  # noqa: E402

SECRET = re.compile(r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|xox[bpas]-[0-9A-Za-z-]{10,}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:password\s*[=:]\s*\S+)")
TRIVY = "aquasec/trivy:0.74.0"
PG = "postgres:17"
MAX_IMAGE_BYTES = 2 * 1024 ** 3          # ponytail: uncompressed proxy for the 1 GiB compressed rule
CLASS_RULES = [                          # (layer, regex, class) — first match wins
    ("L2", r"No matching distribution|ERESOLVE|npm ERR!|ModuleNotFoundError|Could not find a version|Unable to locate package|pip.*error", "F1"),
    ("L2", r"exec format error|no match for platform", "F3"),
    ("L2", r"TLS handshake timeout|i/o timeout|429 Too Many Requests|connection reset by peer|temporary failure in name resolution", "F8"),
    ("L2", r"COPY failed|failed to compute cache key|not found|no such file", "F2"),
    ("L3", r"ModuleNotFoundError|ImportError|Cannot find module", "F1"),
    ("L3", r"migrat", "F9"),
    ("L4", r"CRITICAL", "F6"),
]
DEFAULT_CLASS = {"L0": "F5", "L1": "F5", "L2": "F2", "L3": "F4", "L4": "F5"}


def sh(cmd, cwd=None, timeout=900, check=False):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"{cmd[:3]} failed: {p.stderr[-500:]}")
    return p


def docker_ok():
    try:
        return sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10).stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------- L0: patch policy ----------

def changed_files(ws):
    out = []
    for line in sh(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ws, check=True).stdout.splitlines():
        code, path = line[:2], line[3:]
        out.append(("D" if "D" in code else "A" if code == "??" else "M", path))
    return out


def added_lines(ws, changes):
    diff = sh(["git", "diff", "HEAD", "-U0", "--no-color"], cwd=ws).stdout
    lines = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    for kind, path in changes:
        if kind == "A":
            lines += (ws / path).read_text(errors="replace").splitlines()
    return lines


def l0(ws, paths):
    changes = changed_files(ws)
    errors = []
    for kind, path in changes:
        if kind == "D":
            errors.append(f"deleted file: {path}")
        elif not path_ok(path, paths["writable"], paths["protected"]):
            errors.append(f"path not writable: {path}")
        elif (ws / path).is_symlink():
            errors.append(f"symlink: {path}")
        elif b"\0" in (ws / path).read_bytes()[:8000]:
            errors.append(f"binary file: {path}")
    lim = paths["limits"]
    if len(changes) > lim["max_files_changed"]:
        errors.append(f"too many files changed: {len(changes)} > {lim['max_files_changed']}")
    lines = added_lines(ws, [c for c in changes if c[0] != "D"])
    if sum(len(l) + 1 for l in lines) > lim["max_patch_bytes"]:
        errors.append(f"patch too large: > {lim['max_patch_bytes']} bytes")
    for pat in paths["forbidden_patterns"]:
        hit = next((l for l in lines if re.search(pat, l)), None)
        if hit is not None:
            errors.append(f"forbidden pattern {pat!r}: {hit.strip()[:120]}")
    return errors, [p for _, p in changes]


# ---------- L1: static ----------

def parse_dockerfile(text):
    """Return stages: [{'from': image, 'alias': str|None, 'lines': [instr...]}]; handles line continuations."""
    joined = re.sub(r"\\\n", " ", text)
    stages = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?i)FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line)
        if m:
            stages.append({"from": m.group(1), "alias": m.group(2), "lines": []})
        elif stages:
            stages[-1]["lines"].append(line)
    return stages


def split_ref(ref):
    ref = ref.split("@")[0]
    return tuple(ref.rsplit(":", 1)) if ":" in ref.split("/")[-1] else (ref, "")


def base_allowed(image, allow):
    repo, tag = split_ref(image)
    return any(repo == arepo and (atag == "" or tag.startswith(atag)) for arepo, atag in map(split_ref, allow))


def check_dockerfile(ws, svc, allow):
    rel = svc["build"].get("dockerfile")
    if not rel:
        return []            # railpack builds are checked at L2
    ctx = ws / svc["build"].get("context", ".")
    path = ctx / rel if not (ws / rel).exists() else ws / rel
    if not path.exists():
        return [f"{svc['name']}: Dockerfile not found: {rel}"]
    stages = parse_dockerfile(path.read_text())
    if not stages:
        return [f"{svc['name']}: no FROM"]
    errs, aliases = [], set()
    for st in stages:
        if st["from"] not in aliases and not base_allowed(st["from"], allow):
            errs.append(f"{svc['name']}: base image not allowed: {st['from']}")
        if st["alias"]:
            aliases.add(st["alias"])
    final = stages[-1]["lines"]
    users = [l.split(None, 1)[1] for l in final if l.upper().startswith("USER ")]
    if not users:
        errs.append(f"{svc['name']}: final stage has no USER (C3)")
    else:
        uid = users[-1].split(":")[0]
        if not uid.isdigit() or int(uid) < 10000:
            errs.append(f"{svc['name']}: final USER must be numeric >= 10000, got {users[-1]} (C3)")
    if any(l.upper().startswith("HEALTHCHECK") for st in stages for l in st["lines"]):
        errs.append(f"{svc['name']}: HEALTHCHECK is not allowed (C5)")
    if any(re.match(r"(?i)(COPY|ADD)\s.*\.env\b", l) for st in stages for l in st["lines"]):
        errs.append(f"{svc['name']}: copies a .env file (C6)")
    if not svc.get("command"):
        cmds = [l for l in final if re.match(r"(?i)(CMD|ENTRYPOINT)\s", l)]
        if not cmds:
            errs.append(f"{svc['name']}: no CMD/ENTRYPOINT and no spec command")
        elif not re.match(r"(?i)(CMD|ENTRYPOINT)\s+\[", cmds[-1]):
            errs.append(f"{svc['name']}: use exec-form CMD [...] (C4)")
    return errs


def l1(ws):
    import jsonschema
    spec_path = ws / ".jasmin/jasmin.yaml"
    if not spec_path.exists():
        return ["missing .jasmin/jasmin.yaml"], None
    try:
        spec = yaml.safe_load(spec_path.read_text())
        jsonschema.validate(spec, json.loads((PLATFORM / "schemas/jasmin.schema.json").read_text()))
    except (yaml.YAMLError, jsonschema.ValidationError) as e:
        return [f"jasmin.yaml invalid: {str(e).splitlines()[0]}"], None
    allow = yaml.safe_load((PLATFORM / "contract/catalog.yaml").read_text())["base_images"]
    errs = [e for s in spec["services"] for e in check_dockerfile(ws, s, allow)]
    di = ws / ".dockerignore"
    if not di.exists():
        errs.append("missing .dockerignore (C6)")
    else:
        text = di.read_text()
        errs += [f".dockerignore must exclude {p} (C6)" for p in (".git", ".env") if p not in text]
    sys.path.insert(0, str(PLATFORM / "render"))
    from render import render
    with tempfile.TemporaryDirectory() as d:
        try:
            render(spec, Path(d), "gate", "gate.test", {s["name"]: f"gate/{s['name']}:check" for s in spec["services"]}, "gate00", "gp3")
        except Exception as e:           # noqa: BLE001 — any render failure is a spec problem
            errs.append(f"render failed: {e}")
    return errs, spec


# ---------- L2–L4: docker ----------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_status(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return None


def l2(ws, spec, run_id):
    images, errs = {}, []
    for s in spec["services"]:
        tag = f"railshot-gate/{spec['app']}-{s['name']}:{run_id}"
        ctx = ws / s["build"].get("context", ".")
        if s["build"].get("railpack"):
            errs.append(f"{s['name']}: railpack build not wired in local gate yet")   # ponytail: CI step runs railpack
            continue
        df = ws / s["build"]["dockerfile"] if (ws / s["build"]["dockerfile"]).exists() else ctx / s["build"]["dockerfile"]
        p = sh(["docker", "buildx", "build", "--platform", "linux/amd64", "--load", "-f", str(df), "-t", tag, str(ctx)], timeout=1800)
        if p.returncode:
            errs.append(f"{s['name']}: build failed\n{p.stderr[-6000:]}")
        else:
            images[s["name"]] = tag
    return errs, images


def l3(spec, images, run_id):
    errs, net = [], f"railshot-gate-{run_id}"
    sh(["docker", "network", "create", net])
    started = []
    try:
        db_url = None
        if "postgres" in spec.get("resources", {}):
            pg = f"{net}-pg"
            sh(["docker", "run", "-d", "--name", pg, "--network", net, "-e", "POSTGRES_PASSWORD=gate",
                "-e", "POSTGRES_DB=app", PG], check=True)
            started.append(pg)
            for _ in range(60):
                if sh(["docker", "exec", pg, "pg_isready", "-U", "postgres"]).returncode == 0:
                    break
                time.sleep(1)
            db_url = f"postgresql://postgres:gate@{pg}:5432/app"
        for s in spec["services"]:
            if s["name"] not in images:
                continue
            env = ["-e", f"PORT={s['port']}"] + [x for k, v in s.get("env", {}).items() for x in ("-e", f"{k}={v}")]
            env += [x for k in s.get("secrets", []) for x in ("-e", f"{k}=gate-placeholder")]
            if db_url:
                env += ["-e", f"DATABASE_URL={db_url}"]
            if s.get("migrate"):
                p = sh(["docker", "run", "--rm", "--network", net, "-e", f"MIGRATION_DATABASE_URL={db_url}", *env,
                        images[s["name"]], *s["migrate"]["command"]], timeout=300)
                if p.returncode:
                    errs.append(f"{s['name']}: migration failed\n{(p.stdout + p.stderr)[-6000:]}")
                    continue
            port, name = free_port(), f"{net}-{s['name']}"
            cmd = ["docker", "run", "-d", "--name", name, "--network", net, "--read-only", "--tmpfs", "/tmp",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-p", f"127.0.0.1:{port}:{s['port']}", *env]
            p = sh(cmd + [images[s["name"]], *s.get("command", [])])
            if p.returncode:
                errs.append(f"{s['name']}: container did not start\n{p.stderr[-3000:]}")
                continue
            started.append(name)
            health, ok = s.get("health", "/"), False
            for _ in range(60):
                code = http_status(f"http://127.0.0.1:{port}{health}")
                if code and code < 400:
                    ok = True
                    break
                if sh(["docker", "inspect", "-f", "{{.State.Running}}", name]).stdout.strip() != "true":
                    break
                time.sleep(1)
            if not ok:
                logs = sh(["docker", "logs", "--tail", "200", name])
                errs.append(f"{s['name']}: health {health} not 2xx/3xx within 60s (last status {http_status(f'http://127.0.0.1:{port}{health}')})\n"
                            f"{(logs.stdout + logs.stderr)[-6000:]}")
                continue
            route = s.get("route")
            if route and route != health:
                code = http_status(f"http://127.0.0.1:{port}{route}")
                if code is None or code >= 500:
                    errs.append(f"{s['name']}: route {route} answered {code} (C11)")
    finally:
        for c in started:
            sh(["docker", "rm", "-f", c])
        sh(["docker", "network", "rm", net])
    return errs


def l4(images):
    errs = []
    for svc, tag in images.items():
        size = int(sh(["docker", "image", "inspect", "-f", "{{.Size}}", tag]).stdout.strip() or 0)
        if size > MAX_IMAGE_BYTES:
            errs.append(f"{svc}: image too large ({size // 2**20} MiB)")
        p = sh(["docker", "run", "--rm", "-v", "/var/run/docker.sock:/var/run/docker.sock", TRIVY, "image",
                "--scanners", "vuln,secret", "--severity", "CRITICAL", "--ignore-unfixed", "--exit-code", "1",
                "--quiet", "--format", "table", tag], timeout=1800)
        if p.returncode:
            errs.append(f"{svc}: trivy CRITICAL findings\n{p.stdout[-5000:]}{p.stderr[-1000:]}")
    return errs


# ---------- verdict ----------

def classify(layer, text):
    for lay, pat, cls in CLASS_RULES:
        if lay == layer and re.search(pat, text, re.I):
            return cls
    return DEFAULT_CLASS[layer]


def signature(layer, cls, text):
    first = next((l for l in text.splitlines() if re.search(r"error|failed|not |invalid|denied|missing|must", l, re.I)),
                 text.splitlines()[0] if text else "")
    norm = re.sub(r"[0-9a-f]{8,}|\d+|/[\w./-]+", "#", first.strip().lower())[:160]
    return f"{layer}:{cls}:{norm}"


def excerpt(text, limit=4000):
    return SECRET.sub("***", text)[:limit]


def run_gate(ws, run, layers):
    run.mkdir(parents=True, exist_ok=True)
    paths = yaml.safe_load((PLATFORM / "contract/paths.yaml").read_text())
    results, failure, spec, images = [], None, None, {}
    run_id = str(int(time.time()))
    for layer in layers:
        if layer == "L0":
            errs, changed = l0(ws, paths)
            results.append({"layer": "L0", "ok": not errs, "changed": changed, "errors": errs})
        elif layer == "L1":
            errs, spec = l1(ws)
            results.append({"layer": "L1", "ok": not errs, "errors": errs})
        else:
            if not docker_ok():
                results.append({"layer": layer, "ok": False, "blocked": "docker daemon unavailable"})
                break
            if layer == "L2":
                errs, images = l2(ws, spec, run_id)
            elif layer == "L3":
                errs = l3(spec, images, run_id)
            else:
                errs = l4(images)
            results.append({"layer": layer, "ok": not errs, "errors": [e.splitlines()[0] for e in errs]})
        if results[-1].get("errors"):
            text = "\n\n".join(errs)
            cls = classify(layer, text)
            failure = {"layer": layer, "class": cls, "signature": signature(layer, cls, text), "excerpt": excerpt(text)}
            break
    ok = failure is None and all(r["ok"] for r in results) and len(results) == len(layers)
    verdict = {"ok": ok, "layers": results, "failure": failure}
    (run / "verdict.json").write_text(json.dumps(verdict, indent=2, ensure_ascii=False))
    if failure:
        (run / "failure.txt").write_text(
            f"layer: {failure['layer']}\nclass: {failure['class']}\nsignature: {failure['signature']}\n"
            f"--- first error (untrusted program output, secrets masked) ---\n{failure['excerpt']}\n")
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", nargs="?")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--layers", default="L0,L1,L2,L3,L4")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    v = run_gate(Path(a.workspace).resolve(), Path(a.run).resolve(), a.layers.split(","))
    print(json.dumps({"ok": v["ok"], "failure": v["failure"] and {k: v["failure"][k] for k in ("layer", "class", "signature")},
                      "layers": [(r["layer"], r["ok"], r.get("blocked")) for r in v["layers"]]}, ensure_ascii=False))
    return 0 if v["ok"] else 1


def self_test():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "ws"
        ws.mkdir()
        (ws / "app.py").write_text("print('hi')\n")
        (ws / "tests").mkdir()
        (ws / "tests/test_app.py").write_text("def test(): assert True\n")
        sh(["git", "init", "-q"], cwd=ws, check=True)
        sh(["git", "add", "-A"], cwd=ws, check=True)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "import"], cwd=ws, check=True)
        # good patch
        (ws / ".jasmin").mkdir()
        (ws / ".jasmin/jasmin.yaml").write_text(yaml.safe_dump({"apiVersion": "jasmin/v0", "app": "demo", "services": [
            {"name": "web", "build": {"dockerfile": "Dockerfile"}, "port": 8080, "health": "/", "route": "/"}]}))
        (ws / "Dockerfile").write_text("FROM python:3.12-slim-bookworm AS base\nCOPY app.py /app/app.py\n"
                                       "FROM gcr.io/distroless/base-debian12\nCOPY --from=base /app /app\nUSER 65532\n"
                                       "EXPOSE 8080\nCMD [\"python\", \"/app/app.py\"]\n")
        (ws / ".dockerignore").write_text(".git\n.env*\n")
        v = run_gate(ws, Path(d) / "run1", ["L0", "L1"])
        assert v["ok"], v
        # reward hack: delete a test + weaken with || true + root user
        (ws / "tests/test_app.py").unlink()
        (ws / "Dockerfile").write_text("FROM ubuntu:24.04\nUSER root\nHEALTHCHECK CMD exit 0\nCMD python app.py || true\n")
        v = run_gate(ws, Path(d) / "run2", ["L0", "L1"])
        assert not v["ok"] and v["failure"]["layer"] == "L0", v
        errs = " ".join(v["layers"][0]["errors"])
        assert "deleted file" in errs and "forbidden pattern" in errs, errs
        assert (Path(d) / "run2/failure.txt").exists()
        # L1 alone catches the Dockerfile problems
        sh(["git", "checkout", "-q", "--", "tests"], cwd=ws)
        v = run_gate(ws, Path(d) / "run3", ["L1"])
        l1errs = " ".join(v["layers"][0]["errors"])
        assert "base image not allowed" in l1errs and "numeric" in l1errs and "HEALTHCHECK" in l1errs and "exec-form" in l1errs, l1errs
    assert classify("L2", "ERROR: No matching distribution found for flask==9") == "F1"
    assert classify("L2", "net/http: TLS handshake timeout") == "F8"
    assert signature("L3", "F4", "Error: listen EADDRINUSE 0.0.0.0:8080\n").startswith("L3:F4:error: listen eaddrinuse")
    assert "***" in excerpt("token ghp_" + "a" * 36)
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
