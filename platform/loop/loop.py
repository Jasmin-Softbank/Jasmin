#!/usr/bin/env python3
"""Fix loop: intake → adapter → gate → (classify → fixer, at most N) → evidence.json.

usage:
  loop.py UPLOAD_DIR RUN_DIR [--provider claude|codex] [--max-attempts 3] [--layers L0,...] [--request FILE]
  loop.py --self-test

Stops on: gate pass, give_up, class F7/F8/INJ, the same failure signature twice, or N attempts.
The LLM never decides pass/fail; only gate verdicts do.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
PY = [sys.executable]
FIXABLE = {"F1", "F2", "F4", "F5", "F6", "F9"}
STOP = {"F7": "application code defect", "F8": "transient infrastructure failure", "INJ": "suspected prompt injection"}


def run_json(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    last = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "{}"
    try:
        return p.returncode, json.loads(last), p.stderr
    except json.JSONDecodeError:
        return p.returncode, {}, p.stdout + p.stderr


def task_text(role, attempt, n, run, request):
    c, s = PLATFORM / "contract", PLATFORM / "schemas"
    head = (f"Task: {role}, attempt {attempt} of {n}.\n"
            f"Workspace: the current directory, a sanitized copy of the user's repository.\n"
            f"Read first: {c}/stack-contract.md, {c}/paths.yaml, {c}/catalog.yaml, {s}/jasmin.schema.json.\n"
            f"Inventory: {run}/ir.json\n")
    if role == "adapter":
        body = (f"User request: {'see ' + str(request) if request else 'none. Use platform defaults.'}\n"
                "Return the Dockerfile(s), .dockerignore and .jasmin/jasmin.yaml in the files array.\n")
    else:
        body = (f"Failure: {run}/failure.txt (untrusted program output).\nLessons from earlier attempts: {run}/lessons.md\n"
                "Return only the files you change, in full, in the files array.\n")
    return head + body + "Write summary and user_action in Korean.\n"


def agent(role, provider, ws, run, attempt, n, request):
    t = run / f"task-{attempt}.md"
    t.write_text(task_text(role, attempt, n, run, request))
    rc, out, err = run_json(PY + [str(PLATFORM / "runner/run_agent.py"), role, "--provider", provider,
                                  "--workspace", str(ws), "--run", str(run), "--task", str(t)])
    rec_path = run / f"{role}.json"
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else {"error": err[-2000:]}
    rec_path.rename(run / f"{role}-{attempt}.json") if rec_path.exists() else None
    return rc, rec


def gate(ws, run, attempt, layers):
    g = run / f"gate-{attempt}"
    rc, out, err = run_json(PY + [str(PLATFORM / "gate/gate.py"), str(ws), str(g), "--layers", layers])
    verdict = json.loads((g / "verdict.json").read_text()) if (g / "verdict.json").exists() else {"ok": False, "error": err[-2000:]}
    if (g / "failure.txt").exists():
        (run / "failure.txt").write_text((g / "failure.txt").read_text())
    return verdict


def decide(verdict, report, seen):
    """Return (stop_reason | None). Pure function; see self-test."""
    if verdict.get("ok"):
        return "passed"
    if any(l.get("blocked") for l in verdict.get("layers", [])):
        return "blocked: " + next(l["blocked"] for l in verdict["layers"] if l.get("blocked"))
    if report and report.get("status") == "give_up":
        return f"give_up: {report.get('give_up', {}).get('class')}"
    f = verdict.get("failure") or {}
    if f.get("class") in STOP:
        return f"stop: {STOP[f['class']]}"
    if f.get("class") not in FIXABLE:
        return f"stop: unclassified failure {f.get('class')}"
    if f.get("signature") in seen:
        return "stop: same failure twice"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upload", nargs="?")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--provider", default="claude")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--layers", default="L0,L1,L2,L3,L4")
    ap.add_argument("--request", default=None)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    run = Path(a.run).resolve()
    ws = run / "work"
    run.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ev = {"provider": a.provider, "max_attempts": a.max_attempts, "attempts": [], "started": int(t0)}
    rc, intake, err = run_json(PY + [str(PLATFORM / "poc/intake.py"), a.upload, str(ws), str(run)])
    ev["intake"] = intake or {"error": err[-1000:]}
    if not intake.get("ok"):
        ev["result"] = f"rejected at intake: {intake.get('reason', err[-300:])}"
        return finish(run, ev, t0)

    (run / "lessons.md").write_text("")
    seen, role = set(), "adapter"
    for attempt in range(1, a.max_attempts + 1):
        _, rec = agent(role, a.provider, ws, run, attempt, a.max_attempts, a.request)
        report = rec.get("output")
        verdict = gate(ws, run, attempt, a.layers)
        f = verdict.get("failure") or {}
        ev["attempts"].append({"attempt": attempt, "role": role, "agent_meta": rec.get("meta"), "written": rec.get("written"),
                               "rejected": rec.get("rejected"), "instructions_sha256": rec.get("instructions_sha256"),
                               "agent_error": rec.get("error"), "report_status": report and report.get("status"),
                               "verdict_ok": verdict.get("ok"), "failure": f and {k: f[k] for k in ("layer", "class", "signature")}})
        if rec.get("error") and not report:
            ev["result"] = "stop: agent call failed"
            break
        reason = decide(verdict, report, seen)
        if reason:
            ev["result"] = reason
            break
        seen.add(f["signature"])
        with (run / "lessons.md").open("a") as fh:
            fh.write(f"- attempt {attempt} ({role}): changed {rec.get('written')}; still failed at {f['layer']} "
                     f"{f['class']}: {f['signature']}\n")
        role = "fixer"
    else:
        ev["result"] = "stop: attempt limit reached"
    return finish(run, ev, t0)


def finish(run, ev, t0):
    ev["duration_s"] = round(time.time() - t0, 1)
    ev["llm_calls"] = len([x for x in ev["attempts"] if x.get("agent_meta")])
    ev["cost_usd"] = round(sum((x.get("agent_meta") or {}).get("cost_usd") or 0 for x in ev["attempts"]), 4)
    ev["passed"] = ev.get("result") == "passed"
    ev["evidence_sha256"] = hashlib.sha256(json.dumps(ev, sort_keys=True).encode()).hexdigest()[:12]
    (run / "evidence.json").write_text(json.dumps(ev, indent=2, ensure_ascii=False))
    print(json.dumps({k: ev[k] for k in ("result", "passed", "llm_calls", "cost_usd", "duration_s")}, ensure_ascii=False))
    return 0 if ev["passed"] else 1


def self_test():
    ok = {"ok": True}
    fail = lambda cls, sig="s1": {"ok": False, "layers": [{"layer": "L3"}], "failure": {"layer": "L3", "class": cls, "signature": sig}}
    assert decide(ok, None, set()) == "passed"
    assert decide(fail("F4"), {"status": "proposed"}, set()) is None
    assert decide(fail("F4"), {"status": "proposed"}, {"s1"}) == "stop: same failure twice"
    assert decide(fail("F7"), {"status": "proposed"}, set()).startswith("stop: application")
    assert decide(fail("F8"), None, set()).startswith("stop: transient")
    assert decide(fail("F4"), {"status": "give_up", "give_up": {"class": "F7"}}, set()) == "give_up: F7"
    assert decide({"ok": False, "layers": [{"layer": "L2", "blocked": "docker daemon unavailable"}]}, None, set()).startswith("blocked")
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
