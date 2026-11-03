#!/usr/bin/env python3
"""Add the learning-harness Stop + SessionEnd hooks to a Claude Code settings file.

  install_hooks.py --scope user                 # ~/.claude/settings.json (all projects)
  install_hooks.py --scope project --dir PATH   # PATH/.claude/settings.json
  install_hooks.py ... --uninstall

Merges into existing settings; never removes other hooks. Idempotent.
"""
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve().parent / "log_session.py"
MARKER = "log_session.py"


def handler() -> dict:
    return {"type": "command", "command": f"python3 {shlex.quote(str(HOOK_SCRIPT))}", "timeout": 30}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", choices=("user", "project"), required=True)
    ap.add_argument("--dir", default=".")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    path = (Path.home() / ".claude" / "settings.json") if a.scope == "user" \
        else Path(a.dir).resolve() / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    hooks = settings.setdefault("hooks", {})
    for event in ("Stop", "SessionEnd"):
        groups = hooks.get(event, [])
        # drop any previous copy of our handler
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if MARKER not in h.get("command", "")]
        groups = [g for g in groups if g.get("hooks")]
        if not a.uninstall:
            groups.append({"hooks": [handler()]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"{'removed from' if a.uninstall else 'installed in'} {path}")


if __name__ == "__main__":
    main()
