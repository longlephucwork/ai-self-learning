"""Harness self-update from tagged releases, against real git repos (a bare 'GitHub', a dev and a user clone)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def git(repo, *a, check=True):
    p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    if check and p.returncode:
        raise AssertionError(p.stderr)
    return p.stdout.strip()


class Update(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.x", GIT_COMMITTER_NAME="t",
                          GIT_COMMITTER_EMAIL="t@e.x", LEARNING_HARNESS_STORE=str(t / "store"))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(t / "github.git")], check=True)
        self.dev = t / "dev"
        subprocess.run(["git", "clone", "-q", str(t / "github.git"), str(self.dev)], check=True, capture_output=True)
        git(self.dev, "checkout", "-q", "-b", "main")
        self.release("1.0.0", {"app.txt": "v1\n"}, notes=None)
        self.user = t / "user"
        subprocess.run(["git", "clone", "-q", str(t / "github.git"), str(self.user)], check=True, capture_output=True)
        from harness_lib import update
        self.u = update

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear(); os.environ.update(self._env)

    def release(self, version, files, notes="", tag=True, branch="main"):
        for k, v in {**files, "VERSION": version + "\n"}.items():
            (self.dev / k).parent.mkdir(parents=True, exist_ok=True)
            (self.dev / k).write_text(v)
        git(self.dev, "add", "-A"); git(self.dev, "commit", "-qm", f"release {version}")
        if tag:
            git(self.dev, "tag", "-a", f"v{version}", "-m", notes or f"v{version}") if notes is not None else \
                git(self.dev, "tag", f"v{version}")
        git(self.dev, "push", "-q", "origin", branch, "--tags")

    def learned_commit(self, path="skills/x/SKILL.md"):
        (self.user / path).parent.mkdir(parents=True, exist_ok=True)
        (self.user / path).write_text("learned\n")
        git(self.user, "add", "-A"); git(self.user, "commit", "-qm", "learned: install skill x v1")

    def test_check_finds_new_release_with_notes(self):
        self.assertFalse(self.u.check(self.user)["available"])
        self.release("1.1.0", {"app.txt": "v1.1\n"}, notes="Faster detection\n- fixed X")
        st = self.u.check(self.user)
        self.assertEqual((st["available"], st["latest"], st["current"]), (True, "v1.1.0", "1.0.0"))
        self.assertIn("Faster detection", st["notes"])
        self.assertEqual((self.user / "VERSION").read_text().strip(), "1.0.0")     # check changes nothing

    def test_only_semver_tags_on_main_count(self):
        git(self.dev, "tag", "v2"); git(self.dev, "tag", "release-3"); git(self.dev, "push", "-q", "--tags")
        git(self.dev, "checkout", "-q", "-b", "side")
        self.release("9.9.9", {"app.txt": "side\n"}, notes="not on main", branch="side")
        st = self.u.check(self.user)
        self.assertFalse(st["available"], st)
        with self.assertRaises(self.u.UpdateError) as e:
            self.u.apply(self.user, tag="v9.9.9")
        self.assertIn("not part of origin/main", str(e.exception))

    def test_apply_keeps_learned_commits_and_rolls_back(self):
        self.learned_commit()
        self.release("1.1.0", {"app.txt": "v1.1\n"}, notes="notes")
        after = []
        r = self.u.apply(self.user, self_test=lambda repo: (True, ""), after_merge=after.append)
        self.assertEqual((r["from"], r["to"]), ("1.0.0", "1.1.0"))
        self.assertEqual((self.user / "app.txt").read_text(), "v1.1\n")
        self.assertTrue((self.user / "skills/x/SKILL.md").exists())               # learned item kept
        self.assertEqual(after, [self.user])
        self.assertFalse(self.u.state()["available"])
        r = self.u.rollback(self.user)
        self.assertEqual(r["version"], "1.0.0")
        self.assertTrue(self.u.state()["available"])                              # v1.1.0 still offered
        self.assertEqual((self.user / "app.txt").read_text(), "v1\n")
        self.assertTrue((self.user / "skills/x/SKILL.md").exists())               # it predates the update
        with self.assertRaises(self.u.UpdateError):
            self.u.rollback(self.user)                                            # nothing left to undo

    def test_rollback_refused_after_new_commits(self):
        self.release("1.1.0", {"app.txt": "v1.1\n"})
        self.u.apply(self.user)
        self.learned_commit()
        with self.assertRaises(self.u.UpdateError) as e:
            self.u.rollback(self.user)
        self.assertIn("revert -m 1", str(e.exception))
        self.assertTrue((self.user / "skills/x/SKILL.md").exists())

    def test_refuses_dirty_tree_and_aborts_conflicts(self):
        self.release("1.1.0", {"app.txt": "v1.1\n"})
        (self.user / "app.txt").write_text("my edit\n")
        with self.assertRaises(self.u.UpdateError) as e:
            self.u.apply(self.user)
        self.assertIn("uncommitted changes", str(e.exception))
        git(self.user, "commit", "-qam", "my local change")
        head = git(self.user, "rev-parse", "HEAD")
        with self.assertRaises(self.u.UpdateError) as e:
            self.u.apply(self.user)
        self.assertIn("conflicts with your local changes in: app.txt", str(e.exception))
        self.assertEqual(git(self.user, "rev-parse", "HEAD"), head)                # nothing changed
        self.assertEqual(git(self.user, "status", "--porcelain"), "")              # no half-done merge
        self.assertEqual((self.user / "VERSION").read_text().strip(), "1.0.0")

    def test_failed_self_test_undoes_the_update(self):
        self.release("1.1.0", {"app.txt": "broken\n"})
        head = git(self.user, "rev-parse", "HEAD")
        with self.assertRaises(self.u.UpdateError) as e:
            self.u.apply(self.user, self_test=lambda repo: (False, "FAILED (errors=1)"))
        self.assertIn("update was undone", str(e.exception))
        self.assertEqual(git(self.user, "rev-parse", "HEAD"), head)
        self.assertEqual((self.user / "VERSION").read_text().strip(), "1.0.0")

    def test_not_newer_is_refused(self):
        with self.assertRaises(self.u.UpdateError):
            self.u.apply(self.user, tag="v1.0.0")

    def test_session_start_note_once_per_version(self):
        self.release("1.1.0", {"app.txt": "v1.1\n"}, notes="Better skills")
        self.u.check(self.user)
        with mock.patch.object(self.u, "current_version", return_value="1.0.0"):
            text = self.u.session_start_context()
            self.assertIn("v1.1.0 (installed: 1.0.0)", text)
            self.assertIn("Better skills", text)
            self.assertIn("Only if the user explicitly says yes", text)
            self.assertIsNone(self.u.session_start_context())
            self.release("1.2.0", {"app.txt": "v1.2\n"})
            self.u.check(self.user)
            self.assertIn("v1.2.0", self.u.session_start_context())                 # a newer one: announced

    def test_daily_check_throttle_and_hook(self):
        self.assertFalse(self.u.due())                                            # off by default
        self.u.set_config(check=True)
        self.assertTrue(self.u.due())
        self.u._save_state(checked_at=datetime.now(timezone.utc).isoformat())
        self.assertFalse(self.u.due())
        self.u._save_state(checked_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
        self.assertTrue(self.u.due())
        from hooks import log_session
        from tests.test_phase3 import SESSIONS, transcript
        c, j, _, _ = SESSIONS["s-fruit"]
        tp = transcript(self.t / "x.jsonl", "s-fruit", c, j)
        with mock.patch.object(self.u, "spawn_check_in_background") as spawn, \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s-fruit", "transcript_path": tp,
                                                                "hook_event_name": "SessionEnd"}))):
            log_session.hook_main()
        spawn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
