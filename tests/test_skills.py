"""Skill validation: bundled scripts are tested in the sandbox, evidence comes from judged checks."""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests import test_phase4 as p4  # noqa: E402

CLASSIFY = ("import sys, json\nlines = [l.strip() for l in open(sys.argv[1]) if l.strip()]\n"
            "print(json.dumps({'fixes': [l for l in lines if 'fix' in l.lower()],"
            " 'other': [l for l in lines if 'fix' not in l.lower()]}))\n")
GOOD_MD = ("---\nname: notes-skill\ndescription: Write user-facing notes\n---\n\n"
           "Run `python3 <skill base directory>/scripts/classify.py commits.txt` (the base directory is shown "
           "when the skill loads), then write the notes.\n")


def helper_test(expected='{"fixes": ["fix: a"], "other": ["feat: b"]}', script="scripts/classify.py"):
    return {"name": "classify", "source_session": "s-fruit", "provenance": "synthetic", "rationale": "",
            "args": ["commits.txt"], "stdin": "", "input_files": [{"path": "commits.txt", "content": "fix: a\nfeat: b\n"}],
            "expected_exit": 0, "expected_stdout": expected, "stdout_compare": "json", "expected_files": [],
            "script": script}


def skill(md=GOOD_MD, tests=None, scripts=True):
    files = [{"path": "SKILL.md", "content": md}] + ([{"path": "scripts/classify.py", "content": CLASSIFY}] if scripts else [])
    return {"type": "skill", "type_deviation_reason": "", "name": "notes-skill", "description": "notes", "spec": "s",
            "entrypoint": "", "tool_input_schema_json": "", "files": files,
            "tests": [helper_test()] if tests is None else tests}


class Skills(p4.Phase4):
    def setUp(self):
        super().setUp()
        self.make_pattern("notes", 0.0, "skill")
        os.environ["FAKE_CLAUDE_LOG"] = str(self.t / "calls.jsonl")

    def propose(self, prop):
        p = self.t / "p.json"; p.write_text(json.dumps(prop)); os.environ["FAKE_PROPOSAL_FILE"] = str(p)
        from generate.propose import propose
        return propose("notes", log=lambda *_: None, max_attempts=1)

    def judge_calls(self):
        p = self.t / "calls.jsonl"
        calls = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
        return [c for c in calls if '"verdict"' in c["args"][c["args"].index("--json-schema") + 1]]

    def test_skill_with_helper_validates_via_judge(self):
        r = self.propose(skill())
        self.assertTrue(r["passed"], r["problems"])
        rep = r["report"]
        self.assertEqual([t["passed"] for t in rep["tests"]], [True])            # helper ran in the sandbox
        self.assertEqual(len(rep["judged"]), 3)                                   # one per successful session
        self.assertEqual(rep["coverage"]["method"], "judged")
        self.assertEqual(len(self.judge_calls()), 3)

    def test_judge_failure_blocks(self):
        os.environ["FAKE_JUDGE_VERDICT"] = "fail"
        r = self.propose(skill())
        self.assertFalse(r["passed"])
        self.assertTrue(any(p.startswith("[judged ") for p in r["problems"]), r["problems"])
        self.assertEqual(self.store.load_pattern("notes")["status"], "candidate")

    def test_broken_helper_fails_before_any_judge_call(self):
        r = self.propose(skill(tests=[helper_test(expected='{"fixes": []}')]))
        self.assertTrue(any("[classify] failed" in p for p in r["problems"]), r["problems"])
        self.assertEqual(self.judge_calls(), [])                                  # no money spent on a broken draft

    def test_structure_rules(self):
        r = self.propose(skill(md=GOOD_MD.replace("<skill base directory>/", "")))
        self.assertTrue(any("bare relative path" in p for p in r["problems"]), r["problems"])
        r = self.propose(skill(md=GOOD_MD.replace("classify.py", "missing.py"), tests=[]))
        self.assertTrue(any("references missing file scripts/missing.py" in p for p in r["problems"]), r["problems"])
        r = self.propose(skill(tests=[]))
        self.assertTrue(any("bundles scripts but has no tests" in p for p in r["problems"]), r["problems"])
        r = self.propose(skill(tests=[helper_test(script="")], scripts=True) | {"files": [
            {"path": "SKILL.md", "content": GOOD_MD}, {"path": "scripts/classify.py", "content": CLASSIFY},
            {"path": "scripts/other.py", "content": "print(1)\n"}]})
        self.assertTrue(any("names no bundled script" in p for p in r["problems"]), r["problems"])

    def test_skill_without_scripts_needs_no_tests(self):
        md = "---\nname: notes-skill\ndescription: Write notes\n---\n\nGroup changes; lead with security.\n"
        r = self.propose(skill(md=md, tests=[], scripts=False))
        self.assertTrue(r["passed"], r["problems"])

    def test_judged_results_are_cached_on_approve(self):
        self.assertTrue(self.propose(skill())["passed"])
        before = len(self.judge_calls())
        rc, out = self.cli("approve", "notes-skill", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.judge_calls()), before)                         # unchanged -> no new judge calls
        rc, out = self.cli("review", "notes-skill")
        self.assertIn("judged checks", out)

    def test_judged_checks_rerun_when_skill_changes(self):
        self.assertTrue(self.propose(skill())["passed"])
        before = len(self.judge_calls())
        f = self.repo / "generated" / "notes-skill" / "files" / "SKILL.md"
        f.write_text(f.read_text() + "\nAlso mention breaking changes.\n")
        rc, out = self.cli("revalidate", "notes-skill")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.judge_calls()), before + 3)


for _n in [n for n in vars(p4.Phase4) if n.startswith("test_")]:
    setattr(Skills, _n, None)


if __name__ == "__main__":
    unittest.main()


class SkillStandard(unittest.TestCase):
    """The Claude skill standard, as checked by skill-creator's quick_validate.py."""

    def probs(self, fm, body="Do the thing.\n", name="notes-skill", extra_files=None):
        from validate.replay import skill_standard_problems
        files = {"SKILL.md": f"---\n{fm}\n---\n\n{body}", **(extra_files or {})}
        return skill_standard_problems(files, expected_name=name)

    def test_valid(self):
        self.assertEqual(self.probs("name: notes-skill\ndescription: Write notes. Use when asked for notes."), [])

    def test_rules(self):
        cases = {
            "name: Notes_Skill\ndescription: d": "kebab-case",
            "name: -notes\ndescription: d": "start or end with a hyphen",
            "name: notes--skill\ndescription: d": "contain '--'",
            f"name: {'a' * 65}\ndescription: d": "maximum is 64",
            "name: notes-skill\ndescription: use <this>": "angle brackets",
            f"name: notes-skill\ndescription: {'x' * 1025}": "maximum is 1024",
            "name: notes-skill\ndescription: d\nversion: 2": "unexpected frontmatter key(s): version",
            "description: d": "missing 'name'",
            "name: notes-skill": "missing 'description'",
            "name: other-name\ndescription: d": "must match the skill's folder name",
            f"name: notes-skill\ndescription: d\ncompatibility: {'c' * 501}": "compatibility",
        }
        for fm, want in cases.items():
            with self.subTest(fm=fm[:40]):
                self.assertTrue(any(want in p for p in self.probs(fm, name="notes-skill")), (fm, self.probs(fm)))
        self.assertTrue(any("under 500" in p for p in self.probs("name: notes-skill\ndescription: d", body="x\n" * 600)))
        self.assertTrue(any("exactly one SKILL.md" in p for p in self.probs(
            "name: notes-skill\ndescription: d", extra_files={"references/SKILL.md": "x"})))
        self.assertEqual(self.probs("name: notes-skill\ndescription: d", extra_files={"evals/case/SKILL.md": "x"}), [])
        from validate.replay import skill_standard_problems
        self.assertIn("no YAML frontmatter", skill_standard_problems({"SKILL.md": "# no frontmatter"})[0])

    def test_official_checker_agrees_when_installed(self):
        from validate.replay import find_skill_creator, official_skill_check
        if find_skill_creator() is None:
            self.skipTest("skill-creator isn't installed here")
        good = official_skill_check({"SKILL.md": "---\nname: notes-skill\ndescription: Write notes.\n---\n\nBody.\n"})
        self.assertTrue(good["ran"] and good["valid"], good)
        bad = official_skill_check({"SKILL.md": "---\nname: notes-skill\ndescription: use <this>\n---\n\nBody.\n"})
        self.assertTrue(bad["ran"] and not bad["valid"], bad)
        self.assertIn("angle brackets", bad["message"])

    def test_builtin_learning_harness_skill_meets_the_standard(self):
        import install
        from validate.replay import official_skill_check, skill_standard_problems
        files = {"SKILL.md": install.builtin_skill_text()}
        self.assertEqual(skill_standard_problems(files, expected_name="learning-harness"), [])
        r = official_skill_check(files)
        if r["ran"]:
            self.assertTrue(r["valid"], r)
