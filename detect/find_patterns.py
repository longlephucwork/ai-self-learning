#!/usr/bin/env python3
"""Phase 2: detect + classify repeated work.

  find_patterns.py summarize [--limit N] [--force] [--include-active] [--session ID ...]
  find_patterns.py cluster   [--method claude|family] [--min-success 3] [--success accepted,edited]
  find_patterns.py run       (summarize, then cluster; takes both sets of options)
  find_patterns.py list      [--all]       patterns, candidates first
  find_patterns.py show KEY                pattern detail with member sessions
  find_patterns.py summary SESSION         one session's stored summary

Files:
  store/sessions/<id>/summary.json   per-session summary (git-ignored)
  patterns/<key>.json                detected patterns (git-tracked)

Summaries use headless `claude -p` (model: --model, else $LEARNING_HARNESS_MODEL,
else claude-haiku-5-5). Only new or changed sessions are re-summarized. Sessions
still active (no SessionEnd, activity < --idle-minutes ago) are skipped unless
--include-active.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402
from harness_lib.claude_cli import ClaudeError  # noqa: E402
from detect import cluster as cl  # noqa: E402
from detect import summarize as sm  # noqa: E402


def do_summarize(a):
    only = {store.resolve_session(s) for s in a.session} if a.session else None
    sids, active = sm.pending_sessions(force=a.force, include_active=a.include_active,
                                       idle_minutes=a.idle_minutes, only=only)
    if active:
        print(f"skipping {len(active)} active session(s): {', '.join(s[:8] for s in active)} "
              f"(use --include-active)")
    if a.limit:
        sids = sids[: a.limit]
    if not sids:
        print("nothing to summarize")
        return
    print(f"summarizing {len(sids)} session(s) with {a.model or sm.DEFAULT_MODEL}")
    st = sm.summarize_sessions(sids, model=a.model, workers=a.workers)
    print(f"done: {st['ok']} ok, {st['failed']} failed, ${st['cost_usd']:.3f}")


def do_cluster(a):
    summaries = cl.load_summaries()
    if not summaries:
        print("no summaries yet; run `summarize` first")
        return
    success = tuple(x.strip() for x in a.success.split(","))
    bad = set(success) - set(store.OUTCOMES)
    if bad:
        sys.exit(f"unknown outcome(s) in --success: {sorted(bad)}")
    if a.method == "claude":
        keys = [p["key"] for p in store.iter_patterns()]
        clusters, cost = cl.group_with_claude(summaries, keys, model=a.model)
        print(f"clustered {len(summaries)} summaries with claude (${cost:.3f})")
    else:
        clusters = cl.group_by_family(summaries)
        print(f"clustered {len(summaries)} summaries by task_family")
    if a.min_success is None:
        from refine import auto
        a.min_success = int(auto.config()["min_success"])
    rows = cl.apply_clusters(clusters, summaries, method=a.method, min_success=a.min_success,
                             success_outcomes=success)
    _print_patterns(rows)


def _print_patterns(rows):
    print(f"\n{'status':10} {'ok/n':>6} {'det':>5} {'type':7} key")
    for r in rows:
        mark = f"  <-- {r.get('min_success', 3)}+ successes" if r["status"] == "candidate" else ""
        excl = f" (+{len(r['excluded_injection'])} excluded: injection)" if r.get("excluded_injection") else ""
        print(f"{r['status']:10} {r['n_success']:>3}/{r['n_sessions']:<2} {r['deterministic_frac']:>5.0%} "
              f"{r['suggested_type']:7} {r['key']}{excl}{mark}")


def do_list(a):
    rows = store.iter_patterns()
    if not a.all:
        rows = [r for r in rows if r["n_sessions"] > 1 or r["status"] != "forming"]
    if not rows:
        print("no patterns" + ("" if a.all else " with more than one session (use --all)"))
        return
    rows.sort(key=lambda r: (r["status"] != "candidate", -r["n_success"], r["key"]))
    _print_patterns(rows)


def do_show(a):
    r = store.load_pattern(a.key)
    if not r:
        sys.exit(f"no pattern {a.key!r}")
    for k in ("key", "label", "status", "n_sessions", "n_success", "success_outcomes", "deterministic_frac",
              "suggested_type", "method", "created_at", "updated_at"):
        print(f"{k:19} {r.get(k)}")
    print(f"{'description':19} {r['description']}")
    print(f"{'type_rationale':19} {textwrap.fill(r['type_rationale'] or '', 100, subsequent_indent=' ' * 20)}")
    print("\nmember sessions:")
    for sid in r["session_ids"]:
        summ = store.load_summary(sid) or {"summary": {}}
        inj = " [INJECTION SUSPECTED - excluded]" if sid in r.get("excluded_injection", []) else ""
        print(f"  {sid[:8]} {store.outcome(sid)['outcome']:9} {summ['summary'].get('intent', '?')[:90]}{inj}")


def do_summary(a):
    sid = store.resolve_session(a.session)
    r = store.load_summary(sid)
    if not r:
        sys.exit(f"{sid[:8]} has no summary yet")
    print(json.dumps(r["summary"], indent=2, ensure_ascii=False))
    print(f"\nmodel={r['model']} cost=${r['cost_usd']:.4f} created={r['created_at']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def summ_opts(p):
        p.add_argument("--limit", type=int)
        p.add_argument("--force", action="store_true", help="re-summarize even if unchanged")
        p.add_argument("--include-active", action="store_true")
        p.add_argument("--idle-minutes", type=int, default=30)
        p.add_argument("--session", nargs="+", help="only these sessions (ids or prefixes)")
        p.add_argument("--workers", type=int, default=4)

    def clus_opts(p):
        p.add_argument("--method", choices=("claude", "family"), default="claude")
        p.add_argument("--min-success", type=int, default=None,
                       help="successful sessions before a task is learned (default: auto.min_success, 3)")
        p.add_argument("--success", default="accepted,edited", help="outcomes counted as success")

    for name, fns in (("summarize", [summ_opts]), ("cluster", [clus_opts]), ("run", [summ_opts, clus_opts])):
        p = sub.add_parser(name)
        p.add_argument("--model")
        for f in fns:
            f(p)
    p = sub.add_parser("list"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("show"); p.add_argument("key")
    p = sub.add_parser("summary"); p.add_argument("session")
    a = ap.parse_args(argv)

    try:
        if a.cmd in ("summarize", "run"):
            do_summarize(a)
        if a.cmd in ("cluster", "run"):
            print()
            do_cluster(a)
        {"list": do_list, "show": do_show, "summary": do_summary}.get(a.cmd, lambda _: None)(a)
    except (LookupError, ClaudeError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
