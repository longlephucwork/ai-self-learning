# Changelog

What changed in each version of the learning harness. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Claude shows a version's section here when it offers you the update.

## [Unreleased]

### Changed
- **The repository is now `ai-self-learning`** (it was `claude-learning-harness`). The install commands use the new name, and a new install goes into `~/ai-self-learning`. An install from before the rename keeps its folder, `~/claude-learning-harness`: re-running the installer uses it, and updates keep working because GitHub redirects the old address.

### Added
- **Allowed hosts for generated skills.** A skill's `curl`, `wget`, `ssh`, `scp` and `rsync` no longer fail the safety check when every host they reach is localhost or on your allowlist. Manage the list with `review/cli.py allowed-hosts --add/--remove` (exact hosts, IPs or `*.example.com`). These uses are shown to the reviewer but don't block. Briefs tell skill-creator the rule and the list. Still blocked: other hosts, targets in variables, `nc`/`ncat`, and piping a download into a shell (now flagged as `exec`). Scripts and tools stay localhost-only.
- **Choose the budget and schedule.** `refine.py auto-config` shows and changes how background learning runs: the monthly budget (`--budget`), how often it runs (`--every 12h`, `90m`, `1d`), how many new sessions it waits for (`--after-sessions`), how many successful repeats a task needs before it's learned (`--learn-after`), drafts per run (`--max-drafts`) and how long old one-off sessions are kept (`--keep-days`). Values are checked, and `--reset` brings back the defaults. You can also ask Claude to change them. The installer takes `--every`, `--after-sessions` and `--learn-after` alongside `--budget`.

- **Skills Claude didn't use get their description fixed.** When 2 or more later sessions do a learned skill's task without Claude loading it, the background run writes an update brief. It lists how you actually asked, and skill-creator fixes the skill's description with its description optimization, using a ready trigger eval set built from your real requests (plus near misses from your other tasks). `refine.py fix-triggers <skill>` does it by hand. Corrections and missed triggers for the same skill share one brief.

- **Memory like a brain's.** Repeated work builds a path that strengthens with every successful repeat and fades without one. Spaced practice (repeats on later days) makes a path last longer than massed practice. A task that fades before it's learned is forgotten, so its pattern and sessions are cleaned up instead of being kept forever. A learned task is in long-term memory, where it fades more slowly and every use counts as rehearsal. `refine.py memory` shows every path, `refine.py report` shows each learned item's, and `auto-config --memory-days` sets how fast unpractised tasks fade. When a task is learned still follows `--learn-after`.

### Fixed
- **No more "Python was not found" from hooks in the harness folder on Windows.** The repo had its own project-level `.claude/settings.json` and `.mcp.json`, left over from before the installer existed. They ran the hooks and the MCP server with a bare `python3`, which on Windows is often the Microsoft Store placeholder. Inside the harness folder they also ran every hook twice, alongside the installer's own. Both files are removed: the installer's user-level hooks and MCP server, which use your Python's full path, cover every project including this one.
- **Skills stuck after a failed `adopt` are no longer silent.** A skill built with skill-creator that failed the checks was never revisited or mentioned again, so it was never installed, tracked or improved. The session start note and `auto-status` now list stuck skills with their problems, weekly until they're fixed.
- **Fewer false safety flags on skills.** These are now shown as information instead of blocking:
  - a script's `#!/usr/bin/env` first line (the harness's own eval fixtures had it);
  - a skill mentioning its own `~/.claude/skills/` folder;
  - `curl` or URLs that only reach `localhost`, such as a health check.

  Claude's credentials and settings files, and any remote network use, still block. `review` and `approve` now scan the same files `adopt` does, so the harness's own eval fixtures are no longer flagged.
- **A hand-made copy of a learned skill no longer blocks it for good.** A folder in `~/.claude/skills/` with a learned skill's name that the harness didn't install is reported as not tracked. `approve <name> --replace-unmanaged` moves it to `store/replaced/` and installs the tracked version. Skill briefs now say not to copy a skill there by hand.
- "Learn this" on a session of a task that's already being learned (for example, one fixing that skill) adds the session to that task, instead of creating a second copy of the task under a new name.
- **Background learning no longer waits for a session to start or end.** It used to check only then, so a long session, or a window left open all day, could go without a run even after the wait was over. Now it's also checked after replies. A cheap gate keeps that to two small file reads per reply until a run could be due, and the full check runs at most every 10 minutes. `refine.py auto-status` shows when the next run is allowed and what the last check found.
- A part of a session that used a skill is no longer reported as having missed it.
- Skill briefs tell Claude to run skill-creator's test runs with `LEARNING_HARNESS_DISABLE=1`, so the harness doesn't log those runs as your sessions and learn from them.
- Re-running the installer no longer saves the default budget as your own choice, so a better default in a later release still reaches you.
- A setting you choose with `auto-config` is never mistaken for an old installer's default. Before, choosing a 24-hour cycle would quietly have turned back into 6 hours.

## [0.0.4] - 2026-11-05

### Added
- **Learn just one part of a session.** Say "learn the release part" in a long session that also did other work, and only that part is learned. Claude numbers the session's requests (`refine.py learn --list`) and learns the chosen ones (`refine.py learn --requests 7-9 --focus "..."`). The part becomes its own derived session, so the summary, skill brief, test cases and drafted code come from that part only. Only the part is rated `accepted`, and the parent session is left as it was.

### Fixed
- After an update, the built-in "learn this" skill now gets the new version's instructions at the next session start. Before, `install.py --update` refreshed the hooks but left the skill's old text in place.

## [0.0.3] - 2026-11-04

### Added
- **Muscle memory can make scripts.** `refine.py promote <skill> --as script` turns a well-practised skill into a script instead of an MCP tool. The script's tests are the real inputs and outputs of the skill's uses, as for a tool, and approving it retires the skill.
- New setting `"promote_as"` under `"auto"` in `store/config.json`: `"tool"` (the default) or `"script"`. It decides what the background run, and `promote` without `--as`, turns a skill into.

### Changed
- `refine.py demote` brings a skill back from a promoted script as well as a tool, and says which one it removed.
- `review/cli.py approve` names what will replace the retired skill (the tool or the script).
- **The transcript copy is append-only.** After each reply, the Stop hook now redacts and stores only the lines added since the last run, instead of reprocessing the whole transcript. On a 10.8 MB transcript, that's about 20 ms instead of about 3 s per reply. The copy is rebuilt from the start whenever the saved position can't be trusted.
- **Parsing is incremental too.** The parser saves its progress and resumes from it, so each reply's Stop hook reads only the new lines. Together with the append-only copy, a reply's hook on a 10.8 MB transcript takes about 80 ms instead of about 3.3 s. The saved progress holds only redacted text, as `session.json` does.

## [0.0.2] - 2026-11-04

### Added
- **Learning while you work.** When you ask for a task you've done before, Claude does it, then offers once to make it reusable. You don't run any commands. This uses a new `UserPromptSubmit` hook, and the matching is local, with no model call.
- **Skills improve on their own.** When you keep having to correct a learned skill, the background run writes an update brief listing your corrections. Claude then offers to update the skill with skill-creator. Each skill version is briefed once.
- **Quick learn from one session.** Say "learn this" (or run `refine.py learn`) to learn from the current session right away. It now also uses earlier sessions of the same task.
- **Muscle memory.** A skill you use often and accurately, for a mechanical task, is proposed as a tool, so Claude just calls it. Use `refine.py promote` to do this by hand and `refine.py demote` to bring the skill back.
- **Skills are built with skill-creator.** The harness writes a brief from your sessions: requests, accepted output, corrections and ready-made test cases. `review/cli.py adopt` checks the result against the Claude skill standard, runs the safety scan and judges it against your sessions.
- The installer adds skill-creator (from github.com/anthropics/skills) if it's missing, and finds it when it's installed as a Claude Code plugin. Use `--no-skill-creator` to skip this.
- Claude asks you about repeated tasks whose outcome was unclear, instead of guessing (`refine.py confirm`).
- One-off sessions older than 90 days are forgotten (`refine.py prune`).
- Installed scripts are listed for Claude at the start of every session, with the exact command to run them.
- Re-running `install.py` links approved skills that Claude Code can't see yet.

### Changed
- Simpler install: the installer shows one summary and asks one question. Use `--custom` to choose each part.
- Background learning runs every 6 hours, down from 24. A run also starts when a session begins, because closing a terminal window skips the session-end hook.
- Approved items live in `learned/`, a local git repo that's never pushed with the code.

### Fixed
- Automatic learning on native Windows:
  - the harness's own hooks no longer fire inside its background runs
  - long arguments no longer get mangled
  - reading a file while it's being replaced no longer fails
  - background windows no longer appear
  - the hook's transcript copy and the install folder rename are retried
- Text you pasted into a session is no longer flagged as a prompt injection.
- A failed background run is retried after an hour, instead of waiting a full day.
- Repeat detection stays fast on large session histories (a cached index).

## [0.0.1] - 2026-11-03

### Added
- Session capture through Claude Code hooks, into a plain-file store with secrets redacted.
- Pattern detection: sessions are summarised and grouped into repeated tasks.
- Drafting of scripts and MCP tools, validated in a sandbox against your real sessions.
- Review and approval gate (`review/cli.py`). Nothing installs without your OK.
- A registry with a git commit for every install.
- Usage tracking, a refine report, and revisions with locked regression tests.
- MCP tool server, so Claude can find and call approved tools.
- Cross-platform installer for Linux, macOS, Windows, WSL and Docker, with a portable sandbox.
- Automatic learning in the background, within a monthly budget.
- Self-update from tagged releases: a daily check, Claude asks first, a safe merge, and rollback.

[Unreleased]: https://github.com/longlephucwork/ai-self-learning/compare/v0.0.4...HEAD
[0.0.4]: https://github.com/longlephucwork/ai-self-learning/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/longlephucwork/ai-self-learning/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/longlephucwork/ai-self-learning/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/longlephucwork/ai-self-learning/releases/tag/v0.0.1
