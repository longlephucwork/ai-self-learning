"""SQLite store. One file: store/sessions.db (override with LEARNING_HARNESS_STORE)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
OUTCOMES = ("accepted", "edited", "redone", "unknown")


def store_dir() -> Path:
    d = Path(os.environ.get("LEARNING_HARNESS_STORE", REPO_ROOT / "store"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    cwd               TEXT,
    git_branch        TEXT,
    cc_version        TEXT,
    entrypoint        TEXT,
    models            TEXT,          -- JSON list
    started_at        TEXT,
    last_activity_at  TEXT,
    ended_at          TEXT,          -- set by SessionEnd only
    end_reason        TEXT,
    transcript_path   TEXT,          -- original location
    transcript_copy   TEXT,          -- redacted gzip copy in store/transcripts
    first_request     TEXT,
    final_result      TEXT,
    n_requests        INTEGER,
    n_tool_calls      INTEGER,
    n_tool_errors     INTEGER,
    n_sidechain_lines INTEGER,
    ingest_count      INTEGER DEFAULT 0,
    first_ingested_at TEXT,
    last_ingested_at  TEXT,
    last_hook_event   TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    session_id TEXT, idx INTEGER, timestamp TEXT, text TEXT,
    PRIMARY KEY (session_id, idx)
);

CREATE TABLE IF NOT EXISTS steps (
    session_id TEXT, idx INTEGER, tool_use_id TEXT, tool_name TEXT,
    input_json TEXT, timestamp TEXT, is_error INTEGER, result_preview TEXT,
    PRIMARY KEY (session_id, idx)
);

CREATE TABLE IF NOT EXISTS files_touched (
    session_id TEXT, path TEXT, action TEXT, count INTEGER,
    PRIMARY KEY (session_id, path, action)
);

-- Append-only; the latest row per session is the current outcome.
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted','edited','redone','unknown')),
    source TEXT NOT NULL DEFAULT 'manual',     -- manual | inferred
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, event TEXT, reason TEXT, received_at TEXT
);

-- Reserved for Phase 2+ (created now so the schema is stable).
CREATE TABLE IF NOT EXISTS summaries (
    session_id TEXT PRIMARY KEY, summary_json TEXT, model TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, intent TEXT, session_ids TEXT,
    n_success INTEGER, deterministic INTEGER, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER, type TEXT, name TEXT,
    payload_json TEXT, status TEXT, decided_at TEXT, decision_note TEXT, created_at TEXT
);

CREATE VIEW IF NOT EXISTS session_outcomes AS
SELECT s.session_id, COALESCE(o.outcome, 'unknown') AS outcome, o.source, o.note, o.created_at AS rated_at
FROM sessions s
LEFT JOIN outcomes o ON o.id = (SELECT MAX(id) FROM outcomes WHERE session_id = s.session_id);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or store_dir() / "sessions.db"
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con


def upsert_session(con: sqlite3.Connection, s, *, transcript_path: str, transcript_copy: str | None,
                   hook_event: str | None, end_reason: str | None,
                   last_assistant_message: str | None = None) -> None:
    sid = s.session_id
    final = last_assistant_message or s.final_result
    ts = now()
    n_err = sum(1 for st in s.steps if st.is_error)
    with con:
        con.execute(
            """
            INSERT INTO sessions (session_id, cwd, git_branch, cc_version, entrypoint, models,
                started_at, last_activity_at, transcript_path, transcript_copy, first_request,
                final_result, n_requests, n_tool_calls, n_tool_errors, n_sidechain_lines,
                ingest_count, first_ingested_at, last_ingested_at, last_hook_event)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
                cwd=excluded.cwd, git_branch=excluded.git_branch, cc_version=excluded.cc_version,
                entrypoint=excluded.entrypoint, models=excluded.models,
                started_at=excluded.started_at, last_activity_at=excluded.last_activity_at,
                transcript_path=excluded.transcript_path,
                transcript_copy=COALESCE(excluded.transcript_copy, sessions.transcript_copy),
                first_request=excluded.first_request,
                final_result=COALESCE(excluded.final_result, sessions.final_result),
                n_requests=excluded.n_requests, n_tool_calls=excluded.n_tool_calls,
                n_tool_errors=excluded.n_tool_errors, n_sidechain_lines=excluded.n_sidechain_lines,
                ingest_count=sessions.ingest_count + 1,
                last_ingested_at=excluded.last_ingested_at,
                last_hook_event=excluded.last_hook_event
            """,
            (sid, s.cwd, s.git_branch, s.cc_version, s.entrypoint, json.dumps(s.models),
             s.started_at, s.last_activity_at, transcript_path, transcript_copy,
             s.requests[0]["text"] if s.requests else None, final,
             len(s.requests), len(s.steps), n_err, s.n_sidechain_lines, ts, ts, hook_event),
        )
        if end_reason is not None:
            con.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE session_id=?", (ts, end_reason, sid))

        # Child rows are fully re-derived from the transcript on every ingest.
        for t in ("requests", "steps", "files_touched"):
            con.execute(f"DELETE FROM {t} WHERE session_id=?", (sid,))
        con.executemany("INSERT INTO requests VALUES (?,?,?,?)",
                        [(sid, r["idx"], r["timestamp"], r["text"]) for r in s.requests])
        con.executemany("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?)",
                        [(sid, st.idx, st.tool_use_id, st.tool_name, st.input_json, st.timestamp,
                          None if st.is_error is None else int(st.is_error), st.result_preview)
                         for st in s.steps])
        con.executemany("INSERT INTO files_touched VALUES (?,?,?,?)",
                        [(sid, p, a, c) for (p, a), c in s.files.items()])
        con.execute("INSERT INTO hook_events (session_id, event, reason, received_at) VALUES (?,?,?,?)",
                    (sid, hook_event, end_reason, ts))


def rate(con: sqlite3.Connection, session_id: str, outcome: str, note: str | None = None,
         source: str = "manual") -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    with con:
        con.execute("INSERT INTO outcomes (session_id, outcome, source, note, created_at) VALUES (?,?,?,?,?)",
                    (session_id, outcome, source, note, now()))


def resolve_session(con: sqlite3.Connection, prefix: str) -> str:
    """Accept a full id, a unique prefix, or 'last'."""
    if prefix == "last":
        row = con.execute("SELECT session_id FROM sessions ORDER BY last_activity_at DESC LIMIT 1").fetchone()
        if not row:
            raise LookupError("no sessions stored")
        return row[0]
    rows = con.execute("SELECT session_id FROM sessions WHERE session_id LIKE ?", (prefix + "%",)).fetchall()
    if not rows:
        raise LookupError(f"no session matches {prefix!r}")
    if len(rows) > 1:
        raise LookupError(f"{prefix!r} is ambiguous ({len(rows)} matches)")
    return rows[0][0]
