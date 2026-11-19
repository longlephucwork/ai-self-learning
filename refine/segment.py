"""Segments: learn one part of a conversation instead of the whole session.

A long session often does several jobs in a row (update a repo, merge a PR, then release). To learn just one of
them, pick the requests that make it up (`refine.py learn --list` numbers them) and learn only those:

    refine.py learn --requests 7-9 --focus "release a new version"

That makes a segment: a derived session `<session_id>~r7-9` in the store, holding the chosen requests and every
transcript line from the first of them up to (not including) the next request after the last. It is rebuilt
from the parent's redacted transcript copy with the same parser as any session, so summaries, skill briefs, eval
cases, test provenance and drafting all work on it unchanged. The parent session is left as it was.

A segment has no transcript_path of its own (hooks never re-ingest it), is finished when made (so old ones are
pruned like any one-off), and is rated on its own: learning the release part says nothing about the rest.

Long sessions are also split automatically (split_sessions, run by the background run): when a finished session's
summary lists two or more separate jobs, each becomes an automatic segment ("auto": true), rated, summarised and
grouped on its own, and the whole session is left out of grouping so its work isn't counted twice. If the session
changes (resumed), its automatic segments are made again; ones a brief or installed item uses as evidence are kept.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import tempfile

from harness_lib import store
from harness_lib.transcript import TranscriptParser


class SegmentError(ValueError):
    pass


def requests(sid: str) -> list[dict]:
    rec = store.load_session(sid)
    if not rec:
        raise LookupError(f"no session {sid!r}")
    return rec.get("requests", [])


def format_requests(sid: str, width: int = 100) -> str:
    """The session's requests, numbered from 1 the way --requests takes them."""
    lines = []
    for n, r in enumerate(requests(sid), 1):
        text = " ".join((r.get("text") or "").split())
        lines.append(f"{n:4}  {(r.get('timestamp') or '')[:16].replace('T', ' ')}  "
                     f"{text[:width]}{'…' if len(text) > width else ''}")
    return "\n".join(lines)


def parse_range(spec: str, n_requests: int) -> tuple[int, int]:
    """'7-9', '7' or '7-' (to the last request), numbered from 1; returns (first, last), both inclusive."""
    m = re.fullmatch(r"\s*(\d+)\s*(?:(-)\s*(\d*)\s*)?", spec or "")
    if not m:
        raise SegmentError(f"--requests takes a request number or a range like 7-9, not {spec!r}")
    a = int(m.group(1))
    b = (int(m.group(3)) if m.group(3) else n_requests) if m.group(2) else a
    if not 1 <= a <= b <= n_requests:
        raise SegmentError(f"requests {spec!r} aren't within 1-{n_requests} (see `refine.py learn --list`)")
    return a, b


def segment_id(sid: str, a: int, b: int) -> str:
    return f"{sid}{store.SEGMENT_SEP}r{a}-{b}"


def make_segment(sid: str, spec: str, *, focus: str | None = None, auto: bool = False) -> str:
    """Build (or rebuild) the segment of session `sid` holding requests `spec`; returns its session id."""
    if store.is_segment(sid):
        raise SegmentError("that's already a segment; pick the requests from the whole session")
    reqs = requests(sid)
    if not reqs:
        raise SegmentError(f"session {sid[:8]} has no requests to choose from")
    a, b = parse_range(spec, len(reqs))
    start = reqs[a - 1].get("timestamp")
    end = reqs[b].get("timestamp") if b < len(reqs) else None
    if not start or (b < len(reqs) and not end):
        raise SegmentError("those requests have no timestamps in the transcript, so their part can't be cut out")
    src = store.session_dir(sid) / "transcript.jsonl.gz"
    if not src.is_file():
        raise SegmentError(f"session {sid[:8]} has no transcript copy to cut the part from")

    seg = segment_id(sid, a, b)
    d = store.session_dir(seg, create=True)
    parser = TranscriptParser()
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".transcript.", suffix=".tmp")
    os.close(fd)
    os.chmod(tmp, store.FILE_MODE)
    try:
        with gzip.open(src, "rt", encoding="utf-8") as fin, gzip.open(tmp, "wt", encoding="utf-8") as fout:
            for line in fin:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = obj.get("timestamp") if isinstance(obj, dict) else None
                # ISO timestamps in one format compare as strings. Lines without one (bookkeeping) are left out.
                if not ts or ts < start or (end is not None and ts >= end):
                    continue
                parser.feed(obj)
                fout.write(line if line.endswith("\n") else line + "\n")
        store._replace(tmp, d / "transcript.jsonl.gz")
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    s = parser.session()
    s.session_id = seg
    if not s.requests:
        if not (d / "session.json").exists():   # a new segment that came out empty: leave nothing behind
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        raise SegmentError("nothing the user asked for falls in those requests")
    meta = {"of": sid, "requests": [a, b], "focus": focus}
    if auto:   # made by split_sessions from the session's summary, as of this version of the session
        meta.update(auto=True, parent_hash=(store.load_session(sid) or {}).get("content_hash"))
    store.upsert_session(s, transcript_path=None, hook_event="segment", end_reason="segment", extra={"segment": meta})
    return seg


# ---------------- splitting long sessions into their jobs ----------------

def auto_segments(sid: str) -> list[dict]:
    """The automatic segments made from session `sid`: [{id, requests, parent_hash}]."""
    out = []
    for d in store.sessions_dir().glob(f"{sid}{store.SEGMENT_SEP}r*"):
        seg = (store.read_json(d / "session.json") or {}).get("segment") or {}
        if seg.get("auto") and seg.get("of") == sid:
            out.append({"id": d.name, "requests": seg.get("requests"), "parent_hash": seg.get("parent_hash")})
    return out


def split_parents() -> set[str]:
    """Sessions that have been split into automatic segments: the segments stand in for them in grouping."""
    out = set()
    for d in store.sessions_dir().glob(f"*{store.SEGMENT_SEP}r*"):
        seg = (store.read_json(d / "session.json") or {}).get("segment") or {}
        if seg.get("auto") and seg.get("of"):
            out.add(seg["of"])
    return out


def _jobs(summary: dict, n_requests: int) -> list[tuple[int, int, str]]:
    """The summary's separate jobs as non-overlapping request ranges in order; [] unless there are two or more."""
    jobs, last = [], 0
    for t in sorted(summary.get("tasks") or [], key=lambda t: (t.get("first_request", 0), t.get("last_request", 0))):
        a, b = int(t.get("first_request", 0)), int(t.get("last_request", 0))
        if not 1 <= a <= b <= n_requests or a <= last:
            continue   # out of range, or overlapping the previous job
        jobs.append((a, b, str(t.get("title") or "")[:120]))
        last = b
    return jobs if len(jobs) >= 2 else []


def _evidence_elsewhere() -> set[str]:
    """Sessions a brief or an installed item relies on (pattern membership is rebuilt every run, so not that)."""
    from refine.brief import briefs_dir
    from registry import install as reg
    keep = set()
    for b in briefs_dir(create=False).glob("*/brief.json"):
        keep |= set((store.read_json(b) or {}).get("sessions", []))
    for e in reg.load_registry()["installed"].values():
        keep |= set((e.get("evidence") or {}).get("sessions", []))
    return keep


def split_sessions(log=lambda *_: None) -> list[str]:
    """Split every finished session whose summary lists two or more jobs into automatic segments, one per job.
    Returns the segments made (they need rating and summarising)."""
    import shutil
    made, keep = [], None
    for r in store.iter_sessions(segments=False):
        sid = r["session_id"]
        if not r.get("ended_at"):
            continue   # still going: split once it's finished
        summ = store.load_summary(sid)
        if not summ or summ.get("source_content_hash") != r.get("content_hash"):
            continue   # its summary is out of date: wait for the new one
        jobs = _jobs(summ.get("summary", {}), r.get("n_requests") or 0)
        have = auto_segments(sid)
        want = {segment_id(sid, a, b) for a, b, _ in jobs}
        current = {x["id"] for x in have if x["parent_hash"] == r.get("content_hash")}
        if current == want:
            continue
        keep = _evidence_elsewhere() if keep is None else keep
        for x in have:   # out of date (the session changed) or no longer a job of it
            if x["id"] not in want or x["parent_hash"] != r.get("content_hash"):
                if x["id"] in keep:
                    continue   # a brief or installed item uses it as evidence
                shutil.rmtree(store.session_dir(x["id"]), ignore_errors=True)
        for a, b, title in jobs:
            seg = segment_id(sid, a, b)
            old = (store.load_session(seg) or {}).get("segment") or {}
            if old and not old.get("auto"):
                continue   # the user made this part with `learn --requests`: theirs to keep
            if seg in current:
                continue
            try:
                made.append(make_segment(sid, f"{a}-{b}", focus=title, auto=True))
            except SegmentError as e:
                log(f"  couldn't split {sid[:8]} at requests {a}-{b}: {e}")
        if jobs:
            log(f"  split {sid[:8]} into {len(jobs)} job(s): " + "; ".join(f"{a}-{b} {t[:40]}" for a, b, t in jobs))
    return made
