"""Static safety scan of generated artifacts. Deterministic, no execution.

Flags (reported to the reviewer; Phase 4 shows them before approval):
  network     sockets, HTTP clients, curl/wget, URLs
  exec        subprocess, os.system, eval/exec, dynamic imports
  secrets     environment access, credential files, keychains
  delete      file/directory removal
  abs_path    absolute filesystem paths or parent-directory escapes
  write_any   (info) opens files for writing; expected for most scripts
  network_local (info) curl/wget/ssh or URLs that only reach this machine (localhost, 127.0.0.1, ::1)
  network_allowed (info, skills only) curl/wget/ssh/scp/rsync or URLs whose every host is localhost or on the
              user's allowlist (`review/cli.py allowed-hosts`). A target the scan can't read (a URL in a
              variable, a host without http(s)://), any other host, nc/ncat, and HTTP libraries in Python stay
              `network`. Piping a download into a shell (`curl ... | sh`) is `exec`.
  claude_path (info) mentions a .claude folder, e.g. where a skill is installed. Claude's credentials and
              settings files (.claude.json, .claude/.credentials.json, .claude/settings*.json) are `secrets`.
A script's first line `#!/usr/bin/env <interpreter>` is not an environment read.
For Python the scan walks the AST; for other files it uses regexes.
"""
from __future__ import annotations

import ast
import re
import shlex

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
NET_TOOL = re.compile(r"\b(curl|wget|nc|ncat|ssh|scp|rsync)\b", re.IGNORECASE)
PIPE_TO_SHELL = re.compile(r"\|\s*(sudo\s+)?(ba|z|da|k)?sh\b")
SSH_VALUE_FLAGS = set("bcDEeFIiJLlmOopQRSWw")   # ssh options that take a value: their value isn't the host
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
HOST_NAME = re.compile(rf"(\*\.)?{_LABEL}(\.{_LABEL})*")   # api.example.com, buildbox, *.example.com


def _host(h: str) -> str:
    return h.lower().strip("[]").rsplit("@", 1)[-1]


def host_allowed(host: str, allowed: frozenset | set | None) -> bool:
    """localhost always; else an exact entry of `allowed`, or `*.example.com` for its subdomains."""
    h = _host(host)
    if h in {x.strip("[]") for x in LOCAL_HOSTS}:
        return True
    for a in allowed or ():
        a = a.lower()
        if h == a or (a.startswith("*.") and h.endswith(a[1:])):
            return True
    return False


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:   # an unbalanced quote: fall back to plain splitting
        return text.split()


def _ssh_target(tool: str, rest: str) -> str | None:
    """The remote host of an ssh/scp/rsync command (rest = what follows the tool's name), or None if unreadable."""
    words = []
    for w in _words(rest):
        if w in ("|", "||", "&&", ";") or w.startswith((">", "<")):
            break
        words.append(w)
    if tool == "ssh":
        skip = False
        for w in words:
            if skip:
                skip = False
                continue
            if w.startswith("-"):
                skip = len(w) == 2 and w[1] in SSH_VALUE_FLAGS
                continue
            return None if "$" in w else w.rsplit("@", 1)[-1]
        return None
    for w in words:   # scp / rsync: [user@]host:path, or rsync://host/...
        if w.startswith("rsync://"):
            return w[len("rsync://"):].split("/")[0].split(":")[0]
        m = re.match(r"^(?:[^@/\s]+@)?([^:/\s$]+):", w)
        if m and not re.match(r"^[A-Za-z]$", m.group(1)):   # not a Windows drive (C:)
            return m.group(1)
    return None


def network_targets(line: str) -> tuple[list[str], list[str]]:
    """(hosts the line reaches, network uses on it whose target can't be read)."""
    hosts = [h for h in URL_HOST.findall(line)]
    unknown = []
    for m in NET_TOOL.finditer(line):
        tool, rest = m.group(1).lower(), line[m.end():]
        before = line[:m.start()].rstrip()
        if before.endswith(("-e", "--rsh=", "--rsh")):
            continue   # rsync -e ssh: rsync's own target is read
        if tool in ("nc", "ncat"):
            unknown.append(f"{tool} (never allowed)")
        elif tool in ("ssh", "scp", "rsync"):
            h = _ssh_target(tool, rest)
            if h:
                hosts.append(h)
            else:
                unknown.append(f"{tool} with a target it can't read")
        else:   # curl / wget: need a literal http(s):// URL, or a bare localhost
            seg = re.split(r"\||;|&&", rest, maxsplit=1)[0]
            if not URL_HOST.search(seg) and not re.search(r"(^|\s)['\"]?(localhost|127\.0\.0\.1)(:\d+)?(/|\s|['\"]|$)", seg):
                unknown.append(f"{tool} without a literal http(s):// URL")
            elif not URL_HOST.search(seg):
                hosts.append("localhost")
    return hosts, unknown


def _network_kind(line: str, allowed) -> tuple[str, str]:
    """('network_local' | 'network_allowed' | 'network', detail) for a line that uses the network."""
    hosts, unknown = network_targets(line)
    bad = sorted({_host(h) for h in hosts if not host_allowed(h, allowed)})
    if unknown or bad or not hosts:
        why = "; ".join(unknown + ([f"host not allowed: {', '.join(bad)}"] if bad else []))
        return "network", why or "network use"
    if all(_host(h) in {x.strip("[]") for x in LOCAL_HOSTS} for h in hosts):
        return "network_local", "only localhost"
    return "network_allowed", f"allowed host(s): {', '.join(sorted({_host(h) for h in hosts}))}"


def _local_only(line: str) -> bool:
    """Every network use on the line stays on this machine."""
    return _network_kind(line, None)[0] == "network_local"

TEXT_RULES = [
    ("network", re.compile(r"\b(curl|wget|nc|ncat|ssh|scp|rsync)\b|https?://", re.I)),
    ("exec", re.compile(r"\b(eval|exec|bash -c|sh -c|subprocess|os\.system)\b")),
    ("exec", PIPE_TO_SHELL),
    ("secrets", re.compile(r"\$\{?[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)|printenv|\benv\b|" + SECRET_PATTERNS.pattern, re.I)),
    ("delete", re.compile(r"\brm\s+-|\brmdir\b|\bunlink\b|shutil\.rmtree|os\.remove")),
]


def _flag(out, kind, path, line, detail):
    out.append({"kind": kind, "file": path, "line": line, "detail": detail})


def scan_python(path: str, src: str, allowed=None) -> list[dict]:
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
                _flag(out, _network_kind(v, allowed)[0], path, ln, f"URL {v[:60]!r}")
            elif CLAUDE_PATH.search(v) and not SECRET_PATTERNS.search(v):
                _flag(out, "claude_path", path, ln, f"string mentions {v[:60]!r}")
            if ".." in v.split("/"):
                _flag(out, "abs_path", path, ln, f"parent-directory path {v[:60]!r}")
    return out


def scan_text(path: str, src: str, allowed=None) -> list[dict]:
    out: list[dict] = []
    for i, line in enumerate(src.splitlines(), 1):
        if i == 1 and SHEBANG.fullmatch(line):
            continue   # #!/usr/bin/env bash: picks the interpreter, reads nothing
        for kind, rx in TEXT_RULES:
            m = rx.search(line)
            if not m:
                continue
            if kind == "network":
                kind, why = _network_kind(line, allowed)
                _flag(out, kind, path, i, f"matches {m.group(0)!r} ({why})")
                continue
            _flag(out, kind, path, i, f"matches {m.group(0)!r}")
        if CLAUDE_PATH.search(line) and not SECRET_PATTERNS.search(line):
            _flag(out, "claude_path", path, i, "mentions a .claude folder")
    return out


def scan_files(files: dict[str, str], *, allowed_hosts=None) -> list[dict]:
    """`allowed_hosts`: hosts besides localhost that network use may reach without blocking (skills only)."""
    allowed = frozenset(allowed_hosts or ())
    flags: list[dict] = []
    for path, src in files.items():
        flags += scan_python(path, src, allowed) if path.endswith(".py") else scan_text(path, src, allowed)
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


# ---------------- the user's allowlist, and the gate for each kind of artifact ----------------

def allowed_hosts() -> list[str]:
    """Hosts a generated skill may reach besides localhost: "scan": {"allowed_hosts": [...]} in store/config.json."""
    from harness_lib import store
    return list(((store.read_json(store.store_dir() / "config.json") or {}).get("scan") or {}).get("allowed_hosts", []))


def check_host_entry(entry: str) -> str:
    """A valid allowlist entry, normalised: a host name (api.example.com, buildbox), *.example.com for its
    subdomains, or an IP address. ValueError otherwise (no scheme, port or path)."""
    import ipaddress
    e = entry.strip().lower()
    bare = e[1:-1] if e.startswith("[") and e.endswith("]") else e
    try:
        return str(ipaddress.ip_address(bare))
    except ValueError:
        pass
    if not HOST_NAME.fullmatch(e):
        raise ValueError(f"{entry!r} isn't a host: use a name like api.example.com, *.example.com or an IP "
                         f"(no scheme, port or path)")
    return e


def set_allowed_hosts(hosts: list[str]) -> list[str]:
    from harness_lib import store
    path = store.store_dir() / "config.json"
    cfg = store.read_json(path) or {}
    cfg.setdefault("scan", {})["allowed_hosts"] = sorted(set(hosts))
    store.write_json_atomic(path, cfg)
    return allowed_hosts()


def scan_artifact(kind: str, files: dict[str, str]) -> list[dict]:
    """The safety scan the gate applies to an artifact of this kind. A skill's evals/ are test fixtures the harness
    wrote (not part of the skill), and a skill may reach the user's allowed hosts. Scripts and tools may only
    reach localhost: their tests run without a network."""
    if kind == "skill":
        return scan_files({k: v for k, v in files.items() if not k.startswith("evals/")},
                          allowed_hosts=allowed_hosts())
    return scan_files(files)
