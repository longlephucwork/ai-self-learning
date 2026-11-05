"""Memory like a brain's: repeated work builds a path that strengthens with practice and fades without it.

Each task the harness has seen (a pattern) has a path, worked out from its history with no model call:

  strength   the synapse weight. Every successful repeat adds 1; between repeats it decays exponentially:
             strength(t) = weight * 0.5 ** (days since the last repeat / stability)
  stability  how slowly the path fades: a half-life in days. It starts at `memory_days` (30). Practice spread
             out over time builds it (the spacing effect): each repeat on a later day doubles it, up to a year.
             Repeats on the same day add strength but no stability (massed practice).

The path moves through the stages of memory:

  short-term  a task not learned yet. It fades, and once its strength drops below FORGET_BELOW it is
              forgotten: the pattern goes and its sessions are no longer evidence, so they're pruned like any
              one-off. A task you keep doing never fades enough to be forgotten.
  long-term   a learned task (briefed, drafted or installed). Consolidation doubles its stability once, and
              every later use is rehearsal: a repeat that keeps the path strong.
  automatic   a task done by an installed script or tool: muscle memory (refine/memory.py).

When a task is learned is still decided by the count of successful repeats (`min_success`, `--learn-after`),
so the memory model never stops a task from being learned. It decides what is forgotten, and `refine.py
memory` shows every path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harness_lib import store

FORGET_BELOW = 0.25      # a short-term path weaker than this is forgotten
MAX_STABILITY_DAYS = 365
SPACED = timedelta(hours=20)   # a repeat this long after the last counts as spaced practice (a later day)
SHORT_TERM = {"forming", "candidate"}
LONG_TERM = {"proposed", "briefed", "installed"}
FORGOTTEN = "forgotten.json"   # sessions of forgotten tasks: left out of grouping until they're pruned


def _ts(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def memory_days() -> float:
    from refine import auto
    return float(auto.config().get("memory_days", 30))


def strengthen(events: list[datetime], *, stability: float, now: datetime) -> dict:
    """Run a path through its repeats, oldest first. Returns weight, stability and strength now."""
    weight, last = 0.0, None
    for t in sorted(events):
        if last is not None:
            weight *= 0.5 ** (max((t - last).total_seconds(), 0) / 86400 / stability)
            if t - last >= SPACED:
                stability = min(stability * 2, MAX_STABILITY_DAYS)
        weight += 1
        last = t
    strength = weight * 0.5 ** (max((now - last).total_seconds(), 0) / 86400 / stability) if last else 0.0
    return {"weight": round(weight, 3), "stability_days": round(stability, 1), "strength": round(strength, 3),
            "last": last.isoformat() if last else None, "repeats": len(events)}


def _session_time(sid: str):
    rec = store.load_session(sid) or {}
    return _ts(rec.get("ended_at") or rec.get("last_activity_at") or rec.get("started_at"))


def path(pattern: dict, *, now: datetime | None = None) -> dict:
    """The memory path of one pattern: stage, strength, stability, and whether it's fading or forgotten."""
    from registry import install as reg
    now = now or datetime.now(timezone.utc)
    ok = set(pattern.get("success_outcomes", ["accepted", "edited"]))
    members = [s for s in pattern.get("session_ids", []) if s not in pattern.get("excluded_injection", [])]
    repeats = {s: t for s in members if store.outcome(s)["outcome"] in ok and (t := _session_time(s))}
    status = pattern.get("status")
    items = [e for e in reg.load_registry()["installed"].values() if e["pattern_key"] == pattern["key"]]
    stage = ("automatic" if any(e["type"] in ("script", "tool") for e in items) else "long-term") \
        if status in LONG_TERM else "short-term" if status in SHORT_TERM else status or "unknown"
    stability = memory_days()
    events = list(repeats.values())
    if stage in ("long-term", "automatic"):
        stability *= 2   # consolidation
        from refine import refine
        rehearsals: dict = {}   # one per session (a session is one repeat, however often it used the item)
        for e in items:
            for u in refine.collect_uses(e):
                t = _ts(u.get("at"))
                if u["failed"] or not t:
                    continue
                k = u.get("session_id") or f"{u['source']}:{u['at']}"
                rehearsals[k] = min(t, rehearsals.get(k, t))
        events = [t for s, t in repeats.items() if s not in rehearsals] + list(rehearsals.values())
    p = strengthen(events, stability=stability, now=now)
    fading = p["strength"] < 0.5 * max(p["weight"], 1)
    state = "forgotten-soon" if stage == "short-term" and p["strength"] < FORGET_BELOW else \
        "fading" if fading else "strong"
    return {"key": pattern["key"], "stage": stage, "status": status, **p, "state": state,
            "items": [e["name"] for e in items]}


def paths(now: datetime | None = None) -> list[dict]:
    return sorted((path(p, now=now) for p in store.iter_patterns()), key=lambda r: -r["strength"])


# ---------------- forgetting ----------------

def forgotten_sessions() -> set[str]:
    return set((store.read_json(store.store_dir() / FORGOTTEN) or {}).get("sessions", []))


def _save_forgotten(sids: set[str], log_entry: dict | None = None) -> None:
    path_ = store.store_dir() / FORGOTTEN
    rec = store.read_json(path_) or {}
    rec["sessions"] = sorted(sids)
    if log_entry:
        rec["log"] = (rec.get("log") or [])[-49:] + [log_entry]
    store.write_json_atomic(path_, rec)


def forget_faded(now: datetime | None = None, log=lambda *_: None) -> list[str]:
    """Forget short-term tasks whose path has faded below FORGET_BELOW. Their sessions are left out of
    grouping from now on (so the task isn't re-formed from stale evidence) and pruned once old. A task you
    asked to learn ("learn this") isn't forgotten."""
    now = now or datetime.now(timezone.utc)
    gone, released = [], forgotten_sessions()
    for p in store.iter_patterns():
        if p.get("status") not in SHORT_TERM or p.get("quick"):
            continue
        r = path(p, now=now)
        if r["strength"] >= FORGET_BELOW:
            continue
        released |= set(p.get("session_ids", []))
        store.delete_pattern(p["key"])
        gone.append(p["key"])
        _save_forgotten(released, {"key": p["key"], "at": now.isoformat(), "strength": r["strength"],
                                   "repeats": r["repeats"], "last": r["last"]})
        log(f"  forgot {p['key']}: not repeated since {(r['last'] or '?')[:10]} (strength {r['strength']:.2f})")
    return gone


def drop_pruned(existing: set[str]) -> None:
    """Keep forgotten.json to sessions that still exist."""
    sids = forgotten_sessions()
    if sids - existing:
        _save_forgotten(sids & existing)


def remember(sids) -> None:
    """Sessions used again (e.g. `learn this` on a task once forgotten) are memories again, not forgotten."""
    now_forgotten = forgotten_sessions()
    if now_forgotten & set(sids):
        _save_forgotten(now_forgotten - set(sids))
