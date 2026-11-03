#!/usr/bin/env python3
"""Install the learning harness on this machine (Linux, macOS or Windows).

Usually started by ./install.sh (Linux/macOS) or install.bat / install.ps1 (Windows),
which find a suitable Python and clone the repo first. Run directly:

  python3 install.py                 interactive: asks before each change
  python3 install.py --yes           accept the defaults without asking
  python3 install.py --check         show what is installed; change nothing
  python3 install.py --uninstall     undo everything this installer did (keeps the repo and your data)

Options: --no-hooks  --no-mcp  --no-path  --skip-tests  --allow-unsandboxed

What it does (each step asks first, and is safe to re-run):
  1. checks Python >= 3.10, git and the `claude` CLI
  2. detects the code sandbox (Linux: unshare, macOS: sandbox-exec; none on native Windows)
  3. runs a quick offline self-test
  4. hooks:  logs every Claude Code session (Stop + SessionEnd in ~/.claude/settings.json)
  5. MCP:    registers mcp_tools/server.py as the user-scope MCP server `learned`
  6. PATH:   adds bin/ (usage-logging wrappers for installed scripts) and sets LEARNING_HARNESS_PYTHON
Nothing is sent anywhere; nothing runs in the background.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
MIN_PY = (3, 10)
MARK_BEGIN = "# >>> learning-harness >>>"
MARK_END = "# <<< learning-harness <<<"
IS_WIN = os.name == "nt"


# ---------------- small helpers ----------------

class UI:
    def __init__(self, yes: bool):
        self.yes = yes
        self.interactive = sys.stdin.isatty() and not yes

    def ask(self, question: str, default: bool = True) -> bool:
        if not self.interactive:
            return default
        hint = "[Y/n]" if default else "[y/N]"
        ans = input(f"  {question} {hint} ").strip().lower()
        return default if not ans else ans in ("y", "yes")


def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say(f"  [ok]   {msg}")


def warn(msg):
    say(f"  [warn] {msg}")


def fail(msg):
    say(f"  [fail] {msg}")


def python_exe() -> str:
    return Path(sys.executable).resolve().as_posix()


def claude_bin() -> str | None:
    return os.environ.get("LEARNING_HARNESS_CLAUDE_BIN") or shutil.which("claude")


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def os_name() -> str:
    return {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux" if sys.platform.startswith("linux")
                                                       else sys.platform)


# ---------------- 1. prerequisites ----------------

def check_prereqs() -> bool:
    good = True
    v = sys.version_info
    if v[:2] >= MIN_PY:
        ok(f"Python {v.major}.{v.minor}.{v.micro} ({python_exe()})")
    else:
        fail(f"Python {v.major}.{v.minor} is too old; need {MIN_PY[0]}.{MIN_PY[1]}+")
        good = False
    if shutil.which("git"):
        ok("git found")
    else:
        fail("git not found (needed: approvals commit every learned change)")
        good = False
    if claude_bin():
        r = run([claude_bin(), "--version"])
        ok(f"Claude Code CLI found ({(r.stdout or r.stderr).strip()[:60]})")
    else:
        warn("`claude` CLI not found: hooks and the MCP server need Claude Code; install it, then re-run")
    return good


# ---------------- 2. sandbox ----------------

def sandbox_status(ui: UI, allow_flag: bool, dry: bool = False) -> None:
    sys.path.insert(0, str(REPO))
    from harness_lib import sandbox, store
    b = sandbox.backend()
    if b != "none":
        probe = ("import socket\ntry:\n socket.create_connection(('1.1.1.1', 53), timeout=3); print('open')\n"
                 "except OSError:\n print('blocked')\n")
        home = str(store.store_dir())
        r = sandbox.run([sys.executable, "-I", "-c", probe], cwd=home, env=sandbox.clean_env(home), timeout=20)
        if r.stdout.strip() == "blocked":
            ok(f"code sandbox: {b} (network blocked for generated code and tools)")
        else:
            warn(f"code sandbox {b} started, but the network probe said {r.stdout.strip() or r.stderr.strip()!r}")
        return
    hint = {"Linux": "unprivileged user namespaces are off (Ubuntu 24.04+: see README > Installation)",
            "Windows": "native Windows has no equivalent; WSL gives you the Linux sandbox",
            "macOS": "sandbox-exec is missing or blocked"}.get(os_name(), "")
    warn(f"no code sandbox on this system: {hint}")
    if sandbox.allow_unsandboxed():
        warn("you previously allowed running generated code and tools WITHOUT network isolation")
        return
    if dry:
        return
    say("  Without a sandbox, validation and MCP tools refuse to run code. You can allow them to run without\n"
        "  network isolation; the static safety scan still rejects network code, and nothing installs\n"
        "  without your approval.")
    allow = allow_flag or (ui.interactive and ui.ask("Allow running code without network isolation?", default=False))
    if allow:
        cfg_path = store.store_dir() / "config.json"
        cfg = store.read_json(cfg_path) or {}
        cfg["allow_unsandboxed"] = True
        store.write_json_atomic(cfg_path, cfg)
        warn(f"allowed (saved in {cfg_path}); delete that setting to undo")
    else:
        warn("left disabled: hooks, detection, review and the MCP server still work; tools won't run")


# ---------------- 3. self-test ----------------

def self_test() -> bool:
    r = run([sys.executable, "-m", "unittest", "-q", "tests.test_phase1", "tests.test_phase2"], cwd=REPO)
    tail = (r.stderr.strip().splitlines() or ["?"])[-1]
    (ok if r.returncode == 0 else fail)(f"self-test: {tail}")
    if r.returncode:
        say(r.stderr[-2000:])
    return r.returncode == 0


# ---------------- 4. hooks ----------------

def user_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def hooks_installed() -> bool:
    try:
        s = json.loads(user_settings().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return any("log_session.py" in h.get("command", "")
               for ev in ("Stop", "SessionEnd") for g in s.get("hooks", {}).get(ev, []) for h in g.get("hooks", []))


def setup_hooks(uninstall=False) -> bool:
    cmd = [sys.executable, str(REPO / "hooks" / "install_hooks.py"), "--scope", "user", "--python", python_exe()]
    r = run(cmd + (["--uninstall"] if uninstall else []))
    (ok if r.returncode == 0 else fail)(("removed hooks from " if uninstall else "session-logging hooks in ")
                                        + str(user_settings()) + ("" if r.returncode == 0 else f": {r.stderr[-300:]}"))
    return r.returncode == 0


# ---------------- 5. MCP ----------------

def mcp_installed() -> bool:
    c = claude_bin()
    return bool(c) and run([c, "mcp", "get", "learned"]).returncode == 0


def setup_mcp(uninstall=False) -> bool:
    c = claude_bin()
    server = (REPO / "mcp_tools" / "server.py").as_posix()
    if not c:
        warn("skipped MCP registration (no `claude` CLI). Later run:\n"
             f"         claude mcp add --scope user learned -- \"{python_exe()}\" \"{server}\"")
        return False
    run([c, "mcp", "remove", "learned", "-s", "user"])  # idempotent: replace any older registration
    if uninstall:
        ok("removed MCP server `learned` (user scope)")
        return True
    r = run([c, "mcp", "add", "--scope", "user", "learned", "--", python_exe(), server])
    if r.returncode != 0:
        fail(f"claude mcp add failed: {(r.stderr or r.stdout).strip()[:300]}")
        return False
    ok("MCP server `learned` registered for all projects (tools appear as mcp__learned__<name>)")
    return True


# ---------------- 6. PATH + LEARNING_HARNESS_PYTHON ----------------

def rc_file() -> Path:
    shell = os.path.basename(os.environ.get("SHELL", ""))
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    if shell == "bash":
        return home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    return home / ".profile"


def rc_block(rc: Path) -> str:
    bin_dir, py = (REPO / "bin").as_posix(), python_exe()
    if rc.name == "config.fish":
        body = f'set -gx PATH "{bin_dir}" $PATH\nset -gx LEARNING_HARNESS_PYTHON "{py}"'
    else:
        body = f'export PATH="{bin_dir}:$PATH"\nexport LEARNING_HARNESS_PYTHON="{py}"'
    return f"{MARK_BEGIN}\n{body}\n{MARK_END}\n"


def edit_rc(rc: Path, block: str | None) -> None:
    """Replace (or remove, when block is None) our marked block; leave everything else untouched."""
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""
    if MARK_BEGIN in text and MARK_END in text:
        a = text.index(MARK_BEGIN)
        b = text.index(MARK_END) + len(MARK_END)
        text = (text[:a].rstrip("\n") + "\n" if text[:a].strip() else "") + text[b:].lstrip("\n")
    if block:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    if text or rc.exists():
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text(text, encoding="utf-8")


def _win_env(set_vars: dict[str, str | None], path_add: str | None, path_remove: str | None) -> None:
    import winreg  # Windows only
    import ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            cur, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            cur, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in cur.split(";") if p]
        norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731
        if path_remove:
            parts = [p for p in parts if norm(p) != norm(path_remove)]
        if path_add and norm(path_add) not in {norm(p) for p in parts}:
            parts.insert(0, path_add)
        winreg.SetValueEx(k, "Path", 0, kind, ";".join(parts))
        for name, val in set_vars.items():
            if val is None:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
    # tell running programs (Explorer, new terminals) that the environment changed
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)


def setup_path(uninstall=False) -> bool:
    bin_dir = str(REPO / "bin")
    if IS_WIN:
        _win_env({"LEARNING_HARNESS_PYTHON": None if uninstall else str(Path(sys.executable).resolve())},
                 None if uninstall else bin_dir, bin_dir if uninstall else None)
        ok(("removed bin\\ from" if uninstall else "added bin\\ to") + " your user PATH (open a new terminal)")
        return True
    rc = rc_file()
    edit_rc(rc, None if uninstall else rc_block(rc))
    ok(("removed the PATH block from " if uninstall else "added bin/ to PATH in ") + f"{rc} (open a new terminal)")
    return True


def path_installed() -> bool:
    if IS_WIN:
        return str(REPO / "bin").lower() in os.environ.get("PATH", "").lower()
    rc = rc_file()
    return rc.exists() and MARK_BEGIN in rc.read_text(encoding="utf-8")


# ---------------- main ----------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", "-y", action="store_true", help="accept defaults without asking")
    ap.add_argument("--check", action="store_true", help="report status only")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-hooks", action="store_true")
    ap.add_argument("--no-mcp", action="store_true")
    ap.add_argument("--no-path", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--allow-unsandboxed", action="store_true",
                    help="allow generated code/tools to run without network isolation (systems with no sandbox)")
    a = ap.parse_args(argv)
    ui = UI(a.yes)

    say(f"learning-harness installer: {os_name()}, repo at {REPO}")
    if a.check:
        say("\nstatus:")
        check_prereqs()
        sandbox_status(ui, False, dry=True)
        (ok if hooks_installed() else warn)(f"hooks {'installed' if hooks_installed() else 'not installed'}")
        (ok if mcp_installed() else warn)(f"MCP server `learned` {'registered' if mcp_installed() else 'not registered'}")
        (ok if path_installed() else warn)(f"bin/ {'on' if path_installed() else 'not on'} PATH")
        return 0

    if a.uninstall:
        say("\nuninstalling (your repo, store/ data and installed artifacts are kept):")
        if not a.no_hooks and ui.ask("Remove the session-logging hooks?"):
            setup_hooks(uninstall=True)
        if not a.no_mcp and ui.ask("Unregister the MCP server `learned`?"):
            setup_mcp(uninstall=True)
        if not a.no_path and ui.ask("Remove bin/ from PATH?"):
            setup_path(uninstall=True)
        say(f"\ndone. To delete everything else: remove {REPO}")
        return 0

    say("\n1. prerequisites")
    if not check_prereqs():
        say("\nfix the [fail] items above and re-run.")
        return 1
    say("\n2. code sandbox")
    sandbox_status(ui, a.allow_unsandboxed)
    if not a.skip_tests:
        say("\n3. self-test")
        if not self_test():
            say("\nself-test failed; nothing was changed. Re-run with --skip-tests to install anyway.")
            return 1
    say("\n4. session logging (hooks)")
    if a.no_hooks:
        say("  skipped (--no-hooks)")
    elif ui.ask(f"Log every Claude Code session? (adds Stop/SessionEnd hooks to {user_settings()})"):
        setup_hooks()
    say("\n5. MCP tool server")
    if a.no_mcp:
        say("  skipped (--no-mcp)")
    elif ui.ask("Register the `learned` MCP server for all projects? (claude mcp add --scope user)"):
        setup_mcp()
    say("\n6. PATH")
    if a.no_path:
        say("  skipped (--no-path)")
    elif ui.ask("Add bin/ (wrappers for installed scripts) to your PATH?"):
        setup_path()

    py = "python" if IS_WIN else "python3"
    say(f"\ndone. Next (from {REPO}):")
    say("  - use Claude Code as usual; sessions are logged to store/ in this repo (git-ignored)")
    say(f"  - rate sessions:   {py} review/cli.py list   /   {py} review/cli.py rate last accepted")
    say(f"  - find patterns:   {py} refine/refine.py cycle")
    say(f"  - review/approve:  {py} review/cli.py proposals")
    say(f"  - status/undo:     {py} install.py --check   /   {py} install.py --uninstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
