import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_phase3 import GOOD_SCRIPT, proposal  # noqa: E402
from tests import test_phase4 as p4  # noqa: E402  (module import: a bare class import would get its tests re-discovered here)
git = p4.git


def bash_session(path, sid, command, is_error, ts):
    L = [{"type": "user", "sessionId": sid, "timestamp": ts, "message": {"role": "user", "content": "convert data.csv"}},
         {"type": "assistant", "sessionId": sid, "timestamp": ts, "message": {"model": "m", "content": [
             {"type": "tool_use", "id": f"{sid}1", "name": "Bash", "input": {"command": command}}]}},
         {"type": "user", "sessionId": sid, "timestamp": ts, "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": f"{sid}1", "is_error": is_error,
              "content": "error: row 3 has more cells than headers" if is_error else "Wrote 2 rows"}]}}]
    Path(path).write_text("\n".join(json.dumps(x) for x in L) + "\n")
    return str(path)


class Phase5(p4.Phase4):
    """Reuses Phase 4's throwaway git repo + store; installs csv-to-json, then exercises refinement."""

    def setUp(self):
        super().setUp()
        self.draft(proposal())
        rc, out = self.cli("approve", "csv-to-json", "--yes")
        self.assertEqual(rc, 0, out)
        self.later = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")

    def shim(self, *args, env=None):
        (self.t / "in.csv").write_text("a,b\n1,x\n")
        e = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")}
        e.update(env or {})
        return subprocess.run([str(self.repo / "bin" / "csv-to-json"), *args], env=e, capture_output=True, text=True,
                              cwd=self.t).returncode

    def add_session(self, sid, command, is_error, rating=None, note=None, in_pattern=True):
        from hooks.log_session import ingest
        ingest(bash_session(self.t / f"{sid}.jsonl", sid, command, is_error, self.later), hook_event="SessionEnd",
               end_reason="other", session_id=sid)
        if rating:
            self.store.rate(sid, rating, note)
        if in_pattern:
            p = self.store.load_pattern("csv-to-json")
            p["session_ids"] = sorted(set(p["session_ids"]) | {sid})
            self.store.save_pattern(p)

    def stats(self):
        from refine import refine
        from registry import install
        return refine.stats(install.load_registry()["installed"]["csv-to-json"])

    def test_wrapper_logs_runs_and_preserves_exit_code(self):
        self.assertTrue(os.access(self.repo / "bin" / "csv-to-json", os.X_OK))
        self.assertIn("bin/csv-to-json", git(self.repo, "show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(self.shim("in.csv", "out.json"), 0)
        self.assertEqual(json.loads((self.t / "out.json").read_text()), [{"a": 1, "b": "x"}])
        self.assertEqual(self.shim("missing.csv", "out.json"), 2)  # script's own exit code passes through
        self.assertEqual(self.shim("in.csv", "o2.json", env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sess-z"}), 0)
        log = self.store.read_jsonl(self.store.store_dir() / "usage" / "csv-to-json.jsonl")
        self.assertEqual([(r["exit"], r["via"], r["session_id"]) for r in log],
                         [(0, "terminal", None), (2, "terminal", None), (0, "claude", "sess-z")])
        self.assertNotIn("in.csv", json.dumps(log))            # arguments are never logged
        st = self.stats()
        self.assertEqual((st["uses"], st["failures"]), (3, 1))

    def test_transcript_uses_redone_and_no_double_count(self):
        self.add_session("u-ok", "bin/csv-to-json data.csv data.json", False, "accepted")
        self.add_session("u-err", "python3 scripts/csv-to-json/csv_to_json.py bad.csv o.json", True)
        self.add_session("u-redo", "bin/csv-to-json semi.csv o.json", False, "redone", "it ignored the ; delimiter")
        # the wrapper also logged u-ok's run under its Claude session -> must count once
        self.shim("in.csv", "x.json", env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "u-ok"})
        st = self.stats()
        self.assertEqual(st["uses"], 3)
        self.assertEqual(st["failures"], 2)                    # u-err (tool error), u-redo (rated redone)
        self.assertEqual(st["missed"], [])

    def test_missed_opportunity(self):
        self.add_session("m-1", "python3 -c 'import csv,json; ...'", False, "accepted")
        st = self.stats()
        self.assertEqual(st["missed"], ["m-1"])
        from refine import refine
        rec = refine.recommend(st, now=datetime.now(timezone.utc), stale_days=60, min_failures=2, fail_rate=0.3)
        self.assertEqual(rec["action"], "missed")
        self.assertIn("Claude may not know it exists", rec["reasons"][0])

    def test_report_revise_retire_reconsider(self):
        from refine import refine
        self.assertEqual(refine.build_report()["installed"][0]["action"], "keep")
        future = datetime.now(timezone.utc) + timedelta(days=90)
        self.assertEqual(refine.build_report(now=future)["installed"][0]["action"], "retire")
        self.shim("missing.csv", "o.json"); self.shim("missing2.csv", "o.json"); self.shim("in.csv", "o.json")
        rep = refine.build_report()
        self.assertEqual(rep["installed"][0]["action"], "revise")
        self.assertTrue((self.store.store_dir() / "refine_report.json").exists())
        self.store.save_pattern({"key": "old-idea", "label": "x", "description": "", "status": "rejected", "n_sessions": 6,
                                 "n_success": 6, "deterministic_frac": 1, "suggested_type": "script", "type_rationale": "",
                                 "session_ids": [], "rejected": {"reason": "meh", "n_success_at_rejection": 3}})
        self.assertEqual(refine.build_report()["rejected"][0]["pattern"], "old-idea")

    def test_revise_keeps_regression_tests_and_installs_v2(self):
        from refine import refine
        from registry import install
        entry = install.load_registry()["installed"]["csv-to-json"]
        self.assertEqual(refine.revision_input(entry)["failures"], [])
        self.add_session("r-1", "bin/csv-to-json semi.csv o.json", False, "redone", "semicolon CSV not handled")
        rev = refine.revision_input(install.load_registry()["installed"]["csv-to-json"])
        self.assertEqual(len(rev["failures"]), 1)
        self.assertEqual(rev["failures"][0]["user_note"], "semicolon CSV not handled")
        # model returns only one new test and tries to weaken an old one by reusing its name
        v2 = proposal(script=GOOD_SCRIPT.replace(
            "rows = [{k: conv(v) for k, v in r.items()} for r in csv.DictReader(fh)]",
            "delim = ';' if ';' in fh.readline() else ','\n            fh.seek(0)\n"
            "            rows = [{k: conv(v) for k, v in r.items()} for r in csv.DictReader(fh, delimiter=delim)]"))
        weakened = dict(v2["tests"][0], name="regression: t-s-fruit", expected_files=[])
        v2["tests"] = [weakened]
        v2["name"] = "something-else"
        p = self.t / "p.json"; p.write_text(json.dumps(v2)); os.environ["FAKE_PROPOSAL_FILE"] = str(p)
        from generate.propose import propose
        r = propose("csv-to-json", revision=rev, max_attempts=1, log=lambda *_: None)
        self.assertTrue(r["passed"], r["problems"])
        self.assertTrue(r["draft"].endswith("csv-to-json-v2"))
        meta = json.loads((Path(r["draft"]) / "proposal.json").read_text())
        self.assertEqual(meta["name"], "csv-to-json")                    # name kept
        old = {t["name"]: t for t in rev["previous_tests"]}
        names = [t["name"] for t in meta["tests"]]
        self.assertEqual(sorted(names), sorted(f"regression: {n}" for n in old))
        fruit = next(t for t in meta["tests"] if t["name"] == "regression: t-s-fruit")
        self.assertEqual(fruit["expected_files"], old["t-s-fruit"]["expected_files"])   # not weakened
        self.assertEqual(self.store.load_pattern("csv-to-json")["status"], "installed")
        rc, out = self.cli("approve", "csv-to-json-v2", "--yes")
        self.assertEqual(rc, 0, out)
        e = install.load_registry()["installed"]["csv-to-json"]
        self.assertEqual(e["version"], 2)
        self.assertIn("v2", (self.repo / "bin" / "csv-to-json").read_text())
        self.assertNotIn("pending_revision", self.store.load_pattern("csv-to-json"))
        self.assertIn("learned: update script csv-to-json v2", git(self.repo, "log", "-1", "--format=%s"))
        self.assertIn("delimiter=delim", (self.repo / "scripts" / "csv-to-json" / "csv_to_json.py").read_text())


# Phase 4's tests are inherited through the base class; they already run in test_phase4.
for _n in [n for n in vars(p4.Phase4) if n.startswith("test_")]:
    setattr(Phase5, _n, None)


if __name__ == "__main__":
    unittest.main()
