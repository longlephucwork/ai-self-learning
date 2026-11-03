"""Quick learn: draft an artifact from ONE session, right now (`refine.py learn [session]`).

Saying "learn this" counts as accepting the session: an unrated session is rated `accepted`
(a session you rated `redone` is refused). The session is summarised, given its own single-session
pattern (or the pattern it already belongs to), and drafted with the normal validation: tests from
its real inputs and outputs, or for a skill, a judged check against it. The draft still waits for
your approval, and its evidence is labelled as one session.
"""
from __future__ import annotations

import os

from harness_lib import store


class LearnError(RuntimeError):
    pass


def resolve(session: str | None) -> str:
    if session:
        return store.resolve_session(session)
    env = os.environ.get("CLAUDE_CODE_SESSION_ID")  # set by Claude Code when Claude runs this command
    if env and store.load_session(env):
        return env
    return store.resolve_session("last")


def _refresh(sid: str) -> None:
    """Re-ingest from the transcript, so a session still in progress is learned up to now."""
    rec = store.load_session(sid) or {}
    tp = rec.get("transcript_path")
    if tp and os.path.isfile(tp):
        from hooks.log_session import ingest
        ingest(tp, hook_event="learn", session_id=sid)


def learn(session: str | None = None, *, model: str | None = None, log=print) -> dict:
    from detect import summarize as sm
    from detect.summarize import slugify
    from generate.propose import propose

    sid = resolve(session)
    _refresh(sid)
    o = store.outcome(sid)
    if o.get("source") == "manual" and o["outcome"] == "redone":
        raise LearnError(f"session {sid[:8]} is rated `redone`; there's nothing good to learn from it")
    if not (o.get("source") == "manual" and o["outcome"] in ("accepted", "edited")):
        store.rate(sid, "accepted", "learn this", source="manual")

    summ = store.load_summary(sid)
    if not summ or summ.get("source_content_hash") != (store.load_session(sid) or {}).get("content_hash"):
        log(f"summarising session {sid[:8]} ...")
        st = sm.summarize_sessions([sid], model=model, log=lambda *_: None)
        if st["failed"]:
            raise LearnError("couldn't summarise the session")
        summ = store.load_summary(sid)
    s = summ["summary"]
    if s.get("injection_suspected"):
        raise LearnError("the session looks like it contains a prompt injection, so it can't be learned from")

    existing = [p for p in store.iter_patterns() if sid in p.get("session_ids", [])]
    for p in existing:
        if p["status"] in ("installed", "proposed", "briefed"):
            raise LearnError(f"already learned as pattern {p['key']!r} ({p['status']})")
    if existing and existing[0]["status"] in ("forming", "candidate"):
        key = existing[0]["key"]
        pattern = existing[0]
        pattern["status"] = "candidate"
    else:
        key = slugify(summ["task_family"])
        if store.load_pattern(key):
            key = f"{key}-{sid[:8]}"
        det = bool(s.get("deterministic"))
        pattern = {"key": key, "label": key.replace("-", " "), "description": s.get("intent", ""),
                   "status": "candidate", "n_sessions": 1, "n_success": 1, "min_success": 1,
                   "success_outcomes": ["accepted", "edited"], "deterministic_frac": 1.0 if det else 0.0,
                   "suggested_type": "script" if det else "skill",
                   "type_rationale": f"quick learn from one session; {s.get('deterministic_reason', '')}",
                   "session_ids": [sid], "excluded_injection": [], "method": "quick", "quick": True,
                   "created_at": store.now(), "updated_at": store.now()}
    store.save_pattern(pattern)
    if pattern["suggested_type"] == "skill":
        from refine.brief import write_brief
        b = write_brief(key)
        log(f"this needs judgment, so it becomes a skill; skills are built with skill-creator. Brief: {b['path']}")
        return {"session": sid, "pattern": key, "brief": b["brief"], "brief_path": b["path"], "passed": None,
                "cost_usd": 0.0, "draft": None, "problems": []}
    log(f"drafting a {pattern['suggested_type']} from session {sid[:8]} ({key}) ...")
    r = propose(key, model=model, quick=True, log=log)
    return {"session": sid, "pattern": key, **r}
