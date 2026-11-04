"""Spot a repeated task the moment the user asks for it again (plain text matching, no model call).

Used by the UserPromptSubmit hook: the new prompt is compared with the first request of earlier sessions.
Word-set overlap is crude but cheap enough to run on every prompt, and repeated work (the same deploy, the
same report, often the same pasted steps) overlaps heavily. Anything it misses is still caught by the
background run, which groups sessions by meaning.
"""
from __future__ import annotations

import re

from . import store

STOP = set("""a an and are as at be been but by can could do does for from had has have how i if in into is it
its just me my no not of on or our please should so that the their them then there these this those to up us
was we were what when where which who will with would you your can't don't i'm it's let's want need make get
also now some all any one two new use using via per""".split())
MIN_SCORE = 0.45      # Jaccard overlap of content words between two requests
MAX_SCAN = 500        # most recent sessions compared, to keep the hook fast


def words(text: str) -> set[str]:
    text = re.sub(r"</?pasted_content[^>]*>", " ", text or "")
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_.\-]{2,}", text.lower()) if w not in STOP and not w.isdigit()}


def score(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _index() -> list[dict]:
    """(session_id, started_at, first_request) of every session, cached in store/request_index.json.

    session.json files hold every step and can be hundreds of KB, so the hook only re-reads the ones whose
    modification time changed since the last prompt; the rest is a stat() per session."""
    path = store.store_dir() / "request_index.json"
    cache = store.read_json(path) or {}
    out, fresh, changed = [], {}, False
    root = store.sessions_dir()
    for d in root.iterdir() if root.exists() else []:
        f = d / "session.json"
        try:
            mtime = f.stat().st_mtime_ns
        except OSError:
            continue
        row = cache.get(d.name)
        if not row or row.get("mtime") != mtime:
            rec = store.read_json(f) or {}
            row = {"mtime": mtime, "started_at": rec.get("started_at") or "",
                   "first_request": (rec.get("first_request") or "")[:4000]}
            changed = True
        fresh[d.name] = row
        out.append({"session_id": d.name, **row})
    if changed or len(fresh) != len(cache):
        try:
            store.write_json_atomic(path, fresh)
        except OSError:
            pass   # a read-only or locked store only costs speed
    return out


def similar_sessions(prompt: str, *, exclude: str | None = None, min_score: float = MIN_SCORE) -> list[tuple[str, float]]:
    """Earlier sessions whose first request looks like `prompt`, best first."""
    target = words(prompt)
    if len(target) < 3:
        return []
    recs = sorted(_index(), key=lambda r: r["started_at"], reverse=True)[:MAX_SCAN]
    out = []
    for r in recs:
        if r["session_id"] == exclude or not r.get("first_request"):
            continue
        s = score(target, words(r["first_request"]))
        if s >= min_score:
            out.append((r["session_id"], round(s, 3)))
    return sorted(out, key=lambda x: -x[1])


def learned_already(session_ids: list[str]) -> dict | None:
    """The pattern these sessions already belong to, if it's been learned or is being learned."""
    for p in store.iter_patterns():
        if p.get("status") in ("installed", "briefed", "proposed") and set(session_ids) & set(p.get("session_ids", [])):
            return p
    return None
