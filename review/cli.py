#!/usr/bin/env python3
"""Review CLI for the learning harness.

Phase 1 commands:
  list [-n N]                       recent sessions with outcome
  show <session|prefix|last>        requests, tool steps, files, final result
  rate <session|prefix|last> <accepted|edited|redone|unknown> [--note TEXT]
  stats                             totals, tool usage, outcome breakdown

Phase 4 will add: proposals, approve, reject.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import db  # noqa: E402


def _one_line(s: str | None, n: int = 70) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_list(con, args):
    rows = con.execute(
        """SELECT s.session_id, s.last_activity_at, s.n_requests, s.n_tool_calls, s.end_reason,
                  s.first_request, o.outcome
           FROM sessions s JOIN session_outcomes o USING (session_id)
           ORDER BY s.last_activity_at DESC LIMIT ?""", (args.n,)).fetchall()
    if not rows:
        print("no sessions stored yet")
        return
    print(f"{'session':10} {'last activity':20} {'req':>3} {'tools':>5} {'outcome':9} {'ended':10} first request")
    for r in rows:
        print(f"{r['session_id'][:8]:10} {(r['last_activity_at'] or '')[:19]:20} {r['n_requests']:>3} "
              f"{r['n_tool_calls']:>5} {r['outcome']:9} {(r['end_reason'] or '-'):10} {_one_line(r['first_request'], 60)}")


def cmd_show(con, args):
    sid = db.resolve_session(con, args.session)
    s = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    o = con.execute("SELECT * FROM session_outcomes WHERE session_id=?", (sid,)).fetchone()
    print(f"session   {sid}")
    for k in ("cwd", "git_branch", "cc_version", "entrypoint", "models", "started_at", "last_activity_at",
              "ended_at", "end_reason", "n_requests", "n_tool_calls", "n_tool_errors", "ingest_count",
              "transcript_copy"):
        print(f"{k:17} {s[k]}")
    print(f"{'outcome':17} {o['outcome']} ({o['source'] or 'unrated'}){' — ' + o['note'] if o['note'] else ''}")

    print("\n== requests")
    for r in con.execute("SELECT * FROM requests WHERE session_id=? ORDER BY idx", (sid,)):
        print(f"[{r['idx']}] {textwrap.shorten(r['text'], 300)}")

    print("\n== tool steps")
    for st in con.execute("SELECT * FROM steps WHERE session_id=? ORDER BY idx", (sid,)):
        flag = " ERROR" if st["is_error"] else ""
        print(f"{st['idx']:>3}. {st['tool_name']:14}{flag} {_one_line(st['input_json'], 100 if args.verbose else 80)}")

    files = con.execute("SELECT * FROM files_touched WHERE session_id=? ORDER BY action, path", (sid,)).fetchall()
    if files:
        print("\n== files touched")
        for f in files:
            print(f"  {f['action']:5} x{f['count']}  {f['path']}")

    print("\n== final result")
    print(textwrap.indent(textwrap.shorten(s["final_result"] or "(none)", 1200) if not args.verbose
                          else (s["final_result"] or "(none)"), "  "))


def cmd_rate(con, args):
    sid = db.resolve_session(con, args.session)
    db.rate(con, sid, args.outcome, args.note)
    print(f"{sid[:8]} -> {args.outcome}")


def cmd_stats(con, args):
    n = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(f"sessions: {n}")
    print("outcomes:", dict(con.execute("SELECT outcome, COUNT(*) FROM session_outcomes GROUP BY outcome").fetchall()))
    print("top tools:")
    for r in con.execute("SELECT tool_name, COUNT(*) c, SUM(is_error) e FROM steps GROUP BY tool_name ORDER BY c DESC LIMIT 15"):
        print(f"  {r['tool_name']:20} {r['c']:>5}  errors={r['e'] or 0}")
    errs = db.store_dir() / "hook_errors.log"
    if errs.exists() and errs.stat().st_size:
        print(f"hook errors logged: {sum(1 for _ in open(errs))} (see {errs})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("-n", type=int, default=20); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("session"); p.add_argument("-v", "--verbose", action="store_true"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("rate"); p.add_argument("session"); p.add_argument("outcome", choices=db.OUTCOMES)
    p.add_argument("--note"); p.set_defaults(fn=cmd_rate)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)
    args = ap.parse_args(argv)
    con = db.connect()
    try:
        args.fn(con, args)
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
