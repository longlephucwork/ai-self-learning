"""Exporting installed skills/agents/scripts/tools as a standalone Claude Code plugin (contribute/export.py)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Contribute(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.root = self.t / "learned"
        os.environ["LEARNING_HARNESS_INSTALL_ROOT"] = str(self.root)
        os.environ["LEARNING_HARNESS_STORE"] = str(self.t / "store")
        self.registry = {"format_version": 1, "installed": {}, "history": []}

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._env)

    def save_registry(self):
        from harness_lib import store
        from registry import install as reg
        store.write_json_atomic(reg.registry_path(), self.registry)

    def add_skill(self, name, desc, with_evals=True):
        d = self.root / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\nSteps.\n")
        if with_evals:
            (d / "evals" / "case1").mkdir(parents=True, exist_ok=True)
            (d / "evals" / "case1" / "prompt.md").write_text("a private snippet of a real session")
        self.registry["installed"][name] = {"name": name, "type": "skill", "path": f"skills/{name}",
                                             "description": desc, "version": 1}

    def add_agent(self, name, desc, skills):
        d = self.root / "agents" / name
        d.mkdir(parents=True, exist_ok=True)
        skills_line = f"\nskills: [{', '.join(skills)}]" if skills else ""
        (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: {desc}{skills_line}\nmodel: inherit\n"
                                       f"---\n\nBody.\n")
        self.registry["installed"][name] = {"name": name, "type": "agent", "path": f"agents/{name}",
                                             "description": desc, "version": 1, "members": skills}

    def add_script(self, name, desc, entrypoint="run.py"):
        d = self.root / "scripts" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / entrypoint).write_text("print('hi')\n")
        self.registry["installed"][name] = {"name": name, "type": "script", "path": f"scripts/{name}",
                                             "entrypoint": entrypoint, "description": desc, "version": 1}

    def add_tool(self, name, desc, entrypoint="tool.py", wraps=None):
        d = self.root / "mcp_tools" / "tools" / name
        d.mkdir(parents=True, exist_ok=True)
        spec = {"description": desc, "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}
        if wraps:
            spec["wraps"] = {"script": wraps, "argv": ["{x}"]}
        else:
            (d / entrypoint).write_text(
                "import sys, json\nprint(json.dumps({'got': json.load(sys.stdin)}))\n")
        (d / "tool.json").write_text(json.dumps(spec))
        self.registry["installed"][name] = {"name": name, "type": "tool", "path": f"mcp_tools/tools/{name}",
                                             "entrypoint": None if wraps else entrypoint, "description": desc,
                                             "version": 1}

    def add_workflow(self, name, desc, steps):
        d = self.root / "workflows" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n"
                                     + "\n".join(f"{i}. **{s}**." for i, s in enumerate(steps, 1)) + "\n")
        self.registry["installed"][name] = {"name": name, "type": "workflow", "path": f"workflows/{name}",
                                             "description": desc, "version": 1, "steps": steps}

    def test_list_shows_nothing_when_empty(self):
        self.save_registry()
        from contribute import export
        out = io.StringIO()
        with redirect_stdout(out):
            export.main(["list"])
        self.assertIn("nothing installed", out.getvalue())

    def test_list_groups_by_type(self):
        self.add_skill("tidy-notes", "Tidy meeting notes")
        self.add_script("csv-to-json", "Convert csv to json")
        self.save_registry()
        from contribute import export
        out = io.StringIO()
        with redirect_stdout(out):
            export.main(["list"])
        text = out.getvalue()
        self.assertIn("skills:", text)
        self.assertIn("tidy-notes", text)
        self.assertIn("scripts:", text)
        self.assertIn("csv-to-json", text)

    def test_export_skill_strips_evals_and_writes_a_plugin(self):
        self.add_skill("tidy-notes", "Tidy meeting notes")
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        result = export.export(["tidy-notes"], dest, name="my-plugin")
        self.assertEqual(result["added"], ["skill tidy-notes"])
        self.assertTrue((dest / "skills" / "tidy-notes" / "SKILL.md").is_file())
        self.assertFalse((dest / "skills" / "tidy-notes" / "evals").exists())
        manifest = json.loads((dest / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "my-plugin")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertTrue((dest / ".git").is_dir())
        log = subprocess.run(["git", "-C", str(dest), "log", "-1", "--format=%s"], capture_output=True,
                              text=True, check=True).stdout
        self.assertIn("export: skill tidy-notes", log)
        self.assertIn("tidy-notes", (dest / "README.md").read_text())

    def test_export_agent_pulls_in_its_skills(self):
        self.add_skill("alpha", "Alpha skill")
        self.add_skill("beta", "Beta skill")
        self.add_agent("devops", "DevOps agent", ["alpha", "beta"])
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        result = export.export(["devops"], dest, name="plug")
        self.assertIn("agent devops", result["added"])
        self.assertIn("skill alpha", result["added"])
        self.assertIn("skill beta", result["added"])
        self.assertTrue((dest / "agents" / "devops.md").is_file())
        self.assertTrue(any("needs skill" in n for n in result["notes"]))

    def test_export_workflow_pulls_in_every_step_and_lands_in_skills(self):
        self.add_skill("implement-feature", "Implement a feature")
        self.add_script("run-tests", "Run the test suite")
        self.add_tool("cut-release", "Cut a release")
        self.add_workflow("release-pipeline", "Use when shipping a change end to end.",
                          ["implement-feature", "run-tests", "cut-release"])
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        result = export.export(["release-pipeline"], dest, name="plug")
        self.assertIn("workflow release-pipeline", result["added"])
        self.assertIn("skill implement-feature", result["added"])
        self.assertIn("script run-tests", result["added"])
        self.assertIn("tool cut-release", result["added"])
        self.assertTrue(any("also needs step" in n for n in result["notes"]))
        self.assertTrue((dest / "skills" / "release-pipeline" / "SKILL.md").is_file())
        self.assertTrue((dest / "skills" / "implement-feature" / "SKILL.md").is_file())
        self.assertTrue((dest / "scripts" / "run-tests").is_dir())
        self.assertTrue((dest / "mcp_tools" / "cut-release" / "server.py").is_file())

    def test_export_script_becomes_a_slash_command(self):
        self.add_script("csv-to-json", "Convert csv to json", "convert.py")
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        export.export(["csv-to-json"], dest, name="plug")
        self.assertTrue((dest / "scripts" / "csv-to-json" / "convert.py").is_file())
        cmd = (dest / "commands" / "csv-to-json.md").read_text()
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/csv-to-json/convert.py", cmd)
        self.assertIn("$ARGUMENTS", cmd)

    def test_wrapping_tool_exports_the_wrapped_script_instead(self):
        self.add_script("csv-to-json", "Convert csv to json", "convert.py")
        self.add_tool("csv-to-json-tool", "Wraps the script", wraps="csv-to-json")
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        result = export.export(["csv-to-json-tool"], dest, name="plug")
        self.assertEqual(result["added"], ["script csv-to-json"])
        self.assertTrue(any("just wraps script" in n for n in result["notes"]))
        self.assertFalse((dest / "mcp_tools").exists())

    def test_generated_tool_becomes_a_working_standalone_mcp_server(self):
        self.add_tool("word-count", "Count words", entrypoint="tool.py")
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        export.export(["word-count"], dest, name="plug")
        server = dest / "mcp_tools" / "word-count" / "server.py"
        self.assertTrue(server.is_file())
        compile(server.read_text(encoding="utf-8"), str(server), "exec")  # must be valid Python
        mcp = json.loads((dest / ".mcp.json").read_text())
        self.assertEqual(mcp["mcpServers"]["plug-word-count"]["args"],
                          ["${CLAUDE_PLUGIN_ROOT}/mcp_tools/word-count/server.py"])
        proc = subprocess.run(
            [sys.executable, str(server)],
            input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
                  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
                  '{"name":"word-count","arguments":{"x":"hi"}}}\n',
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(lines[0]["result"]["tools"][0]["name"], "word-count")
        self.assertFalse(lines[1]["result"]["isError"], lines[1])
        self.assertIn("hi", lines[1]["result"]["content"][0]["text"])

    def test_re_export_bumps_patch_version_and_commits_again(self):
        self.add_skill("a", "A skill")
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        r1 = export.export(["a"], dest, name="plug")
        self.assertEqual(json.loads((dest / ".claude-plugin" / "plugin.json").read_text())["version"], "0.1.0")
        self.add_skill("b", "B skill")
        self.save_registry()
        r2 = export.export(["a", "b"], dest, name="plug")
        self.assertEqual(json.loads((dest / ".claude-plugin" / "plugin.json").read_text())["version"], "0.1.1")
        self.assertIsNotNone(r2["commit"])
        self.assertNotEqual(r1["commit"], r2["commit"])

    def test_re_export_with_nothing_new_makes_no_commit(self):
        self.add_skill("a", "A skill")
        self.save_registry()
        from contribute import export
        dest = self.t / "plugin"
        export.export(["a"], dest, name="plug")
        r2 = export.export(["a"], dest, name="plug", version="0.1.0")  # pin the version: no manifest diff either
        self.assertIsNone(r2["commit"])

    def test_unknown_name_is_rejected(self):
        self.save_registry()
        from contribute import export
        with self.assertRaises(export.ExportError):
            export.export(["does-not-exist"], self.t / "plugin")

    def test_bad_plugin_name_is_rejected(self):
        self.add_skill("a", "A skill")
        self.save_registry()
        from contribute import export
        with self.assertRaises(export.ExportError):
            export.export(["a"], self.t / "plugin", name="Not Kebab Case")


if __name__ == "__main__":
    unittest.main()
