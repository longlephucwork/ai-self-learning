import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_phase3 import SESSIONS, proposal, transcript  # noqa: E402

SKILL_MD = "---\nname: tidy-notes\ndescription: Tidy meeting notes into decisions and actions\n---\n\nRead the notes; group into Decisions / Actions.\n"


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout


class Phase4(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        repo = self.repo = t / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".gitignore").write_text("store/\n")
        git(repo, "add", ".gitignore")
        os.environ.update(
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
            LEARNING_HARNESS_STORE=str(repo / "store"), LEARNING_HARNESS_PATTERNS=str(repo / "patterns"),
            LEARNING_HARNESS_GENERATED=str(repo / "generated"), LEARNING_HARNESS_INSTALL_ROOT=str(repo),
            LEARNING_HARNESS_SKILLS_DIR=str(t / "home" / ".claude" / "skills"),
            LEARNING_HARNESS_CLAUDE_CMD=f"{sys.executable} {ROOT / 'tests' / 'fake_claude.py'}")
        git(repo, "commit", "-qm", "init")
        from hooks.log_session import ingest
        from harness_lib import store
        self.store = store
        for sid, (c, j, rating, note) in SESSIONS.items():
            ingest(transcript(t / f"{sid}.jsonl", sid, c, j), hook_event="SessionEnd", end_reason="other", session_id=sid)
            store.rate(sid, rating, note)
        self.make_pattern("csv-to-json", 1.0, "script")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._env)

    def make_pattern(self, key, det, stype):
        self.store.save_pattern({"key": key, "label": key, "description": "d", "status": "candidate", "n_sessions": 3,
                                 "n_success": 3, "success_outcomes": ["accepted", "edited"], "deterministic_frac": det,
                                 "suggested_type": stype, "type_rationale": "r", "session_ids": sorted(SESSIONS),
                                 "excluded_injection": []})

    def draft(self, prop, key="csv-to-json"):
        p = self.t / "p.json"; p.write_text(json.dumps(prop)); os.environ["FAKE_PROPOSAL_FILE"] = str(p)
        from generate.propose import propose
        r = propose(key, log=lambda *_: None, max_attempts=1)
        self.assertTrue(r["passed"], r["problems"])
        return r

    def cli(self, *argv, stdin_tty=False, typed=None):
        from review import cli
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", out), \
                mock.patch("sys.stdin.isatty", return_value=stdin_tty), \
                mock.patch("builtins.input", return_value=typed or ""):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def installed(self):
        from registry import install
        return install.load_registry()["installed"]

    def test_approval_requires_explicit_confirmation(self):
        self.draft(proposal())
        rc, out = self.cli("approve", "csv-to-json")                       # no tty, no --yes
        self.assertEqual(rc, 1); self.assertIn("needs an interactive terminal", out)
        rc, out = self.cli("approve", "csv-to-json", stdin_tty=True, typed="yes")   # wrong word typed
        self.assertEqual(rc, 1); self.assertIn("cancelled", out)
        self.assertEqual(self.installed(), {})
        self.assertFalse((self.repo / "scripts").exists())

    def test_approve_installs_and_commits_only_its_paths(self):
        self.draft(proposal())
        (self.repo / "unrelated.txt").write_text("my work in progress")
        rc, out = self.cli("approve", "csv-to-json", "--note", "looks good", stdin_tty=True, typed="csv-to-json")
        self.assertEqual(rc, 0, out)
        script = self.repo / "scripts" / "csv-to-json" / "csv_to_json.py"
        self.assertTrue(os.access(script, os.X_OK))
        e = self.installed()["csv-to-json"]
        self.assertEqual((e["version"], e["tests_passed"], e["approval_note"]), (1, 3, "looks good"))
        self.assertEqual(self.store.load_pattern("csv-to-json")["status"], "installed")
        log = git(self.repo, "log", "-1", "--format=%B")
        self.assertIn("learned: install script csv-to-json v1", log)
        self.assertIn("seen 3 times", log)
        committed = git(self.repo, "show", "--name-only", "--format=", "HEAD").split()
        self.assertIn("scripts/csv-to-json/csv_to_json.py", committed)
        self.assertIn("registry.json", committed)
        self.assertNotIn("unrelated.txt", committed)
        self.assertIn("unrelated.txt", git(self.repo, "status", "--porcelain"))
        # the installed script works for real
        (self.t / "in.csv").write_text("a,b\n1,x\n")
        subprocess.run([sys.executable, str(script), str(self.t / "in.csv"), str(self.t / "o.json")], check=True,
                       capture_output=True)
        self.assertEqual(json.loads((self.t / "o.json").read_text()), [{"a": 1, "b": "x"}])
        rc, _ = self.cli("approve", "csv-to-json", "--yes")
        self.assertEqual(rc, 1)                                             # already installed

    def test_tampering_after_validation_blocks_install(self):
        r = self.draft(proposal())
        f = self.repo / "generated" / "csv-to-json" / "files" / "csv_to_json.py"
        f.write_text("import socket\n" + f.read_text())
        rc, out = self.cli("approve", "csv-to-json", "--yes")
        self.assertEqual(rc, 1); self.assertIn("safety scan: network", out)
        f.write_text(f.read_text().replace("import socket\n", "").replace("return t(v)", "return v"))  # wrong output
        rc, out = self.cli("approve", "csv-to-json", "--yes")
        self.assertEqual(rc, 1); self.assertIn("validation failed", out)
        self.assertEqual(self.installed(), {})

    def test_reject_is_remembered(self):
        self.draft(proposal())
        rc, out = self.cli("reject", "csv-to-json", "--reason", "I use pandas for this")
        self.assertEqual(rc, 0, out)
        p = self.store.load_pattern("csv-to-json")
        self.assertEqual((p["status"], p["rejected"]["reason"]), ("rejected", "I use pandas for this"))
        from generate.propose import propose
        with self.assertRaises(ValueError):
            propose("csv-to-json", log=lambda *_: None)
        from detect import cluster as cl                 # re-clustering keeps the rejection
        summ = [{"session_id": s, "task_family": "csv-to-json", "deterministic": True, "injection": False,
                 "outcome": "accepted", "intent": "", "inputs": [], "outputs": []} for s in SESSIONS]
        cl.apply_clusters(cl.group_by_family(summ), summ, method="family")
        self.assertEqual(self.store.load_pattern("csv-to-json")["status"], "rejected")
        self.assertIn("learned: reject", git(self.repo, "log", "-1", "--format=%s"))
        rc, _ = self.cli("approve", "csv-to-json", "--yes")
        self.assertEqual(rc, 1)

    def test_uninstall_reverts(self):
        self.draft(proposal())
        self.cli("approve", "csv-to-json", "--yes")
        from registry import install
        install.uninstall("csv-to-json", reason="not needed")
        self.assertFalse((self.repo / "scripts" / "csv-to-json").exists())
        self.assertEqual(self.installed(), {})
        self.assertEqual(self.store.load_pattern("csv-to-json")["status"], "retired")
        self.assertIn("learned: uninstall script csv-to-json", git(self.repo, "log", "-1", "--format=%s"))
        self.assertIn("scripts/csv-to-json/csv_to_json.py", git(self.repo, "show", "--name-only", "--format=", "HEAD"))

    def test_skill_install_symlinks_and_tool_refused(self):
        self.make_pattern("notes", 0.0, "skill")
        skill = {"type": "skill", "type_deviation_reason": "", "name": "tidy-notes", "description": "Tidy notes",
                 "spec": "s", "entrypoint": "", "tool_input_schema_json": "",
                 "files": [{"path": "SKILL.md", "content": SKILL_MD}],
                 "tests": [{"name": f"t{i}", "source_session": sid, "provenance": "derived_from_feedback" if sid == "s-kv" else "logged",
                            "rationale": "", "args": [], "stdin": "", "input_files": [], "expected_exit": 0,
                            "expected_stdout": "", "stdout_compare": "ignore", "expected_files": []}
                           for i, sid in enumerate(sorted(SESSIONS))]}
        self.draft(skill, key="notes")
        rc, out = self.cli("approve", "tidy-notes", "--yes")
        self.assertEqual(rc, 0, out)
        link = Path(os.environ["LEARNING_HARNESS_SKILLS_DIR"]) / "tidy-notes"
        self.assertTrue(link.is_symlink())
        self.assertEqual((link / "SKILL.md").read_text(), SKILL_MD)
        from registry import install
        install.uninstall("tidy-notes")
        self.assertFalse(link.exists() or link.is_symlink())
        with self.assertRaises(install.InstallError):
            install.target_dir("tool", "x")

    def test_refuses_without_git(self):
        self.draft(proposal())
        subprocess.run(["rm", "-rf", str(self.repo / ".git")], check=True)
        rc, out = self.cli("approve", "csv-to-json", "--yes")
        self.assertEqual(rc, 1); self.assertIn("not a git repository", out)


if __name__ == "__main__":
    unittest.main()
