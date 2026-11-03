"""Run headless `claude -p` for structured-output jobs (summaries, clustering, proposals).

Hardening for untrusted input:
  * no tools at all (`--tools ""`), so a prompt injection in logged content can't act
  * runs in an empty temp dir and doesn't persist the session
  * LEARNING_HARNESS_DISABLE=1 so the harness's hooks don't log its own calls
  * output must validate against a JSON schema; we re-check it locally anyway
  * prompt goes over stdin (argv has a 128KB per-argument limit)

LEARNING_HARNESS_CLAUDE_CMD overrides the executable (tests use a fake).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile

DEFAULT_MODEL = os.environ.get("LEARNING_HARNESS_MODEL", "claude-haiku-5-5")


class ClaudeError(RuntimeError):
    pass


def run_structured(prompt: str, schema: dict, *, system: str, model: str | None = None,
                   max_budget_usd: float = 0.50, timeout: int = 300) -> tuple[dict, float]:
    """Return (structured_output, cost_usd)."""
    exe = shlex.split(os.environ.get("LEARNING_HARNESS_CLAUDE_CMD", "claude"))
    cmd = exe + [
        "-p", "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--json-schema", json.dumps(schema),
        "--system-prompt", system,
        "--model", model or DEFAULT_MODEL,
        "--max-budget-usd", str(max_budget_usd),
    ]
    env = {**os.environ, "LEARNING_HARNESS_DISABLE": "1"}
    with tempfile.TemporaryDirectory(prefix="lh-claude-") as cwd:
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env,
                               cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"claude timed out after {timeout}s") from e
    if p.returncode != 0:
        raise ClaudeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[:500]}")
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeError(f"non-JSON output: {p.stdout[:300]!r}") from e
    if d.get("is_error"):
        raise ClaudeError(f"claude reported error: {str(d.get('result'))[:300]}")
    out = d.get("structured_output")
    if not isinstance(out, dict):
        raise ClaudeError("no structured_output in response")
    return out, float(d.get("total_cost_usd") or 0.0)
