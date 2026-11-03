# learning-harness

Turns repeated Claude Code work into reusable skills, scripts and tools. **Nothing installs without explicit human approval.**

Pipeline: CAPTURE → DETECT → CLASSIFY → GENERATE → VALIDATE → APPROVE → REGISTER → REFINE.
Status: **All five phases are done**, plus the MCP tool server that lets Claude find and call approved tools by itself. Nothing is ever installed without your explicit approval.

## Installation

Needs **Python 3.10+**, **git**, and **Claude Code** (`claude` CLI). The installer asks before each change, is safe to re-run, and `--uninstall` reverses it.

**Linux / macOS**
```bash
curl -fsSL https://raw.githubusercontent.com/longlephucwork/claude-learning-harness/main/install.sh | bash
```
or from a clone: `git clone https://github.com/longlephucwork/claude-learning-harness.git && cd claude-learning-harness && ./install.sh`

**Windows** (PowerShell)
```powershell
irm https://raw.githubusercontent.com/longlephucwork/claude-learning-harness/main/install.ps1 | iex
```
or from a clone: double-click `install.bat` (or run `install.bat` in a terminal).

The launchers find a suitable Python, clone the repo to `~/claude-learning-harness` (or `$LEARNING_HARNESS_DIR`) if needed, and run `install.py`, which:
1. checks Python, git and the `claude` CLI
2. detects the code sandbox and proves it blocks the network
3. runs a quick offline self-test
4. adds the session-logging hooks to `~/.claude/settings.json`, using the absolute Python path
5. registers the MCP server: `claude mcp add --scope user learned -- <python> mcp_tools/server.py`
6. adds `bin/` to your PATH and sets `LEARNING_HARNESS_PYTHON` (shell rc file on Linux/macOS, user environment on Windows)

```bash
python3 install.py --check        # what is installed (use `python` on Windows)
python3 install.py --uninstall    # undo it; keeps the repo, store/ data and installed artifacts
python3 install.py --yes          # no questions; also: --no-hooks --no-mcp --no-path --skip-tests
```

**Code sandbox per OS.** Generated code (during validation) and learned MCP tools run without network access:

| OS | Sandbox | Notes |
|---|---|---|
| Linux | `unshare -rn` (user + network namespace) | Needs unprivileged user namespaces. Ubuntu 24.04+ restricts them via AppArmor; if the installer warns, allow them (`sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`) or add an AppArmor profile |
| macOS | `sandbox-exec` with a deny-network profile | Built in; Apple marks it deprecated, but it still ships |
| Windows | none | Code refuses to run unless you opt in (the installer asks; `--allow-unsandboxed`). For full isolation install inside **WSL2** with `install.sh` ([WSL test](#testing-the-wsl-install)) |

#### Testing the Linux install

This is the reference platform: these steps have been run on Linux, and every test runs here. To check your machine, open a terminal; run the commands from the repo folder (`~/claude-learning-harness`) unless noted.

**1. Prerequisites.** Each should print a version, and Python must be 3.10+:
```bash
python3 --version; git --version; claude --version
```

**2. The sandbox works for your user.** This should print `0`:
```bash
unshare -rn true; echo $?
```
If it fails (`Operation not permitted`), unprivileged user namespaces are disabled:
- **Ubuntu 23.10+ / 24.04+** (AppArmor restriction): `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. To keep it across reboots: `echo 'kernel.apparmor_restrict_unprivileged_userns=0' | sudo tee /etc/sysctl.d/60-userns.conf`
- **Older Debian kernels**: `sudo sysctl -w kernel.unprivileged_userns_clone=1`
- **Inside Docker or another container**: the container's seccomp profile usually blocks it. Run the harness on the host, or start the container with user namespaces allowed.

Without it the installer still works, but validation and MCP tools refuse to run code unless you opt in.

**3. Run the installer.** Run `./install.sh`, or the `curl ... | bash` one-liner above. Expected:
- `[ok]` for Python, git and the Claude Code CLI
- `[ok] code sandbox: unshare (network blocked for generated code and tools)`
- `[ok] self-test: OK`
- `[ok]` for the hooks (`~/.claude/settings.json`), the MCP server, and the PATH (`~/.bashrc`, `~/.zshrc`, `~/.config/fish/config.fish` or `~/.profile`, depending on `$SHELL`)

**4. Status.** Open a **new** terminal first (or `source` your rc file). Expect `[ok]` for everything, including the sandbox, `hooks installed`, `MCP server `learned` registered` and `bin/ on PATH`:
```bash
python3 install.py --check
```

**5. MCP server.** Run this outside the repo folder, so the repo's own `.mcp.json` doesn't answer instead. Expect `Scope: User config (available in all your projects)`, `Status: √ Connected` and your Python as the command:
```bash
cd ~ && claude mcp get learned
```

**6. PATH and the script wrapper.** Expect `.../claude-learning-harness/bin/csv-to-json`, the `usage: csv_to_json.py ...` line, and your Python's path:
```bash
command -v csv-to-json
csv-to-json --help
echo "$LEARNING_HARNESS_PYTHON"
```

**7. Session logging.** Start `claude` in any folder, send one message, then exit (`/exit`). Back in the repo folder, the session should appear at the top:
```bash
python3 review/cli.py list -n 3
```
If it doesn't, check the hook command in `~/.claude/settings.json` (the quoted Python path followed by the quoted `hooks/log_session.py` path) and `store/hook_errors.log`.

**8. Tests.** Every test runs on Linux, nothing is skipped, and they take about 25 seconds:
```bash
python3 -m unittest tests.test_sandbox -v      # 8 tests, starts with: platform=linux backend=unshare
python3 -m unittest discover -s tests -t .     # the full suite
```

**9. Uninstall.** Expect `[ok]` lines for removing the hooks, the MCP server and the PATH block from your rc file:
```bash
python3 install.py --uninstall
```

If a step fails, please open an issue with the output of steps 2–4 and 8, plus `cat /etc/os-release | head -3` and `uname -r`.

#### Testing the macOS sandbox

The macOS sandbox (`sandbox-exec` with the profile `(version 1)(allow default)(deny network*)`) is covered by unit tests but hasn't been run on a real Mac yet. To check it on yours, run from the repo folder:

**1. The sandbox tool exists and accepts the profile.** This should print `0`:
```bash
sandbox-exec -p '(version 1)(allow default)(deny network*)' /usr/bin/true; echo $?
```

**2. It blocks the network, and only inside the sandbox.** Run the same connection without the sandbox first, so a Mac that is simply offline can't look like a pass:
```bash
python3 -c "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); print('open')"
sandbox-exec -p '(version 1)(allow default)(deny network*)' \
  python3 -c "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); print('open')"
```
Expected: the first prints `open`; the second fails with `PermissionError` / `Operation not permitted` (or another `OSError`) instead of printing `open`.

**3. The harness picks it up.** Expected output includes `code sandbox: sandbox-exec (network blocked for generated code and tools)`:
```bash
python3 install.py --check
```

**4. The sandbox test suite passes.** All 8 tests should pass, including `platform=darwin backend=sandbox-exec`:
```bash
python3 -m unittest tests.test_sandbox -v
```
It checks that, inside the sandbox:
- outbound TCP and DNS are blocked
- files can still be written in the working folder (tools need this)
- secret environment variables don't leak in
- the CPU limit stops a runaway loop
- an MCP tool call runs inside the sandbox

**5. Optional: the full suite.** On macOS the memory-limit check is skipped, because macOS doesn't enforce `RLIMIT_AS`:
```bash
python3 -m unittest discover -s tests -t .
```

If a step fails, please open an issue with the output of steps 1–4 and `sw_vers`. The most likely differences on some macOS versions are DNS (`test_dns_is_blocked`) and an extra environment variable (`test_environment_is_scrubbed`).

#### Testing the Windows install

The Windows launchers and the Windows-specific code have unit tests but haven't been run on a real Windows machine yet. That code covers the user PATH, `.cmd` wrappers, file locking and UTF-8 I/O. To check it on yours, use PowerShell; run the commands from the repo folder unless noted.

**1. Prerequisites.** Each should print a version, and Python must be 3.10+:
```powershell
py -3 --version; git --version; claude --version
```
If `python` opens the Microsoft Store instead of running, turn off its alias in *Settings > Apps > Advanced app settings > App execution aliases*.

**2. Run the installer.** Run `.\install.bat`, or the `irm ... | iex` one-liner above. Expected:
- `[ok]` for Python, git and the Claude Code CLI
- `[warn] no code sandbox on this system: native Windows has no equivalent; WSL gives you the Linux sandbox`
- the question `Allow running code without network isolation? [y/N]`: answer **N** for this test, and expect `[warn] left disabled ...`
- `[ok] self-test: OK`, then `[ok]` for the hooks, the MCP server and the PATH

**3. Status.** Open a **new** terminal first, so the PATH change applies. Expect `hooks installed`, `MCP server `learned` registered` and `bin/ on PATH`:
```powershell
python install.py --check
```

**4. MCP server.** Expect `Scope: User config`, `Status: √ Connected` and the full path of `python.exe` as the command:
```powershell
claude mcp get learned
```

**5. PATH and the script wrapper.** Expect `...\claude-learning-harness\bin\csv-to-json.cmd`, the `usage: csv_to_json.py ...` line, and the path of `python.exe`:
```powershell
where.exe csv-to-json
csv-to-json --help
$env:LEARNING_HARNESS_PYTHON
```

**6. Session logging.** Start `claude` in any folder, send one message, then exit (`/exit`). Back in the repo folder, the session should appear at the top of the list:
```powershell
python review\cli.py list -n 3
```
If it doesn't, check the hook command (it should be the quoted full `python.exe` path followed by the quoted `hooks/log_session.py` path) and `store\hook_errors.log`:
```powershell
Get-Content $env:USERPROFILE\.claude\settings.json
```

**7. Tests.** All of these should pass. The network-isolation checks in `tests.test_sandbox` report as skipped, which is expected without a sandbox:
```powershell
python -m unittest tests.test_phase1 tests.test_phase2 tests.test_sandbox tests.test_install -v
```
The full suite (`python -m unittest discover -s tests -t .`) should also pass, with the Phase 3–5 and MCP tests skipped: they run generated code, which needs the Linux/macOS sandbox.

**8. Uninstall.** Expect `[ok]` lines for removing the hooks, the MCP server and the PATH entry; `LEARNING_HARNESS_PYTHON` is removed too:
```powershell
python install.py --uninstall
```

If a step fails, please open an issue with the output of steps 2–4 and 7, plus `[Environment]::OSVersion` and `py -3 --version`. The most likely trouble spots are the PATH update (step 5), how Claude Code on Windows runs hook commands (step 6), and user folders with spaces or non-ASCII characters in the name.

**Full sandbox on Windows: use WSL.** See [Testing the WSL install](#testing-the-wsl-install) below. You get the `unshare` sandbox and every test runs.

#### Testing the WSL install

WSL (Windows Subsystem for Linux) runs a real Linux, so this is the Linux install, plus a few WSL-specific checks. The installer detects WSL and prints `Linux (WSL2)` in its first line. It has unit tests for the WSL checks but hasn't been run on a real WSL machine yet.

**1. WSL2, not WSL1.** In **PowerShell**, check that your distro's `VERSION` column says `2`:
```powershell
wsl -l -v
```
- If it says `1`, convert it: `wsl --set-version <distro> 2`. WSL1 has no network namespaces, so the sandbox can't work there, and the installer warns about it.
- No distro yet? `wsl --install` (installs Ubuntu; reboot when asked).

**Everything from here runs inside WSL**, in the Ubuntu (or other distro) terminal, not PowerShell.

**2. Linux tools, not the Windows ones.** WSL puts Windows programs on its PATH, so check where each one comes from:
```bash
command -v python3 git claude
```
None of the three paths may start with `/mnt/`; those are the Windows programs. If `claude` is missing or under `/mnt/c/...`, install Claude Code inside WSL, the same way as on Linux. The installer refuses a Windows-side `claude` or Python: it would register the MCP server in the Windows config with Linux paths, and that can't work. Missing tools: `sudo apt install python3 git`.

**3. The sandbox works.** This should print `0`:
```bash
unshare -rn true; echo $?
```
If it doesn't, see step 2 of the Linux section: Ubuntu 24.04's AppArmor restriction can apply in WSL too.

**4. Install into the Linux filesystem.** Run the Linux one-liner from your WSL home folder:
```bash
cd ~ && curl -fsSL https://raw.githubusercontent.com/longlephucwork/claude-learning-harness/main/install.sh | bash
```
It clones to `~/claude-learning-harness`. Don't use a clone under `/mnt/c/...`: the Windows drive is slow from WSL, and file permissions misbehave there (the installer warns). Expected output:
- a first line containing `Linux (WSL2)`
- `[ok]` for Python, git and the Claude Code CLI
- `[ok] code sandbox: unshare (network blocked ...)`
- `[ok] self-test: OK`
- `[ok]` for the hooks, the MCP server and the PATH

**5. Then steps 4–9 of the Linux section.** Run them inside WSL, from `~/claude-learning-harness`: `install.py --check`, `claude mcp get learned` (from `~`), the PATH and wrapper, session logging, the tests (all run, nothing skipped), and uninstall.

**6. Which Claude Code sees it.** The hooks and the MCP server are installed for the Claude Code that runs **inside WSL**:
- in the WSL terminal: `claude`
- in VS Code: open the folder with the **WSL** extension (*WSL: Connect to WSL*), so Claude Code runs in WSL

A Claude Code started natively on Windows (PowerShell, or VS Code without WSL) uses the Windows config and won't log sessions or see the `learned` tools. That's expected. Use the Windows install for that, without a sandbox.

If a step fails, please open an issue with the output of steps 1–4, plus `uname -r` (run inside WSL) and `wsl --version` (run in PowerShell).

Resource limits (CPU, memory, file size) apply on Linux and macOS. The environment is scrubbed and there's a timeout everywhere. The static safety scan rejects network code on every OS.

**Your data stays local.** `store/` (sessions, ratings, usage) is git-ignored, so a fresh install starts with an empty store. Learned artifacts (`scripts/`, `mcp_tools/tools/`, `registry.json`, `patterns/`, `generated/`) are in git and come with the clone.

## Phase 1: Capture

| Piece | What it does |
|---|---|
| `hooks/log_session.py` | Hook entry point. It reads the hook JSON from stdin, parses the transcript, redacts secrets, and writes `store/sessions/<id>/session.json` plus a redacted `transcript.jsonl.gz`. |
| `hooks/install_hooks.py` | Merges the hooks into user or project `settings.json`. It is idempotent and has `--uninstall`. |
| `harness_lib/transcript.py` | Lenient JSONL parser. It extracts user requests, tool steps (input, error flag, result preview), files touched and the final result. Sidechain/subagent lines are counted but not mixed into the main flow. |
| `harness_lib/redact.py` | Pattern-based redaction plus a literal-value check against secret-looking env vars. It runs on everything before storage. |
| `harness_lib/store.py` | Plain-file store: no database, no server. JSON/JSONL only (layout below). |
| `review/cli.py` | `list`, `show`, `rate`, `stats`. |

### Storage layout (plain files)
```
store/                                 # git-ignored: personal session data
  sessions/<session_id>/
    session.json         # metadata, requests, tool steps, files touched, final result
    transcript.jsonl.gz  # redacted copy of the full transcript
    ratings.jsonl        # append-only outcome history; the last line wins
    summary.json         # Phase 2 structured summary (written only by detect/)
  hook_events.jsonl      # append-only log of Stop / SessionEnd / backfill events
  hook_errors.log
```
Only the hook writes `session.json`, and only `review/cli.py rate` writes `ratings.jsonl`, so they never race. `session.json` is updated under a per-session `flock` and replaced by atomic rename. Appends to the JSONL files use one `O_APPEND` write per line. Everything can be read with `cat`, `jq` or an editor.

### Hook design (checked against the Claude Code hooks docs, Nov 2026)
- **Stop** fires after every turn and ingests the whole session again (idempotent). Data survives even when a session is killed without a clean exit, which is common in cloud containers. It takes `last_assistant_message` from the payload because the docs say the transcript may lag at Stop time. It skips when `stop_hook_active` is true.
- **SessionEnd** records `ended_at` and `reason` (`clear|resume|logout|prompt_input_exit|other`). SessionEnd can't block, and its default budget is 1.5s, so the hook sets `timeout: 30`.
- The hook always exits 0. Errors go to `store/hook_errors.log`.
- Set `LEARNING_HARNESS_DISABLE=1` to skip logging. Later phases use this for their own `claude -p` calls so the harness doesn't learn from itself.

### Usage
```bash
python3 hooks/install_hooks.py --scope user            # log every project
python3 hooks/install_hooks.py --scope project --dir ~/code/myproj
python3 hooks/log_session.py --backfill                # ingest existing ~/.claude/projects transcripts
python3 review/cli.py list
python3 review/cli.py show last
python3 review/cli.py rate <id-prefix|last> accepted|edited|redone|unknown --note "..."
python3 -m unittest discover -s tests -t .
```
This repo's own `.claude/settings.json` logs sessions run inside the repo through `$CLAUDE_PROJECT_DIR`.

### Data handling
`store/` is git-ignored: session data never goes into git. Thinking-block signatures are dropped from the copies. Tool inputs are truncated to 2000 chars and result previews to 500 chars in the DB. The full redacted transcript is kept in the gzip copy.

## Phase 2: Detect + classify

```bash
python3 detect/find_patterns.py run                  # summarize new/changed sessions, then cluster
python3 detect/find_patterns.py list                 # patterns, candidates first
python3 detect/find_patterns.py show csv-to-json-conversion
python3 detect/find_patterns.py summary <session>
python3 detect/find_patterns.py cluster --method family --success accepted   # no LLM, stricter
```

| Piece | What it does |
|---|---|
| `detect/summarize.py` | Builds a compact digest from `session.json` (max 24k chars) and asks `claude -p` for `{intent, task_family, inputs, outputs, steps, deterministic, tools_used, injection_suspected}`. Writes `store/sessions/<id>/summary.json`. |
| `detect/cluster.py` | Groups summaries into `patterns/<key>.json` (git-tracked). `--method claude` (default) merges near-duplicates and reuses existing keys; `--method family` groups by slug with no LLM. |
| `harness_lib/claude_cli.py` | The single locked-down way the harness calls Claude. |
| `harness_lib/schema_check.py` | Local re-validation of every model response before anything is written. |

**Cost:** summaries use `claude-haiku-5-5` by default (`--model` or `$LEARNING_HARNESS_MODEL` to change). That's about $0.001 per session. A session is re-summarized only when its content fingerprint (`content_hash` in `session.json`) changes, so `Stop` re-ingests don't cost anything. Sessions still in progress (no `SessionEnd`, active in the last 30 min) are skipped.

**Rules enforced in code, not by the model:**
- *Success* = latest rating in `--success` (default `accepted,edited`). A pattern is a `candidate` at `--min-success` (default 3) successes.
- Sessions with `injection_suspected` are listed under `excluded_injection` and never counted.
- If fewer than 50% of a pattern's sessions are deterministic, a `script`/`tool` suggestion is overridden to `skill`.
- Sessions the model leaves out of every cluster are recovered via the family grouping, never dropped.
- Patterns with status `proposed`, `rejected`, `installed` or `retired` keep that status and any extra fields across re-clustering. Unlocked patterns that disappear are deleted.

**Untrusted-input handling:** every model call runs with no tools (`--tools ""`), in an empty temp dir, with `--no-session-persistence`, a JSON schema, a budget cap and `LEARNING_HARNESS_DISABLE=1`. Session data is wrapped in random-nonce delimiters and declared untrusted in the system prompt. Detecting injections is best-effort: the model may miss one. That's why the harness doesn't rely on it alone. The other layers are the no-tools sandbox, excluding flagged sessions from counts, and the Phase 4 human review.

## Phase 3: Generate + validate

```bash
python3 generate/propose.py csv-to-json-conversion   # draft + validate (candidate patterns only)
python3 generate/propose.py list                     # drafts and their status
python3 validate/replay.py csv-to-json               # re-run validation after hand-editing a draft
```

A draft lives in `generated/<name>/` (git-tracked) and is **not installed**:
```
proposal.json    type, spec, tests (with provenance), scan flags, evidence session ids, attempts, cost
files/...        the artifact's files
validation.json  provenance, coverage and sandbox results
```
If validation passes, the pattern's status becomes `proposed` and it waits for Phase 4 review.

**Generation** (`generate/propose.py`): Claude (default `claude-sonnet-5-5`, no tools) gets the pattern plus each member session's requests, tool steps and rating notes. These are delimited and marked untrusted, and injection-flagged sessions are left out. It returns `{type, name, description, spec, entrypoint, files[], tests[]}`. If validation fails, the failures go back to Claude for a repair attempt (`--max-attempts`, default 2).

**Checks enforced in code:**
| Check | Rule |
|---|---|
| Structure | Relative paths only (no `..`, absolute or `~`), allowed extensions `.py .md .json .txt`, at most 6 files. A script or tool needs a `.py` entrypoint, and a tool needs a valid JSON Schema. A pattern under 50% deterministic can't become a script or tool. |
| Provenance | A `logged` test's input files and expected outputs must appear in that session's redacted transcript (whitespace and `cat -n` prefixes ignored, JSON compared structurally). A `derived_from_feedback` test needs logged inputs and a rating note on that session. `synthetic` tests are allowed but don't count toward coverage. |
| Coverage | Logged or derived tests must cover at least `min(3, successful sessions)` successful sessions. |
| Safety scan (`harness_lib/scan.py`) | AST/regex flags for network, subprocess/eval, environment or credential access, deletion, and absolute or parent paths. Any of these fails validation and is shown to the reviewer. `write_any` is informational only. |
| Sandbox (`validate/replay.py`) | Each test runs in a fresh temp dir under `unshare -rn` (no network), with `python3 -I`, an environment of only `PATH`/`LANG`/`HOME`, CPU 10s, 512MB memory, 10MB max file size and a 20s timeout. If network isolation is unavailable, it refuses to run unless `--allow-no-netns` is passed. |

Skills are checked statically only: frontmatter `name`/`description`, and any `scripts/...` they reference must exist. Behavioural evaluation of skills needs a model run and isn't implemented yet.

**Note:** tests are built from real session data, so `generated/` contains (redacted) snippets of your sessions, and that folder is committed to git.

## Phase 4: Review + register

```bash
python3 review/cli.py proposals                  # drafts: status, times seen, tests, blocking flags
python3 review/cli.py review csv-to-json         # evidence, rating notes, spec, full code, scan, tests
python3 review/cli.py diff csv-to-json           # vs the installed version (all new on first install)
python3 review/cli.py revalidate csv-to-json     # after hand-editing generated/<name>/files or the tests
python3 review/cli.py approve csv-to-json --note "..."   # asks you to type the name to confirm
python3 review/cli.py reject csv-to-json --reason "..."
python3 registry/install.py list
python3 registry/install.py uninstall csv-to-json --reason "..."
```

**The approval gate (`review/cli.py approve`):**
1. Reads the draft from disk once. Those exact bytes are re-validated and, if approved, installed, so nothing can change in between.
2. Re-runs the full validation (provenance, coverage, sandbox tests, safety scan). Any failure means no install.
3. Shows what will be installed and where, then asks you to **type the draft name**. With no interactive terminal it refuses unless you pass `--yes` explicitly.
4. Refuses if the pattern is `rejected` or `retired`, if the draft is already decided, or if the install root isn't a git repo (`--no-git` overrides).

**Where things go (`registry/install.py`):**
| Type | Installed to | Notes |
|---|---|---|
| script | `scripts/<name>/`, plus the usage wrapper `bin/<name>` | Run `bin/<name> ...` (or add `bin/` to `PATH`) so usage is tracked |
| skill | `skills/<name>/`, plus a symlink `~/.claude/skills/<name>` pointing to it | Git stays the source of truth; reverting the commit reverts the skill |
| tool | `mcp_tools/tools/<name>/` | Served by `mcp_tools/server.py`; Claude sees it as `mcp__learned__<name>` |

`registry.json` records each installed artifact: version, pattern, evidence sessions, file hashes, test results, approval time and note, and a usage counter for Phase 5. It also keeps a `history` of installs and uninstalls.

**Git:** install, reject and uninstall each make one commit (`learned: install script csv-to-json v1`, with the evidence and test counts in the body). The commit covers **only** the paths that action touched, so unrelated work in your tree is never swept in. To undo a learned habit, run `registry/install.py uninstall`, or `git revert` the commit.

**Rejections are remembered:** the pattern is locked as `rejected` with your reason. `propose.py` won't draft it again (except with `--force`), and re-clustering keeps the status.

## Phase 5: Refine

```bash
python3 refine/refine.py usage                    # per installed item: uses, failures, sessions, missed
python3 refine/refine.py report                   # keep / revise / retire / missed / reconsider
python3 refine/refine.py revise csv-to-json       # drafts generated/csv-to-json-v2 from the failures
python3 review/cli.py diff csv-to-json-v2         # then approve or reject as usual
python3 refine/refine.py record csv-to-json fail --note "..."   # a use the harness couldn't see
python3 refine/refine.py cycle --generate 1       # detect -> draft new candidates -> report
```
Nothing in Phase 5 installs, uninstalls or edits an artifact. It writes reports and drafts, and every change still goes through `approve` or `uninstall`.

**Where usage comes from:**
| Source | What it gives |
|---|---|
| `bin/<name>` wrapper -> `store/usage/<name>.jsonl` | One line per run: time, exit code, duration, version, `terminal` or `claude`, and the Claude session id (`CLAUDE_CODE_SESSION_ID`). Never arguments, contents or output. Harness code, not learned code. |
| Session transcripts | Bash steps calling `bin/<name>` or `scripts/<name>/`, and `Skill` calls for installed skills, with the step's error flag. Not double-counted when the wrapper already logged that session. |
| Ratings | Any use inside a session rated `redone` counts as a failure; the rating note is passed on as evidence. |
| `refine.py record` | Manual entries for uses outside both. |

**Recommendations** (thresholds are flags on `report`/`cycle`):
- `revise`: at least `--min-failures` (2) failures and a failure rate of at least `--fail-rate` (30%).
- `retire`: unused for `--stale-days` (60) and the pattern hasn't recurred. Next step: `registry/install.py uninstall`.
- `missed`: later sessions in the same pattern didn't use the artifact. For scripts this usually means Claude doesn't know it exists; `refine.py expose <script>` makes it an MCP tool.
- `reconsider`: a rejected pattern now has at least twice the successes it had when rejected (`propose.py --force` to redraft).

**Revisions** keep the installed name, type and entrypoint. **Every previous test is re-added unchanged as `regression: <name>`**, and any model test with that name is discarded, so a revision can't weaken existing behaviour. The draft goes to `generated/<name>-v<N+1>/`; approving it installs v<N+1> (`learned: update script ...`). The pattern stays `installed` and shows `pending_revision` until then.

**Scheduled runs (optional):** `cycle` costs about $0.001 per new session for summaries, plus about $0.05–0.15 per drafted artifact. A weekly cron entry:
```
17 3 * * 0  cd /path/to/learning-harness && python3 refine/refine.py cycle --generate 1 >> store/cycle.log 2>&1
```
Drafts produced overnight still wait for `review/cli.py approve`.

## MCP tool server (`mcp_tools/server.py`)

**Not a hosted server.** It's a local Python script (standard library only) that Claude Code starts as a child process when a session begins. They talk over stdin/stdout, one JSON-RPC message per line (the MCP stdio transport), and the script exits when the session ends. No port, no daemon, no network.

**Register it once:** the installer does this. By hand:
```bash
claude mcp add --scope user learned -- python3 /path/to/claude-learning-harness/mcp_tools/server.py
```
That makes it available in every project. Sessions inside this repo also pick it up from `.mcp.json` (project scope); interactive sessions ask you to approve project servers the first time. Check with `claude mcp list`.

**What it does:**
- `tools/list` returns the `tool` entries in `registry.json`, with their schema and a description that says how many past sessions they were learned from. The registry is re-read on every request, and a `notifications/tools/list_changed` message is sent when it changes, so approving or uninstalling a tool reaches running sessions.
- `tools/call`:
  1. checks the arguments against the schema (unknown keys and values starting with `-` are rejected)
  2. runs the tool in the project directory (`$CLAUDE_PROJECT_DIR`) without network (see the sandbox table under Installation), with a scrubbed environment, resource limits and a 60s timeout
  3. returns stdout/stderr, with `isError` set on a non-zero exit
  4. appends a usage line (`via: "mcp"`, exit code, Claude's session id, never arguments) to `store/usage/<tool>.jsonl` for Phase 5
- Protocol versions `2025-11-25`, `2025-06-18`, `2025-03-26` and `2024-11-05`.
- Set `LEARNING_HARNESS_MCP_LOG=/path/file` to trace the method names it receives.

**Two kinds of tool:**
| Kind | How it's made | How it runs |
|---|---|---|
| Exposed script | `refine.py expose <script>`: Claude drafts only a schema and an argv template, no code | Arguments are rendered into the installed script's command line, e.g. `["{input}", "{output}", {"when": "delimiter", "argv": ["--delimiter", "{delimiter}"]}]` |
| Generated tool | `propose.py` for a pattern classified as `tool` | `python3 <entrypoint>` with the arguments as one JSON object on stdin |

**Validation:**
- An exposed script passes only if the template renders **exactly** the command line of every validated test of the script, and those tests pass again against the installed script.
- A generated tool's tests must send stdin JSON that matches its schema.
- Both go through the normal `review/cli.py approve`.
- A script can't be uninstalled while a tool wraps it (`--force` overrides).

```bash
python3 refine/refine.py expose csv-to-json        # drafts generated/csv_to_json/
python3 review/cli.py review csv_to_json
python3 review/cli.py approve csv_to_json
```
