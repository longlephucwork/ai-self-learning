import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import install  # noqa: E402

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_CLAUDE_CALLS"]
calls = json.load(open(log)) if os.path.exists(log) else []
calls.append(sys.argv[1:])
json.dump(calls, open(log, "w"))
if sys.argv[1:3] == ["mcp", "get"]:
    sys.exit(0 if any(c[:2] == ["mcp", "add"] for c in calls) and not calls[-2][:2] == ["mcp", "remove"] else 1)
print("9.9.9 (Claude Code)")
"""


class Installer(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        (t / "home").mkdir()
        fake = t / "claude"
        fake.write_text(FAKE_CLAUDE); fake.chmod(0o755)
        if os.name == "nt":  # Windows can't execute a .py by its shebang: wrap it in a .cmd
            (t / "claude.py").write_text(FAKE_CLAUDE)
            fake = t / "claude.cmd"
            fake.write_text(f'@"{sys.executable}" "{t / "claude.py"}" %*\r\n')
        os.environ.update(HOME=str(t / "home"), SHELL="/bin/zsh", LEARNING_HARNESS_CLAUDE_BIN=str(fake),
                          FAKE_CLAUDE_CALLS=str(t / "calls.json"), LEARNING_HARNESS_STORE=str(t / "store"))

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear(); os.environ.update(self._env)

    def calls(self):
        return json.loads((self.t / "calls.json").read_text())

    def run_main(self, *argv):
        out = io.StringIO()
        # Exercise the shell-rc PATH code on every OS and never touch the real Windows registry.
        with redirect_stdout(out), mock.patch.object(install, "IS_WIN", False), \
                mock.patch.object(install, "_win_env", side_effect=AssertionError("registry touched")):
            rc = install.main(list(argv))
        return rc, out.getvalue()

    def run_interactive(self, answer, *argv, sandbox_backend=None):
        from harness_lib import sandbox
        out = io.StringIO()
        patches = [mock.patch.object(install, "IS_WIN", False),
                   mock.patch.object(install, "_win_env", side_effect=AssertionError("registry touched")),
                   mock.patch("sys.stdin.isatty", return_value=True),
                   mock.patch("builtins.input", side_effect=lambda prompt="": self.prompts.append(prompt) or answer)]
        self.prompts = []
        if sandbox_backend:
            patches.append(mock.patch.object(sandbox, "_backend", sandbox_backend))
        with redirect_stdout(out):
            for p in patches:
                p.start()
            try:
                rc = install.main(["--skip-tests", *argv])
            finally:
                for p in reversed(patches):
                    p.stop()
        return rc, out.getvalue()

    def test_simple_install_is_one_question(self):
        rc, out = self.run_interactive("")                                        # Enter = yes
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.prompts), 1, self.prompts)                         # exactly one question
        for line in ("log your Claude Code sessions", "learn in the background", 'say "learn this"',
                     "available to Claude", "check for new releases"):
            self.assertIn(line, out)
        self.assertNotIn("without network isolation", out)                       # this machine has a sandbox
        self.assertTrue(install.hooks_installed())
        self.assertIn("Done. Use Claude Code as usual", out)

    def test_simple_install_no_changes_nothing(self):
        rc, out = self.run_interactive("n")
        self.assertEqual(rc, 1)
        self.assertIn("Cancelled; nothing was changed", out)
        self.assertFalse((Path(os.environ["HOME"]) / ".claude" / "settings.json").exists())
        self.assertFalse(any(c[:2] == ["mcp", "add"] for c in (self.calls() if (self.t / "calls.json").exists() else [])))

    def test_simple_install_without_sandbox_says_so_in_the_summary(self):
        from harness_lib import sandbox
        rc, out = self.run_interactive("y", sandbox_backend="none")
        self.assertEqual(rc, 0, out)
        self.assertIn("test drafted scripts without network isolation", out)
        self.assertEqual(len(self.prompts), 1, self.prompts)                         # still one question
        with mock.patch.object(sandbox, "_backend", "none"):
            self.assertTrue(sandbox.may_run()[0])                                 # the yes covered it

    def test_custom_still_asks_step_by_step(self):
        rc, out = self.run_interactive("y", "--custom")
        self.assertEqual(rc, 0, out)
        self.assertGreater(len(self.prompts), 3)

    def test_rc_block_is_idempotent_and_removable(self):
        rc = Path(os.environ["HOME"]) / ".zshrc"
        rc.write_text("alias ll='ls -l'\n")
        for _ in range(3):
            install.edit_rc(rc, install.rc_block(rc))
        text = rc.read_text()
        self.assertEqual(text.count(install.MARK_BEGIN), 1)
        self.assertIn(f'export PATH="{install.bin_dir_path().as_posix()}:$PATH"', text)
        with mock.patch.dict(os.environ):
            os.environ.pop("LEARNING_HARNESS_INSTALL_ROOT", None)   # the real default
            self.assertEqual(install.bin_dir_path(), ROOT / "learned" / "bin")
        self.assertIn("alias ll='ls -l'", text)
        install.edit_rc(rc, None)
        self.assertEqual(rc.read_text(), "alias ll='ls -l'\n")

    def test_rc_file_per_shell(self):
        home = Path(os.environ["HOME"])
        for shell, name in (("/bin/zsh", ".zshrc"), ("/usr/bin/fish", "config.fish"), ("/bin/sh", ".profile")):
            os.environ["SHELL"] = shell
            self.assertEqual(install.rc_file().name, name)
        os.environ["SHELL"] = "/bin/bash"
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(install.rc_file(), home / ".bash_profile")
        fish = home / ".config" / "fish" / "config.fish"
        self.assertIn("set -gx PATH", install.rc_block(fish))

    def test_full_install_check_and_uninstall(self):
        rc, out = self.run_main("--yes", "--skip-tests")
        self.assertEqual(rc, 0, out)
        settings = json.loads((Path(os.environ["HOME"]) / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for ev in ("Stop", "SessionEnd") for g in settings["hooks"][ev] for h in g["hooks"]]
        self.assertEqual(len(cmds), 2)
        self.assertTrue(all(c.startswith(f'"{install.python_exe()}"') for c in cmds), cmds)  # absolute interpreter
        add = [c for c in self.calls() if c[:2] == ["mcp", "add"]][-1]
        self.assertEqual(add[:5], ["mcp", "add", "--scope", "user", "learned"])
        self.assertEqual(add[-2:], [install.python_exe(), (ROOT / "mcp_tools" / "server.py").as_posix()])
        self.assertIn(install.MARK_BEGIN, (Path(os.environ["HOME"]) / ".zshrc").read_text())
        rc, out = self.run_main("--yes", "--skip-tests")              # re-run: still one of each
        settings = json.loads((Path(os.environ["HOME"]) / ".claude" / "settings.json").read_text())
        self.assertEqual(sum(len(g["hooks"]) for g in settings["hooks"]["Stop"]), 1)
        self.assertEqual((Path(os.environ["HOME"]) / ".zshrc").read_text().count(install.MARK_BEGIN), 1)
        rc, out = self.run_main("--check")
        self.assertIn("hooks installed", out)
        self.assertIn("on PATH", out)
        rc, out = self.run_main("--uninstall", "--yes")
        self.assertEqual(rc, 0, out)
        settings = json.loads((Path(os.environ["HOME"]) / ".claude" / "settings.json").read_text())
        self.assertNotIn("hooks", settings)
        self.assertNotIn(install.MARK_BEGIN, (Path(os.environ["HOME"]) / ".zshrc").read_text())
        self.assertEqual(self.calls()[-1][:3], ["mcp", "remove", "learned"])

    def test_automatic_learning_step(self):
        from refine import auto
        rc, out = self.run_main("--yes", "--skip-tests", "--no-mcp", "--no-path")
        self.assertEqual(rc, 0, out)
        cfg = auto.config()
        self.assertEqual((cfg["enabled"], cfg["monthly_budget_usd"]), (True, 5.0))
        self.assertIn("automatic learning on", out)
        settings = json.loads((Path(os.environ["HOME"]) / ".claude" / "settings.json").read_text())
        self.assertEqual(sorted(settings["hooks"]), ["SessionEnd", "SessionStart", "Stop", "UserPromptSubmit"])
        rc, out = self.run_main("--yes", "--skip-tests", "--no-mcp", "--no-path", "--budget", "2.5")
        self.assertEqual(auto.config()["monthly_budget_usd"], 2.5)
        rc, out = self.run_main("--check")
        self.assertIn("automatic learning on; $0.00 of $2.50", out)
        rc, out = self.run_main("--yes", "--skip-tests", "--no-mcp", "--no-path", "--no-auto")
        self.assertTrue(auto.config()["enabled"])                  # --no-auto skips the step; doesn't flip it
        rc, out = self.run_main("--uninstall", "--yes")
        self.assertFalse(auto.config()["enabled"])
        self.assertIn("automatic learning off", out)

    def test_update_check_step(self):
        from harness_lib import update
        rc, out = self.run_main("--yes", "--skip-tests", "--no-mcp", "--no-path", "--no-auto")
        self.assertTrue(update.config()["check"])
        self.assertIn("daily update check on", out)
        rc, out = self.run_main("--check")
        self.assertIn(f"version {update.current_version()}; daily update check on", out)
        rc, out = self.run_main("--yes", "--skip-tests", "--no-mcp", "--no-path", "--no-auto", "--no-update-check")
        self.assertFalse(update.config()["check"])
        update.set_config(check=True)
        rc, out = self.run_main("--uninstall", "--yes")
        self.assertFalse(update.config()["check"])

    def test_opt_outs(self):
        rc, out = self.run_main("--yes", "--skip-tests", "--no-hooks", "--no-mcp", "--no-path")
        self.assertEqual(rc, 0)
        self.assertFalse((Path(os.environ["HOME"]) / ".claude" / "settings.json").exists())
        self.assertFalse((Path(os.environ["HOME"]) / ".zshrc").exists())
        self.assertFalse(any(c[:2] == ["mcp", "add"] for c in self.calls()))

    def test_without_claude_cli_prints_manual_command(self):
        os.environ["LEARNING_HARNESS_CLAUDE_BIN"] = ""
        with mock.patch("shutil.which", side_effect=lambda n: None if n == "claude" else "/usr/bin/" + n):
            rc, out = self.run_main("--yes", "--skip-tests", "--no-hooks", "--no-path")
        self.assertEqual(rc, 0)
        self.assertIn("claude mcp add --scope user learned", out)

    def test_no_sandbox_refuses_unless_opted_in(self):
        from harness_lib import sandbox
        with mock.patch.object(sandbox, "_backend", "none"):
            self.assertFalse(sandbox.may_run()[0])
            self.assertIn("no network sandbox", sandbox.may_run()[1])
            rc, out = self.run_main("--yes", "--skip-tests", "--no-hooks", "--no-mcp", "--no-path")
            self.assertIn("left disabled", out)                     # --yes never opts in silently
            self.assertFalse(sandbox.may_run()[0])
            rc, out = self.run_main("--yes", "--skip-tests", "--no-hooks", "--no-mcp", "--no-path",
                                    "--allow-unsandboxed")
            self.assertTrue(sandbox.may_run()[0])
            self.assertTrue(json.loads((self.t / "store" / "config.json").read_text())["allow_unsandboxed"])

    def test_sandbox_blocks_network_here(self):
        from harness_lib import sandbox
        if sandbox.backend() == "none":
            self.skipTest("no sandbox backend on this machine")
        r = sandbox.run([sys.executable, "-I", "-c",
                         "import socket\ntry:\n socket.create_connection(('1.1.1.1',53),timeout=3);print('open')\n"
                         "except OSError:\n print('blocked')"], cwd=str(self.t), env=sandbox.clean_env(str(self.t)),
                        timeout=20)
        self.assertEqual(r.stdout.strip(), "blocked")

    def test_windows_env_is_complete(self):
        from harness_lib import sandbox
        with mock.patch.object(os, "name", "nt"), mock.patch.dict(os.environ, {"SYSTEMROOT": r"C:\Windows",
                                                                               "PATH": r"C:\Windows", "API_TOKEN": "x"}):
            env = sandbox.clean_env(r"C:\tmp\box")
        self.assertEqual(env["SYSTEMROOT"], r"C:\Windows")
        self.assertEqual(env["USERPROFILE"], r"C:\tmp\box")
        self.assertNotIn("API_TOKEN", env)

    def test_learned_items_live_in_a_local_repo_that_is_never_pushed(self):
        """learned/ is its own git repo (created on first use) and the code repo ignores it."""
        import subprocess, tempfile
        from harness_lib import store
        with mock.patch.dict(os.environ):
            os.environ.pop("LEARNING_HARNESS_INSTALL_ROOT", None)   # check the real default
            self.assertEqual(store.learned_dir(), ROOT / "learned")
        ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "learned/registry.json"])
        self.assertEqual(ignored.returncode, 0, "learned/ must be git-ignored by the code repo")
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "patterns", "generated", "registry.json",
                                  "scripts", "skills", "bin", "learned"], capture_output=True, text=True).stdout
        self.assertEqual(tracked, "", "the code repo must not ship learned items")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(store, "REPO_ROOT", Path(d)):
            os.environ.pop("LEARNING_HARNESS_INSTALL_ROOT", None)
            from registry import install as reg
            self.assertTrue(reg.git_available())                     # created on first use
            self.assertTrue((Path(d) / "learned" / ".git").is_dir())
            self.assertTrue((Path(d) / "learned" / "README.md").exists())

    def test_wrapper_finds_the_store_relative_to_itself(self):
        import tempfile
        from harness_lib import store
        from registry import install as reg
        with tempfile.TemporaryDirectory() as d:
            os.environ["LEARNING_HARNESS_INSTALL_ROOT"] = str(Path(d) / "learned")
            os.environ["LEARNING_HARNESS_STORE"] = str(Path(d) / "store")
            p = reg.write_shim({"name": "x", "version": 1, "path": "scripts/x", "entrypoint": "x.py"})
            text = p.read_text()
            self.assertIn("'../store'", text)                       # relative: survives moving the repo
            data = p.with_name("x.cmd").read_bytes()
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))   # Windows wrapper: CRLF

    def test_wsl_detection(self):
        cases = {"5.15.153.1-microsoft-standard-WSL2\n": "WSL2", "6.6.36.6-microsoft-standard-WSL2+\n": "WSL2",
                 "4.4.0-19041-Microsoft\n": "WSL1", "6.8.0-45-generic\n": None, "": None}
        with mock.patch.object(install, "base_os", return_value="Linux"), \
                mock.patch.object(install, "in_container", return_value=False):
            for rel, want in cases.items():
                with mock.patch.object(install, "_osrelease", return_value=rel):
                    self.assertEqual(install.wsl_version(), want, rel)
                    self.assertEqual(install.os_name(), f"Linux ({want})" if want else "Linux")
        with mock.patch.object(install, "base_os", return_value="macOS"), \
                mock.patch.object(install, "_osrelease", return_value="microsoft"):
            self.assertIsNone(install.wsl_version())

    def test_wsl_rejects_windows_side_claude_and_warns_about_mnt(self):
        out = io.StringIO()
        with mock.patch.object(install, "wsl_version", return_value="WSL2"), \
                mock.patch.object(install, "claude_bin", return_value="/mnt/c/Users/me/AppData/Roaming/npm/claude"), \
                mock.patch.object(install, "REPO", Path("/mnt/c/Users/me/claude-learning-harness")), redirect_stdout(out):
            good = install.check_prereqs()
        self.assertFalse(good)
        self.assertIn("resolves to the Windows Claude Code", out.getvalue())
        self.assertIn("the repo is on the Windows drive", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(install, "wsl_version", return_value="WSL1"), redirect_stdout(out):
            self.assertTrue(install.check_prereqs())
        self.assertIn("WSL1, which has no network namespaces", out.getvalue())
        rc, out = self.run_main("--yes", "--skip-tests")                      # not WSL here: no WSL warnings
        self.assertNotIn("WSL", out.replace("WSL gives", ""))

    def test_container_detection_and_hint(self):
        with mock.patch.object(install, "base_os", return_value="Linux"), \
                mock.patch.object(install, "wsl_version", return_value=None), \
                mock.patch.object(install, "in_container", return_value=True):
            self.assertEqual(install.os_name(), "Linux (container)")
            from harness_lib import sandbox
            out = io.StringIO()
            with mock.patch.object(sandbox, "_backend", "none"), redirect_stdout(out):
                install.sandbox_status(install.UI(True), False)
        self.assertIn("seccomp=docker/seccomp-userns.json", out.getvalue())

    def test_seccomp_profile_adds_only_unshare(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("msp", ROOT / "docker" / "make-seccomp-profile.py")
        msp = importlib.util.module_from_spec(spec); spec.loader.exec_module(msp)
        base = {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": [{"names": ["read"], "action": "SCMP_ACT_ALLOW"}]}
        out = msp.build(json.loads(json.dumps(base)))
        out = msp.build(out)                                         # idempotent
        self.assertEqual(out["syscalls"][0], base["syscalls"][0])
        self.assertEqual([r["names"] for r in out["syscalls"]], [["read"], ["unshare"]])
        shipped = json.loads((ROOT / "docker" / "seccomp-userns.json").read_text())
        self.assertEqual(shipped["defaultAction"], "SCMP_ACT_ERRNO")
        self.assertEqual(shipped["syscalls"][-1]["names"], ["unshare"])
        with self.assertRaises(SystemExit):
            msp.build({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": []})

    def test_launchers_and_line_endings(self):
        self.assertTrue(os.access(ROOT / "install.sh", os.X_OK))
        self.assertNotIn(b"\r\n", (ROOT / "install.sh").read_bytes())
        for f in ("install.bat", "install.ps1"):
            data = (ROOT / f).read_bytes()
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), f)   # every newline is CRLF
        self.assertIn('install.ps1" %*', (ROOT / "install.bat").read_text())


if __name__ == "__main__":
    unittest.main()
