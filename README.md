# claude-learning-harness

Makes Claude Code learn from your own work. It notices tasks you keep repeating and drafts a reusable **script**, **MCP tool** or **skill** for each one. Nothing is installed until you approve it.

## Install

Needs **Python 3.10+**, **git** and **Claude Code**.

```bash
# Linux / macOS / WSL
curl -fsSL https://raw.githubusercontent.com/longlephucwork/claude-learning-harness/main/install.sh | bash
```
```powershell
# Windows (PowerShell); or double-click install.bat in a clone
irm https://raw.githubusercontent.com/longlephucwork/claude-learning-harness/main/install.ps1 | iex
```

The installer clones the repo to `~/claude-learning-harness`, checks your machine, shows what it will set up and asks once: `Install? [Y/n]`. (Use `--custom` to choose each part.) Per-OS notes (including Docker, and Ubuntu 24.04's sandbox setting) are in [docs/testing-installs.md](docs/testing-installs.md).

## Use

Use Claude Code as usual. The loop:

1. **You repeat a task.** Claude notices it's one you've asked for before, does it, then offers to make it reusable.
2. **You say yes.** A task that needs judgment becomes a skill, which Claude builds with you using skill-creator. A mechanical task becomes a tested script or tool.
3. **You approve.** Claude shows what it made and installs it only on your yes. From then on, Claude Code uses it.
4. **It improves.** When you have to correct a skill's results, the harness writes an update brief, and Claude offers to update the skill the same way.

If you decline, a background run (every few hours, within $5/month) still learns tasks you repeat 3+ times. It also rates how sessions went. Claude mentions what's ready at the start of your next session. When it can't tell whether a repeated task went well, Claude asks you instead of guessing.

**Learn right away:** say **"learn this"** in any session. Claude asks before installing anything.

**Learn just one part:** in a long session (update the repo, merge a PR, then release), say **"learn the release part"**. Claude picks out the requests that make up that part, and only that part is learned.

**Skills are built with [skill-creator](https://github.com/anthropics/skills).** The harness never writes a skill itself. When a task needs judgment, it prepares a brief from your sessions: what you asked, what you accepted, your corrections, and ready-made test cases. Claude then offers to build the skill with skill-creator, together with you. Scripts and tools are drafted and tested by the harness. The installer adds skill-creator if Claude Code doesn't already have it.

**Claude uses what it learned.** In Claude Code:
- skills load from `~/.claude/skills/`
- tools appear through the MCP server
- installed scripts are listed for Claude at the start of every session, with the exact command to run them

The Claude desktop and web apps don't load these.

**Muscle memory:** a skill you use often and accurately, for a task that's mechanical, gets proposed as a tool (or a script: `refine.py promote <skill> --as script`, or `"promote_as": "script"` in `store/config.json` for the background run). Claude then just calls it instead of reasoning through the task each time. If it starts failing, `refine.py demote` brings the skill back. Tasks that need judgment stay skills.

You can also do it all by hand. Run these from `~/claude-learning-harness` (on Windows, use `python` instead of `python3`):

| Command | What it does |
|---|---|
| `python3 review/cli.py proposals` | drafts waiting for you |
| `python3 review/cli.py review <name>` | the evidence, code and checks for one draft |
| `python3 review/cli.py approve <name>` | install it |
| `python3 review/cli.py adopt <name>` | check a skill built with skill-creator from a brief, making it a draft |
| `python3 review/cli.py reject <name> --reason "..."` | drop it; that task won't be proposed again |
| `python3 review/cli.py rate last accepted` | rate your last session yourself (`accepted`, `edited` or `redone`); this overrides the automatic rating |
| `python3 refine/refine.py learn` | learn from your last session now (what "learn this" runs) |
| `python3 refine/refine.py learn --list` | number the session's requests |
| `python3 refine/refine.py learn --requests 7-9 --focus "..."` | learn only that part of the session |
| `python3 refine/refine.py report` | what's learned: uses, accuracy, and what's ready to become automatic |
| `python3 refine/refine.py auto-status` | automatic learning on/off, this month's spend, what's waiting |
| `python3 registry/install.py list` | what's installed |
| `python3 registry/install.py uninstall <name>` | remove an installed item |

## Updates

What changed in each version: [CHANGELOG.md](CHANGELOG.md).

Once a day the harness checks GitHub for a new release. When there is one, Claude tells you what changed and asks whether to update. Your learned items are kept. You can also update yourself:

```bash
python3 install.py --update      # install the newest release
python3 install.py --rollback    # go back to the previous version
```

## Turn it off or remove it

- Stop automatic learning: set `"enabled": false` under `"auto"` in `store/config.json`.
- Uninstall: `python3 install.py --uninstall`. This removes the hooks, the MCP server and the PATH entry, and keeps your data.
- Stop the update check: set `"check": false` under `"update"` in `store/config.json`.
- Status: `python3 install.py --check`.

Your sessions stay on your machine, in `store/`, which only you can read. What the harness learns goes in `learned/`, its own local git repo: each learned item is a commit you can revert, and none of it is ever pushed with the code.

How it works in detail: [docs/how-it-works.md](docs/how-it-works.md).
