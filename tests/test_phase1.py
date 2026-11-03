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

    def test_hook_ingest_idempotent_and_rate(self):
        from hooks import log_session
        from harness_lib import db
        payload = {"session_id": "test-sess-1", "transcript_path": str(FIX), "hook_event_name": "Stop",
                   "last_assistant_message": "Final via Stop token=abcdefgh12345678"}
        for _ in range(2):
            sys.stdin = io.StringIO(json.dumps(payload))
            self.assertEqual(log_session.hook_main(), 0)
        sys.stdin = io.StringIO(json.dumps({**payload, "hook_event_name": "SessionEnd", "reason": "other"}))
        log_session.hook_main()
        sys.stdin = sys.__stdin__

        con = db.connect()
        row = con.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(row["ingest_count"], 3)
        self.assertEqual(row["end_reason"], "other")
        self.assertEqual(row["n_tool_calls"], 3)
        self.assertNotIn("abcdefgh12345678", row["final_result"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 3)
        copy = gzip.open(row["transcript_copy"], "rt").read()
        self.assertNotIn("sk-ant-api03", copy)
        self.assertNotIn("signature", copy)

        db.rate(con, db.resolve_session(con, "test"), "edited", "fixed a column")
        db.rate(con, "test-sess-1", "accepted")
        self.assertEqual(con.execute("SELECT outcome FROM session_outcomes").fetchone()[0], "accepted")

    def test_stop_hook_active_skipped(self):
        from hooks import log_session
        from harness_lib import db
        sys.stdin = io.StringIO(json.dumps({"transcript_path": str(FIX), "hook_event_name": "Stop",
                                            "stop_hook_active": True}))
        log_session.hook_main()
        sys.stdin = sys.__stdin__
        self.assertEqual(db.connect().execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
