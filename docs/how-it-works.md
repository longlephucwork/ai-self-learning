# How it works

What each phase does, the files it writes, and the rules enforced in code. For installation, see the [README](../README.md).

## Where things live

| Folder | What | In the code repo? |
|---|---|---|
| `store/` | your sessions, ratings, summaries, usage, spend, settings (`config.json`) | no: git-ignored, readable only by you |
| `learned/` | everything the harness learns: `patterns/`, drafts in `generated/`, installed `scripts/`, `skills/`, `mcp_tools/tools/`, the `bin/` wrappers and `registry.json` | no: git-ignored. It's **its own local git repo**, created on first use, so every install, revision, rejection and uninstall is still a commit you can revert |
| everything else | the harness's code | yes: what releases and updates contain |

So pushing or publishing a release never includes anything learned on your machine, and updates only touch code, so they can't conflict with what you've learned. `LEARNING_HARNESS_INSTALL_ROOT` moves `learned/` elsewhere.

## Phase 1: Capture

| Piece | What it does |
|---|---|
| `hooks/log_session.py` | Hook entry point. It reads the hook JSON from stdin, parses the transcript, redacts secrets, and writes `store/sessions/<id>/session.json` plus a redacted `transcript.jsonl.gz`. |
| `hooks/install_hooks.py` | Merges the hooks into user or project `settings.json`. It is idempotent and has `--uninstall`. |
| `harness_lib/transcript.py` | Lenient JSONL parser, resumable from saved progress (`parse_incremental`). It extracts user requests, tool steps (input, error flag, result preview), files touched and the final result. Sidechain/subagent lines are counted but not mixed into the main flow. |
| `harness_lib/redact.py` | Pattern-based redaction plus a literal-value check against secret-looking env vars. It runs on everything before storage. |
| `harness_lib/store.py` | Plain-file store: no database, no server. JSON/JSONL only (layout below). |
| `review/cli.py` | `list`, `show`, `rate`, `stats`. |

### Storage layout (plain files)
```
store/                                 # git-ignored: personal session data
  sessions/<session_id>/
    session.json         # metadata, requests, tool steps, files touched, final result
    transcript.jsonl.gz  # redacted copy of the full transcript, appended to as the session grows
    .transcript.state.json  # how far the source transcript has been copied
    .parse.state.json    # the parser's progress (redacted, as in session.json), so it resumes instead of re-parsing
    ratings.jsonl        # append-only outcome history: your latest rating wins, else the latest inferred one
    summary.json         # Phase 2 structured summary (written only by detect/)
  hook_events.jsonl      # append-only log of Stop / SessionEnd / backfill events
  hook_errors.log
```
Only the hook writes `session.json`, and only `review/cli.py rate` and the automatic rater append to `ratings.jsonl`, so they never race. `session.json` is updated under a per-session `flock` and replaced by atomic rename. Appends to the JSONL files use one `O_APPEND` write per line. Everything can be read with `cat`, `jq` or an editor.

### Hook design (checked against the Claude Code hooks docs, Nov 2026)
- **Stop** fires after every turn and ingests the session again (idempotent). The transcript copy is append-only: only lines added since the last run are redacted and appended, as a new gzip member. If the saved position can't be trusted (the source got shorter or changed before that point, or the copy isn't the size the last run left), the copy is rebuilt from the start. Parsing resumes the same way: the parser saves its progress, and the next run feeds it only the new lines, so a tool result that arrives turns after its tool call still lands on the right step. Data survives even when a session is killed without a clean exit, which is common in cloud containers. It takes `last_assistant_message` from the payload because the docs say the transcript may lag at Stop time. It skips when `stop_hook_active` is true.
- **UserPromptSubmit** compares your prompt with the first request of earlier sessions (word overlap, local, no model call; `harness_lib/similar.py`). On a repeat it tells Claude to offer learning the task once it's done. See *Learning while you work* below.
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
| `detect/cluster.py` | Groups summaries into `learned/patterns/<key>.json`. `--method claude` (default) merges near-duplicates and reuses existing keys; `--method family` groups by slug with no LLM. |
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

A draft lives in `learned/generated/<name>/` and is **not installed**:
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
| Safety scan (`harness_lib/scan.py`) | AST/regex flags for network, subprocess/eval, environment or credential access, deletion, and absolute or parent paths. Any of these fails validation and is shown to the reviewer. Informational only, shown but not blocking: `write_any`; `network_local` (curl, wget or URLs that only reach `localhost`, `127.0.0.1` or `::1`, e.g. a health check); `claude_path` (a mention of a `.claude` folder, such as where a skill is installed). Claude's credentials and settings files (`.claude.json`, `.claude/.credentials.json`, `.claude/settings*.json`) are still `secrets`. A script's `#!/usr/bin/env <interpreter>` first line isn't counted as reading the environment. A skill's `evals/` (test fixtures the harness writes) aren't scanned, in `adopt`, `review` and `approve` alike. |
| Sandbox (`validate/replay.py`) | Each test runs in a fresh temp dir under `unshare -rn` (no network), with `python3 -I`, an environment of only `PATH`/`LANG`/`HOME`, CPU 10s, 512MB memory, 10MB max file size and a 20s timeout. If network isolation is unavailable, it refuses to run unless `--allow-no-netns` is passed. |

**Skills are built with skill-creator, never drafted by the harness** (see [Skill briefs](#skill-briefs-refinebriefpy)). `propose.py` refuses skill patterns and skill-type output. When you `adopt` a skill that skill-creator built, it's validated like this, because a skill is instructions rather than code:
1. **The Claude skill standard.** The rules of `skill-creator`'s `scripts/quick_validate.py`:
   - exactly one `SKILL.md`
   - YAML frontmatter with only `name`, `description`, `license`, `allowed-tools`, `metadata` or `compatibility`
   - `name` kebab-case, at most 64 characters, and matching the skill's folder
   - `description` at most 1024 characters, with no `<` or `>`
   - the body under 500 lines
   
   When `skill-creator` is installed, its own checker also runs on the draft, and must agree. The drafting prompt follows `skill-creator`'s writing guidance: the description says what the skill does and when to use it, and the instructions explain why rather than shouting MUST.
2. **Structure:** frontmatter `name` and `description`; every `scripts/...` it mentions exists. Bundled scripts must be run from the skill's base directory, which Claude Code shows when the skill loads, never by a bare relative path (Claude runs commands from the project folder).
3. **Bundled scripts are tested** in the sandbox, like scripts. Each test names its `script`. Their expected outputs are usually intermediate data that never appears in the logs, so these tests are normally `synthetic`. They must pass, but they aren't evidence for the skill.
4. **Judged checks** (`validate/judge.py`) are the evidence. For each successful session (up to 5), a model with no tools sees the skill, the session's request, steps and accepted result, and your rating note. It decides whether following the skill would produce what you accepted, including the correction in an `edited` note. Every check must pass, for at least `min(3, successful sessions)`. This is a model's judgment, not execution, and `review` shows each verdict with the rules it checked. It runs only after everything cheaper has passed, uses `claude-sonnet-5-5` by default (`$LEARNING_HARNESS_JUDGE_MODEL`), and results are cached by content, so re-validating or approving an unchanged draft costs nothing.

**Note:** tests are built from real session data, so drafts contain (redacted) snippets of your sessions. They stay in your local `learned/` repo and are never pushed.

## Phase 4: Review + register

```bash
python3 review/cli.py proposals                  # drafts: status, times seen, tests, blocking flags
python3 review/cli.py review csv-to-json         # evidence, rating notes, spec, full code, scan, tests
python3 review/cli.py diff csv-to-json           # vs the installed version (all new on first install)
python3 review/cli.py revalidate csv-to-json     # after hand-editing learned/generated/<name>/files or the tests
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
| script | `learned/scripts/<name>/`, plus the usage wrapper `learned/bin/<name>` | Run `<name> ...` (the installer puts `learned/bin` on your PATH) so usage is tracked |
| skill | `learned/skills/<name>/`, plus a symlink `~/.claude/skills/<name>` pointing to it | The `learned/` repo stays the source of truth; reverting the commit reverts the skill |
| tool | `learned/mcp_tools/tools/<name>/` | Served by `mcp_tools/server.py`; Claude sees it as `mcp__learned__<name>` |

`learned/registry.json` records each installed artifact: version, pattern, evidence sessions, file hashes, test results, and approval time and note. (Usage is tracked separately, in `store/usage/`.) It also keeps a `history` of installs and uninstalls.

**Git:** install, reject and uninstall each make one commit (`learned: install script csv-to-json v1`, with the evidence and test counts in the body). The commit covers **only** the paths that action touched, so unrelated work in your tree is never swept in. To undo a learned habit, run `registry/install.py uninstall`, or `git revert` the commit.

**Rejections are remembered:** the pattern is locked as `rejected` with your reason. `propose.py` won't draft it again (except with `--force`), and re-clustering keeps the status.

## Phase 5: Refine

```bash
python3 refine/refine.py usage                    # per installed item: uses, failures, sessions, missed
python3 refine/refine.py report                   # keep / revise / retire / missed / reconsider
python3 refine/refine.py revise csv-to-json       # drafts learned/generated/csv-to-json-v2 from the failures
python3 review/cli.py diff csv-to-json-v2         # then approve or reject as usual
python3 refine/refine.py record csv-to-json fail --note "..."   # a use the harness couldn't see
python3 refine/refine.py cycle --generate 1       # detect -> draft new candidates -> report
```
Nothing in Phase 5 installs, uninstalls or edits an artifact. It writes reports and drafts, and every change still goes through `approve` or `uninstall`.

**Where usage comes from:**
| Source | What it gives |
|---|---|
| `learned/bin/<name>` wrapper -> `store/usage/<name>.jsonl` | One line per run: time, exit code, duration, version, `terminal` or `claude`, and the Claude session id (`CLAUDE_CODE_SESSION_ID`). Never arguments, contents or output. Harness code, not learned code. |
| Session transcripts | Bash steps calling `bin/<name>` or `scripts/<name>/`, and `Skill` calls for installed skills, with the step's error flag. Not double-counted when the wrapper already logged that session. |
| Ratings | Any use inside a session rated `redone` counts as a failure; the rating note is passed on as evidence. |
| `refine.py record` | Manual entries for uses outside both. |

**Recommendations** (thresholds are flags on `report`/`cycle`):
- `revise`: at least `--min-failures` (2) failures and a failure rate of at least `--fail-rate` (30%).
- `retire`: unused for `--stale-days` (60) and the pattern hasn't recurred. Next step: `registry/install.py uninstall`.
- `missed`: later sessions in the same pattern didn't use the artifact. For scripts this usually means Claude doesn't know it exists; `refine.py expose <script>` makes it an MCP tool. For skills it means the description doesn't match how you ask. The background run briefs a description fix after 2 missed sessions, or run `refine.py fix-triggers <skill>` to brief it now.
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
claude mcp add --scope user learned -- python3 /path/to/ai-self-learning/mcp_tools/server.py
```
That makes it available in every project. Sessions inside this repo also pick it up from `.mcp.json` (project scope); interactive sessions ask you to approve project servers the first time. Check with `claude mcp list`.

**What it does:**
- `tools/list` returns the `tool` entries in `learned/registry.json`, with their schema and a description that says how many past sessions they were learned from. The registry is re-read on every request, and a `notifications/tools/list_changed` message is sent when it changes, so approving or uninstalling a tool reaches running sessions.
- `tools/call`:
  1. checks the arguments against the schema (unknown keys and values starting with `-` are rejected)
  2. runs the tool in the project directory (`$CLAUDE_PROJECT_DIR`) without network (see the sandbox table in [testing-installs.md](testing-installs.md)), with a scrubbed environment, resource limits and a 60s timeout
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
python3 refine/refine.py expose csv-to-json        # drafts learned/generated/csv_to_json/
python3 review/cli.py review csv_to_json
python3 review/cli.py approve csv_to_json
```

## Automatic learning (`refine/auto.py`)

With automatic learning on (the installer asks; or `store/config.json` → `"auto": {"enabled": true}`), nothing between "you use Claude Code" and "Claude tells you a draft is ready" needs you. Only the approval does.

1. **Trigger.** The hooks check the throttle when a session starts or ends, and after replies. If a run is due, the hook starts `refine.py auto` as a detached background process and returns at once. A lock file prevents two runs at once (a stale lock is replaced after 2 hours).
   - **Session start and end** always check. A terminal window closed without exiting skips `SessionEnd`, and the next `SessionStart` catches up.
   - **After replies** (`Stop`), so a long session, or a window left open all day, isn't left without a run once the wait is over. `Stop` fires after every reply, so it first passes a cheap gate that reads only `store/config.json` and `store/auto_state.json`. That gate skips the check while learning is off, inside the throttle window, or within an hour of a failed run. The full check (budget, new finished sessions, lock) runs at most every 10 minutes.
   - **Every check is recorded** in `store/auto_check.json`: when, which event, and whether it started a run or why not. `refine.py auto-status` shows it, along with when the next run is allowed.
2. **Inferred ratings.** Each finished session you haven't rated gets an outcome from one Haiku call (about $0.001), judged from the session itself: a follow-up correction reads as `edited` and its content becomes the note. Ratings below `min_confidence` (0.6) are stored as `unknown` and don't count. They're stored with `source: inferred`; your own rating always wins, and inferred ratings show as `accepted~` in `review/cli.py list`.
3. **Detect and draft.** New sessions are summarised and grouped, then up to `max_drafts_per_run` new candidates are drafted and validated. A candidate whose draft failed is retried only after `retry_days`.
4. **Tell you.** A `SessionStart` hook reads the validated drafts (local files only, no model call) and adds a short note to Claude's context. Claude mentions the draft once, early in your next session, and offers to show it. It only runs `review/cli.py approve` after your explicit yes. Each draft version is announced once (`store/notified.json`).

**Settings** (`store/config.json`, key `auto`). Change them with `refine.py auto-config` (the option is in brackets), which checks the values. With no options it shows them all, and `--reset NAME` or `--reset all` brings back the defaults. The installer takes `--budget`, `--every`, `--after-sessions` and `--learn-after`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` (the installer offers `true`) | on/off (`--on`, `--off`) |
| `monthly_budget_usd` | `5.0` | background runs stop when this month's spend reaches it (`--budget`) |
| `min_hours_between_runs` | `6` | at most one run per this many hours (`--every`, e.g. `90m`, `12h`, `1d`) |
| `keep_days` | `90` | finished one-off sessions older than this, evidence for nothing, are deleted (`--keep-days`) |
| `min_new_sessions` | `3` | finished sessions needed since the last run (`--after-sessions`) |
| `min_success` | `3` | successful sessions before a task is learned (`--learn-after`; 1 = after one) |
| `max_drafts_per_run` | `1` | caps drafting cost per run (`--max-drafts`) |
| `retry_days` | `7` | wait before re-drafting a candidate that failed |
| `min_confidence` | `0.6` | below this, an inferred rating counts as `unknown` |

**Budget.** Every model call's cost goes to `store/spend.jsonl`, including failed or aborted calls. In background runs each call checks the month's total first, and its own `--max-budget-usd` is capped at what remains, so the cap can't be overshot. Typical spend: about $0.001 per session for rating and summarising, plus $0.05–0.20 per drafted artifact (judged checks included).

```bash
python3 refine/refine.py auto-status     # on/off, spend vs budget, next run, drafts waiting
python3 refine/refine.py auto-config --budget 10 --every 12h --after-sessions 2   # change settings
python3 refine/refine.py auto --force    # run now, ignoring the throttle
tail store/auto.log                      # what the background runs did
```

## Updates (`harness_lib/update.py`)

A **release** is an annotated git tag `vMAJOR.MINOR.PATCH` on `main`. The `VERSION` file in the repo says which version you have. Nothing updates by itself:

1. **Check.** At most once a day (`"update": {"check": true, "interval_hours": 24}` in `store/config.json`; the installer asks), the `SessionEnd` hook starts a detached `install.py --check-update`. It runs `git fetch --tags` and records whether a newer release exists in `store/update_state.json`. That's plain git, with no model call and no GitHub API.
2. **Ask.** The `SessionStart` hook tells Claude, once per version, that the release is available, with its release notes (the tag's message). Claude asks you, and runs `install.py --update --yes` only after your explicit yes.
3. **Apply** (`install.py --update`):
   - Only tags matching `vX.Y.Z` that are reachable from `origin/main` count, so a stray tag on another branch is never offered.
   - Your working tree must be clean; uncommitted edits are never touched.
   - The release is **merged** into your clone, so your `learned: ...` commits stay. A conflict aborts the merge and leaves everything as it was.
   - A self-test runs on the new version; if it fails, the update is undone automatically.
   - The session hooks are refreshed, in case the release changed them.
4. **Roll back** with `install.py --rollback`. This works only if nothing was committed since the update; otherwise it shows the `git revert` command to run.

A fresh install starts at the newest release, not at whatever is on `main` (or on `main` if there are no releases yet). For a fork, set `LEARNING_HARNESS_REPO_URL` before running the installer.

**Publishing a release** (maintainers):
1. Move the `## [Unreleased]` notes in `CHANGELOG.md` under a new `## [X.Y.Z] - date` heading, and put the version in `VERSION`. Commit and push to `main`.
2. Tag it, either on GitHub (**Releases → Draft a new release**, tag `vX.Y.Z` on `main`, label *None*) or with git:
   ```bash
   git tag -a v1.1.0 -m "What changed, in a few lines" && git push origin v1.1.0
   ```
Claude shows users the tag's message as the release notes. A tag made on GitHub has no message, so Claude shows that version's `CHANGELOG.md` section instead.

## Quick learn (`refine/learn.py`)

`refine.py learn [session]` drafts from **one** session right away, with no waiting for 3 repeats. With no argument it uses the current Claude Code session (`CLAUDE_CODE_SESSION_ID`), else your last one. The installer adds a small built-in skill, `~/.claude/skills/learning-harness/SKILL.md` (part of the harness, not learned), so saying **"learn this"** makes Claude run it for you.

1. Asking to learn a session counts as accepting it, so an unrated session is rated `accepted`. A session rated `redone`, or flagged for prompt injection, is refused.
2. The session is summarised, gets its own single-session pattern, and is drafted with the normal validation: tests from its real inputs and outputs, or for a skill, a judged check against it.
3. The draft is labelled "quick learn: one session" in `review`, and still waits for your approval.

### Learning one part of a session (`refine/segment.py`)

A long session often does several jobs in a row: update a repo, merge a PR, then release. To learn just one of them, say so ("learn the release part"). The built-in skill makes Claude number the session's requests with `refine.py learn --list`, pick the ones that make up that part, and run `refine.py learn --requests 86-87 --focus "release a new version"`. You can run the same commands yourself. `--requests` takes `N`, `N-M` or `N-` (to the last request).

- The part becomes a **segment**: a derived session `<session_id>~r86-87` in `store/sessions/`. It holds the chosen requests and every transcript line from the first of them up to, but not including, the next request after the last.
- It's cut from the parent's redacted transcript copy and parsed like any session. So summaries, skill briefs, eval cases, test provenance and drafting see only that part. The summary and the brief say it's part of a longer conversation, and include your `--focus` description.
- Only the segment is rated `accepted`. The rest of the session keeps its own rating, so a session where something else went wrong can still teach its good part.
- The parent session isn't changed, and hooks never re-ingest a segment. A session ID prefix and `last` find whole sessions, never segments.
- Nothing is counted twice. Usage tracking, `review/cli.py stats`, automatic ratings and the background run's new-session count all skip segments. When a session and one of its own segments land in the same pattern, the session is dropped from it.
- Old segments are pruned like any one-off session once they're no longer evidence for anything.

To make the background run do this for every task, set `"min_success": 1` under `"auto"` in `store/config.json` (default 3). It drafts much more, so it costs more.

## Memory paths: short-term and long-term (`refine/synapse.py`)

Repeated work builds a path, like a synapse. It strengthens with practice and fades without it. `refine.py memory` shows every task's path.

- **Strength** (the synapse weight): every successful repeat adds 1. Between repeats it decays exponentially, halving every *stability* days.
- **Stability:** starts at `memory_days` (30). Spaced practice builds it: each repeat on a later day doubles it, up to a year. Repeats on the same day add strength but no stability, as massed practice does.

| Stage | What | How it changes |
|---|---|---|
| **Short-term** | a task seen but not learned yet (a forming or candidate pattern) | fades. Once its strength is below 0.25 it's **forgotten**: the pattern goes, its sessions are no longer grouped or kept as evidence, and they're pruned like any old one-off. |
| **Long-term** | a learned task (briefed, drafted or installed) | consolidation doubles its stability once. Every use of the learned item is **rehearsal**, a repeat that keeps the path strong. |
| **Automatic** | a task done by an installed script or tool | muscle memory (below) |

With the default 30 days and no further repeats:

| Practice | Forgotten after about |
|---|---|
| done once | 2 months |
| 3 times in one afternoon (massed) | 3½ months |
| quarterly, 3 times | 10 months |
| 3 times, a week apart (spaced) | 14 months |

- **Learning still follows the repeat count.** When a task is learned is decided by `min_success` (`--learn-after`), so the memory model never stops a task from being learned. A quarterly task is learned on its third repeat. It decides what's forgotten, and `refine.py report` shows each learned item's path.
- **"Learn this" is never forgotten.** A task you asked to learn isn't forgotten. Learning a forgotten task again brings its sessions back.
- **Settings:** `refine.py auto-config --memory-days N` changes how quickly an unpractised task fades. The background run does the forgetting, right after grouping.

## Muscle memory (`refine/memory.py`)

The harness has two levels, like a person learning a task:

| Level | Artifact | Who does the work |
|---|---|---|
| Thinking | skill | Claude reads the instructions and reasons through the task every time |
| Automatic | MCP tool or script | code does the job; Claude makes one call and doesn't reason through it |

- **Practice:** every use of a skill is tracked. Accuracy is the share of uses whose session you accepted; `edited` and `redone` don't count as accurate. `refine.py report` shows each skill's uses, accuracy and level.
- **Automate:** a skill used at least `promote_min_uses` (5) times, with accuracy of at least `promote_min_accuracy` (0.9), whose uses were mostly mechanical (`promote_min_mechanical`, 0.75, of their sessions summarised as deterministic), is reported as `promote`. The background run can draft it too.
  - `refine.py promote <skill>` drafts a tool whose tests are the real inputs and outputs of those uses. `refine.py promote <skill> --as script` drafts a script instead (run from the shell, listed for Claude at session start). The background run drafts whatever `"promote_as"` says (`"tool"` by default, or `"script"`).
  - Approving it installs the tool or script and retires the skill, so Claude stops loading it.
- **Judgment stays thinking:** a skill whose uses need judgment (writing, deciding) is never promoted. No script can reproduce judgment, and the report says so.
- **Relearn:** a promoted tool or script that keeps failing is reported as `demote`. `refine.py demote <name>` restores the original skill from `learned/` and removes the tool or script, so Claude reasons through the task again.
- **Forgetting:** anything unused for 60 days is suggested for retirement.

The thresholds are in `store/config.json` under `"auto"`. Nothing changes without your approval.

## Skill briefs (`refine/brief.py`)

Every skill is built by **skill-creator**, interactively with you. When a task is worth a skill, the harness writes a brief instead of a skill. That happens when:
- a candidate pattern is of the `skill` type
- you run `refine.py learn` (or say "learn this") on a task that needs judgment
- you run `refine.py revise` on an installed skill

Briefs are written by plain code, with no model call:

```
learned/briefs/<name>/
  BRIEF.md       the captured intent: what repeats, each session's request, the output you accepted and your
                 corrections, the name and trigger phrases, the skill standard, where to write the skill
  brief.json     bookkeeping (pattern, sessions, status: waiting -> adopted)
  evals/<case>/  one test case per session in skill-creator's format (prompt.md, case.yaml, graders/,
                 files/ with the session's real inputs + place-files.sh), ready for its with/without-skill runs
```

1. **Offer.** At your next session start, or straight after "learn this", Claude mentions the brief and offers to build the skill with skill-creator. If skill-creator isn't installed, Claude says so, and the brief waits.
2. **Build.** If you say yes, Claude follows skill-creator's own process in your session, with `BRIEF.md` as the captured intent (its evidence is marked as data, not instructions) and the brief's `evals/` as the test cases. It writes the skill into `learned/generated/<name>/files/`, and never into `~/.claude/skills/` by hand: a copy put there is loaded by Claude Code but isn't tracked, so its corrections never lead to an update, and it blocks the tracked install.
3. **Adopt.** `review/cli.py adopt <name>` checks the skill:
   - the Claude skill standard, plus skill-creator's own `quick_validate.py` when installed
   - the safety scan, before any paid check
   - the judged check against your accepted sessions
   
   It then makes the skill a normal draft. Bundled scripts don't need harness tests, because skill-creator's evals cover them, but they still go through the safety scan.
   If the checks fail, the skill is **stuck**: its task is `briefed`, so the background run doesn't revisit it. The session start note and `refine.py auto-status` list stuck skills with their problems, and repeat weekly until the skill is fixed and adopted.
4. **Approve** as usual. It's linked into `~/.claude/skills/`, and usage tracking, promotion to a tool and retirement work as for any learned item.
   - If `~/.claude/skills/<name>` already holds a folder the harness didn't put there, for example a copy made by hand, `approve` won't overwrite it. `approve <name> --replace-unmanaged`, or a yes when asked in a terminal, moves it to `store/replaced/` (kept, not deleted) and installs the tracked version.
   - Such copies are reported too: the session start note and `auto-status` say when a folder in `~/.claude/skills/` carries a learned task's name but isn't tracked.
   - Saying "learn this" on a session of a task that's already being learned adds the session to that task's evidence, instead of starting a second copy of the task.

Updating an installed skill works the same way, and happens on its own (see *Learning while you work*). By hand, `refine.py revise <skill>` writes an "update this skill" brief for skill-creator's *improving an existing skill* flow, listing the observed failures.

## Learning while you work

The everyday loop needs no commands from you:

1. **You repeat a task.** When your prompt looks like the first request of earlier sessions (word overlap of 45%+ with at least `min_success - 1` earlier sessions not rated `redone`), the `UserPromptSubmit` hook adds a note to Claude's context. Claude does the task, then offers once to make it reusable. This happens at most once per session (`store/suggested.json`), only with automatic learning on.
2. **You say yes.** Claude runs `refine.py learn`, which learns from this session *together with* those earlier ones. A judgment task becomes a skill brief, and Claude builds the skill with skill-creator right there, then runs `review/cli.py adopt`. A mechanical task becomes a validated script or tool draft.
3. **You approve.** Claude shows it and installs it only on your explicit yes. Skills are linked into `~/.claude/skills/`, so Claude Code loads them from then on. Tools appear through the MCP server, and scripts are listed at the start of each session.
4. **It improves.** Every use is tracked. If the user had to correct a skill's results (at least 2 uses failed or were rated `edited`/`redone`, and accuracy is under 80%), the next background run writes an improvement brief listing the corrections. Claude offers at the start of a session to update the skill with skill-creator, then adopt and approve as before. Each skill version is briefed once.
   - **Missed triggers.** If 2 or more later sessions did the skill's task without Claude loading it, the skill's `description` doesn't match how you ask. The brief lists how you asked in those sessions and tells skill-creator to fix the description with its *description optimization*, leaving the body alone unless there are corrections too. It comes with a trigger eval set, `evals/trigger-eval.json`, in skill-creator's format. The *should trigger* queries are your real requests: the missed ones plus the ones that did use the skill. The *shouldn't trigger* queries are requests from your other tasks that share the most words with them, which are near misses to check. `refine.py fix-triggers <skill>` writes this brief by hand, after even one miss.
   - A skill has one brief folder, so when it needs both kinds of fix, one brief covers both. Each reason is briefed once per version.
   - Briefs tell Claude to run skill-creator's test runs with `LEARNING_HARNESS_DISABLE=1`. Those runs start `claude -p`, which the harness would otherwise log as your sessions. A skill that's used often and accurately for a mechanical task is proposed as a tool instead (*Muscle memory*).

If you say no, or ignore the offer, the background run still learns the task once it's repeated `min_success` times, and Claude mentions the result at the start of a later session.

If the task is already learned, the note tells Claude to use the learned item. If a draft or brief is waiting, it tells Claude to mention it.

## How Claude finds what was learned

| Learned item | How Claude Code sees it |
|---|---|
| skill | linked into `~/.claude/skills/<name>`; Claude Code lists it by its description and loads it when a request matches |
| MCP tool | in Claude's tool list as `mcp__learned__<name>`, through the registered `learned` server |
| script | listed at the start of every session by the `SessionStart` hook, with its description and the absolute command to run it, so it works even if Claude Code didn't inherit the PATH change |

This is for Claude Code only; the Claude desktop and web apps don't read `~/.claude/skills` or these hooks.

## Asking instead of guessing (`refine.py confirm`)

Inferred ratings are guesses. A one-off task that ends without any reaction from you often comes out `unknown`, and `unknown` sessions don't count. So a task you've done several times can stay just below the threshold for good. The harness spots these **near misses**: tasks done at least `min_success` times that would qualify if the unclear outcomes were good. They're computed from local files, with no model call.

1. At the start of your next session, Claude asks you once, briefly, whether those sessions went well.
2. Claude records your answer with `refine.py confirm <pattern> --accepted <ids> [--redone <ids>]`. These count as your own ratings, so they override the guesses.
3. If the task now qualifies, it's learned on the spot: a skill brief for skill-creator, or a validated draft for a script or tool. Claude offers to continue right away, and still installs nothing without your yes.

Claude asks once per set of unclear sessions. If you ignore it, it isn't repeated until new sessions of that task appear.

## Forgetting one-offs

- **A task done once isn't a pattern.** Grouping writes no pattern for a one-session group, and removes old ones. The session's summary is kept, so a later repeat is grouped with it. Exceptions: quick-learned patterns, locked ones (briefed, proposed, installed and so on), and `"min_success": 1`.
- **Old one-offs are deleted.** Each background run deletes finished sessions older than `keep_days` (90) that aren't evidence for anything: not in any pattern or brief, and not evidence for an installed item. `refine.py prune --dry-run` shows what would go, and `refine.py prune --days N` runs it by hand.
