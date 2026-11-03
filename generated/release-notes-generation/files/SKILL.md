---
name: release-notes-generation
description: Draft user-facing release notes (RELEASE_NOTES.md) from git commits since the previous release tag. Use when asked for release notes, what's new, or a changelog for users.
---

# Release notes since last tag

1. Find the last tag: `git tag` (pick the latest release tag, e.g. v1.0).
2. Save commits: `git log --oneline <tag>..HEAD > commits.txt` (write it in a scratch location, not the repo). Also read CHANGES/README and skim `git log -p <tag>..HEAD | head -80` for context.
3. Classify: `python3 scripts/classify_commits.py commits.txt` -> JSON buckets: security, features, improvements, fixes, docs, internal, other.
4. Write RELEASE_NOTES.md (judgment):
   - Title `# Release Notes`, subtitle `## What's new since <tag>`.
   - Order: Security first (if any), New features, Improvements, Bug fixes, Documentation (only if user-relevant).
   - Rewrite each item in plain user language, bold the feature name, mention flags/endpoints in backticks.
   - Leave out refactors, chores, CI, tests, renames and dependency bumps unless users notice them (e.g. behaviour change). Never invent details the commits do not support.
   - If a change needs user action (new limit, renamed flag), say what to do.
   - Triage the `other` bucket by hand.
   - The version number is not known; do not invent it.
5. Reply with ONE line: file saved, what was included, what was left out (and any caveat, e.g. commits only touched the changelog).
