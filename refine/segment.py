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


def make_segment(sid: str, spec: str, *, focus: str | None = None) -> str:
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
    store.upsert_session(s, transcript_path=None, hook_event="segment", end_reason="segment",
                         extra={"segment": {"of": sid, "requests": [a, b], "focus": focus}})
    return seg
