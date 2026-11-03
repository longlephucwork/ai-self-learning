#!/usr/bin/env python3
"""Review CLI for the learning harness (reads the file store in store/sessions/).

Phase 1 commands:
  list [-n N]                       recent sessions with outcome
  show <session|prefix|last>        requests, tool steps, files, final result
  rate <session|prefix|last> <accepted|edited|redone|unknown> [--note TEXT]
  stats                             totals, tool usage, outcome breakdown

Phase 4 commands (proposals from generated/):
  proposals                         drafts with status, evidence, tests, flags
  review <draft> [--brief]          evidence, spec, full code, scan flags, test results
  diff <draft>                      changes vs the installed version (or the full new files)
  revalidate <draft>                re-run validation after hand-editing a draft's files/tests
  approve <draft> [--note TEXT] [--yes]   re-validate, then install + git commit (asks you to confirm)
  reject <draft> --reason TEXT      lock the pattern as rejected so it is never re-proposed
  adopt <brief> [--from DIR]        a skill built with skill-creator from a brief -> validated draft
"""
from __future__ import annotations

import argparse
import collections
import difflib
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402
from harness_lib.scan import blocking, scan_files  # noqa: E402
from registry import install as reg  # noqa: E402
from validate import replay  # noqa: E402


def _one_line(s: str | None, n: int = 70) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_list(args):
    recs = sorted(store.iter_sessions(), key=lambda r: r.get("last_activity_at") or "", reverse=True)[: args.n]
    if not recs:
        print("no sessions stored yet")
        return
    print("(~ = rating inferred automatically; rate it yourself to override)")
    print(f"{'session':10} {'last activity':20} {'req':>3} {'tools':>5} {'outcome':9} {'ended':10} first request")
    for r in recs:
        oc = store.outcome(r["session_id"])
        o = oc["outcome"] + ("~" if oc.get("source") == "inferred" else "")
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


# ---------------- Phase 4: review ----------------

def _draft(name):
    try:
        return replay.load_draft(name)
    except LookupError as e:
        raise LookupError(f"{e} (see `cli.py proposals`)")


def cmd_proposals(args):
    rows = []
    for d in sorted(replay.drafts_dir().iterdir()):
        m = store.read_json(d / "proposal.json") if d.is_dir() else None
        if m:
            v = store.read_json(d / "validation.json") or {}
            rows.append((m, v))
    if not rows:
        print("no proposals (run generate/propose.py <pattern>)")
        return
    print(f"{'status':18} {'type':7} {'seen':>4} {'ok':>3} {'tests':>6} {'flags':>5}  name")
    for m, v in rows:
        t = v.get("tests", [])
        print(f"{m['status']:18} {m['type']:7} {m['evidence']['n_sessions']:>4} {m['evidence']['n_success']:>3} "
              f"{sum(x['passed'] for x in t):>3}/{len(t):<2} {len(blocking(m['scan_flags'])):>5}  {m['name']}")


def cmd_review(args):
    proposal, files, d = _draft(args.draft)
    v = store.read_json(d / "validation.json") or {}
    pattern = store.load_pattern(proposal["pattern_key"]) or {}
    ev = proposal["evidence"]
    bar = "=" * 78
    print(bar)
    print(f"PROPOSAL  {proposal['name']}   type={proposal['type']}   status={proposal['status']}")
    if proposal.get("quick"):
        print("Quick learn: drafted from a single session, so the evidence is one example")
    if proposal.get("promotes"):
        print(f"Promotion: replaces the skill {proposal['promotes']['skill']} with code (muscle memory)")
    print(f"Evidence: pattern '{proposal['pattern_key']}' seen {ev['n_sessions']} times, {ev['n_success']} successful "
          f"(counting {', '.join(pattern.get('success_outcomes', ['?']))})")
    print(bar)
    print(f"\n{proposal['description']}\n")
    print("== sessions this was learned from")
    for sid in ev["sessions"]:
        o = store.outcome(sid)
        summ = (store.load_summary(sid) or {}).get("summary", {})
        note = f"  note: {o['note']!r}" if o.get("note") else ""
        print(f"  {sid[:8]}  {o['outcome']:9} {summ.get('intent', '?')[:70]}{note}")
    if pattern.get("excluded_injection"):
        print(f"  (excluded, injection suspected: {', '.join(s[:8] for s in pattern['excluded_injection'])})")
    print(f"\n== why a {proposal['type']}")
    print(textwrap.fill(pattern.get("type_rationale", ""), 100, initial_indent="  ", subsequent_indent="  "))
    if proposal.get("type_deviation_reason"):
        print(f"  model deviated from suggested type: {proposal['type_deviation_reason']}")
    print("\n== spec")
    print(textwrap.fill(proposal["spec"], 100, initial_indent="  ", subsequent_indent="  "))

    flags = scan_files(files)  # re-scan what is on disk now
    print("\n== safety scan")
    bl = blocking(flags)
    print("  BLOCKING: " + ("none" if not bl else ""))
    for f in bl:
        print(f"    !! {f['kind']:9} {f['file']}:{f['line']}  {f['detail']}")
    for f in [f for f in flags if f not in bl]:
        print(f"    info {f['kind']:9} {f['file']}:{f['line']}  {f['detail']}")

    print("\n== tests")
    results = {t["name"]: t for t in v.get("tests", [])}
    for t in proposal["tests"]:
        r = results.get(t["name"])
        st = "PASS" if r and r["passed"] else ("FAIL" if r else "----")
        print(f"  {st}  {t['name'][:28]:28} {t['provenance']:22} from {t['source_session'][:8]}  "
              f"{t['rationale'][:50]}")
    if v.get("judged"):
        print("\n== judged checks (a model compared the skill with each accepted session; judgment, not execution)")
        for j in v["judged"]:
            print(f"  {j['verdict'].upper():5} session {j['session_id'][:8]} ({store.outcome(j['session_id'])['outcome']})"
                  f": {j['reason'][:200]}")
            for c in j.get("checks", [])[:8]:
                print(f"         {'ok ' if c['consistent'] else 'NO '} {c['rule'][:90]}")
    c = v.get("coverage", {})
    fp_now = replay.fingerprint(files, proposal["tests"])
    changed = v.get("fingerprint") != fp_now
    print(f"  coverage {c.get('covered')}/{c.get('successful_sessions')} successful sessions; validated "
          f"{v.get('validated_at', 'never')}; network isolated: {v.get('network_isolated')}")
    if changed:
        print("  NOTE: draft changed since it was validated (or predates fingerprints); approve will re-validate")
    for p in v.get("problems", []):
        print(f"  problem: {p}")

    print("\n== files")
    for path, content in files.items():
        lines = content.splitlines()
        print(f"--- {path} ({len(lines)} lines)")
        if not args.brief:
            for i, line in enumerate(lines, 1):
                print(f"{i:4}  {line}")
    print(f"\n{bar}\nDecide:  approve {proposal['name']} [--note ...]   |   reject {proposal['name']} --reason ...")


def cmd_diff(args):
    proposal, files, _ = _draft(args.draft)
    entry = reg.load_registry()["installed"].get(proposal["name"])
    base = reg.install_root() / entry["path"] if entry else None
    print(f"diff vs {'installed v' + str(entry['version']) if entry else 'nothing installed (all new)'}")
    names = set(files) | ({p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file()} if base else set())
    for n in sorted(names):
        old = (base / n).read_text(encoding="utf-8").splitlines(True) if base and (base / n).is_file() else []
        new = files.get(n, "").splitlines(True)
        sys.stdout.writelines(difflib.unified_diff(old, new, f"installed/{n}", f"draft/{n}"))


def cmd_revalidate(args):
    proposal, files, d = _draft(args.draft)
    pattern = store.load_pattern(proposal["pattern_key"])
    rep = replay.validate_draft(proposal, files, pattern, previous=store.read_json(d / "validation.json"))
    for f in blocking(scan_files(files)):
        msg = f"safety scan: {f['kind']} in {f['file']}:{f['line']} ({f['detail']})"
        if msg not in rep["problems"]:
            rep["problems"].append(msg)
    rep["passed"] = not rep["problems"]
    store.write_json_atomic(d / "validation.json", rep)
    proposal["status"] = "validated" if rep["passed"] else "failed_validation"
    proposal["scan_flags"] = scan_files(files)
    store.write_json_atomic(d / "proposal.json", proposal)
    replay.print_report(rep)
    return 0 if rep["passed"] else 1


def cmd_approve(args):
    proposal, files, d = _draft(args.draft)   # read once: these exact bytes are validated and installed
    if proposal["status"] in ("installed", "rejected"):
        raise LookupError(f"draft is already {proposal['status']}")
    pattern = store.load_pattern(proposal["pattern_key"])
    if not pattern:
        raise LookupError(f"pattern {proposal['pattern_key']!r} not found")
    if pattern["status"] not in ("proposed", "candidate", "installed"):
        raise LookupError(f"pattern is {pattern['status']!r}; not approving")

    print("re-validating the draft as it is on disk now ...")
    rep = replay.validate_draft(proposal, files, pattern, previous=store.read_json(d / "validation.json"))
    bl = blocking(scan_files(files))
    for f in bl:
        msg = f"safety scan: {f['kind']} in {f['file']}:{f['line']} ({f['detail']})"
        if msg not in rep["problems"]:
            rep["problems"].append(msg)
    rep["passed"] = not rep["problems"]
    store.write_json_atomic(d / "validation.json", rep)
    replay.print_report(rep)
    if not rep["passed"]:
        print("\nnot approved: validation failed (fix the draft, then `revalidate`)", file=sys.stderr)
        return 1
    try:
        dest = reg.target_dir(proposal["type"], proposal["name"])
    except reg.InstallError as e:
        print(f"\nnot approved: {e}", file=sys.stderr)
        return 1

    ev = proposal["evidence"]
    print(f"\nAbout to install {proposal['type']} '{proposal['name']}' -> {dest}")
    print(f"  learned from {ev['n_sessions']} sessions ({ev['n_success']} successful); "
          f"{sum(t['passed'] for t in rep['tests'])}/{len(rep['tests'])} sandbox tests passed; "
          f"files: {', '.join(sorted(files))}")
    if proposal["type"] == "skill":
        print(f"  and symlink {reg.skills_link_dir() / proposal['name']} -> it (Claude Code will load this skill)")
    if proposal["type"] == "tool":
        how = f"wraps the installed script {proposal['wraps']['script']}" if proposal.get("wraps") else "new code"
        print(f"  served by the MCP server as mcp__learned__{proposal['name']} ({how})")
    if proposal.get("promotes"):
        print(f"  and retire the skill {proposal['promotes']['skill']}: Claude will call the tool instead of "
              f"reasoning through the task")
    if proposal.get("quick"):
        print("  note: learned from a single session (quick learn)")
    if not args.yes:
        if not sys.stdin.isatty():
            print("not approved: approval needs an interactive terminal (or an explicit --yes)", file=sys.stderr)
            return 1
        if input(f"Type '{proposal['name']}' to approve and install, anything else cancels: ").strip() != proposal["name"]:
            print("cancelled; nothing installed")
            return 1
    try:
        e = reg.install(proposal, files, draft_dir=d, note=args.note, validation=rep, use_git=not args.no_git)
    except reg.InstallError as err:
        print(f"not installed: {err}", file=sys.stderr)
        return 1
    print(f"installed {e['type']} {e['name']} v{e['version']} at {e['path']}" +
          (f" (commit {e['commit']})" if e.get("commit") else ""))
    from refine import memory
    retired = memory.after_approve(proposal)
    if retired:
        print(f"retired skill {retired}: this job now runs as code (undo: python3 refine/refine.py demote {e['name']})")
    if e["type"] == "script":
        print(f"run it: python3 {reg.install_root() / e['path'] / e['entrypoint']} --help")
    if e["type"] == "tool":
        print(f"Claude sees it as mcp__learned__{e['name']} once mcp_tools/server.py is registered (install.py does this)")
    print(f"revert: python3 registry/install.py uninstall {e['name']}")
    return 0


def cmd_adopt(args):
    """Turn a skill that skill-creator built from a brief into a validated draft."""
    from refine.brief import briefs_dir, target_dir
    name = args.brief
    bdir = briefs_dir() / name
    meta = store.read_json(bdir / "brief.json")
    if not meta:
        raise LookupError(f"no skill brief {name!r} (see learned/briefs/)")
    target = target_dir(name)
    src = Path(args.source).resolve() if args.source else target
    if not (src / "SKILL.md").is_file():
        raise LookupError(f"no SKILL.md in {src}: build the skill with skill-creator first (BRIEF.md: {bdir / 'BRIEF.md'})")
    files, skipped = {}, []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if not p.is_file() or set(rel.parts) & {"__pycache__", ".git", "node_modules"} or rel.name == ".DS_Store" \
                or any(part.endswith("-workspace") for part in rel.parts):
            continue
        try:
            if p.stat().st_size > 1_000_000:
                raise ValueError("over 1 MB")
            files[rel.as_posix()] = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError) as e:
            skipped.append(f"{rel.as_posix()} ({'binary' if isinstance(e, UnicodeDecodeError) else e})")
    if skipped:
        raise LookupError("the harness only adopts text files; remove or convert: " + ", ".join(skipped[:10]))
    if src != target:
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        for rel, content in files.items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_text(content, encoding="utf-8")
    pattern = store.load_pattern(meta["pattern_key"])
    if not pattern:
        raise LookupError(f"pattern {meta['pattern_key']!r} not found")
    fm, _ = replay._frontmatter(files["SKILL.md"])
    proposal = {"name": name, "type": "skill", "pattern_key": meta["pattern_key"], "status": "draft",
                "created_by": "skill-creator", "description": str((fm or {}).get("description", ""))[:1024],
                "spec": f"built with skill-creator from learned/briefs/{name}/BRIEF.md", "entrypoint": "",
                "type_deviation_reason": "", "file_list": sorted(files), "tests": [],
                "scan_flags": scan_files({k: v for k, v in files.items() if not k.startswith("evals/")}),
                "evidence": {"sessions": meta["sessions"], "n_success": len(meta["sessions"]),
                             "n_sessions": pattern.get("n_sessions")},
                "quick": meta.get("quick", False), "created_at": store.now()}
    if meta.get("improve"):
        proposal["revision_of"] = meta["improve"]
    d = replay.drafts_dir() / name
    previous = store.read_json(d / "validation.json")
    store.write_json_atomic(d / "proposal.json", proposal)
    rep = replay.validate_draft(proposal, files, pattern, previous=previous)   # includes the safety scan
    rep["passed"] = not rep["problems"]
    store.write_json_atomic(d / "validation.json", rep)
    proposal["status"] = "validated" if rep["passed"] else "failed_validation"
    store.write_json_atomic(d / "proposal.json", proposal)
    if rep["passed"]:
        store.write_json_atomic(bdir / "brief.json", {**meta, "status": "adopted", "adopted_at": store.now()})
        if not meta.get("improve"):
            pattern["status"] = "proposed"
            pattern["proposal"] = name
            store.save_pattern(pattern)
    replay.print_report(rep)
    oc = rep.get("official_check") or {}
    print(f"skill-creator's checker: {oc.get('message', 'not run')}")
    if rep["passed"]:
        print(f"\nadopted {name} as a draft. Review: python3 review/cli.py review {name}   "
              f"approve: python3 review/cli.py approve {name}")
        return 0
    print("\nnot adopted yet: fix the problems above (with skill-creator), then run adopt again", file=sys.stderr)
    return 1


def cmd_reject(args):
    proposal, _, d = _draft(args.draft)
    if proposal["status"] == "installed":
        raise LookupError("draft is installed; use registry/install.py uninstall instead")
    c = reg.reject(proposal, draft_dir=d, reason=args.reason, use_git=not args.no_git)
    print(f"rejected {proposal['name']}; pattern {proposal['pattern_key']} locked as rejected" +
          (f" (commit {c})" if c else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("-n", type=int, default=20); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("session"); p.add_argument("-v", "--verbose", action="store_true"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("rate"); p.add_argument("session"); p.add_argument("outcome", choices=store.OUTCOMES)
    p.add_argument("--note"); p.set_defaults(fn=cmd_rate)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("proposals"); p.set_defaults(fn=cmd_proposals)
    p = sub.add_parser("review"); p.add_argument("draft"); p.add_argument("--brief", action="store_true")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("diff"); p.add_argument("draft"); p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("revalidate"); p.add_argument("draft"); p.set_defaults(fn=cmd_revalidate)
    p = sub.add_parser("approve"); p.add_argument("draft"); p.add_argument("--note")
    p.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    p.add_argument("--no-git", action="store_true"); p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("adopt", help="turn a skill built with skill-creator from a brief into a draft")
    p.add_argument("brief"); p.add_argument("--from", dest="source", help="skill folder, if not the brief's target")
    p.set_defaults(fn=cmd_adopt)
    p = sub.add_parser("reject"); p.add_argument("draft"); p.add_argument("--reason", required=True)
    p.add_argument("--no-git", action="store_true"); p.set_defaults(fn=cmd_reject)
    args = ap.parse_args(argv)
    try:
        rc = args.fn(args)
        if rc:
            return rc
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
