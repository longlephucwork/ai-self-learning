# learning-harness

Turns repeated Claude Code work into reusable skills, scripts and tools. **Nothing installs without explicit human approval.**

Pipeline: CAPTURE → DETECT → CLASSIFY → GENERATE → VALIDATE → APPROVE → REGISTER → REFINE.
Status: **Phase 1 (Capture) is done.** The files for Phases 2–5 are stubs.

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
