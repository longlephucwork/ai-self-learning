# learning-harness

Turns repeated Claude Code work into reusable skills, scripts and tools. **Nothing installs without explicit human approval.**

Pipeline: CAPTURE → DETECT → CLASSIFY → GENERATE → VALIDATE → APPROVE → REGISTER → REFINE.
Status: **Phases 1–3 (Capture, Detect + classify, Generate + validate) are done.** Phases 4–5 are stubs. Nothing is ever installed automatically.

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
