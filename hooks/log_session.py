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
import hashlib
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


COPY_STATE = ".transcript.state.json"  # how far the source has been copied (dot file: bookkeeping, not data)
COPY_STATE_VERSION = 1
TAIL_BYTES = 4096                      # bytes before the offset that must be unchanged to trust it


def _redacted(raw: bytes) -> str | None:
    """One transcript line, redacted and re-serialised; None for blank or unparseable lines."""
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return json.dumps(redact_obj(_strip(obj)), ensure_ascii=False) + "\n"


def _copy_lines(fin, start: int, write) -> int:
    """write() each complete line from `start`, redacted; return the offset just past the last one consumed.

    A last line without a newline is taken only if it's whole JSON (a transcript line that's still being
    written isn't), so a half-written line is left for the next run instead of being lost."""
    fin.seek(start)
    pos = start
    for raw in fin:
        if not raw.endswith(b"\n"):
            try:
                json.loads(raw)
            except ValueError:
                break
        pos += len(raw)
        r = _redacted(raw)
        if r:
            write(r)
    return pos


def _tail_hash(fin, offset: int) -> str:
    fin.seek(max(0, offset - TAIL_BYTES))
    return hashlib.sha256(fin.read(offset - max(0, offset - TAIL_BYTES))).hexdigest()


def copy_transcript(src: str, session_id: str, *, full: bool = False) -> str:
    """Keep a redacted, gzipped copy of the transcript in the session's folder, append-only.

    Each run redacts only the lines added since the last run and appends them as a new gzip member
    (gzip readers read multi-member files as one stream). The copy is rebuilt from the start instead when
    `full` is set or the saved position can't be trusted: no state yet, a different source file, the source
    shorter than the offset or changed just before it, or the copy not the size the last run left it."""
    d = store.session_dir(session_id, create=True)
    dst = d / "transcript.jsonl.gz"
    state_path = d / COPY_STATE
    source = os.path.abspath(src)
    with store._locked(d), open(src, "rb") as fin:  # Stop and SessionEnd can ingest a session concurrently
        st = store.read_json(state_path) or {}
        size = os.fstat(fin.fileno()).st_size
        offset = st.get("offset", 0)
        append = (not full and st.get("version") == COPY_STATE_VERSION and st.get("source") == source
                  and 0 < offset <= size and dst.is_file() and dst.stat().st_size == st.get("copy_size")
                  and _tail_hash(fin, offset) == st.get("tail_hash"))
        if append:
            if offset == size:
                return str(dst)  # nothing new since the last run
            out: list[str] = []
            end = _copy_lines(fin, offset, out.append)
            if out:
                # a whole gzip member in one O_APPEND write. If it's torn, the copy's size no longer matches
                # the state, so the next run rebuilds it.
                data = gzip.compress("".join(out).encode("utf-8"))
                fd = os.open(dst, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
        else:
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".transcript.", suffix=".tmp")
            os.close(fd)
            os.chmod(tmp, store.FILE_MODE)
            try:
                with gzip.open(tmp, "wt", encoding="utf-8") as fout:
                    end = _copy_lines(fin, 0, fout.write)
                store._replace(tmp, dst)  # retried on Windows while another process has the file open
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        store.write_json_atomic(state_path, {"version": COPY_STATE_VERSION, "source": source, "offset": end,
                                             "tail_hash": _tail_hash(fin, end), "copy_size": dst.stat().st_size})
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
    if event == "UserPromptSubmit":
        return prompt_submit(payload)
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
        maybe_check_update()
        try:   # keep the repeat-detection index warm, so the next prompt's check is a quick stat() pass
            from harness_lib import similar
            similar._index()
        except Exception:
            _log_error(traceback.format_exc().replace("\n", " | "))
    return 0


def session_start() -> int:
    """Tell Claude about validated drafts waiting for the user (local file reads only; no model call)."""
    parts = []
    for name in ("registry.install", "refine.auto", "harness_lib.update"):
        try:
            mod = __import__(name, fromlist=["session_start_context"])
            t = mod.session_start_context()
            if t:
                parts.append(t)
        except Exception:
            _log_error(traceback.format_exc().replace("\n", " | "))
    text = "\n\n".join(parts)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    # SessionEnd doesn't fire when a terminal window is just closed, so start the (throttled, detached)
    # background work from here as well; both return at once and do nothing when not due.
    maybe_start_auto()
    maybe_check_update()
    return 0


def prompt_submit(payload: dict) -> int:
    """When the user asks for a task they've done before, tell Claude to offer learning it once it's done.

    Plain text matching against earlier sessions (harness_lib/similar.py): fast, local, no model call.
    At most once per session, and only with automatic learning on."""
    try:
        from harness_lib import similar
        from refine import auto
        cfg = auto.config()
        sid, prompt = payload.get("session_id"), payload.get("prompt") or ""
        if not cfg["enabled"] or not sid or not prompt.strip():
            return 0
        seen_path = store.store_dir() / "suggested.json"
        seen = store.read_json(seen_path) or {}
        if sid in seen:
            return 0
        prior = [s for s, _ in similar.similar_sessions(prompt, exclude=sid)
                 if store.outcome(s)["outcome"] != "redone"]
        if len(prior) < max(1, int(cfg["min_success"]) - 1):
            return 0
        seen[sid] = store.now()
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-1000:])   # keep the file small
        store.write_json_atomic(seen_path, seen)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": repeat_context(prior, similar)}}))
    except Exception:
        _log_error(traceback.format_exc().replace("\n", " | "))
    return 0


def repeat_context(prior: list[str], similar) -> str:
    py = Path(sys.executable).resolve().as_posix()
    tool = (store.REPO_ROOT / "refine" / "refine.py").as_posix()
    cli = (store.REPO_ROOT / "review" / "cli.py").as_posix()
    shown = []
    for s in prior[:4]:
        rec = store.load_session(s) or {}
        shown.append(f"{(rec.get('started_at') or '')[:10]}: {(rec.get('first_request') or '')[:70]!r}")
    p = similar.learned_already(prior)
    if p and p["status"] == "installed":
        return (f"[learning-harness] This looks like a task the user has done before ({len(prior)} earlier sessions), "
                f"which the harness has already learned (pattern '{p['key']}'). Use what was learned for it (its skill, "
                f"tool or script) rather than redoing the work by hand.")
    if p:
        return (f"[learning-harness] This looks like a task the user has done before ({len(prior)} earlier sessions). "
                f"Something learned from it is waiting for the user (pattern '{p['key']}', status {p['status']}). After "
                f"the task, mention it in one sentence and offer to finish it ({py} {cli} proposals lists drafts; a skill "
                f"brief is built with skill-creator).")
    return ("[learning-harness] The user has asked for this kind of task before, in " + str(len(prior)) +
            " earlier sessions (" + "; ".join(shown) + "). Do the task as usual. When it's done, offer in one sentence "
            "to turn it into something reusable, so next time takes less effort. Only if the user agrees, run: "
            f"{py} {tool} learn   (it learns from this session together with those earlier ones). Then follow "
            "what it prints: for SKILL BRIEF, build the skill with skill-creator from that brief and run the adopt "
            "command it names; for a validated draft, offer to show it. Install nothing without the user's explicit "
            "yes. Don't bring this up again in this session if they decline.")


def maybe_check_update() -> None:
    """At most once per interval, look for a new release in the background (git fetch; no model calls)."""
    try:
        from harness_lib import update
        if update.due():
            update.spawn_check_in_background()
    except Exception:
        _log_error(traceback.format_exc().replace("\n", " | "))


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
