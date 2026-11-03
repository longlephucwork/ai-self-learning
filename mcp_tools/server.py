#!/usr/bin/env python3
"""Local MCP server `learned`: exposes the tools you approved through review/cli.py.

Not a network service. Claude Code starts this script as a child process when a
session begins and talks to it over stdin/stdout (newline-delimited JSON-RPC 2.0,
the MCP stdio transport). It exits when Claude Code closes the pipe.

Register it once:
  claude mcp add --scope user learned -- python3 /path/to/learning-harness/mcp_tools/server.py
or rely on this repo's .mcp.json (project scope).

Per call it validates the arguments against the tool's schema, runs the tool
sandboxed in the project directory ($CLAUDE_PROJECT_DIR, else cwd), and appends a
usage line to store/usage/<tool>.jsonl (no arguments or output are logged).
registry.json is re-read on every request, and a notifications/tools/list_changed
message is sent when it changes, so approving or uninstalling a tool takes effect
in running sessions.

Diagnostics go to stderr only (stdout is the protocol channel).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402
from harness_lib import tools as rt  # noqa: E402
from registry import install as reg  # noqa: E402

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]  # newest first
from harness_lib.update import current_version  # noqa: E402

SERVER_INFO = {"name": "learning-harness", "title": "Learned tools", "version": current_version()}
INSTRUCTIONS = ("Tools learned from this user's past Claude Code sessions and explicitly approved by them. "
                "Each one does one job deterministically; prefer it over redoing that job by hand.")
POLL_S = 3.0

_out_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[learned-mcp] {msg}", file=sys.stderr, flush=True)
    trace = os.environ.get("LEARNING_HARNESS_MCP_LOG")  # optional diagnostics file
    if trace:
        try:
            with open(trace, "a", encoding="utf-8") as fh:
                fh.write(f"{store.now()} {msg}\n")
        except OSError:
            pass


def send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class Server:
    def __init__(self):
        self.root = reg.install_root()
        self.project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        self.initialized = False
        self._mtime = self._registry_mtime()
        self._tools = self._load()

    # ----- registry -----

    def _registry_mtime(self) -> float:
        try:
            return reg.registry_path().stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _load(self) -> dict:
        return rt.load_tools(self.root, reg.load_registry())

    def refresh(self) -> bool:
        """Reload if registry.json changed; True when the tool set changed."""
        m = self._registry_mtime()
        if m == self._mtime:
            return False
        self._mtime = m
        new = self._load()
        changed = {k: (v["description"], json.dumps(v["input_schema"], sort_keys=True)) for k, v in new.items()} != \
                  {k: (v["description"], json.dumps(v["input_schema"], sort_keys=True)) for k, v in self._tools.items()}
        self._tools = new
        return changed

    def watch(self) -> None:
        while True:
            time.sleep(POLL_S)
            try:
                if self.refresh() and self.initialized:
                    log("registry changed; notifying client")
                    send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            except Exception as e:  # never kill the server over a bad registry write
                log(f"refresh failed: {e}")

    # ----- protocol -----

    def handle(self, msg: dict) -> dict | None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if os.environ.get("LEARNING_HARNESS_MCP_LOG") and method:
            extra = f" protocolVersion={params.get('protocolVersion')}" if method == "initialize" else \
                    f" tool={params.get('name')}" if method == "tools/call" else ""
            log(f"<- {method}{extra}")  # method names only: never arguments
        if method is None:
            return None  # a response to something we never send; ignore
        if mid is None:  # notification
            if method == "notifications/initialized":
                self.initialized = True
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                version = want if want in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                return self._ok(mid, {"protocolVersion": version,
                                      "capabilities": {"tools": {"listChanged": True}},
                                      "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                self.refresh()
                return self._ok(mid, {"tools": [
                    {"name": t["name"], "description": self._describe(t), "inputSchema": t["input_schema"]}
                    for t in sorted(self._tools.values(), key=lambda t: t["name"])]})
            if method == "tools/call":
                return self._ok(mid, self.call(params.get("name"), params.get("arguments") or {}))
            return self._err(mid, -32601, f"method not found: {method}")
        except LookupError as e:
            return self._err(mid, -32602, str(e))
        except Exception as e:
            log(f"internal error on {method}: {e!r}")
            return self._err(mid, -32603, "internal error")

    def _describe(self, t: dict) -> str:
        e = t["entry"]
        ev = e.get("evidence") or {}
        return (f"{t['description']} (Learned from {ev.get('n_success', '?')} successful past sessions; "
                f"v{e['version']}, approved {e['approved_at'][:10]}. Runs sandboxed without network access; "
                f"relative paths resolve against the project directory.)")

    def call(self, name, args) -> dict:
        self.refresh()
        tool = self._tools.get(name)
        if tool is None:
            raise LookupError(f"unknown tool: {name!r}")
        res = rt.run_tool(tool, args, cwd=self.project)
        self._log_usage(tool, res)
        if res.get("rejected"):
            return {"content": [{"type": "text", "text": f"Invalid call: {res['rejected']}"}], "isError": True}
        text = res["stdout"]
        if res["stderr"]:
            text += ("\n" if text else "") + f"[stderr]\n{res['stderr']}"
        if res["exit"] != 0:
            text = f"Tool exited with code {res['exit']}.\n{text}"
        return {"content": [{"type": "text", "text": text or "(no output)"}], "isError": res["exit"] != 0}

    def _log_usage(self, tool: dict, res: dict) -> None:
        try:
            rec = {"name": tool["name"], "version": tool["entry"]["version"], "at": store.now(),
                   "exit": res.get("exit"), "duration_ms": res.get("duration_ms"), "via": "mcp",
                   "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID")}
            if res.get("rejected"):
                rec["rejected"] = res["rejected"][:300]
            store.append_jsonl(store.store_dir() / "usage" / f"{tool['name']}.jsonl", rec)
        except Exception as e:
            log(f"usage log failed: {e}")

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> int:
    # MCP stdio is UTF-8 with \n line endings; Windows would otherwise use the ANSI code page and \r\n.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    srv = Server()
    log(f"started; {len(srv._tools)} tool(s); project={srv.project}")
    threading.Thread(target=srv.watch, daemon=True).start()
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
        replies = [r for m in batch if isinstance(m, dict) and (r := srv.handle(m)) is not None]
        for r in replies:
            send(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
