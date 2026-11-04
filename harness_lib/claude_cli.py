"""Run headless `claude -p` for structured-output jobs (summaries, clustering, proposals).

Hardening for untrusted input:
  * no tools at all (`--tools ""`), so a prompt injection in logged content can't act
  * runs in an empty temp dir and doesn't persist the session
  * LEARNING_HARNESS_DISABLE=1 so the harness's hooks don't log its own calls
  * output must validate against a JSON schema; we re-check it locally anyway
  * prompt goes over stdin (argv has a 128KB per-argument limit)

LEARNING_HARNESS_CLAUDE_CMD overrides the executable (tests use a fake): a JSON list of argv
strings (safe with spaces in paths), or a shell-style string.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile

DEFAULT_MODEL = os.environ.get("LEARNING_HARNESS_MODEL", "claude-haiku-5-5")


class ClaudeError(RuntimeError):
    pass


def month_spend(month: str | None = None) -> float:
    """Total recorded spend (USD) for a month, 'YYYY-MM' (default: this month)."""
    from harness_lib import store
    month = month or store.now()[:7]
    return round(sum(r.get("usd", 0.0) for r in store.read_jsonl(store.store_dir() / "spend.jsonl")
                     if str(r.get("at", "")).startswith(month)), 6)


def budget_remaining() -> float | None:
    """Remaining monthly budget when LEARNING_HARNESS_ENFORCE_BUDGET=1 (background runs), else None."""
    if os.environ.get("LEARNING_HARNESS_ENFORCE_BUDGET") != "1":
        return None
    from harness_lib import store
    cfg = (store.read_json(store.store_dir() / "config.json") or {}).get("auto", {})
    return float(cfg.get("monthly_budget_usd", 5.0)) - month_spend()


def _record_spend(cost: float, model: str) -> None:
    try:
        from harness_lib import store
        store.append_jsonl(store.store_dir() / "spend.jsonl",
                           {"at": store.now(), "usd": round(cost, 6), "model": model,
                            "purpose": os.environ.get("LEARNING_HARNESS_PURPOSE", "manual")})
    except Exception:
        pass  # accounting must never break a run


def resolve_claude() -> list[str]:
    """The command that starts Claude Code, avoiding cmd.exe on Windows.

    An npm install on Windows puts a `claude.cmd` shim on PATH. Running a .cmd goes through cmd.exe, which
    mangles arguments containing quotes, line breaks or & | < > ^ %. So when the shim points at a node script,
    run `node <script>` directly instead."""
    found = shutil.which("claude") or "claude"
    if os.name != "nt" or not found.lower().endswith((".cmd", ".bat")):
        return [found]
    return _node_command_from_shim(found) or [found]


def _node_command_from_shim(shim: str) -> list[str] | None:
    import re
    try:
        with open(shim, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r'"(%~?dp0%?[^"]*?\.(?:c?js|mjs))"', text, re.I)
    if not m:
        return None
    base = os.path.dirname(os.path.abspath(shim))
    script = os.path.normpath(re.sub(r"(?i)%~?dp0%?", lambda _: base + os.sep, m.group(1)))
    if not os.path.isfile(script):
        return None
    local_node = os.path.join(base, "node.exe")
    node = local_node if os.path.isfile(local_node) else shutil.which("node")
    return [node, script] if node else None


def run_structured(prompt: str, schema: dict, *, system: str, model: str | None = None,
                   max_budget_usd: float = 0.50, timeout: int = 300) -> tuple[dict, float]:
    """Return (structured_output, cost_usd). Every call's cost is appended to store/spend.jsonl."""
    remaining = budget_remaining()
    if remaining is not None:
        if remaining <= 0.001:
            raise ClaudeError("monthly learning budget reached; background learning paused until next month")
        max_budget_usd = min(max_budget_usd, remaining)  # one call can never push past the cap
    override = os.environ.get("LEARNING_HARNESS_CLAUDE_CMD")  # a JSON list, or a shell-style string
    if override and override.lstrip().startswith("["):
        exe = json.loads(override)
    elif override:
        exe = shlex.split(override, posix=os.name != "nt")
    else:
        exe = resolve_claude()
    env = {**os.environ, "LEARNING_HARNESS_DISABLE": "1"}
    with tempfile.TemporaryDirectory(prefix="lh-claude-") as cwd:
        # Long or multi-line text goes in files, never on the command line (Windows cmd.exe cuts arguments at
        # the first line break). The harness's own calls run with every hook and MCP server off, so the user's
        # hooks and plugins never fire inside them.
        sys_file = os.path.join(cwd, "system.txt")
        settings_file = os.path.join(cwd, "settings.json")
        with open(sys_file, "w", encoding="utf-8") as fh:
            fh.write(system)
        with open(settings_file, "w", encoding="utf-8") as fh:
            json.dump({"disableAllHooks": True}, fh)
        cmd = exe + [
            "-p", "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--settings", settings_file,
            "--strict-mcp-config",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--system-prompt-file", sys_file,
            "--model", model or DEFAULT_MODEL,
            "--max-budget-usd", str(max_budget_usd),
        ]
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                               cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"claude timed out after {timeout}s") from e
    if p.returncode != 0:
        try:  # an aborted call (e.g. it hit --max-budget-usd) can still report what it cost
            _record_spend(float(json.loads(p.stdout).get("total_cost_usd") or 0.0), model or DEFAULT_MODEL)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            pass
        raise ClaudeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[:500]}")
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeError(f"non-JSON output: {p.stdout[:300]!r}") from e
    cost = float(d.get("total_cost_usd") or 0.0)
    _record_spend(cost, model or DEFAULT_MODEL)  # failed calls cost money too
    if d.get("is_error"):
        raise ClaudeError(f"claude reported error: {str(d.get('result'))[:300]}")
    out = d.get("structured_output")
    if not isinstance(out, dict):
        raise ClaudeError("no structured_output in response")
    return out, cost
