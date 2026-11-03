---
name: release-notes-generation
description: Write user-facing release notes (RELEASE_NOTES.md) for everything changed since the last git release tag. Use when asked to draft release notes or a what's-new summary before shipping.
---

# Release notes since the last tag

The base directory of this skill is shown when it loads; the helper script is `<skill base directory>/scripts/classify_commits.py`.

## Steps

1. Find the tag: `git tag --sort=-v:refname` (use the one the user names, else the newest).
2. List and pre-classify commits:
   `git log --oneline <tag>..HEAD | python3 <skill base directory>/scripts/classify_commits.py -`
   It prints JSON with `category` and a cleaned `message` per commit.
3. Read CHANGES.txt / README.md for context and wording. If a commit message is vague or category is `unknown`, check `git log -p <tag>..HEAD` for what really changed. Commits may only touch CHANGES.txt; then rely on messages.
4. Write RELEASE_NOTES.md (to the project root unless told otherwise).

## Content rules

- Title `# Release Notes`, then `## What's new since <tag>`. Do not invent a version number or date.
- Sections, in this order, only when non-empty: `### Security` (always first), `### New features`, `### Improvements`, `### Bug fixes`, `### Documentation`.
- Write for end users: plain sentences, bold the feature name, mention flags/endpoints in backticks. Say what the user can now do, not how the code changed.
- If a change alters behaviour users must adapt to (e.g. a new input limit), say what to do.
- Omit internal-only changes: refactor, rename, chore, CI, tests, build. Omit dependency upgrades unless users would notice them (then put them under Improvements, described by user benefit).
- Merge duplicate or closely related commits into one bullet.

## Reply

If the user asks for a one-line reply, give one sentence: file saved, what was covered, and what was left out (and that any version number must be added).
