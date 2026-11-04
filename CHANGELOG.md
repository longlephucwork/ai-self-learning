# Changelog

What changed in each version of the learning harness. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Claude shows a version's section here when it offers you the update.

## [Unreleased]

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

[Unreleased]: https://github.com/longlephucwork/claude-learning-harness/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/longlephucwork/claude-learning-harness/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/longlephucwork/claude-learning-harness/releases/tag/v0.0.1
