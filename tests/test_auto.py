"""Automatic learning: inferred ratings, budget, throttle, lock, background run, SessionStart note."""
import io
from contextlib import redirect_stdout
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_phase3 import SESSIONS, proposal, transcript  # noqa: E402

RULES = {"k,v": ["edited", 0.8, "wanted v as a number"]}   # the kv session reads as "user corrected it"


def submit(sid, prompt):
    from hooks.log_session import prompt_submit
    buf = io.StringIO()
    with redirect_stdout(buf):
        prompt_submit({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": prompt})
    out = buf.getvalue().strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else None


class Auto(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        os.environ.update(LEARNING_HARNESS_STORE=str(t / "store"), LEARNING_HARNESS_PATTERNS=str(t / "patterns"),
                          LEARNING_HARNESS_INSTALL_ROOT=str(t / "learned"),
                          LEARNING_HARNESS_GENERATED=str(t / "generated"),
                          LEARNING_HARNESS_CLAUDE_CMD=json.dumps([sys.executable, str(ROOT / "tests" / "fake_claude.py")]),
                          FAKE_CLAUDE_LOG=str(t / "calls.jsonl"), FAKE_RATE_RULES=json.dumps(RULES))
        for k in ("LEARNING_HARNESS_ENFORCE_BUDGET", "LEARNING_HARNESS_PURPOSE", "FAKE_RATE_CONF"):
            os.environ.pop(k, None)
        from harness_lib import store
        from refine import auto
        self.store, self.auto = store, auto
        p = t / "prop.json"; p.write_text(json.dumps(proposal())); os.environ["FAKE_PROPOSAL_FILE"] = str(p)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear(); os.environ.update(self._env)

    def add_sessions(self, names=tuple(SESSIONS)):
        from hooks.log_session import ingest
        for sid in names:
            c, j, _, _ = SESSIONS[sid]
            ingest(transcript(self.t / f"{sid}.jsonl", sid, c, j), hook_event="SessionEnd", end_reason="other",
                   session_id=sid)

    def calls(self, kind=None):
        p = self.t / "calls.jsonl"
        calls = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
        return [c for c in calls if kind is None or kind in c["args"][c["args"].index("--json-schema") + 1]]

    # ---------------- ratings ----------------

    def test_manual_rating_always_wins(self):
        self.add_sessions(["s-fruit"])
        self.store.rate("s-fruit", "redone", "wrong", source="inferred")
        self.assertEqual(self.store.outcome("s-fruit")["outcome"], "redone")
        self.store.rate("s-fruit", "accepted")
        self.store.rate("s-fruit", "edited", source="inferred")          # a later inference doesn't override you
        self.assertEqual(self.store.outcome("s-fruit")["outcome"], "accepted")

    def test_inferred_ratings(self):
        self.add_sessions()
        self.store.rate("s-price", "redone", "I did it by hand")           # manual: never re-rated
        todo = self.auto.sessions_to_rate()
        self.assertEqual(sorted(todo), ["s-fruit", "s-kv"])
        self.auto.infer_ratings(todo, min_confidence=0.6)
        o = self.store.outcome("s-kv")
        self.assertEqual((o["outcome"], o["source"], o["note"]), ("edited", "inferred", "wanted v as a number"))
        self.assertEqual(self.store.outcome("s-fruit")["outcome"], "accepted")
        self.assertEqual(self.store.outcome("s-price")["outcome"], "redone")
        self.assertEqual(self.auto.sessions_to_rate(), [])                  # nothing new -> no more calls
        os.environ["FAKE_RATE_CONF"] = "0.4"
        self.add_sessions(["s-fruit"])                                     # same content: still not re-rated
        self.assertEqual(self.auto.sessions_to_rate(), [])

    def test_low_confidence_becomes_unknown(self):
        self.add_sessions(["s-fruit"])
        os.environ["FAKE_RATE_CONF"] = "0.4"
        self.auto.infer_ratings(["s-fruit"], min_confidence=0.6)
        self.assertEqual(self.store.outcome("s-fruit")["outcome"], "unknown")

    # ---------------- budget ----------------

    def test_spend_is_recorded_and_budget_enforced(self):
        from harness_lib import claude_cli as cc
        schema = {"type": "object", "properties": {"confidence": {"type": "number"}}}
        cc.run_structured("x", schema, system="s")                         # not enforced: recorded only
        self.assertAlmostEqual(cc.month_spend(), 0.001)
        self.auto.set_config(monthly_budget_usd=0.0025)
        os.environ["LEARNING_HARNESS_ENFORCE_BUDGET"] = "1"
        cc.run_structured("x", schema, system="s")
        last = self.calls()[-1]["args"]
        self.assertLessEqual(float(last[last.index("--max-budget-usd") + 1]), 0.0015 + 1e-9)  # capped to what's left
        with self.assertRaises(cc.ClaudeError) as e:
            cc.run_structured("x", schema, system="s")
            cc.run_structured("x", schema, system="s")
        self.assertIn("budget", str(e.exception))

    # ---------------- throttle + lock ----------------

    def test_throttle(self):
        ok, why = self.auto.should_run()
        self.assertFalse(ok); self.assertIn("off", why)
        self.auto.set_config(enabled=True)
        self.add_sessions(["s-fruit", "s-price"])
        ok, why = self.auto.should_run()
        self.assertFalse(ok); self.assertIn("2 new finished", why)
        self.add_sessions(["s-kv"])
        self.assertTrue(self.auto.should_run()[0])
        self.assertTrue(self.auto._acquire())
        self.assertIn("in progress", self.auto.should_run()[1])
        self.assertFalse(self.auto._acquire())
        self.auto._release()
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.store.write_json_atomic(self.store.store_dir() / "auto_state.json", {"last_run_at": recent})
        self.assertIn("waiting 6h", self.auto.should_run()[1])
        self.auto.set_config(monthly_budget_usd=0.0)
        self.assertIn("budget", self.auto.should_run()[1])

    def test_failed_run_does_not_use_up_the_daily_window(self):
        self.auto.set_config(enabled=True)
        self.add_sessions()
        os.environ["FAKE_CLAUDE_FAIL"] = "1"
        r = self.auto.run()
        st = self.auto.state()
        self.assertIn("last_error", st)
        self.assertNotIn("last_run_at", st)
        ok, why = self.auto.should_run()
        self.assertFalse(ok); self.assertIn("the last run failed", why)
        later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
        self.assertTrue(self.auto.should_run(now=later)[0])               # retried after an hour, not 24 h
        os.environ.pop("FAKE_CLAUDE_FAIL")
        r = self.auto.run(force=True)
        self.assertNotIn("last_error", self.auto.state())
        self.assertIn("last_run_at", self.auto.state())

    def test_reads_survive_windows_file_locks(self):
        from harness_lib import store
        p = self.t / "x.json"; p.write_text('{"a": 1}')
        real, n = Path.read_text, []

        def flaky(self_, *a, **k):
            n.append(1)
            if len(n) < 3:
                raise PermissionError(13, "being used by another process")
            return real(self_, *a, **k)
        with mock.patch.object(Path, "read_text", flaky), mock.patch.object(store.os, "name", "nt"):
            self.assertEqual(store.read_json(p), {"a": 1})
        self.assertEqual(len(n), 3)

    def near_miss_setup(self, skill=True):
        """3 deploy-like sessions: one known good, two with unclear outcomes."""
        self.add_sessions()
        self.store.rate("s-fruit", "accepted")
        for sid in ("s-price", "s-kv"):
            self.store.rate(sid, "unknown", source="inferred", confidence=0.55)
        self.store.save_pattern({"key": "deploy-service", "label": "deploy service", "description": "Deploy a service",
                                 "status": "forming", "n_sessions": 3, "n_success": 1, "min_success": 3,
                                 "success_outcomes": ["accepted", "edited"], "deterministic_frac": 0.0,
                                 "suggested_type": "skill" if skill else "script", "type_rationale": "",
                                 "session_ids": sorted(SESSIONS), "excluded_injection": []})

    def test_near_miss_asks_the_user_once(self):
        self.near_miss_setup()
        nm = self.auto.near_misses()
        self.assertEqual([(n["pattern"], n["n_success"], n["need"]) for n in nm], [("deploy-service", 1, 2)])
        text = self.auto.session_start_context()
        self.assertIn("ask the user once", text)
        self.assertIn("'deploy service'", text)
        self.assertIn("confirm <pattern> --accepted", text)
        self.assertIn("s-price", text)
        self.assertIsNone(self.auto.session_start_context())                     # asked once
        self.store.rate("s-price", "unknown", source="inferred")                 # same unclear set: still quiet
        self.assertIsNone(self.auto.session_start_context())

    def test_confirm_learns_right_away(self):
        self.near_miss_setup()
        r = self.auto.confirm("deploy-service", accepted=["s-price"])
        self.assertEqual((r["status"], r["n_success"]), ("forming", 2))         # one confirmation isn't enough
        r = self.auto.confirm("deploy-service", accepted=["s-kv"])
        self.assertEqual(r["status"], "briefed")
        self.assertTrue((Path(r["brief_path"]) / "BRIEF.md").exists())
        self.assertEqual(self.store.outcome("s-kv")["source"], "manual")
        self.assertEqual(self.auto.near_misses(), [])
        text = self.auto.session_start_context()                                 # Claude now offers skill-creator
        self.assertIn("deploy-service", text)

    def test_confirm_redone_stops_asking(self):
        self.near_miss_setup()
        r = self.auto.confirm("deploy-service", redone=["s-price", "s-kv"])
        self.assertEqual(r["status"], "forming")
        self.assertEqual(self.auto.near_misses(), [])                            # nothing unclear left

    def test_old_injection_flags_are_rechecked(self):
        from detect import summarize as sm
        self.add_sessions(["s-fruit", "s-price"])
        for sid, flagged in (("s-fruit", True), ("s-price", False)):
            rec = self.store.load_session(sid)
            self.store.save_summary(sid, {"session_id": sid, "task_family": "x", "model": "m", "cost_usd": 0,
                                          "summary": {"injection_suspected": flagged}, "created_at": self.store.now(),
                                          "source_content_hash": rec["content_hash"]})   # an old (v1) summary
        sids, _ = sm.pending_sessions()
        self.assertEqual(sids, ["s-fruit"])                                      # only the flagged one is redone
        self.assertIn("pasted_content", sm.SYSTEM)
        self.assertIn("never an injection", sm.SYSTEM)

    def test_old_one_offs_are_forgotten_but_evidence_is_kept(self):
        self.add_sessions()
        self.store.save_pattern({"key": "csv", "label": "csv", "description": "", "status": "forming", "n_sessions": 2,
                                 "n_success": 2, "deterministic_frac": 1.0, "suggested_type": "script",
                                 "type_rationale": "", "session_ids": ["s-fruit", "s-price"]})
        later = datetime.now(timezone.utc) + timedelta(days=91)
        self.assertEqual(self.auto.prune_sessions(90, dry_run=True, now=later), ["s-kv"])  # the only one-off
        self.assertIsNotNone(self.store.load_session("s-kv"))                  # dry run deletes nothing
        self.assertEqual(self.auto.prune_sessions(90), [])                      # not old enough yet
        self.auto.prune_sessions(90, now=later)
        self.assertIsNone(self.store.load_session("s-kv"))
        self.assertIsNotNone(self.store.load_session("s-fruit"))                # pattern evidence stays

    def test_stale_lock_is_replaced(self):
        self.store.store_dir()
        self.auto._lock_path().write_text(json.dumps({"pid": 1, "t": time.time() - 3 * 3600}))
        self.assertTrue(self.auto._acquire())
        self.auto._release()

    def test_recently_failed_candidates_wait(self):
        base = {"label": "x", "description": "", "status": "candidate", "n_sessions": 3, "n_success": 3,
                "deterministic_frac": 1.0, "suggested_type": "script", "type_rationale": "", "session_ids": []}
        self.store.save_pattern({**base, "key": "fresh"})
        self.store.save_pattern({**base, "key": "failed-today", "last_proposal": {"at": self.store.now(), "passed": False}})
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        self.store.save_pattern({**base, "key": "failed-long-ago", "last_proposal": {"at": old, "passed": False}})
        self.assertEqual([p["key"] for p in self.auto.candidates_to_draft(7)], ["failed-long-ago", "fresh"])

    # ---------------- a whole run ----------------

    def test_full_run_without_any_manual_rating(self):
        from harness_lib import sandbox
        if not sandbox.network_isolated():
            self.skipTest("drafting runs generated code: needs the sandbox")
        self.auto.set_config(enabled=True)
        self.add_sessions()
        r = self.auto.run()
        self.assertTrue(r["ran"], r)
        self.assertNotIn("error", r)
        self.assertEqual(r["ratings"]["rated"], 3)
        self.assertEqual(r["drafted"][0]["pattern"], "csv-to-json-conversion")
        self.assertTrue(r["drafted"][0]["passed"], r)
        self.assertGreater(r["spent_usd"], 0)
        self.assertFalse(self.auto._lock_path().exists())
        self.assertEqual(self.store.load_pattern("csv-to-json-conversion")["status"], "proposed")
        self.assertFalse(self.auto.should_run()[0])                        # just ran: throttled
        spends = self.store.read_jsonl(self.store.store_dir() / "spend.jsonl")
        self.assertTrue(spends and all(s["purpose"] == "auto" for s in spends))
        # the inbox and the SessionStart note
        self.assertEqual([i["draft"] for i in self.auto.inbox()], ["csv-to-json"])
        text = self.auto.session_start_context()
        self.assertIn("script 'csv-to-json'", text)
        self.assertIn("Only if the user explicitly says yes", text)
        self.assertIsNone(self.auto.session_start_context())               # each draft version: once

    # ---------------- hooks ----------------

    def hook(self, payload):
        from hooks import log_session
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
            log_session.hook_main()
        return out.getvalue()

    def test_session_end_starts_background_run_only_when_due(self):
        c, j, _, _ = SESSIONS["s-fruit"]
        tp = transcript(self.t / "x.jsonl", "s-fruit", c, j)
        with mock.patch.object(self.auto, "spawn_in_background") as spawn:
            self.hook({"session_id": "s-fruit", "transcript_path": tp, "hook_event_name": "SessionEnd", "reason": "other"})
            spawn.assert_not_called()                                       # automatic learning is off
            self.auto.set_config(enabled=True, min_new_sessions=1)
            self.hook({"session_id": "s-fruit", "transcript_path": tp, "hook_event_name": "Stop"})
            spawn.assert_not_called()                                       # only SessionEnd triggers it
            self.hook({"session_id": "s-fruit", "transcript_path": tp, "hook_event_name": "SessionEnd", "reason": "other"})
            spawn.assert_called_once()

    def test_session_start_note(self):
        self.assertEqual(self.hook({"hook_event_name": "SessionStart", "source": "startup"}), "")
        d = Path(os.environ["LEARNING_HARNESS_GENERATED"]) / "notes"
        (d / "files").mkdir(parents=True)
        self.store.write_json_atomic(d / "proposal.json", {"name": "notes", "type": "skill", "status": "validated",
                                                            "pattern_key": "p", "description": "Write notes",
                                                            "evidence": {"n_sessions": 3, "n_success": 3}})
        self.store.write_json_atomic(d / "validation.json", {"fingerprint": "f1", "tests": [],
                                                              "judged": [{"verdict": "pass"}] * 3})
        out = json.loads(self.hook({"hook_event_name": "SessionStart", "source": "startup"}))
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("skill 'notes'", ctx["additionalContext"])
        self.assertIn("judged checks 3/3", ctx["additionalContext"])
        self.assertEqual(self.hook({"hook_event_name": "SessionStart", "source": "startup"}), "")   # once only
        self.store.write_json_atomic(d / "validation.json", {"fingerprint": "f2", "tests": [], "judged": []})
        self.assertIn("notes", self.hook({"hook_event_name": "SessionStart"}))                  # new version: again
        self.assertFalse(self.store.iter_sessions())                       # SessionStart never ingests

    def test_background_process_really_detaches_and_runs(self):
        self.auto.set_config(enabled=True, min_new_sessions=1)
        self.add_sessions(["s-fruit"])
        self.auto.spawn_in_background()
        self.addCleanup(lambda: [c.wait(timeout=60) for c in self.auto._children])
        state = self.store.store_dir() / "auto_state.json"
        for _ in range(150):
            if state.exists() and not self.auto._lock_path().exists():
                break
            time.sleep(0.2)
        self.assertTrue(state.exists(), (self.store.store_dir() / "auto.log").read_text()[-800:])
        self.assertEqual(self.store.outcome("s-fruit")["source"], "inferred")
        self.assertIn("auto run finished", (self.store.store_dir() / "auto.log").read_text())

    # ---------------- learning while you work ----------------

    def test_similarity(self):
        from harness_lib import similar
        a = similar.words("Convert in.csv to JSON please")
        self.assertEqual(a, {"convert", "in.csv", "json"})
        self.assertGreater(similar.score(a, similar.words("convert in.csv to json")), 0.9)
        self.assertEqual(similar.score(a, similar.words("write a poem about the sea")), 0.0)

    def test_repeat_is_suggested_once_per_session(self):
        self.add_sessions()
        self.assertIsNone(submit("new-1", "Convert in.csv to JSON"))       # automatic learning off: quiet
        self.auto.set_config(enabled=True)
        self.assertIsNone(submit("new-1", "What does this repository do?"))  # not a repeat
        text = submit("new-1", "Convert in.csv to JSON")
        self.assertIn("asked for this kind of task before", text)
        self.assertIn("refine.py learn", text)
        self.assertIn("explicit", text)
        self.assertIsNone(submit("new-1", "Convert in.csv to JSON"))       # once per session
        self.assertIsNotNone(submit("new-2", "convert in.csv to json"))

    def test_redone_sessions_dont_count(self):
        self.add_sessions()
        self.auto.set_config(enabled=True)
        for sid in ("s-fruit", "s-price"):
            self.store.rate(sid, "redone")
        self.assertIsNone(submit("new-1", "Convert in.csv to JSON"))       # one good earlier session: not enough

    def test_learned_task_points_to_what_was_learned(self):
        self.add_sessions()
        self.auto.set_config(enabled=True)
        self.store.save_pattern({"key": "csv-json", "label": "csv json", "status": "installed",
                                 "session_ids": ["s-fruit", "s-kv"]})
        text = submit("new-1", "Convert in.csv to JSON")
        self.assertIn("already learned", text)
        self.assertNotIn("refine.py learn", text)


if __name__ == "__main__":
    unittest.main()
