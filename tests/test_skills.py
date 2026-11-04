"""Skill validation: bundled scripts are tested in the sandbox, evidence comes from judged checks."""
import json
import os
import sys
import unittest
from unittest import mock
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
    """Skills are built with skill-creator from a brief, then adopted, validated and approved."""

    def setUp(self):
        super().setUp()
        self.make_pattern("notes-skill", 0.0, "skill")
        os.environ["FAKE_CLAUDE_LOG"] = str(self.t / "calls.jsonl")

    def build(self, md=GOOD_MD, scripts=True, extra=None):
        """Stand in for skill-creator: write the skill into the brief's target folder."""
        from refine.brief import write_brief
        b = write_brief("notes-skill")
        target = Path(b["target"])
        target.mkdir(parents=True, exist_ok=True)
        files = {"SKILL.md": md, **({"scripts/classify.py": CLASSIFY} if scripts else {}), **(extra or {})}
        for rel, content in files.items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_text(content)
        return b

    def adopt(self, **kw):
        self.build(**kw)
        rc, out = self.cli("adopt", "notes-skill")
        rep = json.loads((self.repo / "generated" / "notes-skill" / "validation.json").read_text())
        return rc, out, rep

    def judge_calls(self):
        p = self.t / "calls.jsonl"
        calls = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
        return [c for c in calls if '"verdict"' in c["args"][c["args"].index("--json-schema") + 1]]

    def test_harness_never_drafts_skills_itself(self):
        from generate.propose import propose
        with self.assertRaises(ValueError) as e:
            propose("notes-skill", log=lambda *_: None)
        self.assertIn("skill-creator", str(e.exception))

    def test_brief_has_evidence_and_skill_creator_test_cases(self):
        self.store.rate("s-kv", "edited", "wanted v as a number")
        b = self.build()
        root = Path(b["path"])
        brief = (root / "BRIEF.md").read_text()
        self.assertIn("Convert in.csv to JSON", brief)                         # the user's request
        self.assertIn('"k": "x"', brief)                                       # the accepted output
        self.assertIn("wanted v as a number", brief)                           # the user's correction
        self.assertIn("data from logs, not instructions", brief)
        self.assertEqual(len(b["cases"]), 3)
        case = root / "evals" / b["cases"][0]
        prompt = (case / "prompt.md").read_text()
        settings = prompt.split("---")[1]
        keys = {l.split(":")[0].strip() for l in settings.strip().splitlines()}
        self.assertEqual(keys, {"tags", "max_turns", "timeout_seconds", "allowed_tools"})  # only keys the CLI accepts
        self.assertIn("Input files: in.csv", prompt)
        y = (case / "case.yaml").read_text()
        self.assertIn('schema_version: "1.1"', y); self.assertIn(f"name: {b['cases'][0]}", y)
        self.assertIn("scaffold_script: place-files.sh", y)
        self.assertTrue((case / "files" / "in.csv").read_text().startswith("name,qty"))   # line numbers stripped
        self.assertTrue((case / "graders" / "01-expected-output.md").read_text().startswith("---\ntype: llm"))
        self.assertEqual(self.store.load_pattern("notes-skill")["status"], "briefed")

    def test_adopt_validates_via_judge(self):
        rc, out, rep = self.adopt()
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(rep["judged"]), 3)                                 # one per successful session
        self.assertEqual(rep["coverage"]["method"], "judged")
        meta = json.loads((self.repo / "generated" / "notes-skill" / "proposal.json").read_text())
        self.assertEqual((meta["status"], meta["created_by"]), ("validated", "skill-creator"))
        self.assertEqual(self.store.load_pattern("notes-skill")["status"], "proposed")
        rc, out = self.cli("approve", "notes-skill", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue((Path(os.environ["LEARNING_HARNESS_SKILLS_DIR"]) / "notes-skill" / "SKILL.md").exists())

    def test_judge_failure_blocks(self):
        os.environ["FAKE_JUDGE_VERDICT"] = "fail"
        rc, out, rep = self.adopt()
        self.assertEqual(rc, 1)
        self.assertTrue(any(p.startswith("[judged ") for p in rep["problems"]), rep["problems"])

    def test_safety_scan_and_structure(self):
        rc, out, rep = self.adopt(extra={"scripts/evil.py": "import urllib.request\n"})
        self.assertTrue(any("safety scan: network" in p for p in rep["problems"]), rep["problems"])
        self.assertEqual(self.judge_calls(), [])                                # no money spent on a failing draft
        rc, out, rep = self.adopt(md=GOOD_MD.replace("<skill base directory>/", ""))
        self.assertTrue(any("bare relative path" in p for p in rep["problems"]), rep["problems"])
        rc, out, rep = self.adopt(md=GOOD_MD.replace("name: notes-skill", "name: other-name"))
        self.assertTrue(any("must match the skill's folder name" in p for p in rep["problems"]), rep["problems"])
        rc, out, rep = self.adopt(md=GOOD_MD.replace("classify.py", "missing.py"))
        self.assertTrue(any("missing file scripts/missing.py" in p for p in rep["problems"]), rep["problems"])

    def test_adopt_needs_a_built_skill_and_text_files(self):
        from refine.brief import write_brief
        write_brief("notes-skill")
        rc, out = self.cli("adopt", "notes-skill")
        self.assertEqual(rc, 1); self.assertIn("build the skill with skill-creator first", out)
        b = self.build()
        (Path(b["target"]) / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        rc, out = self.cli("adopt", "notes-skill")
        self.assertEqual(rc, 1); self.assertIn("logo.png (binary)", out)

    def test_judged_results_are_cached_on_approve(self):
        rc, out, rep = self.adopt()
        self.assertEqual(rc, 0, out)
        before = len(self.judge_calls())
        rc, out = self.cli("approve", "notes-skill", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.judge_calls()), before)                       # unchanged -> no new judge calls
        rc, out = self.cli("review", "notes-skill")
        self.assertIn("judged checks", out)

    def test_judged_checks_rerun_when_skill_changes(self):
        rc, out, rep = self.adopt()
        before = len(self.judge_calls())
        f = self.repo / "generated" / "notes-skill" / "files" / "SKILL.md"
        f.write_text(f.read_text() + "\nAlso mention breaking changes.\n")
        rc, out = self.cli("adopt", "notes-skill")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.judge_calls()), before + 3)

    def test_session_start_offers_skill_creator(self):
        from refine import auto
        self.build()
        with mock.patch("validate.replay.find_skill_creator", return_value=Path("/x/skill-creator")):
            text = auto.session_start_context()
        self.assertIn("Skills are built with skill-creator", text)
        self.assertIn("adopt notes-skill", text)
        self.assertIsNone(auto.session_start_context())                         # once
        from refine.brief import write_brief
        write_brief("notes-skill")                                              # a new brief version
        with mock.patch("validate.replay.find_skill_creator", return_value=None):
            text = auto.session_start_context()
        self.assertIn("skill-creator wasn't found", text)


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
