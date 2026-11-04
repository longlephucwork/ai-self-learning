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


def similar_sessions(prompt: str, *, exclude: str | None = None, min_score: float = MIN_SCORE) -> list[tuple[str, float]]:
    """Earlier sessions whose first request looks like `prompt`, best first."""
    target = words(prompt)
    if len(target) < 3:
        return []
    recs = sorted(store.iter_sessions(), key=lambda r: r.get("started_at") or "", reverse=True)[:MAX_SCAN]
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
