"""Muscle memory: skills that are practised often and accurately become automatic tools or scripts.

Two levels, like a person learning a task:
  thinking   a skill: Claude reads instructions and reasons through the task every time
  automatic  an MCP tool or a script: code does the job; Claude makes one call and doesn't reason through it

  practice   every use of a skill is tracked (refine.collect_uses); accuracy = uses whose session you
             accepted (edited and redone don't count as accurate)
  automate   a skill used >= promote_min_uses times, with accuracy >= promote_min_accuracy, whose uses were
             mostly mechanical (>= promote_min_mechanical of their sessions summarised as deterministic) is
             proposed for promotion: a tool (or, with `--as script` / "promote_as": "script", a script) drafted
             and tested against the real inputs and outputs of those uses. Approving it installs it and retires
             the skill.
  judgment   a skill whose uses need judgment is never promoted: no script can reproduce judgment.
  relearn    a promoted tool or script that keeps failing is demoted: the old skill comes back from learned/,
             the code goes, and Claude reasons through the task again.

Thresholds live in store/config.json under "auto" (defaults below), with "promote_as" ("tool" or "script")
choosing what the background run drafts. Nothing changes without your approval.
"""
from __future__ import annotations

from pathlib import Path

from harness_lib import store
from registry import install as reg

PROMOTE_DEFAULTS = {"promote_min_uses": 5, "promote_min_accuracy": 0.9, "promote_min_mechanical": 0.75}
PROMOTE_TYPES = ("tool", "script")


def promote_as() -> str:
    """What a promotion drafts when nobody says: "promote_as" in the auto config ("tool" by default)."""
    from refine import auto
    t = auto.config().get("promote_as", "tool")
    return t if t in PROMOTE_TYPES else "tool"


def thresholds() -> dict:
    from refine import auto
    cfg = auto.config()
    return {k: cfg.get(k, v) for k, v in PROMOTE_DEFAULTS.items()}


def practice(entry: dict, uses: list[dict] | None = None) -> dict:
    """How much and how well an installed item has been used, and how mechanical those uses were."""
    from refine import refine
    uses = refine.collect_uses(entry) if uses is None else uses
    accurate = 0
    for u in uses:
        if u.get("session_id"):
            accurate += (not u["failed"]) and u.get("session_outcome") == "accepted"
        else:
            accurate += not u["failed"]
    sessions = sorted({u["session_id"] for u in uses if u.get("session_id")})
    flags = []
    for sid in sessions:
        summ = store.load_summary(sid)
        if summ:
            flags.append(bool(summ["summary"].get("deterministic")))
    return {"uses": len(uses), "accurate": accurate, "accuracy": round(accurate / len(uses), 3) if uses else None,
            "sessions": sessions, "mechanical": round(sum(flags) / len(flags), 3) if flags else None,
            "summarised": len(flags)}


def promotion_status(entry: dict, uses: list[dict] | None = None) -> tuple[str, str]:
    """('ready' | 'practising' | 'judgment' | 'n/a', reason)."""
    if entry["type"] != "skill":
        return "n/a", ""
    t, p = thresholds(), practice(entry, uses)
    if p["uses"] < t["promote_min_uses"]:
        return "practising", f"used {p['uses']} of {t['promote_min_uses']} times needed to become automatic"
    if (p["accuracy"] or 0) < t["promote_min_accuracy"]:
        return "practising", (f"{p['accuracy']:.0%} accurate over {p['uses']} uses; needs "
                              f"{t['promote_min_accuracy']:.0%} to become automatic")
    if p["mechanical"] is None:
        return "practising", "its uses haven't been summarised yet (the next learning run does that)"
    if p["mechanical"] < t["promote_min_mechanical"]:
        return "judgment", (f"used {p['uses']} times, {p['accuracy']:.0%} accurate, but only {p['mechanical']:.0%} of "
                            f"uses were mechanical: the task needs judgment, so it stays a skill")
    pattern = store.load_pattern(entry["pattern_key"]) or {}
    if pattern.get("pending_promotion"):
        return "practising", f"a promotion draft is waiting for you: {Path(pattern['pending_promotion']).name}"
    return "ready", (f"used {p['uses']} times, {p['accuracy']:.0%} accurate, {p['mechanical']:.0%} mechanical: "
                     f"ready to become automatic (`refine.py promote {entry['name']}`)")


def promote(name: str, *, as_type: str | None = None, model: str | None = None, log=print) -> dict:
    """Draft a tool or script (as_type; default from promote_as()) that does the skill's job as code,
    from the real uses of the skill."""
    from generate.propose import propose
    as_type = as_type or promote_as()
    if as_type not in PROMOTE_TYPES:
        raise ValueError(f"a skill can be promoted to a {' or a '.join(PROMOTE_TYPES)}, not a {as_type!r}")
    installed = reg.load_registry()["installed"]
    entry = installed.get(name)
    if not entry or entry["type"] != "skill":
        raise LookupError(f"{name!r} is not an installed skill")
    state, why = promotion_status(entry)
    if state != "ready":
        raise ValueError(f"{name} isn't ready to become automatic: {why}")
    skill_md = (reg.install_root() / entry["path"] / "SKILL.md").read_text(encoding="utf-8")
    # The skill's real uses are the evidence: make sure the pattern includes those sessions.
    pattern = store.load_pattern(entry["pattern_key"])
    p = practice(entry)
    used = p["sessions"]
    pattern["session_ids"] = sorted(set(pattern.get("session_ids", [])) | set(used))
    # How mechanical the job is now comes from how the skill is actually used, not from the sessions it was
    # first learned from (re-clustering recomputes it the same way, as these sessions are members now).
    pattern["deterministic_frac"] = p["mechanical"]
    store.save_pattern(pattern)
    log(f"promoting skill {name} to a {as_type}, from {len(used)} session(s) that used it ...")
    return propose(entry["pattern_key"], model=model, force_type=as_type,
                   promotes={**entry, "skill_md": skill_md}, log=log)


def demote(name: str, *, note: str | None = None) -> dict:
    """Bring back the skill a tool or script replaced, and remove that tool or script."""
    from validate import replay
    installed = reg.load_registry()["installed"]
    tool = installed.get(name)
    if not tool or not tool.get("promoted_from"):
        raise LookupError(f"{name!r} is not a tool or script that was promoted from a skill")
    skill = tool["promoted_from"]["skill"]
    d = replay.drafts_dir() / skill
    proposal = store.read_json(d / "proposal.json")
    if not proposal:
        raise LookupError(f"can't find the original draft of skill {skill!r} in {replay.drafts_dir()}")
    files = {f: (d / "files" / f).read_text(encoding="utf-8") for f in proposal["file_list"]}
    validation = store.read_json(d / "validation.json") or {}
    entry = reg.install({**proposal, "promotes": None}, files, draft_dir=d,
                        note=note or f"demoted from tool {name}", validation=validation)
    reg.uninstall(name, reason=note or f"demoted back to skill {skill}")
    return {"skill": skill, "skill_version": entry["version"], "removed_tool": name, "removed_type": tool["type"]}


def after_approve(proposal: dict) -> str | None:
    """When a promotion is approved, retire the skill it replaces. Returns the retired skill's name."""
    p = proposal.get("promotes")
    if not p:
        return None
    if p["skill"] in reg.load_registry()["installed"]:
        reg.uninstall(p["skill"], reason=f"promoted to {proposal['type']} {proposal['name']} (muscle memory)")
    return p["skill"]
