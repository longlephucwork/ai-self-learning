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

The installer clones the repo to `~/claude-learning-harness` and asks before each change. Per-OS notes (including Docker, and Ubuntu 24.04's sandbox setting) are in [docs/testing-installs.md](docs/testing-installs.md).

## Use

Use Claude Code as usual. In the background, the harness:
1. logs each session
2. rates how it went
3. spots tasks you've done 3+ times
4. drafts and checks an artifact for each one

At most once a day, within $5/month. When a draft is ready, Claude mentions it at the start of your next session. Say yes to see it, then yes again to install it.

You can also do it all by hand. Run these from `~/claude-learning-harness` (on Windows, use `python` instead of `python3`):

| Command | What it does |
|---|---|
| `python3 review/cli.py proposals` | drafts waiting for you |
| `python3 review/cli.py review <name>` | the evidence, code and checks for one draft |
| `python3 review/cli.py approve <name>` | install it |
| `python3 review/cli.py reject <name> --reason "..."` | drop it; that task won't be proposed again |
| `python3 review/cli.py rate last accepted` | rate your last session yourself (`accepted`, `edited` or `redone`); this overrides the automatic rating |
| `python3 refine/refine.py auto-status` | automatic learning on/off, this month's spend, what's waiting |
| `python3 registry/install.py list` | what's installed |
| `python3 registry/install.py uninstall <name>` | remove an installed item |

## Updates

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

Your sessions stay on your machine, in `store/`, which only you can read and which is never committed. Each learned item is a git commit, so you can always revert it.

How it works in detail: [docs/how-it-works.md](docs/how-it-works.md).
