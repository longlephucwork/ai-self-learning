# claude-learning-harness

Makes Claude Code learn from your own work. It logs your sessions, notices tasks you keep repeating, and drafts a reusable **script**, **MCP tool** or **skill** for each one, with tests built from your real sessions.

You review every draft. Nothing is installed without your "yes", and every learned change is a git commit you can revert.

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

The installer clones the repo to `~/claude-learning-harness`, then asks before each change:
- session-logging hooks
- the `learned` MCP server
- `bin/` on your PATH

`python3 install.py --check` shows the status; `python3 install.py --uninstall` undoes it. Per-OS notes and step-by-step checks for Linux, macOS, Windows, WSL and Docker are in **[docs/testing-installs.md](docs/testing-installs.md)**.

## How it works

1. **Capture.** Hooks log each Claude Code session to `store/`, with secrets redacted.
2. **Detect.** Claude summarises sessions and groups similar ones. A group with 3+ sessions you rated `accepted` or `edited` becomes a candidate.
3. **Generate.** Claude drafts the artifact plus tests made from the logged inputs and outputs. The tests run in a no-network sandbox.
4. **Review.** You see the evidence, the code, a safety scan and the test results, then approve or reject.
5. **Refine.** Usage is tracked. Failing or unused artifacts get revision or retirement suggestions.

Approved tools are served to Claude through the local MCP server. It isn't a hosted service: Claude Code starts it as a child process. Claude sees each tool as `mcp__learned__<name>` and uses it by itself.

Details, file formats and the rules enforced in code: **[docs/how-it-works.md](docs/how-it-works.md)**.

## Everyday use

Run from the repo folder (on Windows, use `python` instead of `python3`):

| Command | What it does |
|---|---|
| `python3 review/cli.py list` / `rate last accepted` | see logged sessions, rate one (`accepted`, `edited`, `redone`) |
| `python3 refine/refine.py cycle` | find patterns, draft new candidates, write a health report |
| `python3 review/cli.py proposals` | list drafts waiting for you |
| `python3 review/cli.py review <name>` | evidence, code, safety scan and tests for one draft |
| `python3 review/cli.py approve <name>` / `reject <name> --reason "..."` | install it (asks you to confirm) or drop it for good |
| `python3 refine/refine.py report` | which installed items to keep, revise or retire |
| `python3 registry/install.py uninstall <name>` | remove an installed item |

## Safety

- **Nothing installs without you.** `approve` re-runs every check on the exact files, then asks you to type the draft's name.
- **Logs are treated as untrusted.** Model calls run with no tools. Sessions flagged for prompt injection never count. A static scan rejects code that uses the network, runs commands, reads secrets or deletes files.
- **Generated code runs sandboxed**, with no network access: `unshare` on Linux, `sandbox-exec` on macOS. Native Windows has no sandbox, so code runs there only if you opt in; WSL gives you the Linux one.
- **Your data stays local.** `store/` (sessions, ratings, usage) is git-ignored and readable only by you.
- **Everything is versioned.** Every install, revision and rejection is a git commit that touches only its own files.

## Development

```bash
python3 -m unittest discover -s tests -t .    # about 25 s; tests that need the sandbox skip when it's unavailable
```

`hooks/` capture · `detect/` summarise and cluster · `generate/` draft · `validate/` sandboxed replay · `review/` CLI · `registry/` install · `refine/` usage and revisions · `mcp_tools/` MCP server · `harness_lib/` shared code · `docker/` seccomp profile.
