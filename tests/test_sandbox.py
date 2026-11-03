"""Checks that the code sandbox works on THIS machine (Linux, macOS or Windows).

    python3 -m unittest tests.test_sandbox -v

Linux uses `unshare -rn`, macOS uses `sandbox-exec`; on native Windows there is no sandbox and
the isolation tests are skipped (the refusal test still runs). See README > Installation.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness_lib import sandbox, tools  # noqa: E402

PROBE = ("import json, os, socket\n"
         "res = {}\n"
         "for name, addr in (('tcp_1.1.1.1:53', ('1.1.1.1', 53)), ('tcp_8.8.8.8:443', ('8.8.8.8', 443))):\n"
         "    try:\n"
         "        socket.create_connection(addr, timeout=3).close(); res[name] = 'open'\n"
         "    except OSError as e:\n"
         "        res[name] = 'blocked'\n"
         "try:\n"
         "    socket.getaddrinfo('example.com', 443); res['dns'] = 'resolved'\n"
         "except OSError:\n"
         "    res['dns'] = 'blocked'\n"
         "open('written.txt', 'w').write('ok')\n"
         "res['env'] = sorted(os.environ)\n"
         "print(json.dumps(res))\n")


def isolated():
    return sandbox.backend() != "none"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self):
        os.environ["SECRET_TOKEN_SANDBOX_TEST"] = "sk-must-not-leak-123456"
        try:
            r = sandbox.run([sys.executable, "-I", "-c", PROBE], cwd=self.box, env=sandbox.clean_env(self.box),
                            timeout=30)
        finally:
            os.environ.pop("SECRET_TOKEN_SANDBOX_TEST", None)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_backend_matches_os(self):
        b = sandbox.backend()
        print(f"\n    platform={sys.platform} backend={b}", end=" ")
        if sys.platform == "darwin":
            self.assertEqual(b, "sandbox-exec", "sandbox-exec should exist on macOS (/usr/bin/sandbox-exec)")
        elif sys.platform.startswith("linux"):
            if b == "none":
                self.skipTest("unshare -rn failed: unprivileged user namespaces are disabled (see README)")
            self.assertEqual(b, "unshare")
        else:
            self.assertEqual(b, "none")

    @unittest.skipUnless(isolated(), "no sandbox on this machine")
    def test_network_is_blocked(self):
        res = self.probe()
        self.assertEqual(res["tcp_1.1.1.1:53"], "blocked", res)
        self.assertEqual(res["tcp_8.8.8.8:443"], "blocked", res)

    @unittest.skipUnless(isolated(), "no sandbox on this machine")
    def test_dns_is_blocked(self):
        self.assertEqual(self.probe()["dns"], "blocked")

    @unittest.skipUnless(isolated(), "no sandbox on this machine")
    def test_files_can_be_written_in_cwd(self):
        self.probe()
        self.assertEqual(Path(self.box, "written.txt").read_text(), "ok")   # tools must write their outputs

    @unittest.skipUnless(isolated(), "no sandbox on this machine")
    def test_environment_is_scrubbed(self):
        env = self.probe()["env"]
        self.assertNotIn("SECRET_TOKEN_SANDBOX_TEST", env)
        self.assertTrue(set(env) <= {"PATH", "LANG", "HOME", "PYTHONDONTWRITEBYTECODE", "LC_CTYPE",
                                     "__CF_USER_TEXT_ENCODING"}, env)

    @unittest.skipUnless(os.name == "posix" and isolated(), "CPU limits need POSIX rlimits")
    def test_cpu_limit_stops_busy_loop(self):
        t0 = time.time()
        r = sandbox.run([sys.executable, "-I", "-c", "while True: pass"], cwd=self.box,
                        env=sandbox.clean_env(self.box), timeout=30, cpu_s=1)
        self.assertNotEqual(r.returncode, 0)
        self.assertLess(time.time() - t0, 15)

    @unittest.skipUnless(isolated(), "no sandbox on this machine")
    def test_mcp_tool_runs_inside_sandbox(self):
        script = Path(self.box, "probe_tool.py")
        script.write_text(PROBE)
        tool = {"name": "probe", "mode": "stdin-json", "command": script, "argv_template": None,
                "input_schema": {"type": "object", "properties": {}}}
        res = tools.run_tool(tool, {}, cwd=self.box)
        self.assertEqual(res["exit"], 0, res)
        self.assertEqual(json.loads(res["stdout"])["tcp_1.1.1.1:53"], "blocked")

    def test_refuses_without_sandbox_unless_opted_in(self):
        old = sandbox._backend
        sandbox._backend = "none"
        env_before = os.environ.pop("LEARNING_HARNESS_ALLOW_UNSANDBOXED", None)
        os.environ["LEARNING_HARNESS_STORE"] = self.box
        try:
            self.assertFalse(sandbox.may_run()[0])
            os.environ["LEARNING_HARNESS_ALLOW_UNSANDBOXED"] = "1"
            self.assertTrue(sandbox.may_run()[0])
        finally:
            sandbox._backend = old
            os.environ.pop("LEARNING_HARNESS_ALLOW_UNSANDBOXED", None)
            os.environ.pop("LEARNING_HARNESS_STORE", None)
            if env_before is not None:
                os.environ["LEARNING_HARNESS_ALLOW_UNSANDBOXED"] = env_before


if __name__ == "__main__":
    unittest.main()
