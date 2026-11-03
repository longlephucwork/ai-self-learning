#!/usr/bin/env python3
"""Review CLI for the learning harness (reads the file store in store/sessions/).

Phase 1 commands:
  list [-n N]                       recent sessions with outcome
  show <session|prefix|last>        requests, tool steps, files, final result
  rate <session|prefix|last> <accepted|edited|redone|unknown> [--note TEXT]
  stats                             totals, tool usage, outcome breakdown

Phase 4 will add: proposals, approve, reject.
"""
from __future__ import annotations

import argparse
import collections
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402


def _one_line(s: str | None, n: int = 70) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_list(args):
    recs = sorted(store.iter_sessions(), key=lambda r: r.get("last_activity_at") or "", reverse=True)[: args.n]
    if not recs:
        print("no sessions stored yet")
        return
    print(f"{'session':10} {'last activity':20} {'req':>3} {'tools':>5} {'outcome':9} {'ended':10} first request")
    for r in recs:
        o = store.outcome(r["session_id"])["outcome"]
        print(f"{r['session_id'][:8]:10} {(r['last_activity_at'] or '')[:19]:20} {r['n_requests']:>3} "
              f"{r['n_tool_calls']:>5} {o:9} {(r['end_reason'] or '-'):10} {_one_line(r['first_request'], 60)}")


def cmd_show(args):
    sid = store.resolve_session(args.session)
    s = store.load_session(sid)
    o = store.outcome(sid)
    print(f"session   {sid}")
    print(f"{'folder':17} {store.session_dir(sid)}")
    for k in ("cwd", "git_branch", "cc_version", "entrypoint", "models", "started_at", "last_activity_at",
              "ended_at", "end_reason", "n_requests", "n_tool_calls", "n_tool_errors", "ingest_count"):
        print(f"{k:17} {s.get(k)}")
    print(f"{'outcome':17} {o['outcome']} ({o['source'] or 'unrated'}){' — ' + o['note'] if o['note'] else ''}")

    print("\n== requests")
    for r in s["requests"]:
        print(f"[{r['idx']}] {textwrap.shorten(r['text'], 300)}")

    print("\n== tool steps")
    for st in s["steps"]:
        flag = " ERROR" if st["is_error"] else ""
        print(f"{st['idx']:>3}. {st['tool_name']:14}{flag} {_one_line(st['input_json'], 100 if args.verbose else 80)}")

    if s["files_touched"]:
        print("\n== files touched")
        for f in sorted(s["files_touched"], key=lambda f: (f["action"], f["path"])):
            print(f"  {f['action']:5} x{f['count']}  {f['path']}")

    print("\n== final result")
    fr = s.get("final_result") or "(none)"
    print(textwrap.indent(fr if args.verbose else textwrap.shorten(fr, 1200), "  "))


def cmd_rate(args):
    sid = store.resolve_session(args.session)
    store.rate(sid, args.outcome, args.note)
    print(f"{sid[:8]} -> {args.outcome}")


def cmd_stats(args):
    recs = store.iter_sessions()
    print(f"sessions: {len(recs)}")
    print("outcomes:", dict(collections.Counter(store.outcome(r["session_id"])["outcome"] for r in recs)))
    calls, errs = collections.Counter(), collections.Counter()
    for r in recs:
        for st in r["steps"]:
            calls[st["tool_name"]] += 1
            errs[st["tool_name"]] += bool(st["is_error"])
    print("top tools:")
    for name, c in calls.most_common(15):
        print(f"  {name:20} {c:>5}  errors={errs[name]}")
    log = store.store_dir() / "hook_errors.log"
    if log.exists() and log.stat().st_size:
        print(f"hook errors logged: {sum(1 for _ in open(log))} (see {log})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("-n", type=int, default=20); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("session"); p.add_argument("-v", "--verbose", action="store_true"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("rate"); p.add_argument("session"); p.add_argument("outcome", choices=store.OUTCOMES)
    p.add_argument("--note"); p.set_defaults(fn=cmd_rate)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
