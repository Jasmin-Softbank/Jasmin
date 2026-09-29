#!/usr/bin/env python3
"""Run one Jasmin agent role through an agent API (Claude Agent SDK or Codex exec).

usage:
  run_agent.py ROLE --provider claude|codex --workspace DIR --run DIR --task FILE
  run_agent.py --self-test

Agents only get read access. Files come back in the JSON output ("files": [{path, content}]);
this script validates every path and writes it. Output and metadata go to RUN/<role>.json.
Needs: pip packages pyyaml, jsonschema, claude-agent-sdk (claude provider); `codex` on PATH (codex provider).
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

PLATFORM = Path(__file__).resolve().parents[1]


def load_yaml(path):
    import yaml
    return yaml.safe_load(Path(path).read_text())


def writable_rules(spec):
    if isinstance(spec, list):
        return spec, []
    paths = load_yaml(PLATFORM / spec)
    return paths["writable"], paths["protected"]


def path_ok(rel, allow, deny):
    """True if rel is a clean relative path matching an allow glob and no deny glob."""
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not rel or rel.startswith("~"):
        return False
    hit = lambda globs: any(p.full_match(g) or p.full_match(g.removeprefix("**/")) for g in globs)
    return hit(allow) and not hit(deny)


def instructions(profile, role_cfg):
    text = (PLATFORM / profile["instructions_prefix"]).read_text() + "\n\n" + (PLATFORM / role_cfg["instructions"]).read_text()
    text += ("\n\n## How to return files\nYou cannot edit files. Return the full content of every file you create or "
             "change in the `files` array of your JSON output, with paths relative to the workspace root. "
             "Files you do not list stay unchanged.\n")
    return text


def with_files(schema, allow):
    """Add the files array to the role schema (draft-07)."""
    s = copy.deepcopy(schema)
    s["properties"]["files"] = {
        "type": "array", "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False, "required": ["path", "content"],
                  "properties": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 20000}}}}
    if not allow:
        s["properties"]["files"]["maxItems"] = 0
    return s


def strict_variant(schema):
    """Codex structured output wants every property required; optional ones become nullable."""
    s = copy.deepcopy(schema)
    s.pop("$schema", None)
    s.pop("allOf", None)

    def walk(node):
        if isinstance(node, dict):
            node.pop("contains", None)
            if node.get("type") == "object" and "properties" in node:
                req = set(node.get("required", []))
                for k, v in node["properties"].items():
                    walk(v)
                    if k not in req:
                        node["properties"][k] = {"anyOf": [v, {"type": "null"}]}
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


def drop_nulls(obj):
    if isinstance(obj, dict):
        return {k: drop_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_nulls(v) for v in obj]
    return obj


async def run_claude(cfg, system, task, schema, workspace, read_roots, read_deny):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

    async def guard(inp, tool_use_id, ctx):
        args = inp.get("tool_input", {})
        pattern = args.get("pattern", "")
        target = args.get("file_path") or args.get("path") or (pattern if pattern.startswith(("/", "~", "..")) else str(workspace))
        full = Path(os.path.expanduser(target) if Path(os.path.expanduser(target)).is_absolute() else workspace / target).resolve()
        inside = any(full == r or r in full.parents for r in read_roots)
        rel = full.relative_to(workspace).as_posix() if workspace in full.parents else full.name
        if not inside or (rel and not path_ok(rel, ["**"], read_deny)):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": f"read denied: {target}"}}
        return {}

    async def prompt():
        yield {"type": "user", "message": {"role": "user", "content": task}}

    opts = ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code", "append": system},
        cwd=str(workspace), add_dirs=[str(r) for r in read_roots if r != workspace],
        tools=["Read", "Glob", "Grep"], allowed_tools=["Read", "Glob", "Grep"],
        setting_sources=[],              # never load CLAUDE.md, settings or hooks from disk
        strict_mcp_config=True, mcp_servers={},
        hooks={"PreToolUse": [HookMatcher(matcher="Read|Glob|Grep", hooks=[guard])]},
        max_turns=cfg["max_turns"], max_budget_usd=cfg["max_budget_usd"], model=cfg["model"],
        output_format={"type": "json_schema", "schema": schema},
    )
    result = None
    async for msg in query(prompt=prompt(), options=opts):
        if isinstance(msg, ResultMessage):
            result = msg
    if result is None or result.is_error or result.structured_output is None:
        raise RuntimeError(f"claude run failed: {getattr(result, 'subtype', None)} {getattr(result, 'errors', None)}")
    meta = {"turns": result.num_turns, "cost_usd": result.total_cost_usd, "duration_ms": result.duration_ms,
            "denials": len(result.permission_denials or [])}
    return result.structured_output, meta


def run_codex(cfg, system, task, schema, workspace, run):
    schema_file = run / "codex.schema.json"
    schema_file.write_text(json.dumps(strict_variant(schema)))
    last = run / "codex.last.json"
    cmd = ["codex", "exec", "--cd", str(workspace), "--sandbox", cfg["sandbox"], "--skip-git-repo-check",
           "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "-c", 'approval_policy="never"', "-c", f"developer_instructions={json.dumps(system)}",
           "--output-schema", str(schema_file), "-o", str(last), "--color", "never"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    t = time.time()
    proc = subprocess.run(cmd + ["-"], input=task, text=True, capture_output=True, timeout=1800)
    (run / "codex.log").write_text(proc.stdout[-20000:] + "\n--stderr--\n" + proc.stderr[-20000:])
    if proc.returncode != 0 or not last.exists():
        raise RuntimeError(f"codex exec failed ({proc.returncode}); see {run / 'codex.log'}")
    return drop_nulls(json.loads(last.read_text())), {"duration_ms": int((time.time() - t) * 1000)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role", nargs="?")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--workspace")
    ap.add_argument("--run")
    ap.add_argument("--task")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    profile = load_yaml(PLATFORM / "runner/profiles.yaml")
    role_cfg = profile["roles"][a.role]
    provider = a.provider or "claude"
    workspace, run = Path(a.workspace).resolve(), Path(a.run).resolve()
    run.mkdir(parents=True, exist_ok=True)
    allow, protect = writable_rules(role_cfg["writable"])
    schema = with_files(json.loads((PLATFORM / role_cfg["schema"]).read_text()), allow)
    system = instructions(profile, role_cfg)
    task = Path(a.task).read_text()
    read_roots = [workspace, PLATFORM / "contract", PLATFORM / "schemas", run]

    if provider == "claude":
        # ponytail: drop the parent Claude Code session's variables so the child starts clean; keep auth
        for k in [k for k in os.environ if k.startswith("CLAUDE_CODE_") and k != "CLAUDE_CODE_OAUTH_TOKEN"] + ["CLAUDECODE"]:
            os.environ.pop(k, None)
        out, meta = asyncio.run(run_claude(profile["providers"]["claude"], system, task, schema, workspace,
                                           read_roots, profile["read_deny"]))
    else:
        out, meta = run_codex(profile["providers"]["codex"], system, task, schema, workspace, run)

    import jsonschema
    jsonschema.validate(out, schema)                     # the output contract, whatever the provider
    written, rejected = [], []
    for f in out.get("files", []):
        if path_ok(f["path"], allow, protect):
            dest = workspace / f["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f["content"])
            written.append(f["path"])
        else:
            rejected.append(f["path"])
    record = {"role": a.role, "provider": provider, "model": profile["providers"][provider].get("model"),
              "instructions_sha256": hashlib.sha256(system.encode()).hexdigest()[:12],
              "written": written, "rejected": rejected, "meta": meta,
              "output": {k: v for k, v in out.items() if k != "files"}}
    (run / f"{a.role}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
    print(json.dumps({k: record[k] for k in ("role", "provider", "written", "rejected", "meta")}, ensure_ascii=False))
    return 1 if rejected else 0


def self_test():
    allow, protect = writable_rules("contract/paths.yaml")
    ok = ["Dockerfile", "api.Dockerfile", "backend/Dockerfile", ".dockerignore", ".jasmin/jasmin.yaml"]
    bad = ["../x", "/etc/passwd", "app.py", ".github/workflows/x.yml", "tests/Dockerfile", "AGENTS.md", "~/x", ""]
    assert all(path_ok(p, allow, protect) for p in ok), [p for p in ok if not path_ok(p, allow, protect)]
    assert not any(path_ok(p, allow, protect) for p in bad), [p for p in bad if path_ok(p, allow, protect)]
    s = strict_variant(with_files(json.loads((PLATFORM / "schemas/report.schema.json").read_text()), allow))
    assert set(s["required"]) == set(s["properties"]) and "allOf" not in s
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
