"""File-based store. No database: everything is plain JSON / JSONL files on disk.

Layout (root = $LEARNING_HARNESS_STORE or <repo>/store):

  sessions/<session_id>/
    session.json         derived record; rewritten by the hook on each ingest
    transcript.jsonl.gz  redacted copy of the full transcript
    ratings.jsonl        append-only outcome history, last line wins (only `rate` writes it)
  hook_events.jsonl      append-only log of hook invocations
  hook_errors.log

Concurrency: the hook (Stop / SessionEnd) and the CLI never write the same
file. session.json updates are read-modify-write under a per-session lock file
and land via atomic rename, so readers never see a half-written file.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: rely on atomic rename only
    fcntl = None

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMAT_VERSION = 1
OUTCOMES = ("accepted", "edited", "redone", "unknown")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_dir() -> Path:
    d = Path(os.environ.get("LEARNING_HARNESS_STORE", REPO_ROOT / "store"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir() -> Path:
    d = store_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_dir(sid: str, create: bool = False) -> Path:
    if not sid or "/" in sid or sid.startswith("."):
        raise ValueError(f"bad session id {sid!r}")
    d = sessions_dir() / sid
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- low-level file helpers ----------

def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)  # single O_APPEND write: lines never interleave
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate a torn/edited line
    return out


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


@contextmanager
def _locked(d: Path):
    if fcntl is None:
        yield
        return
    with open(d / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------- sessions ----------

def upsert_session(s, *, transcript_path: str, hook_event: str | None, end_reason: str | None,
                   last_assistant_message: str | None = None) -> dict:
    """Write session.json from a parsed transcript, keeping ingest bookkeeping and end info."""
    d = session_dir(s.session_id, create=True)
    ts = now()
    with _locked(d):
        old = read_json(d / "session.json") or {}
        steps = [asdict(st) for st in s.steps]
        rec = {
            "format_version": FORMAT_VERSION,
            "session_id": s.session_id,
            "cwd": s.cwd,
            "git_branch": s.git_branch,
            "cc_version": s.cc_version,
            "entrypoint": s.entrypoint,
            "models": s.models,
            "started_at": s.started_at,
            "last_activity_at": s.last_activity_at,
            "ended_at": ts if end_reason is not None else old.get("ended_at"),
            "end_reason": end_reason if end_reason is not None else old.get("end_reason"),
            "transcript_path": transcript_path,
            "first_request": s.requests[0]["text"] if s.requests else None,
            "final_result": last_assistant_message or s.final_result or old.get("final_result"),
            "n_requests": len(s.requests),
            "n_tool_calls": len(steps),
            "n_tool_errors": sum(1 for st in steps if st["is_error"]),
            "n_sidechain_lines": s.n_sidechain_lines,
            "ingest_count": old.get("ingest_count", 0) + 1,
            "first_ingested_at": old.get("first_ingested_at", ts),
            "last_ingested_at": ts,
            "last_hook_event": hook_event,
            "requests": s.requests,
            "steps": steps,
            "files_touched": [{"path": p, "action": a, "count": c} for (p, a), c in sorted(s.files.items())],
        }
        write_json_atomic(d / "session.json", rec)
    append_jsonl(store_dir() / "hook_events.jsonl",
                 {"session_id": s.session_id, "event": hook_event, "reason": end_reason, "received_at": ts})
    return rec


def load_session(sid: str) -> dict | None:
    return read_json(session_dir(sid) / "session.json")


def iter_sessions() -> list[dict]:
    out = []
    for d in sessions_dir().iterdir():
        if d.is_dir():
            rec = read_json(d / "session.json")
            if rec:
                out.append(rec)
    return out


def resolve_session(prefix: str) -> str:
    """Accept a full id, a unique prefix, or 'last'."""
    if prefix == "last":
        recs = iter_sessions()
        if not recs:
            raise LookupError("no sessions stored")
        return max(recs, key=lambda r: r.get("last_activity_at") or "")["session_id"]
    ids = [d.name for d in sessions_dir().iterdir()
           if d.is_dir() and d.name.startswith(prefix) and (d / "session.json").exists()]
    if not ids:
        raise LookupError(f"no session matches {prefix!r}")
    if len(ids) > 1:
        raise LookupError(f"{prefix!r} is ambiguous ({len(ids)} matches)")
    return ids[0]


# ---------- outcomes ----------

def rate(sid: str, outcome: str, note: str | None = None, source: str = "manual") -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    d = session_dir(sid)
    if not (d / "session.json").exists():
        raise LookupError(f"no session {sid!r}")
    append_jsonl(d / "ratings.jsonl", {"outcome": outcome, "source": source, "note": note, "created_at": now()})


def outcome(sid: str) -> dict:
    hist = read_jsonl(session_dir(sid) / "ratings.jsonl")
    if not hist:
        return {"outcome": "unknown", "source": None, "note": None, "created_at": None}
    return hist[-1]
