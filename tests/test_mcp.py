import json
import os
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests import test_phase4 as p4  # noqa: E402  (module import so its tests aren't re-discovered)
from tests.test_phase3 import proposal  # noqa: E402

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["input", "output"],
          "properties": {"input": {"type": "string", "description": "CSV path"},
                         "output": {"type": "string", "description": "JSON path"},
                         "delimiter": {"type": "string", "description": "field delimiter"}}}
TEMPLATE = ["{input}", "{output}", {"when": "delimiter", "argv": ["--delimiter", "{delimiter}"]}]
STDIN_TOOL = ('import json, sys\nargs = json.load(sys.stdin)\nwords = args["text"].split()\n'
              'print(json.dumps({"words": len(words), "longest": max(words, key=len) if words else ""}))\n')


def expose_answer(template=TEMPLATE, name="csv_to_json", examples=None):
    tests = ["t-s-fruit", "t-s-price", "t-s-kv"]
    return {"tool_name": name, "description": "Convert a CSV file to a JSON array of objects.",
            "input_schema_json": json.dumps(SCHEMA), "argv_template_json": json.dumps(template),
            "examples": examples if examples is not None else
            [{"test": t, "arguments_json": json.dumps({"input": "in.csv", "output": "out.json"})} for t in tests]}


class MCPServer:
    """Drive mcp_tools/server.py over stdio exactly like a client would."""

    def __init__(self, env, cwd):
        self.p = subprocess.Popen([sys.executable, str(ROOT / "mcp_tools" / "server.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)
        self.n = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return None if notify else self.read(lambda m: m.get("id") == self.n)

    def raw(self, line):
        self.p.stdin.write(line + "\n"); self.p.stdin.flush()
        return self.read(lambda m: True)

    def read(self, want, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.p.stdout], [], [], 0.5)
            if r:
                line = self.p.stdout.readline()
                if not line:
                    raise EOFError("server closed stdout")
                m = json.loads(line)          # every stdout line must be one JSON-RPC message
                if want(m):
                    return m
        raise TimeoutError("no matching message")

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)
        self.p.stdout.close()
        self.p.stderr.close()
        return self.p.returncode


class MCP(p4.Phase4):
    def setUp(self):
        super().setUp()
        self.draft(proposal())
        rc, out = self.cli("approve", "csv-to-json", "--yes")
        self.assertEqual(rc, 0, out)
        self.project = self.t / "project"
        self.project.mkdir()
        (self.project / "data.csv").write_text("region;sales\nnorth;120\n")
        (self.project / "plain.csv").write_text("a,b\n1,x\n")

    def expose(self, answer, second=None, attempts=1):
        p = self.t / "e1.json"; p.write_text(json.dumps(answer)); os.environ["FAKE_EXPOSE_FILE"] = str(p)
        if second:
            q = self.t / "e2.json"; q.write_text(json.dumps(second)); os.environ["FAKE_EXPOSE_FILE_2"] = str(q)
        from refine.expose import expose
        return expose("csv-to-json", max_attempts=attempts, log=lambda *_: None)

    def server(self, **extra):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.project), **extra}
        s = MCPServer(env, cwd=self.t)
        def cleanup():
            if s.p.poll() is None:
                s.p.kill(); s.p.wait()
            for f in (s.p.stdin, s.p.stdout, s.p.stderr):
                if not f.closed:
                    f.close()
        self.addCleanup(cleanup)
        init = s.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})
        s.send("notifications/initialized", notify=True)
        return s, init

    def install_exposed(self):
        r = self.expose(expose_answer())
        self.assertTrue(r["passed"], r["report"]["problems"])
        rc, out = self.cli("approve", "csv_to_json", "--yes")
        self.assertEqual(rc, 0, out)

    # ---------------- expose: validation ----------------

    def test_expose_validates_against_script_tests(self):
        r = self.expose(expose_answer())
        self.assertTrue(r["passed"], r["report"]["problems"])
        self.assertEqual(r["report"]["coverage"], {"successful_sessions": 3, "covered": 3, "required": 3})
        self.assertEqual(len(r["report"]["tests"]), 3)         # script tests re-run for real
        self.assertTrue(all(t["passed"] for t in r["report"]["tests"]))
        meta = json.loads((self.repo / "generated" / "csv_to_json" / "proposal.json").read_text())
        self.assertEqual((meta["type"], meta["wraps"]["script"], meta["file_list"]), ("tool", "csv-to-json", ["tool.json"]))

    def test_expose_rejects_wrong_mapping_then_repairs(self):
        r = self.expose(expose_answer(template=["{output}", "{input}"]), second=expose_answer(), attempts=2)
        self.assertTrue(r["passed"])
        r = self.expose(expose_answer(template=["{output}", "{input}"]))
        self.assertFalse(r["passed"])
        self.assertTrue(any("template renders ['out.json', 'in.csv']" in p for p in r["report"]["problems"]))

    def test_expose_rejects_bad_templates_and_missing_examples(self):
        r = self.expose(expose_answer(template=["{input}", "{output}", "--delimiter", "{delimiter}"]))
        self.assertTrue(any("must be inside a {'when'" in p for p in r["report"]["problems"]))
        r = self.expose(expose_answer(examples=[]))
        self.assertTrue(any("no example arguments" in p for p in r["report"]["problems"]))

    # ---------------- install + protocol ----------------

    def test_protocol_and_calls(self):
        self.install_exposed()
        self.assertTrue((self.repo / "mcp_tools" / "tools" / "csv_to_json" / "tool.json").exists())
        s, init = self.server(CLAUDE_CODE_SESSION_ID="sess-mcp")
        res = init["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertEqual(res["capabilities"], {"tools": {"listChanged": True}})
        self.assertEqual(s.send("ping")["result"], {})
        tools = s.send("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["csv_to_json"])
        self.assertEqual(tools[0]["inputSchema"], SCHEMA)
        self.assertIn("Learned from 3 successful past sessions", tools[0]["description"])

        ok = s.send("tools/call", {"name": "csv_to_json", "arguments": {"input": "plain.csv", "output": "plain.json"}})
        self.assertFalse(ok["result"]["isError"], ok)
        self.assertEqual(json.loads((self.project / "plain.json").read_text()), [{"a": 1, "b": "x"}])

        bad = s.send("tools/call", {"name": "csv_to_json", "arguments": {"input": "--help", "output": "x.json"}})
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("may not start with '-'", bad["result"]["content"][0]["text"])
        extra = s.send("tools/call", {"name": "csv_to_json", "arguments": {"input": "a", "output": "b", "rm": "x"}})
        self.assertIn("unknown argument", extra["result"]["content"][0]["text"])
        fail = s.send("tools/call", {"name": "csv_to_json", "arguments": {"input": "missing.csv", "output": "m.json"}})
        self.assertTrue(fail["result"]["isError"])
        self.assertIn("exited with code", fail["result"]["content"][0]["text"])

        self.assertEqual(s.send("tools/call", {"name": "nope", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(s.send("resources/list")["error"]["code"], -32601)
        self.assertEqual(s.raw("{not json")["error"]["code"], -32700)
        self.assertEqual(s.send("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"],
                         "2025-11-25")                         # unknown version -> our latest
        self.assertEqual(s.close(), 0)                         # exits cleanly when the client closes stdin

        log = self.store.read_jsonl(self.store.store_dir() / "usage" / "csv_to_json.jsonl")
        self.assertEqual([(r["via"], r["session_id"], r["exit"], bool(r.get("rejected"))) for r in log],
                         [("mcp", "sess-mcp", 0, False), ("mcp", "sess-mcp", None, True),
                          ("mcp", "sess-mcp", None, True), ("mcp", "sess-mcp", 2, False)])
        self.assertNotIn("plain.csv", json.dumps(log))         # arguments are never logged
        from refine import refine
        from registry import install
        st = refine.stats(install.load_registry()["installed"]["csv_to_json"])
        self.assertEqual((st["uses"], st["failures"], st["by_via"]["mcp"]), (4, 3, 4))

    def test_tool_runs_sandboxed_without_network(self):
        from harness_lib import tools as rt
        probe = self.t / "probe.py"
        probe.write_text("import socket,os,json\ntry:\n socket.create_connection(('1.1.1.1',53),timeout=3);n='open'\n"
                         "except OSError:\n n='blocked'\nprint(json.dumps({'net':n,'cwd':os.getcwd(),'env':sorted(os.environ)}))\n")
        os.environ["SECRET_TOKEN_X"] = "sk-not-for-tools-123456"
        tool = {"name": "probe", "mode": "stdin-json", "command": probe, "argv_template": None,
                "input_schema": {"type": "object", "properties": {}}}
        out = json.loads(rt.run_tool(tool, {}, cwd=str(self.project))["stdout"])
        self.assertEqual(out["net"], "blocked")
        self.assertEqual(out["cwd"], str(self.project))
        self.assertNotIn("SECRET_TOKEN_X", out["env"])

    def test_list_changed_when_registry_changes(self):
        self.install_exposed()
        s, _ = self.server()
        self.assertEqual(len(s.send("tools/list")["result"]["tools"]), 1)
        from registry import install
        with self.assertRaises(install.InstallError):         # the tool depends on the script
            install.uninstall("csv-to-json")
        install.uninstall("csv_to_json", reason="test")
        note = s.read(lambda m: m.get("method") == "notifications/tools/list_changed", timeout=15)
        self.assertNotIn("id", note)
        self.assertEqual(s.send("tools/list")["result"]["tools"], [])
        s.close()

    def test_generated_stdin_tool_end_to_end(self):
        self.make_pattern("word-stats", 1.0, "tool")
        t = {"name": "count", "source_session": "s-fruit", "provenance": "synthetic", "rationale": "", "args": [],
             "stdin": json.dumps({"text": "a bb ccc"}), "input_files": [], "expected_exit": 0,
             "expected_stdout": '{"words": 3, "longest": "ccc"}', "stdout_compare": "json", "expected_files": []}
        tool = {"type": "tool", "type_deviation_reason": "", "name": "word-stats", "description": "Count words",
                "spec": "s", "entrypoint": "word_stats.py",
                "tool_input_schema_json": json.dumps({"type": "object", "required": ["text"],
                                                      "properties": {"text": {"type": "string"}}}),
                "files": [{"path": "word_stats.py", "content": STDIN_TOOL}], "tests": [t]}
        p = self.t / "p.json"; p.write_text(json.dumps(tool)); os.environ["FAKE_PROPOSAL_FILE"] = str(p)
        from generate.propose import propose
        r = propose("word-stats", log=lambda *_: None, max_attempts=1)
        # the only test is synthetic, so coverage of real sessions must fail
        self.assertTrue(any("tests cover 0 successful" in x for x in r["problems"]), r["problems"])
        bad = dict(t, stdin=json.dumps({"txt": "a"}))
        p.write_text(json.dumps({**tool, "tests": [bad]}))
        r = propose("word-stats", log=lambda *_: None, max_attempts=1)
        self.assertTrue(any("missing text" in x for x in r["problems"]), r["problems"])

        # install a valid generated tool directly (validation of logged coverage is covered elsewhere)
        from registry import install
        d = self.repo / "generated" / "word-stats"
        files = {"word_stats.py": STDIN_TOOL, "tool.json": json.dumps({"name": "word-stats", "description": "Count words",
                 "runner": "stdin-json", "input_schema": json.loads(tool["tool_input_schema_json"])})}
        (d / "files").mkdir(parents=True, exist_ok=True)
        for k, v in files.items():
            (d / "files" / k).write_text(v)
        meta = {"name": "word-stats", "type": "tool", "pattern_key": "word-stats", "status": "validated",
                "description": "Count words", "entrypoint": "word_stats.py", "file_list": sorted(files), "tests": [t],
                "evidence": {"sessions": [], "n_success": 3, "n_sessions": 3}}
        self.store.write_json_atomic(d / "proposal.json", meta)
        install.install(meta, files, draft_dir=d, note=None, validation={"tests": []})
        s, _ = self.server()
        names = [x["name"] for x in s.send("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["word-stats"])
        out = s.send("tools/call", {"name": "word-stats", "arguments": {"text": "hello big world"}})
        self.assertEqual(json.loads(out["result"]["content"][0]["text"]), {"words": 3, "longest": "hello"})
        miss = s.send("tools/call", {"name": "word-stats", "arguments": {}})
        self.assertTrue(miss["result"]["isError"])
        s.close()


for _n in [n for n in vars(p4.Phase4) if n.startswith("test_")]:
    setattr(MCP, _n, None)


if __name__ == "__main__":
    unittest.main()
