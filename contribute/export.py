#!/usr/bin/env python3
"""Export installed skills/agents/scripts/tools as a standalone Claude Code plugin.

Nothing here touches `learned/` (its own local git repo, never pushed) or this code repo. It only
reads what `review/cli.py approve` already installed -- skills in learned/skills/, agents in
learned/agents/, scripts in learned/scripts/, tools in learned/mcp_tools/tools/ -- and copies a
clean, shareable copy into a separate plugin folder you choose. Nothing in learned/generated/
(unapproved drafts) can be exported: only items a human already approved.

  python3 contribute/export.py list                                  # what can be exported
  python3 contribute/export.py export <names...> --dest DIR          # build/update the plugin at DIR
  python3 contribute/export.py export csv-to-json devops --dest ~/my-claude-plugin --name my-tools

Skills are copied without their evals/ (skill-creator's test fixtures, which hold redacted
snippets of your real sessions) -- that folder never leaves this machine, for any destination.
Agents pull in the skills they list under `skills:` automatically. Scripts become a bundled file
plus a slash command that runs it. A tool that just wraps a script (`"wraps"` in its tool.json)
exports as that script instead; a real generated tool becomes its own small standalone MCP server
bundled with the plugin (no sandboxing there -- review its code before trusting it elsewhere).

DIR becomes its own git repo (if it isn't one already) and each export is one commit, so you can
see and revert what changed. Nothing is ever pushed: that's for you to do once you're happy with it:

  cd DIR && git remote add origin <url> && git push -u origin main
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402
from registry import install as reg  # noqa: E402

NAME_RX = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_EXCLUDE = {"evals", "__pycache__", ".git", ".learning-harness-copy"}


class ExportError(RuntimeError):
    pass


# ---------------- inventory ----------------

def exportable() -> dict[str, dict]:
    """name -> registry entry, for every installed skill/agent/script/tool (never unapproved drafts)."""
    installed = reg.load_registry()["installed"]
    return {n: e for n, e in installed.items() if e["type"] in ("skill", "agent", "script", "tool")}


def cmd_list(args) -> None:
    items = exportable()
    if not items:
        print("nothing installed yet -- approve a draft first (review/cli.py approve)")
        return
    for kind in ("skill", "agent", "script", "tool"):
        names = sorted(n for n, e in items.items() if e["type"] == kind)
        if not names:
            continue
        print(f"{kind}s:")
        for n in names:
            print(f"  {n:24} {_clean_desc(items[n].get('description', ''), 90)}")


# ---------------- dependency resolution ----------------

def _frontmatter_of(path: Path) -> dict:
    from validate.replay import _frontmatter
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    fm, _ = _frontmatter(text)
    return fm if isinstance(fm, dict) else {}


def resolve(names: list[str]) -> tuple[dict[str, set], list[str]]:
    """Expand the requested names into {skill,agent,script,tool: {name,...}}, pulling in what each
    needs (an agent's listed skills; the script a wrapper tool just wraps). Returns (picked, notes)."""
    items = exportable()
    unknown = [n for n in names if n not in items]
    if unknown:
        raise ExportError(f"not installed (or not a skill/agent/script/tool): {', '.join(unknown)}")
    picked: dict[str, set] = {"skill": set(), "agent": set(), "script": set(), "tool": set()}
    notes: list[str] = []
    queue, seen = list(names), set()
    while queue:
        n = queue.pop()
        if n in seen or n not in items:
            continue
        seen.add(n)
        e = items[n]
        kind = e["type"]
        if kind == "tool":
            spec = json.loads((reg.install_root() / e["path"] / "tool.json").read_text(encoding="utf-8"))
            if "wraps" in spec:
                wrapped = spec["wraps"]["script"]
                notes.append(f"tool {n!r} just wraps script {wrapped!r}; exporting the script instead")
                if wrapped in items:
                    queue.append(wrapped)
                continue
            picked["tool"].add(n)
            continue
        picked[kind].add(n)
        if kind == "agent":
            fm = _frontmatter_of(reg.install_root() / e["path"] / f"{n}.md")
            for s in fm.get("skills") or []:
                if s in items and items[s]["type"] == "skill" and s not in seen:
                    notes.append(f"agent {n!r} also needs skill {s!r}; added it")
                    queue.append(s)
    return picked, notes


# ---------------- copying ----------------

def _clean_desc(s: str, n: int = 300) -> str:
    return " ".join((s or "").replace("<", "(").replace(">", ")").split())[:n]


def _copy_dir_filtered(src: Path, dst: Path, exclude: set[str]) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*exclude))


def export_skill(name: str, e: dict, dest: Path) -> None:
    _copy_dir_filtered(reg.install_root() / e["path"], dest / "skills" / name, SKILL_EXCLUDE)


def export_agent(name: str, e: dict, dest: Path) -> None:
    out = dest / "agents" / f"{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(reg.install_root() / e["path"] / f"{name}.md", out)


COMMAND_TEMPLATE = Template('''---
description: $description
---

Run the bundled script with the arguments given, then report its output.

```bash
python3 "$${CLAUDE_PLUGIN_ROOT}/scripts/$name/$entrypoint" $$ARGUMENTS
```
''')


def export_script(name: str, e: dict, dest: Path) -> None:
    _copy_dir_filtered(reg.install_root() / e["path"], dest / "scripts" / name, {"__pycache__"})
    body = COMMAND_TEMPLATE.substitute(description=json.dumps(_clean_desc(e.get("description", ""), 300)),
                                        name=name, entrypoint=e["entrypoint"])
    cmd_path = dest / "commands" / f"{name}.md"
    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    cmd_path.write_text(body, encoding="utf-8")


TOOL_SERVER_TEMPLATE = Template('''#!/usr/bin/env python3
"""Standalone MCP server for the exported tool '$name'.

Generated by ai-self-learning's contribute/export.py. Self-contained: no dependency on the original
ai-self-learning install, and no sandboxing (no network or resource isolation) -- review $entry
before trusting it on another machine. Runs $entry with the call's arguments as one JSON object on
stdin, and returns stdout as the tool's result.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / $entry_repr
NAME = $name_repr
DESCRIPTION = $description_repr
INPUT_SCHEMA = $schema_repr
SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write(chr(10))
    sys.stdout.flush()


def call(args):
    p = subprocess.run([sys.executable, "-I", str(ENTRY)], input=json.dumps(args), capture_output=True,
                        text=True, timeout=60)
    text = p.stdout
    if p.stderr:
        text = text + (chr(10) if text else "") + "[stderr]" + chr(10) + p.stderr
    if p.returncode != 0:
        text = "Tool exited with code " + str(p.returncode) + "." + chr(10) + text
    return {"content": [{"type": "text", "text": text or "(no output)"}], "isError": p.returncode != 0}


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or mid is None:
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        version = want if want in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": version,
                "capabilities": {"tools": {}}, "serverInfo": {"name": NAME, "version": "1.0.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": NAME, "description": DESCRIPTION, "inputSchema": INPUT_SCHEMA}]}}
    if method == "tools/call":
        if params.get("name") != NAME:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": "unknown tool: " + repr(params.get("name"))}}
        return {"jsonrpc": "2.0", "id": mid, "result": call(params.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: " + str(method)}}


def main():
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline=chr(10))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        batch = msg if isinstance(msg, list) else [msg]
        for m in batch:
            if isinstance(m, dict):
                r = handle(m)
                if r is not None:
                    send(r)


if __name__ == "__main__":
    sys.exit(main())
''')


def export_tool(name: str, e: dict, dest: Path) -> None:
    src = reg.install_root() / e["path"]
    spec = json.loads((src / "tool.json").read_text(encoding="utf-8"))
    entry_name = e["entrypoint"]
    out = dest / "mcp_tools" / name
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / entry_name, out / entry_name)
    server = TOOL_SERVER_TEMPLATE.substitute(
        name=name, entry=entry_name, entry_repr=repr(entry_name), name_repr=repr(name),
        description_repr=repr(_clean_desc(spec.get("description", e.get("description", "")), 900)),
        schema_repr=repr(spec.get("input_schema", {})))
    (out / "server.py").write_text(server, encoding="utf-8")


def _update_mcp_json(dest: Path, plugin_name: str, tool_names: list[str]) -> None:
    path = dest / ".mcp.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    servers = data.setdefault("mcpServers", {})
    for n in tool_names:
        servers[f"{plugin_name}-{n}"] = {"command": "python3",
                                          "args": [f"${{CLAUDE_PLUGIN_ROOT}}/mcp_tools/{n}/server.py"]}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _write_readme(dest: Path, plugin_name: str) -> None:
    def names(folder: str, strip_suffix: str = "") -> list[str]:
        d = dest / folder
        if not d.is_dir():
            return []
        out = [p.name[: -len(strip_suffix)] if strip_suffix and p.name.endswith(strip_suffix) else p.name
               for p in sorted(d.iterdir())]
        return [n for n in out if n]

    lines = [f"# {plugin_name}", "",
             "Skills, agents, scripts and tools exported from "
             "[ai-self-learning](https://github.com/longlephucwork/ai-self-learning): tasks this user's Claude "
             "Code learned from their own repeated work and approved, then exported for sharing. No session "
             "data or test fixtures are included.", "", "## Contents", ""]
    for label, folder, suffix in (("Skills", "skills", ""), ("Agents", "agents", ".md"),
                                   ("Commands (bundled scripts)", "commands", ".md"), ("MCP tools", "mcp_tools", "")):
        found = names(folder, suffix)
        if found:
            lines.append(f"- **{label}:** {', '.join(found)}")
    lines += ["", f"Last exported {store.now()}.",
              "", "Add this directory as a Claude Code plugin (see the Claude Code plugin documentation for the "
              "current steps), or push it to a remote repo first and point a marketplace entry at that."]
    (dest / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------- plugin manifest + git ----------------

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "claude-plugin"


def _bump_patch(version: str) -> str:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version or "")
    if not m:
        return "0.1.0"
    major, minor, patch = m.groups()
    return f"{major}.{minor}.{int(patch) + 1}"


def _write_manifest(dest: Path, *, name: str | None, description: str | None, version: str | None) -> str:
    manifest_dir = dest / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / "plugin.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    plugin_name = name or manifest.get("name") or _slug(dest.name)
    if not NAME_RX.match(plugin_name):
        raise ExportError(f"plugin name {plugin_name!r} must be lowercase kebab-case (letters, digits, hyphens)")
    manifest["name"] = plugin_name
    manifest["description"] = description or manifest.get("description") or \
        "Skills, agents and tools exported from ai-self-learning."
    if version:
        manifest["version"] = version
    elif "version" in manifest:
        manifest["version"] = _bump_patch(manifest["version"])
    else:
        manifest["version"] = "0.1.0"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return plugin_name


def _ensure_git(dest: Path) -> None:
    if not (dest / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(dest)], check=True, capture_output=True)


def _git_identity(dest: Path) -> list[str]:
    have = subprocess.run(["git", "-C", str(dest), "config", "user.email"], capture_output=True,
                           text=True).stdout.strip()
    return [] if have else ["-c", "user.name=ai-self-learning-export", "-c", "user.email=export@localhost"]


def _git_commit(dest: Path, message: str) -> str | None:
    _ensure_git(dest)
    ident = _git_identity(dest)
    subprocess.run(["git", "-C", str(dest), *ident, "add", "-A"], check=True, capture_output=True)
    if subprocess.run(["git", "-C", str(dest), "diff", "--cached", "--quiet"], capture_output=True).returncode == 0:
        return None  # nothing changed
    subprocess.run(["git", "-C", str(dest), *ident, "commit", "-q", "-m", message], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(dest), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True).stdout.strip()


# ---------------- export ----------------

def export(names: list[str], dest: Path, *, name: str | None = None, description: str | None = None,
           version: str | None = None) -> dict:
    picked, notes = resolve(names)
    if not any(picked.values()):
        raise ExportError("nothing to export")
    items = exportable()
    dest.mkdir(parents=True, exist_ok=True)
    plugin_name = _write_manifest(dest, name=name, description=description, version=version)

    added = []
    for n in sorted(picked["skill"]):
        export_skill(n, items[n], dest)
        added.append(f"skill {n}")
    for n in sorted(picked["agent"]):
        export_agent(n, items[n], dest)
        added.append(f"agent {n}")
    for n in sorted(picked["script"]):
        export_script(n, items[n], dest)
        added.append(f"script {n}")
    tool_names = sorted(picked["tool"])
    for n in tool_names:
        export_tool(n, items[n], dest)
        added.append(f"tool {n}")
    if tool_names:
        _update_mcp_json(dest, plugin_name, tool_names)
    _write_readme(dest, plugin_name)

    message = f"export: {', '.join(added)}" + ("\n\n" + "\n".join(notes) if notes else "")
    commit = _git_commit(dest, message)
    return {"plugin_name": plugin_name, "added": added, "notes": notes, "commit": commit, "dest": str(dest)}


def cmd_export(args) -> None:
    result = export(args.names, Path(args.dest).expanduser().resolve(), name=args.name,
                     description=args.description, version=args.version)
    for note in result["notes"]:
        print(f"note: {note}")
    if not result["added"]:
        print("nothing new to export")
    else:
        print(f"exported {', '.join(result['added'])} -> {result['dest']}")
    if result["commit"]:
        print(f"commit {result['commit']} in {result['dest']}")
    else:
        print("no changes to commit (already up to date)")
    print(f"nothing was pushed. When you're ready: cd {result['dest']} && git remote add origin <url> && "
          f"git push -u origin main")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("export")
    p.add_argument("names", nargs="+", help="installed skill/agent/script/tool names to export")
    p.add_argument("--dest", required=True, help="plugin folder to create or update")
    p.add_argument("--name", help="plugin name (kebab-case); defaults to the dest folder's name")
    p.add_argument("--description", help="plugin description")
    p.add_argument("--version", help="plugin version (default: bump the patch version, or 0.1.0 if new)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            cmd_list(a)
            return 0
        if a.cmd == "export":
            cmd_export(a)
            return 0
    except ExportError as e:
        sys.exit(f"error: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
