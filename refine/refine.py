#!/usr/bin/env python3
"""Phase 5: track how installed artifacts are used, and recommend revision or retirement.

  refine.py usage [<name>]                     uses, failures and missed opportunities per installed item
  refine.py record <name> ok|fail [--note T]   log a use the harness can't see (e.g. run by path)
  refine.py report [--stale-days 60] [--min-failures 2] [--fail-rate 0.3]
  refine.py revise <name> [--model M]          draft v(N+1) from the failures (normal review/approve applies)
  refine.py expose <script> [--model M]        draft an MCP tool that wraps an installed script (no new code)
  refine.py learn [session]                    quick learn: draft from ONE session now (default: this/last session)
  refine.py learn --list [session]             number the session's requests, to pick a part of it
  refine.py learn [session] --requests 7-9 [--focus TEXT]   learn only that part of the session
  refine.py confirm <pattern> --accepted ID... [--redone ID...]   record what the user said about unclear
                                               sessions; learns the task right away if it now qualifies
  refine.py promote <skill> [--as tool|script] turn a well-practised skill into a tool or script (muscle memory)
  refine.py demote <tool|script>               bring back the skill a promoted tool or script replaced
  refine.py fix-triggers <skill>               brief a description fix for a skill Claude didn't use when it should
                                               have (the background run does this after 2 missed sessions)
  refine.py prune [--days N] [--dry-run]       delete old one-off sessions that are evidence for nothing
  refine.py auto [--force]                     one automatic-learning run (normally started by the SessionEnd hook)
  refine.py auto-status                        settings, budget, last run, drafts waiting
  refine.py auto-config [--budget USD] [--every 6h] [--after-sessions N] [--learn-after N] [--max-drafts N]
                        [--keep-days N] [--on|--off] [--reset NAME... | --reset all]
                                               change automatic learning's settings (no options: show them)
  refine.py cycle [--generate N] [--no-detect] one maintenance pass: detect -> draft new candidates -> report

Nothing here installs, uninstalls or changes an artifact. It writes reports
and drafts. Every change still goes through `review/cli.py approve` or
`registry/install.py uninstall`.

Where usage comes from:
  1. store/usage/<name>.jsonl    one line per run, written by the bin/<name> wrapper or the MCP server
                                 (exit code, terminal / claude / mcp, and Claude's session id)
  2. session transcripts         tool steps that call bin/<name>, scripts/<name>/..., or Skill(<name>)
  3. manual records              `refine.py record`
A use that the wrapper logged under a Claude session isn't double-counted from the transcript.
A use fails when: the wrapper saw a non-zero exit, the tool step errored, or the
session was rated `redone`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402
from harness_lib import tools as rt  # noqa: E402
from registry import install as reg  # noqa: E402


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def usage_log_path(name: str) -> Path:
    return store.store_dir() / "usage" / f"{name}.jsonl"


def _matchers(entry: dict):
    name = re.escape(entry["name"])
    if entry["type"] == "skill":
        rx = re.compile(rf"(^|:){name}$")
        return lambda st: st["tool_name"] == "Skill" and bool(rx.search(json.loads(st["input_json"]).get("skill", "")))
    if entry["type"] == "tool":
        mcp_name = f"mcp__{rt.SERVER_NAME}__{entry['name']}"
        return lambda st: st["tool_name"] == mcp_name
    rx = re.compile(rf"(bin/{name}\b|scripts/{name}/)")
    return lambda st: st["tool_name"] == "Bash" and bool(rx.search(st["input_json"]))


def collect_uses(entry: dict) -> list[dict]:
    """All observed uses of one installed artifact, oldest first."""
    since = _ts(entry["approved_at"])
    uses = []
    shim_sessions = set()
    for r in store.read_jsonl(usage_log_path(entry["name"])):
        sid = r.get("session_id") if r.get("via") in ("claude", "mcp") else None
        if sid:
            shim_sessions.add(sid)
        rejected = r.get("rejected")
        uses.append({"source": "mcp" if r.get("via") == "mcp" else "wrapper", "at": r["at"],
                     "version": r.get("version"), "via": r.get("via"), "session_id": sid, "exit": r.get("exit"),
                     "failed": bool(rejected) or r.get("exit") not in (0, None),
                     "detail": f"rejected arguments: {rejected}" if rejected else f"exit {r.get('exit')}"})
    for r in store.read_jsonl(store.store_dir() / "usage" / "_manual.jsonl"):
        if r.get("name") == entry["name"]:
            uses.append({"source": "manual", "at": r["at"], "via": "manual", "session_id": None,
                         "failed": r["result"] == "fail", "detail": r.get("note") or r["result"]})
    match = _matchers(entry)
    for s in store.iter_sessions(segments=False):   # a segment would count its parent's uses again
        started = _ts(s.get("started_at"))
        if since and started and started < since - timedelta(minutes=5):
            continue
        for st in s["steps"]:
            if not match(st):
                continue
            if s["session_id"] in shim_sessions:
                continue  # already counted (with the exact exit code) from the wrapper log
            uses.append({"source": "transcript", "at": st.get("timestamp") or s.get("started_at"), "via": "claude",
                         "session_id": s["session_id"], "failed": bool(st.get("is_error")),
                         "detail": (st.get("result_preview") or "")[:300], "step_input": st["input_json"][:1500]})
    # a session rated `redone` makes every use in it a failure
    for u in uses:
        if u.get("session_id"):
            o = store.outcome(u["session_id"])
            u["session_outcome"] = o["outcome"]
            u["session_note"] = o.get("note")
            if o["outcome"] == "redone":
                u["failed"] = True
    return sorted(uses, key=lambda u: u["at"] or "")


def missed_opportunities(entry: dict, uses: list[dict]) -> list[str]:
    """Sessions after install that the clusterer put in this pattern but that never used the artifact."""
    pattern = store.load_pattern(entry["pattern_key"]) or {}
    since = _ts(entry["approved_at"])
    used_in = {u["session_id"] for u in uses if u.get("session_id")}
    # a session that used a sibling artifact of the same pattern (e.g. the MCP tool wrapping this script)
    # did not miss it
    for other in reg.load_registry()["installed"].values():
        if other["name"] != entry["name"] and other["pattern_key"] == entry["pattern_key"]:
            used_in |= {u["session_id"] for u in collect_uses(other) if u.get("session_id")}
    match = _matchers(entry)
    out = []
    for sid in pattern.get("session_ids", []):
        s = store.load_session(sid) or {}
        started = _ts(s.get("started_at"))
        if not (since and started and started > since) or sid in used_in:
            continue
        if store.is_segment(sid) and any(match(st) for st in s.get("steps", [])):
            continue   # uses are counted on the whole session, so a part that used it looks at its own steps
        out.append(sid)
    return out


def stats(entry: dict) -> dict:
    uses = collect_uses(entry)
    fails = [u for u in uses if u["failed"]]
    last = uses[-1]["at"] if uses else None
    return {"name": entry["name"], "type": entry["type"], "version": entry["version"],
            "approved_at": entry["approved_at"], "uses": len(uses), "failures": len(fails),
            "fail_rate": round(len(fails) / len(uses), 3) if uses else None,
            "by_via": {v: sum(u["via"] == v for u in uses) for v in ("terminal", "claude", "mcp", "manual")},
            "last_used": last, "missed": missed_opportunities(entry, uses), "uses_detail": uses}


def recommend(st: dict, *, now: datetime, stale_days: int, min_failures: int, fail_rate: float) -> dict:
    reasons = []
    action = "keep"
    if st["failures"] >= min_failures and (st["fail_rate"] or 0) >= fail_rate:
        action = "revise"
        reasons.append(f"{st['failures']} of {st['uses']} uses failed ({st['fail_rate']:.0%})")
    ref = _ts(st["last_used"]) or _ts(st["approved_at"])
    idle = (now - ref).days if ref else 0
    if action == "keep" and idle >= stale_days and not st["missed"]:
        action = "retire"
        reasons.append(f"unused for {idle} days and the pattern hasn't recurred")
    if st["missed"]:
        reasons.append(f"{len(st['missed'])} later session(s) did this task without using it"
                       + (f" (Claude may not know it exists; `refine.py expose {st['name']}` makes it an MCP tool)"
                          if st["type"] == "script" else
                          f" (its description doesn't match how you ask; `refine.py fix-triggers {st['name']}` "
                          f"briefs a fix, as the background run does after 2)" if st["type"] == "skill" else ""))
        if action == "keep":
            action = "missed"
    if not reasons:
        reasons.append(f"{st['uses']} use(s), {st['failures']} failure(s)")
    return {"action": action, "reasons": reasons}


def reconsider_rejected() -> list[dict]:
    out = []
    for p in store.iter_patterns():
        if p.get("status") != "rejected":
            continue
        then = (p.get("rejected") or {}).get("n_success_at_rejection")
        if then and p.get("n_success", 0) >= 2 * then:
            out.append({"pattern": p["key"], "action": "reconsider",
                        "reasons": [f"rejected at {then} successes, now {p['n_success']} "
                                    f"(reason given: {p['rejected'].get('reason')!r}); propose.py --force to redraft"]})
    return out


def build_report(*, stale_days=60, min_failures=2, fail_rate=0.3, now=None) -> dict:
    from refine import memory
    now = now or datetime.now(timezone.utc)
    items = []
    for entry in reg.load_registry()["installed"].values():
        st = stats(entry)
        rec = recommend(st, now=now, stale_days=stale_days, min_failures=min_failures, fail_rate=fail_rate)
        if entry["type"] == "skill":
            state, why = memory.promotion_status(entry, uses=st["uses_detail"])
            if state == "ready" and rec["action"] in ("keep", "missed"):
                rec["action"] = "promote"
            rec["reasons"].append(why)
            st["level"] = "thinking"
        else:
            st["level"] = "automatic" if entry["type"] in ("tool", "script") else entry["type"]
        if entry.get("promoted_from") and rec["action"] == "revise":
            rec["action"] = "demote"
            rec["reasons"].append(f"it replaced the skill {entry['promoted_from']['skill']}; "
                                  f"demoting brings that skill back")
        items.append({**{k: v for k, v in st.items() if k != "uses_detail"}, **rec})
    rep = {"generated_at": store.now(), "thresholds": {"stale_days": stale_days, "min_failures": min_failures,
                                                       "fail_rate": fail_rate},
           "installed": items, "rejected": reconsider_rejected()}
    store.write_json_atomic(store.store_dir() / "refine_report.json", rep)
    return rep


def print_report(rep: dict) -> None:
    print(f"{'action':10} {'type':7} {'ver':>3} {'uses':>4} {'fail':>4}  name")
    for it in rep["installed"]:
        print(f"{it['action']:10} {it['type']:7} {it['version']:>3} {it['uses']:>4} {it['failures']:>4}  {it['name']}")
        for r in it["reasons"]:
            print(f"{'':12}- {r}")
        nxt = {"revise": f"python3 refine/refine.py revise {it['name']}",
               "promote": f"python3 refine/refine.py promote {it['name']}",
               "demote": f"python3 refine/refine.py demote {it['name']}",
               "retire": f"python3 registry/install.py uninstall {it['name']} --reason 'unused'"}.get(it["action"])
        if nxt:
            print(f"{'':12}next: {nxt}")
    for it in rep["rejected"]:
        print(f"{'reconsider':10} {'':7} {'':>3} {'':>4} {'':>4}  {it['pattern']}")
        for r in it["reasons"]:
            print(f"{'':12}- {r}")
    if not rep["installed"] and not rep["rejected"]:
        print("nothing installed yet")


def revision_input(entry: dict) -> dict:
    """Current files, previous tests and concrete failures for a revision draft."""
    base = reg.install_root() / entry["path"]
    files = {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8") for p in sorted(base.rglob("*"))
             if p.is_file()}
    draft = store.read_json(reg.resolve(entry["draft"]) / "proposal.json") or {}
    failures = []
    for u in collect_uses(entry):
        if not u["failed"]:
            continue
        f = {"source": u["source"], "at": u["at"], "detail": u.get("detail")}
        if u.get("step_input"):
            f["command"] = u["step_input"]
        if u.get("session_note"):
            f["user_note"] = u["session_note"]
        failures.append(f)
    return {"name": entry["name"], "type": entry["type"], "version": entry["version"],
            "entrypoint": entry.get("entrypoint"), "files": files, "failures": failures[-20:],
            "previous_tests": draft.get("tests", [])}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("usage"); p.add_argument("name", nargs="?")
    p = sub.add_parser("record"); p.add_argument("name"); p.add_argument("result", choices=("ok", "fail"))
    p.add_argument("--note")
    p = sub.add_parser("report")
    for q in (p, sub.add_parser("cycle")):
        q.add_argument("--stale-days", type=int, default=60)
        q.add_argument("--min-failures", type=int, default=2)
        q.add_argument("--fail-rate", type=float, default=0.3)
    q.add_argument("--generate", type=int, default=1, help="draft at most N new candidate patterns")
    q.add_argument("--no-detect", action="store_true")
    p = sub.add_parser("revise"); p.add_argument("name"); p.add_argument("--model")
    p.add_argument("--max-attempts", type=int, default=2)
    p = sub.add_parser("expose"); p.add_argument("name"); p.add_argument("--model")
    p.add_argument("--max-attempts", type=int, default=2)
    p = sub.add_parser("learn"); p.add_argument("session", nargs="?", help="session id or prefix (default: this one)")
    p.add_argument("--model")
    p.add_argument("--list", action="store_true", help="number the session's requests and stop")
    p.add_argument("--requests", metavar="N-M", help="learn only requests N..M (from --list), e.g. 7-9, 7 or 7-")
    p.add_argument("--focus", metavar="TEXT", help="what that part does, in the user's words (with --requests)")
    p = sub.add_parser("prune"); p.add_argument("--days", type=int); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("brief"); p.add_argument("pattern", help="pattern key to brief for skill-creator")
    p = sub.add_parser("confirm"); p.add_argument("pattern")
    p.add_argument("--accepted", nargs="*", default=[]); p.add_argument("--redone", nargs="*", default=[])
    p = sub.add_parser("promote"); p.add_argument("name"); p.add_argument("--model")
    p.add_argument("--as", dest="as_type", choices=["tool", "script"],
                   help='what to turn the skill into (default: "promote_as" in the auto config, else tool)')
    p = sub.add_parser("demote"); p.add_argument("name"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("fix-triggers"); p.add_argument("name", help="an installed skill")
    p = sub.add_parser("auto"); p.add_argument("--force", action="store_true", help="ignore the throttle")
    sub.add_parser("auto-status")
    p = sub.add_parser("auto-config", help="show or change automatic learning's settings")
    p.add_argument("--budget", metavar="USD", help="monthly budget for background learning (default 5)")
    p.add_argument("--every", metavar="TIME", help="at most one background run per TIME: 6h, 90m, 1d (default 6h)")
    p.add_argument("--after-sessions", metavar="N", help="finished sessions needed since the last run (default 3)")
    p.add_argument("--learn-after", metavar="N", help="successful repeats before a task is learned (default 3)")
    p.add_argument("--max-drafts", metavar="N", help="drafts per background run (default 1)")
    p.add_argument("--keep-days", metavar="N", help="days before a finished one-off session is forgotten (default 90)")
    onoff = p.add_mutually_exclusive_group()
    onoff.add_argument("--on", dest="enabled", action="store_const", const=True)
    onoff.add_argument("--off", dest="enabled", action="store_const", const=False)
    p.add_argument("--reset", nargs="+", metavar="NAME", default=[], help="back to the default: a setting name or all")
    a = ap.parse_args(argv)
    installed = reg.load_registry()["installed"]

    if a.cmd == "usage":
        names = [a.name] if a.name else sorted(installed)
        for n in names:
            if n not in installed:
                sys.exit(f"error: {n!r} is not installed")
            st = stats(installed[n])
            print(f"{n} v{st['version']} ({st['type']}): {st['uses']} uses, {st['failures']} failures, "
                  f"by {st['by_via']}, last used {st['last_used'] or 'never'}")
            for u in st["uses_detail"]:
                sid = f" session {u['session_id'][:8]} ({u.get('session_outcome')})" if u.get("session_id") else ""
                print(f"  {'FAIL' if u['failed'] else 'ok  '} {u['at'][:19]} {u['source']:10} {u['via']:8}{sid} "
                      f"{(u.get('detail') or '')[:60]}")
            if st["missed"]:
                print(f"  missed in: {', '.join(s[:8] for s in st['missed'])}")
        if not names:
            print("nothing installed yet")
    elif a.cmd == "record":
        if a.name not in installed:
            sys.exit(f"error: {a.name!r} is not installed")
        store.append_jsonl(store.store_dir() / "usage" / "_manual.jsonl",
                           {"name": a.name, "result": a.result, "note": a.note, "at": store.now()})
        print(f"recorded {a.result} for {a.name}")
    elif a.cmd == "report":
        print_report(build_report(stale_days=a.stale_days, min_failures=a.min_failures, fail_rate=a.fail_rate))
    elif a.cmd == "revise":
        if a.name not in installed:
            sys.exit(f"error: {a.name!r} is not installed")
        entry = installed[a.name]
        if entry.get("wraps"):
            sys.exit(f"error: {a.name!r} only wraps the script {entry['wraps']!r}; revise that instead")
        if entry["type"] == "skill":   # skills are revised with skill-creator, from a brief
            from refine.brief import write_brief
            rev = revision_input(entry)
            b = write_brief(entry["pattern_key"], improve={**entry, "failures": rev["failures"]})
            print(f"skills are updated with skill-creator. Brief: {b['path']}/BRIEF.md\n"
                  f"In a Claude Code session say: \"update the {a.name} skill with skill-creator using that brief\", "
                  f"then run: python3 review/cli.py adopt {a.name}")
            return 0
        rev = revision_input(entry)
        if not rev["failures"]:
            sys.exit(f"error: no recorded failures for {a.name}; nothing to revise")
        from generate.propose import propose
        from harness_lib.claude_cli import ClaudeError
        print(f"revising {a.name} v{entry['version']} from {len(rev['failures'])} failure(s); "
              f"{len(rev['previous_tests'])} existing tests become regression tests")
        try:
            r = propose(entry["pattern_key"], model=a.model, max_attempts=a.max_attempts, revision=rev)
        except (LookupError, ValueError, ClaudeError) as e:
            sys.exit(f"error: {e}")
        print(f"\n{'VALIDATED' if r['passed'] else 'NOT VALIDATED'}: {r['draft']}  (${r['cost_usd']:.3f})")
        if r["passed"]:
            name = Path(r["draft"]).name
            print(f"review: python3 review/cli.py diff {name}  then  python3 review/cli.py approve {name}")
        return 0 if r["passed"] else 1
    elif a.cmd == "learn":
        from harness_lib.claude_cli import ClaudeError
        from refine.learn import LearnError, learn, list_requests
        if a.list:
            try:
                sid, listing = list_requests(a.session)
            except (LearnError, LookupError) as e:
                sys.exit(f"error: {e}")
            print(f"session {sid}: learn a part with --requests N-M\n{listing}")
            return 0
        if a.focus and not a.requests:
            sys.exit("error: --focus describes the part chosen with --requests")
        try:
            r = learn(a.session, requests=a.requests, focus=a.focus, model=a.model)
        except (LearnError, LookupError, ValueError, ClaudeError) as e:
            sys.exit(f"error: {e}")
        if r.get("brief"):
            print(f"\nSKILL BRIEF: {r['brief_path']}/BRIEF.md\n"
                  f"This task needs judgment, so it becomes a skill, and skills are built with skill-creator. Ask "
                  f"Claude to build it with skill-creator from that brief, then run: "
                  f"python3 review/cli.py adopt {r['brief']}")
            return 0
        name = Path(r["draft"]).name if r.get("draft") else None
        print(f"\n{'VALIDATED' if r['passed'] else 'NOT VALIDATED'}: {r.get('draft') or '(no draft)'}  (${r['cost_usd']:.3f})")
        if r["passed"]:
            print(f"review: python3 review/cli.py review {name}   approve: python3 review/cli.py approve {name}")
        else:
            for p in r["problems"][:8]:
                print("  -", p)
        return 0 if r["passed"] else 1
    elif a.cmd == "prune":
        from refine import auto
        days = a.days if a.days is not None else int(auto.config()["keep_days"])
        gone = auto.prune_sessions(days, dry_run=a.dry_run)
        print(f"{'would delete' if a.dry_run else 'deleted'} {len(gone)} one-off session(s) older than {days} days"
              + (": " + ", ".join(s[:8] for s in gone[:20]) if gone else ""))
        return 0
    elif a.cmd == "confirm":
        from harness_lib.claude_cli import ClaudeError
        from refine import auto
        try:
            r = auto.confirm(a.pattern, accepted=a.accepted, redone=a.redone)
        except (LookupError, ValueError, ClaudeError) as e:
            sys.exit(f"error: {e}")
        if r["status"] == "forming":
            print(f"recorded; {r['n_success']} of {r['needed']} successful sessions so far")
        elif r.get("brief"):
            print(f"SKILL BRIEF: {r['brief_path']}/BRIEF.md\nbuild it with skill-creator, then run: "
                  f"python3 review/cli.py adopt {r['brief']}")
        else:
            print(f"{'VALIDATED' if r.get('passed') else 'NOT VALIDATED'} draft: {r.get('draft')}")
            for p in r.get("problems") or []:
                print("  -", p)
        return 0
    elif a.cmd == "brief":
        from refine.brief import write_brief
        try:
            b = write_brief(a.pattern)
        except LookupError as e:
            sys.exit(f"error: {e}")
        print(f"brief: {b['path']}/BRIEF.md  ({len(b['cases'])} test case(s) for skill-creator)\n"
              f"build it with skill-creator, writing into {b['target']}/, then: python3 review/cli.py adopt {b['brief']}")
        return 0
    elif a.cmd == "promote":
        from harness_lib.claude_cli import ClaudeError
        from refine import memory
        try:
            r = memory.promote(a.name, as_type=a.as_type, model=a.model)
        except (LookupError, ValueError, ClaudeError) as e:
            sys.exit(f"error: {e}")
        name = Path(r["draft"]).name if r.get("draft") else None
        print(f"\n{'VALIDATED' if r['passed'] else 'NOT VALIDATED'}: {r.get('draft')}  (${r['cost_usd']:.3f})")
        if r["passed"]:
            print(f"review: python3 review/cli.py review {name}   approve (retires the skill): "
                  f"python3 review/cli.py approve {name}")
        return 0 if r["passed"] else 1
    elif a.cmd == "fix-triggers":
        from refine import auto
        entry = installed.get(a.name)
        if not entry or entry["type"] != "skill":
            sys.exit(f"error: {a.name!r} is not an installed skill")
        uses = collect_uses(entry)
        missed = missed_opportunities(entry, uses)
        if not missed:
            sys.exit(f"error: no session since v{entry['version']} did {a.name}'s task without using it, so there's "
                     f"nothing to fix the description from")
        failures, acc = auto._corrections(entry, uses)
        corrections = len(failures) >= 2 and acc is not None and acc < 0.8
        b = auto.write_update_brief(entry, failures=failures if corrections else [], missed=missed,
                                    sessions=sorted({u["session_id"] for u in uses if u.get("session_id")}))
        print(f"UPDATE BRIEF: {b['path']}/BRIEF.md  ({len(missed)} missed session(s); trigger eval set in "
              f"{b['path']}/evals/trigger-eval.json)\n"
              f"Ask Claude to update the skill with skill-creator from that brief, then run: "
              f"python3 review/cli.py adopt {b['brief']}")
        return 0
    elif a.cmd == "demote":
        from refine import memory
        if not a.yes and not (sys.stdin.isatty() and input(f"Bring back the skill and remove {a.name}? [y/N] ")
                              .strip().lower() in ("y", "yes")):
            print("cancelled")
            return 1
        try:
            r = memory.demote(a.name)
        except LookupError as e:
            sys.exit(f"error: {e}")
        print(f"restored skill {r['skill']} (v{r['skill_version']}) and removed {r['removed_type']} "
              f"{r['removed_tool']}")
        return 0
    elif a.cmd == "auto":
        from refine import auto
        r = auto.run(force=a.force)
        if not r["ran"]:
            print(f"not run: {r['reason']}")
        return 0
    elif a.cmd == "auto-config":
        from refine import auto
        values = {n: getattr(a, n.replace("-", "_")) for n in auto.SETTINGS}
        changing = a.enabled is not None or a.reset or any(v is not None for v in values.values())
        if changing:
            try:
                auto.configure(enabled=a.enabled, reset=a.reset, **values)
            except ValueError as e:
                sys.exit(f"error: {e}")
        cfg = auto.config()
        print(f"automatic learning: {'ON' if cfg['enabled'] else 'off'}" + ("  (saved)" if changing else ""))
        for name, shown, meaning, default in auto.describe_settings():
            print(f"  --{name:15} {shown:>8}  {meaning}{'' if default else '  [changed]'}")
        if not changing:
            print("change one with e.g.: python3 refine/refine.py auto-config --budget 10 --every 4h")
        return 0
    elif a.cmd == "auto-status":
        from refine import auto
        from harness_lib.claude_cli import month_spend
        cfg, st = auto.config(), auto.state()
        print(f"automatic learning: {'ON' if cfg['enabled'] else 'off'}")
        print(f"budget: ${month_spend():.2f} of ${cfg['monthly_budget_usd']:.2f} used this month")
        print(f"throttle: at most every {auto._hours_text(cfg['min_hours_between_runs'])}, after "
              f"{cfg['min_new_sessions']}+ new sessions; a task is learned after {cfg['min_success']} successful repeat(s)")
        print("change settings: python3 refine/refine.py auto-config")
        ok, why = auto.should_run()
        print(f"next run: {'due now' if ok else why}")
        lr = st.get("last_result") or {}
        print(f"last successful run: {st.get('last_run_at', 'never')}")
        if st.get("last_error"):
            print(f"last attempt FAILED at {st.get('last_attempt_at')}: {st['last_error'][:200]}  (details: store/auto.log)")
        elif lr:
            print(f"last run spent ${lr.get('spent_usd', 0):.3f}")
        for i in auto.inbox():
            print(f"waiting for you: {i['type']} {i['draft']}  ->  python3 review/cli.py review {i['draft']}")
        return 0
    elif a.cmd == "expose":
        from harness_lib.claude_cli import ClaudeError
        from refine.expose import expose
        try:
            r = expose(a.name, model=a.model, max_attempts=a.max_attempts)
        except (LookupError, ValueError, ClaudeError) as e:
            sys.exit(f"error: {e}")
        print(f"\n{'VALIDATED' if r['passed'] else 'NOT VALIDATED'}: {r['draft']}  (${r['cost_usd']:.3f})")
        from validate import replay
        replay.print_report(r["report"])
        if r["passed"]:
            print(f"review: python3 review/cli.py review {r['name']}  then  python3 review/cli.py approve {r['name']}")
        return 0 if r["passed"] else 1
    elif a.cmd == "cycle":
        if not a.no_detect:
            from detect import find_patterns
            print("== detect")
            find_patterns.main(["run"])
        print("\n== draft new candidates")
        cands = [p for p in store.iter_patterns() if p["status"] == "candidate"][: max(0, a.generate)]
        if not cands:
            print("no new candidates")
        from generate.propose import propose
        from harness_lib.claude_cli import ClaudeError
        for p in cands:
            try:
                r = propose(p["key"])
                print(f"  {p['key']}: {'validated' if r['passed'] else 'not validated'} -> {r['draft']}")
            except (LookupError, ValueError, ClaudeError) as e:
                print(f"  {p['key']}: error {e}")
        print("\n== refine report")
        print_report(build_report(stale_days=a.stale_days, min_failures=a.min_failures, fail_rate=a.fail_rate))
        print("\nNothing was installed. Review drafts with: python3 review/cli.py proposals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
