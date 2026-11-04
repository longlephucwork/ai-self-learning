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
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LEARNING_HARNESS_STORE"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._env)

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

    def test_windows_lock_and_replace_fallbacks(self):
        """The msvcrt lock (Windows) must serialize like flock; os.replace is retried on PermissionError."""
        import threading
        from unittest import mock
        from harness_lib import store
        from hooks import log_session
        locks, guard = {}, threading.Lock()

        class FakeMsvcrt:  # stands in for Windows' msvcrt.locking, keyed by the locked file
            LK_LOCK, LK_UNLCK = 1, 0

            @staticmethod
            def locking(fd, mode, n):
                key = os.path.realpath(f"/proc/self/fd/{fd}")
                with guard:
                    lk = locks.setdefault(key, threading.Lock())
                lk.acquire() if mode == FakeMsvcrt.LK_LOCK else lk.release()

        if not os.path.exists("/proc/self/fd"):
            self.skipTest("needs /proc to emulate msvcrt here")
        with mock.patch.object(store, "fcntl", None), mock.patch.object(store, "msvcrt", FakeMsvcrt):
            self._hook({"session_id": "test-sess-1", "transcript_path": str(FIX), "hook_event_name": "Stop"})
            ts = [threading.Thread(target=lambda: [log_session.ingest(str(FIX), hook_event="Stop",
                                                                      session_id="test-sess-1") for _ in range(10)])
                  for _ in range(3)]
            [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(store.load_session("test-sess-1")["ingest_count"], 31)

        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")
            real(src, dst)
        # the hook's transcript copy goes through the same retry (it used to call os.replace directly)
        calls.clear()

        class WindowsOs:  # what the store sees; pathlib keeps the real os module
            name, replace = "nt", staticmethod(flaky)

            def __getattr__(self, attr):
                return getattr(os, attr)
        with mock.patch.object(store, "os", WindowsOs()):
            log_session.copy_transcript(str(FIX), "test-sess-1", full=True)  # a full rebuild replaces the file
        self.assertEqual(len(calls), 4)   # the copy: 2 failures + 1 success; then its position file: 1
        calls.clear()
        target = Path(self.tmp.name) / "x.json"   # build paths before faking os.name (pathlib checks it)
        with mock.patch.object(store.os, "replace", flaky), mock.patch.object(store.os, "name", "nt"):
            store.write_json_atomic(target, {"a": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual(store.read_json(Path(self.tmp.name) / "x.json"), {"a": 1})

    # ---------------- append-only transcript copy ----------------

    @staticmethod
    def _read_copy(sid):
        from harness_lib import store
        return gzip.open(store.session_dir(sid) / "transcript.jsonl.gz", "rt").read()

    def _copy(self, src, sid="inc"):
        from hooks import log_session
        log_session.copy_transcript(str(src), sid)
        return self._read_copy(sid)

    def _full(self, src):
        from hooks import log_session
        log_session.copy_transcript(str(src), "full-ref", full=True)
        return self._read_copy("full-ref")

    def test_copy_appends_only_new_lines(self):
        from unittest import mock

        from harness_lib import store
        from hooks import log_session
        lines = FIX.read_bytes().splitlines(keepends=True)
        src = Path(self.tmp.name) / "live.jsonl"
        src.write_bytes(b"".join(lines[:5]))
        self._copy(src)
        with open(src, "ab") as fh:
            fh.write(b"".join(lines[5:]))
        with mock.patch.object(log_session.gzip, "open", side_effect=AssertionError("rebuilt")), \
                mock.patch.object(log_session, "_redacted", wraps=log_session._redacted) as red:
            log_session.copy_transcript(str(src), "inc")                     # appends: no rebuild
        got = self._read_copy("inc")
        self.assertEqual(red.call_count, len(lines) - 5)                     # only the new lines were redacted
        self.assertEqual(got, self._full(src))                               # same as copying it all at once
        self.assertNotIn("sk-ant-api03", got)
        size = (store.session_dir("inc") / "transcript.jsonl.gz").stat().st_size
        with mock.patch.object(log_session, "_redacted", side_effect=AssertionError("re-read")):
            self._copy(src)                                                  # nothing new: no work at all
        self.assertEqual((store.session_dir("inc") / "transcript.jsonl.gz").stat().st_size, size)

    def test_copy_waits_for_a_half_written_line(self):
        lines = FIX.read_bytes().splitlines(keepends=True)
        src = Path(self.tmp.name) / "live.jsonl"
        half = len(lines[6]) // 2
        src.write_bytes(b"".join(lines[:6]) + lines[6][:half])
        self._copy(src)
        with open(src, "ab") as fh:
            fh.write(lines[6][half:] + b"".join(lines[7:]))
        self.assertEqual(self._copy(src), self._full(src))                   # the torn line isn't lost

    def test_copy_rebuilds_when_it_cant_trust_the_offset(self):
        from harness_lib import store
        lines = FIX.read_bytes().splitlines(keepends=True)
        src = Path(self.tmp.name) / "live.jsonl"
        src.write_bytes(b"".join(lines))
        self._copy(src)
        src.write_bytes(b"".join(lines[:4]))                                 # source got shorter
        self.assertEqual(self._copy(src), self._full(src))
        self._copy(src)
        src.write_bytes(b"".join(reversed(lines[:4])) + b"".join(lines[4:]))  # same size or more, earlier bytes changed
        self.assertEqual(self._copy(src), self._full(src))
        dst = store.session_dir("inc") / "transcript.jsonl.gz"
        with open(dst, "ab") as fh:
            fh.write(b"torn")                                                # copy isn't what the last run left
        with open(src, "ab") as fh:
            fh.write(lines[0])
        self.assertEqual(self._copy(src), self._full(src))
        other = Path(self.tmp.name) / "other.jsonl"
        other.write_bytes(b"".join(lines[:3]))
        self.assertEqual(self._copy(other), self._full(other))               # a different source file

    # ---------------- incremental parse ----------------

    def test_incremental_parse_matches_full_parse(self):
        """Fed in any chunks, including half lines and tool results that arrive turns later, the resumed
        parse gives exactly what a full parse gives."""
        from harness_lib.transcript import parse_incremental, parse_transcript
        data = FIX.read_bytes()
        src = Path(self.tmp.name) / "live.jsonl"
        for step in (1, 7, 50, 333):
            state = None
            for cut in [*range(step, len(data), step), len(data)]:
                src.write_bytes(data[:cut])
                s, state = parse_incremental(str(src), state)
                self.assertEqual(s, parse_transcript(str(src)), f"step {step}, cut {cut}")
        self.assertTrue(s.steps and all(st.result_preview is not None for st in s.steps))
        self.assertEqual(json.loads(json.dumps(state)), state)               # the state is plain JSON

    def test_ingest_parses_only_new_lines(self):
        from unittest import mock

        from harness_lib import store
        from harness_lib.transcript import TranscriptParser, parse_transcript
        from hooks import log_session
        lines = FIX.read_bytes().splitlines(keepends=True)
        src = Path(self.tmp.name) / "inc-parse.jsonl"
        src.write_bytes(b"".join(lines[:4]))
        log_session.ingest(str(src), session_id="inc-parse", hook_event="Stop")
        with open(src, "ab") as fh:
            fh.write(b"".join(lines[4:]))
        with mock.patch.object(TranscriptParser, "feed_raw", autospec=True,
                               side_effect=TranscriptParser.feed_raw) as fed:
            log_session.ingest(str(src), session_id="inc-parse", hook_event="Stop")
        self.assertEqual(fed.call_count, len(lines) - 4)                     # earlier lines weren't re-read
        rec = store.load_session("inc-parse")
        full = parse_transcript(str(src))
        self.assertEqual(rec["n_tool_calls"], len(full.steps))
        self.assertEqual([r["text"] for r in rec["requests"]], [r["text"] for r in full.requests])
        self.assertEqual(rec["final_result"], full.final_result)
        state_path = store.session_dir("inc-parse") / log_session.PARSE_STATE
        before = state_path.stat().st_mtime_ns
        with mock.patch.object(TranscriptParser, "feed_raw", side_effect=AssertionError("re-read")):
            log_session.ingest(str(src), session_id="inc-parse", hook_event="SessionEnd", end_reason="other")
        self.assertEqual(state_path.stat().st_mtime_ns, before)              # nothing new: state not rewritten
        self.assertEqual(store.load_session("inc-parse")["end_reason"], "other")
        src.write_bytes(b"".join(lines[:3]))                                 # the source got shorter: start over
        log_session.ingest(str(src), session_id="inc-parse", hook_event="Stop")
        self.assertEqual(store.load_session("inc-parse")["n_tool_calls"], len(parse_transcript(str(src)).steps))

    def test_stop_hook_active_skipped(self):
        from harness_lib import store
        self._hook({"transcript_path": str(FIX), "hook_event_name": "Stop", "stop_hook_active": True})
        self.assertEqual(store.iter_sessions(), [])

    def test_missing_transcript_is_silently_skipped(self):
        from harness_lib import store
        self._hook({"session_id": "cli-subcommand", "hook_event_name": "SessionEnd", "reason": "other",
                    "transcript_path": str(Path(self.tmp.name) / "never-written.jsonl")})
        self.assertEqual(store.iter_sessions(), [])
        self.assertFalse((Path(self.tmp.name) / "hook_errors.log").exists())
        from hooks import log_session
        with self.assertRaises(FileNotFoundError):          # a wrong path given by hand is still an error
            log_session.ingest(str(Path(self.tmp.name) / "typo.jsonl"))

    def test_file_permissions(self):
        """Atomic writes get normal (umask) permissions, not mkstemp's 0600; the store dir is private."""
        if os.name != "posix":
            self.skipTest("POSIX permissions")
        from harness_lib import store
        p = Path(self.tmp.name) / "perm.json"
        store.write_json_atomic(p, {"a": 1})
        self.assertEqual(p.stat().st_mode & 0o777, store.FILE_MODE)
        self.assertEqual(store.store_dir().stat().st_mode & 0o777, 0o700)

    def test_bad_session_id_rejected(self):
        from harness_lib import store
        for bad in ("../etc", "a/b", ".hidden", ""):
            with self.assertRaises(ValueError):
                store.session_dir(bad)


if __name__ == "__main__":
    unittest.main()
