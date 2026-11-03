"""Runtime for learned MCP tools, shared by mcp_tools/server.py and validate/replay.py.

A tool's definition lives in mcp_tools/tools/<name>/tool.json and comes in one of two kinds:

  runner "stdin-json"  generated tool: python3 <entrypoint>, arguments passed as one JSON object on stdin
  "wraps"              an installed script exposed as a tool: arguments are rendered into its argv
                       with a template, e.g.
                         ["{input}", "{output}", {"when": "delimiter", "argv": ["--delimiter", "{delimiter}"]}]

Every call runs sandboxed (harness_lib/sandbox.py): no network (unshare -rn on Linux, sandbox-exec on
macOS), scrubbed environment, CPU/memory/file-size limits where the OS supports them, and a timeout.
Its cwd is the project directory, because tools act on the user's files.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import sandbox
from .schema_check import validate

SERVER_NAME = "learned"           # Claude Code shows tools as mcp__learned__<tool>
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RUN_TIMEOUT_S = 60
MAX_OUTPUT = 20_000


class ToolError(ValueError):
    pass


# ---------------- definitions ----------------

def load_tools(root: Path, registry: dict) -> dict[str, dict]:
    """name -> {name, description, input_schema, command, mode, argv_template, entry}. Broken entries are skipped."""
    tools = {}
    installed = registry.get("installed", {})
    for name, e in installed.items():
        if e.get("type") != "tool":
            continue
        try:
            spec = json.loads((root / e["path"] / "tool.json").read_text(encoding="utf-8"))
            if "wraps" in spec:
                s = installed.get(spec["wraps"]["script"])
                if not s or s["type"] != "script":
                    continue  # wrapped script was uninstalled
                command = root / s["path"] / s["entrypoint"]
                mode, template = "argv", spec["wraps"]["argv"]
            else:
                command = root / e["path"] / e["entrypoint"]
                mode, template = "stdin-json", None
            tools[name] = {"name": name, "description": spec["description"], "input_schema": spec["input_schema"],
                           "command": command, "mode": mode, "argv_template": template, "entry": e}
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    return tools


def check_template(template, schema: dict) -> list[str]:
    probs = []
    props = set(schema.get("properties", {}))
    required = set(schema.get("required", []))
    if not isinstance(template, list) or not template:
        return ["argv template must be a non-empty list"]
    for item in template:
        if isinstance(item, str):
            for ph in PLACEHOLDER.findall(item):
                if ph not in props:
                    probs.append(f"placeholder {{{ph}}} is not a schema property")
                elif ph not in required:
                    probs.append(f"{{{ph}}} is optional, so it must be inside a {{'when': ...}} group")
        elif isinstance(item, dict) and set(item) == {"when", "argv"} and isinstance(item["argv"], list):
            if item["when"] not in props:
                probs.append(f"'when' {item['when']!r} is not a schema property")
            for x in item["argv"]:
                if not isinstance(x, str):
                    probs.append("argv entries inside a 'when' group must be strings")
                    continue
                for ph in PLACEHOLDER.findall(x):
                    if ph not in props:
                        probs.append(f"placeholder {{{ph}}} is not a schema property")
        else:
            probs.append(f"bad template item {item!r}")
    return probs


def check_arguments(args, schema: dict, *, mode: str) -> list[str]:
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]
    probs = validate(args, schema)
    if schema.get("additionalProperties") is False:
        extra = set(args) - set(schema.get("properties", {}))
        if extra:
            probs.append(f"unknown argument(s): {sorted(extra)}")
    if mode == "argv":
        for k, v in args.items():
            if isinstance(v, (dict, list)):
                probs.append(f"{k}: nested values can't be passed on a command line")
            elif isinstance(v, str) and v.startswith("-"):
                probs.append(f"{k}: values may not start with '-' (they would be read as options)")
    return probs


def render_argv(template: list, args: dict) -> list[str]:
    def fill(s: str) -> str:
        def sub(m):
            k = m.group(1)
            if k not in args or args[k] is None:
                raise ToolError(f"missing argument {k!r}")
            v = args[k]
            return ("true" if v else "false") if isinstance(v, bool) else str(v)
        return PLACEHOLDER.sub(sub, s)

    out: list[str] = []
    for item in template:
        if isinstance(item, str):
            out.append(fill(item))
        elif args.get(item["when"]) not in (None, False, ""):
            out += [fill(x) for x in item["argv"]]
    return out


# ---------------- execution ----------------

def netns_available() -> bool:
    return sandbox.network_isolated()


def run_tool(tool: dict, args: dict, *, cwd: str, require_netns: bool = True) -> dict:
    """Validate, then run one call. Returns {exit, stdout, stderr, duration_ms, rejected?}."""
    probs = check_arguments(args, tool["input_schema"], mode=tool["mode"])
    if probs:
        return {"exit": None, "rejected": "; ".join(probs), "stdout": "", "stderr": "", "duration_ms": 0}
    ok, why = sandbox.may_run(not require_netns)
    if not ok:
        return {"exit": None, "rejected": why, "stdout": "", "stderr": "", "duration_ms": 0}
    if tool["mode"] == "argv":
        try:
            argv, stdin = render_argv(tool["argv_template"], args), ""
        except ToolError as e:
            return {"exit": None, "rejected": str(e), "stdout": "", "stderr": "", "duration_ms": 0}
    else:
        argv, stdin = [], json.dumps(args)
    cmd = [sys.executable, "-I", str(tool["command"]), *argv]
    home = tempfile.mkdtemp(prefix="lh-tool-home-")
    t0 = time.time()
    try:
        p = sandbox.run(cmd, cwd=cwd, env=sandbox.clean_env(home), stdin=stdin, timeout=RUN_TIMEOUT_S,
                        cpu_s=30, mem_mb=1024, fsize_mb=100)
        res = {"exit": p.returncode, "stdout": p.stdout[:MAX_OUTPUT], "stderr": p.stderr[:MAX_OUTPUT]}
    except subprocess.TimeoutExpired:
        res = {"exit": -9, "stdout": "", "stderr": f"timed out after {RUN_TIMEOUT_S}s"}
    finally:
        shutil.rmtree(home, ignore_errors=True)
    res["duration_ms"] = int((time.time() - t0) * 1000)
    return res
