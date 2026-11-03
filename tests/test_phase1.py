import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures" / "sample.jsonl"


class Phase1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LEARNING_HARNESS_STORE"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_redact(self):
        from harness_lib.redact import redact
        for secret in ["sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123",
                       "ghp_" + "a" * 36, "AKIAABCDEFGHIJKLMNOP",
                       "Authorization: Bearer abcdefghijklmnopqrstuv",
                       'password = "hunter2hunter2"', "https://bob:s3cretpass@host/x",
                       "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"]:
            out = redact(secret)
            self.assertIn("REDACTED", out, secret)
        self.assertEqual(redact("max_tokens=4096 and plain text"), "max_tokens=4096 and plain text")

    def test_parse(self):
        from harness_lib.transcript import parse_transcript
        s = parse_transcript(str(FIX))
        self.assertEqual(s.session_id, "test-sess-1")
        self.assertEqual([r["text"] for r in s.requests], ["Convert data.csv to JSON", "/review now"])
        self.assertEqual([st.tool_name for st in s.steps], ["Read", "Bash", "Write"])
        self.assertTrue(s.steps[1].is_error)
        self.assertNotIn("sk-ant-api03", s.steps[1].input_json)
        self.assertEqual(s.files, {("/proj/data.csv", "read"): 1, ("/proj/data.json", "write"): 1})
        self.assertEqual(s.final_result, "Done: wrote data.json")
        self.assertEqual(s.n_sidechain_lines, 1)

    def _hook(self, payload):
        from hooks import log_session
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            self.assertEqual(log_session.hook_main(), 0)
        finally:
            sys.stdin = sys.__stdin__

    def test_hook_ingest_idempotent_and_rate(self):
        from harness_lib import store
        payload = {"session_id": "test-sess-1", "transcript_path": str(FIX), "hook_event_name": "Stop",
                   "last_assistant_message": "Final via Stop token=abcdefgh12345678"}
        self._hook(payload)
        self._hook(payload)
        self._hook({**payload, "hook_event_name": "SessionEnd", "reason": "other"})

        d = Path(self.tmp.name) / "sessions" / "test-sess-1"
        self.assertEqual(sorted(p.name for p in d.iterdir() if not p.name.startswith(".")),
                         ["session.json", "transcript.jsonl.gz"])
        rec = json.loads((d / "session.json").read_text())
        self.assertEqual(rec["ingest_count"], 3)
        self.assertEqual(rec["end_reason"], "other")
        self.assertEqual(rec["n_tool_calls"], 3)
        self.assertEqual(len(rec["steps"]), 3)
        self.assertNotIn("abcdefgh12345678", rec["final_result"])
        copy = gzip.open(d / "transcript.jsonl.gz", "rt").read()
        self.assertNotIn("sk-ant-api03", copy)
        self.assertNotIn("signature", copy)
        events = store.read_jsonl(Path(self.tmp.name) / "hook_events.jsonl")
        self.assertEqual([e["event"] for e in events], ["Stop", "Stop", "SessionEnd"])

        self.assertEqual(store.outcome("test-sess-1")["outcome"], "unknown")
        store.rate(store.resolve_session("test"), "edited", "fixed a column")
        store.rate("test-sess-1", "accepted")
        self.assertEqual(store.outcome("test-sess-1")["outcome"], "accepted")
        self.assertEqual(len(store.read_jsonl(d / "ratings.jsonl")), 2)
        # a later Stop re-ingest must not touch ratings, and end info must persist
        self._hook(payload)
        rec = json.loads((d / "session.json").read_text())
        self.assertEqual(rec["end_reason"], "other")
        self.assertEqual(store.outcome("test-sess-1")["outcome"], "accepted")
        with self.assertRaises(LookupError):
            store.rate("nope", "accepted")

    def test_concurrent_hooks_and_rating(self):
        import threading
        from harness_lib import store
        payload = {"session_id": "test-sess-1", "transcript_path": str(FIX), "hook_event_name": "Stop"}
        self._hook(payload)
        from hooks import log_session
        def ingest():
            for _ in range(10):
                log_session.ingest(str(FIX), hook_event="Stop", session_id="test-sess-1")
        def rater():
            for _ in range(10):
                store.rate("test-sess-1", "accepted")
        ts = [threading.Thread(target=f) for f in (ingest, ingest, rater)]
        [t.start() for t in ts]; [t.join() for t in ts]
        rec = store.load_session("test-sess-1")
        self.assertEqual(rec["ingest_count"], 21)
        self.assertEqual(len(store.read_jsonl(store.session_dir("test-sess-1") / "ratings.jsonl")), 10)

    def test_stop_hook_active_skipped(self):
        from harness_lib import store
        self._hook({"transcript_path": str(FIX), "hook_event_name": "Stop", "stop_hook_active": True})
        self.assertEqual(store.iter_sessions(), [])

    def test_bad_session_id_rejected(self):
        from harness_lib import store
        for bad in ("../etc", "a/b", ".hidden", ""):
            with self.assertRaises(ValueError):
                store.session_dir(bad)


if __name__ == "__main__":
    unittest.main()
