"""Static safety scan of generated artifacts. Deterministic, no execution.

Flags (reported to the reviewer; Phase 4 shows them before approval):
  network     sockets, HTTP clients, curl/wget, URLs
  exec        subprocess, os.system, eval/exec, dynamic imports
  secrets     environment access, credential files, keychains
  delete      file/directory removal
  abs_path    absolute filesystem paths or parent-directory escapes
  write_any   (info) opens files for writing; expected for most scripts
  network_local (info) curl/wget or URLs that only reach this machine (localhost, 127.0.0.1, ::1)
  claude_path (info) mentions a .claude folder, e.g. where a skill is installed. Claude's credentials and
              settings files (.claude.json, .claude/.credentials.json, .claude/settings*.json) are `secrets`.
A script's first line `#!/usr/bin/env <interpreter>` is not an environment read.
For Python the scan walks the AST; for other files it uses regexes.
"""
from __future__ import annotations

import ast
import re

NETWORK_MODULES = {"socket", "ssl", "urllib", "urllib2", "urllib3", "http", "httpx", "requests", "aiohttp",
                   "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "websocket", "paramiko"}
EXEC_MODULES = {"subprocess", "pty", "multiprocessing", "ctypes", "importlib", "runpy", "code"}
EXEC_CALLS = {"eval", "exec", "compile", "__import__"}
OS_EXEC = {"system", "popen", "spawnl", "spawnv", "spawnve", "execv", "execve", "execl", "fork", "kill"}
OS_DELETE = {"remove", "unlink", "rmdir", "removedirs"}
SHUTIL_DELETE = {"rmtree", "move"}
SECRET_PATTERNS = re.compile(r"(\.ssh|\.aws|\.netrc|\.gnupg|\.config/gcloud|id_rsa|id_ed25519|keychain|"
                             r"credentials|\.env\b|/etc/passwd|/etc/shadow|\.claude\.json|\.claude[/\\]\.?credentials|"
                             r"\.claude[/\\]settings)", re.I)
CLAUDE_PATH = re.compile(r"\.claude\b")
SHEBANG = re.compile(r"#!\s*/[\w./-]+(\s+[\w.+-]+)?\s*")   # #!/usr/bin/env bash, #!/bin/sh
URL_HOST = re.compile(r"https?://(\[[^\]]*\]|[^/\s\"'`:?#]+)", re.IGNORECASE)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"}
REMOTE_TOOLS = re.compile(r"\b(nc|ncat|ssh|scp|rsync)\b", re.IGNORECASE)


def _local_only(line: str) -> bool:
    """The line's network use stays on this machine: every URL is localhost, and no remote-shell tool."""
    hosts = [h.lower() for h in URL_HOST.findall(line)]
    return bool(hosts) and all(h in LOCAL_HOSTS for h in hosts) and not REMOTE_TOOLS.search(line)

TEXT_RULES = [
    ("network", re.compile(r"\b(curl|wget|nc|ncat|ssh|scp|rsync)\b|https?://", re.I)),
    ("exec", re.compile(r"\b(eval|exec|bash -c|sh -c|subprocess|os\.system)\b")),
    ("secrets", re.compile(r"\$\{?[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)|printenv|\benv\b|" + SECRET_PATTERNS.pattern, re.I)),
    ("delete", re.compile(r"\brm\s+-|\brmdir\b|\bunlink\b|shutil\.rmtree|os\.remove")),
]


def _flag(out, kind, path, line, detail):
    out.append({"kind": kind, "file": path, "line": line, "detail": detail})


def scan_python(path: str, src: str) -> list[dict]:
    out: list[dict] = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        _flag(out, "syntax_error", path, e.lineno or 0, str(e))
        return out
    for n in ast.walk(tree):
        ln = getattr(n, "lineno", 0)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for m in mods:
                root = m.split(".")[0]
                if root in NETWORK_MODULES:
                    _flag(out, "network", path, ln, f"imports {m}")
                if root in EXEC_MODULES:
                    _flag(out, "exec", path, ln, f"imports {m}")
        elif isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else None
            attr = f.attr if isinstance(f, ast.Attribute) else None
            base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
            if name in EXEC_CALLS:
                _flag(out, "exec", path, ln, f"calls {name}()")
            if base == "os" and attr in OS_EXEC:
                _flag(out, "exec", path, ln, f"calls os.{attr}()")
            if base == "os" and attr in OS_DELETE or base == "shutil" and attr in SHUTIL_DELETE:
                _flag(out, "delete", path, ln, f"calls {base}.{attr}()")
            if attr in ("unlink", "rmdir") and base not in ("os",):
                _flag(out, "delete", path, ln, f"calls .{attr}()")
            if base == "os" and attr in ("getenv", "putenv", "unsetenv"):
                _flag(out, "secrets", path, ln, f"calls os.{attr}()")
            if name == "open" or attr == "open":
                mode = None
                if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                    mode = n.args[1].value
                for kw in n.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if isinstance(mode, str) and any(c in mode for c in "wax+"):
                    _flag(out, "write_any", path, ln, f"opens a file with mode {mode!r}")
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            if n.value.id == "os" and n.attr in ("environ", "environb"):
                _flag(out, "secrets", path, ln, "reads os.environ")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value
            if SECRET_PATTERNS.search(v):
                _flag(out, "secrets", path, ln, f"string mentions {SECRET_PATTERNS.search(v).group(0)!r}")
            if re.match(r"^(/|~/|[A-Za-z]:\\)", v) and len(v) > 1 and "\n" not in v:
                _flag(out, "abs_path", path, ln, f"absolute path {v[:60]!r}")
            if re.search(r"https?://", v):
                _flag(out, "network_local" if _local_only(v) else "network", path, ln, f"URL {v[:60]!r}")
            elif CLAUDE_PATH.search(v) and not SECRET_PATTERNS.search(v):
                _flag(out, "claude_path", path, ln, f"string mentions {v[:60]!r}")
            if ".." in v.split("/"):
                _flag(out, "abs_path", path, ln, f"parent-directory path {v[:60]!r}")
    return out


def scan_text(path: str, src: str) -> list[dict]:
    out: list[dict] = []
    for i, line in enumerate(src.splitlines(), 1):
        if i == 1 and SHEBANG.fullmatch(line):
            continue   # #!/usr/bin/env bash: picks the interpreter, reads nothing
        for kind, rx in TEXT_RULES:
            m = rx.search(line)
            if not m:
                continue
            if kind == "network" and _local_only(line):
                kind = "network_local"
            _flag(out, kind, path, i, f"matches {m.group(0)!r}")
        if CLAUDE_PATH.search(line) and not SECRET_PATTERNS.search(line):
            _flag(out, "claude_path", path, i, "mentions a .claude folder")
    return out


def scan_files(files: dict[str, str]) -> list[dict]:
    flags: list[dict] = []
    for path, src in files.items():
        flags += scan_python(path, src) if path.endswith(".py") else scan_text(path, src)
    # de-duplicate identical findings
    seen, out = set(), []
    for f in flags:
        k = (f["kind"], f["file"], f["line"], f["detail"])
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


BLOCKING_KINDS = {"network", "exec", "secrets", "delete", "abs_path", "syntax_error"}


def blocking(flags: list[dict]) -> list[dict]:
    return [f for f in flags if f["kind"] in BLOCKING_KINDS]
