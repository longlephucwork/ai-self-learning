"""Updates of the harness itself, from tagged releases (vX.Y.Z) on the git remote.

  check()     git fetch --tags; is there a release newer than VERSION? (no model calls, no API)
  apply()     merge that release into your clone, keeping your learned commits; self-test; auto-rollback
  rollback()  return to the version before the last update (only if nothing was committed since)

Safety rules, enforced here:
  * only tags named vMAJOR.MINOR.PATCH count, and the tag must be reachable from origin/main
  * the working tree must be clean (your own edits are never touched; store/ is git-ignored)
  * the release is merged, so learned commits (`learned: install ...`) are kept; any conflict aborts
    the merge and leaves everything as it was
  * after merging, a self-test runs; if it fails, the update is undone automatically
  * nothing is applied without being asked: the background check only records what's available, and
    Claude asks you at the start of a session (or you run `install.py --update`)

State: store/update_state.json. Settings: store/config.json -> "update": {"check": true, "interval_hours": 24}.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DEFAULTS = {"check": False, "interval_hours": 24}


class UpdateError(RuntimeError):
    pass


def _git(repo: Path, *args, check=True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and p.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def parse(v: str) -> tuple[int, int, int] | None:
    m = TAG_RE.match(v if v.startswith("v") else "v" + v)
    return tuple(int(x) for x in m.groups()) if m else None


def current_version(repo: Path | None = None) -> str:
    repo = repo or store.REPO_ROOT
    try:
        return (repo / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def config() -> dict:
    return {**DEFAULTS, **(store.read_json(store.store_dir() / "config.json") or {}).get("update", {})}


def set_config(**values) -> dict:
    path = store.store_dir() / "config.json"
    cfg = store.read_json(path) or {}
    cfg["update"] = {**DEFAULTS, **cfg.get("update", {}), **values}
    store.write_json_atomic(path, cfg)
    return cfg["update"]


def state() -> dict:
    return store.read_json(store.store_dir() / "update_state.json") or {}


def _save_state(**values) -> dict:
    st = {**state(), **values}
    store.write_json_atomic(store.store_dir() / "update_state.json", st)
    return st


# ---------------- check ----------------

def release_notes(repo: Path, tag: str) -> str:
    """The annotated tag's message, else the subjects of the commits it adds (learned commits excluded)."""
    msg = _git(repo, "tag", "-l", "--format=%(contents)", tag, check=False).stdout.strip()
    if msg:
        return msg[:2000]
    log = _git(repo, "log", "--no-merges", "--format=%s", f"HEAD..{tag}", check=False).stdout.splitlines()
    return "\n".join(f"- {s}" for s in log if not s.startswith("learned:"))[:2000]


def check(repo: Path | None = None, *, remote: str = "origin") -> dict:
    """Fetch tags and record whether a newer release exists. Safe to run anytime; changes no files in the repo."""
    repo = repo or store.REPO_ROOT
    # explicit refspec: works even when the clone has no remote.<name>.fetch configured
    _git(repo, "fetch", "--quiet", "--tags", remote, f"+refs/heads/main:refs/remotes/{remote}/main")
    cur = parse(current_version(repo)) or (0, 0, 0)
    reachable = set(_git(repo, "tag", "--merged", f"{remote}/main", "--list", "v*").stdout.split())
    releases = sorted((t for t in reachable if parse(t)), key=parse)
    latest = releases[-1] if releases else None
    available = bool(latest and parse(latest) > cur)
    return _save_state(checked_at=store.now(), current=current_version(repo), latest=latest,
                       available=available, notes=release_notes(repo, latest) if available else "")


def due() -> bool:
    cfg = config()
    if not cfg["check"]:
        return False
    last = state().get("checked_at")
    if not last:
        return True
    try:
        t = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - t >= timedelta(hours=cfg["interval_hours"])


def spawn_check_in_background() -> None:
    """Run `install.py --check-update` detached, so the session never waits for the network."""
    logf = open(store.store_dir() / "update.log", "a", encoding="utf-8")
    kw = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf, "cwd": str(store.REPO_ROOT)}
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: hidden, but with a console, which the claude/node/bash
        # processes it starts need (a fully DETACHED_PROCESS breaks them)
        kw["creationflags"] = 0x08000000 | 0x00000200
    else:
        kw["start_new_session"] = True
    _children.append(subprocess.Popen([sys.executable, str(store.REPO_ROOT / "install.py"), "--check-update"],
                                      **kw))
    logf.close()


_children: list = []


# ---------------- apply / rollback ----------------

def _dirty(repo: Path) -> list[str]:
    return [l for l in _git(repo, "status", "--porcelain").stdout.splitlines() if l.strip()]


def apply(repo: Path | None = None, *, tag: str | None = None, remote: str = "origin",
          self_test=None, after_merge=None) -> dict:
    """Merge release `tag` (default: the latest available) into the clone. Returns a summary dict.

    self_test(repo) -> (ok, output) runs after the merge; on failure the merge is undone.
    after_merge(repo) refreshes installed hooks etc.; it runs only after the self-test passes."""
    repo = repo or store.REPO_ROOT
    st = check(repo, remote=remote)
    tag = tag or st.get("latest")
    if not tag or not parse(tag):
        raise UpdateError("no release to update to")
    if parse(tag) <= (parse(current_version(repo)) or (0, 0, 0)):
        raise UpdateError(f"already on {current_version(repo)}; {tag} is not newer")
    if _git(repo, "merge-base", "--is-ancestor", tag, f"{remote}/main", check=False).returncode != 0:
        raise UpdateError(f"{tag} is not part of {remote}/main; refusing to install it")
    dirty = _dirty(repo)
    if dirty:
        raise UpdateError("you have uncommitted changes; commit or stash them first:\n  " + "\n  ".join(dirty[:10]))

    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    old_version = current_version(repo)
    m = _git(repo, "merge", "--no-edit", "-m", f"Update learning harness to {tag}", tag, check=False)
    if m.returncode != 0:
        conflicts = _git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        _git(repo, "merge", "--abort", check=False)
        raise UpdateError(f"{tag} conflicts with your local changes in: {', '.join(conflicts) or '(unknown)'}; "
                          f"nothing was changed. Merge it by hand: git -C {repo} merge {tag}")
    after = _git(repo, "rev-parse", "HEAD").stdout.strip()

    if self_test is not None:
        ok, out = self_test(repo)
        if not ok:
            _git(repo, "reset", "--hard", before)  # the tree was clean before the merge: exact restore
            raise UpdateError(f"{tag} failed its self-test, so the update was undone:\n{out[-1500:]}")
    if after_merge is not None:
        after_merge(repo)
    _save_state(current=current_version(repo), available=False, previous_head=before, updated_head=after,
                previous_version=old_version, updated_at=store.now(), updated_to=tag)
    return {"from": old_version, "to": current_version(repo), "tag": tag, "previous_head": before, "head": after}


def rollback(repo: Path | None = None) -> dict:
    repo = repo or store.REPO_ROOT
    st = state()
    prev, upd = st.get("previous_head"), st.get("updated_head")
    if not prev or not upd:
        raise UpdateError("no update to roll back")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != upd:
        raise UpdateError("commits were made after the update (e.g. a learned item was installed), so an "
                          f"automatic rollback could lose them. Roll back by hand: git -C {repo} revert -m 1 {upd}")
    if _dirty(repo):
        raise UpdateError("you have uncommitted changes; commit or stash them first")
    _git(repo, "reset", "--hard", prev)
    latest = st.get("latest")
    still_newer = bool(latest and parse(latest) and parse(latest) > (parse(current_version(repo)) or (0, 0, 0)))
    _save_state(previous_head=None, updated_head=None, current=current_version(repo), rolled_back_at=store.now(),
                available=still_newer)  # the release you rolled back from is still there to update to
    return {"version": current_version(repo), "head": prev}


# ---------------- SessionStart note ----------------

def session_start_context() -> str | None:
    """Text for Claude when a newer release is available; each version is announced once."""
    st = state()
    if not st.get("available") or not st.get("latest") or st.get("notified") == st["latest"]:
        return None
    if parse(st["latest"]) <= (parse(current_version()) or (0, 0, 0)):
        return None
    _save_state(notified=st["latest"])
    py = Path(sys.executable).resolve().as_posix()
    inst = (store.REPO_ROOT / "install.py").as_posix()
    notes = "\n".join("  " + l for l in (st.get("notes") or "(no release notes)").splitlines()[:15])
    return ("[learning-harness] A new version of the user's learning harness is available: "
            f"{st['latest']} (installed: {current_version()}). Release notes:\n{notes}\n"
            "At a natural point early in this conversation, mention it in one or two sentences and ask whether "
            f"to update. Only if the user explicitly says yes, run: {py} {inst} --update --yes\n"
            f"(It keeps their learned items, self-tests, and undoes itself on failure; to go back later: "
            f"{py} {inst} --rollback.) Don't bring it up again if they decline or ignore it.")
