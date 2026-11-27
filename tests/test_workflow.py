"""Workflows: ordered, gated chains mined from how installed skills/scripts/tools are actually used
(refine/workflow.py), validated (validate/replay.py) and installed like a skill (registry/install.py)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def skill_step(name, t):
    return {"tool_name": "Skill", "input_json": json.dumps({"skill": name}), "timestamp": t, "is_error": False}


def script_step(name, t):
    return {"tool_name": "Bash", "input_json": json.dumps({"command": f"bin/{name} --x"}), "timestamp": t,
            "is_error": False}


def tool_step(name, t):
    return {"tool_name": f"mcp__learned__{name}", "input_json": "{}", "timestamp": t, "is_error": False}


class Workflow(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.repo = self.t / "repo"
        self.repo.mkdir()
        os.environ.update(
            LEARNING_HARNESS_STORE=str(self.repo / "store"),
            LEARNING_HARNESS_PATTERNS=str(self.repo / "patterns"),
            LEARNING_HARNESS_GENERATED=str(self.repo / "generated"),
            LEARNING_HARNESS_INSTALL_ROOT=str(self.repo),
            LEARNING_HARNESS_SKILLS_DIR=str(self.t / "home" / ".claude" / "skills"),
            LEARNING_HARNESS_CLAUDE_CMD=json.dumps([sys.executable, str(ROOT / "tests" / "fake_claude.py")]))
        self.registry = {"format_version": 1, "installed": {}, "history": []}

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._env)

    def save_registry(self):
        from harness_lib import store
        from registry import install as reg
        store.write_json_atomic(reg.registry_path(), self.registry)

    def add(self, name, kind, desc="does a thing"):
        self.registry["installed"][name] = {"name": name, "type": kind, "path": f"{kind}/{name}",
                                             "description": desc, "version": 1, "pattern_key": f"test-{name}",
                                             "approved_at": "2020-01-01T00:00:00+00:00"}

    def write_session(self, sid, steps, started_at="2024-01-01T00:00:00+00:00"):
        from harness_lib import store
        d = store.session_dir(sid, create=True)
        store.write_json_atomic(d / "session.json", {"session_id": sid, "started_at": started_at,
                                                      "ended_at": started_at, "steps": steps, "requests": []})

    # ---------------- mining ----------------

    def test_mine_chains_finds_a_recurring_linear_order(self):
        self.add("implement-feature", "skill")
        self.add("run-tests", "script")
        self.add("cut-release", "tool")
        self.save_registry()
        for i in range(3):
            self.write_session(f"s{i}", [skill_step("implement-feature", f"2024-01-0{i+1}T00:00:01+00:00"),
                                         script_step("run-tests", f"2024-01-0{i+1}T00:00:02+00:00"),
                                         tool_step("cut-release", f"2024-01-0{i+1}T00:00:03+00:00")])
        from refine import workflow as wf
        self.assertEqual(wf.mine_chains(), [["implement-feature", "run-tests", "cut-release"]])

    def test_below_the_repeat_threshold_is_not_mined(self):
        self.add("a", "skill")
        self.add("b", "script")
        self.save_registry()
        for i in range(2):   # WORKFLOW_MIN_REPEATS is 3
            self.write_session(f"s{i}", [skill_step("a", f"2024-01-0{i+1}T00:00:01+00:00"),
                                         script_step("b", f"2024-01-0{i+1}T00:00:02+00:00")])
        from refine import workflow as wf
        self.assertEqual(wf.mine_chains(), [])

    def test_a_failed_use_does_not_count_as_a_step(self):
        self.add("a", "skill")
        self.add("b", "script")
        self.save_registry()
        for i in range(3):
            steps = [skill_step("a", f"2024-01-0{i+1}T00:00:01+00:00"),
                    script_step("b", f"2024-01-0{i+1}T00:00:02+00:00")]
            steps[1]["is_error"] = True   # b failed every time: a -> b is never real evidence
            self.write_session(f"s{i}", steps)
        from refine import workflow as wf
        self.assertEqual(wf.mine_chains(), [])

    def test_a_branch_point_truncates_the_chain_instead_of_guessing(self):
        self.add("a", "skill")
        self.add("b", "script")
        self.add("c", "tool")
        self.add("d", "tool")
        self.save_registry()
        for i in range(3):
            self.write_session(f"ab{i}", [skill_step("a", f"2024-01-0{i+1}T00:00:01+00:00"),
                                          script_step("b", f"2024-01-0{i+1}T00:00:02+00:00")])
            self.write_session(f"bc{i}", [script_step("b", f"2024-01-1{i+1}T00:00:01+00:00"),
                                          tool_step("c", f"2024-01-1{i+1}T00:00:02+00:00")])
            self.write_session(f"ad{i}", [skill_step("a", f"2024-01-2{i+1}T00:00:01+00:00"),
                                          tool_step("d", f"2024-01-2{i+1}T00:00:02+00:00")])
        from refine import workflow as wf
        # a is ambiguous (goes to both b and d): both its edges are dropped. b -> c is unambiguous and survives.
        self.assertEqual(wf.mine_chains(), [["b", "c"]])

    # ---------------- validation ----------------

    def test_validate_workflow_requires_order_and_gates(self):
        from validate import replay
        self.add("implement-feature", "skill")
        self.add("run-tests", "script")
        self.save_registry()
        good = ("---\nname: ship-it\ndescription: Use when shipping.\n---\n\n"
               f"1. **implement-feature** (skill). {wf_soft()}\n"
               f"2. **run-tests** (script: run it with `bin/run-tests ...`). {wf_hard()}\n")
        proposal = {"name": "ship-it", "type": "workflow", "steps": ["implement-feature", "run-tests"]}
        rep = replay.validate_workflow(proposal, {"SKILL.md": good})
        self.assertTrue(rep["passed"], rep["problems"])

        no_gate = good.replace(wf_hard(), "")
        rep = replay.validate_workflow(proposal, {"SKILL.md": no_gate})
        self.assertFalse(rep["passed"])
        self.assertTrue(any("gate" in p for p in rep["problems"]), rep["problems"])

        wrong_order = ("---\nname: ship-it\ndescription: Use when shipping.\n---\n\n"
                      f"1. **run-tests** (script). {wf_hard()}\n2. **implement-feature** (skill). {wf_soft()}\n")
        rep = replay.validate_workflow(proposal, {"SKILL.md": wrong_order})
        self.assertFalse(rep["passed"])
        self.assertTrue(any("order" in p for p in rep["problems"]), rep["problems"])

        proposal_missing = {"name": "ship-it", "type": "workflow", "steps": ["implement-feature", "no-such-step"]}
        rep = replay.validate_workflow(proposal_missing, {"SKILL.md": good})
        self.assertFalse(rep["passed"])
        self.assertTrue(any("no-such-step" in p for p in rep["problems"]), rep["problems"])

    def test_validate_workflow_needs_at_least_two_distinct_steps(self):
        from validate import replay
        self.save_registry()
        text = "---\nname: solo\ndescription: d\n---\n\n1. **implement-feature** (skill). x\n"
        rep = replay.validate_workflow({"name": "solo", "type": "workflow", "steps": ["implement-feature"]},
                                       {"SKILL.md": text})
        self.assertFalse(rep["passed"])

    # ---------------- draft ----------------

    def test_propose_workflows_drafts_and_validates_a_chain(self):
        self.add("implement-feature", "skill", "Implement a feature")
        self.add("run-tests", "script", "Run the test suite")
        self.add("cut-release", "tool", "Cut a release")
        self.save_registry()
        for i in range(3):
            self.write_session(f"s{i}", [skill_step("implement-feature", f"2024-01-0{i+1}T00:00:01+00:00"),
                                         script_step("run-tests", f"2024-01-0{i+1}T00:00:02+00:00"),
                                         tool_step("cut-release", f"2024-01-0{i+1}T00:00:03+00:00")])
        os.environ["FAKE_WORKFLOW_DRAFT"] = json.dumps({
            "name": "release-pipeline", "workflow_description": "Use when shipping a finished change end to end.",
            "step_notes": ["Make sure the feature is complete.", "All tests must be green.", "Tag and publish."]})
        from refine import workflow as wf
        made = wf.propose_workflows()
        self.assertEqual([(m["draft"], m["passed"]) for m in made], [("release-pipeline", True)])
        text = (self.repo / "generated" / "release-pipeline" / "files" / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: release-pipeline\n"))
        self.assertIn(wf.MANAGED, text)
        self.assertIn("implement-feature", text)
        self.assertIn("run-tests", text)
        self.assertIn("cut-release", text)
        self.assertLess(text.index("implement-feature"), text.index("run-tests"))
        self.assertLess(text.index("run-tests"), text.index("cut-release"))
        self.assertIn(wf.SOFT_GATE, text)   # after the skill step
        self.assertEqual(text.count(wf.HARD_GATE), 2)   # after the script and the tool step
        # mining again without new evidence drafts nothing new
        self.assertEqual(wf.propose_workflows(), [])

    def test_propose_workflows_skips_a_chain_whose_step_is_no_longer_installed(self):
        self.add("a", "skill")
        self.add("b", "script")
        self.save_registry()
        for i in range(3):
            self.write_session(f"s{i}", [skill_step("a", f"2024-01-0{i+1}T00:00:01+00:00"),
                                         script_step("b", f"2024-01-0{i+1}T00:00:02+00:00")])
        self.registry["installed"].pop("b")   # uninstalled after the sessions were recorded
        self.save_registry()
        from refine import workflow as wf
        self.assertEqual(wf.propose_workflows(), [])

    # ---------------- install / link ----------------

    def test_install_links_a_workflow_like_a_skill(self):
        from harness_lib import store
        from refine import workflow as wf
        from registry import install as reg
        self.add("implement-feature", "skill")
        self.add("run-tests", "script")
        self.save_registry()
        chain = ["implement-feature", "run-tests"]
        text = wf.render("ship-it", "Use when shipping.", chain,
                         {"implement-feature": {"kind": "skill"}, "run-tests": {"kind": "script"}}, ["", ""])
        proposal = {"name": "ship-it", "type": "workflow", "pattern_key": "workflow-ship-it",
                   "description": "Use when shipping.", "entrypoint": None, "steps": chain,
                   "evidence": {"sessions": []}}
        draft_dir = self.repo / "generated" / "ship-it"
        store.write_json_atomic(draft_dir / "proposal.json", proposal)
        entry = reg.install(proposal, {"SKILL.md": text}, draft_dir=draft_dir,
                            note="test", validation={"tests": []}, use_git=False)
        self.assertEqual(entry["type"], "workflow")
        self.assertEqual(entry["steps"], chain)
        dest = self.repo / "workflows" / "ship-it" / "SKILL.md"
        self.assertEqual(dest.read_text(encoding="utf-8"), text)
        link = Path(os.environ["LEARNING_HARNESS_SKILLS_DIR"]) / "ship-it"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), dest.parent.resolve())
        # a workflow linked into the skills dir is never mistaken for a hand-made skill to group
        from refine.agents import inventory
        self.assertNotIn("ship-it", {i["name"] for i in inventory()})
        reg.uninstall("ship-it", use_git=False)
        self.assertFalse(link.exists() or link.is_symlink())


def wf_soft():
    from refine.workflow import SOFT_GATE
    return SOFT_GATE


def wf_hard():
    from refine.workflow import HARD_GATE
    return HARD_GATE


if __name__ == "__main__":
    unittest.main()
