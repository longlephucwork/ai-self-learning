#!/usr/bin/env python3
"""Claude Code hook: ingest a session transcript into the learning-harness file store (store/sessions/<id>/).

Wired to both Stop (fires after every turn, so data survives sessions that are
killed without a clean exit) and SessionEnd (records end time + reason).
Ingestion is idempotent: each run re-derives the session from the transcript.

Hook mode (stdin = hook JSON payload):
    log_session.py
Manual / backfill:
    log_session.py --transcript PATH
    log_session.py --backfill [DIR]      # default ~/.claude/projects

Never fails the host session: errors go to store/hook_errors.log, exit 0.
Set LEARNING_HARNESS_DISABLE=1 to skip logging (used by the harness's own
headless `claude -p` calls so it doesn't learn from itself).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402
from harness_lib.redact import redact_obj  # noqa: E402
from harness_lib.transcript import parse_transcript  # noqa: E402

DROP_KEYS = {"signature"}  # opaque, large, useless for learning


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, list):
        return [_strip(x) for x in obj]
    return obj


def copy_transcript(src: str, session_id: str) -> str:
    """Write a redacted, gzipped copy of the transcript into the session's folder."""
    dst = store.session_dir(session_id, create=True) / "transcript.jsonl.gz"
    # unique temp name: Stop and SessionEnd can ingest the same session concurrently
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".transcript.", suffix=".tmp")
    os.close(fd)
    os.chmod(tmp, store.FILE_MODE)
    with open(src, encoding="utf-8", errors="replace") as fin, gzip.open(tmp, "wt", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            fout.write(json.dumps(redact_obj(_strip(obj)), ensure_ascii=False) + "\n")
    os.replace(tmp, dst)
    return str(dst)


def ingest(transcript_path: str, *, hook_event: str | None = None, end_reason: str | None = None,
           session_id: str | None = None, last_assistant_message: str | None = None) -> str | None:
    transcript_path = os.path.expanduser(transcript_path)
    if not os.path.isfile(transcript_path):
        raise FileNotFoundError(transcript_path)
    s = parse_transcript(transcript_path)
    s.session_id = session_id or s.session_id or Path(transcript_path).stem
    if not s.requests and not s.steps:
        return None  # empty / metadata-only transcript
    copy_transcript(transcript_path, s.session_id)
    lam = redact_obj(last_assistant_message) if last_assistant_message else None
    store.upsert_session(s, transcript_path=transcript_path, hook_event=hook_event,
                         end_reason=end_reason, last_assistant_message=lam)
    return s.session_id


def _log_error(msg: str) -> None:
    try:
        with open(store.store_dir() / "hook_errors.log", "a") as fh:
            fh.write(f"{store.now()} {msg}\n")
    except Exception:
        pass


def hook_main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _log_error(f"bad hook payload: {raw[:200]!r}")
        return 0
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        return session_start()
    if event == "Stop" and payload.get("stop_hook_active"):
        return 0  # we're inside a stop-hook continuation loop; nothing new to log
    tp = payload.get("transcript_path")
    if not tp:
        _log_error(f"no transcript_path in {event} payload")
        return 0
    if not os.path.isfile(os.path.expanduser(tp)):
        # Claude Code also fires SessionEnd for CLI subcommands (`claude mcp add`, `claude --version`, ...)
        # that never write a transcript: nothing to log, and not an error.
        return 0
    ingest(tp, hook_event=event,
           end_reason=payload.get("reason") if event == "SessionEnd" else None,
           session_id=payload.get("session_id"),
           last_assistant_message=payload.get("last_assistant_message"))
    if event == "SessionEnd":
        maybe_start_auto()
    return 0


def session_start() -> int:
    """Tell Claude about validated drafts waiting for the user (local file reads only; no model call)."""
    try:
        from refine import auto
        text = auto.session_start_context()
    except Exception:
        _log_error(traceback.format_exc().replace("\n", " | "))
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    return 0


def maybe_start_auto() -> None:
    """Start a detached automatic-learning run if the throttle allows; never delays the session."""
    try:
        from refine import auto
        ok, _ = auto.should_run()
        if ok:
            auto.spawn_in_background()
    except Exception:
        _log_error(traceback.format_exc().replace("\n", " | "))


def main() -> int:
    if os.environ.get("LEARNING_HARNESS_DISABLE") == "1":
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcript", help="ingest one transcript file")
    ap.add_argument("--backfill", nargs="?", const="~/.claude/projects", help="ingest every *.jsonl under DIR")
    args = ap.parse_args()
    try:
        if args.transcript:
            print(ingest(args.transcript, hook_event="manual"))
        elif args.backfill:
            root = Path(os.path.expanduser(args.backfill))
            n = 0
            for p in sorted(root.rglob("*.jsonl")):
                if "subagents" in p.parts:
                    continue
                try:
                    if ingest(str(p), hook_event="backfill"):
                        n += 1
                except Exception as e:  # keep going
                    print(f"skip {p}: {e}", file=sys.stderr)
            print(f"ingested {n} sessions from {root}")
        else:
            return hook_main()
    except Exception:
        _log_error(traceback.format_exc().replace("\n", " | "))
        if args.transcript or args.backfill:
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
