"""Cross-platform sandbox for running generated code and learned tools.

Network isolation backend, picked automatically:
  linux    unshare -rn          new user + network namespace (needs unprivileged user namespaces)
  macos    sandbox-exec         Seatbelt profile that denies all network access
  other    none                 no isolation available (e.g. native Windows)
Resource limits (CPU, memory, file size, no core dumps) are applied on POSIX systems, as far as the
OS supports each one. The environment is always scrubbed and every run has a timeout.

With no isolation backend, callers refuse to run code unless you opted in, either with
LEARNING_HARNESS_ALLOW_UNSANDBOXED=1 or with {"allow_unsandboxed": true} in store/config.json
(the installer asks). The static safety scan (harness_lib/scan.py) blocks network code regardless.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import store

MAC_PROFILE = "(version 1)(allow default)(deny network*)"
_backend: str | None = None


def _ok(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def backend() -> str:
    """'unshare', 'sandbox-exec' or 'none' (cached)."""
    global _backend
    if _backend is None:
        if sys.platform.startswith("linux") and shutil.which("unshare") and _ok(["unshare", "-rn", "true"]):
            _backend = "unshare"
        elif sys.platform == "darwin" and shutil.which("sandbox-exec") and \
                _ok(["sandbox-exec", "-p", MAC_PROFILE, "/usr/bin/true"]):
            _backend = "sandbox-exec"
        else:
            _backend = "none"
    return _backend


def network_isolated() -> bool:
    return backend() != "none"


def config() -> dict:
    return store.read_json(store.store_dir() / "config.json") or {}


def allow_unsandboxed() -> bool:
    return os.environ.get("LEARNING_HARNESS_ALLOW_UNSANDBOXED") == "1" or bool(config().get("allow_unsandboxed"))


def may_run(explicit_allow: bool = False) -> tuple[bool, str]:
    """Whether code may run here, and why not."""
    if network_isolated() or explicit_allow or allow_unsandboxed():
        return True, ""
    return False, ("no network sandbox is available on this system (Linux: unshare -rn, macOS: sandbox-exec); "
                   "refusing to run code. Re-run the installer to opt in, or set LEARNING_HARNESS_ALLOW_UNSANDBOXED=1")


def wrap(cmd: list[str]) -> list[str]:
    b = backend()
    if b == "unshare":
        return ["unshare", "-rn", "--", *cmd]
    if b == "sandbox-exec":
        return ["sandbox-exec", "-p", MAC_PROFILE, *cmd]
    return list(cmd)


def clean_env(home: str) -> dict[str, str]:
    env = {"HOME": home, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":
        # Windows can't start Python without these; nothing secret in them.
        for k in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        env.update(PATH=os.environ.get("PATH", ""), TEMP=home, TMP=home, USERPROFILE=home)
    else:
        env["PATH"] = "/usr/bin:/bin"
    return env


def _limiter(cpu_s: int, mem_mb: int, fsize_mb: int):
    def apply():
        try:
            import resource
        except ImportError:
            return
        for name, value in (("RLIMIT_CPU", cpu_s), ("RLIMIT_AS", mem_mb * 2**20),
                            ("RLIMIT_FSIZE", fsize_mb * 2**20), ("RLIMIT_CORE", 0)):
            lim = getattr(resource, name, None)
            if lim is None:
                continue
            try:
                resource.setrlimit(lim, (value, value))
            except (ValueError, OSError):
                pass  # e.g. macOS doesn't enforce RLIMIT_AS; keep the others
    return apply


def run(cmd: list[str], *, cwd: str, env: dict, stdin: str = "", timeout: int,
        cpu_s: int = 10, mem_mb: int = 512, fsize_mb: int = 10) -> subprocess.CompletedProcess:
    """Run `cmd` sandboxed. Raises subprocess.TimeoutExpired on timeout."""
    kw = {}
    if os.name == "posix":
        kw["preexec_fn"] = _limiter(cpu_s, mem_mb, fsize_mb)
    return subprocess.run(wrap(cmd), cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout, **kw)
